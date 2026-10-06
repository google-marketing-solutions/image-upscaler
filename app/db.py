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

"""Managed Cloud Database Layer (`app/db.py`) using Google Cloud Firestore.

Guarantees strong consistency across all horizontally scaled Cloud Run Backend
instances:
- Primary Cloud Database: Google Cloud Firestore (`google-cloud-firestore`)
  connected to the Terraform-provisioned named database (`FIRESTORE_DATABASE =
  "gmc-upscaler-db"`).
- Collections:
  1. `upscale_requests` (Document key: `request_id`): Stores request status
     (`QUEUED`, `PROCESSING`, `COMPLETED`, `FAILED`), counts, GCS input/output
     URIs (`gs://<bucket>/requests/<id>/input.csv` and `output.csv`), and daily
     log URI (`gs://<bucket>/status/{YYYYMMDD}/log.csv`).
  2. `conversion_logs`: Stores individual converted image audit records by
     `date_yyyymmdd`.
- Large CSV Payloads: Stored in Google Cloud Storage
  (`gs://<bucket>/requests/<request_id>/*.csv`) so feeds of any size (even
  >10 MB, well beyond Firestore's 1 MiB document cap) are stored reliably and
  retrieved with strong consistency.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.models import ConversionLogEntry
from app.models import FeedInputRow
from app.models import UpscaleRequestRecord
from app.pipeline.gcs_uploader import PublicGcsUploader

logger = logging.getLogger(__name__)

# Firestore inline CSV size threshold (200 KB) to stay well under the 1 MiB
# Firestore document cap
MAX_INLINE_CSV_BYTES = 200_000


class FirestoreRequestDB:
  """Strongly consistent Cloud Firestore database for requests and logs."""

  REQUESTS_COLLECTION = "upscale_requests"
  PAYLOADS_COLLECTION = "upscale_request_payloads"
  LOGS_COLLECTION = "conversion_logs"
  REQUEST_SUMMARY_FIELDS: Tuple[str, ...] = (
      "request_id",
      "created_at_utc",
      "updated_at_utc",
      "date_yyyymmdd",
      "filename",
      "engine_id",
      "target_resolution",
      "status",
      "total_skus",
      "total_images",
      "upscaled_images",
      "skipped_images",
      "failed_images",
      "estimated_cost_usd",
      "duration_ms",
      "input_gcs_uri",
      "output_gcs_uri",
      "daily_log_gcs_uri",
      "error_message",
  )

  def __init__(
      self,
      use_mock_db: Optional[bool] = None,
      database_id: Optional[str] = None,
  ) -> None:
    self.use_mock_db = (
        settings.use_mock_db if use_mock_db is None else use_mock_db
    )
    self.database_id = database_id or settings.firestore_database
    self._firestore_client = None
    self._lock = threading.Lock()
    # In-memory store used only when use_mock_db=True during offline unit
    # tests
    self._mock_requests: Dict[str, Dict[str, Any]] = {}
    self._mock_logs: List[Dict[str, Any]] = []
    # Cache parsed input rows per request_id so 2s live polling never
    # re-reads GCS
    self._in_rows_cache: Dict[str, List[FeedInputRow]] = {}
    # Write-through + short TTL cache of per-SKU ConversionLogEntry maps by
    # request_id
    self._logs_cache_by_req: Dict[
        str, Tuple[float, Dict[str, ConversionLogEntry]]
    ] = {}
    self._logs_cache_updated_at: Dict[str, str] = {}
    # Adaptive TTL write-through cache for list_requests() to eliminate
    # redundant Firestore scans
    self._requests_list_cache: Optional[
        Tuple[float, List[UpscaleRequestRecord]]
    ] = None

  def _get_client(self):
    if self._firestore_client is None:
      from google.cloud import firestore  # pylint: disable=g-import-not-at-top

      self._firestore_client = firestore.Client(
          project=settings.gcp_project_id,
          database=self.database_id,
      )
    return self._firestore_client

  def _upsert_cached_request(self, record: UpscaleRequestRecord) -> None:
    """Update in-memory list_requests cache on write for immediate reads.

    Args:
      record: Updated UpscaleRequestRecord instance.
    """
    with self._lock:
      if self._requests_list_cache is None:
        return
      cached_ts, existing_list = self._requests_list_cache
      updated_list = [
          r for r in existing_list if r.request_id != record.request_id
      ]
      updated_list.append(record)
      updated_list.sort(key=lambda r: r.created_at_utc, reverse=True)
      self._requests_list_cache = (cached_ts, updated_list)

  def cache_conversion_logs_for_request(
      self,
      request_id: str,
      entries: Sequence[ConversionLogEntry],
      updated_at_utc: str = "",
  ) -> None:
    """Update in-memory conversion log cache for `request_id`.

    Args:
      request_id: Unique request identifier.
      entries: Sequence of ConversionLogEntry records.
      updated_at_utc: Optional ISO-8601 timestamp string for cache versioning.
    """
    with self._lock:
      self._logs_cache_by_req[request_id] = (
          time.monotonic(),
          {e.sku_id: self._normalize_log_entry(e) for e in entries},
      )
      if updated_at_utc:
        self._logs_cache_updated_at[request_id] = updated_at_utc

  def save_request(
      self,
      record: UpscaleRequestRecord,
      input_csv_text: Optional[str] = "",
      output_csv_text: Optional[str] = "",
  ) -> UpscaleRequestRecord:
    """Persist or update a request document in Cloud Firestore.

    Args:
      record: UpscaleRequestRecord to persist.
      input_csv_text: Optional input CSV text for inline caching.
      output_csv_text: Optional output CSV text for inline caching.

    Returns:
      Saved UpscaleRequestRecord instance.
    """
    doc_data = record.model_dump()
    payload_data: Dict[str, Any] = {}
    if input_csv_text:
      if len(input_csv_text.encode("utf-8")) <= MAX_INLINE_CSV_BYTES:
        payload_data["input_csv_inline"] = input_csv_text
      elif not self.use_mock_db:
        # pylint: disable=g-import-not-at-top
        from google.cloud import firestore

        payload_data["input_csv_inline"] = firestore.DELETE_FIELD
    if output_csv_text:
      if len(output_csv_text.encode("utf-8")) <= MAX_INLINE_CSV_BYTES:
        payload_data["output_csv_inline"] = output_csv_text
      elif not self.use_mock_db:
        # pylint: disable=g-import-not-at-top
        from google.cloud import firestore

        payload_data["output_csv_inline"] = firestore.DELETE_FIELD

    self._upsert_cached_request(record)

    if self.use_mock_db:
      with self._lock:
        existing = self._mock_requests.get(record.request_id, {})
        merged = {**existing, **doc_data, **payload_data}
        if (
            input_csv_text
            and len(input_csv_text.encode("utf-8")) > MAX_INLINE_CSV_BYTES
        ):
          merged.pop("input_csv_inline", None)
        if (
            output_csv_text
            and len(output_csv_text.encode("utf-8")) > MAX_INLINE_CSV_BYTES
        ):
          merged.pop("output_csv_inline", None)
        self._mock_requests[record.request_id] = merged
      return record

    client = self._get_client()
    doc_ref = client.collection(self.REQUESTS_COLLECTION).document(
        record.request_id
    )
    doc_ref.set(doc_data, merge=True)
    if payload_data:
      payload_ref = client.collection(self.PAYLOADS_COLLECTION).document(
          record.request_id
      )
      payload_ref.set(payload_data, merge=True)
    return record

  def update_request_progress(
      self,
      request_id: str,
      upscaled_images: int,
      skipped_images: int,
      failed_images: int,
      estimated_cost_usd: float,
  ) -> None:
    """Update live progress counters on an in-flight PROCESSING request.

    Args:
      request_id: Unique request identifier.
      upscaled_images: Count of images upscaled so far.
      skipped_images: Count of already-compliant images skipped so far.
      failed_images: Count of failed images so far.
      estimated_cost_usd: Cumulative estimated cost in USD.
    """
    now_iso, _ = utc_now_iso_and_yyyymmdd()
    patch = {
        "updated_at_utc": now_iso,
        "upscaled_images": int(upscaled_images),
        "skipped_images": int(skipped_images),
        "failed_images": int(failed_images),
        "estimated_cost_usd": float(estimated_cost_usd),
    }
    with self._lock:
      if self._requests_list_cache is not None:
        cached_ts, existing_list = self._requests_list_cache
        self._requests_list_cache = (
            cached_ts,
            [
                r.model_copy(update=patch) if r.request_id == request_id else r
                for r in existing_list
            ],
        )

    if self.use_mock_db:
      with self._lock:
        if request_id in self._mock_requests:
          self._mock_requests[request_id].update(patch)
      return

    try:
      client = self._get_client()
      doc_ref = client.collection(self.REQUESTS_COLLECTION).document(request_id)
      doc_ref.set(patch, merge=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.debug(
          "Non-fatal progress update error for %s: %s", request_id, exc
      )

  def get_request(self, request_id: str) -> Optional[UpscaleRequestRecord]:
    """Fetch a single request document by ID from Cloud Firestore."""
    if self.use_mock_db:
      with self._lock:
        raw = self._mock_requests.get(request_id)
        if not raw:
          return None
        return self._dict_to_record(raw)

    client = self._get_client()
    snap = (
        client.collection(self.REQUESTS_COLLECTION).document(request_id).get()
    )
    if not snap.exists:
      return None
    return self._dict_to_record(snap.to_dict() or {})

  def get_request_csvs(self, request_id: str) -> Tuple[str, str]:
    """Retrieve `(input_csv_text, output_csv_text)` from Firestore or GCS.

    Args:
      request_id: Unique request identifier.

    Returns:
      Tuple of `(input_csv_text, output_csv_text)`.
    """
    in_csv = ""
    out_csv = ""
    if self.use_mock_db:
      with self._lock:
        raw_doc = dict(self._mock_requests.get(request_id, {}))
      in_csv = str(raw_doc.get("input_csv_inline") or "")
      out_csv = str(raw_doc.get("output_csv_inline") or "")
    else:
      client = self._get_client()
      payload_snap = (
          client.collection(self.PAYLOADS_COLLECTION).document(request_id).get()
      )
      if payload_snap.exists:
        p_doc = payload_snap.to_dict() or {}
        in_csv = str(p_doc.get("input_csv_inline") or "")
        out_csv = str(p_doc.get("output_csv_inline") or "")

      if not in_csv or not out_csv:
        snap = (
            client.collection(self.REQUESTS_COLLECTION)
            .document(request_id)
            .get()
        )
        if snap.exists:
          raw_doc = snap.to_dict() or {}
          in_csv = in_csv or str(raw_doc.get("input_csv_inline") or "")
          out_csv = out_csv or str(raw_doc.get("output_csv_inline") or "")

    uploader = PublicGcsUploader(use_mock_gcs=self.use_mock_db)
    if not in_csv:
      in_csv = uploader.read_request_csv(request_id, "input")
    if not out_csv:
      out_csv = uploader.read_request_csv(request_id, "output")

    return in_csv, out_csv

  def _get_cached_input_rows(self, request_id: str) -> List[FeedInputRow]:
    """Return parsed FeedInputRow list for `request_id` with memory caching.

    Args:
      request_id: Unique request identifier.

    Returns:
      List of parsed FeedInputRow objects.
    """
    with self._lock:
      cached = self._in_rows_cache.get(request_id)
    if cached is not None:
      return cached

    in_csv, _ = self.get_request_csvs(request_id)
    if not in_csv:
      return []
    try:
      # pylint: disable=g-import-not-at-top
      from app.pipeline.feed_io import parse_delimited_feed

      rows = parse_delimited_feed(in_csv)
      with self._lock:
        if len(self._in_rows_cache) > 128:
          self._in_rows_cache.clear()
        self._in_rows_cache[request_id] = rows
      return rows
    except Exception:  # pylint: disable=broad-exception-caught
      return []

  def _migrate_legacy_inline_csvs_async(
      self, docs_to_slim: List[Tuple[str, Dict[str, Any]]]
  ) -> None:
    """Move legacy inline CSV fields out of `upscale_requests` in background.

    Args:
      docs_to_slim: List of `(request_id, inline_csv_payload)` tuples.
    """
    if not docs_to_slim or self.use_mock_db:
      return

    def _worker() -> None:
      try:
        # pylint: disable=g-import-not-at-top
        from google.cloud import firestore

        client = self._get_client()
        for req_id, payload in docs_to_slim:
          if not req_id:
            continue
          client.collection(self.PAYLOADS_COLLECTION).document(req_id).set(
              payload, merge=True
          )
          client.collection(self.REQUESTS_COLLECTION).document(req_id).update({
              "input_csv_inline": firestore.DELETE_FIELD,
              "output_csv_inline": firestore.DELETE_FIELD,
          })
      except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("Non-fatal inline CSV slimming skipped: %s", exc)

    threading.Thread(target=_worker, daemon=True).start()

  def list_requests(
      self, limit: int = 100, force_refresh: bool = False
  ) -> List[UpscaleRequestRecord]:
    """Query Cloud Firestore for all requests ordered by `created_at_utc`.

    Args:
      limit: Maximum number of records to return.
      force_refresh: Whether to bypass the in-memory TTL cache.

    Returns:
      List of UpscaleRequestRecord objects ordered newest first.
    """
    if self.use_mock_db:
      with self._lock:
        items = list(self._mock_requests.values())
      items.sort(key=lambda d: str(d.get("created_at_utc", "")), reverse=True)
      return [self._dict_to_record(d) for d in items[:limit]]

    now_mono = time.monotonic()
    if not force_refresh:
      with self._lock:
        if self._requests_list_cache is not None:
          cached_ts, cached_records = self._requests_list_cache
          has_active = any(
              r.status in ("PROCESSING", "QUEUED") for r in cached_records
          )
          ttl_sec = 2.0 if has_active else 45.0
          if (now_mono - cached_ts) <= ttl_sec and len(cached_records) > 0:
            return cached_records[:limit]

    from google.cloud import firestore  # pylint: disable=g-import-not-at-top

    client = self._get_client()
    query = (
        client.collection(self.REQUESTS_COLLECTION)
        .order_by("created_at_utc", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    records: List[UpscaleRequestRecord] = []
    docs_to_slim: List[Tuple[str, Dict[str, Any]]] = []
    for doc in query.stream():
      raw = doc.to_dict() or {}
      if "input_csv_inline" in raw or "output_csv_inline" in raw:
        slim_payload: Dict[str, Any] = {}
        if raw.get("input_csv_inline"):
          slim_payload["input_csv_inline"] = raw["input_csv_inline"]
        if raw.get("output_csv_inline"):
          slim_payload["output_csv_inline"] = raw["output_csv_inline"]
        docs_to_slim.append(
            (str(raw.get("request_id") or doc.id), slim_payload)
        )
      records.append(self._dict_to_record(raw))

    with self._lock:
      self._requests_list_cache = (time.monotonic(), records)

    if docs_to_slim:
      self._migrate_legacy_inline_csvs_async(docs_to_slim)

    return records

  @staticmethod
  def _log_doc_id(entry: ConversionLogEntry) -> str:
    """Compute deterministic Firestore document ID for idempotent log writes."""
    raw_key = f"{entry.request_id}:{entry.sku_id}:{entry.source_image_url}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:28]

  def record_conversion_logs(
      self, entries: Sequence[ConversionLogEntry]
  ) -> None:
    """Write converted image entries to Firestore `conversion_logs` in batches.

    Args:
      entries: Sequence of ConversionLogEntry records to persist.
    """
    if not entries:
      return

    now_mono = time.monotonic()
    with self._lock:
      if len(self._logs_cache_by_req) > 64:
        self._logs_cache_by_req.clear()
      for e in entries:
        norm_e = self._normalize_log_entry(e)
        _, sku_map = self._logs_cache_by_req.get(e.request_id, (now_mono, {}))
        sku_map[e.sku_id] = norm_e
        self._logs_cache_by_req[e.request_id] = (now_mono, sku_map)

    if self.use_mock_db:
      with self._lock:
        index_by_key = {
            (
                d.get("request_id"),
                d.get("sku_id"),
                d.get("source_image_url"),
            ): idx
            for idx, d in enumerate(self._mock_logs)
        }
        for e in entries:
          dumped = e.model_dump()
          k = (e.request_id, e.sku_id, e.source_image_url)
          if k in index_by_key:
            self._mock_logs[index_by_key[k]] = dumped
          else:
            index_by_key[k] = len(self._mock_logs)
            self._mock_logs.append(dumped)
      return

    client = self._get_client()
    col = client.collection(self.LOGS_COLLECTION)
    # Firestore WriteBatch supports up to 500 operations per batch
    batch_size = 400
    for idx in range(0, len(entries), batch_size):
      batch = client.batch()
      for entry in entries[idx : idx + batch_size]:
        doc_ref = col.document(self._log_doc_id(entry))
        batch.set(doc_ref, entry.model_dump())
      batch.commit()

  def get_conversion_logs_by_date(
      self, date_yyyymmdd: str
  ) -> List[ConversionLogEntry]:
    """Fetch conversion log documents for a given `YYYYMMDD` date.

    Args:
      date_yyyymmdd: UTC date string in `YYYYMMDD` format.

    Returns:
      List of ConversionLogEntry records sorted chronologically.
    """
    if self.use_mock_db:
      with self._lock:
        matched = [
            d
            for d in self._mock_logs
            if d.get("date_yyyymmdd") == date_yyyymmdd
        ]
      return [ConversionLogEntry(**d) for d in matched]

    client = self._get_client()
    query = client.collection(self.LOGS_COLLECTION).where(
        "date_yyyymmdd", "==", date_yyyymmdd
    )
    results = [
        ConversionLogEntry(**(doc.to_dict() or {})) for doc in query.stream()
    ]
    results.sort(key=lambda r: r.timestamp_utc)
    return results

  @staticmethod
  def _normalize_log_entry(entry: ConversionLogEntry) -> ConversionLogEntry:
    """Normalize status and populate error_message for legacy FAILED entries.

    Args:
      entry: Raw ConversionLogEntry instance.

    Returns:
      Normalized ConversionLogEntry instance.
    """
    raw_status = entry.status or ""
    err_msg = entry.error_message or ""
    if raw_status.startswith("FAILED"):
      if not err_msg and "(" in raw_status and raw_status.endswith(")"):
        extracted = raw_status[raw_status.find("(") + 1 : -1].strip()
        if extracted in ("ReadError:", "ReadError"):
          extracted = (
              "ReadError: Remote CDN connection reset or timed out "
              "while downloading image"
          )
        err_msg = extracted
      return entry.model_copy(
          update={
              "status": "FAILED",
              "error_message": (
                  err_msg or "Failed to download or convert source image"
              ),
          }
      )
    return entry

  def get_conversion_logs_by_request(
      self,
      request_id: str,
      force_refresh: bool = False,
  ) -> List[ConversionLogEntry]:
    """Fetch per-SKU conversion log items for a specific `request_id`.

    When a job is PROCESSING, merges already-completed `conversion_logs`
    documents with the remaining pending SKUs from `input.csv` so the user
    sees live parallel progress.

    Args:
      request_id: Unique request identifier.
      force_refresh: Whether to bypass the in-memory TTL cache.

    Returns:
      Ordered list of ConversionLogEntry records for the request.
    """
    rec = self.get_request(request_id)
    is_in_progress = bool(rec and rec.status in ("PROCESSING", "QUEUED"))

    results: List[ConversionLogEntry] = []
    if self.use_mock_db:
      with self._lock:
        matched = [
            d for d in self._mock_logs if d.get("request_id") == request_id
        ]
      results = [
          self._normalize_log_entry(ConversionLogEntry(**d)) for d in matched
      ]
    else:
      now_mono = time.monotonic()
      ttl_sec = 2.5 if is_in_progress else 30.0
      rec_updated_at = (rec.updated_at_utc or "") if rec else ""
      cached_list: Optional[List[ConversionLogEntry]] = None
      if not force_refresh:
        with self._lock:
          cached_pair = self._logs_cache_by_req.get(request_id)
          cached_updated_at = self._logs_cache_updated_at.get(request_id, "")
          if cached_pair is not None:
            cached_ts, sku_map = cached_pair
            expected_done = int(
                (rec.upscaled_images + rec.skipped_images + rec.failed_images)
                if rec
                else 0
            )
            is_version_fresh = is_in_progress or (
                bool(rec_updated_at) and cached_updated_at == rec_updated_at
            )
            if (
                (now_mono - cached_ts) <= ttl_sec
                and is_version_fresh
                and len(sku_map) >= expected_done
                and len(sku_map) > 0
            ):
              cached_list = list(sku_map.values())
      if cached_list is not None:
        results = cached_list
      else:
        client = self._get_client()
        query = client.collection(self.LOGS_COLLECTION).where(
            "request_id", "==", request_id
        )
        results = [
            self._normalize_log_entry(
                ConversionLogEntry(**(doc.to_dict() or {}))
            )
            for doc in query.stream()
        ]
        with self._lock:
          self._logs_cache_by_req[request_id] = (
              time.monotonic(),
              {e.sku_id: e for e in results},
          )
          if rec_updated_at:
            self._logs_cache_updated_at[request_id] = rec_updated_at

    if not rec:
      return results
    if results and not is_in_progress:
      # Completed job: preserve input CSV row order if available,
      # otherwise sort by sku_id
      in_rows = self._get_cached_input_rows(request_id)
      if in_rows:
        order_map = {r.id: idx for idx, r in enumerate(in_rows)}
        results.sort(key=lambda r: (order_map.get(r.sku_id, 999999), r.sku_id))
      else:
        results.sort(key=lambda r: r.sku_id)
      return results

    # Job is PROCESSING (merge completed logs + remaining pending SKUs) OR
    # fallback when logs absent
    in_rows = self._get_cached_input_rows(request_id)
    if not in_rows:
      return results

    completed_by_sku: Dict[str, ConversionLogEntry] = {
        entry.sku_id: entry for entry in results
    }
    merged_items: List[ConversionLogEntry] = []
    for r in in_rows:
      if r.id in completed_by_sku:
        merged_items.append(completed_by_sku[r.id])
      elif is_in_progress:
        merged_items.append(
            ConversionLogEntry(
                timestamp_utc=rec.updated_at_utc,
                date_yyyymmdd=rec.date_yyyymmdd,
                request_id=request_id,
                sku_id=r.id,
                source_image_url=r.image_link,
                converted_gcs_url="",
                engine_id=rec.engine_id,
                target_resolution=rec.target_resolution,
                original_dimensions="Processing...",
                output_dimensions=(
                    f"{rec.target_resolution}x{rec.target_resolution}"
                ),
                status="PROCESSING",
                processing_ms=0.0,
                error_message="",
            )
        )
      else:
        merged_items.append(
            ConversionLogEntry(
                timestamp_utc=rec.updated_at_utc,
                date_yyyymmdd=rec.date_yyyymmdd,
                request_id=request_id,
                sku_id=r.id,
                source_image_url=r.image_link,
                converted_gcs_url="",
                engine_id=rec.engine_id,
                target_resolution=rec.target_resolution,
                original_dimensions="0x0",
                output_dimensions="0x0",
                status="FAILED",
                processing_ms=0.0,
                error_message=rec.error_message or "Image conversion failed",
            )
        )
    return merged_items

  @staticmethod
  def _dict_to_record(data: Dict[str, Any]) -> UpscaleRequestRecord:
    err_msg = data.get("error_message")
    if isinstance(err_msg, str) and "(ReadError: )" in err_msg:
      err_msg = err_msg.replace(
          "(ReadError: )",
          "(ReadError: Remote CDN connection reset or timed out while "
          "downloading image)",
      )
    return UpscaleRequestRecord(
        request_id=str(data.get("request_id", "")),
        created_at_utc=str(data.get("created_at_utc", "")),
        updated_at_utc=str(data.get("updated_at_utc", "")),
        date_yyyymmdd=str(data.get("date_yyyymmdd", "")),
        filename=str(data.get("filename", "")),
        engine_id=str(data.get("engine_id", "local-lanczos-canvas")),
        target_resolution=int(data.get("target_resolution", 500)),
        status=data.get("status", "QUEUED"),
        total_skus=int(data.get("total_skus", 0)),
        total_images=int(data.get("total_images", 0)),
        upscaled_images=int(data.get("upscaled_images", 0)),
        skipped_images=int(data.get("skipped_images", 0)),
        failed_images=int(data.get("failed_images", 0)),
        estimated_cost_usd=float(data.get("estimated_cost_usd", 0.0)),
        duration_ms=float(data.get("duration_ms", 0.0)),
        input_gcs_uri=str(data.get("input_gcs_uri", "")),
        output_gcs_uri=str(data.get("output_gcs_uri", "")),
        daily_log_gcs_uri=str(data.get("daily_log_gcs_uri", "")),
        error_message=err_msg,
    )


# Maintain `LightweightRequestDB` alias for clean imports across orchestrator &
# tests
LightweightRequestDB = FirestoreRequestDB
request_db = FirestoreRequestDB()


def utc_now_iso_and_yyyymmdd() -> Tuple[str, str]:
  """Return `(iso8601_utc_str, yyyymmdd_str)`."""
  now = datetime.datetime.now(datetime.timezone.utc)
  return now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y%m%d")
