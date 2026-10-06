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

variable "project_id" {
  description = "GCP Project ID where ImageUpscaler will be deployed"
  type        = string
}

variable "region" {
  description = "GCP Region for Cloud Run, Firestore, AR, and GCS Bucket"
  type        = string
  default     = "asia-northeast1"
}

variable "access_code" {
  description = "Access code set at deployment to unlock the Frontend UI"
  type        = string
  sensitive   = true
}

variable "firestore_database_id" {
  description = "Dedicated Cloud Firestore Native database name"
  type        = string
  default     = "gmc-upscaler-db"
}

variable "public_gcs_bucket_name" {
  description = "Optional custom GCS bucket name (<project_id>-gmc-upscaler)"
  type        = string
  default     = ""
}

variable "auto_build_container" {
  description = "When true, runs Cloud Build to build & push the image"
  type        = bool
  default     = true
}

variable "container_image" {
  description = "Optional pre-built container image URI"
  type        = string
  default     = ""
}

variable "enable_l4_gpu" {
  description = "When true, provisions Backend with 1x NVIDIA L4 GPU"
  type        = bool
  default     = false
}

variable "default_target_resolution" {
  description = "Default GMC target resolution mode (500 or 1500)"
  type        = number
  default     = 500
}

variable "default_engine_id" {
  description = "Default upscaling engine ID"
  type        = string
  default     = "local-lanczos-canvas"
}

variable "job_task_shards" {
  description = "Number of horizontal Cloud Run Job tasks (--tasks=N)"
  type        = number
  default     = 10
}
