# ADR-0001: Clean-Room Rewrite & Non-AI External Dependencies

## Status
Accepted (2026-08-10)

## Context
StudioTower is being developed for the Google Cloud Agentic Cinema Hackathon. The historical prototype in `incubator/cinema-production-agent` was built with third-party coding tools and must be quarantined to guarantee complete provenance, security integrity, and hackathon rule compliance.

Furthermore, StudioTower integrates two deterministic non-AI libraries: `pdx-artifact-engine` and `prodocux`. These external libraries are published, deterministic packages and are not part of the clean-room rewrite.

## Decision
1. **Complete Clean-Room Implementation**:
   - All source code, frontend UI, backend API, schemas, prompt templates, tests, Dockerfiles, and CI scripts are authored from scratch by Google Antigravity.
   - Zero copy or translation of code from `incubator/cinema-production-agent`.
2. **Pinned External Dependencies**:
   - Pin `pdx-artifact-engine==0.3.0a6` from PyPI and call `ArtifactRuntime.execute_plan` / `validate_execution_plan` at runtime.
   - Pin `prodocux==0.3.0rc5` from PyPI (`prodocux_kernel`).
   - Consume them via standard package interfaces; do not vendor their source trees into StudioTower.
3. **AI Ownership**:
   - All AI reasoning, breakdown extraction, workflow orchestration, and conversational logic belong exclusively to Google Cloud AI (Gemini via `google-genai`).
   - Neither PDX nor ProDocuX are described or used as agent frameworks.

## Consequences
- Clean provenance trail verifiable by audit logs and git history.
- Deterministic gates and document parsing are decoupled from LLM non-determinism.
