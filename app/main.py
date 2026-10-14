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

"""FastAPI BFF server, access-code auth gate, and backend API endpoints."""

from __future__ import annotations

import asyncio
import pathlib
import secrets
from typing import Any, Dict, List, Optional

from app.config import DEPLOYMENT_ACCESS_CODE
from app.config import ENGINE_CATALOG
from app.config import settings
from app.db import request_db
from app.db import utc_now_iso_and_yyyymmdd
from app.engines.vertex_nano_banana import build_vertex_batch_jsonl_line
from app.models import CatalogAuditReport
from app.models import SupplementalFeedRow
from app.models import UpscalePipelineReport
from app.models import UpscaleRequestDetailResponse
from app.models import UpscaleRequestRecord
from app.pipeline.auditor import audit_catalog_feed
from app.pipeline.auditor import fetch_image_bytes
from app.pipeline.feed_io import ensure_output_csv_has_input_url
from app.pipeline.feed_io import parse_delimited_feed
from app.pipeline.feed_io import serialize_gmc_supplemental_only_csv
from app.pipeline.feed_io import serialize_supplemental_feed
from app.pipeline.gcs_uploader import PublicGcsUploader
from app.pipeline.orchestrator import create_and_process_request
from app.pipeline.orchestrator import maybe_resume_stale_request
from app.pipeline.orchestrator import retry_failed_skus
from app.pipeline.orchestrator import retry_single_sku
from app.pipeline.orchestrator import run_upscale_pipeline
from app.pipeline.orchestrator import submit_request_async
from app.pipeline.synthetic_catalog import generate_synthetic_product_image
from app.pipeline.synthetic_catalog import SAMPLE_ANCHOR_FEED_CSV
from app.pricing import GcpComponentUnitRates
from app.pricing import pricing_service
from app.pricing import PricingCatalogSnapshot
from app.security import create_auth_session_cookie
from app.security import generate_csrf_token
from app.security import inspect_image_magic_bytes
from app.security import safe_resolve_within_sandbox
from app.security import SecurityHeadersMiddleware
from app.security import validate_csv_upload_filename
from app.security import validate_external_image_url
from app.security import verify_auth_session_cookie
from app.security import verify_csrf_token
from app.security import verify_deployment_access_code
from fastapi import Depends
from fastapi import FastAPI
from fastapi import File
from fastapi import Form
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi import UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel
from pydantic import Field

STATIC_DIR = pathlib.Path(__file__).parent / "static"

app = FastAPI(
    title=(
        "ImageUpscaler — Automated GMC Product Image Super-Resolution & "
        "Compliance Pipeline"
    ),
    version="1.0.0",
    description=(
        "All-in-one Terraform-deployed GMC image super-resolution system "
        "with Access Code Frontend, "
        "Scalable Backend Engine, Lightweight SQLite+GCS DB, and daily gs://"
        "<bucket>/status/{YYYYMMDD}/log.csv logging."
    ),
)

app.add_middleware(SecurityHeadersMiddleware)
allowed_origins = [
    o.strip() for o in settings.allowed_cors_origins.split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=(
        r"https://.*\.run\.app|http://localhost(:\d+)?|http://"
        r"127\.0\.0\.1(:\d+)?"
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token", "X-Access-Code"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _get_session_cookie_name(request: Request) -> str:
  if request.url.scheme == "https":
    return "__Host-gmc_upscaler_sid"
  return "gmc_upscaler_sid"


def _get_auth_cookie_name(request: Request) -> str:
  if request.url.scheme == "https":
    return "__Host-gmc_upscaler_auth"
  return "gmc_upscaler_auth"


def _extract_session_id(request: Request) -> Optional[str]:
  return (
      request.cookies.get(_get_session_cookie_name(request))
      or request.cookies.get("__Host-gmc_upscaler_sid")
      or request.cookies.get("gmc_upscaler_sid")
  )


def _is_request_authenticated(
    request: Request, x_access_code: Optional[str] = None
) -> bool:
  """Return True if the request has a valid access code or auth cookie."""
  header_code = x_access_code or request.headers.get("X-Access-Code")
  if header_code and verify_deployment_access_code(header_code):
    return True

  session_id = _extract_session_id(request)
  auth_cookie = (
      request.cookies.get(_get_auth_cookie_name(request))
      or request.cookies.get("__Host-gmc_upscaler_auth")
      or request.cookies.get("gmc_upscaler_auth")
  )
  if (
      session_id
      and auth_cookie
      and verify_auth_session_cookie(session_id, auth_cookie)
  ):
    return True
  return False


def require_csrf_protection(
    request: Request,
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    x_access_code: Optional[str] = Header(default=None, alias="X-Access-Code"),
) -> None:
  """Validate Double-Submit HMAC CSRF token or X-Access-Code on POST."""
  if x_access_code and verify_deployment_access_code(x_access_code):
    return
  session_id = _extract_session_id(request)
  if (
      not session_id
      or not x_csrf_token
      or not verify_csrf_token(session_id, x_csrf_token)
  ):
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="CSRF token validation failed or missing session cookie.",
    )


def require_access_code_auth(
    request: Request,
    x_access_code: Optional[str] = Header(default=None, alias="X-Access-Code"),
) -> None:
  """Require valid deployment Access Code authentication."""
  if not _is_request_authenticated(request, x_access_code=x_access_code):
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Access Code authentication required. Please log in with the "
            "deployment Access Code."
        ),
    )


