# ADR-0002: Space Tenant Isolation & Intra-Space Project Tagging

## Status
Accepted (2026-08-10)

## Context
StudioTower provides collaborative film prep control. Users can work 1:1 in an Agent DM or in N+1 Shared Spaces with team members. In production environments, a single Space (e.g. "Feature Film: Project Bersama") frequently handles multiple distinct sub-projects, shooting blocks, episodes, or workstreams (e.g., `#episode-1`, `#block-a-disaster`, `#stunts`, `#vfx-review`).

We need to support project segmentation without introducing complex enterprise Org trees or breaking Space-level binary tenant isolation.

## Decision
1. **Space Membership as Sole Tenant Boundary**:
   - `(space_id, uid)` membership is the single authorization key for all read/write operations on messages, files, runs, and artifacts.
   - Non-members attempting any Space access receive `403 Forbidden` without leaking resource existence.
   - Agent DMs (`kind = 'agent_dm'`) enforce single-user membership.
2. **Intra-Space Project / Topic Tagging**:
   - Each Space has a `tags` registry: `[{ id: string, name: string, slug: string, color: string }]`. Default tag `#general` is created on Space initialization.
   - `Message.project_tag`: Assigns a chat message to a specific project stream.
   - `FileRecord.project_tags`: Categorizes uploaded or generated files.
   - `Run.project_tag`: Binds a breakdown/execution run to a project tag.
3. **AI Context Scoping**:
   - When a user chats within a filtered tag view, the Gemini brain (`google-genai`) scopes retrieval to files and runs associated with that tag.
   - Explicit cross-tag references (e.g. `@agent check with #block-b`) permit the agent to read other tags within the *same* Space.
   - Tagging never breaches the Space boundary: non-members cannot see or query any tags or files.

## Consequences
- Team members can fluidly work on multiple sub-projects/blocks in a single collaborative Space.
- The UI can filter chat, files, and runs by active project tag.
- Tenant isolation remains crisp, testable, and robust.
