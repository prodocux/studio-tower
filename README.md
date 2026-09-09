# StudioTower — Observable Production Control Platform

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Preact + Vite](https://img.shields.io/badge/Frontend-Preact%20%2B%20Vite-673AB7.svg)](https://vitejs.dev/)
[![Google Cloud](https://img.shields.io/badge/Cloud%20Run-us--central1-4285F4.svg)](https://cloud.google.com/run)
[![Grafana Cloud MCP](https://img.shields.io/badge/Observability-Grafana%20Cloud%20MCP-F46800.svg)](https://grafana.com/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

> **Google Cloud Agentic Cinema Hackathon Entry**  
> **Preferred Partner Track**: **Grafana** (official `grafana/mcp-grafana` at runtime)  
> **Author**: Steven Wu & Google Antigravity Clean-Room Rebuild  

---

## Overview

**StudioTower** is an enterprise-grade, observable collaborative control tower engineered for film prep and multi-agent production coordination. Built on a clean-room architecture, it unites generative AI reasoning with deterministic execution gates, end-to-end artifact lineage tracking, and live Grafana telemetry.

### Key Capabilities

1. **AI Brain & Crew Coordination**:
   - Gemini (`gemini-3.6-flash`) via the **`google-genai` SDK** for structured scene breakdown, resource planning, shooting schedules, and continuity risk analysis. Crew leadership voices live in **one system prompt**, not Google ADK multi-agent runners.
2. **Deterministic Governance & Human-in-the-Loop Gates**:
   - **PDX Artifact Engine** from PyPI (`pdx-artifact-engine==0.3.0a6`): `validate_execution_plan` + `ArtifactRuntime.execute_plan` for plan verification and checksummed run manifests. StudioTower still owns Space membership and human approval gates (header **Tasks & Approvals**).
3. **Deterministic Document Intake and Rendering**:
   - **ProDocuX Kernel** (`prodocux==0.3.0rc5`) for deterministic ingestion of **PDF**, **DOCX**, **XLSX**, **PPTX**, and **CSV** via `prodocux_kernel` extract/profile APIs, plus Kernel writers for the selected output format. **FDX** and **TXT** have no Kernel extractors and stay StudioTower parsers. PPTX uses Kernel `profile_pptx_bytes` (text/tables/notes/image counts), not a vision model.
4. **Working-file deliverables**:
   - Kernel can write **PDF**, **DOCX**, **XLSX**, **PPTX**, **CSV**, and **JSON**. These are source-grounded working files. Crew-department templates (industry call sheets, breakdown layouts) are not fully exercised in this demo.
5. **Deep Observability with Grafana Cloud**:
   - Production Cloud Run runs official **`grafana/mcp-grafana`** (Streamable HTTP) beside FastAPI. Hackathon **Lineage** draws a process flow from Grafana Tempo Space spans (`file` → `message` → `run` → `gate`). **Telemetry** shows Prometheus stats and a Space-events trend (not Billing/Usage). Space activity is pushed with Grafana Cloud OTLP (`glc_`). The inspector does not print the Grafana stack hostname. Local DAG and span waterfall are parked (`HACKATHON_GRAFANA_ONLY`).
6. **Binary Space Isolation & Project Tagging**:
   - Google Chat-style binary Space boundaries ensuring zero cross-tenant contamination, augmented with intra-Space project tagging (#block-a, #stunts, #vfx).
7. **Role-Based Access Control (RBAC)**:
   - 5 dedicated persona modes: **Admin**, **Producer**, **Director**, **Crew**, and **Auditor** with strict capability-driven route enforcement.

---

## Live Demonstrations

| Component | Target URL | Status |
|---|---|---|
| **Production Web UI** | [https://agentic-cinema-demo-2026.web.app](https://agentic-cinema-demo-2026.web.app) | Live (Firebase Hosting) |
| **Backend API (Cloud Run)** | [https://studio-tower-api-mbvd6nfacq-uc.a.run.app](https://studio-tower-api-mbvd6nfacq-uc.a.run.app) | Live (Cloud Run us-central1) |
| **API Healthcheck** | [https://studio-tower-api-mbvd6nfacq-uc.a.run.app/v1/healthz](https://studio-tower-api-mbvd6nfacq-uc.a.run.app/v1/healthz) | Healthy |
| **API Documentation** | [https://studio-tower-api-mbvd6nfacq-uc.a.run.app/docs](https://studio-tower-api-mbvd6nfacq-uc.a.run.app/docs) | Swagger UI |

---

## System Architecture

```text
                    ┌──────────────────────────────────────────────┐
                    │      StudioTower Web UI (Preact / Vite)       │
                    │   - Space Selector & Intra-Space Tags        │
                    │   - Lineage tab: Grafana Tempo process flow  │
                    │   - Telemetry tab: Prometheus stats + trend  │
                    │   - Tasks & Approvals (HITL) in the header   │
                    └──────────────────────┬───────────────────────┘
                                           │ Firebase ID Token / Dev Auth
                                           ▼
                    ┌──────────────────────────────────────────────┐
                    │       FastAPI Control Tower (Cloud Run)       │
                    │  ├── Space & RBAC Policy Enforcement         │
                    │  ├── Multi-Format Deliverable Generator      │
                    │  ├── Ingestion & Action Runners (Cloud Tasks)│
                    │  ├── grafana/mcp-grafana sidecar (:8000/mcp) │
                    │  └── OTLP export + telemetry sanitizer       │
                    └───┬──────────────┬──────────────┬────────────┘
                        │              │              │
           ┌────────────▼──┐    ┌──────▼──────┐ ┌─────▼───────────────┐
           │ Cloud Storage │    │  Firestore  │ │ google-genai SDK    │
           │ (SHA-256      │    │  (Tenancy   │ │ (Gemini 3.6 film-   │
           │  artifacts)   │    │   Metadata) │ │  prep reasoning)    │
           └───────────────┘    └─────────────┘ └─────────────────────┘
                        │              │              │
           ┌────────────▼──────────────▼──────────────▼───────────────┐
           │                     External Gates                       │
           │  ├── PDX Artifact Engine (PyPI ArtifactRuntime)          │
           │  ├── ProDocuX Kernel (PyPI extract/profile + writers)    │
           │  └── Grafana Cloud (HTTP API read + OTLP write + MCP)    │
           └──────────────────────────────────────────────────────────┘
```

---

## Quickstart (Local Development in 3 Minutes)

StudioTower is engineered with **Zero-Cloud Local Mocking**. You can boot and test the entire stack locally without Google Cloud credentials or external API keys.

### 1. Prerequisites
- **Python 3.12+**
- **Node.js 20+** (LTS recommended)
- **Git**

### 2. Backend Setup
```bash
# Clone or navigate to the repository
cd backend

# Create and activate Python virtual environment
python -m venv .venv

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies in editable mode
pip install -e ".[dev]"

# Copy development environment template
cp .env.example .env

# Start FastAPI dev server (defaults to in-memory store & dev auth)
uvicorn app.main:app --reload --port 8000
```
Backend API will be live at: http://127.0.0.1:8000 (Healthcheck: http://127.0.0.1:8000/healthz or `/v1/healthz`).

Local diagnosis **fail-closes** Grafana MCP unless you run `grafana/mcp-grafana` yourself and set `GRAFANA_MCP_ENDPOINT` plus a Grafana **service account token** (`glsa_...`). Production Cloud Run starts that sidecar via `scripts/docker-start.sh`.

### 3. Frontend Setup
Open a new terminal:
```bash
cd frontend

# Install Node dependencies
npm install

# Copy local development environment template
cp .env.example .env.local

# Start Vite dev server
npm run dev
```
Frontend UI will be live at: http://localhost:5173.

> In local dev mode (`STUDIO_TOWER_AUTH_MODE=dev`), the UI automatically bypasses external Google Sign-In and offers a quick persona switcher (Admin, Producer, Director, Crew, Auditor).

---

## Testing & Quality Gates

StudioTower enforces strict quality, security, and accessibility gates across both backend and frontend.

### Backend Test Suite
```bash
cd backend
# Run full test suite with coverage
pytest
```
*Features covered:*
- Multi-tenant space boundary isolation (`test_auth_and_tenancy.py`, `test_space_governance.py`)
- 5-format deliverable export verification (`test_deliverable_5_formats.py`) — Kernel writers exist; this is format plumbing, not a full matrix of crew-department templates.
- Grafana HTTP API visual board + OTLP ingest (`test_grafana_visual.py`, `test_grafana_otlp.py`)
- Grafana MCP JSON-RPC client (`test_grafana_mcp.py`, `test_grafana_mcp_protocol.py`)
- Telemetry sanitizer & zero-leak assertions (`test_slice_e_telemetry_and_observability.py`)

### Frontend Test Suite
```bash
cd frontend
# Unit & component tests
npm run test

# Accessibility (WCAG / a11y) audit
npm run test:a11y

# Playwright End-to-End test suite
npm run test:e2e
```

### Full Release Audit Gate
```powershell
# Run the automated candidate release audit
powershell -ExecutionPolicy Bypass -File scripts/audit-candidate-release.ps1
```

---

## Environment Variables Reference

### Backend (`backend/.env`)

| Variable | Default | Description |
|---|---|---|
| ENV | development | Runtime environment (development, staging, production) |
| STUDIO_TOWER_AUTH_MODE | dev | Authentication provider: `dev` (mock headers) or `firebase` (Google ID tokens) |
| STUDIO_TOWER_STORE | memory | Tenancy database: `memory` (local fast mock) or `firestore` (Google Cloud Firestore) |
| STUDIO_TOWER_ARTIFACT_BACKEND | local | Storage provider: `local` (disk cache) or `gcs` (Google Cloud Storage) |
| STUDIO_TOWER_GCS_BUCKET | *empty* | Target GCS bucket for immutable deliverables when using `gcs` |
| GEMINI_API_KEY | *empty* | Google Gemini API key (optional; deterministic mock engine used if omitted) |
| GEMINI_MODEL | gemini-3.6-flash | Gemini model variant for film prep reasoning |
| GRAFANA_SERVICE_ACCOUNT_TOKEN | *empty* | Preferred Grafana **instance** service account token (`glsa_...`). Do not use Cloud Access Policy `glc_` or hosted MCP OAuth for unattended Cloud Run. |
| GRAFANA_CLOUD_API_KEY | *empty* | Fallback Bearer token if the service-account secret is unset |
| GRAFANA_MCP_ENDPOINT | *empty* locally; `http://127.0.0.1:8000/mcp` in production | StudioTower JSON-RPC client talks to official `grafana/mcp-grafana` |
| GRAFANA_BASE_URL | https://loftyladybug3305.grafana.net | Grafana Cloud stack URL used by the sidecar (`GRAFANA_URL`) |
| GRAFANA_ALLOWED_HOSTS | stack hostname | Allowlist for optional HTTPS Grafana dashboard deep links. Prefer JSON array form in Cloud Run (`["loftyladybug3305.grafana.net"]`). |
| GRAFANA_OTLP_TOKEN | *empty* | Grafana Cloud Access Policy token (`glc_...`) with `metrics:write` and `traces:write`. Required to **push** Space activity. |
| GRAFANA_OTLP_ENDPOINT | *empty* | OTLP gateway base URL, e.g. `https://otlp-gateway-prod-us-east-2.grafana.net/otlp`. Prefer the region from the stack Prometheus URL over a hardcoded region. |
| GRAFANA_OTLP_INSTANCE_ID | *empty* | Grafana Cloud OTLP/metrics instance id (numeric). This is **not** the Grafana.com org id. |

### Frontend (`frontend/.env`)

| Variable | Default | Description |
|---|---|---|
| VITE_AUTH_MODE | dev | `dev` (persona switcher) or `firebase` (Google OAuth) |
| VITE_API_BASE_URL | http://127.0.0.1:8000 | Backend API base URL |
| VITE_FIREBASE_API_KEY | *empty* | Firebase Web API Key |
| VITE_FIREBASE_PROJECT_ID | *empty* | Firebase Project ID |
| VITE_FIREBASE_APP_ID | *empty* | Firebase Web App ID |

---

## Repository Structure

```text
studiotower/
├── backend/                       # FastAPI Backend & Agent Service
│   ├── app/
│   │   ├── api/                  # REST routers (auth, spaces, chat, files, runs, deliverables)
│   │   ├── agent/                # Gemini prompts, google-genai brain, tool schemas
│   │   ├── core/                 # Security, tenancy policies, sanitizer, OpenTelemetry
│   │   ├── integrations/         # grafana_mcp.py, PDX ArtifactRuntime, ProDocuX Kernel adapter
│   │   ├── models/               # Domain entities (Space, User, Run, Lineage, Deliverable)
│   │   └── services/             # Business logic (ActionRunner, DeliverableService, Lineage)
│   └── tests/                    # Backend pytest modules (incl. Grafana MCP protocol)
├── frontend/                      # Web SPA (Preact + Vite + Tailwind CSS)
│   ├── src/
│   │   ├── components/           # ChatArea, FileCenter, RightPanel, GrafanaCloudBoard, Header
│   │   ├── services/             # API client, Auth, Signals Store, Toast
│   │   └── types/                # Strict TypeScript domain types
│   ├── scripts/                  # Bundle auditing & config validation
│   └── tests/                    # Vitest and Playwright test suites
├── docs/                          # Comprehensive Documentation
│   ├── INSTALLATION.md           # Step-by-step installation & operator guide
│   ├── DEPLOYMENT.md             # Production Cloud Run & Firebase deployment
│   └── api_contracts.md          # REST API contracts & error schemas
├── scripts/                       # Automation scripts
│   ├── deploy.ps1                # Cloud Run deployment script (PowerShell)
│   ├── deploy.sh                 # Cloud Run deployment script (Bash)
│   ├── docker-start.sh           # Cloud Run entry: mcp-grafana sidecar + uvicorn
│   ├── export_public_sync.py     # Deterministic Mode B public export gate
│   ├── list_public_export.py     # Export file enumerator
│   └── audit-candidate-release.ps1 # Pre-flight candidate audit script
├── .env.example                   # Root environment configuration template
├── Dockerfile                     # Multi-stage production container (copies mcp-grafana)
├── firebase.json                  # Firebase Hosting routing rules
├── firestore.indexes.json         # Firestore composite indexes
├── PRIOR_ART.md                   # Pinned dependency hashes & provenance
├── LICENSE                        # Apache 2.0 License
└── pyproject.toml                 # Python project specification
```

---

## Documentation Index

- [**Installation & Operator Guide**](docs/INSTALLATION.md): Complete setup walkthrough for Windows, Linux, and macOS.
- [**Production Deployment Guide**](docs/DEPLOYMENT.md): Cloud Run containerization, Firestore indexes, and Firebase Hosting.
- [**API Contracts Specification**](docs/api_contracts.md): Full REST endpoint schemas, headers, and error models.

---

## Security & Clean-Room Assurance

- **Zero Hardcoded Secrets**: Secrets are injected via Google Cloud Secret Manager in production or .env files locally.
- **Strict Tenancy Boundaries**: Every request resolves space membership before reading or writing to datastores.
- **Fail-Closed Auth**: Dev headers are strictly rejected in staging and production environments.
- **Deterministic Deliverables**: User-selected formats are generated through ProDocuX Kernel with content hashing and SHA-256 lineage logs.
- **Grafana write vs read**: Dashboard reads use `GRAFANA_SERVICE_ACCOUNT_TOKEN` (`glsa_`). OTLP ingest uses `GRAFANA_OTLP_TOKEN` (`glc_`) from Secret Manager. Tokens are never returned in API payloads. The inspector kicker does not display `GRAFANA_BASE_URL`; “Open in Grafana” links must be HTTPS on `*.grafana.net`.
- **Public export**: `PROJECT_STORY.md`, `JUDGES_GUIDE.md`, `AI_DEVELOPMENT_LOG.md`, `FRONTEND_PRODUCT_IMPROVEMENT_PLAN.md`, `docs/phase0_baseline.md`, and `docs/adr/` stay in the incubator tree and are **not** copied to the public clone. `scripts/export_public_sync.py` fail-closes if the export set contains live `glsa_` / `glc_eyJ` / Google API keys or private-key PEM.

---

## License

Distributed under the Apache 2.0 License. See [LICENSE](LICENSE) for more information.
