# ADR-0004: PDX Core & ProDocuX Deterministic Integration

## Status
Accepted (2026-08-10)

## Context
StudioTower relies on deterministic, reproducible planning, risk gating, and document ingestion alongside AI reasoning. AI models can hallucinate or produce inconsistent schema outputs; deterministic engines are required to validate plans, pause execution at human approval gates, and produce verifiable cryptographic manifests.

## Decision
1. **ProDocuX Kernel intake**:
   - Pinned to PyPI `prodocux==0.3.0rc5`.
   - StudioTower calls `prodocux_kernel` extract/profile APIs for PDF (`extract_pdf_bytes`), DOCX/CSV/XLSX (`extract_content_blocks`), and PPTX (`profile_pptx_bytes`).
   - Final Draft `.fdx` and plain `.txt` have no Kernel extractors; those two stay StudioTower parsers.
   - Ingested text and structure are returned as immutable chunk records stored in GCS/Firestore.
2. **PDX Artifact Engine**:
   - Pinned to PyPI `pdx-artifact-engine==0.3.0a6`.
   - StudioTower builds a `pdx_execution_plan_v1` from Gemini's structured scene breakdown, validates it with Core `validate_execution_plan`, and executes it with `ArtifactRuntime.execute_plan`.
   - Human approval remains a StudioTower Space gate (`awaiting_approval -> running -> completed`) before the engine is invoked.
   - Produces immutable run artifacts (`Shoot_Schedule.csv`, `Cast_Prop_Conflict_Matrix.json`, `Unit_Handoff_Manifest.json`).
   - Generates `RunManifest.json` containing the engine `run_manifest` / `artifact_manifest` plus SHA-256 digests of uploaded outputs.
3. **Fail-Closed Security**:
   - If document extraction or schema verification fails, execution halts with a clear error; no mock fixture is silently substituted in production mode.

## Consequences
- Guarantees end-to-end plan determinism and verifiable cryptographic integrity.
- Clear separation between generative AI reasoning and non-AI deterministic execution.

## Implementation notes (2026-09-09)

`PDXEngine` imports `pdx_artifact_engine` from PyPI. Cinema schedule rows are StudioTower domain data; writing those artifacts and checksummed manifests is `ArtifactRuntime`. Intake: PDF/DOCX/CSV/XLSX call Kernel `extract_content_blocks` or `extract_pdf_bytes`; PPTX calls Kernel `profile_pptx_bytes` (rc5 `extract_content_blocks` TypeError on PPTX tables); FDX/TXT are StudioTower parsers. Formula-like cells are flagged, not stripped. Kernel writers cover the selected output format; crew-department templates are a follow-on.
