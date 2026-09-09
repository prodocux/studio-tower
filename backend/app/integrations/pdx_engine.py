import csv
import io
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pdx_artifact_core import canonical_digest, validate_execution_plan
from pdx_artifact_engine import ArtifactRuntime, __version__ as PDX_ENGINE_VERSION
from pdx_artifact_engine.paths import ensure_within_directory
from pdx_artifact_engine.registry import SkillDefinition, SkillRegistry

from app.models.action_proposal import ArtifactDescriptor
from app.models.file_record import FileSourceType
from app.models.run import Run
from app.models.user import User
from app.services.file_service import FileService
from app.services.storage import store

PDX_ENGINE_LABEL = f"pdx-artifact-engine=={PDX_ENGINE_VERSION}"

_CSV_FIELDS = (
    "shoot_day",
    "scene_number",
    "slugline",
    "location",
    "cast",
    "props",
    "stunt_level",
    "vfx_tier",
)

_SKILL_FAILURE = ({"code": "WRITE_FAILED", "meaning": "Artifact write failed", "retryable": False},)


def _skill(name: str, description: str) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        version=PDX_ENGINE_VERSION,
        domain="studiotower",
        description=description,
        entrypoint="app.integrations.pdx_engine:_write_artifact_executor",
        inputs=("filename", "format", "payload"),
        outputs=("file",),
        artifacts=("file",),
        failure_codes=_SKILL_FAILURE,
        verification_hooks=("file_exists",),
    )


_STUDIO_SKILLS = SkillRegistry(
    [
        _skill("studiotower.write_schedule", "Write Shoot_Schedule.csv from the validated plan matrix."),
        _skill("studiotower.write_conflicts", "Write Cast_Prop_Conflict_Matrix.json."),
        _skill("studiotower.write_handoff", "Write Unit_Handoff_Manifest.json."),
    ]
)


