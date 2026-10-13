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

"""Unit and integration test suite for ImageUpscaler."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import threading

from app.cli import main as cli_main
from app.config import DEPLOYMENT_ACCESS_CODE
from app.config import ENGINE_CATALOG
from app.db import LightweightRequestDB
from app.db import request_db
from app.db import utc_now_iso_and_yyyymmdd
from app.engines.base import get_image_dimensions_and_format
from app.engines.router import engine_router
from app.engines.router import MultiEngineRouter
from app.engines.vertex_nano_banana import _detect_image_mime_type
from app.engines.vertex_nano_banana import _get_vertex_model_candidates
from app.engines.vertex_nano_banana import _is_retryable_vertex_error
from app.engines.vertex_nano_banana import _lock_to_original_image
from app.engines.vertex_nano_banana import build_vertex_batch_jsonl_line
from app.engines.vertex_nano_banana import VertexNanoBananaEngine
from app.engines.vertex_nano_banana import VertexRateLimiter
from app.main import app
from app.models import ConversionLogEntry
from app.pipeline.auditor import audit_catalog_feed
from app.pipeline.feed_io import ensure_output_csv_has_input_url
from app.pipeline.feed_io import parse_delimited_feed
from app.pipeline.gcs_uploader import PublicGcsUploader
from app.pipeline.orchestrator import create_and_process_request
from app.pipeline.synthetic_catalog import generate_synthetic_product_image
from app.pipeline.synthetic_catalog import SAMPLE_ANCHOR_FEED_CSV
from app.security import inspect_image_magic_bytes
from app.security import safe_resolve_within_sandbox
from app.security import validate_csv_upload_filename
from app.security import validate_external_image_url
from fastapi.testclient import TestClient
from PIL import Image
from PIL import ImageChops
from PIL import ImageStat
import pytest


def test_lanczos_engine_upscales_sub_500_to_exact_500_and_1500() -> None:
  """Verify Lanczos engine upscales sub-500px images to exact 500 and 1500."""
  src_bytes = generate_synthetic_product_image(
      "synthetic://retail-jp/SKU-1/320x240/serum.jpg"
  )
  w, h, fmt = get_image_dimensions_and_format(src_bytes)
  assert (w, h, fmt) == (320, 240, "JPEG")

  out_500 = engine_router.upscale(
      src_bytes, target_px=500, engine_id="local-lanczos-canvas"
  )
  assert not out_500.skipped_already_compliant
  assert (out_500.output_width, out_500.output_height) == (500, 500)
  real_w, real_h, _ = get_image_dimensions_and_format(out_500.image_bytes)
  assert (real_w, real_h) == (500, 500)

  out_1500 = engine_router.upscale(
      src_bytes, target_px=1500, engine_id="local-lanczos-canvas"
  )
  assert not out_1500.skipped_already_compliant
  assert (out_1500.output_width, out_1500.output_height) == (1500, 1500)
  real_w1500, real_h1500, _ = get_image_dimensions_and_format(
      out_1500.image_bytes
  )
  assert (real_w1500, real_h1500) == (1500, 1500)


def test_already_compliant_images_are_skipped() -> None:
  """Verify images meeting target resolution are skipped without re-encoding."""
  img_640 = generate_synthetic_product_image(
      "synthetic://retail-jp/SKU-2/640x640/dryer.jpg"
  )

  res_500 = engine_router.upscale(
      img_640, target_px=500, engine_id="local-lanczos-canvas"
  )
  assert res_500.skipped_already_compliant
  assert (res_500.output_width, res_500.output_height) == (640, 640)

  res_1500 = engine_router.upscale(
      img_640, target_px=1500, engine_id="local-lanczos-canvas"
  )
  assert not res_1500.skipped_already_compliant
  assert (res_1500.output_width, res_1500.output_height) == (1500, 1500)


def test_all_five_engines_and_recommendation_router() -> None:
  """Verify all five engines upscale and recommendation router selects well."""
  src = generate_synthetic_product_image(
      "synthetic://marketplace-apparel/SKU-3/200x200/bag.jpg"
  )
  for engine_id in ENGINE_CATALOG:
    out = engine_router.upscale(src, target_px=500, engine_id=engine_id)
    assert (out.output_width, out.output_height) == (500, 500)
    assert out.engine_used == engine_id

  assert (
      MultiEngineRouter.recommend_engine(200, 200, has_text_or_logo=True)
      == "local-lanczos-canvas"
  )
  assert (
      MultiEngineRouter.recommend_engine(320, 320, has_text_or_logo=False)
      == "local-lanczos-canvas"
  )
  assert (
      MultiEngineRouter.recommend_engine(
          200, 200, has_text_or_logo=False, target_px=500
      )
      == "gemini-3.1-flash-lite-image"
  )
  assert (
      MultiEngineRouter.recommend_engine(
          200, 200, has_text_or_logo=False, target_px=1500
      )
      == "gemini-3.1-flash-image"
  )
  assert (
      MultiEngineRouter.recommend_engine(
          200, 200, has_text_or_logo=False, is_hero_sku=True
      )
      == "gemini-3-pro-image"
  )


def test_flexible_csv_url_parsing() -> None:
  """Verify flexible CSV/TSV parser handles URL-only and headerless feeds."""
  # 1. CSV with only 'url' header (auto-generates SKU IDs)
  simple_csv = (
      "url\nsynthetic://retail-jp/A/200x200/a.jpg\nsynthetic://"
      "industrial-supply/B/300x300/b.jpg\n"
  )
  rows = parse_delimited_feed(simple_csv)
  assert len(rows) == 2
  assert rows[0].id == "SKU-0001"

  # 2. Headerless list of URLs
  headerless = (
      "synthetic://retail-jp/A/200x200/a.jpg\nsynthetic://"
      "industrial-supply/B/300x300/b.jpg\n"
  )
  rows2 = parse_delimited_feed(headerless)
  assert len(rows2) == 2
  assert rows2[1].id == "SKU-0002"


def test_vertex_batch_jsonl_builder() -> None:
  """Verify Vertex AI batch prediction JSONL line formatting."""
  line = build_vertex_batch_jsonl_line(
      custom_id="SKU-100",
      gcs_source_uri="gs://client-bucket/raw/item100.jpg",
      model_id="gemini-3.1-flash-image",
      target_px=1500,
  )
  parsed = json.loads(line)
  assert parsed["custom_id"] == "SKU-100"
  assert parsed["request"]["generation_config"]["response_modalities"] == [
      "IMAGE"
  ]


@pytest.mark.asyncio
async def test_stage1_dimension_auditor_and_cost_matrix() -> None:
  """Verify Stage 1 dimension auditor counts and 5-engine cost matrix."""
  rows = parse_delimited_feed(SAMPLE_ANCHOR_FEED_CSV)
  assert len(rows) == 7

  report = await audit_catalog_feed(rows)
  assert report.total_skus == 7
  assert report.total_image_references == 12
  assert report.unique_image_urls == 11
  assert report.sub_500x500_urls == 9
  assert report.between_500_and_1499_urls == 1
  assert report.compliant_1500x1500_urls == 1
  assert len(report.cost_matrix) == 5


@pytest.mark.asyncio
async def test_lightweight_db_and_daily_gcs_status_log(
    tmp_path: pathlib.Path,
) -> None:
  """Verify lightweight DB persistence and daily cumulative GCS status log."""
  db = LightweightRequestDB(use_mock_db=True)
  uploader = PublicGcsUploader(
      bucket_name="retail-jp-gmc-public",
      local_storage_dir=tmp_path / "gcs_sandbox",
      use_mock_gcs=True,
  )

  rec1 = await create_and_process_request(
      csv_content=SAMPLE_ANCHOR_FEED_CSV,
      filename="batch_1.csv",
      engine_id="local-lanczos-canvas",
      target_px=500,
      uploader=uploader,
      db=db,
  )
  assert rec1.status == "COMPLETED"
  assert rec1.total_skus == 7
  assert rec1.upscaled_images == 9
  assert rec1.skipped_images == 2
  assert rec1.daily_log_gcs_uri.endswith("/log.csv")
  assert "/status/" in rec1.daily_log_gcs_uri

  # Run a second request on the same day to verify `status/{YYYYMMDD}/log.csv`
  # appends cumulatively
  second_csv = (
      "id,image_link\nEXTRA-SKU-99,synthetic://marketplace-apparel/"
      "EXTRA-99/250x250/hat.jpg\n"
  )
  rec2 = await create_and_process_request(
      csv_content=second_csv,
      filename="batch_2.csv",
      engine_id="realesrgan",
      target_px=1500,
      uploader=uploader,
      db=db,
  )
  assert rec2.status == "COMPLETED"

  all_requests = db.list_requests()
  assert len(all_requests) == 2

  _, today_yyyymmdd = utc_now_iso_and_yyyymmdd()
  daily_log_csv = uploader.read_daily_status_log(today_yyyymmdd)
  log_lines = daily_log_csv.strip().splitlines()
  # 1 header line + 12 image refs from batch_1 + 1 image ref from batch_2 = 14
  # lines
  assert log_lines[0].startswith(
      "timestamp_utc,date_yyyymmdd,request_id,sku_id,source_image_url,"
      "converted_gcs_url"
  )
  assert len(log_lines) == 14


def test_security_controls_ssrf_magic_bytes_and_sandbox(
    tmp_path: pathlib.Path,
) -> None:
  """Verify SSRF prevention, image magic-byte check, and sandbox path bounds."""
  for blocked_url in [
      "http://127.0.0.1/admin",
      "http://localhost:8080/secret",
      "http://169.254.169.254/latest/meta-data/",
      "http://10.0.0.1/internal.jpg",
      "http://192.168.1.1/router.jpg",
      "file:///etc/passwd",
  ]:
    with pytest.raises(ValueError):
      validate_external_image_url(blocked_url)

  with pytest.raises(ValueError):
    inspect_image_magic_bytes(b"<script>alert(1)</script>")

  assert (
      validate_csv_upload_filename("../../feeds/catalog.csv") == "catalog.csv"
  )
  with pytest.raises(ValueError):
    validate_csv_upload_filename("exploit.php")

  safe_path = safe_resolve_within_sandbox(
      tmp_path, "../../upscaled/500px/img.jpg"
  )
  assert str(safe_path).startswith(str(tmp_path.resolve()))


def test_access_code_auth_requests_db_and_downloads() -> None:
  """Verify access-code authentication, request endpoints, and CSV downloads."""
  client = TestClient(app)

  # 1. Initialize session -> unauthenticated initially
  session_resp = client.get("/api/session")
  assert session_resp.status_code == 200
  assert not session_resp.json()["authenticated"]
  csrf_token = session_resp.json()["csrf_token"]
  today_yyyymmdd = session_resp.json()["today_yyyymmdd"]

  # 2. Unauthenticated access to /api/requests is blocked with 401
  unauth_list = client.get("/api/requests")
  assert unauth_list.status_code == 401

  # 3. Wrong Access Code login is rejected with 401
  bad_login = client.post(
      "/api/auth/login",
      headers={"X-CSRF-Token": csrf_token},
      json={"access_code": "wrong-code-123"},
  )
  assert bad_login.status_code == 401

  # 4. Valid deployment Access Code login unlocks session
  ok_login = client.post(
      "/api/auth/login",
      headers={"X-CSRF-Token": csrf_token},
      json={"access_code": DEPLOYMENT_ACCESS_CODE},
  )
  assert ok_login.status_code == 200
  assert ok_login.json()["authenticated"]

  # 5. Submit an upscaling request via POST /api/requests
  create_resp = client.post(
      "/api/requests",
      headers={"X-CSRF-Token": csrf_token},
      json={
          "filename": "merchant_upload.csv",
          "csv_content": SAMPLE_ANCHOR_FEED_CSV,
          "engine_id": "local-lanczos-canvas",
          "target_resolution": 500,
      },
  )
  assert create_resp.status_code == 200
  created_rec = create_resp.json()
  req_id = created_rec["request_id"]
  assert created_rec["status"] == "COMPLETED"
  assert created_rec["upscaled_images"] == 9

  # 6. List requests from Lightweight DB via GET /api/requests
  list_resp = client.get("/api/requests")
  assert list_resp.status_code == 200
  records = list_resp.json()
  assert any(r["request_id"] == req_id for r in records)

  # 6b. Open /request/<request-id> SPA route & fetch
  # /api/requests/<request-id> detail JSON
  spa_resp = client.get(f"/request/{req_id}")
  assert spa_resp.status_code == 200
  assert "ImageUpscaler" in spa_resp.text

  detail_resp = client.get(f"/api/requests/{req_id}")
  assert detail_resp.status_code == 200
  detail_payload = detail_resp.json()
  assert detail_payload["request"]["request_id"] == req_id
  assert len(detail_payload["items"]) == 12

  # 7. Download Output CSV for the completed request
  dl_resp = client.get(f"/api/requests/{req_id}/download-output")
  assert dl_resp.status_code == 200
  dl_lines = dl_resp.text.strip().splitlines()
  expected_5col_hdr = (
      "id,input_url,scaled_image_url,"
      "additional_image_link,additional_scaled_image_link"
  )
  assert dl_lines[0] == expected_5col_hdr
  assert len(dl_lines) == 8

  # 8. Download daily status/{YYYYMMDD}/log.csv
  log_resp = client.get(f"/api/status-log/{today_yyyymmdd}")
  assert log_resp.status_code == 200
  assert "converted_gcs_url" in log_resp.text


def test_cli_audit_and_upscale(tmp_path: pathlib.Path) -> None:
  """Verify CLI audit and upscale subcommands produce valid output."""
  out_csv = tmp_path / "supplemental_out.csv"
  rc_audit = cli_main(["audit", "--input", "sample"])
  assert rc_audit == 0

  rc_up = cli_main([
      "upscale",
      "--input",
      "sample",
      "--output",
      str(out_csv),
      "--resolution",
      "500",
      "--engine",
      "local-lanczos-canvas",
      "--mock-gcs",
  ])
  assert rc_up == 0
  assert out_csv.is_file()
  lines = out_csv.read_text(encoding="utf-8").strip().splitlines()
  expected_5col_hdr = (
      "id,input_url,scaled_image_url,"
      "additional_image_link,additional_scaled_image_link"
  )
  assert lines[0] == expected_5col_hdr
  assert len(lines) == 8


def test_ensure_output_csv_has_input_url_legacy_backfill() -> None:
  """Verify legacy output CSVs are normalized to GMC supplemental feed."""
  input_csv = (
      "id,image_link\nSKU-1,https://example.com/orig1.jpg\nSKU-2,https://"
      "example.com/orig2.jpg\n"
  )
  legacy_output_csv = (
      "id,image_link,additional_image_link\n"
      "SKU-1,https://storage.googleapis.com/b/out1.jpg,\n"
      "SKU-2,https://storage.googleapis.com/b/out2.jpg,\n"
  )
  upgraded = ensure_output_csv_has_input_url(input_csv, legacy_output_csv)
  lines = upgraded.strip().splitlines()
  assert lines[0] == "id,input_url,scaled_image_url"
  expected_row1 = (
      "SKU-1,https://example.com/orig1.jpg,"
      "https://storage.googleapis.com/b/out1.jpg"
  )
  expected_row2 = (
      "SKU-2,https://example.com/orig2.jpg,"
      "https://storage.googleapis.com/b/out2.jpg"
  )
  assert lines[1] == expected_row1
  assert lines[2] == expected_row2

  # Also verify 4-column legacy CSV
  # (id,input_url,image_link,additional_image_link) backfills original + scaled
  in_with_add = (
      "id,image_link,additional_image_link\n"
      "SKU-1,https://example.com/orig1.jpg,https://example.com/orig_extra.jpg\n"
  )
  legacy_four_col_csv = (
      "id,input_url,image_link,additional_image_link\n"
      "SKU-1,https://example.com/orig1.jpg,https://storage.googleapis.com/"
      "b/out1.jpg,https://storage.googleapis.com/b/extra.jpg\n"
  )
  upgraded_four = ensure_output_csv_has_input_url(
      in_with_add, legacy_four_col_csv
  )
  lines_four = upgraded_four.strip().splitlines()
  assert lines_four[0] == (
      "id,input_url,scaled_image_url,"
      "additional_image_link,additional_scaled_image_link"
  )
  assert lines_four[1] == (
      f"{expected_row1},https://example.com/orig_extra.jpg,"
      "https://storage.googleapis.com/b/extra.jpg"
  )


def test_vertex_rate_limiter_and_retry_classification() -> None:
  """Verify Vertex rate limiter concurrency and retryable error detection."""
  # Test multi-threaded concurrent pacing (regression test against
  # multi-thread livelock)
  limiter = VertexRateLimiter(max_concurrency=4, max_qpm=600)
  completed = []

  def _worker(idx: int) -> None:
    with limiter.acquire(enforce_pacing=True):
      completed.append(idx)

  threads = [threading.Thread(target=_worker, args=(i,)) for i in range(5)]
  for t in threads:
    t.start()
  for t in threads:
    t.join(timeout=3.0)
    assert not t.is_alive(), "VertexRateLimiter.wait_for_slot livelocked"

  assert len(completed) == 5
  assert _is_retryable_vertex_error(
      RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded")
  )
  assert _is_retryable_vertex_error(RuntimeError("503 Service Unavailable"))
  assert not _is_retryable_vertex_error(
      RuntimeError("404 NOT_FOUND: model not found for 1500px")
  )
  assert not _is_retryable_vertex_error(ValueError("Invalid image format"))
  assert (
      _detect_image_mime_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
  )
  assert _detect_image_mime_type(b"\x89PNG\r\n\x1a\n0000") == "image/png"


def test_sku_retry_billing_update_and_gcp_pricing_sync() -> None:
  """Verify SKU retry updates billing cost and GCP pricing sync works."""
  client = TestClient(app)
  s_resp = client.get("/api/session")
  assert s_resp.status_code == 200
  csrf_token = s_resp.json()["csrf_token"]
  assert "pricing" in s_resp.json()

  # 1. Authenticate
  login_resp = client.post(
      "/api/auth/login",
      headers={"X-CSRF-Token": csrf_token},
      json={"access_code": DEPLOYMENT_ACCESS_CODE},
  )
  assert login_resp.status_code == 200

  # 2. Verify GET /api/pricing and POST /api/pricing/sync
  p_get = client.get("/api/pricing")
  assert p_get.status_code == 200
  p_data = p_get.json()
  assert len(p_data["engines"]) == 5
  assert p_data["unit_rates"]["cloud_run_nvidia_l4_per_hr_usd"] > 0

  p_sync = client.post(
      "/api/pricing/sync",
      headers={"X-CSRF-Token": csrf_token},
      json={},
  )
  assert p_sync.status_code == 200
  sync_data = p_sync.json()
  assert sync_data["last_synced_utc"]

  # 3. Create a 2-SKU request and simulate 1 failed SKU, then retry that SKU
  # and verify billing updates
  csv_two_skus = (
      "id,image_link\n"
      "SKU-RETRY-1,synthetic://watch-alpha-320x320.jpg\n"
      "SKU-RETRY-2,synthetic://bag-beta-400x400.jpg\n"
  )
  create_resp = client.post(
      "/api/requests",
      headers={"X-CSRF-Token": csrf_token},
      json={
          "filename": "retry_test.csv",
          "csv_content": csv_two_skus,
          "engine_id": "local-lanczos-canvas",
          "target_resolution": 1500,
      },
  )
  assert create_resp.status_code == 200
  req_id = create_resp.json()["request_id"]
  full_two_sku_cost = create_resp.json()["estimated_cost_usd"]
  assert full_two_sku_cost > 0

  # Simulate SKU-RETRY-2 having failed initially (1 converted, 1 failed ->
  # half billing cost)
  existing_logs = request_db.get_conversion_logs_by_request(
      req_id, force_refresh=True
  )
  sku2_log = next(it for it in existing_logs if it.sku_id == "SKU-RETRY-2")
  failed_sku2 = ConversionLogEntry(
      timestamp_utc=sku2_log.timestamp_utc,
      date_yyyymmdd=sku2_log.date_yyyymmdd,
      request_id=req_id,
      sku_id="SKU-RETRY-2",
      source_image_url=sku2_log.source_image_url,
      converted_gcs_url="",
      engine_id="local-lanczos-canvas",
      target_resolution=1500,
      original_dimensions="0x0",
      output_dimensions="0x0",
      status="FAILED",
      processing_ms=0.0,
      error_message="ReadError: Simulated transient CDN timeout",
  )
  request_db.record_conversion_logs([failed_sku2])
  rec_before = request_db.get_request(req_id)
  assert rec_before is not None
  half_cost = round(full_two_sku_cost / 2.0, 6)
  request_db.save_request(
      rec_before.model_copy(
          update={
              "upscaled_images": 1,
              "failed_images": 1,
              "estimated_cost_usd": half_cost,
              "error_message": (
                  "1 URL(s) failed (ReadError: Simulated transient CDN timeout)"
              ),
          }
      )
  )

  # 4. Retry SKU-RETRY-2 via POST
  # /api/requests/{req_id}/skus/SKU-RETRY-2/retry
  retry_resp = client.post(
      f"/api/requests/{req_id}/skus/SKU-RETRY-2/retry",
      headers={"X-CSRF-Token": csrf_token},
      json={"engine_id": "local-lanczos-canvas"},
  )
  assert retry_resp.status_code == 200
  retry_payload = retry_resp.json()
  updated_req = retry_payload["request"]
  assert updated_req["upscaled_images"] == 2
  assert updated_req["failed_images"] == 0
  assert updated_req["status"] == "COMPLETED"
  assert updated_req["error_message"] is None
  assert updated_req["estimated_cost_usd"] == full_two_sku_cost

  retried_item = next(
      it for it in retry_payload["items"] if it["sku_id"] == "SKU-RETRY-2"
  )
  assert retried_item["status"] == "CONVERTED"
  assert retried_item["converted_gcs_url"].startswith(
      "https://storage.googleapis.com/"
  )


def test_post_login_inline_hydration_and_ui_loaders() -> None:
  """Verify POST /api/auth/login returns requests & pricing inline."""
  client = TestClient(app)

  init_session = client.get("/api/session")
  assert init_session.status_code == 200
  csrf_token = init_session.json()["csrf_token"]

  login_resp = client.post(
      "/api/auth/login",
      headers={"X-CSRF-Token": csrf_token},
      json={"access_code": DEPLOYMENT_ACCESS_CODE},
  )
  assert login_resp.status_code == 200
  login_data = login_resp.json()
  assert login_data["authenticated"]
  assert isinstance(login_data.get("requests"), list)
  assert isinstance(login_data.get("pricing"), dict)
  assert "engines" in login_data["pricing"]

  # Authenticated GET /api/session also hydrates requests and pricing inline
  session_resp = client.get("/api/session")
  assert session_resp.status_code == 200
  session_data = session_resp.json()
  assert session_data["authenticated"]
  assert isinstance(session_data.get("requests"), list)
  assert isinstance(session_data.get("pricing"), dict)

  # Verify loader helpers & zero unsafe DOM sinks in app/static/app.js
  app_js = pathlib.Path("app/static/app.js").read_text(encoding="utf-8")
  assert "function setButtonLoading(" in app_js
  assert "function setKpisLoading(" in app_js
  assert "function renderTableLoader(" in app_js
  assert ".innerHTML" not in app_js
  assert ".outerHTML" not in app_js


def test_sku_preview_proxy_and_slider_comparator() -> None:
  """Verify /api/preview-source proxy, CSP headers, and slider comparator UI."""
  client = TestClient(app)

  # 1. Synthetic URL preview returns valid JPEG bytes
  syn_resp = client.get(
      "/api/preview-source",
      params={"url": "synthetic://catalog/sku-comp?w=240&h=180"},
  )
  assert syn_resp.status_code == 200
  assert syn_resp.headers["content-type"] == "image/jpeg"
  assert syn_resp.content.startswith(b"\xff\xd8\xff")

  # 2. SSRF protection blocks internal loopback URLs on /api/preview-source
  ssrf_resp = client.get(
      "/api/preview-source", params={"url": "http://127.0.0.1/internal.png"}
  )
  assert ssrf_resp.status_code == 400

  # 3. Unsupported scheme is rejected
  bad_scheme = client.get(
      "/api/preview-source", params={"url": "ftp://example.com/file.jpg"}
  )
  assert bad_scheme.status_code == 400

  # 4. CSP allows https: image sources and inline CSS variables for the
  # comparator slider
  index_resp = client.get("/")
  assert index_resp.status_code == 200
  csp = index_resp.headers.get("Content-Security-Policy", "")
  assert "img-src 'self' data: blob: https: http:;" in csp
  assert "style-src 'self' 'unsafe-inline';" in csp

  # 5. HTML, CSS, and JS contain the interactive slider comparator components
  index_html = pathlib.Path("app/static/index.html").read_text(encoding="utf-8")
  styles_css = pathlib.Path("app/static/styles.css").read_text(encoding="utf-8")
  app_js = pathlib.Path("app/static/app.js").read_text(encoding="utf-8")

  assert 'id="sku-comparator-section"' in index_html
  assert 'id="sku-comp-stage"' in index_html
  assert 'id="sku-comp-range"' in index_html
  assert 'id="sku-comp-range-bar"' in index_html
  assert 'id="sku-comp-badge-before"' in index_html
  assert ">Before</span>" in index_html
  assert 'id="sku-comp-badge-after"' in index_html
  assert ">After</span>" in index_html
  assert "sku-modal-preview-grid" not in index_html
  assert ".sku-comp-before-img" in styles_css
  assert "clip-path: inset(" in styles_css
  assert "function buildPreviewProxyUrl(" in app_js
  assert "function setComparatorSplit(" in app_js


def test_nano_banana_zero_hallucination_original_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Verify Nano Banana engines call _invoke_vertex_genai and lock pixels."""
  src_bytes = generate_synthetic_product_image(
      "synthetic://catalog/sku-no-hallucination?w=300&h=400"
  )
  orig_img = Image.open(io.BytesIO(src_bytes)).convert("RGB")

  # Simulate a hallucinated generative output where a region was
  # erased/whited-out
  hallucinated = orig_img.resize((1024, 1024))
  for x in range(100, 300):
    for y in range(100, 300):
      hallucinated.putpixel((x, y), (255, 255, 255))

  locked = _lock_to_original_image(
      orig_img, target_px=1500, gen_img=hallucinated
  )
  expected_resized = orig_img.resize(
      locked.size, resample=Image.Resampling.LANCZOS
  )
  diff = ImageChops.difference(locked, expected_resized)
  mean_diff = sum(ImageStat.Stat(diff).mean) / 3.0
  # Mean RGB difference against pure Lanczos resize of original must be tiny
  # (< 1.5 levels from luma unsharp only)
  assert mean_diff < 1.5

  for model_id in (
      "gemini-3.1-flash-lite-image",
      "gemini-3.1-flash-image",
      "gemini-3-pro-image",
  ):
    cands = _get_vertex_model_candidates(model_id)
    assert cands[0] == model_id
    assert f"{model_id}-preview" in cands
    res = engine_router.upscale(src_bytes, target_px=1500, engine_id=model_id)
    assert (res.output_width, res.output_height) == (1500, 1500)
    assert res.engine_used == model_id

  # Verify upscale() invokes _invoke_vertex_genai and uses the Vertex AI
  # high-res image when returned
  eng = VertexNanoBananaEngine("gemini-3.1-flash-lite-image")
  called_with: list[int] = []
  fake_vertex_img = Image.new("RGB", (1024, 1024), (42, 128, 200))
  buf = io.BytesIO()
  fake_vertex_img.save(buf, format="JPEG", quality=95)
  fake_vertex_bytes = buf.getvalue()

  def _fake_invoke(unused_image_bytes: bytes, target_px: int) -> bytes:
    del unused_image_bytes
    called_with.append(target_px)
    return fake_vertex_bytes

  monkeypatch.setattr(eng, "_invoke_vertex_genai", _fake_invoke)
  live_out = eng.upscale(src_bytes, target_px=1500)
  assert called_with == [1500]
  assert (live_out.output_width, live_out.output_height) == (1500, 1500)
  out_pil = Image.open(io.BytesIO(live_out.image_bytes)).convert("RGB")
  center_px = out_pil.getpixel((750, 750))
  assert (
      abs(center_px[0] - 42) <= 3
      and abs(center_px[1] - 128) <= 3
      and abs(center_px[2] - 200) <= 3
  )


