import enum
import uuid
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict


class TelemetryStatus(str, enum.Enum):
    NOT_INSTRUMENTED = "not_instrumented"
    EXPORTING = "exporting"
    AVAILABLE = "available"
    DELAYED = "delayed"
    UNAVAILABLE = "unavailable"


class TelemetryErrorCode(str, enum.Enum):
    PDX_EXECUTION_FAILURE = "PDX_EXECUTION_FAILURE"
    EXCLUSIVE_TECH_LOCK_VIOLATION = "EXCLUSIVE_TECH_LOCK_VIOLATION"
    STORAGE_COMMIT_TIMEOUT = "STORAGE_COMMIT_TIMEOUT"
    APPROVAL_GATE_REJECTED = "APPROVAL_GATE_REJECTED"
    INGESTION_EXTRACTION_FAILURE = "INGESTION_EXTRACTION_FAILURE"
    AI_REASONING_TIMEOUT = "AI_REASONING_TIMEOUT"
    TENANT_ACCESS_DENIED = "TENANT_ACCESS_DENIED"
    DISPATCH_ENQUEUE_FAILURE = "DISPATCH_ENQUEUE_FAILURE"
    STORAGE_CONFLICT = "STORAGE_CONFLICT"
    UNKNOWN_INTERNAL_ERROR = "UNKNOWN_INTERNAL_ERROR"


ALLOWED_TELEMETRY_ERROR_CODES = {e.value for e in TelemetryErrorCode}

DEFAULT_LATENCY_HISTOGRAM_BUCKETS = [25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000]


def get_histogram_bucket_key(duration_ms: int) -> str:
    for b in DEFAULT_LATENCY_HISTOGRAM_BUCKETS:
        if duration_ms <= b:
            return f"le_{b}"
    return "le_inf"


def normalize_failure_code(code: Optional[str]) -> str:
    if not code:
        return TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value
    code_str = str(code).strip()
    if code_str in ALLOWED_TELEMETRY_ERROR_CODES:
        return code_str
    return TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value


ERROR_CODE_SAFE_SUMMARIES: Dict[str, str] = {
    TelemetryErrorCode.PDX_EXECUTION_FAILURE.value: "PDX action execution encountered an unexpected processing failure.",
    TelemetryErrorCode.EXCLUSIVE_TECH_LOCK_VIOLATION.value: "Technical asset lock conflict: resource is exclusively claimed by another operation.",
    TelemetryErrorCode.STORAGE_COMMIT_TIMEOUT.value: "Storage transaction or commit operation exceeded allocated timeout budget.",
    TelemetryErrorCode.APPROVAL_GATE_REJECTED.value: "Human-in-the-loop review gate rejected the pending action.",
    TelemetryErrorCode.INGESTION_EXTRACTION_FAILURE.value: "Document ingestion or chunk extraction encountered a parsing error.",
    TelemetryErrorCode.AI_REASONING_TIMEOUT.value: "LLM reasoning and output generation exceeded timeout limit.",
    TelemetryErrorCode.TENANT_ACCESS_DENIED.value: "Multi-tenant access fence violation: operation denied across tenant boundary.",
    TelemetryErrorCode.DISPATCH_ENQUEUE_FAILURE.value: "Background dispatch queue failed to enqueue Cloud Tasks payload.",
    TelemetryErrorCode.STORAGE_CONFLICT.value: "State version conflict during optimistic concurrency CAS commit.",
    TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value: "An internal operational error occurred during execution.",
}


def get_safe_error_summary(error_code: Optional[str]) -> Optional[str]:
    if not error_code:
        return None
    normalized = normalize_failure_code(error_code)
    return ERROR_CODE_SAFE_SUMMARIES.get(
        normalized,
        ERROR_CODE_SAFE_SUMMARIES[TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value],
    )


# Strict Allowlist of permitted Span Attributes (E0 contract 4)
SPAN_ALLOWED_ATTRIBUTES = {
    "space_id_hash",
    "space_id",
    "event_type",
    "resource_type",
    "resource_id",
    "resource_name",
    "file_id",
    "run_id",
    "artifact_id",
    "action_type",
    "stage",
    "status",
    "duration_ms",
    "error_code",
    "retry_count",
    "model_id",
    "model_family",
    "input_tokens",
    "output_tokens",
    "artifact_count",
    "tenant_key_id",
    "service_name",
}


# Controlled Low-Cardinality Metric Labels (E0 contract 2)
PROMETHEUS_ALLOWED_METRIC_LABELS = {
    "action_type",
    "status",
    "stage",
    "model_family",
}


class SpanWaterfallNode(BaseModel):
    span_id: str
    parent_span_id: Optional[str] = None
    name: str = Field(..., max_length=128)
    service_name: str = Field("studiotower-api", max_length=64)
    start_time_iso: str
    end_time_iso: str
    duration_ms: int
    offset_ms: int
    status: str = "ok"  # "ok", "error", "warning"
    attributes: Dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class RunTraceResponse(BaseModel):
    trace_id: str
    run_id: str
    space_id: str
    service_name: str = "studiotower-api"
    total_spans: int
    spans: List[SpanWaterfallNode] = Field(default_factory=list)
    grafana_dashboard_url: Optional[str] = None
    has_real_telemetry: bool = False


