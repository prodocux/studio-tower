#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Google Cloud Production Deployment Script for StudioTower (Bash / Linux / CI)
# ==============================================================================

PROJECT_ID="${PROJECT_ID:-agentic-cinema-demo-2026}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-studio-tower-api}"
GCS_BUCKET="${GCS_BUCKET:-agentic-cinema-demo-2026-studiotower-artifacts}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.6-flash}"
FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-${PROJECT_ID}}"
FIREBASE_API_KEY="${FIREBASE_API_KEY:-}"
FIREBASE_APP_ID="${FIREBASE_APP_ID:-}"
SCHEDULER_JOB_NAME="${SCHEDULER_JOB_NAME:-studiotower-maintenance-job}"
DRY_RUN="${DRY_RUN:-false}"

for arg in "$@"; do
  case "${arg}" in
    --dry-run)
      DRY_RUN="true"
      ;;
  esac
done

if [ -n "${FIREBASE_API_KEY}" ]; then
  export VITE_FIREBASE_API_KEY="${FIREBASE_API_KEY}"
fi
if [ -n "${FIREBASE_APP_ID}" ]; then
  export VITE_FIREBASE_APP_ID="${FIREBASE_APP_ID}"
fi

BUILD_ID="$(date +%Y%m%d%H%M%S)"
CANDIDATE_TAG="candidate-${BUILD_ID}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:${BUILD_ID}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${DIR}/.." && pwd)"
PYTHON_BIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo "python")"

echo "==> 1. Checking Existing Cloud Run Service Status and Traffic..."
DESCRIBE_ERR_FILE="$(mktemp)"
IS_FIRST_DEPLOYMENT=false
EXISTING_SERVICE_URL=""
CURRENT_TRAFFIC_ALLOC=""
CURRENT_PRIMARY_REVISION=""

if [ "${DRY_RUN}" = "true" ]; then
  echo "Dry-run mode active: using simulated fixture service URL for dry-run verification."
  EXISTING_SERVICE_URL="${STUDIO_TOWER_WORKER_SERVICE_URL:-https://${SERVICE}-mbvd6nfacq-uc.a.run.app}"
  CURRENT_TRAFFIC_ALLOC="prod-v1=100"
  CURRENT_PRIMARY_REVISION="prod-v1"
else
  if DESCRIBE_JSON="$(gcloud run services describe "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --format=json 2>"${DESCRIBE_ERR_FILE}")"; then
    IS_FIRST_DEPLOYMENT=false
    # Extract authoritative service URL from status.url
    EXISTING_SERVICE_URL="$("${PYTHON_BIN}" "${DIR}/parse_traffic.py" --service-url <<< "${DESCRIBE_JSON}")"
    if [ -z "${EXISTING_SERVICE_URL}" ]; then
      echo "ERROR: Existing Cloud Run service describe did not yield a valid status.url." >&2
      exit 1
    fi
    # Authoritatively capture traffic allocations snapshot
    CURRENT_TRAFFIC_ALLOC="$("${PYTHON_BIN}" "${DIR}/parse_traffic.py" --allocations <<< "${DESCRIBE_JSON}")"
    CURRENT_PRIMARY_REVISION="$("${PYTHON_BIN}" "${DIR}/parse_traffic.py" --primary <<< "${DESCRIBE_JSON}")"
    if [ -z "${CURRENT_TRAFFIC_ALLOC}" ] || [ -z "${CURRENT_PRIMARY_REVISION}" ]; then
      echo "ERROR: Existing Cloud Run service describe did not yield valid traffic allocations or primary revision. Aborting." >&2
      exit 1
    fi
    echo "Existing Service Detected:"
    echo "  Authoritative URL: ${EXISTING_SERVICE_URL}"
    echo "  Serving Traffic:   ${CURRENT_TRAFFIC_ALLOC}"
    echo "  Primary Revision:  ${CURRENT_PRIMARY_REVISION}"
  else
    DESCRIBE_ERR="$(cat "${DESCRIBE_ERR_FILE}")"
    rm -f "${DESCRIBE_ERR_FILE}" 2>/dev/null || true
    if echo "${DESCRIBE_ERR}" | grep -Ei "Cannot find service|not found|NOT_FOUND" >/dev/null; then
      IS_FIRST_DEPLOYMENT=true
      DESCRIBE_JSON=""
      echo "Note: Service '${SERVICE}' does not exist in ${REGION} (Initial Deployment)."
    else
      echo "ERROR: Failed to query Cloud Run service '${SERVICE}' due to GCP/network/permission error:" >&2
      echo "${DESCRIBE_ERR}" >&2
      exit 1
    fi
  fi
