"""Schema-first StudioTower adapter for the ProDocuX deterministic renderer."""

from __future__ import annotations

import json
from typing import Any

from app.models.action_proposal import ActionProposal, DeliverableFormat
from app.models.deliverable_spec import DeliverableSpec, resolve_deliverable_spec
from app.models.user import User
from prodocux_kernel.rendering import validate_content_blocks, write_content_blocks

MEDIA_TYPES = {
    DeliverableFormat.PDF: "application/pdf",
    DeliverableFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DeliverableFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DeliverableFormat.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    DeliverableFormat.CSV: "text/csv",
    DeliverableFormat.JSON: "application/json",
}


def _text(value: Any) -> str:
    return str(value if value is not None else "")


def _provenance_rows(extracted: dict[str, Any]) -> list[list[str]]:
    rows = [["Source file", "Generation", "SHA-256", "Chunks"]]
    for source in extracted.get("provenance") or []:
        rows.append([
            _text(source.get("filename") or source.get("file_id")),
            _text(source.get("generation")),
            _text(source.get("sha256")),
            _text(source.get("chunk_count")),
        ])
    if len(rows) == 1:
        rows.append(["REVIEW", "TBD", "No indexed source evidence supplied", "0"])
    return rows


def _scene_rows(extracted: dict[str, Any]) -> list[list[str]]:
    characters = ", ".join((extracted.get("characters") or [])[:12]) or "TBD"
    rows = [["Scene", "Slugline", "Day/Night", "Location", "Characters", "Evidence status"]]
    for scene in extracted.get("scenes") or []:
        rows.append([
            _text(scene.get("scene_number")),
            _text(scene.get("slugline")),
            _text(scene.get("day_night")),
            _text(scene.get("location")),
            characters,
            "SOURCE-DERIVED",
        ])
    if len(rows) == 1:
        rows.append(["TBD", "TBD", "TBD", "TBD", "TBD", "UNGROUNDED / REVIEW: screenplay scene evidence required"])
    return rows


def _scene_detail_blocks(extracted: dict[str, Any]) -> list[dict[str, Any]]:
    """Build page-friendly scene cards with a narrow evidence table."""
    scenes = list(extracted.get("scenes") or [])
    if not scenes:
        return [{
            "id": "scene-review",
            "type": "paragraphs",
            "paragraphs": ["REVIEW: No screenplay scene evidence was confidently extracted."],
        }]

    blocks: list[dict[str, Any]] = []
    for ordinal, scene in enumerate(scenes, 1):
        scene_number = _text(scene.get("scene_number") or ordinal)
        scene_characters = scene.get("characters") if "characters" in scene else extracted.get("characters")
        scene_elements = (
            scene.get("production_elements")
            if "production_elements" in scene
            else extracted.get("production_elements")
        )
        characters = ", ".join((scene_characters or [])[:12]) or "TBD / REVIEW"
        elements = ", ".join(scene_elements or []) or "TBD / REVIEW"
        blocks.extend([
            {
                "id": f"scene-{ordinal}-heading",
                "type": "heading",
                "level": 2,
                "text": f"Scene {scene_number}",
            },
            {
                "id": f"scene-{ordinal}-details",
                "type": "paragraphs",
                "paragraphs": [
                    f"Slugline: {_text(scene.get('slugline')) or 'TBD'}",
                ],
            },
            {
                "id": f"scene-{ordinal}-breakdown-table",
                "type": "table",
                "table": {
                    "header_rows": 1,
                    "rows": [
                        ["Breakdown field", "Evidence-derived value"],
                        ["Day / Night", _text(scene.get("day_night")) or "TBD / REVIEW"],
                        ["Location", _text(scene.get("location")) or "TBD / REVIEW"],
                        ["Characters", characters],
                        ["Production elements", elements],
                        ["Evidence status", "SOURCE-DERIVED / REVIEW BEFORE USE"],
                    ],
                },
            },
        ])
    return blocks


def _budget_rows(extracted: dict[str, Any]) -> list[list[str]]:
    """Create evidence-derived accounts while leaving commercial inputs TBD."""
    rows = [["Account", "Description", "Quantity", "Rate", "Amount", "Status"]]
    characters = sorted(set(extracted.get("characters") or []))
    locations = sorted({
        _text(scene.get("location")) for scene in extracted.get("scenes") or []
        if _text(scene.get("location")) and "TBD" not in _text(scene.get("location"))
    })
    elements = list(dict.fromkeys(extracted.get("production_elements") or []))
    if characters:
        rows.append(["2000", f"Cast: {', '.join(characters)}", str(len(characters)), "TBD", "TBD", "RATE CARD REQUIRED"])
    if locations:
        rows.append(["3000", f"Locations: {', '.join(locations)}", str(len(locations)), "TBD", "TBD", "SCHEDULE / RATE REQUIRED"])
    for index, element in enumerate(elements, 1):
        rows.append([f"4{index:03d}", element, "TBD", "TBD", "TBD", "DEPARTMENT REVIEW REQUIRED"])
    if len(rows) == 1:
        rows.append(["TBD", "No evidence-derived account or approved rate card supplied", "TBD", "TBD", "TBD", "REVIEW"])
    return rows


