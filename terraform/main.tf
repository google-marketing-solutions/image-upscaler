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
#
# ============================================================================
# ImageUpscaler — All-at-Once GCP Infrastructure (Terraform IaC)
# ============================================================================
# Spawns:
# 1. Required GCP APIs & Least-Privilege IAM Service Account
# 2. Secret Manager storing the deployment Access Code (`var.access_code`)
# 3. Cloud Firestore (Native Mode) Database (`upscaler_db`)
# 4. Artifact Registry + Automated Cloud Build container image build
# 5. Public GCS Bucket (`gs://<bucket>`) + initial `status/{YYYYMMDD}/log.csv`
# 6. Scalable Backend Cloud Run Service (`gmc-upscaler-backend`)
# 7. Frontend UI Cloud Run Service (`gmc-upscaler-frontend`) with BFF proxy
# ============================================================================

locals {
  required_apis = [
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "firestore.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudbilling.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ]

  bucket_name = (
    var.public_gcs_bucket_name != ""
    ? var.public_gcs_bucket_name
    : "${var.project_id}-gmc-upscaler"
  )

  ar_repo_id = google_artifact_registry_repository.upscaler_repo.repository_id
  default_image_uri = format(
    "%s-docker.pkg.dev/%s/%s/gmc-image-upscaler:latest",
    var.region,
    var.project_id,
    local.ar_repo_id
  )

  built_image_uri = (
    var.container_image != ""
    ? var.container_image
    : local.default_image_uri
  )

  deploy_date_yyyymmdd = formatdate("YYYYMMDD", timestamp())

  sa_member = "serviceAccount:${google_service_account.upscaler_sa.email}"
  secret_id = google_secret_manager_secret.access_code_secret.secret_id

  log_csv_header = join(",", [
    "timestamp_utc",
    "date_yyyymmdd",
    "request_id",
    "sku_id",
    "source_image_url",
    "converted_gcs_url",
    "engine_id",
    "target_resolution",
    "original_dimensions",
    "output_dimensions",
    "status",
    "processing_ms\n",
  ])
}

# 1. Enable Required GCP APIs
resource "google_project_service" "enabled_apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 2. Least-Privilege Pipeline Service Account & IAM Bindings
resource "google_service_account" "upscaler_sa" {
  account_id   = "gmc-image-upscaler-sa"
  display_name = "GMC ImageUpscaler Pipeline Service Account"
  description  = "Executes Frontend, Backend, Firestore, Vertex AI, and GCS"
  depends_on   = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = local.sa_member
}

resource "google_project_iam_member" "firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = local.sa_member
}

resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = local.sa_member
}

# 3. Managed Cloud Database: Google Cloud Firestore (Native Mode)
resource "google_firestore_database" "upscaler_db" {
  project                     = var.project_id
  name                        = var.firestore_database_id
  location_id                 = var.region
  type                        = "FIRESTORE_NATIVE"
  concurrency_mode            = "OPTIMISTIC"
  app_engine_integration_mode = "DISABLED"
  delete_protection_state     = "DELETE_PROTECTION_DISABLED"
  deletion_policy             = "DELETE"

  depends_on = [google_project_service.enabled_apis]
}

# 4. Secret Manager for Deployment Access Code (`var.access_code`)
resource "google_secret_manager_secret" "access_code_secret" {
  secret_id = "gmc-upscaler-access-code"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_version" "access_code_version" {
  secret      = google_secret_manager_secret.access_code_secret.id
  secret_data = var.access_code
}

resource "google_secret_manager_secret_iam_member" "sa_secret_accessor" {
  secret_id = google_secret_manager_secret.access_code_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.sa_member
}

# 5. Artifact Registry Docker Repository + Automated Cloud Build
resource "google_artifact_registry_repository" "upscaler_repo" {
  location      = var.region
  repository_id = "image-upscaler-repo"
  description   = "Container repository for GMC ImageUpscaler"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled_apis]
}

resource "terraform_data" "build_and_push_image" {
  count = var.auto_build_container ? 1 : 0

  triggers_replace = [
    filesha256("${path.module}/../Dockerfile"),
    filesha256("${path.module}/../requirements.txt"),
    filesha256("${path.module}/../app/main.py"),
    filesha256("${path.module}/../app/db.py"),
    filesha256("${path.module}/../app/pipeline/orchestrator.py"),
    filesha256("${path.module}/../app/static/index.html"),
    filesha256("${path.module}/../app/static/app.js"),
  ]

  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    command     = <<-EOT
      gcloud builds submit \
        --project="${var.project_id}" \
        --tag="${local.default_image_uri}" \
        .
    EOT
  }

  depends_on = [google_artifact_registry_repository.upscaler_repo]
}

# 6. Publicly Accessible GCS Bucket + Initial `status/{YYYYMMDD}/log.csv`
resource "google_storage_bucket" "public_upscaled_bucket" {
  name                        = local.bucket_name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = true

  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD", "OPTIONS"]
    response_header = ["Content-Type", "Cache-Control", "Origin"]
    max_age_seconds = 3600
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_storage_bucket_iam_member" "sa_object_admin" {
  bucket = google_storage_bucket.public_upscaled_bucket.name
  role   = "roles/storage.objectAdmin"
  member = local.sa_member
}

