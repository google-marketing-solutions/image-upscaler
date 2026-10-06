# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Stage 1 Dimension Auditor & Engine Cost Matrix Calculator."""

from __future__ import annotations

import asyncio
from typing import Dict, List, Sequence
import urllib.parse

from app.config import ENGINE_CATALOG
from app.config import settings
from app.engines.base import get_image_dimensions_and_format
from app.engines.base import meets_target_resolution
from app.engines.router import MultiEngineRouter
from app.models import CatalogAuditReport
from app.models import EngineCostEstimate
from app.models import FeedInputRow
from app.models import ImageAuditRecord
from app.pipeline.synthetic_catalog import generate_synthetic_product_image
from app.security import inspect_image_magic_bytes
from app.security import validate_external_image_url
import httpx

BROWSER_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


async def fetch_image_bytes(url: str, client: httpx.AsyncClient) -> bytes:
  """Safely download or synthesize image bytes for a single validated URL.

  Includes standard browser headers, origin Referer fallback, and 3-attempt
  exponential backoff retry for transient CDN/network errors (e.g., ReadError,
  ConnectTimeout, 502/503/504).

  Args:
    url: External or synthetic URL to download.
    client: Shared AsyncClient instance.

  Returns:
    Raw validated image bytes.

  Raises:
    RuntimeError: If the HTTP download fails after retries or returns HTTP 4xx+.
    ValueError: If the URL is invalid or payload exceeds maximum byte limits.
  """
  validated_url = validate_external_image_url(url, allow_synthetic_scheme=True)
  if validated_url.startswith("synthetic://"):
    return generate_synthetic_product_image(validated_url)

  parsed = urllib.parse.urlparse(validated_url)
  origin_referer = f"{parsed.scheme}://{parsed.netloc}/"

  last_exc: Exception | None = None
  resp: httpx.Response | None = None

  for attempt in range(3):
    try:
      resp = await client.get(
          validated_url,
          headers={**BROWSER_IMAGE_HEADERS, "Referer": origin_referer},
          follow_redirects=True,
          timeout=20.0,
      )
      if resp.status_code == 403:
        # Retry without Referer in case CDN blocks self-referer hotlink
        resp = await client.get(
            validated_url,
            headers=BROWSER_IMAGE_HEADERS,
            follow_redirects=True,
            timeout=20.0,
        )
      if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
        await asyncio.sleep(0.4 * (attempt + 1))
        continue
      break
    except (httpx.TimeoutException, httpx.TransportError) as exc:
      last_exc = exc
      if attempt < 2:
        await asyncio.sleep(0.4 * (attempt + 1))
        continue
      detail = (
          str(exc).strip()
          or "Remote CDN connection reset or timed out during download"
      )
      raise RuntimeError(f"{type(exc).__name__}: {detail}") from exc

  if resp is None:
    raise RuntimeError(
        f"Failed to download image after 3 attempts ({last_exc})"
    )

  if resp.status_code >= 400:
    raise RuntimeError(
        f"HTTP {resp.status_code} ({resp.reason_phrase or 'Error'}) from "
        f"{parsed.netloc}"
    )

  content = resp.content
  if len(content) > settings.max_image_download_bytes:
    raise ValueError(
        "Image exceeds maximum allowed download size "
        f"({settings.max_image_download_bytes} bytes)"
    )
  inspect_image_magic_bytes(content)
  return content


def build_cost_matrix_estimates(
    images_needing_500: int,
    images_needing_1500: int,
) -> List[EngineCostEstimate]:
  """Compute projected catalog costs across all 5 engines for 500 and 1500px.

  Args:
    images_needing_500: Count of unique images below 500x500.
    images_needing_1500: Count of unique images below 1500x1500.

  Returns:
    List of EngineCostEstimate objects for all configured engines.
  """
  estimates: List[EngineCostEstimate] = []
  for engine_id, spec in ENGINE_CATALOG.items():
    estimates.append(
        EngineCostEstimate(
            engine_id=engine_id,
            display_name=spec.display_name,
            role=spec.role,
            machine_spec=spec.machine_spec,
            zero_hallucination=spec.zero_hallucination,
            images_needing_500=images_needing_500,
            images_needing_1500=images_needing_1500,
            cost_500_batch_usd=round(
                images_needing_500 * spec.cost_500_per_img, 6
            ),
            cost_1500_batch_usd=round(
                images_needing_1500 * spec.cost_1500_per_img, 6
            ),
            cost_500_std_usd=round(
                images_needing_500 * spec.cost_500_std_per_img, 6
            ),
            cost_1500_std_usd=round(
                images_needing_1500 * spec.cost_1500_std_per_img, 6
            ),
            cost_per_10k_500_usd=round(10_000 * spec.cost_500_per_img, 2),
            cost_per_10k_1500_usd=round(10_000 * spec.cost_1500_per_img, 2),
            when_recommended=spec.when_recommended,
        )
    )
  return estimates


