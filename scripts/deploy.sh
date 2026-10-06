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
# ImageUpscaler — 1-Command Pure `gcloud` Deployment (`scripts/deploy.sh`)
# ============================================================================
# Requires ONLY `gcloud` CLI (no local `terraform` or `docker` needed!).
#
# Spawns and wires the entire system on GCP:
# 1. Required GCP APIs (Cloud Run, Firestore, Vertex AI, Cloud Storage, etc.)
# 2. Least-Privilege Service Account (`gmc-image-upscaler-sa`) & IAM bindings
# 3. Google Secret Manager secret storing your deployment `ACCESS_CODE`
# 4. Managed Cloud Database: Cloud Firestore (Native Mode) (`gmc-upscaler-db`)
# 5. Public GCS Bucket (`gs://<bucket>`) + `status/{YYYYMMDD}/log.csv`
# 6. Remote Container Build in Cloud Build (`gcloud builds submit`)
# 7. Scalable Backend Cloud Run Service (`gmc-upscaler-backend`)
# 8. Frontend UI Cloud Run Service (`gmc-upscaler-frontend`) with `BACKEND_URL`
# ============================================================================

set -euo pipefail

DEFAULT_PROJ="$(gcloud config get-value project 2>/dev/null || true)"
PROJECT_ID="${GCP_PROJECT_ID:-${DEFAULT_PROJ}}"
REGION="${REGION:-asia-northeast1}"
ACCESS_CODE="${ACCESS_CODE:-}"
FIRESTORE_DB="${FIRESTORE_DATABASE:-gmc-upscaler-db}"
PUBLIC_BUCKET="${PUBLIC_GCS_BUCKET:-${PROJECT_ID}-gmc-upscaler}"
BACKEND_SERVICE="${BACKEND_SERVICE:-gmc-upscaler-backend}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-gmc-upscaler-frontend}"
AR_REPO="${AR_REPO:-image-upscaler-repo}"
SA_NAME="${SA_NAME:-gmc-image-upscaler-sa}"
SECRET_NAME="gmc-upscaler-access-code"
COMPUTE_MODE="${COMPUTE_MODE:-cpu}" # "cpu" or "gpu"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: GCP_PROJECT_ID is not set. Run:" >&2
  echo "  GCP_PROJECT_ID=my-proj ACCESS_CODE=secret bash scripts/deploy.sh" >&2
  exit 1
fi

if [[ -z "${ACCESS_CODE}" ]]; then
  echo "ERROR: ACCESS_CODE is not set. Set the deployment access code:" >&2
  echo "  GCP_PROJECT_ID=${PROJECT_ID} ACCESS_CODE=secret \\" >&2
  echo "    bash scripts/deploy.sh" >&2
  exit 1
fi

echo "==> [1/8] Enabling required GCP APIs in project: ${PROJECT_ID}"
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudbilling.googleapis.com \
  iam.googleapis.com \
  --project="${PROJECT_ID}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo "==> [2/8] Provisioning Service Account & IAM: ${SA_EMAIL}"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="GMC ImageUpscaler Pipeline Service Account" \
    --project="${PROJECT_ID}"
fi

for ROLE in \
  "roles/datastore.user" \
  "roles/storage.objectAdmin" \
  "roles/aiplatform.user" \
  "roles/secretmanager.secretAccessor" \
  "roles/logging.logWriter"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet >/dev/null
done

retry_cmd() {
  local max_attempts=4
  local attempt=1
  while true; do
    if "$@"; then
      return 0
    fi
    if (( attempt >= max_attempts )); then
      return 1
    fi
    echo "    Transient API delay (${attempt}/${max_attempts}), retrying..."
    sleep 8
    (( attempt++ ))
  done
}

echo "==> [3/8] Storing Access Code in Secret Manager: ${SECRET_NAME}"
if ! gcloud secrets describe "${SECRET_NAME}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  retry_cmd gcloud secrets create "${SECRET_NAME}" \
    --replication-policy="automatic" \
    --project="${PROJECT_ID}"
fi
printf "%s" "${ACCESS_CODE}" | retry_cmd gcloud secrets versions add \
  "${SECRET_NAME}" \
  --data-file=- \
  --project="${PROJECT_ID}" >/dev/null

echo "==> [4/8] Provisioning Cloud Firestore DB: ${FIRESTORE_DB}"
if ! gcloud firestore databases describe \
  --database="${FIRESTORE_DB}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  retry_cmd gcloud firestore databases create \
    --database="${FIRESTORE_DB}" \
    --location="${REGION}" \
    --type=firestore-native \
    --project="${PROJECT_ID}"
fi

TODAY_YYYYMMDD="$(date -u +%Y%m%d)"
LOG_PATH="gs://${PUBLIC_BUCKET}/status/${TODAY_YYYYMMDD}/log.csv"
echo "==> [5/8] Provisioning GCS Bucket (gs://${PUBLIC_BUCKET}) & daily log"
if ! gcloud storage buckets describe "gs://${PUBLIC_BUCKET}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${PUBLIC_BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
fi

if ! gcloud storage buckets add-iam-policy-binding "gs://${PUBLIC_BUCKET}" \
  --member="allUsers" \
  --role="roles/storage.objectViewer" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "    NOTE: Org Policy prevented allUsers on gs://${PUBLIC_BUCKET}."
fi

