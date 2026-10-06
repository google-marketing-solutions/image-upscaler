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

"""Configuration, Engine Cost Matrix constants, and secure secret resolution."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import pathlib
import secrets
from typing import Dict

logger = logging.getLogger(__name__)


def get_app_secret(
    env_var: str = "IMAGE_UPSCALER_SECRET_KEY",
    secret_file: str = "csrf_secret.txt",
) -> str:
  """Multi-tiered secret resolution per secure coding guidelines.

  Resolution order:
  1. Environment variable (KMS / Secret Manager injected in Cloud Run)
  2. Local file query (if mounted secret file exists inside workspace)
  3. Deterministic derivation from ACCESS_CODE if available across Cloud Run
     instances
  4. Cryptographically random ephemeral secret (for local offline testing)

  Args:
    env_var: Environment variable name to inspect first.
    secret_file: Relative filename inside the workspace to check second.

  Returns:
    The resolved secret string.
  """
  env_val = os.getenv(env_var)
  if env_val and env_val.strip():
    return env_val.strip()

  secret_path = pathlib.Path(secret_file).resolve()
  workspace_root = pathlib.Path.cwd().resolve()
  if (
      str(secret_path).startswith(str(workspace_root) + os.sep)
      and secret_path.is_file()
  ):
    content = secret_path.read_text(encoding="utf-8").strip()
    if content:
      return content

  access_code_env = os.getenv("ACCESS_CODE", "").strip()
  if access_code_env and env_var != "ACCESS_CODE":
    return hashlib.sha256(
        f"gmc-upscaler-key:{env_var}:{access_code_env}".encode("utf-8")
    ).hexdigest()

  logger.warning(
      "Generating ephemeral secret for %s. Instance-isolated! Configure "
      "Secret Manager in production.",
      env_var,
  )
  return secrets.token_hex(16)


@dataclasses.dataclass(frozen=True)
class EngineSpec:
  """Specification and pricing for an upscaling engine in the Cost Matrix."""

  engine_id: str
  display_name: str
  role: str
  machine_spec: str
  cost_500_per_img: float
  cost_1500_per_img: float
  cost_500_std_per_img: float
  cost_1500_std_per_img: float
  zero_hallucination: bool
  is_vertex_ai: bool
  key_characteristics: str
  when_recommended: str


ENGINE_CATALOG: Dict[str, EngineSpec] = {
    "local-lanczos-canvas": EngineSpec(
        engine_id="local-lanczos-canvas",
        display_name="Local Smart Lanczos + White Canvas (Default CPU)",
        role="Default (CPU)",
        machine_spec="Cloud Run 1 vCPU / 2 GiB RAM",
        cost_500_per_img=0.00000775,
        cost_1500_per_img=0.00001645,
        cost_500_std_per_img=0.00000775,
        cost_1500_std_per_img=0.00001645,
        zero_hallucination=True,
        is_vertex_ai=False,
        key_characteristics=(
            "0% text/logo hallucination. Scales product to full target edge "
            "+ minimal white padding. "
            "Basis: CPU ($0.000029/s x 25ms/85ms) + Req ($0.0000004) + GCS "
            "PUT ($0.000005) + 1mo GCS Storage."
        ),
        when_recommended=(
            "[Default: 70-80%+ of Catalog] All SKUs >=300x300 px, and ANY "
            "item with Japanese packaging text, "
            "model numbers, or brand logos (cosmetics, food, electronics, "
            "industrial parts)."
        ),
    ),
    "realesrgan": EngineSpec(
        engine_id="realesrgan",
        display_name=(
            "Local Smart Lanczos / Self-Hosted Real-ESRGAN (Cloud Run L4 GPU)"
        ),
        role="Default (L4 GPU)",
        machine_spec=(
            "Cloud Run 1x NVIDIA L4 GPU (24 GB VRAM) / 4-8 vCPU / 16-32 GiB "
            "RAM (gen2)"
        ),
        cost_500_per_img=0.00006470,
        cost_1500_per_img=0.00015880,
        cost_500_std_per_img=0.00006470,
        cost_1500_std_per_img=0.00015880,
        zero_hallucination=True,
        is_vertex_ai=False,
        key_characteristics=(
            "0% text/logo hallucination. Basis: Cloud Run L4 GPU ($0.672/hr) "
            "+ 4vCPU/16GiB ($0.000136/s) "
            "x 15-45ms (Lanczos) or 180-450ms (Real-ESRGAN) + GCS PUT & "
            "Storage."
        ),
        when_recommended=(
            "[Optional High-Throughput GPU Mode] Sub-300px SKUs needing "
            "non-generative edge sharpening "
            "at 100x lower cost than generative APIs."
        ),
    ),
    "gemini-3.1-flash-lite-image": EngineSpec(
        engine_id="gemini-3.1-flash-lite-image",
        display_name=(
            "Vertex AI Nano Banana 2 Lite (gemini-3.1-flash-lite-image)"
        ),
        role="Selectable (Lowest-Cost AI)",
        machine_spec=(
            "Cloud Run 1-2 vCPU / 2-4 GiB RAM + Vertex AI (location=global)"
        ),
        cost_500_per_img=0.01680775,
        cost_1500_per_img=0.01681645,
        cost_500_std_per_img=0.03360775,
        cost_1500_std_per_img=0.03361645,
        zero_hallucination=False,
        is_vertex_ai=True,
        key_characteristics=(
            "Lowest-cost Vertex AI model. Native output fixed at 1K "
            "(1024x1024, $0.0168 Batch); "
            "500px (downscale) and 1500px (1.46x Lanczos) cost the same. "
            "Reconstructs fabric texture."
        ),
        when_recommended=(
            "[Recommended Nano Banana Entry Tier] Extreme low-res (<=250x250 "
            "px) text-free products "
            "(unbranded apparel, shoes, bags, furniture, fresh food return "
            "gifts)."
        ),
    ),
    "gemini-3.1-flash-image": EngineSpec(
        engine_id="gemini-3.1-flash-image",
        display_name=(
            "Vertex AI Nano Banana 2 Standard (gemini-3.1-flash-image)"
        ),
        role="Selectable (Native 2K AI)",
        machine_spec=(
            "Cloud Run 1-2 vCPU / 2-4 GiB RAM + Vertex AI (location=global)"
        ),
        cost_500_per_img=0.02250775,
        cost_1500_per_img=0.04501645,
        cost_500_std_per_img=0.04500775,
        cost_1500_std_per_img=0.09001645,
        zero_hallucination=False,
        is_vertex_ai=True,
        key_characteristics=(
            "Supports native 512x512 (747 tok) for 500x500 mode and native "
            "2K=2048x2048 (1,500 tok) "
            "for 1500x1500 mode (2x cost for 1500x1500). Superior detail "
            "sharpness."
        ),
        when_recommended=(
            "[Recommended for 1500x1500 Visual SKUs] High-margin apparel, "
            "leather goods, and home decor "
            "where 1500x1500px zoom clarity drives conversion."
        ),
    ),
    "gemini-3-pro-image": EngineSpec(
        engine_id="gemini-3-pro-image",
        display_name="Vertex AI Nano Banana Pro (gemini-3-pro-image)",
        role="Selectable (Premium Pro AI)",
        machine_spec=(
            "Cloud Run 1-2 vCPU / 2-4 GiB RAM + Vertex AI (location=global)"
        ),
        cost_500_per_img=0.06700775,
        cost_1500_per_img=0.06701645,
        cost_500_std_per_img=0.13400775,
        cost_1500_std_per_img=0.13401645,
        zero_hallucination=False,
        is_vertex_ai=True,
        key_characteristics=(
            "Both 1K and 2K (2048x2048) consume 1,120 tokens ($0.067 Batch), "
            "so 1500x1500 costs "
            "the same as 500x500. Best reasoning and studio typography "
            "fidelity."
        ),
        when_recommended=(
            "[Recommended for Top 1% Hero SKUs] Flagship campaign products "
            "(tens to hundreds of SKUs) "
            "requiring maximum generative fidelity."
        ),
    ),
}


@dataclasses.dataclass
class AppSettings:
  """Runtime settings loaded from environment variables."""

  service_role: str = os.getenv(
      "SERVICE_ROLE", "unified"
  )  # "frontend", "backend", or "unified"
  backend_url: str = os.getenv("BACKEND_URL", "").rstrip("/")
  gcp_project_id: str = os.getenv(
      "GCP_PROJECT_ID", "client-gmc-upscaler-project"
  )
  firestore_database: str = os.getenv("FIRESTORE_DATABASE", "gmc-upscaler-db")
  vertex_location: str = os.getenv("VERTEX_LOCATION", "global")
  public_gcs_bucket: str = os.getenv(
      "PUBLIC_GCS_BUCKET", "client-gmc-upscaled-images-public"
  )
  gcs_object_prefix: str = os.getenv("GCS_OBJECT_PREFIX", "upscaled")
  local_storage_dir: pathlib.Path = pathlib.Path(
      os.getenv("LOCAL_STORAGE_DIR", "./local_gcs_bucket")
  ).resolve()
  use_mock_gcs: bool = os.getenv("USE_MOCK_GCS", "true").lower() in (
      "1",
      "true",
      "yes",
  )
  use_mock_db: bool = os.getenv(
      "USE_MOCK_DB", os.getenv("USE_MOCK_GCS", "true")
  ).lower() in ("1", "true", "yes")
  allow_vertex_fallback_to_lanczos: bool = os.getenv(
      "ALLOW_VERTEX_FALLBACK_TO_LANCZOS", "true"
  ).lower() in (
      "1",
      "true",
      "yes",
  )
  async_io_concurrency: int = int(os.getenv("ASYNC_IO_CONCURRENCY", "128"))
  cpu_worker_threads: int = int(os.getenv("CPU_WORKER_THREADS", "16"))
  gcs_upload_threads: int = int(os.getenv("GCS_UPLOAD_THREADS", "64"))
  vertex_max_concurrency: int = int(os.getenv("VERTEX_MAX_CONCURRENCY", "4"))
  vertex_max_qpm: int = int(os.getenv("VERTEX_MAX_QPM", "60"))
  vertex_max_retries: int = int(os.getenv("VERTEX_MAX_RETRIES", "4"))
  enable_gcs_image_cache: bool = os.getenv(
      "ENABLE_GCS_IMAGE_CACHE", "true"
  ).lower() in ("1", "true", "yes")
  max_upload_bytes: int = int(
      os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))
  )
  max_image_download_bytes: int = int(
      os.getenv("MAX_IMAGE_DOWNLOAD_BYTES", str(20 * 1024 * 1024))
  )
  cloud_run_task_index: int = int(os.getenv("CLOUD_RUN_TASK_INDEX", "0"))
  cloud_run_task_count: int = int(os.getenv("CLOUD_RUN_TASK_COUNT", "1"))
  allowed_cors_origins: str = os.getenv(
      "ALLOWED_CORS_ORIGINS", "http://127.0.0.1:8080,http://localhost:8080"
  )


settings = AppSettings()
DEPLOYMENT_ACCESS_CODE = get_app_secret("ACCESS_CODE", "access_code.txt")
CSRF_SECRET_KEY = get_app_secret("IMAGE_UPSCALER_SECRET_KEY", "csrf_secret.txt")