resource "google_storage_bucket_iam_member" "public_object_viewer" {
  bucket = google_storage_bucket.public_upscaled_bucket.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_storage_bucket_object" "initial_daily_status_log" {
  name         = "status/${local.deploy_date_yyyymmdd}/log.csv"
  bucket       = google_storage_bucket.public_upscaled_bucket.name
  content_type = "text/csv; charset=utf-8"
  content      = local.log_csv_header

  lifecycle {
    ignore_changes = [content, name]
  }
}

# 7. Scalable Backend Cloud Run Service (`gmc-upscaler-backend`)
resource "google_cloud_run_v2_service" "upscaler_backend" {
  provider = google-beta
  name     = "gmc-upscaler-backend"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account       = google_service_account.upscaler_sa.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
    timeout               = "3600s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.enable_l4_gpu ? 10 : 25
    }

    dynamic "node_selector" {
      for_each = var.enable_l4_gpu ? [1] : []
      content {
        accelerator = "nvidia-l4"
      }
    }

    containers {
      image = local.built_image_uri

      resources {
        cpu_idle = false
        limits = {
          cpu              = "4"
          memory           = var.enable_l4_gpu ? "16Gi" : "8Gi"
          "nvidia.com/gpu" = var.enable_l4_gpu ? "1" : null
        }
      }

      env {
        name  = "SERVICE_ROLE"
        value = "backend"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.upscaler_db.name
      }
      env {
        name  = "VERTEX_LOCATION"
        value = "global"
      }
      env {
        name  = "VERTEX_MAX_CONCURRENCY"
        value = "4"
      }
      env {
        name  = "VERTEX_MAX_QPM"
        value = "60"
      }
      env {
        name  = "PUBLIC_GCS_BUCKET"
        value = google_storage_bucket.public_upscaled_bucket.name
      }
      env {
        name  = "USE_MOCK_GCS"
        value = "false"
      }
      env {
        name  = "USE_MOCK_DB"
        value = "false"
      }
      env {
        name = "ACCESS_CODE"
        value_source {
          secret_key_ref {
            secret  = local.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    terraform_data.build_and_push_image,
    google_firestore_database.upscaler_db,
    google_project_iam_member.firestore_user,
    google_storage_bucket_iam_member.sa_object_admin,
    google_secret_manager_secret_iam_member.sa_secret_accessor,
    google_secret_manager_secret_version.access_code_version,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "backend_public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.upscaler_backend.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# 8. Frontend UI Cloud Run Service (`gmc-upscaler-frontend`)
resource "google_cloud_run_v2_service" "upscaler_frontend" {
  provider = google-beta
  name     = "gmc-upscaler-frontend"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account       = google_service_account.upscaler_sa.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      min_instance_count = 0
      max_instance_count = 15
    }

    containers {
      image = local.built_image_uri

      resources {
        cpu_idle = true
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
      }

      env {
        name  = "SERVICE_ROLE"
        value = "frontend"
      }
      env {
        name  = "BACKEND_URL"
        value = google_cloud_run_v2_service.upscaler_backend.uri
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.upscaler_db.name
      }
      env {
        name  = "PUBLIC_GCS_BUCKET"
        value = google_storage_bucket.public_upscaled_bucket.name
      }
      env {
        name  = "USE_MOCK_GCS"
        value = "false"
      }
      env {
        name  = "USE_MOCK_DB"
        value = "false"
      }
      env {
        name = "ACCESS_CODE"
        value_source {
          secret_key_ref {
            secret  = local.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_cloud_run_v2_service.upscaler_backend,
    google_cloud_run_v2_service_iam_member.backend_public_invoker,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "frontend_public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.upscaler_frontend.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# 9. Sharded Cloud Run v2 Batch Job for Massive Catalog Feeds
resource "google_cloud_run_v2_job" "upscaler_batch_job" {
  provider = google-beta
  name     = "gmc-image-upscaler-job"
  location = var.region

  template {
    parallelism = var.job_task_shards
    task_count  = var.job_task_shards

    template {
      service_account       = google_service_account.upscaler_sa.email
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
      timeout               = "3600s"

      containers {
        image   = local.built_image_uri
        command = ["python", "-m", "app.cli", "upscale"]
        args = [
          "--resolution",
          tostring(var.default_target_resolution),
          "--engine",
          var.default_engine_id,
          "--bucket",
          google_storage_bucket.public_upscaled_bucket.name,
        ]

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "FIRESTORE_DATABASE"
          value = google_firestore_database.upscaler_db.name
        }
        env {
          name  = "VERTEX_LOCATION"
          value = "global"
        }
        env {
          name  = "PUBLIC_GCS_BUCKET"
          value = google_storage_bucket.public_upscaled_bucket.name
        }
        env {
          name  = "USE_MOCK_GCS"
          value = "false"
        }
        env {
          name  = "USE_MOCK_DB"
          value = "false"
        }
      }
    }
  }

  depends_on = [
    terraform_data.build_and_push_image,
    google_firestore_database.upscaler_db,
    google_storage_bucket_iam_member.sa_object_admin,
  ]
}
