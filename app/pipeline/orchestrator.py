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

"""3-Tier High-Speed Parallel Processing Engine (`orchestrator.py`)."""

from __future__ import annotations

import asyncio
from concurrent import futures
import datetime
import logging
import time
from typing import Any, Dict, List, Optional, Sequence
import uuid

from app.config import ENGINE_CATALOG
from app.config import settings
from app.db import LightweightRequestDB
from app.db import request_db
from app.db import utc_now_iso_and_yyyymmdd
from app.engines.router import engine_router
from app.models import ConversionLogEntry
from app.models import FeedInputRow
from app.models import ProcessedImageMetrics
from app.models import SupplementalFeedRow
from app.models import UpscalePipelineReport
from app.models import UpscaleRequestRecord
from app.pipeline.auditor import BROWSER_IMAGE_HEADERS
from app.pipeline.auditor import fetch_image_bytes
from app.pipeline.feed_io import parse_delimited_feed
from app.pipeline.feed_io import serialize_supplemental_feed
from app.pipeline.gcs_uploader import PublicGcsUploader
import httpx

logger = logging.getLogger(__name__)


def select_shard_rows(
    rows: Sequence[FeedInputRow],
    shard_index: int = 0,
    shard_count: int = 1,
) -> List[FeedInputRow]:
  """Select the deterministic subset of SKU rows assigned to a task shard.

  Args:
    rows: Sequence of all parsed product feed rows.
    shard_index: Zero-based index of the current worker shard.
    shard_count: Total number of worker shards.

  Returns:
    List of FeedInputRow objects assigned to `shard_index`.

  Raises:
    ValueError: If `shard_index` is outside `[0, shard_count)`.
  """
  if shard_count <= 1:
    return list(rows)
  if shard_index < 0 or shard_index >= shard_count:
    raise ValueError(
        f"Invalid shard_index {shard_index} for shard_count {shard_count}"
    )
  return [
      row for idx, row in enumerate(rows) if (idx % shard_count) == shard_index
  ]