def test_multi_engine_distinct_urls_and_retry_cache_invalidation() -> None:
  """Verify all 5 engines produce distinct GCS URLs and retry updates cache."""
  client = TestClient(app)
  s_resp = client.get("/api/session")
  assert s_resp.status_code == 200
  csrf_token = s_resp.json()["csrf_token"]
  login_resp = client.post(
      "/api/auth/login",
      headers={"X-CSRF-Token": csrf_token},
      json={"access_code": DEPLOYMENT_ACCESS_CODE},
  )
  assert login_resp.status_code == 200

  uploader = PublicGcsUploader(bucket_name="test-bucket")
  src_url = "synthetic://catalog/sku-multi-engine?w=320&h=240"
  src_bytes = generate_synthetic_product_image(src_url)

  all_engines = (
      "local-lanczos-canvas",
      "realesrgan",
      "gemini-3.1-flash-lite-image",
      "gemini-3.1-flash-image",
      "gemini-3-pro-image",
  )

  urls: set[str] = set()
  sha256s: set[str] = set()
  for eng in all_engines:
    res = engine_router.upscale(src_bytes, target_px=1500, engine_id=eng)
    pub_url, _ = uploader.upload_image(
        image_bytes=res.image_bytes,
        source_url=src_url,
        target_px=1500,
        engine_id=eng,
    )
    urls.add(pub_url)
    sha256s.add(hashlib.sha256(res.image_bytes).hexdigest())

  assert len(urls) == len(all_engines), f"Expected 5 distinct URLs, got {urls}"
  assert len(sha256s) == len(
      all_engines
  ), f"Expected 5 distinct image SHA-256 hashes, got {sha256s}"

  # Verify end-to-end single-SKU retry across engines updates GET
  # /api/requests/{id} and output.csv immediately
  csv_text = (
      "id,title,image_link\nSKU-ENG-SWITCH,Switch Test,synthetic://catalog/"
      "sku-switch?w=300&h=300\n"
  )
  create_resp = client.post(
      "/api/requests",
      headers={"X-CSRF-Token": csrf_token},
      json={
          "filename": "engine_switch.csv",
          "csv_content": csv_text,
          "engine_id": "local-lanczos-canvas",
          "target_resolution": 1500,
      },
  )
  assert create_resp.status_code == 200
  req_id = create_resp.json()["request_id"]

  # Prime the GET /api/requests/{id} cache
  get_before = client.get(f"/api/requests/{req_id}")
  assert get_before.status_code == 200
  initial_item = get_before.json()["items"][0]
  initial_url = initial_item["converted_gcs_url"]
  assert "/lc1500/" in initial_url

  # Now retry the same SKU with each of the other 4 engines and verify GET
  # /api/requests/{id} and output.csv update immediately
  expected_prefixes = {
      "realesrgan": "/esr1500/",
      "gemini-3.1-flash-lite-image": "/gfl1500/",
      "gemini-3.1-flash-image": "/gf1500/",
      "gemini-3-pro-image": "/gp1500/",
  }
  seen_retry_urls = {initial_url}
  for eng, expected_seg in expected_prefixes.items():
    retry_resp = client.post(
        f"/api/requests/{req_id}/skus/SKU-ENG-SWITCH/retry",
        headers={"X-CSRF-Token": csrf_token},
        json={"engine_id": eng},
    )
    assert retry_resp.status_code == 200
    retry_item = retry_resp.json()["items"][0]
    assert retry_item["engine_id"] == eng
    assert expected_seg in retry_item["converted_gcs_url"]
    assert retry_item["converted_gcs_url"] not in seen_retry_urls
    seen_retry_urls.add(retry_item["converted_gcs_url"])

    # Subsequent GET /api/requests/{id} MUST return the new engine's URL,
    # never a stale cached URL
    get_after = client.get(f"/api/requests/{req_id}")
    assert get_after.status_code == 200
    after_item = get_after.json()["items"][0]
    assert after_item["engine_id"] == eng
    assert after_item["converted_gcs_url"] == retry_item["converted_gcs_url"]

    # Downloaded output.csv must also contain the updated scaled_image_url
    # and original input_url
    dl_resp = client.get(f"/api/requests/{req_id}/download-output")
    assert dl_resp.status_code == 200
    assert retry_item["converted_gcs_url"] in dl_resp.text
    assert "synthetic://catalog/sku-switch?w=300&h=300" in dl_resp.text


