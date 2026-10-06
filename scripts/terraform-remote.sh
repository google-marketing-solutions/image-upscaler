#!/usr/bin/env bash
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
# Run Terraform Remotely on GCP via Cloud Build (`scripts/terraform-remote.sh`)
# ============================================================================
# Avoids requiring a local `terraform` binary installation by
# executing `terraform init` and `terraform apply` inside Google Cloud Build!
# ============================================================================

set -euo pipefail

DEFAULT_PROJ="$(gcloud config get-value project 2>/dev/null || true)"
PROJECT_ID="${GCP_PROJECT_ID:-${DEFAULT_PROJ}}"
REGION="${REGION:-asia-northeast1}"
ACCESS_CODE="${ACCESS_CODE:-}"
TF_STATE_BUCKET="${TF_STATE_BUCKET:-${PROJECT_ID}-upscaler-tfstate}"

if [[ -z "${PROJECT_ID}" || -z "${ACCESS_CODE}" ]]; then
  echo "Usage:" >&2
  echo "  GCP_PROJECT_ID=my-proj ACCESS_CODE=secret \\" >&2
  echo "    bash scripts/terraform-remote.sh" >&2
  exit 1
fi

echo "==> [1/3] Enabling Cloud Build, Artifact Registry, Storage, and IAM..."
gcloud services enable \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com \
  run.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project="${PROJECT_ID}"

echo "==> [2/3] Ensuring Terraform State Bucket: gs://${TF_STATE_BUCKET}"
if ! gcloud storage buckets describe "gs://${TF_STATE_BUCKET}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${TF_STATE_BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
fi

if ! gcloud artifacts repositories describe "image-upscaler-repo" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "image-upscaler-repo" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}"
fi

SUBS="_REGION=${REGION},_ACCESS_CODE=${ACCESS_CODE}"
SUBS="${SUBS},_TF_STATE_BUCKET=${TF_STATE_BUCKET}"

echo "==> [3/3] Submitting Terraform Apply to Google Cloud Build..."
gcloud builds submit \
  --config=cloudbuild-terraform.yaml \
  --substitutions="${SUBS}" \
  --project="${PROJECT_ID}" \
  .
