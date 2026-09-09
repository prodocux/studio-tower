# StudioTower Installation & Operations Guide

This guide provides step-by-step instructions for installing, configuring, running, and verifying **StudioTower** across Windows, Linux, and macOS environments.

---

## System Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| **Python** | 3.12.0+ | 3.12.5+ |
| **Node.js** | 20.12.0+ (LTS) | 22.0.0+ |
| **Package Manager** | pip (Python), npm 10+ (Node) | pip & npm |
| **Operating System** | Windows 10/11, Ubuntu 22.04+, macOS 13+ | Any modern 64-bit OS |
| **Browser** | Chrome 120+, Firefox 120+, Safari 17+ | Chrome / Chromium |

---

## Option A: Quickstart Local Installation (Zero-Cloud Mode)

StudioTower includes a full in-memory mock engine. You do **not** need Google Cloud credentials, a credit card, or external API keys to run and test all features locally.

### Step 1: Clone or Open the Repository
```bash
cd studiotower
```

---

### Step 2: Backend Installation

1. Navigate to the `backend/` directory:
   ```bash
   cd backend
   ```

2. Create a Python virtual environment:
   ```bash
   python -m venv .venv
   ```

3. Activate the virtual environment:
   - **Windows (PowerShell)**:
     ```powershell
     .venv\Scripts\Activate.ps1
     ```
     *(If you see an execution policy error, run: `Set-ExecutionPolicy -Scope Process RemoteSigned` first)*
   - **Windows (CMD)**:
     ```cmd
     .venv\Scripts\activate.bat
     ```
   - **Linux / macOS**:
     ```bash
     source .venv/bin/activate
     ```

4. Install the package and its dependencies in editable mode:
   ```bash
   pip install -e ".[dev]"
   ```

5. Configure local environment variables:
   ```bash
   # Copy the example file
   cp .env.example .env
   ```
   *(Default configuration in `.env.example` has `STUDIO_TOWER_AUTH_MODE=dev`, `STUDIO_TOWER_STORE=memory`, and `STUDIO_TOWER_ARTIFACT_BACKEND=local`, configured for local testing.)*

6. Launch the FastAPI development server:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

7. Verify backend health in your browser or terminal:
   ```bash
   curl http://127.0.0.1:8000/healthz
   ```
   *Expected response:* `{"status":"ok","env":"development"}`  
   Production smoke tests also use `/v1/healthz` and `/readyz`.

---

### Step 3: Frontend Installation

Open a **separate terminal window**:

1. Navigate to the `frontend/` directory:
   ```bash
   cd frontend
   ```

2. Install Node dependencies:
   ```bash
   npm install
   ```

3. Configure frontend environment variables:
   ```bash
   # Copy the example file
   cp .env.example .env.local
   ```
   Ensure `.env.local` contains:
   ```dotenv
   VITE_AUTH_MODE=dev
   VITE_API_BASE_URL=http://127.0.0.1:8000
   ```

4. Start the Vite development server:
   ```bash
   npm run dev
   ```

5. Open your browser at:
   ```text
   http://localhost:5173
   ```

You are now in **Dev Mode**. The top navigation bar includes an instant Persona Switcher allowing you to test:
- **Admin**: Full platform and space governance permissions
- **Producer**: Financial oversight, human approval gates, deliverable sign-offs
- **Director**: Creative scene planning, script analysis, prompt steering
- **Crew**: Read-only schedule and call-sheet access
- **Auditor**: Tamper-evident log verification and compliance inspection

---

## Option B: Cloud-Connected / Staging Mode

To connect StudioTower to live Google Cloud and Grafana services:

### 1. Google Gemini API
Add your Gemini API key in `backend/.env`:
```dotenv
GEMINI_API_KEY=AIzaSy...your-gemini-key...
GEMINI_MODEL=gemini-3.6-flash
AI_INFERENCE_TIMEOUT_SECONDS=30.0
```

### 2. Google Cloud Firestore & Cloud Storage
1. Authenticate with Google Cloud SDK:
   ```bash
   gcloud auth application-default login
   ```
2. Update `backend/.env`:
   ```dotenv
   STUDIO_TOWER_STORE=firestore
   STUDIO_TOWER_FIREBASE_PROJECT_ID=your-gcp-project-id
   STUDIO_TOWER_ARTIFACT_BACKEND=gcs
   STUDIO_TOWER_GCS_BUCKET=your-bucket-name
   ```

