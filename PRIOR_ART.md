# PRIOR_ART & Locked External Dependencies

> **Product**: StudioTower — Observable production control for film prep  
> **Contest**: Google Cloud Agentic Cinema Hackathon  
> **Clean-Room Policy**: Full clean-room rewrite by Google Antigravity.

---

## 1. Locked External Non-AI Dependencies

PDX Artifact Engine and ProDocuX are external, deterministic, non-AI software dependencies. They are **not part of the StudioTower clean-room rewrite** and are consumed via standard public package/API contracts.

| Dependency | Approved Baseline (Commit / Tag) | Role & Scope | AI Status | License |
|---|---|---|---|---|
| **`pdx-artifact-engine`** | PyPI `0.3.0a6` (tag `v0.3.0a6`, commit `1f29a792b9b86cef1c54706ab154ad4c50cbfc6a`) | Deterministic execution plans, verification gates, checksum manifests, and artifact bundling via `ArtifactRuntime`. | **Strictly Non-AI** (Deterministic core) | Apache-2.0 |
| **`prodocux`** | PyPI `0.3.0rc5` | Deterministic document intake & Kernel extract/render (`prodocux_kernel`). | **Strictly Non-AI** (Deterministic parser) | Apache-2.0 |

### Architecture Boundary Principle

```text
Gemini via google-genai SDK                    (All AI reasoning & orchestration)
  │
  ▼
StudioTower Application Layer
  ├── ProDocuX Kernel (PyPI prodocux==0.3.0rc5)
  ├── PDX Artifact Engine (PyPI pdx-artifact-engine==0.3.0a6)
  └── grafana/mcp-grafana sidecar  (Partner MCP tools/call + local OTel waterfall)
```

- StudioTower does **not** vendor PDX or ProDocuX source trees into this repository as those upstream projects.
- Runtime `import`s published packages: `pdx_artifact_engine` / `pdx_artifact_core` and `prodocux_kernel`. It does not call Vertex AI Agent Engine or Google ADK.
- PDX is **not** an agent framework; all reasoning, breakdown formulation, and orchestration belong to Gemini (`google-genai`).
- Cinema-specific schemas, verifiers, and workflows belong to StudioTower, not PDX Core.

---

## 2. Quarantined Historical Prototype

The historical prototype in `incubator/cinema-production-agent` was produced with third-party coding assistants and is **quarantined**:
- Documents are used solely for understanding product requirements and lessons learned.
- **Zero code, tests, prompts, fixtures, CSS, deployment scripts, Git history, or container images** are copied, translated, or forked.
- All code in `studio-tower` is newly authored under Google Antigravity pair programming.