class AccessCodeLoginRequest(BaseModel):
  """Request payload for logging in with the deployment Access Code."""

  access_code: str = Field(..., min_length=1, max_length=256)


class CreateUpscaleRequestPayload(BaseModel):
  """Request payload for submitting a CSV upscaling job to the backend."""

  filename: str = Field(
      default="uploaded_feed.csv", min_length=1, max_length=255
  )
  csv_content: str = Field(..., min_length=5, max_length=10_000_000)
  engine_id: str = Field(default="local-lanczos-canvas")
  target_resolution: int = Field(default=500, description="500 or 1500")
  async_job: bool = Field(
      default=False,
      description=(
          "If True, immediately enqueue job as PROCESSING and run in "
          "background without blocking."
      ),
  )


class RetrySkuPayload(BaseModel):
  """Optional payload when retrying an individual SKU or all failed SKUs."""

  engine_id: Optional[str] = Field(
      default=None,
      description=(
          "Optional override engine_id for the retry. Defaults to the "
          "request's engine_id."
      ),
  )


class SyncPricingPayload(BaseModel):
  """Optional payload for syncing or overriding GCP component unit rates."""

  unit_rates: Optional[GcpComponentUnitRates] = Field(default=None)


class FeedTextAuditRequest(BaseModel):
  """Request body for auditing CSV/TSV text."""

  csv_content: str = Field(..., min_length=5, max_length=5_000_000)


class FeedTextUpscaleRequest(BaseModel):
  """Request body for running the upscaling pipeline on CSV/TSV text."""

  csv_content: str = Field(..., min_length=5, max_length=5_000_000)
  target_resolution: int = Field(default=500, description="500 or 1500")
  engine_id: str = Field(default="local-lanczos-canvas")
  public_gcs_bucket: Optional[str] = Field(default=None)


class SupplementalExportRequest(BaseModel):
  """Request body for downloading a 3-column GMC Supplemental Feed."""

  rows: List[SupplementalFeedRow]
  format: str = Field(default="csv", pattern="^(csv|tsv)$")


def _should_proxy_to_backend() -> bool:
  """Return True when this instance proxies to a remote BACKEND_URL."""
  return settings.service_role == "frontend" and bool(settings.backend_url)


_shared_proxy_client: Optional[httpx.AsyncClient] = None
_warmup_tasks: set[asyncio.Task[Any]] = set()


def _get_proxy_client(timeout_sec: float = 120.0) -> httpx.AsyncClient:
  """Return a shared persistent HTTP client pool for BFF calls."""
  global _shared_proxy_client
  if _shared_proxy_client is None or _shared_proxy_client.is_closed:
    _shared_proxy_client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_sec, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    )
  return _shared_proxy_client


async def _background_warm_db_and_backend() -> None:
  """Pre-warm Firestore request list cache, pricing config, and backend."""
  try:
    await asyncio.to_thread(pricing_service.get_current_pricing, True)
    await asyncio.to_thread(request_db.list_requests, 200)
  except Exception:  # pylint: disable=broad-exception-caught
    pass
  if _should_proxy_to_backend():
    try:
      client = _get_proxy_client(timeout_sec=15.0)
      await client.get(f"{settings.backend_url}/healthz")
    except Exception:  # pylint: disable=broad-exception-caught
      pass


def _schedule_warmup() -> None:
  """Trigger non-blocking background warmup while viewing login screen."""
  try:
    loop = asyncio.get_running_loop()
    task = loop.create_task(_background_warm_db_and_backend())
    _warmup_tasks.add(task)
    task.add_done_callback(_warmup_tasks.discard)
  except RuntimeError:
    pass


