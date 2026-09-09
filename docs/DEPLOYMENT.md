# StudioTower Production Deployment Guide

This document outlines the deployment architecture, configuration pipelines, and verification procedures for **StudioTower** on Google Cloud Platform and Firebase Hosting.

---

## Infrastructure Architecture

```text
[ Browser Client ]
        │
        ▼ (HTTPS)
[ Firebase Hosting CDN ] (https://agentic-cinema-demo-2026.web.app)
        │
        ▼ (/v1/** rewrite → Cloud Run)
[ Google Cloud Run ] (studio-tower-api)
        URL: https://studio-tower-api-mbvd6nfacq-uc.a.run.app
        ├── Regional auto-scaling (us-central1, min 0, max 3)
        ├── Identity Token validation via Firebase Admin SDK
        ├── Secret Manager injected credentials
        ├── docker-start.sh: grafana/mcp-grafana on 127.0.0.1:8000
        │                     + uvicorn on $PORT (8080)
        │
        ├── Google Cloud Firestore (Multi-tenant space, user, message, run records)
        ├── Google Cloud Storage (Immutable artifacts, 5 deliverable formats)
        ├── Google Cloud Tasks (Background ingestion & action runner queues)
        │
        └── External Integrations:
              ├── Google Gemini 3.6 API via google-genai (film-prep reasoning)
              └── Grafana Cloud stack https://loftyladybug3305.grafana.net
                  ├── HTTP API + MCP reads (`glsa_` service account)
                  └── OTLP writes (`glc_` access policy) → Prometheus + Tempo
```

---

## Secret Manager Configuration

In production (`ENV=production`), StudioTower binds secrets securely via **Google Cloud Secret Manager**. No credentials or keys are packaged into the Docker container or checked into source control.

The following secrets must be provisioned in your GCP project:

| Secret Name | Purpose | Example / Format |
|---|---|---|
| GEMINI_API_KEY | Gemini model inference | AIzaSy... |
| STUDIO_TOWER_MAINTENANCE_SECRET | Admin maintenance endpoint protection | High-entropy random hex string |
| STUDIO_TOWER_TASK_SECRET | Cloud Tasks webhook authentication | High-entropy random hex string |
| CURSOR_SIGNING_SECRET | Telemetry cursor encryption & signing | High-entropy random hex string |
| ACTION_SIGNING_SECRET | Action proposal signature & tamper-check | High-entropy random hex string |
| TELEMETRY_TENANT_KEY_PRIMARY | Tenant telemetry encryption key | High-entropy random hex string |
| GRAFANA_SERVICE_ACCOUNT_TOKEN | Grafana **instance** service account token for `grafana/mcp-grafana` and dashboard reads. | `glsa_...` only. Cannot write OTLP metrics. |
| GRAFANA_OTLP_TOKEN | Grafana Cloud Access Policy token used to **push** StudioTower Space activity to Grafana Cloud OTLP. | `glc_...` with `metrics:write` and `traces:write`. Create under Grafana Cloud Portal → OpenTelemetry. |

`GRAFANA_CLOUD_API_KEY` is an optional fallback if the service-account secret is absent. Prefer `GRAFANA_SERVICE_ACCOUNT_TOKEN` for reads. A `glc_` value may also be used as `GRAFANA_OTLP_TOKEN`.

Non-secret Grafana env (set by `scripts/deploy.ps1` / `deploy.sh`):

| Variable | Production value |
|---|---|
| GRAFANA_MCP_ENDPOINT | `http://127.0.0.1:8000/mcp` |
| GRAFANA_BASE_URL | `https://loftyladybug3305.grafana.net` |
| GRAFANA_ALLOWED_HOSTS | JSON array via `--env-vars-file`, e.g. `'["loftyladybug3305.grafana.net"]'` (plain comma-separated `--set-env-vars` strips quotes and breaks pydantic JSON list parsing) |
| GRAFANA_OTLP_ENDPOINT | `https://otlp-gateway-prod-us-east-2.grafana.net/otlp` (this stack's Prometheus/Tempo region; do not copy `prod-us-east-0` from an older token claim) |
| GRAFANA_OTLP_INSTANCE_ID | Grafana Cloud OTLP instance id for this stack (numeric). Not the org id. |

The in-app Lineage / Telemetry inspector does **not** display `GRAFANA_BASE_URL`. “Open in Grafana” is an HTTPS deep link allowlisted to `*.grafana.net`.

---

## 1. Backend Deployment (Google Cloud Run)

### Using Automated Deployment Script

StudioTower provides automated deployment scripts with **zero-downtime candidate verification** and safe rollback support:

- **Windows PowerShell**:
  ```powershell
  # Deploy to GCP project agentic-cinema-demo-2026 in us-central1
  powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1 -ProjectId "agentic-cinema-demo-2026" -Region "us-central1"
  ```

- **Linux / macOS Bash**:
  ```bash
  chmod +x scripts/deploy.sh
  ./scripts/deploy.sh --project-id "agentic-cinema-demo-2026" --region "us-central1"
  ```

### What the Deployment Pipeline Does:
1. **Secret Bindings**: Validates and provisions Secret Manager bindings.
2. **Container Build**: Builds the production Docker image using Google Cloud Build (`gcr.io/<project>/studio-tower-api:<tag>`). The image copies the official `grafana/mcp-grafana` binary.
3. **0% Traffic Candidate**: Deploys a new Cloud Run revision with **0% traffic tag** (`candidate-<timestamp>`).
4. **Smoke Verification**: Executes automated health and smoke tests against the candidate URL (`/v1/healthz`, `/readyz`).
5. **Traffic Shift**: Shifts 100% live traffic to the candidate revision only upon 100% test success.
6. **Automatic Rollback**: If health checks or smoke tests fail, the candidate is discarded with zero impact to users.

---

## 2. Frontend Deployment (Firebase Hosting)

### Step 1: Build the Production Bundle
```bash
cd frontend

# Run strict production build with bundle audit and budget checks
npm run build:prod
```
*Note: Bare `npm run build` is disallowed by design to prevent accidental unvalidated builds.*

### Step 2: Deploy to Firebase Hosting
```bash
# Ensure Firebase CLI is installed and logged in
firebase login

# Deploy hosting assets
firebase deploy --only hosting
```
The application will be live at:
https://<project-id>.web.app (e.g. https://agentic-cinema-demo-2026.web.app)

Firebase Hosting rewrites `/v1/**` to the same Cloud Run service. Redeploy hosting after SPA changes; API-only Cloud Run deploys do not refresh the Hosting bundle.

Cloud Run production uses `min-instances=0`. StudioTower flushes OTLP traces and metrics on the request path so Grafana ingest is not lost when CPU freezes after the response.

---

## 3. Database Indexes Deployment (Cloud Firestore)

Firestore requires composite indexes for complex multi-tenant filtering (such as queries sorted by `created_at` filtered by `space_id` and tag).

Deploy indexes defined in [firestore.indexes.json](../firestore.indexes.json):
```bash
firebase deploy --only firestore:indexes
```

---

## 4. Pre-Flight Release Audit Gate

Before promoting any build to official release, run the automated candidate release audit:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/audit-candidate-release.ps1
```

This gate runs comprehensive automated checks:
- [x] Strict clean-room inspection (zero local path strings, zero credential leaks)
- [x] Backend unit, integration, and security test suites
- [x] Frontend unit and accessibility (a11y) checks
- [x] Bundle size and dependency budget audit
- [x] Cloud Run remote health check verification