async def run_upscale_pipeline(
    rows: Sequence[FeedInputRow],
    target_px: int = 500,
    engine_id: str = "local-lanczos-canvas",
    uploader: Optional[PublicGcsUploader] = None,
    shard_index: Optional[int] = None,
    shard_count: Optional[int] = None,
    request_id: Optional[str] = None,
    db: Optional[LightweightRequestDB] = None,
) -> UpscalePipelineReport:
  """Execute the 3-Tier Parallel Upscaling Pipeline and return a report.

  Args:
    rows: Sequence of FeedInputRow records to process.
    target_px: Target square dimension in pixels (`500` or `1500`).
    engine_id: Upscaling engine identifier.
    uploader: Optional custom PublicGcsUploader instance.
    shard_index: Optional worker shard index override.
    shard_count: Optional total worker shard count override.
    request_id: Optional request identifier.
    db: Optional custom database instance.

  Returns:
    UpscalePipelineReport containing processed rows, metrics, and costs.

  Raises:
    ValueError: If `target_px` or `engine_id` is unsupported.
  """
  start_ts = time.perf_counter()
  if target_px not in (500, 1500):
    raise ValueError("target_px must be 500 or 1500")
  if engine_id not in ENGINE_CATALOG:
    raise ValueError(f"Unsupported engine_id: {engine_id}")

  job_id = request_id or f"req-{uuid.uuid4().hex[:10]}"
  now_iso, date_yyyymmdd = utc_now_iso_and_yyyymmdd()
  active_db = db or request_db

  s_idx = settings.cloud_run_task_index if shard_index is None else shard_index
  s_cnt = settings.cloud_run_task_count if shard_count is None else shard_count
  sharded_rows = select_shard_rows(rows, shard_index=s_idx, shard_count=s_cnt)

  gcs_uploader = uploader or PublicGcsUploader()

  unique_urls: List[str] = []
  seen = set()
  url_to_primary_rows: Dict[str, List[FeedInputRow]] = {}
  for row in sharded_rows:
    url_to_primary_rows.setdefault(row.image_link, []).append(row)
    for url in [row.image_link, *row.additional_image_links]:
      if url and url not in seen:
        seen.add(url)
        unique_urls.append(url)

  spec = ENGINE_CATALOG[engine_id]
  unit_cost = (
      spec.cost_500_per_img if target_px == 500 else spec.cost_1500_per_img
  )

  # Engine-aware concurrency: protect Vertex AI from burst bombardment while
  # keeping local CPU/GPU fast
  if spec.is_vertex_ai:
    effective_io_concurrency = max(
        2,
        min(
            settings.async_io_concurrency,
            settings.vertex_max_concurrency * 2,
        ),
    )
    effective_cpu_threads = max(
        2, min(settings.cpu_worker_threads, settings.vertex_max_concurrency)
    )
    flush_batch_threshold = 4
  else:
    effective_io_concurrency = settings.async_io_concurrency
    effective_cpu_threads = settings.cpu_worker_threads
    flush_batch_threshold = 15

  sem = asyncio.Semaphore(effective_io_concurrency)
  processed_map: Dict[str, ProcessedImageMetrics] = {}
  failed_errors_map: Dict[str, str] = {}
  urls_failed = 0
  live_upscaled = 0
  live_skipped = 0
  incremental_log_buffer: List[ConversionLogEntry] = []
  last_flush_ts = time.perf_counter()
  flush_in_flight = False
  loop = asyncio.get_running_loop()
  http_limits = httpx.Limits(
      max_connections=effective_io_concurrency,
      max_keepalive_connections=min(64, effective_io_concurrency),
  )

  with (
      futures.ThreadPoolExecutor(max_workers=effective_cpu_threads) as cpu_pool,
      futures.ThreadPoolExecutor(
          max_workers=settings.gcs_upload_threads
      ) as gcs_io_pool,
  ):
    async with httpx.AsyncClient(
        headers=BROWSER_IMAGE_HEADERS, limits=http_limits
    ) as http_client:

      async def _maybe_flush_incremental_progress(
          force: bool = False,
      ) -> None:
        nonlocal last_flush_ts, flush_in_flight
        if not incremental_log_buffer:
          return
        now_perf = time.perf_counter()
        if not force:
          if flush_in_flight:
            return
          if (
              len(incremental_log_buffer) < flush_batch_threshold
              and (now_perf - last_flush_ts) < 1.2
          ):
            return

        batch_to_write = list(incremental_log_buffer)
        incremental_log_buffer.clear()
        last_flush_ts = now_perf
        flush_in_flight = True
        snap_upscaled = live_upscaled
        snap_skipped = live_skipped
        snap_failed = urls_failed
        snap_cost = round(snap_upscaled * unit_cost, 6)
        try:
          await loop.run_in_executor(
              gcs_io_pool,
              active_db.record_conversion_logs,
              batch_to_write,
          )
          await loop.run_in_executor(
              gcs_io_pool,
              active_db.update_request_progress,
              job_id,
              snap_upscaled,
              snap_skipped,
              snap_failed,
              snap_cost,
          )
        except Exception as flush_exc:  # pylint: disable=broad-exception-caught
          logger.debug(
              "Non-fatal incremental progress flush error: %s",
              flush_exc,
          )
        finally:
          flush_in_flight = False

      async def _process_single_url(url: str) -> None:
        nonlocal urls_failed, live_upscaled, live_skipped
        async with sem:
          t0 = time.perf_counter()
          try:
            raw_bytes = await fetch_image_bytes(url, http_client)

            engine_out = await loop.run_in_executor(
                cpu_pool,
                engine_router.upscale,
                raw_bytes,
                target_px,
                engine_id,
            )

            public_url, local_preview = await loop.run_in_executor(
                gcs_io_pool,
                gcs_uploader.upload_image,
                engine_out.image_bytes,
                url,
                target_px,
                engine_out.engine_used,
                engine_out.content_type,
            )

            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            orig_dims = (
                f"{engine_out.original_width}x{engine_out.original_height}"
            )
            out_dims = f"{engine_out.output_width}x{engine_out.output_height}"
            is_skip = engine_out.skipped_already_compliant
            is_c2pa = engine_out.c2pa_synthid_preserved
            metrics = ProcessedImageMetrics(
                source_url=url,
                public_gcs_url=public_url,
                local_preview_url=local_preview,
                original_width=engine_out.original_width,
                original_height=engine_out.original_height,
                output_width=engine_out.output_width,
                output_height=engine_out.output_height,
                engine_used=engine_out.engine_used,
                skipped_already_compliant=is_skip,
                processing_ms=elapsed_ms,
                c2pa_synthid_preserved=is_c2pa,
            )

            processed_map[url] = metrics
            if metrics.skipped_already_compliant:
              live_skipped += 1
            else:
              live_upscaled += 1

            for sku_row in url_to_primary_rows.get(url, ()):
              incremental_log_buffer.append(
                  ConversionLogEntry(
                      timestamp_utc=now_iso,
                      date_yyyymmdd=date_yyyymmdd,
                      request_id=job_id,
                      sku_id=sku_row.id,
                      source_image_url=url,
                      converted_gcs_url=metrics.public_gcs_url,
                      engine_id=metrics.engine_used,
                      target_resolution=target_px,
                      original_dimensions=orig_dims,
                      output_dimensions=out_dims,
                      status="SKIPPED_COMPLIANT"
                      if metrics.skipped_already_compliant
                      else "CONVERTED",
                      processing_ms=metrics.processing_ms,
                  )
              )
          except Exception as exc:  # pylint: disable=broad-exception-caught
            urls_failed += 1
            default_err = (
                "Remote CDN connection reset or timeout during image download"
            )
            exc_detail = str(exc).strip() or default_err
            err_reason = (
                exc_detail
                if exc_detail.startswith(type(exc).__name__)
                else f"{type(exc).__name__}: {exc_detail}"
            )
            failed_errors_map[url] = err_reason
            logger.warning(
                "Failed processing URL %s in job %s: %s",
                url,
                job_id,
                err_reason,
            )

            for sku_row in url_to_primary_rows.get(url, ()):
              incremental_log_buffer.append(
                  ConversionLogEntry(
                      timestamp_utc=now_iso,
                      date_yyyymmdd=date_yyyymmdd,
                      request_id=job_id,
                      sku_id=sku_row.id,
                      source_image_url=url,
                      converted_gcs_url="",
                      engine_id=engine_id,
                      target_resolution=target_px,
                      original_dimensions="0x0",
                      output_dimensions="0x0",
                      status="FAILED",
                      processing_ms=0.0,
                      error_message=err_reason,
                  )
              )

          await _maybe_flush_incremental_progress(force=False)

      if unique_urls:
        url_queue: asyncio.Queue[str] = asyncio.Queue()
        for u in unique_urls:
          url_queue.put_nowait(u)

        async def _queue_worker() -> None:
          while not url_queue.empty():
            try:
              next_url = url_queue.get_nowait()
            except asyncio.QueueEmpty:
              break
            try:
              await _process_single_url(next_url)
            finally:
              url_queue.task_done()

        worker_count = max(1, min(len(unique_urls), effective_io_concurrency))
        await asyncio.gather(*(_queue_worker() for _ in range(worker_count)))
        await _maybe_flush_incremental_progress(force=True)

  supplemental_rows: List[SupplementalFeedRow] = []
  daily_log_entries: List[ConversionLogEntry] = []

  for row in sharded_rows:
    primary_metrics = processed_map.get(row.image_link)
    if not primary_metrics:
      # Record failed URL in daily log with explicit error_message per SKU
      err_msg = failed_errors_map.get(
          row.image_link, "Failed to fetch or convert image"
      )
      daily_log_entries.append(
          ConversionLogEntry(
              timestamp_utc=now_iso,
              date_yyyymmdd=date_yyyymmdd,
              request_id=job_id,
              sku_id=row.id,
              source_image_url=row.image_link,
              converted_gcs_url="",
              engine_id=engine_id,
              target_resolution=target_px,
              original_dimensions="0x0",
              output_dimensions="0x0",
              status="FAILED",
              processing_ms=0.0,
              error_message=err_msg,
          )
      )
      if row.image_link.startswith("https://"):
        supplemental_rows.append(
            SupplementalFeedRow(
                id=row.id,
                input_url=row.image_link,
                image_link=row.image_link,
                additional_image_link="",
            )
        )
      continue

    daily_log_entries.append(
        ConversionLogEntry(
            timestamp_utc=now_iso,
            date_yyyymmdd=date_yyyymmdd,
            request_id=job_id,
            sku_id=row.id,
            source_image_url=row.image_link,
            converted_gcs_url=primary_metrics.public_gcs_url,
            engine_id=primary_metrics.engine_used,
            target_resolution=target_px,
            original_dimensions=(
                f"{primary_metrics.original_width}x"
                f"{primary_metrics.original_height}"
            ),
            output_dimensions=(
                f"{primary_metrics.output_width}x"
                f"{primary_metrics.output_height}"
            ),
            status="SKIPPED_COMPLIANT"
            if primary_metrics.skipped_already_compliant
            else "CONVERTED",
            processing_ms=primary_metrics.processing_ms,
        )
    )

    mapped_additional: List[str] = []
    for add_url in row.additional_image_links:
      add_metrics = processed_map.get(add_url)
      if add_metrics:
        mapped_additional.append(add_metrics.public_gcs_url)
        daily_log_entries.append(
            ConversionLogEntry(
                timestamp_utc=now_iso,
                date_yyyymmdd=date_yyyymmdd,
                request_id=job_id,
                sku_id=row.id,
                source_image_url=add_url,
                converted_gcs_url=add_metrics.public_gcs_url,
                engine_id=add_metrics.engine_used,
                target_resolution=target_px,
                original_dimensions=(
                    f"{add_metrics.original_width}x"
                    f"{add_metrics.original_height}"
                ),
                output_dimensions=(
                    f"{add_metrics.output_width}x{add_metrics.output_height}"
                ),
                status="SKIPPED_COMPLIANT"
                if add_metrics.skipped_already_compliant
                else "CONVERTED",
                processing_ms=add_metrics.processing_ms,
            )
        )

    supplemental_rows.append(
        SupplementalFeedRow(
            id=row.id,
            input_url=row.image_link,
            image_link=primary_metrics.public_gcs_url,
            additional_image_link=",".join(mapped_additional),
        )
    )

  daily_log_gcs_uri = gcs_uploader.append_daily_status_log(
      date_yyyymmdd, daily_log_entries
  )
  active_db.record_conversion_logs(daily_log_entries)

  urls_upscaled = sum(
      1 for m in processed_map.values() if not m.skipped_already_compliant
  )
  urls_skipped = sum(
      1 for m in processed_map.values() if m.skipped_already_compliant
  )

  estimated_cost = round(urls_upscaled * unit_cost, 6)
  total_duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)

  return UpscalePipelineReport(
      job_id=job_id,
      target_resolution=target_px,
      engine_id=engine_id,
      shard_index=s_idx,
      shard_count=s_cnt,
      total_skus_processed=len(supplemental_rows),
      unique_urls_evaluated=len(unique_urls),
      urls_upscaled=urls_upscaled,
      urls_skipped_already_compliant=urls_skipped,
      urls_failed=urls_failed,
      estimated_cost_usd=estimated_cost,
      total_duration_ms=total_duration_ms,
      public_gcs_bucket=gcs_uploader.bucket_name,
      daily_log_gcs_uri=daily_log_gcs_uri,
      supplemental_feed_rows=supplemental_rows,
      processed_images=processed_map,
      failed_errors=failed_errors_map,
  )


