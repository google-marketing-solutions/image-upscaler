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

output "frontend_ui_url" {
  description = "Frontend Web UI URL (unlock with your deployment Access Code)"
  value       = google_cloud_run_v2_service.upscaler_frontend.uri
}

output "backend_api_url" {
  description = "Scalable Backend Engine URL (wired to Frontend via BFF proxy)"
  value       = google_cloud_run_v2_service.upscaler_backend.uri
}

output "firestore_database_name" {
  description = "Managed Google Cloud Firestore (Native Mode) database name"
  value       = google_firestore_database.upscaler_db.name
}

output "gcs_bucket_name" {
  description = "GCS Bucket hosting generated images, CSVs, and daily logs"
  value       = google_storage_bucket.public_upscaled_bucket.name
}

output "public_gcs_image_url_prefix" {
  description = "Public HTTPS URL prefix where generated images are hosted"
  value = format(
    "https://storage.googleapis.com/%s/upscaled/",
    google_storage_bucket.public_upscaled_bucket.name
  )
}

output "daily_status_log_gcs_path_template" {
  description = "Daily cumulative conversion log path in GCS"
  value = format(
    "gs://%s/status/{YYYYMMDD}/log.csv",
    google_storage_bucket.public_upscaled_bucket.name
  )
}

output "cloud_run_job_name" {
  description = "Name of the sharded Cloud Run batch job"
  value       = google_cloud_run_v2_job.upscaler_batch_job.name
}
