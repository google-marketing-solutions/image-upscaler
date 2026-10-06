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

"""Pydantic schemas for GMC feeds, request tracking, and pipeline reports."""

from __future__ import annotations

import enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator


class TargetResolution(int, enum.Enum):
  """Selectable target resolution patterns per GMC Data Specification."""

  COMPLIANCE_500 = 500
  RECOMMENDED_1500 = 1500


class EngineId(str, enum.Enum):
  """Selectable upscaling engines supported by the pipeline."""

  LOCAL_LANCZOS_CANVAS = "local-lanczos-canvas"
  REALESRGAN_GPU = "realesrgan"
  NANO_BANANA_2_LITE = "gemini-3.1-flash-lite-image"
  NANO_BANANA_2_STD = "gemini-3.1-flash-image"
  NANO_BANANA_PRO = "gemini-3-pro-image"


class FeedInputRow(BaseModel):
  """Normalized input row from a Google Sheet, CSV, or TSV feed."""

  id: str = Field(
      ...,
      min_length=1,
      max_length=256,
      description="GMC Product ID (offer_id)",
  )
  image_link: str = Field(
      ...,
      min_length=1,
      max_length=2048,
      description="Primary product image URL",
  )
  additional_image_links: List[str] = Field(
      default_factory=list,
      description="Zero or more additional product image URLs",
  )

  @field_validator("id")
  @classmethod
  def strip_id(cls, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
      raise ValueError("Product 'id' must not be empty")
    return cleaned


class SupplementalFeedRow(BaseModel):
  """GMC Supplemental Feed row with id, input_url, and image links."""

  id: str
  input_url: str = ""
  image_link: str
  additional_image_link: str = ""


class ConversionLogEntry(BaseModel):
  """Single row appended daily to the GCS status log CSV."""

  timestamp_utc: str
  date_yyyymmdd: str
  request_id: str
  sku_id: str
  source_image_url: str
  converted_gcs_url: str
  engine_id: str
  target_resolution: int
  original_dimensions: str
  output_dimensions: str
  status: str
  processing_ms: float
  error_message: str = ""


class UpscaleRequestRecord(BaseModel):
  """Persistent job record stored in DB and displayed in the Requests table."""

  request_id: str
  created_at_utc: str
  updated_at_utc: str
  date_yyyymmdd: str
  filename: str
  engine_id: str
  target_resolution: int
  status: Literal["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]
  total_skus: int
  total_images: int
  upscaled_images: int = 0
  skipped_images: int = 0
  failed_images: int = 0
  estimated_cost_usd: float = 0.0
  duration_ms: float = 0.0
  input_gcs_uri: str = ""
  output_gcs_uri: str = ""
  daily_log_gcs_uri: str = ""
  error_message: Optional[str] = None


class UpscaleRequestDetailResponse(BaseModel):
  """Detailed view of a single request and its per-SKU items."""

  request: UpscaleRequestRecord
  items: List[ConversionLogEntry] = Field(default_factory=list)


class ImageAuditRecord(BaseModel):
  """Dimension and compliance audit record for a single deduplicated URL."""

  url: str
  width: int
  height: int
  format: str
  meets_500x500: bool
  meets_1500x1500: bool
  recommended_engine: str
  status: Literal[
      "compliant_1500", "compliant_500_only", "non_compliant_sub_500", "error"
  ]
  error_message: Optional[str] = None


class EngineCostEstimate(BaseModel):
  """Projected cost for an engine across 500x500 and 1500x1500 target modes."""

  engine_id: str
  display_name: str
  role: str
  machine_spec: str
  zero_hallucination: bool
  images_needing_500: int
  images_needing_1500: int
  cost_500_batch_usd: float
  cost_1500_batch_usd: float
  cost_500_std_usd: float
  cost_1500_std_usd: float
  cost_per_10k_500_usd: float
  cost_per_10k_1500_usd: float
  when_recommended: str


class CatalogAuditReport(BaseModel):
  """Stage 1 Dimension Auditor summary report."""

  total_skus: int
  total_image_references: int
  unique_image_urls: int
  deduplication_savings_pct: float
  sub_500x500_urls: int
  sub_500x500_exposure_pct: float
  between_500_and_1499_urls: int
  compliant_1500x1500_urls: int
  failed_urls: int
  cost_matrix: List[EngineCostEstimate]
  sample_records: List[ImageAuditRecord]


class ProcessedImageMetrics(BaseModel):
  """Metrics for a single processed image URL."""

  source_url: str
  public_gcs_url: str
  local_preview_url: Optional[str] = None
  original_width: int
  original_height: int
  output_width: int
  output_height: int
  engine_used: str
  skipped_already_compliant: bool
  processing_ms: float
  c2pa_synthid_preserved: bool = False


class UpscalePipelineReport(BaseModel):
  """Execution summary returned by the Parallel Upscaling Orchestrator."""

  job_id: str
  target_resolution: int
  engine_id: str
  shard_index: int
  shard_count: int
  total_skus_processed: int
  unique_urls_evaluated: int
  urls_upscaled: int
  urls_skipped_already_compliant: int
  urls_failed: int
  estimated_cost_usd: float
  total_duration_ms: float
  public_gcs_bucket: str
  daily_log_gcs_uri: str = ""
  supplemental_feed_rows: List[SupplementalFeedRow]
  processed_images: Dict[str, ProcessedImageMetrics]
  failed_errors: Dict[str, str] = Field(default_factory=dict)