### 3. Firebase Google Authentication
1. In Firebase Console, enable **Google Sign-In** under Authentication.
2. In `backend/.env`:
   ```dotenv
   STUDIO_TOWER_AUTH_MODE=firebase
   ```
3. In `frontend/.env.production` (or `.env.local`):
   ```dotenv
   VITE_AUTH_MODE=firebase
   VITE_FIREBASE_API_KEY=AIzaSy...
   VITE_FIREBASE_PROJECT_ID=your-firebase-project-id
   VITE_FIREBASE_APP_ID=1:123456789:web:abcdef
   ```

### 4. Grafana Cloud MCP (Model Context Protocol)
Local `uvicorn` does **not** start `grafana/mcp-grafana`. Diagnosis fail-closes MCP unless you run the official server yourself. Production Cloud Run starts it via `scripts/docker-start.sh`.

Add a Grafana **instance service account** token in `backend/.env` (`glsa_...` only — not Cloud Access Policy `glc_`, not hosted `mcp.grafana.com` OAuth):

```dotenv
GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_...
GRAFANA_MCP_ENDPOINT=http://127.0.0.1:8000/mcp
GRAFANA_BASE_URL=https://loftyladybug3305.grafana.net
GRAFANA_ALLOWED_HOSTS=loftyladybug3305.grafana.net
```

`GRAFANA_ALLOWED_HOSTS` allowlists optional HTTPS Grafana dashboard deep links (`*.grafana.net` in the SPA). The inspector UI does not print `GRAFANA_BASE_URL`. Cloud Run should pass the allowlist as a JSON array via `--env-vars-file` (see [DEPLOYMENT.md](DEPLOYMENT.md)). Grafana Cloud AI Observability / OTLP alone does not satisfy the Grafana partner-track MCP requirement.

To **export** Space activity into Grafana Cloud Prometheus/Tempo (needed for the hackathon Lineage process flow and Telemetry stats):

```dotenv
GRAFANA_OTLP_TOKEN=glc_...
GRAFANA_OTLP_ENDPOINT=https://otlp-gateway-prod-us-east-2.grafana.net/otlp
GRAFANA_OTLP_INSTANCE_ID=1742899
```

Use the OpenTelemetry instance id from the Grafana Cloud portal (or a probe-accepted id). Do not use the Grafana.com org id. The gateway host must match the stack Prometheus region.

---

## Verifying the Installation

Run automated test suites to ensure all services are functioning properly.

### 1. Backend Verification
```bash
cd backend
.venv\Scripts\Activate.ps1  # or source .venv/bin/activate
pytest -v
```
Backend pytest should pass. Include `test_grafana_mcp_protocol.py`, `test_grafana_visual.py`, and `test_grafana_otlp.py` for Grafana read/write paths.

### 2. Frontend Unit & Accessibility Verification
```bash
cd frontend
# Unit tests
npm run test

# Accessibility audit (WCAG AA compliance)
npm run test:a11y
```

### 3. End-to-End Test Suite (Playwright)
```bash
cd frontend
# Install Playwright browser binaries if first time
npx playwright install --with-deps chromium

# Run E2E journeys
npm run test:e2e
```

---

## Common Troubleshooting & FAQs

### Q1: Activate.ps1 cannot be loaded because running scripts is disabled on this system (Windows)
**Solution**: Run PowerShell as Administrator and execute:
```powershell
Set-ExecutionPolicy -Scope Process RemoteSigned
```
Then run `.venv\Scripts\Activate.ps1` again.

### Q2: Port 8000 or 5173 is already in use
**Solution**:
- To change backend port:
  ```bash
  uvicorn app.main:app --reload --port 8080
  ```
  *(Remember to update `VITE_API_BASE_URL=http://127.0.0.1:8080` in `frontend/.env.local`)*
- To change frontend port:
  ```bash
  npm run dev -- --port 3000
  ```

### Q3: Deliverables fail to generate locally
**Solution**: Ensure that ReportLab and openpyxl are installed:
```bash
pip install reportlab openpyxl python-docx
```
All deliverable templates fall back gracefully to structured plain text/markdown if specific binary formatting engines are absent.

### Q4: CORS errors when calling the API from frontend
**Solution**: Ensure `VITE_API_BASE_URL` matches the backend host and port. StudioTower's FastAPI backend enables permissive local CORS (`localhost:5173`, `127.0.0.1:5173`) in development mode by default.