_background_tasks: set[asyncio.Task[Any]] = set()
_active_request_ids: set[str] = set()


async def _execute_request_pipeline(
    initial_rec: UpscaleRequestRecord,
    rows: Sequence[FeedInputRow],
    csv_content: str,
    gcs_uploader: PublicGcsUploader,
    active_db: LightweightRequestDB,
) -> UpscaleRequestRecord:
  """Run the upscaling pipeline for an initialized request and persist status.

  Args:
    initial_rec: Initial PROCESSING UpscaleRequestRecord.
    rows: Parsed FeedInputRow records.
    csv_content: Raw input CSV text.
    gcs_uploader: PublicGcsUploader instance.
    active_db: LightweightRequestDB instance.

  Returns:
    Completed or failed UpscaleRequestRecord.
  """
  req_id = initial_rec.request_id
  _active_request_ids.add(req_id)
  try:
    report = await run_upscale_pipeline(
        rows=rows,
        target_px=initial_rec.target_resolution,
        engine_id=initial_rec.engine_id,
        uploader=gcs_uploader,
        request_id=req_id,
        db=active_db,
    )
    output_csv = serialize_supplemental_feed(report.supplemental_feed_rows)
    output_gcs_uri = gcs_uploader.upload_request_csv(
        req_id, "output", output_csv
    )
    done_iso, _ = utc_now_iso_and_yyyymmdd()

    final_status = (
        "COMPLETED"
        if (report.urls_upscaled + report.urls_skipped_already_compliant) > 0
        else "FAILED"
    )
    err_summary: Optional[str] = None
    if report.urls_failed > 0:
      first_err = next(
          iter(report.failed_errors.values()), "Failed to fetch image"
      )
      err_summary = f"{report.urls_failed} URL(s) failed ({first_err[:120]})"

    completed_rec = initial_rec.model_copy(
        update={
            "updated_at_utc": done_iso,
            "status": final_status,
            "upscaled_images": report.urls_upscaled,
            "skipped_images": report.urls_skipped_already_compliant,
            "failed_images": report.urls_failed,
            "estimated_cost_usd": report.estimated_cost_usd,
            "duration_ms": report.total_duration_ms,
            "output_gcs_uri": output_gcs_uri,
            "daily_log_gcs_uri": report.daily_log_gcs_uri,
            "error_message": err_summary,
        }
    )
    return active_db.save_request(
        completed_rec,
        input_csv_text=csv_content,
        output_csv_text=output_csv,
    )
  except Exception as exc:  # pylint: disable=broad-exception-caught
    err_iso, _ = utc_now_iso_and_yyyymmdd()
    failed_rec = initial_rec.model_copy(
        update={
            "updated_at_utc": err_iso,
            "status": "FAILED",
            "error_message": str(exc),
        }
    )
    return active_db.save_request(failed_rec, input_csv_text=csv_content)
  finally:
    _active_request_ids.discard(req_id)


