# ADR-0003: Production Control & Lineage Center (Right Panel Design)

## Status
Accepted (2026-08-10)

## Context
In film prep workflows, coordinators and department heads must track not only that an AI ran, but exactly what artifacts were produced, how they were derived from source documents, what gates they passed through, and whether the underlying run was healthy. A simple text ledger or external dashboard link does not provide immediate spatial and causal clarity.

## Decision
1. **Right Panel Architecture: Production Control & Lineage Center**:
   - The right drawer is divided into two primary views with instant tab switching:
     - **View 1: `[ 🌿 Artifact Lineage Graph ]` (Default)**
     - **View 2: `[ 📊 Run Health & Grafana Trace ]`**
2. **Artifact Lineage Directed Acyclic Graph (DAG)**:
   - Visualizes the causal derivation chain of files and state within the active Space:
     - `source_file` (Uploaded PDF/treatment)
     - `extracted_data` (ProDocuX parsed chunks)
     - `ai_breakdown` (Gemini `google-genai` structured output)
     - `approval_gate` (Deterministic human approval status: `pending`, `approved`, `rejected`)
     - `control_artifact` (PDX generated files: CSV shoot schedule, JSON conflict matrix, etc.)
     - `manifest` (Cryptographic `RunManifest.json` with SHA-256 digests)
   - Clicking any node opens an inspector showing file metadata, SHA-256 checksum, creator, creation timestamp, run ID, trace ID, associated project tag, and direct download/preview actions.
3. **Grafana Cloud MCP Observability & Live Diagnosis**:
   - Every execution run binds a unique `run_id` and OpenTelemetry-compatible `trace_id`.
   - The in-app waterfall shows local OTel spans (duration, LLM latency, tool sequences, gate waits).
   - For simulated or real execution failures, diagnosis issues MCP `tools/call` against the official `grafana/mcp-grafana` sidecar and presents a diagnostic summary. The waterfall itself is not a Grafana iframe.

## Consequences
- Full provenance transparency: users immediately see which inputs produced which schedule/manifest.
- Observable evidence directly tied to file artifacts and hackathon demo requirements.

## Implementation notes (2026-09-09)

Hackathon UI (`HACKATHON_GRAFANA_ONLY`): Lineage tab is a Grafana Tempo **process flow** for the active Space (span `event_type` / `resource_id` in order: file → message → run → gate). Telemetry tab is Grafana Prometheus **stats and timeseries** for Space activity (`studiotower_space_events_total`), not Tempo tables and not Billing/Usage. The inspector does not print the Grafana stack URL. Local artifact DAG and span waterfall remain in the codebase behind that flag. HITL approvals are in the header Tasks & Approvals view, not the Telemetry tab.

Lineage DAG icons still special-case PDF/FDX when that view is enabled. Historical Firestore diagnoses are not rewritten when MCP wiring changes.