async def _notify_backend_resume_async(
    request_id: Optional[str] = None,
) -> None:
  """Trigger non-blocking backend call to resume stale PROCESSING jobs."""
  if not _should_proxy_to_backend():
    return
  try:
    client = _get_proxy_client(timeout_sec=20.0)
    target_path = (
        f"/api/requests/{request_id}" if request_id else "/api/requests"
    )
    await client.get(
        f"{settings.backend_url}{target_path}",
        headers={"X-Access-Code": DEPLOYMENT_ACCESS_CODE},
    )
  except Exception:  # pylint: disable=broad-exception-caught
    pass


@app.get("/healthz")
async def healthcheck() -> Dict[str, str]:
  """Cloud Run readiness/liveness probe endpoint."""
  return {"status": "ok", "service_role": settings.service_role}


@app.get("/", include_in_schema=False)
async def serve_dashboard() -> FileResponse:
  """Serve the secure single-page Web Dashboard."""
  return FileResponse(STATIC_DIR / "index.html")


@app.get("/billing", include_in_schema=False)
async def serve_billing_page() -> FileResponse:
  """Serve the SPA dashboard for direct URL navigation to `/billing`."""
  return FileResponse(STATIC_DIR / "index.html")


@app.get("/request/{request_id}", include_in_schema=False)
async def serve_request_detail_page(request_id: str) -> FileResponse:
  """Serve the SPA dashboard for direct navigation to a request page."""
  del request_id
  return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/session")
async def init_session(request: Request, response: Response) -> Dict[str, Any]:
  """Initialize session cookie, check auth status, and return CSRF token."""
  cookie_name = _get_session_cookie_name(request)
  session_id = _extract_session_id(request)
  if not session_id:
    session_id = secrets.token_hex(24)
    response.set_cookie(
        key=cookie_name,
        value=session_id,
        httponly=True,
        secure=(request.url.scheme == "https"),
        samesite="lax",
        max_age=3600 * 8,
        path="/",
    )

  csrf_token = generate_csrf_token(session_id)
  _, today_yyyymmdd = utc_now_iso_and_yyyymmdd()
  authenticated = _is_request_authenticated(request)
  # Non-blocking pricing snapshot so initial page boot never waits on cold
  # Firestore
  pricing_snap = pricing_service.get_current_pricing(
      allow_blocking_db_load=False
  )
  _schedule_warmup()

  initial_requests: Optional[List[Dict[str, Any]]] = None
  if authenticated:
    try:
      records = await asyncio.to_thread(request_db.list_requests, 200)
      initial_requests = [r.model_dump() for r in records]
    except Exception:  # pylint: disable=broad-exception-caught
      initial_requests = None

  engines = [
      {
          "engine_id": spec.engine_id,
          "display_name": spec.display_name,
          "role": spec.role,
          "machine_spec": spec.machine_spec,
          "cost_500_per_img": spec.cost_500_per_img,
          "cost_1500_per_img": spec.cost_1500_per_img,
          "cost_500_std_per_img": spec.cost_500_std_per_img,
          "cost_1500_std_per_img": spec.cost_1500_std_per_img,
          "zero_hallucination": spec.zero_hallucination,
          "is_vertex_ai": spec.is_vertex_ai,
          "key_characteristics": spec.key_characteristics,
          "when_recommended": spec.when_recommended,
      }
      for spec in ENGINE_CATALOG.values()
  ]
  payload: Dict[str, Any] = {
      "authenticated": authenticated,
      "csrf_token": csrf_token,
      "service_role": settings.service_role,
      "default_bucket": settings.public_gcs_bucket,
      "today_yyyymmdd": today_yyyymmdd,
      "daily_log_gcs_uri": (
          f"gs://{settings.public_gcs_bucket}/status/{today_yyyymmdd}/log.csv"
      ),
      "sample_feed_csv": SAMPLE_ANCHOR_FEED_CSV.strip(),
      "engines": engines,
      "pricing": pricing_snap.model_dump(),
  }
  if initial_requests is not None:
    payload["requests"] = initial_requests
  return payload


@app.post("/api/auth/login", dependencies=[Depends(require_csrf_protection)])
async def api_login_with_access_code(
    payload: AccessCodeLoginRequest,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
  """Verify the deployment Access Code and issue HttpOnly auth cookie."""
  if not verify_deployment_access_code(payload.access_code):
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Invalid Access Code. Please enter the Access Code "
            "configured at deployment."
        ),
    )

  session_id = _extract_session_id(request)
  if not session_id:
    session_id = secrets.token_hex(24)
    response.set_cookie(
        key=_get_session_cookie_name(request),
        value=session_id,
        httponly=True,
        secure=(request.url.scheme == "https"),
        samesite="lax",
        max_age=3600 * 8,
        path="/",
    )

  auth_badge = create_auth_session_cookie(session_id)
  response.set_cookie(
      key=_get_auth_cookie_name(request),
      value=auth_badge,
      httponly=True,
      secure=(request.url.scheme == "https"),
      samesite="lax",
      max_age=3600 * 8,
      path="/",
  )
  _schedule_warmup()
  try:
    records = await asyncio.to_thread(request_db.list_requests, 200)
    return {
        "authenticated": True,
        "requests": [r.model_dump() for r in records],
        "pricing": (
            pricing_service.get_current_pricing(
                allow_blocking_db_load=False
            ).model_dump()
        ),
    }
  except Exception:  # pylint: disable=broad-exception-caught
    return {"authenticated": True}