def maybe_resume_stale_request(
    rec: UpscaleRequestRecord,
    stale_after_sec: float = 75.0,
    uploader: Optional[PublicGcsUploader] = None,
    db: Optional[LightweightRequestDB] = None,
) -> bool:
  """Resume a stale PROCESSING request in the background if idle.

  Args:
    rec: UpscaleRequestRecord to inspect.
    stale_after_sec: Idle threshold in seconds before auto-resuming.
    uploader: Optional custom PublicGcsUploader instance.
    db: Optional custom database instance.

  Returns:
    True if a background resume task was spawned, else False.
  """
  if rec.status != "PROCESSING" or rec.request_id in _active_request_ids:
    return False

  try:
    ts_str = (rec.updated_at_utc or rec.created_at_utc).replace("Z", "+00:00")
    last_dt = datetime.datetime.fromisoformat(ts_str)
    age_sec = (
        datetime.datetime.now(datetime.timezone.utc) - last_dt
    ).total_seconds()
    if age_sec < stale_after_sec:
      return False
  except Exception:  # pylint: disable=broad-exception-caught
    return False

  active_db = db or request_db
  gcs_uploader = uploader or PublicGcsUploader()
  in_csv, _ = active_db.get_request_csvs(rec.request_id)
  if not in_csv:
    return False

  try:
    rows = parse_delimited_feed(in_csv)
  except Exception:  # pylint: disable=broad-exception-caught
    return False

  now_iso, _ = utc_now_iso_and_yyyymmdd()
  resumed_rec = rec.model_copy(update={"updated_at_utc": now_iso})
  active_db.save_request(resumed_rec, input_csv_text=in_csv)

  _active_request_ids.add(rec.request_id)
  logger.info(
      "Auto-resuming stale PROCESSING job %s (idle %.1fs)",
      rec.request_id,
      age_sec,
  )
  task = asyncio.create_task(
      _execute_request_pipeline(
          initial_rec=resumed_rec,
          rows=rows,
          csv_content=in_csv,
          gcs_uploader=gcs_uploader,
          active_db=active_db,
      )
  )
  _background_tasks.add(task)
  task.add_done_callback(_background_tasks.discard)
  return True