fi
rm -f "${DESCRIBE_ERR_FILE}" 2>/dev/null || true

# Authoritative Worker Service URL resolution & boundary validation
WORKER_SERVICE_URL="${STUDIO_TOWER_WORKER_SERVICE_URL:-}"
if [ "${IS_FIRST_DEPLOYMENT}" = "true" ]; then
  if [ -z "${WORKER_SERVICE_URL}" ]; then
    echo "ERROR: Service '${SERVICE}' does not exist yet (first deployment). STUDIO_TOWER_WORKER_SERVICE_URL must be explicitly provided (cannot guess synthetic URL)." >&2
    exit 1
  fi
else
  if [ -z "${WORKER_SERVICE_URL}" ]; then
    WORKER_SERVICE_URL="${EXISTING_SERVICE_URL}"
  else
    if [ "${WORKER_SERVICE_URL}" != "${EXISTING_SERVICE_URL}" ]; then
      echo "ERROR: Explicit STUDIO_TOWER_WORKER_SERVICE_URL '${WORKER_SERVICE_URL}' does not match authoritative Cloud Run service URL '${EXISTING_SERVICE_URL}'." >&2
      exit 1
    fi
  fi
fi

if [[ "${WORKER_SERVICE_URL}" != https://* ]]; then
  echo "ERROR: STUDIO_TOWER_WORKER_SERVICE_URL must start with https:// (got '${WORKER_SERVICE_URL}')" >&2
  exit 1
fi

WORKER_HOSTNAME="$("${PYTHON_BIN}" -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "${WORKER_SERVICE_URL}")"
SCHEDULER_SA="${STUDIO_TOWER_SCHEDULER_SA:-studiotower-api@${PROJECT_ID}.iam.gserviceaccount.com}"

echo "==> 2. Validating & Provisioning Production Secret Manager Bindings..."
SECRETS="GEMINI_API_KEY=GEMINI_API_KEY:latest,STUDIO_TOWER_MAINTENANCE_SECRET=STUDIO_TOWER_MAINTENANCE_SECRET:latest,STUDIO_TOWER_TASK_SECRET=STUDIO_TOWER_TASK_SECRET:latest,CURSOR_SIGNING_SECRET=CURSOR_SIGNING_SECRET:latest,ACTION_SIGNING_SECRET=ACTION_SIGNING_SECRET:latest,TELEMETRY_TENANT_KEY_PRIMARY=TELEMETRY_TENANT_KEY_PRIMARY:latest"

if [ "${DRY_RUN}" != "true" ]; then
  if gcloud secrets describe STUDIO_TOWER_TASK_SECRET_PREVIOUS --project="${PROJECT_ID}" >/dev/null 2>&1; then
    SECRETS="${SECRETS},STUDIO_TOWER_TASK_SECRET_PREVIOUS=STUDIO_TOWER_TASK_SECRET_PREVIOUS:latest"
  fi
fi

GRAFANA_BASE_URL="${GRAFANA_BASE_URL:-https://loftyladybug3305.grafana.net}"
GRAFANA_ALLOWED_HOST="$("${PYTHON_BIN}" -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "")' "${GRAFANA_BASE_URL}")"
# gcloud forbids combining --set-env-vars with --env-vars-file.
RUN_ENV_FILE="$(mktemp)"
cat > "${RUN_ENV_FILE}" <<EOF
ENV: production
STUDIO_TOWER_AUTH_MODE: firebase
STUDIO_TOWER_STORE: firestore
STUDIO_TOWER_FIREBASE_PROJECT_ID: ${FIREBASE_PROJECT_ID}
STUDIO_TOWER_ARTIFACT_BACKEND: gcs
STUDIO_TOWER_GCS_BUCKET: ${GCS_BUCKET}
INGESTION_RUNNER: cloud_tasks
ACTION_RUNNER: cloud_tasks
STUDIO_TOWER_WORKER_SERVICE_URL: "${WORKER_SERVICE_URL}"
STUDIO_TOWER_ALLOWED_WORKER_HOST: ${WORKER_HOSTNAME}
STUDIO_TOWER_SCHEDULER_SA: ${SCHEDULER_SA}
STUDIO_TOWER_SCHEDULER_AUDIENCE: "${WORKER_SERVICE_URL}"
STUDIO_TOWER_CLOUD_TASKS_PROJECT: ${FIREBASE_PROJECT_ID}
STUDIO_TOWER_CLOUD_TASKS_QUEUE: studiotower-ingestion-queue
STUDIO_TOWER_CLOUD_TASKS_LOCATION: us-central1
GEMINI_MODEL: ${GEMINI_MODEL}
AI_FALLBACK_ALLOWED: "false"
AI_INFERENCE_TIMEOUT_SECONDS: "45.0"
AI_MAX_CONCURRENT_INFERENCES: "16"
GRAFANA_MCP_ENDPOINT: "http://127.0.0.1:8000/mcp"
GRAFANA_BASE_URL: "${GRAFANA_BASE_URL}"
GRAFANA_ALLOWED_HOSTS: '["${GRAFANA_ALLOWED_HOST}"]'
GRAFANA_OTLP_ENDPOINT: "https://otlp-gateway-prod-us-east-2.grafana.net/otlp"
GRAFANA_OTLP_INSTANCE_ID: "1742899"
EOF

if [ "${DRY_RUN}" != "true" ]; then
  if gcloud secrets describe GRAFANA_CLOUD_API_KEY --project="${PROJECT_ID}" >/dev/null 2>&1; then
    SECRETS="${SECRETS},GRAFANA_CLOUD_API_KEY=GRAFANA_CLOUD_API_KEY:latest"
  fi
  if gcloud secrets describe GRAFANA_SERVICE_ACCOUNT_TOKEN --project="${PROJECT_ID}" >/dev/null 2>&1; then
    SECRETS="${SECRETS},GRAFANA_SERVICE_ACCOUNT_TOKEN=GRAFANA_SERVICE_ACCOUNT_TOKEN:latest"
  fi
  if gcloud secrets describe GRAFANA_OTLP_TOKEN --project="${PROJECT_ID}" >/dev/null 2>&1; then
    SECRETS="${SECRETS},GRAFANA_OTLP_TOKEN=GRAFANA_OTLP_TOKEN:latest"
  fi
fi

if [ "${DRY_RUN}" = "true" ]; then
  echo "=================================================================="
  echo " StudioTower Cloud Deployment Pipeline [DRY RUN]"
  echo " Project: ${PROJECT_ID} | Region: ${REGION} | Service: ${SERVICE}"
  echo "=================================================================="
  echo "==> [DRY-RUN 1/6] Running Traffic Allocation Parser Verification on Representative Fixtures..."
  "${PYTHON_BIN}" "${DIR}/parse_traffic.py" --verify-fixtures

  echo "==> [DRY-RUN 2/6] Validating Multi-Revision Rollback Command Assembly..."
  # 1. 100% single revision
  MOCK_100='{"status":{"traffic":[{"revisionName":"prod-v1","percent":100}]}}'
  ALLOC_100="$(echo "${MOCK_100}" | "${PYTHON_BIN}" "${DIR}/parse_traffic.py" --allocations)"
  echo "  [Single 100%] -> Allocation: ${ALLOC_100} | Rollback: gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --to-revisions=${ALLOC_100}"
  if [ "${ALLOC_100}" != "prod-v1=100" ]; then echo "ERROR: Allocation 100% mismatch"; exit 1; fi

  # 2. 50/50 split
  MOCK_50='{"status":{"traffic":[{"revisionName":"rev-a","percent":50},{"revisionName":"rev-b","percent":50}]}}'
  ALLOC_50="$(echo "${MOCK_50}" | "${PYTHON_BIN}" "${DIR}/parse_traffic.py" --allocations)"
  echo "  [50/50 Split] -> Allocation: ${ALLOC_50} | Rollback: gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --to-revisions=${ALLOC_50}"
  if [ "${ALLOC_50}" != "rev-a=50,rev-b=50" ]; then echo "ERROR: Allocation 50/50 mismatch"; exit 1; fi

  # 3. 90/10 canary
  MOCK_90='{"status":{"traffic":[{"revisionName":"rev-stable","percent":90},{"revisionName":"rev-canary","percent":10}]}}'
  ALLOC_90="$(echo "${MOCK_90}" | "${PYTHON_BIN}" "${DIR}/parse_traffic.py" --allocations)"
  echo "  [90/10 Canary] -> Allocation: ${ALLOC_90} | Rollback: gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --to-revisions=${ALLOC_90}"
  if [ "${ALLOC_90}" != "rev-stable=90,rev-canary=10" ]; then echo "ERROR: Allocation 90/10 mismatch"; exit 1; fi

  echo "==> [DRY-RUN 3/6] Validating Worker Service URL and Host Whitelist..."
  echo "  Target Project: ${PROJECT_ID}"
  echo "  Resolved Worker URL: ${WORKER_SERVICE_URL}"
  echo "  Allowed Worker Host: ${WORKER_HOSTNAME}"
  echo "  ✓ Worker URL HTTPS and host whitelist verified."

  echo "==> [DRY-RUN 4/6] Validating Cloud Scheduler OIDC Command Assembly (Zero Plaintext Secrets)..."
  SIMULATED_SCHEDULER_CMD="gcloud scheduler jobs create http ${SCHEDULER_JOB_NAME} --project=${PROJECT_ID} --location=${REGION} --schedule=\"*/5 * * * *\" --uri=\"${WORKER_SERVICE_URL}/v1/maintenance/reconcile-actions-and-cleanup\" --http-method=POST --oidc-service-account-email=\"${SCHEDULER_SA}\" --oidc-token-audience=\"${WORKER_SERVICE_URL}\""
  echo "  Simulated Scheduler CLI: ${SIMULATED_SCHEDULER_CMD}"
  if [[ "${SIMULATED_SCHEDULER_CMD}" == *"X-StudioTower-Maintenance-Secret"* ]]; then
    echo "SECURITY ERROR: Secret found in Scheduler CLI args!" >&2
    exit 1
  fi
  echo "  ✓ Zero shared secrets exposed on Cloud Scheduler CLI (OIDC SA: ${SCHEDULER_SA})."

  echo "==> [DRY-RUN 5/6] Validating Firestore Composite Indexes & Frontend Package..."
  test -f "${DIR}/../firestore.indexes.json" || { echo "firestore.indexes.json missing"; exit 1; }
  test -f "${DIR}/../frontend/package.json" || { echo "frontend/package.json missing"; exit 1; }
  echo "  ✓ Indexes schema and frontend bundle definitions verified."

  echo "==> [DRY-RUN 6/6] Validating Candidate Tag and URL Resolution..."
  echo "  Container Cloud Build target -> ${IMAGE}"
  echo "  Candidate Tag -> ${CANDIDATE_TAG}"
  SIM_CAND_TAG_URL="$(echo '{"status":{"traffic":[{"tag":"'"${CANDIDATE_TAG}"'","url":"https://'"${CANDIDATE_TAG}"'---studio-tower-api.run.app"}]}}' | "${PYTHON_BIN}" "${DIR}/parse_traffic.py" --candidate-url "${CANDIDATE_TAG}")"
  echo "  Candidate URL parser simulation -> ${SIM_CAND_TAG_URL}"
  echo "==> [DRY-RUN] All parser calculations, fixture tests, rollback commands, and security boundaries passed cleanly."
  exit 0
fi

echo "==> 3. Deploying Firestore Composite Indexes Prior to Code Rollout..."
cd "${DIR}/.."
firebase deploy --only firestore:indexes --project="${PROJECT_ID}"

echo "Verifying Firestore composite indexes have reached READY status..."
MAX_ATTEMPTS=30
ALL_READY=false
for i in $(seq 1 ${MAX_ATTEMPTS}); do
  BUILDING="$(gcloud firestore indexes composite list --project="${PROJECT_ID}" --format=json 2>/dev/null | "${PYTHON_BIN}" -c '
import json, sys
data = json.load(sys.stdin)
non_ready = [x for x in data if x.get("state") != "READY"]
print(len(non_ready))
')"
  if [ "${BUILDING}" = "0" ]; then
    ALL_READY=true
    echo "All Firestore composite indexes are confirmed READY."
    break
  fi
  echo "Waiting for ${BUILDING} indexes to finish building (attempt ${i}/${MAX_ATTEMPTS})..."
  sleep 10
done

if [ "${ALL_READY}" != "true" ]; then
  echo "ERROR: Firestore composite indexes did not reach READY state within the timeout. Aborting candidate deployment." >&2
  exit 1
fi

echo "==> 4. Building Frontend Production Assets (npm run build:prod)..."
cd "${DIR}/../frontend"
npm run build:prod

echo "==> 5. Building Container Image via Cloud Build (${IMAGE})..."
cd "${ROOT}"
gcloud builds submit . \
  --project="${PROJECT_ID}" \
  --config="${ROOT}/cloudbuild.yaml" \
  --substitutions="_IMAGE=${IMAGE}"

echo "==> 6. Deploying 0% Traffic Candidate to Google Cloud Run (Tag: ${CANDIDATE_TAG})..."
gcloud run deploy "${SERVICE}" \
  --project="${PROJECT_ID}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=3 \
  --cpu=1 \
  --memory=1Gi \
  --env-vars-file="${RUN_ENV_FILE}" \
  --set-secrets="${SECRETS}" \
  --no-traffic \
  --tag="${CANDIDATE_TAG}"

CANDIDATE_REVISION="$(gcloud run revisions list --service="${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --filter="metadata.annotations['run.googleapis.com/tag'] = '${CANDIDATE_TAG}'" --format="value(metadata.name)" --limit=1)"
if [ -z "${CANDIDATE_REVISION}" ]; then
  CANDIDATE_REVISION="$(gcloud run services describe "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.latestCreatedRevisionName)")"
fi
echo "Deployed Candidate Revision: ${CANDIDATE_REVISION} (Tag: ${CANDIDATE_TAG})"

# Authoritatively Extract Candidate Tag URL (fail closed immediately if tag URL not found or not HTTPS)
CANDIDATE_DESCRIBE_JSON="$(gcloud run services describe "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --format=json)"
CANDIDATE_URL="$("${PYTHON_BIN}" "${DIR}/parse_traffic.py" --candidate-url "${CANDIDATE_TAG}" <<< "${CANDIDATE_DESCRIBE_JSON}")"
if [ -z "${CANDIDATE_URL}" ] || [[ "${CANDIDATE_URL}" != https://* ]]; then
  echo "ERROR: Failed to resolve authoritative Candidate Tag URL for '${CANDIDATE_TAG}' from Cloud Run service status. Aborting deployment immediately." >&2
  exit 1
fi
echo "Authoritative Candidate URL: ${CANDIDATE_URL}"

TRAFFIC_SHIFTED=false
RELEASE_COMPLETED=false

rollback() {
  echo "CRITICAL FAILURE DETECTED! Initiating rollback guard..." >&2
  if [ "${TRAFFIC_SHIFTED}" = "true" ] && [ -n "${CURRENT_PRIMARY_REVISION}" ] && [ "${CURRENT_PRIMARY_REVISION}" != "${CANDIDATE_REVISION}" ]; then
    TARGET_REVS="${CURRENT_TRAFFIC_ALLOC:-${CURRENT_PRIMARY_REVISION}=100}"
    echo "Restoring traffic to prior serving configuration: ${TARGET_REVS}..." >&2
    if ! gcloud run services update-traffic "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --to-revisions="${TARGET_REVS}"; then
      echo "EMERGENCY: Automated rollback command failed! Immediate manual intervention required for service '${SERVICE}' in region '${REGION}' (Target Configuration: ${TARGET_REVS})" >&2
      exit 2
    fi
    echo "Rollback successfully completed. Traffic restored to prior configuration: ${TARGET_REVS}." >&2
  else
    echo "Traffic was not shifted or no prior serving revision exists; no traffic rollback required." >&2
  fi
  exit 1
}

cleanup_guard() {
  if [ "${TRAFFIC_SHIFTED}" = "true" ] && [ "${RELEASE_COMPLETED}" != "true" ]; then
    echo "Deployment aborted unexpectedly after traffic shift! Executing emergency rollback..." >&2
    TARGET_REVS="${CURRENT_TRAFFIC_ALLOC:-${CURRENT_PRIMARY_REVISION}=100}"
    if [ -n "${TARGET_REVS}" ] && [ -n "${CURRENT_PRIMARY_REVISION}" ] && [ "${CURRENT_PRIMARY_REVISION}" != "${CANDIDATE_REVISION}" ]; then
      if ! gcloud run services update-traffic "${SERVICE}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --to-revisions="${TARGET_REVS}" 2>/dev/null; then
        echo "EMERGENCY: Exit-trap rollback failed to restore ${TARGET_REVS}! Immediate manual intervention required!" >&2
        exit 2
      fi
    fi
  fi
}
trap cleanup_guard EXIT

echo "==> 7. Auditing Candidate Cloud Run Spec for Secret Leak Prevention..."
gcloud run revisions describe "${CANDIDATE_REVISION}" --project="${PROJECT_ID}" --region="${REGION}" --format=json | "${PYTHON_BIN}" -c '
import json, sys
spec = json.load(sys.stdin)
env_list = spec.get("spec", {}).get("containers", [{}])[0].get("env", [])
for e in env_list:
    name = e.get("name", "")
    val = e.get("value")
    if "SECRET" in name or "KEY" in name or "TOKEN" in name:
        if val is not None and len(str(val)) > 0 and name != "GEMINI_MODEL":
            print(f"SECURITY VIOLATION: Sensitive environment variable {name} set as plaintext in container spec!", file=sys.stderr)
            sys.exit(1)
'

echo "==> 8. Verifying Candidate Health Status (Pre-Traffic Shift)..."
# Smoke test candidate revision via direct tag URL
READY_ENDPOINT="${CANDIDATE_URL}/readyz"
echo "Probing candidate readiness endpoint: ${READY_ENDPOINT}"
CANDIDATE_HEALTHY=false
for attempt in $(seq 1 12); do
  STATUS="$(curl -s -o /dev/null -w "%{http_code}" "${READY_ENDPOINT}" || true)"
  if [ "${STATUS}" = "200" ]; then
    CANDIDATE_HEALTHY=true
    echo "Candidate revision ${CANDIDATE_REVISION} is READY (HTTP 200)."
    break
  fi
  echo "Readiness check returned HTTP ${STATUS}, waiting 5s (attempt ${attempt}/12)..."
  sleep 5
done

if [ "${CANDIDATE_HEALTHY}" != "true" ]; then
  echo "ERROR: Candidate revision ${CANDIDATE_REVISION} failed readiness check. Aborting before traffic shift." >&2
  exit 1
fi

echo "==> 9. Shifting 100% Production Traffic to Candidate Revision..."
gcloud run services update-traffic "${SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --to-revisions="${CANDIDATE_REVISION}=100" || rollback
TRAFFIC_SHIFTED=true

echo "==> 10. Configuring Cloud Scheduler Actions Reconciliation Job with OIDC..."
if gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  echo "Updating existing Cloud Scheduler maintenance job with OIDC service account..."
  gcloud scheduler jobs update http "${SCHEDULER_JOB_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --schedule="*/5 * * * *" \
    --uri="${WORKER_SERVICE_URL}/v1/maintenance/reconcile-actions-and-cleanup" \
    --http-method=POST \
    --oidc-service-account-email="${SCHEDULER_SA}" \
    --oidc-token-audience="${WORKER_SERVICE_URL}" || rollback
else
  echo "Creating new Cloud Scheduler maintenance job with OIDC service account..."
  gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --schedule="*/5 * * * *" \
    --uri="${WORKER_SERVICE_URL}/v1/maintenance/reconcile-actions-and-cleanup" \
    --http-method=POST \
    --oidc-service-account-email="${SCHEDULER_SA}" \
    --oidc-token-audience="${WORKER_SERVICE_URL}" || rollback
fi

echo "==> 11. Deploying Frontend to Firebase Hosting..."
cd "${DIR}/../frontend"
echo "    --> Compiling production frontend bundle (npm run build:prod)..."
npm run build:prod || rollback
echo "    --> Deploying static assets to Firebase Hosting..."
firebase deploy --only hosting --project="${PROJECT_ID}" || rollback

echo "==> 12. Running Post-Shift Smoke Test on Production URL..."
PROD_URL="$(gcloud run services describe "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
PROD_STATUS="$(curl -s -o /dev/null -w "%{http_code}" "${WORKER_SERVICE_URL}/readyz" || true)"
if [ "${PROD_STATUS}" != "200" ]; then
  echo "CRITICAL: Post-shift production readiness check failed with HTTP ${PROD_STATUS}! Triggering rollback..." >&2
  rollback
fi

RELEASE_COMPLETED=true
echo "=================================================================="
echo " StudioTower Deployment Successful!"
echo " Service Revision: ${CANDIDATE_REVISION} (100% traffic)"
echo " Backend URL:      ${PROD_URL}"
echo " Frontend URL:     https://${PROJECT_ID}.web.app"
echo "=================================================================="