@app.post("/api/auth/logout", dependencies=[Depends(require_csrf_protection)])
async def api_logout(request: Request, response: Response) -> Dict[str, Any]:
  """Invalidate session and authentication cookies on logout."""
  response.delete_cookie(_get_auth_cookie_name(request), path="/")
  response.delete_cookie(_get_session_cookie_name(request), path="/")
  return {"authenticated": False}


@app.post(
    "/api/requests",
    response_model=UpscaleRequestRecord,
    dependencies=[
        Depends(require_csrf_protection),
        Depends(require_access_code_auth),
    ],
)
async def api_submit_upscale_request(
    payload: CreateUpscaleRequestPayload,
) -> UpscaleRequestRecord:
  """Submit a new CSV upscaling request and process via Backend Engine."""
  if _should_proxy_to_backend():
    client = _get_proxy_client(timeout_sec=180.0)
    resp = await client.post(
        f"{settings.backend_url}/api/requests",
        headers={"X-Access-Code": DEPLOYMENT_ACCESS_CODE},
        json=payload.model_dump(),
    )
    if resp.status_code >= 400:
      raise HTTPException(status_code=resp.status_code, detail=resp.text)
    rec = UpscaleRequestRecord(**resp.json())
    request_db._upsert_cached_request(rec)  # pylint: disable=protected-access
    return rec

  try:
    clean_filename = validate_csv_upload_filename(payload.filename)
    if payload.async_job:
      return await submit_request_async(
          csv_content=payload.csv_content,
          filename=clean_filename,
          engine_id=payload.engine_id,
          target_px=payload.target_resolution,
      )
    return await create_and_process_request(
        csv_content=payload.csv_content,
        filename=clean_filename,
        engine_id=payload.engine_id,
        target_px=payload.target_resolution,
    )
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc


@app.post(
    "/api/requests/upload",
    response_model=UpscaleRequestRecord,
    dependencies=[
        Depends(require_csrf_protection),
        Depends(require_access_code_auth),
    ],
)
async def api_submit_upscale_file(
    file: UploadFile = File(...),
    engine_id: str = Form(default="local-lanczos-canvas"),
    target_resolution: int = Form(default=500),
    async_job: bool = Form(default=False),
) -> UpscaleRequestRecord:
  """Upload a CSV/TSV file directly and process via Backend Engine."""
  try:
    clean_name = validate_csv_upload_filename(
        file.filename or "uploaded_feed.csv"
    )
    raw_bytes = await file.read()
    if len(raw_bytes) > settings.max_upload_bytes:
      raise ValueError(
          "Uploaded file exceeds size limit "
          f"({settings.max_upload_bytes} bytes)"
      )
    csv_text = raw_bytes.decode("utf-8-sig")
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc

  payload = CreateUpscaleRequestPayload(
      filename=clean_name,
      csv_content=csv_text,
      engine_id=engine_id,
      target_resolution=target_resolution,
      async_job=async_job,
  )
  return await api_submit_upscale_request(payload)


@app.get(
    "/api/requests",
    response_model=List[UpscaleRequestRecord],
    dependencies=[Depends(require_access_code_auth)],
)
async def api_list_all_requests() -> List[UpscaleRequestRecord]:
  """Return all upscaling requests from Cloud Firestore."""
  records = await asyncio.to_thread(request_db.list_requests, 200)
  has_processing = any(r.status == "PROCESSING" for r in records[:10])
  if has_processing:
    if _should_proxy_to_backend():
      task = asyncio.create_task(_notify_backend_resume_async())
      _warmup_tasks.add(task)
      task.add_done_callback(_warmup_tasks.discard)
    else:
      for r in records[:10]:
        if r.status == "PROCESSING":
          maybe_resume_stale_request(r)
  return records


