"""Server-authoritative presentation contract for generated deliverables."""

from __future__ import annotations

from typing import Any, Literal

from app.models.action_proposal import ActionProposal, DeliverableFormat
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DeliverableSpecError(ValueError):
    """Raised when a requested presentation contract is not supported."""


class DeliverableSpec(BaseModel):
    """Versioned, signed, and allowlisted user presentation intent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["studiotower_deliverable_spec_v1"] = "studiotower_deliverable_spec_v1"
    artifact_type: Literal[
        "call_sheet",
        "shot_list",
        "stunt_risk",
        "production_budget",
        "scene_breakdown",
        "pitch_deck",
    ]
    output_format: DeliverableFormat
    audience: Literal["production", "producer", "director", "crew", "executive"] = "production"
    purpose: Literal["working_document", "presentation", "review", "data_exchange"] = "working_document"
    layout: Literal[
        "scene_cards",
        "one_scene_per_slide",
        "one_shot_per_slide",
        "department_tables",
        "tabular",
        "structured_data",
        "production_report",
        "pitch_deck",
    ]
    density: Literal["compact", "standard", "detailed", "visual"] = "standard"
    editable: bool = True
    sections: list[str] = Field(default_factory=list, max_length=12)
    unknown_value_policy: Literal["mark_tbd", "mark_review"] = "mark_tbd"


ACTION_ARTIFACT_TYPES = {
    "create_call_sheet": "call_sheet",
    # Legacy/internal action names remain renderable for queued work and
    # telemetry recovery. They are not added to the public confirmation
    # allowlist, so this does not widen what clients may request.
    "produce_schedule": "call_sheet",
    "generate_shot_list": "shot_list",
    "stunt_risk_breakdown": "stunt_risk",
    "export_production_budget": "production_budget",
    "create_scene_breakdown": "scene_breakdown",
    "create_deliverable": "scene_breakdown",
    "export_scene_list": "scene_breakdown",
    "generate_pitch_deck": "pitch_deck",
}

DEFAULT_LAYOUTS = {
    DeliverableFormat.PDF: "production_report",
    DeliverableFormat.DOCX: "production_report",
    DeliverableFormat.XLSX: "tabular",
    DeliverableFormat.CSV: "tabular",
    DeliverableFormat.PPTX: "one_scene_per_slide",
    DeliverableFormat.JSON: "structured_data",
}

ALLOWED_LAYOUTS = {
    DeliverableFormat.PDF: {"scene_cards", "department_tables", "production_report"},
    DeliverableFormat.DOCX: {"scene_cards", "department_tables", "production_report"},
    DeliverableFormat.XLSX: {"department_tables", "tabular"},
    DeliverableFormat.CSV: {"tabular"},
    DeliverableFormat.PPTX: {"one_scene_per_slide", "one_shot_per_slide", "pitch_deck"},
    DeliverableFormat.JSON: {"structured_data"},
}


def resolve_deliverable_spec(proposal: ActionProposal) -> DeliverableSpec:
    """Rebuild a model-proposed spec from an allowlist and signed proposal data."""
    output_format = proposal.output_format or DeliverableFormat.PDF
    raw = (proposal.metadata or {}).get("deliverable_spec") or {}
    if not isinstance(raw, dict):
        raise DeliverableSpecError("INVALID_DELIVERABLE_SPEC")

    artifact_type = ACTION_ARTIFACT_TYPES.get(proposal.action_type)
    if artifact_type is None:
        raise DeliverableSpecError("UNSUPPORTED_ARTIFACT_TYPE")

    safe_fields: dict[str, Any] = {
        key: raw[key]
        for key in (
            "audience",
            "purpose",
            "layout",
            "density",
            "editable",
            "sections",
            "unknown_value_policy",
        )
        if key in raw
    }
    safe_fields.update({
        "artifact_type": artifact_type,
        "output_format": output_format,
        "layout": safe_fields.get("layout") or DEFAULT_LAYOUTS[output_format],
    })
    try:
        spec = DeliverableSpec.model_validate(safe_fields)
    except ValidationError as exc:
        raise DeliverableSpecError("INVALID_DELIVERABLE_SPEC") from exc
    if spec.layout not in ALLOWED_LAYOUTS[output_format]:
        raise DeliverableSpecError("LAYOUT_NOT_SUPPORTED_FOR_FORMAT")
    return spec