class TelemetryMetricsSummary(BaseModel):
    space_id: str
    project_tag: Optional[str] = None
    time_window_hours: int = 24
    rollup_schema_version: int = 2
    data_status: str = "available"  # "available", "warming_up", "unavailable"
    cost_data_status: str = "unavailable"  # "available", "partial", "unavailable"
    sample_count: int = 0
    total_runs: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    success_rate: float = 0.0
    latency_percentile_method: str = "histogram"
    latency_histogram_buckets: List[int] = Field(default_factory=lambda: list(DEFAULT_LATENCY_HISTOGRAM_BUCKETS))
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_percentile_capped: bool = False
    total_tokens_used: int = 0
    estimated_cost_usd: Optional[float] = None
    pricing_version: Optional[str] = None
    pricing_versions_truncated: bool = False
    currency: str = "USD"
    failures_by_code: Dict[str, int] = Field(default_factory=dict)
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class EvidenceSpanSummary(BaseModel):
    span_id: str
    name: str
    duration_ms: int
    status: str
    error_code: Optional[str] = None
    key_attributes: Dict[str, Any] = Field(default_factory=dict)


class DiagnosisRecord(BaseModel):
    diagnosis_id: str = Field(default_factory=lambda: f"diag_{uuid.uuid4().hex[:12]}")
    space_id: str
    run_id: str
    trace_id: str
    telemetry_generation: int = 1
    schema_version: int = 1
    engine: str = "rule_based"  # "gemini_2_flash", "rule_based"
    model_id: Optional[str] = None
    diagnostic_status: str = "unavailable"  # "complete", "fallback", "no_failure_evidence", "unavailable"
    faulting_span: Optional[EvidenceSpanSummary] = None
    error_code: str = TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value
    error_summary: str = ""
    observations: List[str] = Field(default_factory=list)
    likely_causes: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    evidence_span_ids: List[str] = Field(default_factory=list)
    is_retryable: bool = False
    suggested_action: str = "investigate"  # "retry", "reconfigure", "wait_and_retry", "contact_support"
    confidence: str = "low"  # "low", "medium", "high"
    grafana_dashboard_url: Optional[str] = None
    has_real_telemetry: bool = False
    is_local_diagnostic: bool = False
    mcp_connected: bool = False
    mcp_tools_called: List[str] = Field(default_factory=list)
    mcp_error: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: Optional[datetime] = None


# DiagnosticsResponse alias for backwards compatibility and API serialization
DiagnosticsResponse = DiagnosisRecord


class DiagnosisClaimStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DiagnosisClaimRecord(BaseModel):
    claim_id: str = Field(default_factory=lambda: f"claim_{uuid.uuid4().hex[:12]}")
    space_id: str
    run_id: str
    trace_id: Optional[str] = None
    telemetry_generation: int = 1
    schema_version: int = 1
    status: str = DiagnosisClaimStatus.PENDING.value
    lease_owner: Optional[str] = None
    lease_token: Optional[str] = None
    lease_until: Optional[datetime] = None
    diagnosis_id: Optional[str] = None
    attempts: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SuggestedActionEnum(str, enum.Enum):
    RETRY = "retry"
    WAIT_AND_RETRY = "wait_and_retry"
    RECONFIGURE = "reconfigure"
    INSPECT_CONFIGURATION = "inspect_configuration"
    INVESTIGATE = "investigate"
    CONTACT_SUPPORT = "contact_support"
    ESCALATE_HUMAN_REVIEW = "escalate_human_review"
    NOOP = "noop"


class DiagnosisConfidenceEnum(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class GeminiDiagnosisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    faulting_span_id: str = Field(..., max_length=128)
    error_code: str = Field(..., max_length=64)
    error_summary: str = Field(..., max_length=512)
    observations: List[str] = Field(default_factory=list, max_length=10)
    likely_causes: List[str] = Field(default_factory=list, max_length=10)
    recommendations: List[str] = Field(default_factory=list, max_length=10)
    evidence_span_ids: List[str] = Field(default_factory=list, max_length=20)
    is_retryable: bool = False
    suggested_action: SuggestedActionEnum = SuggestedActionEnum.INVESTIGATE
    confidence: DiagnosisConfidenceEnum = DiagnosisConfidenceEnum.HIGH


class SpaceHourlyMetricRollup(BaseModel):
    """
    Pre-aggregated hourly metric bucket. Keyed by '{space_id}:{project_tag}:{YYYY-MM-DDTHH}'.
    Enforces bounded metric queries (e.g. 24h query reads at most 24 docs, never scanning full Run table).
    Bounded O(1) document size (< 1 KB) using fixed histogram buckets.
    """
    bucket_id: str
    space_id: str
    project_tag: str = "general"
    hour_str: str
    rollup_schema_version: int = 2
    total_runs: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    latency_histogram: Dict[str, int] = Field(default_factory=dict)
    total_tokens: int = 0
    priced_run_count: int = 0
    unpriced_run_count: int = 0
    pricing_versions: List[str] = Field(default_factory=list)
    pricing_versions_truncated: bool = False
    estimated_cost_usd: Optional[float] = None
    failures_by_code: Dict[str, int] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