if ! gcloud storage ls "${LOG_PATH}" --project="${PROJECT_ID}" >/dev/null 2>&1;
then
  TMP_LOG="$(mktemp)"
  CSV_HDR="timestamp_utc,date_yyyymmdd,request_id,sku_id,source_image_url"
  CSV_HDR="${CSV_HDR},converted_gcs_url,engine_id,target_resolution"
  CSV_HDR="${CSV_HDR},original_dimensions,output_dimensions,status"
  CSV_HDR="${CSV_HDR},processing_ms"
  echo "${CSV_HDR}" > "${TMP_LOG}"
  gcloud storage cp "${TMP_LOG}" "${LOG_PATH}" \
    --project="${PROJECT_ID}" >/dev/null
  rm -f "${TMP_LOG}"
fi

echo "==> [6/8] Building & pushing container image via Cloud Build"
if ! gcloud artifacts repositories describe "${AR_REPO}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1; then
  retry_cmd gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="GMC ImageUpscaler container repository" \
    --project="${PROJECT_ID}"
fi

AR_HOST="${REGION}-docker.pkg.dev"
IMAGE_URI="${AR_HOST}/${PROJECT_ID}/${AR_REPO}/gmc-image-upscaler:latest"
gcloud builds submit \
  --tag="${IMAGE_URI}" \
  --project="${PROJECT_ID}" \
  .

VERTEX_CONCURRENCY="${VERTEX_MAX_CONCURRENCY:-4}"
VERTEX_QPM="${VERTEX_MAX_QPM:-60}"

BE_ENV="SERVICE_ROLE=backend,GCP_PROJECT_ID=${PROJECT_ID}"
BE_ENV="${BE_ENV},FIRESTORE_DATABASE=${FIRESTORE_DB},VERTEX_LOCATION=global"
BE_ENV="${BE_ENV},VERTEX_MAX_CONCURRENCY=${VERTEX_CONCURRENCY}"
BE_ENV="${BE_ENV},VERTEX_MAX_QPM=${VERTEX_QPM}"
BE_ENV="${BE_ENV},PUBLIC_GCS_BUCKET=${PUBLIC_BUCKET}"
BE_ENV="${BE_ENV},USE_MOCK_GCS=false,USE_MOCK_DB=false"

echo "==> [7/8] Deploying Backend Cloud Run Service: ${BACKEND_SERVICE}"
if [[ "${COMPUTE_MODE}" == "gpu" ]]; then
  gcloud run deploy "${BACKEND_SERVICE}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --service-account="${SA_EMAIL}" \
    --allow-unauthenticated \
    --gpu=1 \
    --gpu-type=nvidia-l4 \
    --cpu=4 \
    --memory=16Gi \
    --timeout=3600 \
    --execution-environment=gen2 \
    --no-cpu-throttling \
    --min-instances=0 \
    --max-instances=10 \
    --set-env-vars="${BE_ENV}" \
    --set-secrets="ACCESS_CODE=${SECRET_NAME}:latest" \
    --project="${PROJECT_ID}"
else
  gcloud run deploy "${BACKEND_SERVICE}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --service-account="${SA_EMAIL}" \
    --allow-unauthenticated \
    --cpu=4 \
    --memory=8Gi \
    --timeout=3600 \
    --execution-environment=gen2 \
    --no-cpu-throttling \
    --min-instances=0 \
    --max-instances=25 \
    --set-env-vars="${BE_ENV}" \
    --set-secrets="ACCESS_CODE=${SECRET_NAME}:latest" \
    --project="${PROJECT_ID}"
fi

BACKEND_URL="$(gcloud run services describe "${BACKEND_SERVICE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')"
echo "    Backend URL: ${BACKEND_URL}"

FE_ENV="SERVICE_ROLE=frontend,BACKEND_URL=${BACKEND_URL}"
FE_ENV="${FE_ENV},GCP_PROJECT_ID=${PROJECT_ID}"
FE_ENV="${FE_ENV},FIRESTORE_DATABASE=${FIRESTORE_DB}"
FE_ENV="${FE_ENV},PUBLIC_GCS_BUCKET=${PUBLIC_BUCKET}"
FE_ENV="${FE_ENV},USE_MOCK_GCS=false,USE_MOCK_DB=false"

echo "==> [8/8] Deploying Frontend UI Service: ${FRONTEND_SERVICE}"
gcloud run deploy "${FRONTEND_SERVICE}" \
  --image="${IMAGE_URI}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --cpu=1 \
  --memory=2Gi \
  --execution-environment=gen2 \
  --min-instances=0 \
  --max-instances=15 \
  --set-env-vars="${FE_ENV}" \
  --set-secrets="ACCESS_CODE=${SECRET_NAME}:latest" \
  --project="${PROJECT_ID}"

FRONTEND_URL="$(gcloud run services describe "${FRONTEND_SERVICE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')"

echo "========================================================================="
echo "ALL-AT-ONCE GCP DEPLOYMENT COMPLETE!"
echo "-------------------------------------------------------------------------"
echo "Frontend UI URL       : ${FRONTEND_URL}"
echo "Backend Engine URL    : ${BACKEND_URL}"
echo "Cloud Firestore DB    : ${FIRESTORE_DB}"
echo "Public GCS Images URL : https://storage.googleapis.com/${PUBLIC_BUCKET}/"
echo "Daily Status Log Path : ${LOG_PATH}"
echo "========================================================================="
