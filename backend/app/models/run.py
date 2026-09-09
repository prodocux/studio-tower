import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalGate(BaseModel):
    gate_id: str = Field(default_factory=lambda: f"gate_{uuid.uuid4().hex[:8]}")
    title: str
    description: str
    risk_level: str = "medium"  # low, medium, high, critical
    status: str = "pending"     # pending, approving, approved, rejected
    approved_by: str | None = None
    decided_at: datetime | None = None
    rejection_reason: str | None = None
    decision_lease_token: str | None = None
    decision_by: str | None = None
    decision_started_at: datetime | None = None
    decision_lease_until: datetime | None = None


class RunTelemetry(BaseModel):
    duration_ms: int = 0
    llm_latency_ms: int = 0
    pdx_exec_ms: int = 0
    tokens_used: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    estimated_cost_usd: float | None = None
    pricing_version: str | None = None
    model_id: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    grafana_dashboard_url: str | None = None
    has_real_telemetry: bool = False
    telemetry_status: str = "not_instrumented"  # not_instrumented, exporting, available, delayed, unavailable
    telemetry_generation: int = 1
    telemetry_last_checked_at: datetime | None = None
    telemetry_attempts: int = 0
    telemetry_export_started_at: datetime | None = None
    telemetry_lease_token: str | None = None
    telemetry_lease_until: datetime | None = None
    telemetry_next_check_at: datetime | None = None
    ai_engine: str = "deterministic-fallback"  # "gemini-live" or "deterministic-fallback"


class Run(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    space_id: str
    action_id: str | None = None
    project_tag: str = "general"
    status: RunStatus = RunStatus.PENDING
    trace_id: str = Field(default_factory=lambda: f"trc_{uuid.uuid4().hex}")
    prompt: str = ""
    source_file_id: str | None = None
    scene_breakdown: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    approval_gate: ApprovalGate | None = None
    output_artifact_ids: list[str] = Field(default_factory=list)
    manifest_file_id: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state_version: int = 1
    telemetry: RunTelemetry = Field(default_factory=RunTelemetry)
    telemetry_status: str = "not_instrumented"
    telemetry_generation: int = 1
    telemetry_last_checked_at: datetime | None = None
    telemetry_attempts: int = 0
    telemetry_export_started_at: datetime | None = None
    telemetry_lease_token: str | None = None
    telemetry_lease_until: datetime | None = None
    telemetry_next_check_at: datetime | None = None
    error_summary: str | None = None
    failure_code: str | None = None
    is_retryable: bool = False
    agent_diagnosis: str | None = None
    latest_diagnosis_id: str | None = None
    latest_diagnosis_status: str | None = None
    diagnosis_revision: int = 0
    approval_commit_status: str | None = None
    uncertain_since: datetime | None = None