def _initialize_request_record(
    csv_content: str,
    filename: str,
    engine_id: str,
    target_px: int,
    gcs_uploader: PublicGcsUploader,
    active_db: LightweightRequestDB,
) -> tuple[UpscaleRequestRecord, List[FeedInputRow]]:
  """Validate CSV, upload input.csv, and persist initial PROCESSING record.

  Args:
    csv_content: Raw input CSV text.
    filename: Uploaded filename.
    engine_id: Upscaling engine identifier.
    target_px: Target square dimension in pixels.
    gcs_uploader: PublicGcsUploader instance.
    active_db: LightweightRequestDB instance.

  Returns:
    Tuple of `(initial_rec, parsed_rows)`.

  Raises:
    ValueError: If `target_px` or `engine_id` is invalid.
  """
  if target_px not in (500, 1500):
    raise ValueError("target_px must be 500 or 1500")
  if engine_id not in ENGINE_CATALOG:
    raise ValueError(f"Unsupported engine_id: {engine_id}")

  rows = parse_delimited_feed(csv_content)
  unique_urls: set[str] = set()
  for r in rows:
    for u in [r.image_link, *r.additional_image_links]:
      if u:
        unique_urls.add(u)

  spec = ENGINE_CATALOG[engine_id]
  unit_cost = (
      spec.cost_500_per_img if target_px == 500 else spec.cost_1500_per_img
  )
  upfront_est_cost = round(len(unique_urls) * unit_cost, 6)

  req_id = f"job-{uuid.uuid4().hex[:6]}"
  now_iso, date_yyyymmdd = utc_now_iso_and_yyyymmdd()
  input_gcs_uri = gcs_uploader.upload_request_csv(req_id, "input", csv_content)

  initial_rec = UpscaleRequestRecord(
      request_id=req_id,
      created_at_utc=now_iso,
      updated_at_utc=now_iso,
      date_yyyymmdd=date_yyyymmdd,
      filename=filename,
      engine_id=engine_id,
      target_resolution=target_px,
      status="PROCESSING",
      total_skus=len(rows),
      total_images=len(unique_urls),
      estimated_cost_usd=upfront_est_cost,
      input_gcs_uri=input_gcs_uri,
      daily_log_gcs_uri=(
          f"gs://{gcs_uploader.bucket_name}/status/{date_yyyymmdd}/log.csv"
      ),
  )
  active_db.save_request(initial_rec, input_csv_text=csv_content)
  return initial_rec, rows


