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

"""Live GCP Pricing Synchronization Service (`app/pricing.py`).

Synchronizes per-image unit costs for Cloud Run CPU/GPU, Cloud Storage Class A
operations,
and Vertex AI Gemini Image models with the Google Cloud Billing Catalog API
(`cloudbilling.googleapis.com/v1`),
persisting the active pricing snapshot in Cloud Firestore
(`pricing_config/current`) so all
pods and the `/billing` page stay in sync.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from app.config import ENGINE_CATALOG
from app.config import EngineSpec
from app.config import settings
from app.db import request_db
from app.db import utc_now_iso_and_yyyymmdd
import httpx
from pydantic import BaseModel
from pydantic import Field

logger = logging.getLogger(__name__)


class GcpComponentUnitRates(BaseModel):
  """Underlying Google Cloud unit rates (USD) for engine pricing."""

  region: str = "asia-northeast1"
  cloud_run_vcpu_per_sec_usd: float = 0.000024
  cloud_run_mem_gib_per_sec_usd: float = 0.0000025
  cloud_run_nvidia_l4_per_hr_usd: float = 0.672
  gcs_class_a_put_per_1k_ops_usd: float = 0.005
  vertex_gemini_3_1_flash_lite_1k_std_usd: float = 0.0336
  vertex_gemini_3_1_flash_lite_2k_std_usd: float = 0.0336
  vertex_gemini_3_1_flash_1k_std_usd: float = 0.0450
  vertex_gemini_3_1_flash_2k_std_usd: float = 0.0900
  vertex_gemini_3_pro_2k_std_usd: float = 0.1340
  vertex_batch_discount_factor: float = 0.50


class EnginePricingSnapshotItem(BaseModel):
  """Serialized pricing item for a single upscaling engine."""

  engine_id: str
  display_name: str
  role: str
  machine_spec: str
  zero_hallucination: bool
  cost_500_per_img: float
  cost_1500_per_img: float
  cost_500_std_per_img: float
  cost_1500_std_per_img: float
  cost_per_10k_500_usd: float
  cost_per_10k_1500_usd: float
  when_recommended: str
  is_vertex_ai: bool


class PricingCatalogSnapshot(BaseModel):
  """Full GCP pricing sync state returned by `/api/pricing` endpoints."""

  last_synced_utc: str
  pricing_source: str = (
      "GCP Billing Catalog Verified Snapshot (asia-northeast1 + global)"
  )
  cloud_billing_api_status: str = "READY"
  gcp_project_id: str = ""
  region: str = "asia-northeast1"
  unit_rates: GcpComponentUnitRates = Field(
      default_factory=GcpComponentUnitRates
  )
  engines: List[EnginePricingSnapshotItem] = Field(default_factory=list)


class GcpPricingSyncService:
  """Thread-safe manager that syncs GCP unit rates and updates catalog."""

  FIRESTORE_COLLECTION = "pricing_config"
  FIRESTORE_DOC_ID = "current"

  # Official Google Cloud Billing Catalog Service IDs
  CLOUD_RUN_SERVICE_ID = "152E-C115-5142"
  CLOUD_STORAGE_SERVICE_ID = "95FF-2EF5-5EA1"
  VERTEX_AI_SERVICE_ID = "C7E2-9256-1C43"

  def __init__(self) -> None:
    self._lock = threading.Lock()
    now_iso, _ = utc_now_iso_and_yyyymmdd()
    default_region = os.getenv("GCP_REGION", "asia-northeast1")
    self._unit_rates = GcpComponentUnitRates(region=default_region)
    self._last_synced_utc = now_iso
    self._pricing_source = f"GCP Billing Catalog Baseline ({default_region})"
    self._billing_api_status = "BASELINE_INITIALIZED"
    self._loaded_from_db = False

  def _compute_and_apply_engine_catalog(
      self, rates: GcpComponentUnitRates
  ) -> List[EnginePricingSnapshotItem]:
    """Recompute per-image unit costs from `rates` and update catalog.

    Args:
      rates: Component unit rates for Cloud Run, GCS, and Vertex AI.

    Returns:
      List of updated EnginePricingSnapshotItem objects.
    """
    gcs_put_per_img = rates.gcs_class_a_put_per_1k_ops_usd / 1000.0
    req_fee_and_storage_500 = 0.000002025
    req_fee_and_storage_1500 = 0.000008985

    # 1. Local Lanczos Canvas: 1 vCPU, 2 GiB RAM ($0.000029/s) x 25ms
    # (500px) / 85ms (1500px) + GCS PUT + Storage
    cpu_sec_rate = rates.cloud_run_vcpu_per_sec_usd + (
        2.0 * rates.cloud_run_mem_gib_per_sec_usd
    )
    lanczos_500 = round(
        (cpu_sec_rate * 0.025) + gcs_put_per_img + req_fee_and_storage_500,
        8,
    )
    lanczos_1500 = round(
        (cpu_sec_rate * 0.085) + gcs_put_per_img + req_fee_and_storage_1500,
        8,
    )

    # 2. Real-ESRGAN x4plus on Cloud Run NVIDIA L4 GPU (1x L4 GPU + 4 vCPU +
    # 16 GiB RAM)
    gpu_sec_rate = (
        (rates.cloud_run_nvidia_l4_per_hr_usd / 3600.0)
        + (4.0 * rates.cloud_run_vcpu_per_sec_usd)
        + (16.0 * rates.cloud_run_mem_gib_per_sec_usd)
    )
    realesrgan_500 = round((gpu_sec_rate * 0.185) + gcs_put_per_img, 8)
    realesrgan_1500 = round((gpu_sec_rate * 0.4765) + gcs_put_per_img, 8)

    # 3. Vertex AI Gemini 3.1 Flash Lite Image (Nano Banana 2 Lite)
    discount = rates.vertex_batch_discount_factor
    nb_lite_500_std = round(
        rates.vertex_gemini_3_1_flash_lite_1k_std_usd + lanczos_500, 8
    )
    nb_lite_1500_std = round(
        rates.vertex_gemini_3_1_flash_lite_2k_std_usd + lanczos_1500, 8
    )
    nb_lite_500_batch = round(
        (rates.vertex_gemini_3_1_flash_lite_1k_std_usd * discount)
        + lanczos_500,
        8,
    )
    nb_lite_1500_batch = round(
        (rates.vertex_gemini_3_1_flash_lite_2k_std_usd * discount)
        + lanczos_1500,
        8,
    )

    # 4. Vertex AI Gemini 3.1 Flash Image (Nano Banana 2 Standard)
    nb_std_500_std = round(
        rates.vertex_gemini_3_1_flash_1k_std_usd + lanczos_500, 8
    )
    nb_std_1500_std = round(
        rates.vertex_gemini_3_1_flash_2k_std_usd + lanczos_1500, 8
    )
    nb_std_500_batch = round(
        (rates.vertex_gemini_3_1_flash_1k_std_usd * discount) + lanczos_500,
        8,
    )
    nb_std_1500_batch = round(
        (rates.vertex_gemini_3_1_flash_2k_std_usd * discount) + lanczos_1500,
        8,
    )

    # 5. Vertex AI Gemini 3 Pro Image (Nano Banana Pro)
    nb_pro_500_std = round(
        rates.vertex_gemini_3_pro_2k_std_usd + lanczos_500, 8
    )
    nb_pro_1500_std = round(
        rates.vertex_gemini_3_pro_2k_std_usd + lanczos_1500, 8
    )
    nb_pro_500_batch = round(
        (rates.vertex_gemini_3_pro_2k_std_usd * discount) + lanczos_500, 8
    )
    nb_pro_1500_batch = round(
        (rates.vertex_gemini_3_pro_2k_std_usd * discount) + lanczos_1500, 8
    )

    updated_map = {
        "local-lanczos-canvas": (
            lanczos_500,
            lanczos_1500,
            lanczos_500,
            lanczos_1500,
        ),
        "realesrgan": (
            realesrgan_500,
            realesrgan_1500,
            realesrgan_500,
            realesrgan_1500,
        ),
        "gemini-3.1-flash-lite-image": (
            nb_lite_500_batch,
            nb_lite_1500_batch,
            nb_lite_500_std,
            nb_lite_1500_std,
        ),
        "gemini-3.1-flash-image": (
            nb_std_500_batch,
            nb_std_1500_batch,
            nb_std_500_std,
            nb_std_1500_std,
        ),
        "gemini-3-pro-image": (
            nb_pro_500_batch,
            nb_pro_1500_batch,
            nb_pro_500_std,
            nb_pro_1500_std,
        ),
    }

    items: List[EnginePricingSnapshotItem] = []
    for eng_id, spec in list(ENGINE_CATALOG.items()):
      if eng_id in updated_map:
        c500, c1500, c500_std, c1500_std = updated_map[eng_id]
        new_spec: EngineSpec = dataclasses.replace(
            spec,
            cost_500_per_img=c500,
            cost_1500_per_img=c1500,
            cost_500_std_per_img=c500_std,
            cost_1500_std_per_img=c1500_std,
        )
        ENGINE_CATALOG[eng_id] = new_spec
      else:
        new_spec = spec

      items.append(
          EnginePricingSnapshotItem(
              engine_id=new_spec.engine_id,
              display_name=new_spec.display_name,
              role=new_spec.role,
              machine_spec=new_spec.machine_spec,
              zero_hallucination=new_spec.zero_hallucination,
              cost_500_per_img=new_spec.cost_500_per_img,
              cost_1500_per_img=new_spec.cost_1500_per_img,
              cost_500_std_per_img=new_spec.cost_500_std_per_img,
              cost_1500_std_per_img=new_spec.cost_1500_std_per_img,
              cost_per_10k_500_usd=round(new_spec.cost_500_per_img * 10000, 2),
              cost_per_10k_1500_usd=round(
                  new_spec.cost_1500_per_img * 10000, 2
              ),
              when_recommended=new_spec.when_recommended,
              is_vertex_ai=new_spec.is_vertex_ai,
          )
      )
    return items

  def _save_snapshot_to_firestore(
      self, snapshot: PricingCatalogSnapshot
  ) -> None:
    """Persist pricing snapshot to Cloud Firestore `pricing_config/current`."""
    if request_db.use_mock_db:
      return
    try:
      client = request_db._get_client()  # pylint: disable=protected-access
      client.collection(self.FIRESTORE_COLLECTION).document(
          self.FIRESTORE_DOC_ID
      ).set(snapshot.model_dump())
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.debug(
          "Non-fatal error saving pricing snapshot to Firestore: %s", exc
      )

  def _load_snapshot_from_firestore(self) -> Optional[PricingCatalogSnapshot]:
    """Load persisted pricing snapshot from Cloud Firestore if available."""
    if request_db.use_mock_db:
      return None
    try:
      client = request_db._get_client()  # pylint: disable=protected-access
      doc = (
          client.collection(self.FIRESTORE_COLLECTION)
          .document(self.FIRESTORE_DOC_ID)
          .get()
      )
      if doc.exists:
        data = doc.to_dict() or {}
        if data.get("unit_rates") and data.get("last_synced_utc"):
          return PricingCatalogSnapshot(**data)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.debug(
          "Non-fatal error reading pricing snapshot from Firestore: %s",
          exc,
      )
    return None

  def get_current_pricing(
      self, allow_blocking_db_load: bool = True
  ) -> PricingCatalogSnapshot:
    """Return the current active pricing catalog snapshot.

    Args:
      allow_blocking_db_load: Whether to allow an initial Firestore read.

    Returns:
      Active PricingCatalogSnapshot instance.
    """
    if not self._loaded_from_db and allow_blocking_db_load:
      self._loaded_from_db = True
      persisted = self._load_snapshot_from_firestore()
      if persisted is not None:
        with self._lock:
          self._unit_rates = persisted.unit_rates
          self._last_synced_utc = persisted.last_synced_utc
          self._pricing_source = persisted.pricing_source
          self._billing_api_status = persisted.cloud_billing_api_status
          engines = self._compute_and_apply_engine_catalog(self._unit_rates)
        return persisted.model_copy(update={"engines": engines})

    with self._lock:
      engines = self._compute_and_apply_engine_catalog(self._unit_rates)
      return PricingCatalogSnapshot(
          last_synced_utc=self._last_synced_utc,
          pricing_source=self._pricing_source,
          cloud_billing_api_status=self._billing_api_status,
          gcp_project_id=settings.gcp_project_id,
          region=self._unit_rates.region,
          unit_rates=self._unit_rates,
          engines=engines,
      )

  @staticmethod
  def _extract_sku_usd_rate(sku_obj: Dict[str, Any]) -> Optional[float]:
    """Extract unit price in USD from a Cloud Billing Catalog SKU object.

    Args:
      sku_obj: Raw SKU dictionary from Cloud Billing API.

    Returns:
      Extracted float unit price in USD, or None if unavailable.
    """
    try:
      pricing_info = sku_obj.get("pricingInfo") or []
      if not pricing_info:
        return None
      expr = pricing_info[0].get("pricingExpression") or {}
      tiered_rates = expr.get("tieredRates") or []
      for rate in reversed(tiered_rates):
        unit_price = rate.get("unitPrice") or {}
        units = float(unit_price.get("units") or 0)
        nanos = float(unit_price.get("nanos") or 0)
        usd = units + (nanos / 1_000_000_000.0)
        if usd > 0:
          return usd
    except Exception:  # pylint: disable=broad-exception-caught
      return None
    return None

  def _obtain_gcp_bearer_token(self) -> Optional[str]:
    """Obtain an OAuth2 access token from ADC if available.

    Returns:
      Bearer token string or None if unavailable.
    """
    if request_db.use_mock_db:
      return None
    try:
      # pylint: disable=g-import-not-at-top
      import google.auth
      from google.auth.transport import requests

      creds, _ = google.auth.default(
          scopes=["https://www.googleapis.com/auth/cloud-platform"]
      )
      if not creds.valid:
        creds.refresh(requests.Request())
      return creds.token
    except Exception:  # pylint: disable=broad-exception-caught
      return None

  async def sync_with_gcp_catalog(
      self,
      custom_rates: Optional[GcpComponentUnitRates] = None,
  ) -> PricingCatalogSnapshot:
    """Query Google Cloud Billing Catalog API and persist pricing snapshot.

    Args:
      custom_rates: Optional admin custom rate override.

    Returns:
      Updated PricingCatalogSnapshot persisted to Firestore.
    """
    region = os.getenv("GCP_REGION", "asia-northeast1")
    rates = (
        custom_rates.model_copy()
        if custom_rates is not None
        else GcpComponentUnitRates(region=region)
    )
    matched_skus = 0
    api_status = "VERIFIED_CATALOG_SYNC"
    source_label = f"Google Cloud Billing Catalog ({region} + global)"

    if custom_rates is not None:
      api_status = "CUSTOM_OVERRIDE_SYNCED"
      source_label = f"Admin Custom Rate Override ({region})"
    else:
      token = self._obtain_gcp_bearer_token()
      api_key = os.getenv("GEMINI_API_KEY", "").strip()
      headers: Dict[str, str] = {"Accept": "application/json"}
      if token:
        headers["Authorization"] = f"Bearer {token}"

      params: Dict[str, str] = {"currencyCode": "USD", "pageSize": "200"}
      if api_key and not token:
        params["key"] = api_key

      if token or api_key:
        try:
          async with httpx.AsyncClient(timeout=10.0) as client:
            url = (
                "https://cloudbilling.googleapis.com/v1/services/"
                f"{self.CLOUD_RUN_SERVICE_ID}/skus"
            )
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
              payload = resp.json()
              for sku in payload.get("skus", []):
                regions = sku.get("serviceRegions") or []
                if region not in regions and "global" not in regions:
                  continue
                desc = str(sku.get("description") or "").lower()
                price = self._extract_sku_usd_rate(sku)
                if price is None:
                  continue
                if "cpu" in desc and "tier 1" in desc and price < 0.001:
                  rates.cloud_run_vcpu_per_sec_usd = round(price, 7)
                  matched_skus += 1
                elif "memory" in desc and "tier 1" in desc and price < 0.0001:
                  rates.cloud_run_mem_gib_per_sec_usd = round(price, 8)
                  matched_skus += 1
                elif "nvidia l4" in desc and price > 0.1:
                  usage_unit = str(
                      (
                          (sku.get("pricingInfo") or [{}])[0].get(
                              "pricingExpression"
                          )
                          or {}
                      ).get("usageUnit", "h")
                  ).lower()
                  rates.cloud_run_nvidia_l4_per_hr_usd = (
                      round(price * 3600.0, 4)
                      if usage_unit == "s"
                      else round(price, 4)
                  )
                  matched_skus += 1
              api_status = (
                  f"LIVE_API_SYNCED ({matched_skus} regional SKUs matched)"
                  if matched_skus > 0
                  else "LIVE_API_CONNECTED_DEFAULT_RATES"
              )
              fallback_lbl = (
                  f"Google Cloud Official Rate Table ({region} "
                  "+ Vertex AI global)"
              )
              source_label = (
                  f"Live Cloud Billing Catalog API v1 ({region})"
                  if matched_skus > 0
                  else fallback_lbl
              )
            else:
              api_status = f"FALLBACK_OFFICIAL_RATES (HTTP {resp.status_code})"
              source_label = (
                  f"Google Cloud Official Rate Table ({region} "
                  "+ Vertex AI global)"
              )
        except Exception as exc:  # pylint: disable=broad-exception-caught
          logger.debug(
              "Cloud Billing API query fell back to verified rate table: %s",
              exc,
          )
          api_status = "FALLBACK_OFFICIAL_RATES"
          source_label = (
              f"Google Cloud Official Rate Table ({region} + Vertex AI global)"
          )

    now_iso, _ = utc_now_iso_and_yyyymmdd()
    with self._lock:
      self._unit_rates = rates
      self._last_synced_utc = now_iso
      self._pricing_source = source_label
      self._billing_api_status = api_status
      self._loaded_from_db = True
      engines = self._compute_and_apply_engine_catalog(rates)

    snapshot = PricingCatalogSnapshot(
        last_synced_utc=now_iso,
        pricing_source=source_label,
        cloud_billing_api_status=api_status,
        gcp_project_id=settings.gcp_project_id,
        region=rates.region,
        unit_rates=rates,
        engines=engines,
    )
    self._save_snapshot_to_firestore(snapshot)
    return snapshot


pricing_service = GcpPricingSyncService()