@app.get(
    "/api/requests/{request_id}",
    response_model=UpscaleRequestDetailResponse,
    dependencies=[Depends(require_access_code_auth)],
)
async def api_get_request_detail(
    request_id: str,
) -> UpscaleRequestDetailResponse:
  """Return full details and per-SKU conversion rows for a request."""
  rec = await asyncio.to_thread(request_db.get_request, request_id)
  if not rec:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Request ID not found"
    )
  if rec.status == "PROCESSING":
    if _should_proxy_to_backend():
      task = asyncio.create_task(_notify_backend_resume_async(request_id))
      _warmup_tasks.add(task)
      task.add_done_callback(_warmup_tasks.discard)
    else:
      maybe_resume_stale_request(rec)
  items = await asyncio.to_thread(
      request_db.get_conversion_logs_by_request, request_id
  )
  return UpscaleRequestDetailResponse(request=rec, items=items)


@app.post(
    "/api/requests/{request_id}/skus/{sku_id}/retry",
    response_model=UpscaleRequestDetailResponse,
    dependencies=[
        Depends(require_csrf_protection),
        Depends(require_access_code_auth),
    ],
)
async def api_retry_single_sku(
    request_id: str,
    sku_id: str,
    payload: Optional[RetrySkuPayload] = None,
) -> UpscaleRequestDetailResponse:
  """Retry a single SKU in a request and update output.csv and billing."""
  body_dict = payload.model_dump() if payload else {}
  if _should_proxy_to_backend():
    client = _get_proxy_client(timeout_sec=120.0)
    resp = await client.post(
        f"{settings.backend_url}/api/requests/{request_id}/skus/{sku_id}/retry",
        headers={"X-Access-Code": DEPLOYMENT_ACCESS_CODE},
        json=body_dict,
    )
    if resp.status_code >= 400:
      raise HTTPException(status_code=resp.status_code, detail=resp.text)
    res_obj = UpscaleRequestDetailResponse(**resp.json())
    # pylint: disable=protected-access
    request_db._upsert_cached_request(res_obj.request)
    request_db.cache_conversion_logs_for_request(
        request_id=request_id,
        entries=res_obj.items,
        updated_at_utc=res_obj.request.updated_at_utc,
    )
    return res_obj

  try:
    updated_rec, items = await retry_single_sku(
        request_id=request_id,
        sku_id=sku_id,
        engine_id=payload.engine_id if payload else None,
    )
    return UpscaleRequestDetailResponse(request=updated_rec, items=items)
  except ValueError as exc:
    code = (
        status.HTTP_404_NOT_FOUND
        if "not found" in str(exc).lower()
        else status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=code, detail=str(exc)) from exc


@app.post(
    "/api/requests/{request_id}/retry-failed",
    response_model=UpscaleRequestDetailResponse,
    dependencies=[
        Depends(require_csrf_protection),
        Depends(require_access_code_auth),
    ],
)
async def api_retry_all_failed_skus(
    request_id: str,
    payload: Optional[RetrySkuPayload] = None,
) -> UpscaleRequestDetailResponse:
  """Retry all failed SKUs in a request and update output.csv and billing."""
  body_dict = payload.model_dump() if payload else {}
  if _should_proxy_to_backend():
    client = _get_proxy_client(timeout_sec=180.0)
    resp = await client.post(
        f"{settings.backend_url}/api/requests/{request_id}/retry-failed",
        headers={"X-Access-Code": DEPLOYMENT_ACCESS_CODE},
        json=body_dict,
    )
    if resp.status_code >= 400:
      raise HTTPException(status_code=resp.status_code, detail=resp.text)
    res_obj = UpscaleRequestDetailResponse(**resp.json())
    # pylint: disable=protected-access
    request_db._upsert_cached_request(res_obj.request)
    request_db.cache_conversion_logs_for_request(
        request_id=request_id,
        entries=res_obj.items,
        updated_at_utc=res_obj.request.updated_at_utc,
    )
    return res_obj

  try:
    updated_rec, items = await retry_failed_skus(
        request_id=request_id,
        engine_id=payload.engine_id if payload else None,
    )
    return UpscaleRequestDetailResponse(request=updated_rec, items=items)
  except ValueError as exc:
    code = (
        status.HTTP_404_NOT_FOUND
        if "not found" in str(exc).lower()
        else status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=code, detail=str(exc)) from exc


@app.get(
    "/api/pricing",
    response_model=PricingCatalogSnapshot,
    dependencies=[Depends(require_access_code_auth)],
)
async def api_get_pricing_catalog() -> PricingCatalogSnapshot:
  """Return the active GCP Pricing Catalog snapshot."""
  return await asyncio.to_thread(pricing_service.get_current_pricing, True)