async def submit_request_async(
    csv_content: str,
    filename: str,
    engine_id: str = "local-lanczos-canvas",
    target_px: int = 500,
    uploader: Optional[PublicGcsUploader] = None,
    db: Optional[LightweightRequestDB] = None,
) -> UpscaleRequestRecord:
  """Register a job as PROCESSING and run upscaling in the background.

  Args:
    csv_content: Raw input CSV text.
    filename: Uploaded filename.
    engine_id: Upscaling engine identifier.
    target_px: Target square dimension in pixels.
    uploader: Optional custom PublicGcsUploader instance.
    db: Optional custom database instance.

  Returns:
    Initial PROCESSING UpscaleRequestRecord.
  """
  active_db = db or request_db
  gcs_uploader = uploader or PublicGcsUploader()
  initial_rec, rows = _initialize_request_record(
      csv_content=csv_content,
      filename=filename,
      engine_id=engine_id,
      target_px=target_px,
      gcs_uploader=gcs_uploader,
      active_db=active_db,
  )
  task = asyncio.create_task(
      _execute_request_pipeline(
          initial_rec=initial_rec,
          rows=rows,
          csv_content=csv_content,
          gcs_uploader=gcs_uploader,
          active_db=active_db,
      )
  )
  _background_tasks.add(task)
  task.add_done_callback(_background_tasks.discard)
  return initial_rec


async def create_and_process_request(
    csv_content: str,
    filename: str,
    engine_id: str = "local-lanczos-canvas",
    target_px: int = 500,
    uploader: Optional[PublicGcsUploader] = None,
    db: Optional[LightweightRequestDB] = None,
) -> UpscaleRequestRecord:
  """Create a request record in Firestore, process images, and persist outputs.

  Args:
    csv_content: Raw input CSV text.
    filename: Uploaded filename.
    engine_id: Upscaling engine identifier.
    target_px: Target square dimension in pixels.
    uploader: Optional custom PublicGcsUploader instance.
    db: Optional custom database instance.

  Returns:
    Completed or failed UpscaleRequestRecord.
  """
  active_db = db or request_db
  gcs_uploader = uploader or PublicGcsUploader()
  initial_rec, rows = _initialize_request_record(
      csv_content=csv_content,
      filename=filename,
      engine_id=engine_id,
      target_px=target_px,
      gcs_uploader=gcs_uploader,
      active_db=active_db,
  )
  return await _execute_request_pipeline(
      initial_rec=initial_rec,
      rows=rows,
      csv_content=csv_content,
      gcs_uploader=gcs_uploader,
      active_db=active_db,
  )


