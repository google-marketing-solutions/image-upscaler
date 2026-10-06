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

"""Public GCS Bucket Uploader, Request CSV Archiver, and Daily Log Updater."""

from __future__ import annotations

import hashlib
import logging
import pathlib
import random
import threading
import time
from typing import Optional, Sequence, Tuple

from app.config import settings
from app.models import ConversionLogEntry
from app.pipeline.feed_io import DAILY_STATUS_LOG_COLUMNS
from app.pipeline.feed_io import serialize_daily_status_log
from app.security import inspect_image_magic_bytes
from app.security import safe_resolve_within_sandbox

logger = logging.getLogger(__name__)
_log_lock = threading.Lock()


class PublicGcsUploader:
  """Uploads upscaled product images, request CSVs, and daily logs to GCS.

  Paths stored in GCS (`gs://<bucket>`):
  1. Generated Images:
     `gs://<bucket>/img/{short_eng}{target_px}/{sha256_hash}.jpg`
     Hosted via public HTTPS:
     `https://storage.googleapis.com/<bucket>/img/{eng}{px}/{hash10}.jpg`
  2. Request Input / Output CSVs:
     `gs://<bucket>/requests/{request_id}/input.csv`
     `gs://<bucket>/requests/{request_id}/output.csv`
  3. Daily Cumulative Conversion Log:
     `gs://<bucket>/status/{YYYYMMDD}/log.csv`
  """

  def __init__(
      self,
      bucket_name: str | None = None,
      prefix: str | None = None,
      local_storage_dir: pathlib.Path | None = None,
      use_mock_gcs: bool | None = None,
  ) -> None:
    self.bucket_name = (bucket_name or settings.public_gcs_bucket).strip()
    self.prefix = (prefix or settings.gcs_object_prefix).strip("/")
    self.local_storage_dir = (
        local_storage_dir or settings.local_storage_dir
    ).resolve()
    self.use_mock_gcs = (
        settings.use_mock_gcs if use_mock_gcs is None else use_mock_gcs
    )
    self._gcs_client = None

  def _get_gcs_client(self):
    if self._gcs_client is None:
      # pylint: disable=g-import-not-at-top,protected-access
      from google.cloud import storage
      from requests.adapters import HTTPAdapter

      client = storage.Client(project=settings.gcp_project_id)
      adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64)
      client._http.mount("https://", adapter)
      client._http.mount("http://", adapter)
      self._gcs_client = client
    return self._gcs_client

  SHORT_ENGINE_CODES = {
      "local-lanczos-canvas": "lc",
      "realesrgan": "esr",
      "gemini-3.1-flash-lite-image": "gfl",
      "gemini-3.1-flash-image": "gf",
      "gemini-3-pro-image": "gp",
  }

  def compute_image_paths(
      self,
      source_url: str,
      target_px: int,
      engine_id: str,
  ) -> Tuple[str, str, str]:
    """Return deterministic GCS and preview paths for an upscaled image.

    Args:
      source_url: Original product image URL.
      target_px: Target square dimension in pixels.
      engine_id: Engine identifier used for upscaling.

    Returns:
      Tuple of `(object_rel_path, public_https_url, local_preview_url)`.
    """
    digest = hashlib.sha256(
        f"{source_url}:{target_px}:{engine_id}".encode("utf-8")
    ).hexdigest()[:10]
    short_eng = self.SHORT_ENGINE_CODES.get(
        engine_id,
        "".join(c for c in engine_id if c.isalnum())[:4] or "img",
    )
    tier_folder = f"{short_eng}{target_px}"
    object_rel_path = f"img/{tier_folder}/{digest}.jpg"
    public_https_url = (
        f"https://storage.googleapis.com/{self.bucket_name}/{object_rel_path}"
    )
    local_preview_url = f"/api/preview/{tier_folder}/{digest}.jpg"
    return object_rel_path, public_https_url, local_preview_url

  def check_cached_image(
      self,
      source_url: str,
      target_px: int,
      engine_id: str,
  ) -> Optional[Tuple[str, str]]:
    """Check if an already-upscaled image exists in GCS.

    Args:
      source_url: Original product image URL.
      target_px: Target square dimension in pixels.
      engine_id: Engine identifier.

    Returns:
      Tuple of `(public_https_url, local_preview_url)` if cached, else None.
    """
    if self.use_mock_gcs or not settings.enable_gcs_image_cache:
      return None
    object_rel_path, public_https_url, local_preview_url = (
        self.compute_image_paths(
            source_url=source_url,
            target_px=target_px,
            engine_id=engine_id,
        )
    )
    try:
      client = self._get_gcs_client()
      bucket = client.bucket(self.bucket_name)
      blob = bucket.blob(object_rel_path)
      if blob.exists():
        return public_https_url, local_preview_url
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.debug(
          "Non-fatal GCS cache check error for %s: %s",
          object_rel_path,
          exc,
      )
    return None

  def upload_image(
      self,
      image_bytes: bytes,
      source_url: str,
      target_px: int,
      engine_id: str,
      content_type: str = "image/jpeg",
  ) -> Tuple[str, str]:
    """Validate and upload image bytes to GCS or local sandbox.

    Args:
      image_bytes: Encoded output image bytes.
      source_url: Original product image URL.
      target_px: Target square dimension in pixels.
      engine_id: Engine identifier.
      content_type: MIME content type for the uploaded blob.

    Returns:
      Tuple of `(public_gcs_https_url, local_preview_path)`.
    """
    inspect_image_magic_bytes(image_bytes)

    object_rel_path, public_https_url, local_preview_url = (
        self.compute_image_paths(
            source_url=source_url,
            target_px=target_px,
            engine_id=engine_id,
        )
    )

    if self.use_mock_gcs:
      local_target = safe_resolve_within_sandbox(
          self.local_storage_dir,
          object_rel_path,
      )
      local_target.parent.mkdir(parents=True, exist_ok=True)
      local_target.write_bytes(image_bytes)
    else:
      client = self._get_gcs_client()
      bucket = client.bucket(self.bucket_name)
      blob = bucket.blob(object_rel_path)
      blob.cache_control = "no-cache, max-age=0"
      blob.upload_from_string(image_bytes, content_type=content_type)

    return public_https_url, local_preview_url

  def upload_request_csv(
      self, request_id: str, kind: str, csv_text: str
  ) -> str:
    """Store `input.csv` or `output.csv` for a request in GCS or sandbox.

    Args:
      request_id: Unique request identifier.
      kind: Either `"input"` or `"output"`.
      csv_text: Raw CSV text content.

    Returns:
      GCS URI (`gs://<bucket>/requests/<request_id>/<kind>.csv`).
    """
    safe_req = "".join(c for c in request_id if c.isalnum() or c in ("-", "_"))
    safe_kind = "input" if kind == "input" else "output"
    rel_path = f"requests/{safe_req}/{safe_kind}.csv"
    gcs_uri = f"gs://{self.bucket_name}/{rel_path}"

    if self.use_mock_gcs:
      local_target = safe_resolve_within_sandbox(
          self.local_storage_dir, rel_path
      )
      local_target.parent.mkdir(parents=True, exist_ok=True)
      local_target.write_text(csv_text, encoding="utf-8")
    else:
      client = self._get_gcs_client()
      bucket = client.bucket(self.bucket_name)
      blob = bucket.blob(rel_path)
      blob.upload_from_string(csv_text, content_type="text/csv; charset=utf-8")

    return gcs_uri

  def read_request_csv(self, request_id: str, kind: str) -> str:
    """Read `input.csv` or `output.csv` for a request from GCS or sandbox.

    Args:
      request_id: Unique request identifier.
      kind: Either `"input"` or `"output"`.

    Returns:
      CSV text content, or empty string if not found.
    """
    safe_req = "".join(c for c in request_id if c.isalnum() or c in ("-", "_"))
    safe_kind = "input" if kind == "input" else "output"
    rel_path = f"requests/{safe_req}/{safe_kind}.csv"

    if not self.use_mock_gcs:
      try:
        client = self._get_gcs_client()
        bucket = client.bucket(self.bucket_name)
        blob = bucket.blob(rel_path)
        if blob.exists():
          return blob.download_as_text(encoding="utf-8")
      except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Could not read %s from GCS: %s",
            rel_path,
            type(exc).__name__,
        )

    local_target = safe_resolve_within_sandbox(self.local_storage_dir, rel_path)
    if local_target.is_file():
      return local_target.read_text(encoding="utf-8")
    return ""

  def append_daily_status_log(
      self,
      date_yyyymmdd: str,
      entries: Sequence[ConversionLogEntry],
  ) -> str:
    """Create or append to `gs://<bucket>/status/{YYYYMMDD}/log.csv`.

    Args:
      date_yyyymmdd: UTC date string in `YYYYMMDD` format.
      entries: Sequence of ConversionLogEntry records to append.

    Returns:
      GCS URI of the daily status log.

    Raises:
      ValueError: If `date_yyyymmdd` contains no digits.
    """
    safe_date = "".join(c for c in date_yyyymmdd if c.isdigit())[:8]
    if not safe_date:
      raise ValueError("Invalid date_yyyymmdd format")

    rel_path = f"status/{safe_date}/log.csv"
    gcs_uri = f"gs://{self.bucket_name}/{rel_path}"

    if not entries:
      return gcs_uri

    with _log_lock:
      if self.use_mock_gcs:
        local_log_path = safe_resolve_within_sandbox(
            self.local_storage_dir, rel_path
        )
        local_log_path.parent.mkdir(parents=True, exist_ok=True)
        existing_content = (
            local_log_path.read_text(encoding="utf-8")
            if local_log_path.is_file()
            else ""
        )
        if not existing_content.strip():
          updated_csv = serialize_daily_status_log(entries, include_header=True)
        else:
          new_rows_text = serialize_daily_status_log(
              entries, include_header=False
          )
          base = (
              existing_content
              if existing_content.endswith("\n")
              else existing_content + "\n"
          )
          updated_csv = base + new_rows_text
        local_log_path.write_text(updated_csv, encoding="utf-8")
        return gcs_uri

      # Production GCS optimistic concurrency (`if_generation_match`) so
      # concurrent instances never overwrite
      client = self._get_gcs_client()
      bucket = client.bucket(self.bucket_name)
      new_rows_no_header = serialize_daily_status_log(
          entries, include_header=False
      )

      for attempt in range(5):
        try:
          blob = bucket.blob(rel_path)
          existing_content = ""
          expected_gen: Optional[int] = 0
          if blob.exists():
            blob.reload()
            expected_gen = blob.generation
            existing_content = blob.download_as_text(encoding="utf-8")

          if not existing_content.strip():
            updated_csv = serialize_daily_status_log(
                entries, include_header=True
            )
          else:
            base = (
                existing_content
                if existing_content.endswith("\n")
                else existing_content + "\n"
            )
            updated_csv = base + new_rows_no_header

          blob.cache_control = "no-store"
          blob.upload_from_string(
              updated_csv,
              content_type="text/csv; charset=utf-8",
              if_generation_match=expected_gen,
          )
          break
        except Exception as exc:  # pylint: disable=broad-exception-caught
          if attempt < 4:
            time.sleep(0.15 * (2**attempt) + random.uniform(0.02, 0.12))
            continue
          logger.warning(
              "Daily log GCS append failed after retries (%s); "
              "uploading fallback.",
              type(exc).__name__,
          )
          try:
            blob = bucket.blob(rel_path)
            blob.cache_control = "no-store"
            blob.upload_from_string(
                updated_csv, content_type="text/csv; charset=utf-8"
            )
          except Exception:  # pylint: disable=broad-exception-caught
            pass

    return gcs_uri

  def read_daily_status_log(self, date_yyyymmdd: str) -> str:
    """Read `gs://<bucket>/status/{YYYYMMDD}/log.csv`.

    Args:
      date_yyyymmdd: UTC date string in `YYYYMMDD` format.

    Returns:
      CSV text content (or header-only CSV if no entries exist yet).

    Raises:
      ValueError: If `date_yyyymmdd` contains no digits.
    """
    safe_date = "".join(c for c in date_yyyymmdd if c.isdigit())[:8]
    if not safe_date:
      raise ValueError("Invalid date_yyyymmdd format")

    rel_path = f"status/{safe_date}/log.csv"
    if not self.use_mock_gcs:
      try:
        client = self._get_gcs_client()
        bucket = client.bucket(self.bucket_name)
        blob = bucket.blob(rel_path)
        if blob.exists():
          return blob.download_as_text(encoding="utf-8")
      except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Could not fetch daily log from GCS: %s", type(exc).__name__
        )

    local_log_path = safe_resolve_within_sandbox(
        self.local_storage_dir, rel_path
    )
    if local_log_path.is_file():
      return local_log_path.read_text(encoding="utf-8")

    return ",".join(DAILY_STATUS_LOG_COLUMNS) + "\n"
