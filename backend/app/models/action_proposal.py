import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from app.core.config import settings
from pydantic import BaseModel, Field


class ActionExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class DeliverableFormat(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    CSV = "csv"
    JSON = "json"


class ActionSourceDescriptor(BaseModel):
    file_id: str
    active_generation: int = 1
    content_hash: str = ""
    space_id: str = ""


class ArtifactDescriptor(BaseModel):
    artifact_id: str = Field(default_factory=lambda: f"art_{uuid.uuid4().hex[:12]}")
    space_id: str
    run_id: str
    filename: str
    media_type: str = "text/csv"
    size_bytes: int = 0
    sha256: str = ""
    storage_path: str = ""
    download_endpoint: str = ""
    visibility: str = "published"  # "pending_approval", "published", "rejected"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ActionProposal(BaseModel):
    action_id: str
    action_type: str = "create_call_sheet"  # allowlist: create_call_sheet, generate_shot_list, stunt_risk_breakdown, export_production_budget, create_scene_breakdown
    title: str
    description: str
    # None is accepted only when reading a short-lived legacy v1/v2 proposal.
    # Newly issued v3 proposals always resolve and sign an explicit format.
    output_format: DeliverableFormat | None = None
    space_id: str
    project_tag: str
    user_id: str
    sources: List[ActionSourceDescriptor] = Field(default_factory=list)
    source_file_ids: List[str] = Field(default_factory=list)
    expires_at: datetime
    key_id: str = "v3"
    estimated_duration_seconds: int = 10
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ActionConfirmationPayload(BaseModel):
    action_id: str


class DispatchStatus(str, Enum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    DISPATCH_CONFIRMATION_PENDING = "dispatch_confirmation_pending"
    FAILED = "failed"


class ActionExecutionRecord(BaseModel):
    action_id: str
    run_id: str
    space_id: str
    project_tag: str = "general"
    user_id: str
    status: ActionExecutionStatus = ActionExecutionStatus.PENDING
    lease_owner: Optional[str] = None
    lease_token: Optional[str] = None
    lease_until: Optional[datetime] = None
    state_version: int = 1
    attempts: int = 0
    max_attempts: int = 3
    next_retry_at: Optional[datetime] = None
    failure_code: Optional[str] = None
    is_retryable: bool = True
    output_artifact_ids: List[str] = Field(default_factory=list)
    manifest_file_id: Optional[str] = None

    # Dispatch Fencing & Retry Tracking
    dispatch_status: DispatchStatus = DispatchStatus.PENDING
    dispatch_lease_token: Optional[str] = None
    dispatch_lease_until: Optional[datetime] = None
    dispatch_generation: int = 1
    dispatch_attempts: int = 0
    dispatch_version: int = 1
    task_name: Optional[str] = None
    next_dispatch_at: Optional[datetime] = None

    # Canonical Proposal Snapshot & Integrity
    proposal_snapshot: Optional[ActionProposal] = None
    proposal_snapshot_hash: Optional[str] = None
    proposal_verified_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _get_signing_keys() -> Dict[str, bytes]:
    is_prod = getattr(settings, "ENV", "development") in ("production", "prod")
    curr_secret = getattr(settings, "ACTION_SIGNING_SECRET", None)
    prev_secret = getattr(settings, "ACTION_SIGNING_SECRET_PREV", None)

    if is_prod:
        if not curr_secret or len(curr_secret) < 32:
            raise ValueError("SECURITY_FAIL_CLOSED: ACTION_SIGNING_SECRET is missing or insecure in production.")
        keys = {"v3": curr_secret.encode("utf-8"), "v2": curr_secret.encode("utf-8")}
        if prev_secret and len(prev_secret) >= 32:
            keys["v1"] = prev_secret.encode("utf-8")
        return keys

    # Development / Test fallback
    curr_key = (curr_secret or "default-studiotower-action-signing-secret-v2-32chars").encode("utf-8")
    prev_key = (prev_secret or "default-studiotower-action-signing-secret-v1-32chars").encode("utf-8")
    return {
        "v3": curr_key,
        "v2": curr_key,
        "v1": prev_key,
    }


def _compute_canonical_sources_hash(sources: List[ActionSourceDescriptor]) -> str:
    # Deterministically sort by file_id
    sorted_sources = sorted(
        [
            {
                "file_id": s.file_id,
                "active_generation": s.active_generation,
                "content_hash": s.content_hash,
                "space_id": s.space_id,
            }
            for s in sources
        ],
        key=lambda x: x["file_id"],
    )
    raw = json.dumps(sorted_sources, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_canonical_proposal_hash(proposal: ActionProposal) -> str:
    """Computes deterministic SHA-256 integrity hash of an ActionProposal's payload."""
    canonical_dict = {
        "action_id": proposal.action_id,
        "space_id": proposal.space_id,
        "action_type": proposal.action_type,
        "title": proposal.title,
        "description": proposal.description,
        "project_tag": proposal.project_tag,
        "user_id": proposal.user_id,
        "sources": [
            {
                "file_id": s.file_id,
                "active_generation": s.active_generation,
                "content_hash": s.content_hash,
                "space_id": s.space_id,
            }
            for s in sorted(proposal.sources or [], key=lambda x: x.file_id)
        ],
        "metadata": proposal.metadata,
    }
    if proposal.key_id == "v3":
        canonical_dict["output_format"] = proposal.output_format.value if proposal.output_format else None
    raw = json.dumps(canonical_dict, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()



def issue_action_token(
    space_id: str,
    project_tag: str,
    user_id: str,
    title: str,
    description: str,
    sources: List[ActionSourceDescriptor],
    ttl_seconds: int = 300,
    action_type: str = "create_call_sheet",
    output_format: DeliverableFormat | str | None = None,
    metadata: Optional[Dict[str, Any]] = None,
    key_id: str = "v3",
) -> ActionProposal:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    exp_ts = int(expires_at.timestamp())
    nonce = uuid.uuid4().hex

    keys = _get_signing_keys()
    if key_id not in keys:
        key_id = "v3"
    secret = keys[key_id]

    title_hash = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:16]
    desc_hash = hashlib.sha256(description.strip().encode("utf-8")).hexdigest()[:16]
    meta_hash = hashlib.sha256(json.dumps(metadata or {}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    sources_hash = _compute_canonical_sources_hash(sources)

    domain = "studiotower.action.v3"
    legacy_defaults = {
        "create_call_sheet": DeliverableFormat.CSV,
        "generate_shot_list": DeliverableFormat.CSV,
        "stunt_risk_breakdown": DeliverableFormat.JSON,
        "export_production_budget": DeliverableFormat.XLSX,
        "create_scene_breakdown": DeliverableFormat.JSON,
        "generate_pitch_deck": DeliverableFormat.PPTX,
    }
    resolved_format = DeliverableFormat(output_format) if output_format else legacy_defaults.get(action_type, DeliverableFormat.PDF)
    if key_id == "v3":
        domain = "studiotower.action.v3"
        raw_payload = f"{domain}|{nonce}|{space_id}|{project_tag}|{user_id}|{action_type}|{resolved_format.value}|{title_hash}|{desc_hash}|{meta_hash}|{sources_hash}|{exp_ts}|{key_id}"
    else:
        domain = "studiotower.action.v2"
        raw_payload = f"{domain}|{nonce}|{space_id}|{project_tag}|{user_id}|{action_type}|{title_hash}|{desc_hash}|{meta_hash}|{sources_hash}|{exp_ts}|{key_id}"
    full_signature = hmac.new(secret, raw_payload.encode("utf-8"), hashlib.sha256).hexdigest()

    action_id = f"act_{nonce}_{key_id}_{exp_ts}_{full_signature}"

    return ActionProposal(
        action_id=action_id,
        action_type=action_type,
        title=title,
        description=description,
        output_format=resolved_format,
        space_id=space_id,
        project_tag=project_tag,
        user_id=user_id,
        sources=sources,
        source_file_ids=[s.file_id for s in sources],
        expires_at=expires_at,
        key_id=key_id,
        metadata=metadata or {},
    )


def verify_action_token(
    proposal: ActionProposal,
    expected_user_id: str,
    expected_space_id: str,
) -> bool:
    now = datetime.now(UTC)
    if proposal.expires_at <= now:
        return False
    if proposal.user_id != expected_user_id:
        return False
    if proposal.space_id != expected_space_id:
        return False

    parts = proposal.action_id.split("_")
    if len(parts) != 5 or parts[0] != "act":
        return False

    _, nonce, key_id, exp_str, provided_sig = parts
    try:
        exp_ts = int(exp_str)
    except ValueError:
        return False

    # Enforce exact expiry timestamp equality and not expired
    if exp_ts <= int(now.timestamp()):
        return False
    if int(proposal.expires_at.timestamp()) != exp_ts:
        return False

    keys = _get_signing_keys()
    if key_id not in keys:
        return False
    secret = keys[key_id]

    title_hash = hashlib.sha256(proposal.title.strip().encode("utf-8")).hexdigest()[:16]
    desc_hash = hashlib.sha256(proposal.description.strip().encode("utf-8")).hexdigest()[:16]
    meta_hash = hashlib.sha256(json.dumps(proposal.metadata or {}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    sources_hash = _compute_canonical_sources_hash(proposal.sources)

    if key_id == "v3":
        domain = "studiotower.action.v3"
        if proposal.output_format is None:
            return False
        raw_payload = f"{domain}|{nonce}|{proposal.space_id}|{proposal.project_tag}|{proposal.user_id}|{proposal.action_type}|{proposal.output_format.value}|{title_hash}|{desc_hash}|{meta_hash}|{sources_hash}|{exp_ts}|{key_id}"
    else:
        # Backward compatibility for already-issued, short-lived v1/v2 proposals.
        domain = "studiotower.action.v2"
        raw_payload = f"{domain}|{nonce}|{proposal.space_id}|{proposal.project_tag}|{proposal.user_id}|{proposal.action_type}|{title_hash}|{desc_hash}|{meta_hash}|{sources_hash}|{exp_ts}|{key_id}"
    expected_sig = hmac.new(secret, raw_payload.encode("utf-8"), hashlib.sha256).hexdigest()

    return secrets.compare_digest(provided_sig, expected_sig)