async def audit_catalog_feed(
    rows: Sequence[FeedInputRow],
) -> CatalogAuditReport:
  """Perform a Stage 1 Dimension Audit across all deduplicated URLs in a feed.

  Args:
    rows: Sequence of parsed product feed input rows.

  Returns:
    CatalogAuditReport summarizing compliance and engine cost estimates.
  """
  all_refs: List[str] = []
  unique_urls_ordered: List[str] = []
  seen = set()

  for row in rows:
    for u in [row.image_link, *row.additional_image_links]:
      if not u:
        continue
      all_refs.append(u)
      if u not in seen:
        seen.add(u)
        unique_urls_ordered.append(u)

  total_refs = len(all_refs)
  unique_count = len(unique_urls_ordered)
  dedup_savings_pct = (
      round(((total_refs - unique_count) / float(total_refs)) * 100.0, 2)
      if total_refs > 0
      else 0.0
  )

  sem = asyncio.Semaphore(settings.async_io_concurrency)
  results_map: Dict[str, ImageAuditRecord] = {}

  async with httpx.AsyncClient(headers=BROWSER_IMAGE_HEADERS) as client:

    async def _inspect_one(url: str) -> None:
      async with sem:
        try:
          raw = await fetch_image_bytes(url, client)
          w, h, fmt = get_image_dimensions_and_format(raw)
          ok_500 = meets_target_resolution(w, h, 500)
          ok_1500 = meets_target_resolution(w, h, 1500)
          if ok_1500:
            status_label = "compliant_1500"
          elif ok_500:
            status_label = "compliant_500_only"
          else:
            status_label = "non_compliant_sub_500"

          has_text_hint = any(
              kw in url.lower()
              for kw in (
                  "label",
                  "cosm",
                  "spec",
                  "ind",
                  "elec",
                  "box",
                  "bearing",
              )
          )
          rec_engine = MultiEngineRouter.recommend_engine(
              width=w,
              height=h,
              has_text_or_logo=has_text_hint,
              is_hero_sku=False,
              target_px=500,
          )
          results_map[url] = ImageAuditRecord(
              url=url,
              width=w,
              height=h,
              format=fmt,
              meets_500x500=ok_500,
              meets_1500x1500=ok_1500,
              recommended_engine=rec_engine,
              status=status_label,
          )
        except Exception as exc:  # pylint: disable=broad-exception-caught
          results_map[url] = ImageAuditRecord(
              url=url,
              width=0,
              height=0,
              format="UNKNOWN",
              meets_500x500=False,
              meets_1500x1500=False,
              recommended_engine="local-lanczos-canvas",
              status="error",
              error_message=str(exc),
          )

    await asyncio.gather(*(_inspect_one(u) for u in unique_urls_ordered))

  ordered_records = [results_map[u] for u in unique_urls_ordered]
  sub_500 = sum(
      1 for r in ordered_records if r.status == "non_compliant_sub_500"
  )
  between_500_1499 = sum(
      1 for r in ordered_records if r.status == "compliant_500_only"
  )
  compliant_1500 = sum(
      1 for r in ordered_records if r.status == "compliant_1500"
  )
  failed = sum(1 for r in ordered_records if r.status == "error")

  valid_count = max(1, unique_count - failed)
  exposure_pct = round((sub_500 / float(valid_count)) * 100.0, 2)
  needing_500 = sub_500
  needing_1500 = sub_500 + between_500_1499

  return CatalogAuditReport(
      total_skus=len(rows),
      total_image_references=total_refs,
      unique_image_urls=unique_count,
      deduplication_savings_pct=dedup_savings_pct,
      sub_500x500_urls=sub_500,
      sub_500x500_exposure_pct=exposure_pct,
      between_500_and_1499_urls=between_500_1499,
      compliant_1500x1500_urls=compliant_1500,
      failed_urls=failed,
      cost_matrix=build_cost_matrix_estimates(
          images_needing_500=needing_500,
          images_needing_1500=needing_1500,
      ),
      sample_records=ordered_records[:100],
  )