def test_open_source_release_checklist_compliance() -> None:
  """Verify Apache 2.0 headers, README disclaimer, and zero internal links."""
  root = pathlib.Path(__file__).resolve().parent.parent
  forbidden_domain = "corp." + "google.com"
  forbidden_project = "creative-" + "performance-index"

  readme_text = (root / "README.md").read_text(encoding="utf-8")
  assert "This is not an official Google product." in readme_text
  assert ("docs/" + "STORY.md") not in readme_text
  assert ("docs/" + "BRD.md") not in readme_text
  assert forbidden_domain not in readme_text

  req_text = (root / "requirements.txt").read_text(encoding="utf-8")
  assert "https://pypi.org/simple" in req_text
  assert "--hash=sha256:" in req_text
  assert "--require-hashes" in (root / "Dockerfile").read_text(encoding="utf-8")
  assert "--require-hashes" in (root / "Dockerfile.gpu").read_text(
      encoding="utf-8"
  )

  source_files = [
      *root.glob("app/**/*.py"),
      *root.glob("app/static/*.js"),
      *root.glob("app/static/*.css"),
      *root.glob("app/static/*.html"),
      *root.glob("tests/*.py"),
      *root.glob("tests/*.js"),
      *root.glob("scripts/*.sh"),
      *root.glob("terraform/*.tf"),
      root / "Dockerfile",
      root / "Dockerfile.gpu",
      root / "cloudbuild.yaml",
      root / "cloudbuild-terraform.yaml",
  ]
  assert len(source_files) >= 30
  for src_file in source_files:
    content = src_file.read_text(encoding="utf-8")
    assert (
        "Copyright 2026 Google LLC" in content
    ), f"Missing copyright header in {src_file}"
    assert (
        "Licensed under the Apache License, Version 2.0" in content
    ), f"Missing Apache 2.0 header in {src_file}"
    assert forbidden_domain not in content, f"Internal URL found in {src_file}"
    assert (
        forbidden_project not in content
    ), f"Internal project ID found in {src_file}"
    for line_no, line in enumerate(content.splitlines(), 1):
      assert (
          len(line) <= 80
      ), f"{src_file}:{line_no} exceeds 80 chars ({len(line)} chars)"