@app.post(
    "/api/pricing/sync",
    response_model=PricingCatalogSnapshot,
    dependencies=[
        Depends(require_csrf_protection),
        Depends(require_access_code_auth),
    ],
)
async def api_sync_gcp_pricing(
    payload: Optional[SyncPricingPayload] = None,
) -> PricingCatalogSnapshot:
  """Synchronize unit prices with the Google Cloud Billing Catalog API."""
  body_dict = payload.model_dump(exclude_none=True) if payload else {}
  if _should_proxy_to_backend():
    client = _get_proxy_client(timeout_sec=30.0)
    resp = await client.post(
        f"{settings.backend_url}/api/pricing/sync",
        headers={"X-Access-Code": DEPLOYMENT_ACCESS_CODE},
        json=body_dict,
    )
    if resp.status_code >= 400:
      raise HTTPException(status_code=resp.status_code, detail=resp.text)
    snap = PricingCatalogSnapshot(**resp.json())
    # pylint: disable=protected-access
    pricing_service._compute_and_apply_engine_catalog(snap.unit_rates)
    return snap

  return await pricing_service.sync_with_gcp_catalog(
      custom_rates=payload.unit_rates if payload else None
  )


@app.get(
    "/api/requests/{request_id}/download-output",
    dependencies=[Depends(require_access_code_auth)],
)
async def api_download_request_output_csv(request_id: str) -> Response:
  """Download the generated Output CSV file for a completed request."""
  safe_id = "".join(c for c in request_id if c.isalnum() or c in ("-", "_"))
  rec = await asyncio.to_thread(request_db.get_request, request_id)
  if not rec:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Request ID not found"
    )
  in_csv, output_csv = await asyncio.to_thread(
      request_db.get_request_csvs, request_id
  )
  if not output_csv:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Output CSV is not ready yet",
    )
  output_csv = ensure_output_csv_has_input_url(in_csv, output_csv)

  return Response(
      content=output_csv,
      media_type="text/csv; charset=utf-8",
      headers={
          "Content-Disposition": f'attachment; filename="out-{safe_id}.csv"',
          "X-Content-Type-Options": "nosniff",
      },
  )


@app.get(
    "/api/requests/{request_id}/download-supplemental",
    dependencies=[Depends(require_access_code_auth)],
)
async def api_download_request_supplemental_csv(request_id: str) -> Response:
  """Download strict GMC Supplemental Format Output CSV for a request."""
  safe_id = "".join(c for c in request_id if c.isalnum() or c in ("-", "_"))
  rec = await asyncio.to_thread(request_db.get_request, request_id)
  if not rec:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Request ID not found"
    )
  in_csv, output_csv = await asyncio.to_thread(
      request_db.get_request_csvs, request_id
  )
  if not output_csv:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Output CSV is not ready yet",
    )
  supp_csv = serialize_gmc_supplemental_only_csv(in_csv, output_csv)

  return Response(
      content=supp_csv,
      media_type="text/csv; charset=utf-8",
      headers={
          "Content-Disposition": (
              f'attachment; filename="supplemental-{safe_id}.csv"'
          ),
          "X-Content-Type-Options": "nosniff",
      },
  )


@app.get(
    "/api/requests/{request_id}/download-input",
    dependencies=[Depends(require_access_code_auth)],
)
async def api_download_request_input_csv(request_id: str) -> Response:
  """Download the original Input CSV file for a request."""
  safe_id = "".join(c for c in request_id if c.isalnum() or c in ("-", "_"))
  input_csv, _ = await asyncio.to_thread(
      request_db.get_request_csvs, request_id
  )
  if not input_csv:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Input CSV not found"
    )

  return Response(
      content=input_csv,
      media_type="text/csv; charset=utf-8",
      headers={
          "Content-Disposition": f'attachment; filename="in-{safe_id}.csv"',
          "X-Content-Type-Options": "nosniff",
      },
  )


@app.get(
    "/api/status-log/{date_yyyymmdd}",
    dependencies=[Depends(require_access_code_auth)],
)
async def api_download_daily_status_log(date_yyyymmdd: str) -> Response:
  """Download the daily cumulative conversion log CSV from GCS."""
  safe_date = "".join(c for c in date_yyyymmdd if c.isdigit())[:8]
  if not safe_date:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid YYYYMMDD date",
    )

  uploader = PublicGcsUploader()
  csv_text = await asyncio.to_thread(uploader.read_daily_status_log, safe_date)
  return Response(
      content=csv_text,
      media_type="text/csv; charset=utf-8",
      headers={
          "Content-Disposition": f'attachment; filename="log_{safe_date}.csv"',
          "X-Content-Type-Options": "nosniff",
      },
  )