async def _retry_sku_subset_and_recalculate(
    request_id: str,
    target_sku_ids: Optional[Sequence[str]] = None,
    override_engine_id: Optional[str] = None,
    uploader: Optional[PublicGcsUploader] = None,
    db: Optional[LightweightRequestDB] = None,
) -> tuple[UpscaleRequestRecord, List[ConversionLogEntry]]:
  """Re-run SKUs in an existing request, regenerate CSV, and update billing.

  Args:
    request_id: Unique request identifier.
    target_sku_ids: Specific SKU IDs to retry, or None to retry all failed.
    override_engine_id: Optional override engine identifier.
    uploader: Optional custom PublicGcsUploader instance.
    db: Optional custom database instance.

  Returns:
    Tuple of `(updated_request_record, merged_conversion_log_entries)`.

  Raises:
    ValueError: If the request, engine, or target SKU is not found.
  """
  active_db = db or request_db
  gcs_uploader = uploader or PublicGcsUploader()

  rec = active_db.get_request(request_id)
  if not rec:
    raise ValueError(f"Request '{request_id}' not found")

  chosen_engine = (override_engine_id or rec.engine_id).strip()
  if chosen_engine not in ENGINE_CATALOG:
    raise ValueError(f"Unsupported engine_id: {chosen_engine}")

  target_px = (
      rec.target_resolution if rec.target_resolution in (500, 1500) else 1500
  )
  in_csv, _ = active_db.get_request_csvs(request_id)
  in_rows: List[FeedInputRow] = []
  if in_csv:
    try:
      in_rows = parse_delimited_feed(in_csv)
    except Exception:  # pylint: disable=broad-exception-caught
      in_rows = []

  existing_items = active_db.get_conversion_logs_by_request(
      request_id, force_refresh=True
  )
  items_by_sku: Dict[str, ConversionLogEntry] = {
      it.sku_id: it for it in existing_items
  }
  source_url_by_sku: Dict[str, str] = {
      it.sku_id: it.source_image_url
      for it in existing_items
      if it.source_image_url
  }
  for r in in_rows:
    if r.id not in source_url_by_sku and r.image_link:
      source_url_by_sku[r.id] = r.image_link

  if target_sku_ids is not None:
    skus_to_retry: List[str] = []
    for raw_sku in target_sku_ids:
      clean_sku = raw_sku.strip()
      if not clean_sku:
        continue
      if clean_sku not in source_url_by_sku:
        raise ValueError(
            f"SKU '{clean_sku}' not found in request '{request_id}'"
        )
      skus_to_retry.append(clean_sku)
  else:
    skus_to_retry = [
        it.sku_id
        for it in existing_items
        if (it.status or "").upper().startswith("FAILED")
        or (it.status or "").upper() in ("PENDING", "PROCESSING")
    ]

  if not skus_to_retry:
    return rec, existing_items

  now_iso, date_yyyymmdd = utc_now_iso_and_yyyymmdd()
  loop = asyncio.get_running_loop()
  retried_entries: List[ConversionLogEntry] = []

  with (
      futures.ThreadPoolExecutor(
          max_workers=max(1, min(4, len(skus_to_retry)))
      ) as cpu_pool,
      futures.ThreadPoolExecutor(
          max_workers=max(1, min(4, len(skus_to_retry)))
      ) as gcs_io_pool,
  ):
    async with httpx.AsyncClient(headers=BROWSER_IMAGE_HEADERS) as http_client:
      for sku_id in skus_to_retry:
        source_url = source_url_by_sku[sku_id]
        t0 = time.perf_counter()
        try:
          raw_bytes = await fetch_image_bytes(source_url, http_client)
          engine_out = await loop.run_in_executor(
              cpu_pool,
              engine_router.upscale,
              raw_bytes,
              target_px,
              chosen_engine,
          )
          public_url, _ = await loop.run_in_executor(
              gcs_io_pool,
              gcs_uploader.upload_image,
              engine_out.image_bytes,
              source_url,
              target_px,
              engine_out.engine_used,
              engine_out.content_type,
          )
          elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
          entry = ConversionLogEntry(
              timestamp_utc=now_iso,
              date_yyyymmdd=date_yyyymmdd,
              request_id=request_id,
              sku_id=sku_id,
              source_image_url=source_url,
              converted_gcs_url=public_url,
              engine_id=engine_out.engine_used,
              target_resolution=target_px,
              original_dimensions=(
                  f"{engine_out.original_width}x{engine_out.original_height}"
              ),
              output_dimensions=(
                  f"{engine_out.output_width}x{engine_out.output_height}"
              ),
              status="SKIPPED_COMPLIANT"
              if engine_out.skipped_already_compliant
              else "CONVERTED",
              processing_ms=elapsed_ms,
              error_message="",
          )
        except Exception as exc:  # pylint: disable=broad-exception-caught
          default_err = (
              "Remote CDN connection reset or timeout during image download"
          )
          exc_detail = str(exc).strip() or default_err
          err_reason = (
              exc_detail
              if exc_detail.startswith(type(exc).__name__)
              else f"{type(exc).__name__}: {exc_detail}"
          )
          entry = ConversionLogEntry(
              timestamp_utc=now_iso,
              date_yyyymmdd=date_yyyymmdd,
              request_id=request_id,
              sku_id=sku_id,
              source_image_url=source_url,
              converted_gcs_url="",
              engine_id=chosen_engine,
              target_resolution=target_px,
              original_dimensions="0x0",
              output_dimensions="0x0",
              status="FAILED",
              processing_ms=0.0,
              error_message=err_reason,
          )
        retried_entries.append(entry)
        items_by_sku[sku_id] = entry

  active_db.record_conversion_logs(retried_entries)
  gcs_uploader.append_daily_status_log(date_yyyymmdd, retried_entries)

  # Order merged items consistently with input CSV rows if available
  if in_rows:
    ordered_skus = [r.id for r in in_rows if r.id in items_by_sku]
    extra_skus = [k for k in items_by_sku if k not in set(ordered_skus)]
    merged_items = [items_by_sku[k] for k in [*ordered_skus, *extra_skus]]
  else:
    merged_items = sorted(items_by_sku.values(), key=lambda x: x.sku_id)

  # Refresh in-memory cache with the full merged SKU map and updated_at_utc
  # version
  active_db.cache_conversion_logs_for_request(
      request_id=request_id,
      entries=merged_items,
      updated_at_utc=now_iso,
  )

  # Recalculate counts, billing (estimated_cost_usd), and supplemental output
  # CSV
  url_status_map: Dict[str, ConversionLogEntry] = {}
  for it in merged_items:
    key = it.source_image_url or it.sku_id
    prev = url_status_map.get(key)
    if (
        prev is None
        or it.status == "CONVERTED"
        or (prev.status == "FAILED" and it.status != "FAILED")
    ):
      url_status_map[key] = it

  new_upscaled = sum(
      1 for it in url_status_map.values() if it.status == "CONVERTED"
  )
  new_skipped = sum(
      1 for it in url_status_map.values() if it.status == "SKIPPED_COMPLIANT"
  )
  new_failed = sum(
      1
      for it in url_status_map.values()
      if (it.status or "").startswith("FAILED")
  )

  recalculated_cost = 0.0
  for it in url_status_map.values():
    if it.status == "CONVERTED":
      eng_spec = ENGINE_CATALOG.get(it.engine_id) or ENGINE_CATALOG.get(
          rec.engine_id
      )
      if eng_spec:
        unit_rate = (
            eng_spec.cost_500_per_img
            if it.target_resolution == 500
            else eng_spec.cost_1500_per_img
        )
        recalculated_cost += unit_rate
  recalculated_cost = round(recalculated_cost, 6)

  supplemental_rows: List[SupplementalFeedRow] = []
  for it in merged_items:
    if it.converted_gcs_url:
      supplemental_rows.append(
          SupplementalFeedRow(
              id=it.sku_id,
              input_url=it.source_image_url,
              image_link=it.converted_gcs_url,
              additional_image_link="",
          )
      )
    elif it.source_image_url.startswith("https://"):
      supplemental_rows.append(
          SupplementalFeedRow(
              id=it.sku_id,
              input_url=it.source_image_url,
              image_link=it.source_image_url,
              additional_image_link="",
          )
      )

  output_csv = (
      serialize_supplemental_feed(supplemental_rows)
      if supplemental_rows
      else ""
  )
  output_gcs_uri = rec.output_gcs_uri
  if output_csv:
    output_gcs_uri = gcs_uploader.upload_request_csv(
        request_id, "output", output_csv
    )

  final_status = "COMPLETED" if (new_upscaled + new_skipped) > 0 else "FAILED"
  err_summary: Optional[str] = None
  if new_failed > 0:
    first_failed = next(
        (
            it.error_message
            for it in merged_items
            if (it.status or "").startswith("FAILED") and it.error_message
        ),
        "Failed to fetch or convert image",
    )
    err_summary = f"{new_failed} URL(s) failed ({first_failed[:120]})"

  updated_rec = rec.model_copy(
      update={
          "updated_at_utc": now_iso,
          "status": final_status,
          "upscaled_images": new_upscaled,
          "skipped_images": new_skipped,
          "failed_images": new_failed,
          "estimated_cost_usd": recalculated_cost,
          "output_gcs_uri": output_gcs_uri,
          "error_message": err_summary,
      }
  )
  saved_rec = active_db.save_request(
      updated_rec,
      input_csv_text=in_csv or None,
      output_csv_text=output_csv or None,
  )
  return saved_rec, merged_items


