# ADR-0005: Gemini google-genai Brain & Grafana Cloud MCP Observability

## Status
Accepted (2026-08-10); implementation superseded in part (2026-09-09)

## Context
StudioTower's AI brain must coordinate multi-role film preparation workflows (Production Coordinator, Continuity Lead, Risk Officer) and interact with real-time observability tooling in Grafana Cloud for the Hackathon Partner Track.

Official Contest Rules accept `google-adk`, `google-genai`, `google-generativeai`, or `google-cloud-aiplatform` equally. Grafana Labs requires runtime use of official `grafana/mcp-grafana` or hosted `mcp.grafana.com`.

## Decision
1. **Gemini via `google-genai`** (not Google ADK, not Vertex AI Agent Engine):
   - The `google-genai` SDK powers film-prep reasoning (`gemini-3.6-flash`).
   - Structured JSON schema enforcement (Pydantic) ensures Gemini emits typed breakdown plans.
   - Crew leadership voices live in one system prompt. Tools for document query, Space file association, PDX plan proposal, gate status, and Grafana MCP lookup are StudioTower services, not ADK Agent/Tool/Runner objects.
2. **Grafana Cloud MCP (Model Context Protocol)**:
   - Production Cloud Run runs official **`grafana/mcp-grafana`** (Streamable HTTP) beside FastAPI.
   - Diagnosis issues MCP JSON-RPC `initialize`, `tools/list`, and `tools/call`.
   - Hosted `mcp.grafana.com` browser OAuth is not used for the unattended API. Auth is a Grafana instance service account token (`glsa_...`).
3. **Grafana Cloud OTLP write** (2026-09-09):
   - Space activity metrics (`studiotower_space_events_total`) and Tempo spans (`studiotower.space.event`) export over OTLP HTTP with Basic auth (`instance_id:glc_token`).
   - Read token (`glsa_`) cannot ingest. Gateway region follows the stack Prometheus URL. Cloud Run request-path flush is required because CPU stops after the response.
4. **Hackathon inspector** (2026-09-09):
   - Lineage renders Tempo spans as a process flow. Telemetry renders Prometheus stats and a trend chart from Grafana `ds/query` frames.
   - API payloads omit the Grafana stack URL. Frontend deep links allow only `https://*.grafana.net`.

## Consequences
- Native Google Cloud AI stack (`google-genai`) powering all reasoning, which satisfies the Contest accepted-SDK list.
- Direct alignment with the Grafana Partner Track MCP-at-runtime requirement.

