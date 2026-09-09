from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LineageNodeType(str, Enum):
    SOURCE_FILE = "source_file"          # User uploaded treatment/script
    EXTRACTED_DATA = "extracted_data"    # ProDocuX parsed chunks
    AI_BREAKDOWN = "ai_breakdown"        # Gemini ADK scene breakdown
    APPROVAL_GATE = "approval_gate"      # Human Risk/Approval Gate
    CONTROL_ARTIFACT = "control_artifact"# PDX generated schedules, matrices
    MANIFEST = "manifest"                # RunManifest.json with SHA-256


class LineageRelation(str, Enum):
    DERIVED_FROM = "derived_from"
    EXTRACTED_BY = "extracted_by"
    PLANNED_BY = "planned_by"
    GATED_BY = "gated_by"
    GENERATED_ARTIFACT = "generated_artifact"


class LineageNode(BaseModel):
    id: str
    node_type: LineageNodeType
    label: str
    file_id: str | None = None
    run_id: str | None = None
    project_tag: str = "general"
    status: str | None = None  # e.g., "approved", "pending", "ready"
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LineageEdge(BaseModel):
    from_node_id: str
    to_node_id: str
    relation: LineageRelation


class SpaceLineageGraph(BaseModel):
    space_id: str
    project_tag: str | None = None
    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[LineageEdge] = Field(default_factory=list)