async def retry_single_sku(
    request_id: str,
    sku_id: str,
    engine_id: Optional[str] = None,
    uploader: Optional[PublicGcsUploader] = None,
    db: Optional[LightweightRequestDB] = None,
) -> tuple[UpscaleRequestRecord, List[ConversionLogEntry]]:
  """Retry a single SKU inside an existing request and recalculate billing.

  Args:
    request_id: Unique request identifier.
    sku_id: Target SKU identifier to retry.
    engine_id: Optional override engine identifier.
    uploader: Optional custom PublicGcsUploader instance.
    db: Optional custom database instance.

  Returns:
    Tuple of `(updated_request_record, merged_conversion_log_entries)`.
  """
  return await _retry_sku_subset_and_recalculate(
      request_id=request_id,
      target_sku_ids=[sku_id],
      override_engine_id=engine_id,
      uploader=uploader,
      db=db,
  )


async def retry_failed_skus(
    request_id: str,
    engine_id: Optional[str] = None,
    uploader: Optional[PublicGcsUploader] = None,
    db: Optional[LightweightRequestDB] = None,
) -> tuple[UpscaleRequestRecord, List[ConversionLogEntry]]:
  """Retry all failed SKUs inside an existing request and recalculate billing.

  Args:
    request_id: Unique request identifier.
    engine_id: Optional override engine identifier.
    uploader: Optional custom PublicGcsUploader instance.
    db: Optional custom database instance.

  Returns:
    Tuple of `(updated_request_record, merged_conversion_log_entries)`.
  """
  return await _retry_sku_subset_and_recalculate(
      request_id=request_id,
      target_sku_ids=None,
      override_engine_id=engine_id,
      uploader=uploader,
      db=db,
  )