def _write_artifact_executor(inputs: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = str(inputs["filename"])
    fmt = str(inputs["format"])
    payload = inputs.get("payload")
    path = ensure_within_directory(output_dir, filename, label="studiotower artifact")
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(_CSV_FIELDS))
        writer.writeheader()
        for row in payload or []:
            writer.writerow({key: row.get(key, "") for key in _CSV_FIELDS})
        path.write_text(buffer.getvalue(), encoding="utf-8")
    elif fmt == "json":
        path.write_bytes(json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8"))
    else:
        raise ValueError(f"Unsupported artifact format: {fmt}")
    return {
        "result": {"status": "completed", "filename": filename},
        "files": [path],
        "outputs": {"file": path.as_posix(), "filename": filename},
    }


def _require_valid_plan(plan: dict[str, Any]) -> dict[str, Any]:
    errors = validate_execution_plan(plan)
    if errors:
        raise ValueError("PDX execution plan failed validation: " + "; ".join(errors))
    return plan


def _build_execution_plan(
    *,
    request_id: str,
    schedule_rows: list[dict[str, Any]],
    conflicts: list[Any],
    handoff: dict[str, Any],
    schedule_filename: str,
    conflicts_filename: str,
    handoff_filename: str,
) -> dict[str, Any]:
    return {
        "schema_version": "pdx_execution_plan_v1",
        "request_id": request_id,
        "producer": {"type": "studiotower", "name": "pdx_engine"},
        "intent": {
            "summary": "Bundle shoot schedule, conflict matrix, and unit handoff after human approval.",
            "artifact_type": "studiotower_control_package",
        },
        "steps": [
            {
                "id": "write_schedule",
                "kind": "tool",
                "tool": "studiotower.write_schedule",
                "name": "write shoot schedule",
                "inputs": {
                    "filename": schedule_filename,
                    "format": "csv",
                    "payload": schedule_rows,
                },
                "outputs": ["file"],
                "policies": {"approval_required": False},
            },
            {
                "id": "write_conflicts",
                "kind": "tool",
                "tool": "studiotower.write_conflicts",
                "name": "write conflict matrix",
                "depends_on": ["write_schedule"],
                "inputs": {
                    "filename": conflicts_filename,
                    "format": "json",
                    "payload": conflicts,
                },
                "outputs": ["file"],
                "policies": {"approval_required": False},
            },
            {
                "id": "write_handoff",
                "kind": "tool",
                "tool": "studiotower.write_handoff",
                "name": "write unit handoff",
                "depends_on": ["write_conflicts"],
                "inputs": {
                    "filename": handoff_filename,
                    "format": "json",
                    "payload": handoff,
                },
                "outputs": ["file"],
                "policies": {"approval_required": False},
            },
        ],
        "verification": [
            {"id": "schedule_done", "check": "step_completed:write_schedule", "fail_action": "stop"},
            {"id": "conflicts_done", "check": "step_completed:write_conflicts", "fail_action": "stop"},
            {"id": "handoff_done", "check": "step_completed:write_handoff", "fail_action": "stop"},
        ],
        "policies": {
            "timeout_seconds": 60,
            "max_retries": 0,
            "default_approval_required": False,
        },
    }


def _cinema_schedule(breakdown_dict: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    scenes = breakdown_dict.get("scenes", [])
    total_shoot_days = max(1, (len(scenes) + 1) // 2)
    schedule_rows: list[dict[str, Any]] = []
    for i, sc in enumerate(scenes):
        shoot_day = (i // 2) + 1
        res = sc.get("resources", {})
        schedule_rows.append(
            {
                "shoot_day": shoot_day,
                "scene_number": sc.get("scene_number", i + 1),
                "slugline": sc.get("slugline", "UNTITLED SCENE"),
                "location": ", ".join(res.get("locations", ["Soundstage 1"])),
                "cast": ", ".join(res.get("cast", ["Cast Ensemble"])),
                "props": ", ".join(res.get("props", ["Standard Set Dress"])),
                "stunt_level": res.get("stunt_level", "none"),
                "vfx_tier": res.get("vfx_tier", "none"),
            }
        )
    return schedule_rows, total_shoot_days


def _handoff_payload(space_id: str, run: Run, tag: str, schedule_rows: list[dict[str, Any]]) -> dict[str, Any]:
    unit_assignments: list[dict[str, Any]] = []
    if schedule_rows:
        location_groups: dict[str, list[int]] = {}
        location_leads: dict[str, str] = {}
        for row in schedule_rows:
            loc = row.get("location", "Main Unit").split(",")[0].strip() or "Main Unit"
            sc_num = row.get("scene_number", 1)
            location_groups.setdefault(loc, []).append(sc_num)
            if loc not in location_leads:
                cast_str = row.get("cast", "")
                first_cast = [c.strip() for c in cast_str.split(",") if c.strip() and c.strip() != "Cast Ensemble"]
                location_leads[loc] = first_cast[0] if first_cast else "[UNASSIGNED - PENDING SCRIPT CASTING]"
        for idx, (loc, sc_list) in enumerate(location_groups.items(), start=1):
            unit_assignments.append(
                {
                    "unit": f"Unit {idx} - {loc}",
                    "scenes": sc_list,
                    "lead": location_leads.get(loc, "[UNASSIGNED - PENDING SCRIPT CASTING]"),
                }
            )
    else:
        unit_assignments = [
            {
                "unit": "Unit 1 - General Production",
                "scenes": [1],
                "lead": "[UNASSIGNED - PENDING SCRIPT CASTING]",
            }
        ]
    deterministic_ts = (run.created_at or datetime(2026, 1, 1, tzinfo=UTC)).isoformat()
    return {
        "space_id": space_id,
        "run_id": run.run_id,
        "project_tag": tag,
        "unit_assignments": unit_assignments,
        "generated_at": deterministic_ts,
    }


def _stable_engine_manifests(run_id: str, engine_result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    run_manifest = dict(engine_result.get("run_manifest") or {})
    run_manifest["run_id"] = run_id
    run_manifest["request_id"] = run_id
    stable_steps = []
    for step in run_manifest.get("steps") or []:
        stable_steps.append(
            {
                "step_id": step.get("step_id"),
                "kind": step.get("kind"),
                "name": step.get("name"),
                "status": step.get("status"),
            }
        )
    run_manifest["steps"] = stable_steps

    artifact_manifest = dict(engine_result.get("artifact_manifest") or {})
    artifact_manifest["artifact_id"] = run_id
    files = []
    for entry in artifact_manifest.get("files") or []:
        files.append(
            {
                "path": Path(str(entry.get("path", ""))).name,
                "role": entry.get("role"),
                "sha256": entry.get("sha256"),
            }
        )
    artifact_manifest["files"] = files
    artifact_manifest["provenance"] = []
    return run_manifest, artifact_manifest


def _find_output(output_dir: Path, filename: str) -> Path:
    matches = [path for path in output_dir.rglob(filename) if path.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"PDX engine did not emit exactly one {filename}")
    return matches[0]


class PDXEngine:
    """
    Deterministic plan verification and artifact bundling via the published
    PyPI package pdx-artifact-engine (ArtifactRuntime + Core plan validation).
    """

    @staticmethod
    def generate_plan_matrix(breakdown_dict: dict[str, Any]) -> dict[str, Any]:
        """
        Formulate deterministic shoot units and a validated PDX execution plan.
        """
        schedule_rows, total_shoot_days = _cinema_schedule(breakdown_dict)
        execution_plan = _require_valid_plan(
            _build_execution_plan(
                request_id="studiotower_plan_preview",
                schedule_rows=schedule_rows,
                conflicts=list(breakdown_dict.get("detected_conflicts", [])),
                handoff={"unit_assignments": []},
                schedule_filename="Shoot_Schedule.csv",
                conflicts_filename="Cast_Prop_Conflict_Matrix.json",
                handoff_filename="Unit_Handoff_Manifest.json",
            )
        )
        return {
            "total_scenes": len(breakdown_dict.get("scenes", [])),
            "estimated_shoot_days": total_shoot_days,
            "schedule_rows": schedule_rows,
            "conflicts": breakdown_dict.get("detected_conflicts", []),
            "pdx_version": PDX_ENGINE_LABEL,
            "pdx_plan_digest": canonical_digest(execution_plan),
            "execution_plan": execution_plan,
        }

    @staticmethod
    def execute_and_bundle(space_id: str, run: Run, user: User) -> Run:
        """
        Execute the published ArtifactRuntime and register outputs in the Space library.
        """
        start_time = time.time()
        breakdown_dict = run.scene_breakdown or {}
        plan_matrix = PDXEngine.generate_plan_matrix(breakdown_dict)
        tag = run.project_tag or "general"
        suffix = run.run_id[-6:]
        schedule_filename = f"Shoot_Schedule_{suffix}.csv"
        conflicts_filename = f"Cast_Prop_Conflict_Matrix_{suffix}.json"
        handoff_filename = f"Unit_Handoff_Manifest_{suffix}.json"
        handoff = _handoff_payload(space_id, run, tag, plan_matrix["schedule_rows"])
        execution_plan = _require_valid_plan(
            _build_execution_plan(
                request_id=run.run_id,
                schedule_rows=plan_matrix["schedule_rows"],
                conflicts=list(plan_matrix.get("conflicts", [])),
                handoff=handoff,
                schedule_filename=schedule_filename,
                conflicts_filename=conflicts_filename,
                handoff_filename=handoff_filename,
            )
        )
        plan_matrix["execution_plan"] = execution_plan
        plan_matrix["pdx_plan_digest"] = canonical_digest(execution_plan)
        run.plan = plan_matrix

        runtime = ArtifactRuntime(
            _STUDIO_SKILLS,
            {
                "studiotower.write_schedule": _write_artifact_executor,
                "studiotower.write_conflicts": _write_artifact_executor,
                "studiotower.write_handoff": _write_artifact_executor,
            },
            allow_mock=False,
            planner_name="studiotower.pdx_engine",
        )
        with tempfile.TemporaryDirectory(prefix="studiotower_pdx_") as tmp:
            engine_result = runtime.execute_plan(execution_plan, tmp)
            run_manifest = engine_result.get("run_manifest") or {}
            status = str(run_manifest.get("status") or "")
            if status not in {"completed", "completed_with_review"}:
                detail = "; ".join(run_manifest.get("errors") or [status or "unknown"])
                raise RuntimeError(f"pdx-artifact-engine bundling failed: {detail}")
            output_root = Path(tmp)
            schedule_bytes = _find_output(output_root, schedule_filename).read_bytes()
            conflicts_bytes = _find_output(output_root, conflicts_filename).read_bytes()
            handoff_bytes = _find_output(output_root, handoff_filename).read_bytes()
            engine_run_manifest, engine_artifact_manifest = _stable_engine_manifests(run.run_id, engine_result)

        created_files: list[tuple[Any, bytes]] = []
        artifact_ids: list[str] = []
        try:
            schedule_file = FileService.upload_file(
                space_id=space_id,
                filename=schedule_filename,
                content=schedule_bytes,
                content_type="text/csv",
                user=user,
                project_tags=[tag],
                source_type=FileSourceType.PDX_ARTIFACT,
                run_id=run.run_id,
                publication_status="pending_approval",
                file_id=f"file_{run.run_id}_schedule",
            )
            created_files.append((schedule_file, schedule_bytes))
            artifact_ids.append(schedule_file.file_id)

            conflict_file = FileService.upload_file(
                space_id=space_id,
                filename=conflicts_filename,
                content=conflicts_bytes,
                content_type="application/json",
                user=user,
                project_tags=[tag],
                source_type=FileSourceType.PDX_ARTIFACT,
                run_id=run.run_id,
                publication_status="pending_approval",
                file_id=f"file_{run.run_id}_conflicts",
            )
            created_files.append((conflict_file, conflicts_bytes))
            artifact_ids.append(conflict_file.file_id)

            handoff_file = FileService.upload_file(
                space_id=space_id,
                filename=handoff_filename,
                content=handoff_bytes,
                content_type="application/json",
                user=user,
                project_tags=[tag],
                source_type=FileSourceType.PDX_ARTIFACT,
                run_id=run.run_id,
                publication_status="pending_approval",
                file_id=f"file_{run.run_id}_handoff",
            )
            created_files.append((handoff_file, handoff_bytes))
            artifact_ids.append(handoff_file.file_id)

            deterministic_ts = (run.created_at or datetime(2026, 1, 1, tzinfo=UTC)).isoformat()
            manifest_data = {
                "run_id": run.run_id,
                "space_id": space_id,
                "trace_id": run.trace_id,
                "project_tag": tag,
                "pdx_engine_baseline": PDX_ENGINE_LABEL,
                "pdx_plan_digest": plan_matrix["pdx_plan_digest"],
                "pdx_run_manifest": engine_run_manifest,
                "pdx_artifact_manifest": engine_artifact_manifest,
                "source_file_id": run.source_file_id,
                "approval_gate": {
                    "gate_id": run.approval_gate.gate_id,
                    "title": run.approval_gate.title,
                    "risk_level": run.approval_gate.risk_level,
                    "required_role": getattr(run.approval_gate, "required_role", None),
                }
                if run.approval_gate
                else None,
                "artifacts": [
                    {
                        "file_id": schedule_file.file_id,
                        "filename": schedule_file.filename,
                        "sha256": schedule_file.sha256,
                        "size_bytes": schedule_file.size_bytes,
                    },
                    {
                        "file_id": conflict_file.file_id,
                        "filename": conflict_file.filename,
                        "sha256": conflict_file.sha256,
                        "size_bytes": conflict_file.size_bytes,
                    },
                    {
                        "file_id": handoff_file.file_id,
                        "filename": handoff_file.filename,
                        "sha256": handoff_file.sha256,
                        "size_bytes": handoff_file.size_bytes,
                    },
                ],
                "created_at": deterministic_ts,
            }
            manifest_bytes = json.dumps(manifest_data, indent=2, sort_keys=True, default=str).encode("utf-8")
            manifest_file = FileService.upload_file(
                space_id=space_id,
                filename=f"RunManifest_{suffix}.json",
                content=manifest_bytes,
                content_type="application/json",
                user=user,
                project_tags=[tag],
                source_type=FileSourceType.MANIFEST,
                run_id=run.run_id,
                publication_status="pending_approval",
                file_id=f"file_{run.run_id}_manifest",
            )
            created_files.append((manifest_file, manifest_bytes))

            now = datetime.now(UTC)
            for f_rec, raw_data in created_files:
                store.save_artifact_blob(space_id, f_rec.file_id, f_rec.filename, raw_data)
                store.save_artifact_record(
                    ArtifactDescriptor(
                        artifact_id=f_rec.file_id,
                        space_id=space_id,
                        run_id=run.run_id,
                        filename=f_rec.filename,
                        media_type=f_rec.content_type,
                        size_bytes=f_rec.size_bytes,
                        sha256=f_rec.sha256,
                        storage_path=f_rec.storage_path,
                        download_endpoint=f"/v1/spaces/{space_id}/artifacts/{f_rec.file_id}/download",
                        visibility="pending_approval",
                        created_at=now,
                    )
                )
        except Exception as original_err:
            for f, _ in created_files:
                try:
                    FileService.delete_file_permanently(f)
                except Exception:
                    pass
            raise original_err

        pdx_duration_ms = int((time.time() - start_time) * 1000)
        run.output_artifact_ids = artifact_ids
        run.manifest_file_id = manifest_file.file_id
        run.updated_at = datetime.now(UTC)
        run.telemetry.pdx_exec_ms = pdx_duration_ms
        run.telemetry.duration_ms += pdx_duration_ms
        run.telemetry.tool_calls.extend(
            ["pdx_artifact_engine.validate_execution_plan", "pdx_artifact_engine.ArtifactRuntime.execute_plan"]
        )

        store.save_run(run)
        return run