def test_gmc_additional_image_link_formats_and_bmp_tiff() -> None:
  """Verify GMC additional_image_link formats, 10-URL cap, and BMP/TIFF."""
  # 1. BMP & TIFF magic-byte validation per GMC spec
  bmp_payload = b"BM" + (b"\x00" * 32)
  tiff_le = b"II*\x00" + (b"\x00" * 32)
  tiff_be = b"MM\x00*" + (b"\x00" * 32)
  assert inspect_image_magic_bytes(bmp_payload) == "BMP"
  assert inspect_image_magic_bytes(tiff_le) == "TIFF"
  assert inspect_image_magic_bytes(tiff_be) == "TIFF"

  # 2. Quoted URLs containing commas + %2C encoded commas
  quoted_csv = (
      "id,image_link,additional_image_link\n"
      "SKU-QUOTED,https://example.com/main.jpg,"
      '"https://example.com/img2,3.jpg","https://example.com/img2,4.jpg"\n'
      "SKU-ENC,https://example.com/main2.jpg,"
      "https://example.com/img2%2C3.jpg,https://example.com/img2%2C4.jpg\n"
  )
  parsed_quoted = parse_delimited_feed(quoted_csv)
  assert len(parsed_quoted) == 2
  assert parsed_quoted[0].additional_image_links == [
      "https://example.com/img2,3.jpg",
      "https://example.com/img2,4.jpg",
  ]
  assert parsed_quoted[1].additional_image_links == [
      "https://example.com/img2%2C3.jpg",
      "https://example.com/img2%2C4.jpg",
  ]

  # 3. Repeated additional_image_link headers + numbered headers + >10 cap
  repeated_csv = (
      "id,image_link,additional_image_link,additional_image_link,"
      "additional_image_link_3\n"
      "SKU-REP,https://example.com/main.jpg,https://example.com/a1.jpg,"
      "https://example.com/a2.jpg,https://example.com/a3.jpg\n"
  )
  parsed_rep = parse_delimited_feed(repeated_csv)
  assert parsed_rep[0].additional_image_links == [
      "https://example.com/a1.jpg",
      "https://example.com/a2.jpg",
      "https://example.com/a3.jpg",
  ]

  twelve_urls = ",".join(
      f"https://example.com/extra_{i}.jpg" for i in range(1, 13)
  )
  cap_csv = (
      "id,image_link,additional_image_link\n"
      f"SKU-CAP,https://example.com/main.jpg,{twelve_urls}\n"
  )
  parsed_cap = parse_delimited_feed(cap_csv)
  assert len(parsed_cap[0].additional_image_links) == 10

  # 4. Additional-only supplemental feed (no image_link column)
  add_only_csv = (
      "id,additional_image_link\n"
      "SKU-ADD-ONLY,synthetic://catalog/side?w=300&h=300|"
      "synthetic://catalog/back?w=300&h=300\n"
  )
  parsed_add_only = parse_delimited_feed(add_only_csv)
  assert len(parsed_add_only) == 1
  assert not parsed_add_only[0].image_link
  assert len(parsed_add_only[0].additional_image_links) == 2

  # 5. End-to-end API request + output CSV + retry with additional_image_link
  client = TestClient(app)
  s_resp = client.get("/api/session")
  assert s_resp.status_code == 200
  csrf_token = s_resp.json()["csrf_token"]
  login_resp = client.post(
      "/api/auth/login",
      headers={"X-CSRF-Token": csrf_token},
      json={"access_code": DEPLOYMENT_ACCESS_CODE},
  )
  assert login_resp.status_code == 200

  e2e_csv = (
      "id,image_link,additional_image_link\n"
      "SKU-MULTI,synthetic://retail-jp/SKU-MULTI/300x300/main.jpg,"
      '"synthetic://retail-jp/SKU-MULTI/320x240/side.jpg,'
      'synthetic://retail-jp/SKU-MULTI/1600x1600/back.jpg"\n'
  )
  create_resp = client.post(
      "/api/requests",
      headers={"X-CSRF-Token": csrf_token},
      json={
          "filename": "multi_images.csv",
          "csv_content": e2e_csv,
          "engine_id": "local-lanczos-canvas",
          "target_resolution": 1500,
      },
  )
  assert create_resp.status_code == 200
  req_data = create_resp.json()
  assert req_data["total_skus"] == 1
  assert req_data["total_images"] == 3
  assert req_data["upscaled_images"] == 2
  assert req_data["skipped_images"] == 1
  req_id = req_data["request_id"]

  detail_resp = client.get(f"/api/requests/{req_id}")
  assert detail_resp.status_code == 200
  items = detail_resp.json()["items"]
  assert len(items) == 3

  dl_resp = client.get(f"/api/requests/{req_id}/download-output")
  assert dl_resp.status_code == 200
  dl_lines = dl_resp.text.strip().splitlines()
  assert dl_lines[0] == (
      "id,input_url,scaled_image_url,"
      "additional_image_link,additional_scaled_image_link"
  )
  assert len(dl_lines) == 2
  assert "synthetic://retail-jp/SKU-MULTI/320x240/side.jpg" in dl_lines[1]
  assert dl_lines[1].count("https://storage.googleapis.com/") == 3

  retry_resp = client.post(
      f"/api/requests/{req_id}/skus/SKU-MULTI/retry",
      headers={"X-CSRF-Token": csrf_token},
      json={"engine_id": "realesrgan"},
  )
  assert retry_resp.status_code == 200
  retried_items = retry_resp.json()["items"]
  assert len(retried_items) == 3
  assert all(it["engine_id"] == "realesrgan" for it in retried_items)