@app.post(
    "/api/audit",
    response_model=CatalogAuditReport,
    dependencies=[Depends(require_csrf_protection)],
)
async def api_audit_catalog(
    payload: FeedTextAuditRequest,
) -> CatalogAuditReport:
  """Run Stage 1 Dimension Auditor & Cost Matrix calculation on feed text."""
  if _should_proxy_to_backend():
    async with httpx.AsyncClient(timeout=60.0) as client:
      resp = await client.post(
          f"{settings.backend_url}/api/audit",
          headers={"X-Access-Code": DEPLOYMENT_ACCESS_CODE},
          json=payload.model_dump(),
      )
      if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
      return CatalogAuditReport(**resp.json())

  try:
    rows = parse_delimited_feed(payload.csv_content)
    return await audit_catalog_feed(rows)
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc


@app.post(
    "/api/audit-file",
    response_model=CatalogAuditReport,
    dependencies=[Depends(require_csrf_protection)],
)
async def api_audit_uploaded_file(
    file: UploadFile = File(...),
) -> CatalogAuditReport:
  """Validate and audit an uploaded CSV/TSV feed file."""
  try:
    validate_csv_upload_filename(file.filename or "")
    raw_bytes = await file.read()
    if len(raw_bytes) > settings.max_upload_bytes:
      raise ValueError(
          "Uploaded file exceeds size limit "
          f"({settings.max_upload_bytes} bytes)"
      )
    text = raw_bytes.decode("utf-8-sig")
    rows = parse_delimited_feed(text)
    return await audit_catalog_feed(rows)
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc


@app.post(
    "/api/upscale",
    response_model=UpscalePipelineReport,
    dependencies=[Depends(require_csrf_protection)],
)
async def api_run_upscale(
    payload: FeedTextUpscaleRequest,
) -> UpscalePipelineReport:
  """Execute the upscaling pipeline and return the GMC Supplemental Feed."""
  if _should_proxy_to_backend():
    async with httpx.AsyncClient(timeout=180.0) as client:
      resp = await client.post(
          f"{settings.backend_url}/api/upscale",
          headers={"X-Access-Code": DEPLOYMENT_ACCESS_CODE},
          json=payload.model_dump(),
      )
      if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
      return UpscalePipelineReport(**resp.json())

  try:
    rows = parse_delimited_feed(payload.csv_content)
    uploader = PublicGcsUploader(
        bucket_name=payload.public_gcs_bucket or settings.public_gcs_bucket
    )
    report = await run_upscale_pipeline(
        rows=rows,
        target_px=payload.target_resolution,
        engine_id=payload.engine_id,
        uploader=uploader,
    )
    # Also record in Lightweight DB so it appears in the Requests table
    now_iso, date_yyyymmdd = utc_now_iso_and_yyyymmdd()
    out_csv = serialize_supplemental_feed(report.supplemental_feed_rows)
    in_uri = uploader.upload_request_csv(
        report.job_id, "input", payload.csv_content
    )
    out_uri = uploader.upload_request_csv(report.job_id, "output", out_csv)
    rec = UpscaleRequestRecord(
        request_id=report.job_id,
        created_at_utc=now_iso,
        updated_at_utc=now_iso,
        date_yyyymmdd=date_yyyymmdd,
        filename="interactive_feed.csv",
        engine_id=payload.engine_id,
        target_resolution=payload.target_resolution,
        status="COMPLETED",
        total_skus=report.total_skus_processed,
        total_images=report.unique_urls_evaluated,
        upscaled_images=report.urls_upscaled,
        skipped_images=report.urls_skipped_already_compliant,
        failed_images=report.urls_failed,
        estimated_cost_usd=report.estimated_cost_usd,
        duration_ms=report.total_duration_ms,
        input_gcs_uri=in_uri,
        output_gcs_uri=out_uri,
        daily_log_gcs_uri=report.daily_log_gcs_uri,
    )
    request_db.save_request(
        rec, input_csv_text=payload.csv_content, output_csv_text=out_csv
    )
    return report
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc


@app.post("/api/export-feed", dependencies=[Depends(require_csrf_protection)])
async def api_export_supplemental_feed(
    payload: SupplementalExportRequest,
) -> Response:
  """Download the strict 3-column GMC Supplemental Feed as CSV or TSV."""
  try:
    delimiter = "\t" if payload.format == "tsv" else ","
    ext = "tsv" if payload.format == "tsv" else "csv"
    media_type = (
        "text/tab-separated-values" if payload.format == "tsv" else "text/csv"
    )
    serialized = serialize_supplemental_feed(payload.rows, delimiter=delimiter)
    return Response(
        content=serialized,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="gmc_supplemental_feed.{ext}"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc


@app.post(
    "/api/vertex-batch-jsonl", dependencies=[Depends(require_csrf_protection)]
)
async def api_export_vertex_batch_jsonl(
    csv_content: str = Form(...),
    model_id: str = Form(default="gemini-3.1-flash-lite-image"),
    target_resolution: int = Form(default=500),
) -> PlainTextResponse:
  """Generate a Vertex AI Batch Prediction JSONL input file."""
  try:
    rows = parse_delimited_feed(csv_content)
    lines: List[str] = []
    seen = set()
    for row in rows:
      for url in [row.image_link, *row.additional_image_links]:
        if url and url not in seen:
          seen.add(url)
          lines.append(
              build_vertex_batch_jsonl_line(
                  custom_id=f"{row.id}:{len(lines)}",
                  gcs_source_uri=url,
                  model_id=model_id,
                  target_px=target_resolution,
              )
          )
    return PlainTextResponse(
        content="\n".join(lines) + "\n",
        headers={
            "Content-Disposition": (
                'attachment; filename="vertex_nano_banana_batch.jsonl"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc


@app.get("/api/preview-source")
async def api_preview_synthetic_source(url: str) -> Response:
  """Render a source or converted product image for Web UI comparison.

  Supports:
  - `synthetic://` test catalog URLs
  - `https://storage.googleapis.com/<bucket>/...` (authenticated GCS read
    fallback when Org Policy blocks `allUsers`)
  - External `https://` / `http://` source SKU URLs (SSRF-validated + fetched
    with browser headers to bypass CDN hotlink blocks)
  """
  cleaned = (url or "").strip()
  if not cleaned:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Missing preview URL",
    )

  if cleaned.startswith("synthetic://"):
    img_bytes = generate_synthetic_product_image(cleaned)
    return Response(
        content=img_bytes,
        media_type="image/jpeg",
        headers={
            "Content-Disposition": 'inline; filename="source_preview.jpg"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

  # Check if URL points to our own GCS bucket (handles GCP Org Policy blocking
  # allUsers public access)
  gcs_prefix = f"https://storage.googleapis.com/{settings.public_gcs_bucket}/"
  if cleaned.startswith(gcs_prefix):
    rel_blob_path = cleaned[len(gcs_prefix) :]
    uploader = PublicGcsUploader()
    if uploader.use_mock_gcs:
      try:
        local_file = safe_resolve_within_sandbox(
            uploader.local_storage_dir, rel_blob_path
        )
        if local_file.is_file():
          img_bytes = local_file.read_bytes()
          return Response(
              content=img_bytes,
              media_type="image/jpeg",
              headers={
                  "Content-Disposition": (
                      'inline; filename="converted_preview.jpg"'
                  ),
                  "Cache-Control": "no-cache",
                  "X-Content-Type-Options": "nosniff",
              },
          )
      except Exception:  # pylint: disable=broad-exception-caught
        pass
    else:
      try:

        def _download_gcs_blob() -> Optional[bytes]:
          # pylint: disable=protected-access
          client = uploader._get_gcs_client()
          bucket = client.bucket(uploader.bucket_name)
          blob = bucket.blob(rel_blob_path)
          if blob.exists():
            return blob.download_as_bytes()
          return None

        gcs_bytes = await asyncio.to_thread(_download_gcs_blob)
        if gcs_bytes:
          return Response(
              content=gcs_bytes,
              media_type="image/jpeg",
              headers={
                  "Content-Disposition": (
                      'inline; filename="converted_preview.jpg"'
                  ),
                  "Cache-Control": "no-cache",
                  "X-Content-Type-Options": "nosniff",
              },
          )
      except Exception:  # pylint: disable=broad-exception-caught
        pass

  # External https:// or http:// source SKU image URL
  try:
    safe_url = validate_external_image_url(
        cleaned, allow_synthetic_scheme=False
    )
    client = _get_proxy_client(timeout_sec=20.0)
    img_bytes = await fetch_image_bytes(safe_url, client=client)
    fmt = inspect_image_magic_bytes(img_bytes)
    mime_map = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }
    media_type = mime_map.get(fmt, "image/jpeg")
    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={
            "Content-Disposition": 'inline; filename="sku_preview.jpg"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )
  except Exception as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unable to load preview image: {type(exc).__name__}",
    ) from exc


@app.get("/api/preview/{resolution_dir}/{engine_id}/{filename}")
async def api_serve_upscaled_preview(
    resolution_dir: str, engine_id: str, filename: str
) -> FileResponse:
  """Serve an upscaled image from the local sandbox directory."""
  try:
    target_path = safe_resolve_within_sandbox(
        settings.local_storage_dir,
        f"{resolution_dir}/{engine_id}/{filename}",
    )
  except ValueError as exc:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
    ) from exc

  if not target_path.is_file():
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Preview image not found",
    )

  return FileResponse(
      target_path,
      media_type="image/jpeg",
      headers={
          "Content-Disposition": 'inline; filename="upscaled_preview.jpg"',
          "X-Content-Type-Options": "nosniff",
      },
  )