def _scene_slide_blocks(extracted: dict[str, Any]) -> list[dict[str, Any]]:
    """Build one concise slide per scene for PPTX output."""
    characters = ", ".join((extracted.get("characters") or [])[:8]) or "TBD / REVIEW"
    slides: list[dict[str, Any]] = []
    for ordinal, scene in enumerate(extracted.get("scenes") or [], 1):
        scene_number = _text(scene.get("scene_number") or ordinal)
        slides.append({
            "id": f"scene-{ordinal}-slide",
            "type": "slide",
            "title": f"Scene {scene_number}: {_text(scene.get('slugline'))}",
            "paragraphs": [
                f"Day / Night: {_text(scene.get('day_night')) or 'TBD'}",
                f"Location: {_text(scene.get('location')) or 'TBD'}",
                f"Characters: {characters}",
                "Evidence: SOURCE-DERIVED",
            ],
        })
    return slides or [{
        "id": "scene-review-slide",
        "type": "slide",
        "title": "Scene evidence required",
        "paragraphs": ["REVIEW: No screenplay scene evidence was confidently extracted."],
    }]


def _pdf_friendly_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kernel PDF writer joins table cells with ' | '. Convert tables to labels first."""
    converted: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != "table":
            converted.append(block)
            continue
        rows = list((block.get("table") or {}).get("rows") or [])
        if not rows:
            continue
        header_rows = int((block.get("table") or {}).get("header_rows") or 0)
        header = [str(cell) for cell in rows[0]] if rows else []
        body = rows[header_rows:] if header_rows else rows
        if len(header) == 2:
            converted.append({
                "id": block.get("id") or "fields",
                "type": "key_values",
                "pairs": [
                    {"label": str(row[0]) if row else "", "value": str(row[1]) if len(row) > 1 else ""}
                    for row in body
                ],
            })
            continue
        paragraphs: list[str] = []
        for row in body:
            parts = []
            for index, cell in enumerate(row):
                label = header[index] if index < len(header) else f"Col {index + 1}"
                parts.append(f"{label}: {cell}")
            if parts:
                paragraphs.append(" · ".join(parts))
        converted.append({
            "id": block.get("id") or "rows",
            "type": "paragraphs",
            "paragraphs": paragraphs or ["REVIEW: no table rows"],
        })
    return converted


def _blocks_for_action(
    proposal: ActionProposal,
    actor: User,
    extracted: dict[str, Any],
    target: DeliverableFormat,
    spec: DeliverableSpec,
) -> dict[str, Any]:
    action_type = proposal.action_type
    source_status = "SOURCE-DERIVED" if not extracted.get("is_ungrounded") else "REVIEW / UNGROUNDED"
    blocks: list[dict[str, Any]] = [
        {"id": "title", "type": "heading", "level": 1, "text": proposal.title},
        {
            "id": "meta",
            "type": "key_values",
            "pairs": [
                {"label": "Action", "value": action_type},
                {"label": "Prepared by", "value": actor.display_name},
                {"label": "Project track", "value": proposal.project_tag},
                {"label": "Evidence status", "value": source_status},
                {"label": "Audience", "value": spec.audience},
                {"label": "Purpose", "value": spec.purpose},
                {"label": "Layout", "value": spec.layout},
            ],
        },
    ]

    if action_type == "stunt_risk_breakdown":
        hazards = list(extracted.get("stunts_found") or [])
        hazard_rows = [["Detected hazard", "Evidence status", "Wire rigging", "Required review"]]
        for hazard in hazards:
            wire = "YES" if hazard in {"wire", "fall", "rigging"} else "TBD"
            hazard_rows.append([hazard, "SOURCE-DERIVED", wire, "Safety Coordinator approval required"])
        if not hazards:
            hazard_rows.append([
                "No hazard extracted from indexed text",
                "INSUFFICIENT EVIDENCE",
                "TBD",
                "REVIEW: Safety Coordinator inspection required",
            ])
        blocks.extend([
            {"id": "risk", "type": "heading", "level": 2, "text": "CRITICAL RISK ASSESSMENT"},
            {"id": "hazards", "type": "table", "table": {"header_rows": 1, "rows": hazard_rows}},
            {
                "id": "risk-review",
                "type": "paragraphs",
                "paragraphs": [
                    "REVIEW: This document does not authorize stunt execution. A qualified Safety Coordinator must verify all hazards and controls."
                ],
            },
        ])
    elif action_type == "export_production_budget":
        blocks.extend([
            {"id": "budget", "type": "heading", "level": 2, "text": "Production Budget Evidence Worksheet"},
            {
                "id": "budget-lines",
                "type": "table",
                "table": {
                    "header_rows": 1,
                    "rows": _budget_rows(extracted),
                },
            },
        ])
    elif action_type == "create_call_sheet":
        blocks.extend([
            {"id": "callsheet", "type": "heading", "level": 2, "text": "Call Sheet Draft"},
            {"id": "scenes", "type": "table", "table": {"header_rows": 1, "rows": _scene_rows(extracted)}},
            {"id": "call-review", "type": "paragraphs", "paragraphs": ["REVIEW: Call times, talent assignments, weather, and emergency details require production confirmation."]},
        ])
    elif action_type == "generate_shot_list":
        rows = [["Shot #", "Scene", "Description", "Lens", "Movement", "Status"]]
        for index, scene in enumerate(extracted.get("scenes") or [], 1):
            rows.append([f"{index}A", _text(scene.get("scene_number")), _text(scene.get("slugline")), "TBD", "TBD", "REVIEW"])
        if len(rows) == 1:
            rows.append(["TBD", "TBD", "No source scene extracted", "TBD", "TBD", "REVIEW"])
        if target is DeliverableFormat.PPTX:
            blocks.append({"id": "shots", "type": "heading", "level": 2, "text": "Shot List Draft"})
            for index, scene in enumerate(extracted.get("scenes") or [], 1):
                blocks.append({
                    "id": f"shot-{index}-slide",
                    "type": "slide",
                    "title": f"Shot {index}A - Scene {_text(scene.get('scene_number'))}",
                    "paragraphs": [
                        f"Description: {_text(scene.get('slugline'))}",
                        "Lens: TBD / Director and DP review",
                        "Movement: TBD / Director and DP review",
                        "Status: REVIEW",
                    ],
                })
            if len(rows) == 2 and rows[1][0] == "TBD":
                blocks.append({
                    "id": "shot-review-slide",
                    "type": "slide",
                    "title": "Shot evidence required",
                    "paragraphs": ["REVIEW: No source scene was extracted."],
                })
        else:
            blocks.extend([
                {"id": "shots", "type": "heading", "level": 2, "text": "Shot List Draft"},
                {"id": "shot-table", "type": "table", "table": {"header_rows": 1, "rows": rows}},
            ])
    else:
        blocks.append({"id": "breakdown", "type": "heading", "level": 2, "text": "Scene Breakdown"})
        if target is DeliverableFormat.PDF:
            blocks.extend(_scene_detail_blocks(extracted))
        elif spec.layout in {"tabular", "department_tables"}:
            blocks.append({"id": "scenes", "type": "table", "table": {"header_rows": 1, "rows": _scene_rows(extracted)}})
        elif spec.layout in {"one_scene_per_slide", "one_shot_per_slide", "pitch_deck"}:
            blocks.extend(_scene_slide_blocks(extracted))
        else:
            blocks.extend(_scene_detail_blocks(extracted))

    blocks.extend([
        {"id": "provenance-heading", "type": "heading", "level": 2, "text": "Sources and Provenance"},
        {"id": "provenance", "type": "table", "table": {"header_rows": 1, "rows": _provenance_rows(extracted)}},
    ])
    return {
        "schema_version": "prodocux_content_blocks_v1",
        "document": {"title": proposal.title, "locale": "en"},
        "blocks": blocks,
    }


def render_deliverable(
    proposal: ActionProposal,
    actor: User,
    extracted: dict[str, Any],
) -> tuple[str, str, bytes, bool, str]:
    """Render one signed format without title/description inference or silent fallback."""
    legacy_defaults = {
        "create_call_sheet": DeliverableFormat.CSV,
        "generate_shot_list": DeliverableFormat.CSV,
        "stunt_risk_breakdown": DeliverableFormat.JSON,
        "export_production_budget": DeliverableFormat.XLSX,
        "create_scene_breakdown": DeliverableFormat.JSON,
        "generate_pitch_deck": DeliverableFormat.PPTX,
    }
    target = proposal.output_format or legacy_defaults.get(proposal.action_type, DeliverableFormat.PDF)
    spec = resolve_deliverable_spec(proposal)
    title_slug = proposal.title.lower().replace(" ", "_").replace("/", "_")[:32] or "deliverable"

    if target is DeliverableFormat.PDF and proposal.action_type == "create_scene_breakdown":
        from app.services.deliverable_service import DeliverableExecutionService

        payload = DeliverableExecutionService._build_pdf_scene_breakdown(proposal, actor, extracted)
        return f"{title_slug}.{target.value}", MEDIA_TYPES[target], payload, False, ""

    content = DeliverableLayoutCompiler.compile(proposal, actor, extracted, spec)
    if target is DeliverableFormat.PDF:
        content = {**content, "blocks": _pdf_friendly_blocks(list(content.get("blocks") or []))}
    validate_content_blocks(content)

    if target is DeliverableFormat.JSON:
        payload = json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    else:
        payload = write_content_blocks(content, target.value)

    is_critical = proposal.action_type == "stunt_risk_breakdown"
    gate = "Critical Risk Gate: qualified Safety Coordinator approval is mandatory before use." if is_critical else ""
    return f"{title_slug}.{target.value}", MEDIA_TYPES[target], payload, is_critical, gate


class DeliverableLayoutCompiler:
    """Compile a validated presentation contract into ProDocuX content blocks."""

    @staticmethod
    def compile(
        proposal: ActionProposal,
        actor: User,
        extracted: dict[str, Any],
        spec: DeliverableSpec,
    ) -> dict[str, Any]:
        return _blocks_for_action(proposal, actor, extracted, spec.output_format, spec)
