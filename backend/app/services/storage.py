import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.models.action_proposal import ActionExecutionRecord, ActionExecutionStatus, ArtifactDescriptor, DispatchStatus
from app.models.activity import ActivityEvent, ActivityEventType, ActivityOutboxItem, OutboxStatus
from app.models.file_record import DocumentChunk, FileRecord, IngestionStatus
from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
from app.models.message import Message
from app.models.run import Run, RunStatus
from app.models.space import Invite, MembershipRole, ProjectTag, Space, SpaceKind
from app.models.telemetry import DiagnosisRecord, SpaceHourlyMetricRollup, DiagnosisClaimRecord, DiagnosisClaimStatus
from app.models.user import User
from app.core.config import settings
from app.core.space_metrics import record_space_activity

from enum import Enum
from pydantic import BaseModel, Field

logger = logging.getLogger("studiotower.storage")


class SandboxCleanupPhase(str, Enum):
    PENDING = "pending"
    DELETING_BLOBS = "deleting_blobs"
    DELETING_CHILDREN = "deleting_children"
    VERIFYING_EMPTY = "verifying_empty"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


class SandboxCleanupRecord(BaseModel):
    cleanup_job_id: str
    cleanup_version: int = 1
    space_id: str
    lease_owner: Optional[str] = None
    lease_token: Optional[str] = None
    lease_until: Optional[datetime] = None
    current_phase: SandboxCleanupPhase = SandboxCleanupPhase.PENDING
    current_collection_index: int = 0
    collection_cursor: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 5
    next_retry_at: Optional[datetime] = None
    last_error_code: Optional[str] = None
    cascaded_counts: Dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: Optional[datetime] = None


def encode_activity_cursor(payload: dict) -> str:
    from app.core.config import settings
    secret = (getattr(settings, "CURSOR_SIGNING_SECRET", None) or "default-dev-cursor-secret-key-32-chars-long").encode("utf-8")
    raw_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, raw_bytes, hashlib.sha256).digest()
    b64_payload = base64.urlsafe_b64encode(raw_bytes).decode("utf-8").rstrip("=")
    b64_sig = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    return f"{b64_payload}.{b64_sig}"


def decode_activity_cursor(cursor_str: str, expected_space_id: str, expected_tag: Optional[str] = None) -> dict:
    from app.core.config import settings
    from app.models.activity import InvalidCursorError
    if not cursor_str:
        raise InvalidCursorError("EMPTY_CURSOR")
    if "." not in cursor_str:
        if getattr(settings, "ENV", "development") in ("production", "prod"):
            raise InvalidCursorError("UNSIGNED_CURSOR_FORBIDDEN: Unsigned plain cursor tokens are strictly forbidden in production.")
        # Fallback for plain event ID during legacy dev tests
        return {"event_id": cursor_str, "space_id": expected_space_id, "tag": expected_tag or ""}
    parts = cursor_str.split(".")
    if len(parts) != 2:
        raise InvalidCursorError("INVALID_CURSOR_FORMAT")
    b64_payload, b64_sig = parts[0], parts[1]
    pad_payload = b64_payload + "=" * ((4 - len(b64_payload) % 4) % 4)
    pad_sig = b64_sig + "=" * ((4 - len(b64_sig) % 4) % 4)
    try:
        raw_bytes = base64.urlsafe_b64decode(pad_payload)
        sig = base64.urlsafe_b64decode(pad_sig)
    except Exception:
        raise InvalidCursorError("TAMPERED_CURSOR_SIGNATURE")

    secret = (getattr(settings, "CURSOR_SIGNING_SECRET", None) or "default-dev-cursor-secret-key-32-chars-long").encode("utf-8")
    expected_sig = hmac.new(secret, raw_bytes, hashlib.sha256).digest()
    if not secrets.compare_digest(sig, expected_sig):
        raise InvalidCursorError("TAMPERED_CURSOR_SIGNATURE")

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        raise InvalidCursorError("MALFORMED_CURSOR_PAYLOAD")

    if not isinstance(payload, dict):
        raise InvalidCursorError("MALFORMED_CURSOR_PAYLOAD")
    if payload.get("space_id") != expected_space_id:
        raise InvalidCursorError("CURSOR_SPACE_MISMATCH")
    cursor_tag = payload.get("tag") or ""
    req_tag = expected_tag or ""
    if cursor_tag and req_tag and cursor_tag != req_tag and cursor_tag != "all" and req_tag != "all":
        raise InvalidCursorError("CURSOR_TAG_MISMATCH")
    return payload


def encode_message_cursor(payload: dict) -> str:
    from app.core.config import settings
    secret = (getattr(settings, "CURSOR_SIGNING_SECRET", None) or "default-dev-cursor-secret-key-32-chars-long").encode("utf-8")
    raw_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, raw_bytes, hashlib.sha256).digest()
    b64_payload = base64.urlsafe_b64encode(raw_bytes).decode("utf-8").rstrip("=")
    b64_sig = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    return f"{b64_payload}.{b64_sig}"


def decode_message_cursor(cursor_str: str, expected_space_id: str, expected_tag: Optional[str] = None) -> dict:
    from app.core.config import settings
    from app.models.activity import InvalidCursorError
    if not cursor_str:
        raise InvalidCursorError("EMPTY_CURSOR")
    if "." not in cursor_str:
        if getattr(settings, "ENV", "development") in ("production", "prod"):
            raise InvalidCursorError("UNSIGNED_CURSOR_FORBIDDEN: Unsigned plain cursor tokens are strictly forbidden in production.")
        return {"message_id": cursor_str, "space_id": expected_space_id, "tag": expected_tag or ""}
    parts = cursor_str.split(".")
    if len(parts) != 2:
        raise InvalidCursorError("INVALID_CURSOR_FORMAT")
    b64_payload, b64_sig = parts[0], parts[1]
    pad_payload = b64_payload + "=" * ((4 - len(b64_payload) % 4) % 4)
    pad_sig = b64_sig + "=" * ((4 - len(b64_sig) % 4) % 4)
    try:
        raw_bytes = base64.urlsafe_b64decode(pad_payload)
        sig = base64.urlsafe_b64decode(pad_sig)
    except Exception:
        raise InvalidCursorError("TAMPERED_CURSOR_SIGNATURE")

    secret = (getattr(settings, "CURSOR_SIGNING_SECRET", None) or "default-dev-cursor-secret-key-32-chars-long").encode("utf-8")
    expected_sig = hmac.new(secret, raw_bytes, hashlib.sha256).digest()
    if not secrets.compare_digest(sig, expected_sig):
        raise InvalidCursorError("TAMPERED_CURSOR_SIGNATURE")

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        raise InvalidCursorError("MALFORMED_CURSOR_PAYLOAD")

    if not isinstance(payload, dict):
        raise InvalidCursorError("MALFORMED_CURSOR_PAYLOAD")
    if payload.get("space_id") != expected_space_id:
        raise InvalidCursorError("CURSOR_SPACE_MISMATCH")
    cursor_tag = payload.get("tag") or ""
    req_tag = expected_tag or ""
    if cursor_tag and req_tag and cursor_tag != req_tag and cursor_tag != "all" and req_tag != "all":
        raise InvalidCursorError("CURSOR_TAG_MISMATCH")
    return payload


def _parse_lease_timestamp_fail_closed(ts_val: Any) -> Optional[datetime]:
    if ts_val is None:
        return None
    if isinstance(ts_val, datetime):
        if ts_val.tzinfo is None:
            return ts_val.replace(tzinfo=UTC)
        return ts_val
    if isinstance(ts_val, str):
        try:
            return datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
        except Exception:
            raise ValueError(f"MALFORMED_LEASE_TIMESTAMP: {ts_val}")
    if hasattr(ts_val, "to_datetime"):
        try:
            dt = ts_val.to_datetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            raise ValueError(f"MALFORMED_FIRESTORE_TIMESTAMP: {ts_val}")
    raise ValueError(f"UNRECOGNIZED_TIMESTAMP_TYPE: {type(ts_val)}")


ROLE_PRECEDENCE = {
    MembershipRole.MEMBER: 1,
    MembershipRole.COORDINATOR: 2,
    MembershipRole.ADMIN: 3,
    MembershipRole.OWNER: 4,
}


class StorageConflictError(Exception):
    """Raised when an atomic CAS transition or Firestore transaction encounters a concurrency conflict."""
    pass


class StorageUnavailableError(Exception):
    """Raised when the persistent database backend is temporarily unavailable."""
    pass


class CleanupAuthorizationError(Exception):
    """Raised when delete_generation_document_chunks cannot acquire cleanup authorization.
    Distinguishes a backend error or protected-skip from a normal zero-chunk successful delete,
    so the sweeper never marks a failed authorization as cleaned.
    """
    pass


@contextmanager
def file_lock(lock_path: Optional[str], timeout: float = 5.0):
    """
    Cross-process file-based lock with timeout.
    Strictly raises TimeoutError if lock cannot be acquired within timeout.
    """
    if not lock_path:
        yield True
        return

    start_time = time.time()
    lock_file = f"{lock_path}.lock"
    acquired = False
    while time.time() - start_time < timeout:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.02)
        except Exception:
            break

    if not acquired:
        raise TimeoutError(f"Failed to acquire cross-process file lock on '{lock_path}' within {timeout}s")

    try:
        yield True
    finally:
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass


class ArtifactCleanupRegistry(dict):
    """
    Stores cleanup jobs strictly under canonical IDs (f"cleanup:{art_id}:{ver}").
    Provides transparent read/migration resolution for legacy job IDs (f"clean_art_{art_id}")
    without creating duplicate executable jobs.
    """
    def get(self, key, default=None):
        if key in self:
            return super().get(key, default)
        if isinstance(key, str) and key.startswith("clean_art_"):
            target_aid = key[len("clean_art_"):]
            matches = [v for v in self.values() if isinstance(v, dict) and v.get("artifact_id") == target_aid]
            if len(matches) == 1:
                return matches[0]
            # Multiple matches: fail closed (ambiguous) to prevent operating on wrong version
            return default
        return default

    def __getitem__(self, key):
        res = self.get(key)
        if res is None and key not in self:
            raise KeyError(key)
        return res


class MemoryStore:
    """
    Thread-safe and multi-process safe data store supporting in-memory and atomic local disk persistence.
    """

    def __init__(self, persist_path: Optional[str] = None):
        self.persist_path = persist_path
        self._lock = threading.RLock()
        self.users: Dict[str, User] = {}
        self.spaces: Dict[str, Space] = {}
        # (space_id, uid) -> MembershipRole
        self.memberships: Dict[Tuple[str, str], MembershipRole] = {}
        self.invites: Dict[str, Invite] = {}            # token -> Invite
        self.messages: Dict[str, List[Message]] = {}    # space_id -> list[Message]
        self.files: Dict[str, FileRecord] = {}          # file_id -> FileRecord
        self.file_blobs: Dict[str, bytes] = {}          # file_id -> bytes content
        self.runs: Dict[str, Run] = {}                  # run_id -> Run
        self.chat_idempotency: Dict[str, ChatIdempotencyRecord] = {}  # key -> ChatIdempotencyRecord
        self.document_chunks: Dict[Tuple[str, int], List[DocumentChunk]] = {}  # (file_id, generation) -> list[DocumentChunk]
        self.pending_generation_cleanups: Dict[str, Dict[str, Any]] = {}  # cleanup_id -> cleanup record dict
        self.activity_events: Dict[str, List[ActivityEvent]] = {}  # space_id -> list[ActivityEvent]
        self.activity_outbox: Dict[str, ActivityOutboxItem] = {}  # outbox_id -> ActivityOutboxItem
        self.action_executions: Dict[str, ActionExecutionRecord] = {}  # action_id -> ActionExecutionRecord
        self.artifacts: Dict[str, ArtifactDescriptor] = {}             # artifact_id -> ArtifactDescriptor
        self.artifact_blobs: Dict[Tuple[str, str], bytes] = {}         # (space_id, artifact_id) -> bytes
        self.artifact_cleanups: Dict[str, Dict[str, Any]] = ArtifactCleanupRegistry()         # job_id -> cleanup record dict
        self._telemetry_scan_cursors: Dict[str, Dict[str, Any]] = {}
        self._metric_rollups: Dict[str, Dict[str, Any]] = {}
        self._diagnoses: Dict[str, Dict[str, Any]] = {}
        self._run_diagnoses: Dict[Tuple[str, str], str] = {}
        self._diagnosis_claims: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._sliding_rate_limits: Dict[str, List[float]] = {}

        if self.persist_path and os.path.exists(self.persist_path):
            self._load_from_disk()

    def _sync_read_latest_from_disk(self):
        """Reads fresh state from disk and replaces local state (no stale key resurrection)."""
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.users = {k: User.model_validate(v) for k, v in state.get("users", {}).items()}
            self.spaces = {k: Space.model_validate(v) for k, v in state.get("spaces", {}).items()}
            self.memberships = {(m[0], m[1]): MembershipRole(m[2]) for m in state.get("memberships", [])}
            self.invites = {k: Invite.model_validate(v) for k, v in state.get("invites", {}).items()}
            self.messages = {k: [Message.model_validate(m) for m in v] for k, v in state.get("messages", {}).items()}
            self.files = {k: FileRecord.model_validate(v) for k, v in state.get("files", {}).items()}
            self.runs = {k: Run.model_validate(v) for k, v in state.get("runs", {}).items()}
            self.chat_idempotency = {k: ChatIdempotencyRecord.model_validate(v) for k, v in state.get("chat_idempotency", {}).items()}
            self.document_chunks = {
                (k.split(":")[0], int(k.split(":")[1])): [DocumentChunk.model_validate(c) for c in v]
                for k, v in state.get("document_chunks", {}).items()
            }
            self.activity_events = {
                k: [ActivityEvent.model_validate(e) for e in v]
                for k, v in state.get("activity_events", {}).items()
            }
            self.activity_outbox = {
                k: ActivityOutboxItem.model_validate(v)
                for k, v in state.get("activity_outbox", {}).items()
            }
            self.action_executions = {
                k: ActionExecutionRecord.model_validate(v)
                for k, v in state.get("action_executions", {}).items()
            }
            self.artifacts = {
                k: ArtifactDescriptor.model_validate(v)
                for k, v in state.get("artifacts", {}).items()
            }
            self._telemetry_scan_cursors = state.get("telemetry_scan_cursors", {})
            self._metric_rollups = state.get("metric_rollups", {})
        except Exception as e:
            raise RuntimeError(
                f"Corrupted storage state at '{self.persist_path}': {e}. Cannot safely load database."
            )

    def _mutate_disk_state(self, mutator_fn: Callable[[], None]):
        """
        Executes a state mutation under both thread lock and process file lock.
        Syncs latest state from disk, applies mutation, and atomic-replaces disk state.
        """
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            mutator_fn()
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.to_storage_dict() for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                    "chat_idempotency": {k: v.model_dump(mode="json") for k, v in self.chat_idempotency.items()},
                    "document_chunks": {
                        f"{k[0]}:{k[1]}": [c.model_dump(mode="json") for c in v]
                        for k, v in self.document_chunks.items()
                    },
                    "activity_events": {
                        k: [e.model_dump(mode="json") for e in v]
                        for k, v in self.activity_events.items()
                    },
                    "activity_outbox": {
                        k: v.model_dump(mode="json")
                        for k, v in self.activity_outbox.items()
                    },
                    "action_executions": {
                        k: v.model_dump(mode="json")
                        for k, v in self.action_executions.items()
                    },
                    "artifacts": {
                        k: v.model_dump(mode="json")
                        for k, v in self.artifacts.items()
                    },
                    "telemetry_scan_cursors": self._telemetry_scan_cursors,
                    "metric_rollups": self._metric_rollups,
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)

                os.replace(tmp_path, self.persist_path)

    def _load_from_disk(self):
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()

    def save_file_blob(self, file_id: str, content: bytes) -> None:
        with self._lock:
            self.file_blobs[file_id] = content

    def get_file_blob(self, file_id: str) -> Optional[bytes]:
        with self._lock:
            return self.file_blobs.get(file_id)

    # User operations
    def save_user(self, user: User) -> User:
        def mutate():
            self.users[user.uid] = user
        self._mutate_disk_state(mutate)
        return user

    def get_user(self, uid: str) -> Optional[User]:
        with self._lock:
            return self.users.get(uid)

    def get_user_by_email(self, email: str) -> Optional[User]:
        clean_email = email.strip().lower()
        with self._lock:
            for u in self.users.values():
                if u.email.strip().lower() == clean_email:
                    return u
            return None

    def search_users(self, query: str, limit: int = 10) -> List[User]:
        q = query.strip().lower()
        if not q:
            return []
        with self._lock:
            results = []
            for u in self.users.values():
                if q in u.email.lower() or q in u.display_name.lower() or q in u.uid.lower():
                    results.append(u)
                    if len(results) >= limit:
                        break
            return results

    # Space operations
    def create_space(self, space: Space, creator_uid: str) -> Space:
        def mutate():
            self.spaces[space.space_id] = space
            self.memberships[(space.space_id, creator_uid)] = MembershipRole.OWNER
            if space.space_id not in self.messages:
                self.messages[space.space_id] = []
        self._mutate_disk_state(mutate)
        return space

    def get_space(self, space_id: str) -> Optional[Space]:
        with self._lock:
            return self.spaces.get(space_id)

    def list_spaces_for_user(self, uid: str) -> List[Space]:
        with self._lock:
            user_space_ids = {s_id for (s_id, u_id) in self.memberships.keys() if u_id == uid}
            return [self.spaces[s_id] for s_id in user_space_ids if s_id in self.spaces]

    def is_member(self, space_id: str, uid: str) -> bool:
        with self._lock:
            return (space_id, uid) in self.memberships

    def get_member_role(self, space_id: str, uid: str) -> Optional[MembershipRole]:
        with self._lock:
            return self.memberships.get((space_id, uid))

    def count_space_members_by_role(self, space_id: str) -> Dict[MembershipRole, int]:
        with self._lock:
            counts = {role: 0 for role in MembershipRole}
            for (s_id, _), role in self.memberships.items():
                if s_id == space_id:
                    counts[role] = counts.get(role, 0) + 1
            return counts

    def add_member(self, space_id: str, uid: str, role: MembershipRole = MembershipRole.MEMBER) -> None:
        def mutate():
            self.memberships[(space_id, uid)] = role
        self._mutate_disk_state(mutate)

    def list_members_in_space(self, space_id: str) -> List[Tuple[str, MembershipRole]]:
        with self._lock:
            return [(uid, role) for (s_id, uid), role in self.memberships.items() if s_id == space_id]

    def list_invites_for_space(self, space_id: str) -> List[Invite]:
        now = datetime.now(UTC)
        with self._lock:
            return [
                inv for inv in self.invites.values()
                if inv.space_id == space_id
                and inv.revoked_at is None
                and (not inv.expires_at or inv.expires_at > now)
                and inv.used_count < inv.max_uses
            ]

    def transfer_ownership_atomic(self, space_id: str, expected_current_owner_uid: str, new_owner_uid: str) -> bool:
        with self._lock:
            current_role = self.memberships.get((space_id, expected_current_owner_uid))
            if current_role != MembershipRole.OWNER:
                raise ValueError("CALLER_NOT_CURRENT_OWNER")
            if (space_id, new_owner_uid) not in self.memberships:
                raise ValueError("NEW_OWNER_NOT_MEMBER")

            def mutate():
                self.memberships[(space_id, new_owner_uid)] = MembershipRole.OWNER
                self.memberships[(space_id, expected_current_owner_uid)] = MembershipRole.ADMIN
            self._mutate_disk_state(mutate)
            return True

    def remove_member(self, space_id: str, uid: str) -> bool:
        removed = False
        def mutate():
            nonlocal removed
            if (space_id, uid) in self.memberships:
                del self.memberships[(space_id, uid)]
                removed = True
        self._mutate_disk_state(mutate)
        return removed

    def get_agent_dm_for_user(self, uid: str) -> Optional[Space]:
        with self._lock:
            for space in self.list_spaces_for_user(uid):
                if space.kind == SpaceKind.AGENT_DM and space.created_by == uid:
                    return space
            return None

    # Tag operations
    def add_tag_to_space(self, space_id: str, tag: ProjectTag, actor_uid: Optional[str] = None) -> Optional[Space]:
        space = None
        now = datetime.now(UTC)
        def mutate():
            nonlocal space
            s = self.spaces.get(space_id)
            if not s:
                return
            tag.revision = getattr(tag, "revision", 1) or 1
            tag.updated_at = now
            existing_idx = next((i for i, t in enumerate(s.tags) if t.slug == tag.slug or t.id == tag.id), None)
            if existing_idx is not None:
                tag.revision = (getattr(s.tags[existing_idx], "revision", 1) or 1) + 1
                s.tags[existing_idx] = tag
                evt_type = ActivityEventType.TAG_UPDATED
            else:
                s.tags.append(tag)
                evt_type = ActivityEventType.TAG_CREATED
            space = s

            # Atomic transactional outbox & activity staging in the same disk state mutation
            ev_id = f"{evt_type.value}:{tag.id}:r{tag.revision}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=evt_type,
                space_id=space_id,
                project_tags=[tag.slug],
                resource_type="tag",
                resource_id=tag.id,
                summary=f"Tag '{tag.name}' {'updated' if existing_idx is not None else 'created'}",
                details={"tag_id": tag.id, "slug": tag.slug, "name": tag.name, "revision": tag.revision},
                actor_uid=actor_uid,
                created_at=now,
            )
            if space_id not in self.activity_events:
                self.activity_events[space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[space_id]):
                self.activity_events[space_id].append(ev)
            outbox_id = f"outbox:{ev.event_id}"
            self.activity_outbox[outbox_id] = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
        self._mutate_disk_state(mutate)
        return space

    def update_tag_in_space(
        self,
        space_id: str,
        tag_id_or_slug: str,
        name: Optional[str] = None,
        color: Optional[str] = None,
        description: Optional[str] = None,
        actor_uid: Optional[str] = None,
    ) -> Optional[Space]:
        space = None
        now = datetime.now(UTC)
        def mutate():
            nonlocal space
            s = self.spaces.get(space_id)
            if not s:
                return
            target_tag = next((t for t in s.tags if t.id == tag_id_or_slug or t.slug == tag_id_or_slug), None)
            if not target_tag:
                return
            if name is not None:
                target_tag.name = name
            if color is not None:
                target_tag.color = color
            if description is not None:
                target_tag.description = description
            target_tag.revision = (getattr(target_tag, "revision", 1) or 1) + 1
            target_tag.updated_at = now
            space = s

            # Atomic transactional outbox & activity staging
            ev_id = f"tag.updated:{target_tag.id}:r{target_tag.revision}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=ActivityEventType.TAG_UPDATED,
                space_id=space_id,
                project_tags=[target_tag.slug],
                resource_type="tag",
                resource_id=target_tag.id,
                summary=f"Tag '{target_tag.name}' updated",
                details={"tag_id": target_tag.id, "slug": target_tag.slug, "name": target_tag.name, "revision": target_tag.revision},
                actor_uid=actor_uid,
                created_at=now,
            )
            if space_id not in self.activity_events:
                self.activity_events[space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[space_id]):
                self.activity_events[space_id].append(ev)
            outbox_id = f"outbox:{ev.event_id}"
            self.activity_outbox[outbox_id] = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
        self._mutate_disk_state(mutate)
        return space

    def archive_tag_in_space(
        self, space_id: str, tag_id_or_slug: str, archived: bool = True, actor_uid: Optional[str] = None
    ) -> Optional[Space]:
        space = None
        now = datetime.now(UTC)
        def mutate():
            nonlocal space
            s = self.spaces.get(space_id)
            if not s:
                return
            target_tag = next((t for t in s.tags if t.id == tag_id_or_slug or t.slug == tag_id_or_slug), None)
            if not target_tag:
                return
            target_tag.archived = archived
            target_tag.revision = (getattr(target_tag, "revision", 1) or 1) + 1
            target_tag.updated_at = now
            space = s

            # Atomic transactional outbox & activity staging
            evt_type = ActivityEventType.TAG_ARCHIVED if archived else ActivityEventType.TAG_UNARCHIVED
            ev_id = f"{evt_type.value}:{target_tag.id}:r{target_tag.revision}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=evt_type,
                space_id=space_id,
                project_tags=[target_tag.slug],
                resource_type="tag",
                resource_id=target_tag.id,
                summary=f"Tag '{target_tag.name}' {'archived' if archived else 'restored'}",
                details={"tag_id": target_tag.id, "slug": target_tag.slug, "archived": archived, "revision": target_tag.revision},
                actor_uid=actor_uid,
                created_at=now,
            )
            if space_id not in self.activity_events:
                self.activity_events[space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[space_id]):
                self.activity_events[space_id].append(ev)
            outbox_id = f"outbox:{ev.event_id}"
            self.activity_outbox[outbox_id] = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
        self._mutate_disk_state(mutate)
        return space

    # Activity & Outbox operations
    def record_activity_event(self, event: ActivityEvent) -> ActivityEvent:
        res = event
        created = False
        def mutate():
            nonlocal res, created
            if event.space_id not in self.activity_events:
                self.activity_events[event.space_id] = []
            existing_ev = next((e for e in self.activity_events[event.space_id] if e.event_id == event.event_id), None)
            if existing_ev:
                if (
                    existing_ev.event_type == event.event_type
                    and existing_ev.resource_id == event.resource_id
                    and existing_ev.space_id == event.space_id
                ):
                    res = existing_ev
                    return
                raise ValueError(
                    f"ACTIVITY_EVENT_ID_COLLISION: Event ID '{event.event_id}' already exists with differing payload"
                )
            self.activity_events[event.space_id].append(event)
            created = True
        self._mutate_disk_state(mutate)
        if created:
            record_space_activity(event)
        return res

    def stage_outbox_event(self, event: ActivityEvent) -> ActivityOutboxItem:
        outbox_item = ActivityOutboxItem(
            event_id=event.event_id,
            space_id=event.space_id,
            event=event,
            status=OutboxStatus.PENDING,
        )
        def mutate():
            self.activity_outbox[outbox_item.outbox_id] = outbox_item
        self._mutate_disk_state(mutate)
        return outbox_item

    def dispatch_outbox_events(self, limit: int = 50, worker_id: str = "local_dispatcher") -> int:
        now = datetime.now(UTC)
        dispatched_count = 0
        candidates = []
        tokens = {}

        def claim_mutate():
            nonlocal candidates, tokens
            for item in self.activity_outbox.values():
                if item.status == OutboxStatus.PUBLISHED or item.status == OutboxStatus.FAILED:
                    continue
                if getattr(item, "lease_until", None) and item.lease_until > now and getattr(item, "status", None) == "in_progress":
                    continue
                if getattr(item, "next_retry_at", None) and item.next_retry_at > now:
                    continue
                candidates.append(item)
                if len(candidates) >= limit:
                    break

            for item in candidates:
                tok = uuid.uuid4().hex
                tokens[item.outbox_id] = tok
                item.status = "in_progress"
                item.lease_owner = worker_id
                item.lease_token = tok
                item.lease_until = now + timedelta(seconds=30)
                item.attempts += 1
                item.updated_at = now

        self._mutate_disk_state(claim_mutate)

        for item in candidates:
            tok = tokens.get(item.outbox_id)
            try:
                self.record_activity_event(item.event)

                def publish_mutate():
                    nonlocal dispatched_count
                    curr = self.activity_outbox.get(item.outbox_id)
                    if (
                        curr
                        and curr.status == OutboxStatus.IN_PROGRESS
                        and curr.lease_owner == worker_id
                        and getattr(curr, "lease_token", None) == tok
                        and curr.lease_until is not None
                        and curr.lease_until > datetime.now(UTC)
                    ):
                        curr.status = OutboxStatus.PUBLISHED
                        curr.lease_owner = None
                        curr.lease_until = None
                        curr.lease_token = None
                        curr.updated_at = datetime.now(UTC)
                        dispatched_count += 1

                self._mutate_disk_state(publish_mutate)
            except Exception as e:
                def fail_mutate():
                    curr = self.activity_outbox.get(item.outbox_id)
                    if (
                        curr
                        and curr.status == OutboxStatus.IN_PROGRESS
                        and curr.lease_owner == worker_id
                        and getattr(curr, "lease_token", None) == tok
                        and curr.lease_until is not None
                        and curr.lease_until > datetime.now(UTC)
                    ):
                        is_failed = curr.attempts >= curr.max_attempts
                        backoff_delay = (2 ** min(curr.attempts, 6)) * 5
                        curr.status = OutboxStatus.FAILED if is_failed else OutboxStatus.PENDING
                        curr.last_error = str(e)
                        curr.lease_owner = None
                        curr.lease_until = None
                        curr.lease_token = None
                        curr.next_retry_at = datetime.now(UTC) + timedelta(seconds=backoff_delay)
                        curr.updated_at = datetime.now(UTC)

                self._mutate_disk_state(fail_mutate)

        return dispatched_count

    def list_activity_events(
        self,
        space_id: str,
        tag: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[List[ActivityEvent], Optional[str]]:
        with self._lock:
            events = self.activity_events.get(space_id, [])
            if tag and tag != "all":
                events = [
                    e for e in events
                    if (tag in getattr(e, "project_tags", []) or e.project_tag == tag)
                ]

            # Sort newest first by created_at descending, event_id descending
            sorted_events = sorted(events, key=lambda e: (e.created_at, e.event_id), reverse=True)

            start_idx = 0
            if cursor:
                cursor_payload = decode_activity_cursor(cursor, space_id, tag)
                c_event_id = cursor_payload.get("event_id")
                c_created_at_str = cursor_payload.get("created_at")
                for i, ev in enumerate(sorted_events):
                    if ev.event_id == c_event_id or (c_created_at_str and ev.created_at.isoformat() == c_created_at_str and ev.event_id == c_event_id):
                        start_idx = i + 1
                        break

            page = sorted_events[start_idx : start_idx + limit]
            next_cursor = None
            if start_idx + limit < len(sorted_events):
                last_ev = page[-1]
                next_cursor = encode_activity_cursor({
                    "created_at": last_ev.created_at.isoformat(),
                    "event_id": last_ev.event_id,
                    "space_id": space_id,
                    "tag": tag or "",
                })

            return page, next_cursor

    # Invite operations
    def create_invite(self, invite: Invite) -> Invite:
        def mutate():
            self.invites[invite.token] = invite
        self._mutate_disk_state(mutate)
        return invite

    def get_invite(self, token: str) -> Optional[Invite]:
        with self._lock:
            return self.invites.get(token)

    def revoke_invite(self, token: str) -> Optional[Invite]:
        invite = None
        def mutate():
            nonlocal invite
            inv = self.invites.get(token)
            if inv:
                inv.revoked_at = datetime.now(UTC)
                invite = inv
        self._mutate_disk_state(mutate)
        return invite

    def accept_invite_and_join(self, token: str, user: User) -> Space:
        """
        Atomically validates invite, consumes usage, and assigns membership with role precedence
        under single unified file lock critical section.
        """
        target_space = None
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()

            invite = self.invites.get(token)
            if not invite:
                raise ValueError("INVALID_INVITE")
            if invite.revoked_at is not None or invite.used_count >= invite.max_uses:
                raise ValueError("REVOKED_OR_EXHAUSTED")
            if invite.expires_at and invite.expires_at < datetime.now(UTC):
                raise ValueError("EXPIRED")
            if invite.target_email and user.email.lower().strip() != invite.target_email:
                raise ValueError("EMAIL_MISMATCH")

            space = self.get_space(invite.space_id)
            if not space:
                raise ValueError("SPACE_NOT_FOUND")

            invite.used_count += 1
            if invite.used_count >= invite.max_uses:
                invite.revoked_at = datetime.now(UTC)

            # Role precedence check
            existing_role = self.memberships.get((space.space_id, user.uid))
            if not existing_role or ROLE_PRECEDENCE.get(invite.role, 0) > ROLE_PRECEDENCE.get(existing_role, 0):
                self.memberships[(space.space_id, user.uid)] = invite.role

            # Atomic disk save under existing lock
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                os.replace(tmp_path, self.persist_path)

            target_space = space

        return target_space

    # Message operations
    def add_message(self, message: Message) -> Message:
        now = datetime.now(UTC)
        def mutate():
            space_msgs = self.messages.setdefault(message.space_id, [])
            for existing in space_msgs:
                if existing.message_id == message.message_id:
                    # Idempotent insert-if-absent: avoid duplicate append in space message list
                    return
            space_msgs.append(message)

            # Atomic transactional outbox & activity staging in the same disk mutation
            tag = message.project_tag or "general"
            sender = message.sender_name or message.sender_uid or "User"
            preview = message.content[:60] + "..." if len(message.content) > 60 else message.content
            summary = f"Message sent by {sender}: {preview}"
            ev_id = f"message.created:{message.message_id}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=ActivityEventType.MESSAGE_CREATED,
                space_id=message.space_id,
                project_tags=[tag],
                resource_type="message",
                resource_id=message.message_id,
                summary=summary,
                details={
                    "message_id": message.message_id,
                    "sender_uid": message.sender_uid,
                    "sender_name": message.sender_name,
                    "role": message.role.value if hasattr(message.role, "value") else str(message.role),
                    "content_preview": preview,
                    "attachment_count": len(getattr(message, "attachment_file_ids", []) or []),
                },
                actor_uid=message.sender_uid,
                created_at=now,
            )
            if message.space_id not in self.activity_events:
                self.activity_events[message.space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[message.space_id]):
                self.activity_events[message.space_id].append(ev)
            outbox_id = f"outbox:{ev.event_id}"
            self.activity_outbox[outbox_id] = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=message.space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
        self._mutate_disk_state(mutate)
        return message

    def list_messages(self, space_id: str, project_tag: Optional[str] = None) -> List[Message]:
        with self._lock:
            msgs = self.messages.get(space_id, [])
            if project_tag and project_tag != "all":
                res = [m for m in msgs if m.project_tag == project_tag]
            else:
                res = list(msgs)
            return sorted(res, key=lambda m: (m.created_at or datetime.min.replace(tzinfo=UTC), m.message_id))

    def list_messages_page(
        self,
        space_id: str,
        project_tag: Optional[str] = None,
        limit: int = 30,
        before_cursor: Optional[str] = None,
    ) -> Tuple[List[Message], Optional[str], bool]:
        with self._lock:
            msgs = self.messages.get(space_id, [])
            if project_tag and project_tag != "all":
                filtered = [m for m in msgs if m.project_tag == project_tag]
            else:
                filtered = list(msgs)

            # Sort descending: (created_at DESC, message_id DESC)
            desc_sorted = sorted(
                filtered,
                key=lambda m: (m.created_at or datetime.min.replace(tzinfo=UTC), m.message_id),
                reverse=True,
            )

            start_idx = 0
            if before_cursor:
                cursor_payload = decode_message_cursor(before_cursor, space_id, project_tag)
                c_msg_id = cursor_payload.get("message_id")
                c_created_at_str = cursor_payload.get("created_at")
                c_dt = datetime.fromisoformat(c_created_at_str) if c_created_at_str else None

                for idx, m in enumerate(desc_sorted):
                    m_dt = m.created_at or datetime.min.replace(tzinfo=UTC)
                    if c_dt:
                        if m_dt < c_dt or (m_dt == c_dt and m.message_id < c_msg_id):
                            start_idx = idx
                            break
                    elif m.message_id == c_msg_id:
                        start_idx = idx + 1
                        break
                else:
                    start_idx = len(desc_sorted)

            page_slice = desc_sorted[start_idx : start_idx + limit]
            has_more = (start_idx + limit) < len(desc_sorted)

            next_cursor = None
            if has_more and page_slice:
                oldest_in_page = page_slice[-1]
                next_cursor = encode_message_cursor({
                    "message_id": oldest_in_page.message_id,
                    "created_at": (oldest_in_page.created_at or datetime.min.replace(tzinfo=UTC)).isoformat(),
                    "space_id": space_id,
                    "tag": project_tag or "all",
                })

            # Return ascending order for UI display
            asc_page = list(reversed(page_slice))
            return asc_page, next_cursor, has_more

    def get_message(self, message_id: str) -> Optional[Message]:
        with self._lock:
            for msgs in self.messages.values():
                for m in msgs:
                    if m.message_id == message_id:
                        return m
            return None

    def get_message_by_client_id(
        self, space_id: str, client_message_id: str, sender_uid: Optional[str] = None
    ) -> Optional[Message]:
        with self._lock:
            msgs = self.messages.get(space_id, [])
            for m in msgs:
                if getattr(m, "client_message_id", None) == client_message_id:
                    if sender_uid is None or m.sender_uid == sender_uid:
                        return m
            return None

    def get_user_message_by_client_id(
        self, space_id: str, sender_uid: str, client_message_id: str
    ) -> Optional[Message]:
        return self.get_message_by_client_id(space_id, client_message_id, sender_uid=sender_uid)

    def update_proposal_message_run_id(
        self, space_id: str, action_id: str, run_id: str
    ) -> Optional[Message]:
        now = datetime.now(UTC)
        matched_msg = None
        def mutate():
            nonlocal matched_msg
            msgs = self.messages.get(space_id, [])
            for m in msgs:
                if m.proposed_action and m.proposed_action.action_id == action_id:
                    m.run_id = run_id
                    matched_msg = m
                    break
        self._mutate_disk_state(mutate)
        return matched_msg

    # Chat Idempotency operations
    def get_chat_idempotency(self, key: str) -> Optional[ChatIdempotencyRecord]:
        with self._lock:
            return self.chat_idempotency.get(key)

    def acquire_chat_idempotency(
        self,
        key: str,
        space_id: str,
        sender_uid: str,
        client_message_id: str,
        payload_hash: str,
        lease_duration_seconds: int = 120,
        lease_owner: Optional[str] = None,
    ) -> Tuple[bool, Optional[ChatIdempotencyRecord]]:
        now = datetime.now(UTC)
        owner = lease_owner or f"owner_{uuid.uuid4().hex[:12]}"
        acquired = False
        target_record = None
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            existing = self.chat_idempotency.get(key)
            if existing:
                # Check for expired in-progress lease (e.g. previous server crash)
                if (
                    existing.status == ChatIdempotencyStatus.IN_PROGRESS
                    and existing.lease_until
                    and existing.lease_until < now
                ):
                    existing.status = ChatIdempotencyStatus.IN_PROGRESS
                    existing.version += 1
                    existing.lease_owner = owner
                    existing.lease_until = now + timedelta(seconds=lease_duration_seconds)
                    existing.updated_at = now
                    if self.persist_path:
                        self._save_state_to_disk()
                    return True, existing.model_copy(deep=True)
                return False, existing.model_copy(deep=True)

            record = ChatIdempotencyRecord(
                key=key,
                space_id=space_id,
                sender_uid=sender_uid,
                client_message_id=client_message_id,
                payload_hash=payload_hash,
                status=ChatIdempotencyStatus.IN_PROGRESS,
                version=1,
                lease_owner=owner,
                lease_until=now + timedelta(seconds=lease_duration_seconds),
            )
            self.chat_idempotency[key] = record
            if self.persist_path:
                self._save_state_to_disk()
            acquired = True
            target_record = record.model_copy(deep=True)
        return acquired, target_record

    def transition_chat_idempotency_status(
        self,
        key: str,
        expected_status: ChatIdempotencyStatus,
        new_status: ChatIdempotencyStatus,
        lease_duration_seconds: int = 120,
        lease_owner: Optional[str] = None,
    ) -> Tuple[bool, Optional[ChatIdempotencyRecord]]:
        """
        Atomic Compare-And-Swap (CAS) state transition for Chat Idempotency lifecycle.
        Only a single concurrent worker can win the transition (e.g. FAILED -> IN_PROGRESS on retry).
        """
        now = datetime.now(UTC)
        owner = lease_owner or f"owner_{uuid.uuid4().hex[:12]}"
        transitioned = False
        target_record = None
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.chat_idempotency.get(key)
            if not rec:
                return False, None

            # CAS validation: match expected status, or allow expired lease takeover if in_progress
            is_valid_transition = rec.status == expected_status or (
                rec.status == ChatIdempotencyStatus.IN_PROGRESS
                and rec.lease_until is not None
                and rec.lease_until < now
            )
            if not is_valid_transition:
                return False, rec.model_copy(deep=True)

            rec.status = new_status
            rec.version += 1
            rec.updated_at = now
            if new_status == ChatIdempotencyStatus.IN_PROGRESS:
                rec.lease_owner = owner
                rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
            else:
                rec.lease_owner = None
                rec.lease_until = None

            if self.persist_path:
                self._save_state_to_disk()
            transitioned = True
            target_record = rec.model_copy(deep=True)
        return transitioned, target_record

    def _save_state_to_disk(self):
        """Helper to save full state atomically to disk."""
        if not self.persist_path:
            return
        target_dir = os.path.dirname(os.path.abspath(self.persist_path))
        os.makedirs(target_dir, exist_ok=True)
        state = {
            "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
            "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
            "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
            "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
            "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
            "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
            "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
            "chat_idempotency": {k: v.model_dump(mode="json") for k, v in self.chat_idempotency.items()},
            "document_chunks": {
                f"{k[0]}:{k[1]}": [c.model_dump(mode="json") for c in v]
                for k, v in self.document_chunks.items()
            },
            "activity_events": {
                k: [e.model_dump(mode="json") for e in v]
                for k, v in self.activity_events.items()
            },
            "activity_outbox": {
                k: v.model_dump(mode="json")
                for k, v in self.activity_outbox.items()
            },
            "action_executions": {
                k: v.model_dump(mode="json")
                for k, v in self.action_executions.items()
            },
            "artifacts": {
                k: v.model_dump(mode="json")
                for k, v in self.artifacts.items()
            },
            "telemetry_scan_cursors": self._telemetry_scan_cursors,
            "metric_rollups": self._metric_rollups,
        }
        tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as file_out:
            json.dump(state, file_out, indent=2)
        os.replace(tmp_path, self.persist_path)

    def update_chat_idempotency_fenced(
        self,
        key: str,
        expected_version: Optional[int] = None,
        *,
        expected_lease_owner: Optional[str] = None,
        status: Optional[ChatIdempotencyStatus] = None,
        user_message_id: Optional[str] = None,
        agent_message_id: Optional[str] = None,
        run_id: Optional[str] = None,
        error_status_code: Optional[int] = None,
        error_detail: Optional[str] = None,
        extend_lease_seconds: Optional[int] = None,
    ) -> Tuple[bool, Optional[ChatIdempotencyRecord]]:
        """
        Atomic Compare-And-Swap (CAS) write protected by expected_version, expected_lease_owner, and active lease.
        Prevents stale/expired workers from overwriting states of newer lease holders.
        """
        now = datetime.now(UTC)
        updated = False
        target_record = None
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.chat_idempotency.get(key)
            if not rec:
                return False, None

            # Fencing check 1: expected_lease_owner must match if provided
            if expected_lease_owner is not None and rec.lease_owner != expected_lease_owner:
                return False, rec.model_copy(deep=True)

            # Fencing check 2: version must match if provided
            if expected_version is not None and rec.version != expected_version:
                return False, rec.model_copy(deep=True)

            # Fencing check 3: if in progress and not revoking/failing with matching lease_owner, lease must not be expired
            if (
                rec.status == ChatIdempotencyStatus.IN_PROGRESS
                and status != ChatIdempotencyStatus.FAILED
                and rec.lease_until is not None
                and rec.lease_until < now
            ):
                return False, rec.model_copy(deep=True)

            # Apply state mutations
            if status is not None:
                rec.status = status
            if user_message_id is not None:
                rec.user_message_id = user_message_id
            if agent_message_id is not None:
                rec.agent_message_id = agent_message_id
            if run_id is not None:
                rec.run_id = run_id
            if error_status_code is not None:
                rec.error_status_code = error_status_code
            if error_detail is not None:
                rec.error_detail = error_detail

            # Version bump & lease management
            rec.version += 1
            rec.updated_at = now
            if extend_lease_seconds is not None and rec.status == ChatIdempotencyStatus.IN_PROGRESS:
                rec.lease_until = now + timedelta(seconds=extend_lease_seconds)
            elif rec.status in (ChatIdempotencyStatus.COMPLETED, ChatIdempotencyStatus.FAILED):
                rec.lease_until = None
                rec.lease_owner = None

            if self.persist_path:
                self._save_state_to_disk()
            updated = True
            target_record = rec.model_copy(deep=True)
        return updated, target_record

    def update_chat_idempotency(self, record: ChatIdempotencyRecord) -> None:
        """Transactional update helper executing fenced write with record.version."""
        self.update_chat_idempotency_fenced(
            record.key,
            record.version,
            expected_lease_owner=record.lease_owner,
            status=record.status,
            user_message_id=record.user_message_id,
            agent_message_id=record.agent_message_id,
            run_id=record.run_id,
            error_status_code=record.error_status_code,
            error_detail=record.error_detail,
        )

    # File operations
    def save_file(self, file_rec: FileRecord) -> FileRecord:
        def mutate():
            self.files[file_rec.file_id] = file_rec
        self._mutate_disk_state(mutate)
        return file_rec

    def get_file(self, file_id: str) -> Optional[FileRecord]:
        with self._lock:
            return self.files.get(file_id)

    def delete_file(self, file_id: str) -> bool:
        def mutate():
            if file_id in self.files:
                del self.files[file_id]
            if file_id in self.file_blobs:
                del self.file_blobs[file_id]
            keys_to_del = [k for k in self.document_chunks if k[0] == file_id]
            for k in keys_to_del:
                del self.document_chunks[k]
        self._mutate_disk_state(mutate)
        return True

    def delete_upload_intent_if_owned(self, file_id: str, upload_fencing_token: str) -> bool:
        """
        Atomic conditional deletion: removes pending upload intent ONLY if still owned by the uploader
        and has not been claimed/transitioned by cleanup worker.
        """
        deleted = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.upload_status != "pending_upload":
                return False
            if f.upload_fencing_token != upload_fencing_token:
                return False
            if f.cleanup_status in ("in_progress", "deleting"):
                return False  # Cleanup worker owns this now; do not delete under it

            del self.files[file_id]
            if file_id in self.file_blobs:
                del self.file_blobs[file_id]
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            deleted = True
        return deleted

    def mark_upload_failed_if_owned(self, file_id: str, upload_fencing_token: str, error_msg: str) -> bool:
        """
        Atomic conditional transition to failed: marks record failed ONLY if still owned by the uploader
        and avoids overwriting active cleanup worker lease tokens / state.
        """
        marked = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.upload_status != "pending_upload":
                return False
            if f.upload_fencing_token != upload_fencing_token:
                return False
            if f.cleanup_status in ("in_progress", "deleting"):
                return False  # Cleanup worker already holds active lease!

            f.upload_status = "failed"
            f.cleanup_pending = True
            f.cleanup_last_error = error_msg
            f.upload_fencing_token = None
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            marked = True
        return marked

    def list_files_in_space(self, space_id: str, project_tag: Optional[str] = None) -> List[FileRecord]:
        with self._lock:
            records = [f for f in self.files.values() if f.space_id == space_id and not f.cleanup_pending and f.upload_status == "committed"]
            if project_tag and project_tag not in ("all", "all-tracks", "undefined"):
                return [f for f in records if project_tag in f.project_tags or "all" in f.project_tags]
            return list(records)

    def search_accessible_files(self, accessible_space_ids: List[str], query: str) -> List[FileRecord]:
        with self._lock:
            q_lower = query.lower()
            results = []
            for file_rec in self.files.values():
                if file_rec.space_id in accessible_space_ids and not file_rec.cleanup_pending and file_rec.upload_status == "committed":
                    if q_lower in file_rec.filename.lower() or any(q_lower in tag.lower() for tag in file_rec.project_tags):
                        results.append(file_rec)
            return results

    def list_cleanup_pending_files(self) -> List[FileRecord]:
        now = datetime.now(UTC)
        with self._lock:
            pending = []
            for f in self.files.values():
                is_cleanup = f.cleanup_pending is True
                is_failed_upload = f.upload_status == "failed"
                is_abandoned_upload = (
                    f.upload_status == "pending_upload"
                    and f.upload_lease_until is not None
                    and f.upload_lease_until < now
                )
                if not (is_cleanup or is_failed_upload or is_abandoned_upload):
                    continue
                if f.cleanup_status == "failed":
                    continue
                # If currently in_progress or deleting, only list if active lease has expired
                if f.cleanup_lease_until and f.cleanup_lease_until > now:
                    continue
                pending.append(f)
            return pending

    def claim_cleanup_file(self, file_id: str, lease_duration_seconds: int = 60) -> Optional[FileRecord]:
        """
        Atomically claims a pending file for cleanup with fencing token and active lease protection.
        Allows unexpired deleting/in_progress records to be reclaimed if worker timed out/crashed.
        """
        now = datetime.now(UTC)
        token = uuid.uuid4().hex
        claimed = None
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f:
                return None

            # Protect in-flight uploads with active upload lease
            if f.upload_status == "pending_upload":
                if not f.upload_lease_until or f.upload_lease_until >= now:
                    return None  # In-flight upload grace period active!

            is_cleanup = f.cleanup_pending is True
            is_failed_upload = f.upload_status == "failed"
            is_abandoned_upload = (
                f.upload_status == "pending_upload"
                and f.upload_lease_until is not None
                and f.upload_lease_until < now
            )

            if not (is_cleanup or is_failed_upload or is_abandoned_upload):
                return None

            # If deleting or in_progress, it MUST have expired lease to be reclaimed by a new worker
            if f.cleanup_status in ("in_progress", "deleting"):
                if f.cleanup_lease_until and f.cleanup_lease_until > now:
                    return None  # Active lease held by another worker
            elif f.cleanup_status == "failed":
                return None

            if f.cleanup_retries >= f.max_cleanup_retries:
                return None
            if f.cleanup_next_retry_at and f.cleanup_next_retry_at > now:
                return None

            f.cleanup_pending = True
            f.cleanup_status = "in_progress"
            f.lease_token = token
            f.lease_version += 1
            f.cleanup_lease_until = now + timedelta(seconds=lease_duration_seconds)
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            claimed = f.model_copy(deep=True)
        return claimed

    def mark_cleanup_file_deleting(self, file_id: str, lease_token: str) -> bool:
        """
        Transitions cleanup record to 'deleting' state under valid lease BEFORE irreversible physical deletion.
        """
        now = datetime.now(UTC)
        marked = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.lease_token != lease_token:
                return False
            if not f.cleanup_lease_until or f.cleanup_lease_until < now:
                return False  # Lease expired!
            if f.cleanup_status not in ("in_progress", "deleting"):
                return False
            f.cleanup_status = "deleting"
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            marked = True
        return marked

    def delete_cleanup_file_with_lease(self, file_id: str, lease_token: str) -> bool:
        """
        Deletes a cleaned-up file ONLY if the worker holds the valid active lease token and lease is unexpired.
        """
        now = datetime.now(UTC)
        deleted = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.lease_token != lease_token:
                return False
            if not f.cleanup_lease_until or f.cleanup_lease_until < now:
                return False  # Lease expired!
            del self.files[file_id]
            if file_id in self.file_blobs:
                del self.file_blobs[file_id]
            keys_to_del = [k for k in self.document_chunks if k[0] == file_id]
            for k in keys_to_del:
                del self.document_chunks[k]
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            deleted = True
        return deleted

    def release_cleanup_file_with_lease(
        self,
        file_id: str,
        lease_token: str,
        next_retry_at: Optional[datetime],
        last_error: str,
        is_terminal: bool,
    ) -> bool:
        """
        Releases a failed cleanup lease ONLY if the lease token matches and is unexpired.
        """
        now = datetime.now(UTC)
        released = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.lease_token != lease_token:
                return False
            if not f.cleanup_lease_until or f.cleanup_lease_until < now:
                return False  # Lease expired!
            f.cleanup_retries += 1
            f.lease_token = None
            f.cleanup_lease_until = None
            f.cleanup_last_error = last_error
            f.cleanup_status = "failed" if is_terminal else "pending"
            f.cleanup_next_retry_at = next_retry_at
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            released = True
        return released

    def renew_upload_lease(
        self,
        file_id: str,
        upload_fencing_token: str,
        extension_seconds: int = 60,
    ) -> bool:
        """
        Extends the in-flight upload grace period while data streaming / upload is active.
        Strictly sets lease to (now + extension_seconds), bounding maximum grace window and preventing accumulation.
        """
        now = datetime.now(UTC)
        renewed = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.upload_status != "pending_upload":
                return False
            if f.upload_fencing_token != upload_fencing_token:
                return False
            if f.cleanup_status in ("deleting", "failed") or f.cleanup_pending:
                return False

            f.upload_lease_until = now + timedelta(seconds=extension_seconds)
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            renewed = True
        return renewed

    def commit_uploaded_file(
        self,
        file_id: str,
        upload_fencing_token: str,
        size_bytes: int,
        sha256: str,
    ) -> Optional[FileRecord]:
        """
        Atomic Phase 3 Upload Commit: Commits record ONLY if not expired/claimed by cleanup worker.
        """
        committed = None
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.upload_status != "pending_upload":
                return None
            if f.upload_fencing_token != upload_fencing_token:
                return None
            if f.cleanup_status in ("deleting", "failed") or f.cleanup_pending:
                return None

            f.upload_status = "committed"
            f.cleanup_pending = False
            f.upload_fencing_token = None
            f.upload_lease_until = None
            f.size_bytes = size_bytes
            f.sha256 = sha256
            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            committed = f.model_copy(deep=True)
        return committed

    # Document Chunk & Generation Fencing operations
    def save_document_chunks(
        self,
        space_id: str,
        file_id: str,
        generation: int,
        chunks: List[DocumentChunk],
        expected_job_id: Optional[str] = None,
    ) -> bool:
        """Saves chunks partitioned by (file_id, generation) with lease and job ownership verification.
        Rejects writes to any generation that is currently being cleaned or has been permanently tombstoned.
        """
        now = datetime.now(UTC)
        with self._lock:
            f = self.files.get(file_id)
            if not f or f.space_id != space_id:
                logger.warning(f"Rejected chunk write for {file_id}: file not found or space mismatch")
                return False
            if expected_job_id and f.ingestion_job_id != expected_job_id:
                logger.warning(
                    f"Rejected chunk write for {file_id}: job ownership conflict "
                    f"(expected {expected_job_id}, active {f.ingestion_job_id})"
                )
                return False
            if f.ingestion_lease_until is not None and f.ingestion_lease_until < now:
                logger.warning(f"Rejected chunk write for {file_id}: lease expired")
                return False
            if generation in (f.cleaning_generations or []):
                logger.warning(
                    f"Rejected chunk write for {file_id}: generation {generation} is currently being cleaned"
                )
                return False
            if generation in (f.deleted_generations or []):
                logger.warning(
                    f"Rejected chunk write for {file_id}: generation {generation} has been permanently tombstoned"
                )
                return False

        def mutate():
            self.document_chunks[(file_id, generation)] = list(chunks)
        self._mutate_disk_state(mutate)
        return True

    def get_document_chunks(
        self,
        space_id: str,
        file_id: str,
        generation: Optional[int] = None,
    ) -> List[DocumentChunk]:
        """Gets document chunks for the active or requested generation with tenancy check."""
        with self._lock:
            f = self.files.get(file_id)
            if not f or f.space_id != space_id:
                return []
            target_gen = generation if generation is not None else f.active_generation
            return list(self.document_chunks.get((file_id, target_gen), []))

    def prune_document_chunks(self, space_id: str, file_id: str, before_generation: int) -> None:
        """Prunes historical chunk generations, keeping only the active generation."""
        def mutate():
            keys_to_del = [k for k in self.document_chunks if k[0] == file_id and k[1] < before_generation]
            for k in keys_to_del:
                del self.document_chunks[k]
        self._mutate_disk_state(mutate)

    def delete_generation_document_chunks(
        self,
        space_id: str,
        file_id: str,
        generation: int,
        job_id: Optional[str] = None,
    ) -> int:
        """Deletes chunks strictly belonging to (file_id, generation) with mutual exclusion against active generation.
        Raises CleanupAuthorizationError if the generation is protected (active) or file is missing.
        Stamps deleted_generations tombstone before releasing cleaning_generations, permanently revoking commit rights.
        """
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.space_id != space_id:
                raise CleanupAuthorizationError(
                    f"Cleanup authorization denied for {file_id} gen {generation}: file not found in space {space_id}"
                )
            if f.active_generation == generation:
                logger.warning(f"delete_generation_document_chunks rejected: cannot delete active generation {generation}")
                raise CleanupAuthorizationError(
                    f"Cleanup authorization denied for {file_id} gen {generation}: it is currently the active generation"
                )
            if generation not in (f.cleaning_generations or []):
                f.cleaning_generations = list(set((f.cleaning_generations or []) + [generation]))

        def mutate():
            if (file_id, generation) in self.document_chunks:
                del self.document_chunks[(file_id, generation)]
            if file_id in self.files:
                f_mut = self.files[file_id]
                # Stamp permanent tombstone BEFORE releasing cleaning lock
                if generation not in (f_mut.deleted_generations or []):
                    f_mut.deleted_generations = list(set((f_mut.deleted_generations or []) + [generation]))
                f_mut.cleaning_generations = [
                    g for g in (f_mut.cleaning_generations or []) if g != generation
                ]
        self._mutate_disk_state(mutate)
        with self._lock:
            return len(self.document_chunks.get((file_id, generation), []))


    def list_pending_generation_cleanups(self) -> List[Dict[str, Any]]:
        now = datetime.now(UTC)
        with self._lock:
            pending = []
            for item in self.pending_generation_cleanups.values():
                if item.get("status") == "completed":
                    continue
                lease_until = item.get("lease_until")
                if lease_until:
                    if isinstance(lease_until, str):
                        try:
                            lease_dt = datetime.fromisoformat(lease_until.replace("Z", "+00:00"))
                        except Exception:
                            lease_dt = None
                    else:
                        lease_dt = lease_until
                    if lease_dt and lease_dt > now:
                        continue
                pending.append(dict(item))
            return pending

    def claim_pending_generation_cleanup(self, cleanup_id: str, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        now = datetime.now(UTC)
        token = uuid.uuid4().hex
        with self._lock:
            item = self.pending_generation_cleanups.get(cleanup_id)
            if not item or item.get("status") == "completed":
                return None
            lease_until = item.get("lease_until")
            if lease_until:
                if isinstance(lease_until, str):
                    try:
                        lease_dt = datetime.fromisoformat(lease_until.replace("Z", "+00:00"))
                    except Exception:
                        lease_dt = None
                else:
                    lease_dt = lease_until
                if lease_dt and lease_dt > now:
                    return None
            item["lease_token"] = token
            item["lease_until"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            item["status"] = "in_progress"
            item["retry_count"] = item.get("retry_count", 0) + 1
            item["updated_at"] = now.isoformat()
            return dict(item)

    def complete_pending_generation_cleanup(self, cleanup_id: str, lease_token: str) -> bool:
        with self._lock:
            item = self.pending_generation_cleanups.get(cleanup_id)
            if not item or item.get("lease_token") != lease_token:
                return False
            del self.pending_generation_cleanups[cleanup_id]
            return True

    def sweep_pending_generation_cleanups(self, limit: int = 10) -> Dict[str, int]:
        now = datetime.now(UTC)
        candidates = self.list_pending_generation_cleanups()[:limit]
        claimed_count = 0
        cleaned_count = 0
        failed_count = 0
        deferred_count = 0

        for c in candidates:
            cleanup_id = c.get("cleanup_id")
            if not cleanup_id:
                continue

            # Check deferral (exponential backoff)
            next_retry_raw = c.get("next_retry_at")
            if next_retry_raw:
                try:
                    if isinstance(next_retry_raw, str):
                        next_retry_dt = datetime.fromisoformat(next_retry_raw.replace("Z", "+00:00"))
                    else:
                        next_retry_dt = next_retry_raw
                    if next_retry_dt and next_retry_dt > now:
                        deferred_count += 1
                        continue
                except Exception:
                    pass

            # Check retry ceiling
            retries = c.get("retry_count", 0)
            if retries >= 5:
                failed_count += 1
                continue

            claimed = self.claim_pending_generation_cleanup(cleanup_id, lease_seconds=60)
            if not claimed:
                deferred_count += 1
                continue

            claimed_count += 1
            space_id = claimed["space_id"]
            file_id = claimed["file_id"]
            generation = claimed["generation"]
            job_id = claimed.get("job_id")
            lease_token = claimed.get("lease_token") or ""

            try:
                self.delete_generation_document_chunks(space_id, file_id, generation, job_id)
                if self.complete_pending_generation_cleanup(cleanup_id, lease_token):
                    cleaned_count += 1
                else:
                    failed_count += 1
            except CleanupAuthorizationError as auth_err:
                # Structural denial: active generation protected, file absent, space mismatch.
                # Retrying cannot succeed — resolve the work record permanently.
                logger.warning(f"Protected skip for cleanup {cleanup_id}: {auth_err}")
                if self.complete_pending_generation_cleanup(cleanup_id, lease_token):
                    cleaned_count += 1
                else:
                    failed_count += 1
            except (StorageUnavailableError, Exception) as item_err:
                # Transient backend failure: preserve work record and apply exponential backoff.
                logger.warning(f"Error processing pending generation cleanup {cleanup_id}: {item_err}")
                failed_count += 1
                # Failure update via CAS: only update if our lease_token still matches (expired worker cannot overwrite)
                with self._lock:
                    rec = self.pending_generation_cleanups.get(cleanup_id)
                    if rec and rec.get("lease_token") == lease_token and rec.get("status") != "completed":
                        backoff_sec = 2 ** min(retries + 1, 6) * 5
                        rec["lease_token"] = None
                        rec["lease_until"] = None
                        rec["status"] = "pending"
                        rec["next_retry_at"] = (now + timedelta(seconds=backoff_sec)).isoformat()
                        rec["last_error"] = str(item_err)

        return {
            "scanned": len(candidates),
            "claimed": claimed_count,
            "cleaned": cleaned_count,
            "failed": failed_count,
            "deferred": deferred_count,
        }

    def delete_all_document_chunks(self, space_id: str, file_id: str) -> None:
        """Purges all chunk generations for a file."""
        def mutate():
            keys_to_del = [k for k in self.document_chunks if k[0] == file_id]
            for k in keys_to_del:
                del self.document_chunks[k]
        self._mutate_disk_state(mutate)

    def acquire_ingestion_lease(
        self,
        space_id: str,
        file_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Tuple[bool, Optional[FileRecord], int]:
        """
        Atomically claims an ingestion lease on a committed file.
        Allocates unique monotonic target_generation = max(max_allocated_gen, active_gen) + 1.
        Fails closed if file is currently being processed by another active worker with valid lease.
        """
        now = datetime.now(UTC)
        acquired = False
        target_record = None
        target_gen = 0
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.space_id != space_id or f.upload_status != "committed":
                return (False, None, 0)

            # Invariant: Disallow lease theft if current worker lease is still active
            if (
                f.ingestion_status in (IngestionStatus.EXTRACTING, IngestionStatus.INDEXING)
                and f.ingestion_lease_until is not None
                and f.ingestion_lease_until > now
                and f.ingestion_lease_owner != worker_id
                and f.ingestion_lease_owner != "lease_reclaimer"
            ):
                return (False, f, f.active_generation)

            target_gen = max(f.max_allocated_generation or 0, f.active_generation or 0) + 1
            job_id = f"job_ingest_{file_id}_g{target_gen}_{uuid.uuid4().hex[:8]}"

            f.max_allocated_generation = target_gen
            f.ingestion_status = IngestionStatus.EXTRACTING
            f.ingestion_job_id = job_id
            f.ingestion_lease_owner = worker_id
            f.ingestion_lease_until = now + timedelta(seconds=lease_seconds)
            f.ingestion_started_at = now
            f.ingestion_version = (f.ingestion_version or 0) + 1
            f.ingestion_error_code = None
            f.ingestion_error_message = None

            if self.persist_path:
                self._save_state_to_disk()
            acquired = True
            target_record = f.model_copy(deep=True)

        return (acquired, target_record, target_gen)

    def commit_ingestion_generation(
        self,
        space_id: str,
        file_id: str,
        target_generation: int,
        expected_job_id: str,
        chunk_count: int,
        pages: int,
        ocr_gaps: List[int],
        status: IngestionStatus = IngestionStatus.READY,
        error_code: Optional[str] = None,
        error_msg: Optional[str] = None,
    ) -> bool:
        """
        Atomic CAS commit: Swaps active_generation to target_generation and marks file READY / READY_PARTIAL / NEEDS_OCR.
        Fenced strictly by expected_job_id to prevent stale zombie workers from committing.
        Verifies target_generation is not undergoing cleanup (cleaning_generations) or already tombstoned (deleted_generations).
        Prunes old chunk generations post-commit.
        """
        now = datetime.now(UTC)
        committed = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.space_id != space_id or f.ingestion_job_id != expected_job_id:
                return False
            if f.ingestion_status not in (IngestionStatus.EXTRACTING, IngestionStatus.INDEXING):
                return False
            if f.ingestion_lease_until is not None and f.ingestion_lease_until < now:
                return False
            if f.max_allocated_generation and f.max_allocated_generation != target_generation:
                return False
            if target_generation in (f.cleaning_generations or []):
                logger.warning(f"Rejected commit for {file_id}: target generation {target_generation} is currently being cleaned")
                return False
            if target_generation in (f.deleted_generations or []):
                logger.warning(f"Rejected commit for {file_id}: target generation {target_generation} has been permanently tombstoned (already cleaned)")
                return False

            f.active_generation = target_generation
            f.committed_generations = list(set((f.committed_generations or []) + [target_generation]))
            f.chunk_count = chunk_count
            f.extracted_pages = pages
            f.ocr_gap_pages = list(ocr_gaps)
            f.has_ocr_gaps = len(ocr_gaps) > 0
            f.ingestion_status = status
            f.ingestion_completed_at = now
            f.ingestion_lease_owner = None
            f.ingestion_lease_until = None
            f.ingestion_error_code = None
            f.ingestion_error_message = None

            # Prune previous generation chunks
            keys_to_del = [k for k in self.document_chunks if k[0] == file_id and k[1] != target_generation]
            for k in keys_to_del:
                del self.document_chunks[k]

            if self.persist_path:
                self._save_state_to_disk()
            committed = True

        return committed

    def fail_ingestion_generation(
        self,
        space_id: str,
        file_id: str,
        expected_job_id: str,
        error_code: str,
        error_msg: str,
    ) -> bool:
        """
        Marks ingestion as FAILED without altering active_generation, preserving previously indexed chunks.
        """
        now = datetime.now(UTC)
        failed = False
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            f = self.files.get(file_id)
            if not f or f.space_id != space_id or f.ingestion_job_id != expected_job_id:
                return False

            f.ingestion_status = IngestionStatus.FAILED
            f.ingestion_error_code = error_code
            f.ingestion_error_message = error_msg
            f.ingestion_completed_at = now
            f.ingestion_lease_owner = None
            f.ingestion_lease_until = None

            if self.persist_path:
                self._save_state_to_disk()
            failed = True

        return failed

    def scan_and_reclaim_expired_ingestion_leases(
        self,
        now: Optional[datetime] = None,
    ) -> List[FileRecord]:
        """
        Finds all files in extracting/indexing whose lease expired, retryable failures, or stalled pending,
        and atomically claims a recovery lease for re-dispatch.
        """
        current_time = now or datetime.now(UTC)
        reclaimed: List[FileRecord] = []
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            for f in self.files.values():
                is_expired_lease = (
                    f.ingestion_status in (IngestionStatus.EXTRACTING, IngestionStatus.INDEXING)
                    and f.ingestion_lease_until is not None
                    and f.ingestion_lease_until < current_time
                )
                is_retryable_failure = (
                    f.ingestion_status == IngestionStatus.FAILED
                    and f.ingestion_error_code in ("ERR_ENQUEUE_FAILED", "ERR_CLOUD_TASKS_ENQUEUE_FAILED", "LEASE_EXPIRED", "ERR_LEASE_TIMEOUT")
                )
                is_stalled_pending = (
                    f.upload_status == "committed"
                    and f.ingestion_status == IngestionStatus.PENDING
                    and f.active_generation == 0
                    and (f.ingestion_started_at is None or f.ingestion_started_at < current_time - timedelta(minutes=2))
                )
                if is_expired_lease or is_retryable_failure or is_stalled_pending:
                    target_gen = max(f.max_allocated_generation or 0, f.active_generation or 0) + 1
                    job_id = f"job_ingest_{f.file_id}_g{target_gen}_{uuid.uuid4().hex[:8]}"
                    f.max_allocated_generation = target_gen
                    f.ingestion_job_id = job_id
                    f.ingestion_status = IngestionStatus.EXTRACTING
                    f.ingestion_lease_owner = "lease_reclaimer"
                    f.ingestion_lease_until = current_time + timedelta(seconds=120)
                    f.ingestion_started_at = current_time
                    f.ingestion_error_code = None
                    f.ingestion_error_message = None
                    reclaimed.append(f.model_copy(deep=True))

            if reclaimed and self.persist_path:
                self._save_state_to_disk()

        return reclaimed

    # Run operations
    def save_run(self, run: Run) -> Run:
        now = datetime.now(UTC)
        def mutate():
            if run.run_id in self.runs:
                return
            self.runs[run.run_id] = run

            # Atomic outbox staging for initial run creation
            ev_id = f"run.started:{run.run_id}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=ActivityEventType.RUN_STARTED,
                space_id=run.space_id,
                project_tags=[run.project_tag or "general"],
                resource_type="run",
                resource_id=run.run_id,
                summary=f"Execution run started: {run.prompt[:60]}",
                details={"run_id": run.run_id, "status": run.status.value if hasattr(run.status, "value") else str(run.status)},
                actor_uid=run.created_by,
                created_at=now,
            )
            if run.space_id not in self.activity_events:
                self.activity_events[run.space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[run.space_id]):
                self.activity_events[run.space_id].append(ev)
            outbox_id = f"outbox:{ev.event_id}"
            self.activity_outbox[outbox_id] = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=run.space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
        self._mutate_disk_state(mutate)
        return self.runs.get(run.run_id, run)

    def save_run_fenced(
        self,
        run: Run,
        idemp_key: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Run:
        """
        Atomically saves Run under lease ownership and idempotency version fencing.
        If run_id already exists in store, preserves the first result (create-if-absent).
        If idemp_key & expected_version are supplied, verifies active unexpired lease
        and expected version before saving, strictly fencing off stale zombie workers.
        """
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()

            # 1. Lease fencing check
            if idemp_key is not None and expected_version is not None:
                rec = self.chat_idempotency.get(idemp_key)
                if not rec:
                    raise StorageConflictError("Idempotency record not found during fenced run save.")
                if rec.version != expected_version:
                    raise StorageConflictError(
                        f"Fencing conflict: expected idempotency version {expected_version}, but found {rec.version}."
                    )
                if rec.status == ChatIdempotencyStatus.IN_PROGRESS and rec.lease_until and rec.lease_until < now:
                    raise StorageConflictError("Fencing conflict: operation lease expired during run save.")

            # 2. Create-if-absent idempotency
            if run.run_id in self.runs:
                return self.runs[run.run_id]

            self.runs[run.run_id] = run

            # Atomic outbox staging for initial run creation
            ev_id = f"run.started:{run.run_id}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=ActivityEventType.RUN_STARTED,
                space_id=run.space_id,
                project_tags=[run.project_tag or "general"],
                resource_type="run",
                resource_id=run.run_id,
                summary=f"Execution run started: {run.prompt[:60]}",
                details={"run_id": run.run_id, "status": run.status.value if hasattr(run.status, "value") else str(run.status)},
                actor_uid=run.created_by,
                created_at=now,
            )
            if run.space_id not in self.activity_events:
                self.activity_events[run.space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[run.space_id]):
                self.activity_events[run.space_id].append(ev)
            outbox_id = f"outbox:{ev.event_id}"
            self.activity_outbox[outbox_id] = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=run.space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )

            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            return run

    def get_run(self, run_id: str) -> Optional[Run]:
        with self._lock:
            return self.runs.get(run_id)

    def list_runs_in_space(self, space_id: str, project_tag: Optional[str] = None) -> List[Run]:
        with self._lock:
            runs = [r for r in self.runs.values() if r.space_id == space_id]
            if project_tag and project_tag not in ("all", "all-tracks", "undefined"):
                return [r for r in runs if r.project_tag == project_tag]
            return list(runs)

    def list_stalled_approval_runs(
        self, space_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> List[Run]:
        curr_now = now or datetime.now(UTC)
        with self._lock:
            res = []
            for r in self.runs.values():
                if r.status != RunStatus.RUNNING:
                    continue
                if space_id and r.space_id != space_id:
                    continue
                gate = r.approval_gate
                is_stalled = False
                if gate and gate.status == "approving":
                    if gate.decision_lease_until and gate.decision_lease_until <= curr_now:
                        is_stalled = True
                    elif getattr(r, "uncertain_since", None):
                        if r.uncertain_since + timedelta(seconds=30) <= curr_now:
                            is_stalled = True
                    elif getattr(r, "approval_commit_status", None) == "uncertain":
                        is_stalled = True
                elif getattr(r, "approval_commit_status", None) == "uncertain":
                    is_stalled = True
                if is_stalled:
                    res.append(r.model_copy(deep=True))
            return res

    def compare_and_swap_run_status(
        self,
        run_id: str,
        expected_status: RunStatus,
        new_status: RunStatus,
        mutator_fn: Optional[Callable[[Run], None]] = None,
        actor_uid: Optional[str] = None,
        outbox_event_factory: Optional[Callable[[Run], Optional[ActivityEvent]]] = None,
    ) -> Optional[Run]:
        """
        Atomic Compare-And-Swap (CAS) state transition for Run lifecycle with integrated Transactional Outbox.
        Returns updated Run if transition succeeded, or None on conflict.
        """
        target_run = None
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.status != expected_status:
                return None
            run.status = new_status
            run.state_version = (getattr(run, "state_version", 1) or 1) + 1
            run.updated_at = now
            if mutator_fn:
                mutator_fn(run)

            # Determine Activity Event & Outbox Staging
            ev = None
            if outbox_event_factory:
                ev = outbox_event_factory(run)
            elif expected_status == RunStatus.AWAITING_APPROVAL and new_status == RunStatus.RUNNING:
                ev = ActivityEvent(
                    event_id=f"gate.approved:{run.run_id}:r{run.state_version}",
                    event_type=ActivityEventType.GATE_APPROVED,
                    space_id=run.space_id,
                    project_tags=[run.project_tag or "general"],
                    resource_type="gate",
                    resource_id=run.run_id,
                    summary=f"Approval gate '{run.approval_gate.title if run.approval_gate else 'Gate'}' approved",
                    details={"run_id": run.run_id, "state_version": run.state_version, "approved": True},
                    actor_uid=actor_uid,
                    created_at=now,
                )
            elif expected_status == RunStatus.AWAITING_APPROVAL and (new_status == RunStatus.FAILED or str(new_status).lower() == "rejected"):
                ev = ActivityEvent(
                    event_id=f"gate.rejected:{run.run_id}:r{run.state_version}",
                    event_type=ActivityEventType.GATE_REJECTED,
                    space_id=run.space_id,
                    project_tags=[run.project_tag or "general"],
                    resource_type="gate",
                    resource_id=run.run_id,
                    summary=f"Approval gate '{run.approval_gate.title if run.approval_gate else 'Gate'}' rejected",
                    details={"run_id": run.run_id, "state_version": run.state_version, "approved": False},
                    actor_uid=actor_uid,
                    created_at=now,
                )
            elif new_status == RunStatus.COMPLETED:
                ev = ActivityEvent(
                    event_id=f"run.completed:{run.run_id}:r{run.state_version}",
                    event_type=ActivityEventType.RUN_COMPLETED,
                    space_id=run.space_id,
                    project_tags=[run.project_tag or "general"],
                    resource_type="run",
                    resource_id=run.run_id,
                    summary=f"Execution run completed (Trace: {run.trace_id or run.run_id})",
                    details={"run_id": run.run_id, "state_version": run.state_version},
                    actor_uid=actor_uid,
                    created_at=now,
                )
            elif new_status == RunStatus.FAILED:
                ev = ActivityEvent(
                    event_id=f"run.failed:{run.run_id}:r{run.state_version}",
                    event_type=ActivityEventType.RUN_FAILED,
                    space_id=run.space_id,
                    project_tags=[run.project_tag or "general"],
                    resource_type="run",
                    resource_id=run.run_id,
                    summary=f"Execution run failed: {run.error_summary or 'Internal failure'}",
                    details={"run_id": run.run_id, "state_version": run.state_version, "failure_code": run.failure_code},
                    actor_uid=actor_uid,
                    created_at=now,
                )

            if new_status in (RunStatus.COMPLETED, RunStatus.FAILED):
                dur = (getattr(run, "telemetry", None) and getattr(run.telemetry, "duration_ms", 0)) or 0
                if dur == 0 and run.created_at:
                    dur = max(0, int((now - run.created_at).total_seconds() * 1000))
                tokens = getattr(run, "telemetry", None) and getattr(run.telemetry, "tokens_used", None)
                cost = getattr(run, "telemetry", None) and getattr(run.telemetry, "estimated_cost_usd", None)
                pricing_ver = getattr(run, "telemetry", None) and getattr(run.telemetry, "pricing_version", None)
                fail_code = run.failure_code or (run.telemetry and getattr(run.telemetry, "error_code", None))
                self._record_metric_rollup_event_locked(
                    space_id=run.space_id,
                    project_tag=run.project_tag or "general",
                    duration_ms=dur,
                    status=new_status.value,
                    failure_code=fail_code,
                    tokens_used=tokens,
                    estimated_cost_usd=cost,
                    pricing_version=pricing_ver,
                    timestamp=now,
                )

            if ev:
                if run.space_id not in self.activity_events:
                    self.activity_events[run.space_id] = []
                if not any(e.event_id == ev.event_id for e in self.activity_events[run.space_id]):
                    self.activity_events[run.space_id].append(ev)
                outbox_id = f"outbox:{ev.event_id}"
                self.activity_outbox[outbox_id] = ActivityOutboxItem(
                    outbox_id=outbox_id,
                    event_id=ev.event_id,
                    space_id=run.space_id,
                    event=ev,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    max_attempts=5,
                    created_at=now,
                    updated_at=now,
                    next_retry_at=now,
                )

            if self.persist_path:
                target_dir = os.path.dirname(os.path.abspath(self.persist_path))
                os.makedirs(target_dir, exist_ok=True)
                state = {
                    "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
                    "spaces": {k: v.model_dump(mode="json") for k, v in self.spaces.items()},
                    "memberships": [[k[0], k[1], v.value] for k, v in self.memberships.items()],
                    "invites": {k: v.model_dump(mode="json") for k, v in self.invites.items()},
                    "messages": {k: [m.model_dump(mode="json") for m in v] for k, v in self.messages.items()},
                    "files": {k: v.model_dump(mode="json") for k, v in self.files.items()},
                    "runs": {k: v.model_dump(mode="json") for k, v in self.runs.items()},
                }
                tmp_path = f"{self.persist_path}.{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as file_out:
                    json.dump(state, file_out, indent=2)
                os.replace(tmp_path, self.persist_path)
            target_run = run
        return target_run

    def update_run_telemetry_status(
        self,
        space_id: str,
        run_id: str,
        expected_trace_id: str,
        new_status: str,
        expected_generation: Optional[int] = None,
        expected_status: Optional[str] = None,
        expected_lease_token: Optional[str] = None,
        next_check_at: Optional[datetime] = None,
    ) -> Optional[Run]:
        """
        Version-fenced update of run telemetry status ensuring stale background workers
        cannot overwrite newer execution traces (E0 Contract 4).
        """
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                return None
            if run.trace_id != expected_trace_id:
                return None
            if expected_generation is not None and getattr(run, "telemetry_generation", 1) != expected_generation:
                return None
            if expected_status is not None and getattr(run, "telemetry_status", "not_instrumented") != expected_status:
                return None
            if expected_lease_token is not None:
                if getattr(run, "telemetry_lease_token", None) != expected_lease_token:
                    return None
                lease_until = getattr(run, "telemetry_lease_until", None)
                if lease_until is None or lease_until <= now:
                    return None

            run.telemetry_status = new_status
            run.telemetry_lease_token = None
            run.telemetry_lease_until = None
            run.telemetry_next_check_at = next_check_at
            if run.telemetry:
                run.telemetry.telemetry_status = new_status
                run.telemetry.has_real_telemetry = (new_status == "available")
                run.telemetry.telemetry_last_checked_at = now
                run.telemetry.telemetry_lease_token = None
                run.telemetry.telemetry_lease_until = None
                run.telemetry.telemetry_next_check_at = next_check_at
            run.telemetry_last_checked_at = now
            run.telemetry_attempts = (getattr(run, "telemetry_attempts", 0) or 0) + 1
            if run.telemetry:
                run.telemetry.telemetry_attempts = run.telemetry_attempts
            run.telemetry_generation = (getattr(run, "telemetry_generation", 1) or 1) + 1
            if run.telemetry:
                run.telemetry.telemetry_generation = run.telemetry_generation
            run.updated_at = now

            if self.persist_path:
                self._save_state_to_disk()
            return run.model_copy(deep=True)

    def claim_run_telemetry_verification(
        self,
        space_id: str,
        run_id: str,
        lease_seconds: float = 10.0,
        force: bool = False,
        min_interval_seconds: float = 2.0,
    ) -> Optional[Tuple[Run, str]]:
        """
        Atomically claims a short verification lease for a run to prevent duplicate/concurrent
        queries to the tracing backend (E2 Requirement 2).
        """
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                return None
            if run.telemetry_status == "available":
                return None

            # Terminal / Attempt budget check
            terminal_cooldown = getattr(settings, "TELEMETRY_TERMINAL_COOLDOWN_SECONDS", 60.0)
            max_attempts = getattr(settings, "TELEMETRY_VERIFICATION_MAX_ATTEMPTS", 3)
            is_terminal = (
                run.telemetry_status == "unavailable"
                or (getattr(run, "telemetry_attempts", 0) or 0) >= max_attempts
            )
            if is_terminal:
                if run.telemetry_last_checked_at:
                    elapsed = (now - run.telemetry_last_checked_at).total_seconds()
                    if elapsed < terminal_cooldown:
                        return None  # Enforce terminal cooldown even with force=True
                if not force:
                    return None
                # Controlled recovery: reset attempt budget
                run.telemetry_attempts = 0
                if run.telemetry:
                    run.telemetry.telemetry_attempts = 0

            # Active lease check: another in-flight worker is querying
            if run.telemetry_lease_until and run.telemetry_lease_until > now:
                return None

            # Hard floor throttle (1.0s even with force to prevent rapid backend exhaustion)
            if run.telemetry_last_checked_at:
                elapsed = (now - run.telemetry_last_checked_at).total_seconds()
                hard_floor = 1.0 if force else min_interval_seconds
                if elapsed < hard_floor:
                    return None

            # Backoff timer check (if delayed and scheduled for later)
            if run.telemetry_next_check_at and run.telemetry_next_check_at > now and not force:
                return None

            token = f"tl_{uuid.uuid4().hex[:12]}"
            lease_until = now + timedelta(seconds=lease_seconds)
            run.telemetry_lease_token = token
            run.telemetry_lease_until = lease_until
            run.telemetry_last_checked_at = now
            if run.telemetry:
                run.telemetry.telemetry_lease_token = token
                run.telemetry.telemetry_lease_until = lease_until
                run.telemetry.telemetry_last_checked_at = now
            run.updated_at = now

            if self.persist_path:
                self._save_state_to_disk()
            return run.model_copy(deep=True), token

    def get_telemetry_scan_cursor(self, space_id: Optional[str] = None) -> Optional[str]:
        scope_key = f"scope_{space_id}" if space_id else "scope___all__"
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self._telemetry_scan_cursors.get(scope_key, {})
            return rec.get("cursor_run_id")

    @property
    def last_pending_telemetry_cursor(self) -> Optional[str]:
        return self.get_telemetry_scan_cursor(None)

    @last_pending_telemetry_cursor.setter
    def last_pending_telemetry_cursor(self, val: Optional[str]) -> None:
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self._telemetry_scan_cursors.setdefault("scope___all__", {})
            rec["cursor_run_id"] = val
            if self.persist_path:
                self._save_state_to_disk()

    def list_pending_telemetry_runs(
        self,
        space_id: Optional[str] = None,
        limit: int = 50,
        now: Optional[datetime] = None,
        cursor: Optional[str] = None,
        max_scan: Optional[int] = None,
    ) -> List[Run]:
        """
        Authoritative query for runs requiring telemetry verification ('exporting' or 'delayed').
        Preserves continuation cursor per scope ('scope___all__' vs 'scope_{space_id}') across
        consecutive batches to prevent front-loaded starvations and cross-scope collisions.
        """
        curr_now = now or datetime.now(UTC)
        scope_key = f"scope_{space_id}" if space_id else "scope___all__"
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            scan_budget = max_scan if max_scan is not None else max(limit * 10, 500)

            rec = self._telemetry_scan_cursors.get(scope_key, {})
            lease_token = None
            expected_version = rec.get("version", 0) or 0

            # If cursor is None, claim lease atomically
            if cursor is None:
                l_until_str = rec.get("lease_until")
                l_until = datetime.fromisoformat(l_until_str) if l_until_str else None
                if l_until and l_until > curr_now and rec.get("lease_token"):
                    return []  # Scope is currently leased by another worker

                lease_token = secrets.token_hex(8)
                lease_exp = curr_now + timedelta(seconds=15)
                expected_version += 1
                self._telemetry_scan_cursors[scope_key] = {
                    "scope_key": scope_key,
                    "cursor_run_id": rec.get("cursor_run_id"),
                    "lease_token": lease_token,
                    "lease_until": lease_exp.isoformat(),
                    "version": expected_version,
                    "updated_at": curr_now.isoformat(),
                }
                if self.persist_path:
                    self._save_state_to_disk()

            start_cursor = cursor if cursor is not None else rec.get("cursor_run_id")

            matching = [
                r for r in self.runs.values()
                if (not space_id or r.space_id == space_id)
                and r.telemetry_status in ("exporting", "delayed")
            ]
            matching.sort(key=lambda x: x.run_id)

            start_idx = 0
            if start_cursor:
                found = False
                for idx, r in enumerate(matching):
                    if r.run_id == start_cursor:
                        start_idx = idx + 1
                        found = True
                        break
                if not found:
                    # Cursor run was deleted! Gracefully start from 0
                    start_idx = 0

            candidates = []
            scanned = 0
            last_scanned_id = None
            reached_stream_end = True

            for r in matching[start_idx:]:
                last_scanned_id = r.run_id
                scanned += 1
                if r.telemetry_lease_until and r.telemetry_lease_until > curr_now:
                    if scanned >= scan_budget:
                        reached_stream_end = False
                        break
                    continue
                if r.telemetry_next_check_at and r.telemetry_next_check_at > curr_now:
                    if scanned >= scan_budget:
                        reached_stream_end = False
                        break
                    continue
                candidates.append(r.model_copy(deep=True))
                if len(candidates) >= limit:
                    reached_stream_end = False
                    break
                if scanned >= scan_budget:
                    reached_stream_end = False
                    break

            new_cursor = None if reached_stream_end else last_scanned_id

            # Only commit if leased scan (explicit cursor is strictly read-only)
            if cursor is None and lease_token:
                commit_now = datetime.now(UTC)
                cur_rec = self._telemetry_scan_cursors.get(scope_key, {})
                db_token = cur_rec.get("lease_token")
                db_ver = cur_rec.get("version")
                cur_l_until_str = cur_rec.get("lease_until")
                cur_l_until = datetime.fromisoformat(cur_l_until_str) if cur_l_until_str else None

                # Must simultaneously satisfy: token match, version match, and unexpired lease
                if (
                    db_token == lease_token
                    and db_ver == expected_version
                    and cur_l_until is not None
                    and cur_l_until > commit_now
                ):
                    self._telemetry_scan_cursors[scope_key] = {
                        "scope_key": scope_key,
                        "cursor_run_id": new_cursor,
                        "version": expected_version + 1,
                        "lease_token": None,
                        "lease_until": None,
                        "updated_at": commit_now.isoformat(),
                    }
                    if self.persist_path:
                        self._save_state_to_disk()
                else:
                    logger.warning(
                        "MemoryStore commit rejected for %s: lease token, version or expiry mismatch", scope_key
                    )

            candidates.sort(
                key=lambda x: x.telemetry_last_checked_at.timestamp() if x.telemetry_last_checked_at else 0
            )
            return candidates[:limit]

    def _record_metric_rollup_event_locked(
        self,
        space_id: str,
        project_tag: str,
        duration_ms: int,
        status: str,
        failure_code: Optional[str] = None,
        tokens_used: Optional[int] = None,
        estimated_cost_usd: Optional[float] = None,
        pricing_version: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        now = timestamp or datetime.now(UTC)
        tag = project_tag or "general"
        hour_str = now.strftime("%Y-%m-%dT%H")
        bucket_id = f"{space_id}:{tag}:{hour_str}"

        from app.models.telemetry import get_histogram_bucket_key, normalize_failure_code

        raw = self._metric_rollups.get(bucket_id)
        if raw and raw.get("rollup_schema_version") == 2:
            rec = raw
        else:
            rec = {
                "bucket_id": bucket_id,
                "space_id": space_id,
                "project_tag": tag,
                "hour_str": hour_str,
                "rollup_schema_version": 2,
                "total_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "latency_histogram": {},
                "total_tokens": 0,
                "priced_run_count": 0,
                "unpriced_run_count": 0,
                "pricing_versions": [],
                "pricing_versions_truncated": False,
                "estimated_cost_usd": None,
                "failures_by_code": {},
            }
        rec["total_runs"] = (rec.get("total_runs") or 0) + 1
        if status in ("completed", RunStatus.COMPLETED, RunStatus.COMPLETED.value):
            rec["completed_runs"] = (rec.get("completed_runs") or 0) + 1
        elif status in ("failed", RunStatus.FAILED, RunStatus.FAILED.value):
            rec["failed_runs"] = (rec.get("failed_runs") or 0) + 1
            clean_code = normalize_failure_code(failure_code)
            f_codes = rec.setdefault("failures_by_code", {})
            f_codes[clean_code] = f_codes.get(clean_code, 0) + 1
        if duration_ms >= 0:
            b_key = get_histogram_bucket_key(duration_ms)
            hist = rec.setdefault("latency_histogram", {})
            hist[b_key] = hist.get(b_key, 0) + 1
        if tokens_used:
            rec["total_tokens"] = (rec.get("total_tokens") or 0) + tokens_used

        if estimated_cost_usd is not None and pricing_version:
            rec["priced_run_count"] = (rec.get("priced_run_count") or 0) + 1
            curr_cost = rec.get("estimated_cost_usd")
            base_cost = curr_cost if curr_cost is not None else 0.0
            rec["estimated_cost_usd"] = round(base_cost + estimated_cost_usd, 6)
            p_vers = rec.setdefault("pricing_versions", [])
            if pricing_version not in p_vers:
                if len(p_vers) < 5:
                    p_vers.append(pricing_version)
                else:
                    rec["pricing_versions_truncated"] = True
        else:
            rec["unpriced_run_count"] = (rec.get("unpriced_run_count") or 0) + 1
            # If cost was provided without version, track cost but record as unpriced/unversioned
            if estimated_cost_usd is not None:
                curr_cost = rec.get("estimated_cost_usd")
                base_cost = curr_cost if curr_cost is not None else 0.0
                rec["estimated_cost_usd"] = round(base_cost + estimated_cost_usd, 6)

        rec["updated_at"] = now.isoformat()
        self._metric_rollups[bucket_id] = rec

    def record_metric_rollup_event(
        self,
        space_id: str,
        project_tag: str,
        duration_ms: int,
        status: str,
        failure_code: Optional[str] = None,
        tokens_used: Optional[int] = None,
        estimated_cost_usd: Optional[float] = None,
        pricing_version: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        now = timestamp or datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            self._record_metric_rollup_event_locked(
                space_id=space_id,
                project_tag=project_tag,
                duration_ms=duration_ms,
                status=status,
                failure_code=failure_code,
                tokens_used=tokens_used,
                estimated_cost_usd=estimated_cost_usd,
                pricing_version=pricing_version,
                timestamp=now,
            )
            if self.persist_path:
                self._save_state_to_disk()

    def get_metric_rollups(
        self,
        space_id: str,
        start_time: datetime,
        end_time: datetime,
        project_tag: Optional[str] = None,
    ) -> List[SpaceHourlyMetricRollup]:
        start_hour = start_time.strftime("%Y-%m-%dT%H")
        end_hour = end_time.strftime("%Y-%m-%dT%H")
        res = []
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            for rec in self._metric_rollups.values():
                # Legacy data policy: strictly ignore v1 documents without schema version 2
                if rec.get("rollup_schema_version") != 2:
                    continue
                if rec.get("space_id") == space_id:
                    if project_tag and project_tag != "all" and rec.get("project_tag") != project_tag:
                        continue
                    h = rec.get("hour_str", "")
                    if start_hour <= h <= end_hour:
                        res.append(SpaceHourlyMetricRollup.model_validate(rec))
        res.sort(key=lambda x: x.hour_str)
        return res

    def save_diagnosis(self, diagnosis: DiagnosisRecord) -> None:
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            self._diagnoses[diagnosis.diagnosis_id] = diagnosis.model_dump(mode="json")
            self._run_diagnoses[(diagnosis.space_id, diagnosis.run_id)] = diagnosis.diagnosis_id
            if self.persist_path:
                self._save_state_to_disk()

    def get_diagnosis(self, space_id: str, run_id: str) -> Optional[DiagnosisRecord]:
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            diag_id = self._run_diagnoses.get((space_id, run_id))
            if not diag_id:
                run = self.runs.get(run_id)
                if run and run.space_id == space_id and getattr(run, "latest_diagnosis_id", None):
                    diag_id = run.latest_diagnosis_id
            if diag_id and diag_id in self._diagnoses:
                return DiagnosisRecord.model_validate(self._diagnoses[diag_id])
            return None

    def atomic_commit_diagnosis(
        self,
        space_id: str,
        run_id: str,
        diagnosis: DiagnosisRecord,
        expected_generation: int,
        expected_trace_id: Optional[str] = None,
        expected_diagnosis_revision: Optional[int] = None,
    ) -> bool:
        """
        Workflow-side-effect-free atomic commit of DiagnosisRecord and Run diagnosis pointer.
        Raises StorageConflictError if run does not exist, space does not match,
        telemetry_generation differs, trace_id differs, or diagnosis_revision differs.
        Only mutates diagnosis record, latest_diagnosis_id, latest_diagnosis_status, and agent_diagnosis.
        Never alters run.status, approval_gate, action, or artifact state.
        """
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                return False
            curr_gen = getattr(run, "telemetry_generation", 1) or 1
            if curr_gen != expected_generation:
                return False
            if expected_trace_id is not None:
                if run.trace_id != expected_trace_id or diagnosis.trace_id != expected_trace_id:
                    return False
            curr_rev = getattr(run, "diagnosis_revision", 0) or 0
            if expected_diagnosis_revision is not None and curr_rev != expected_diagnosis_revision:
                return False

            # Monotonic revision increment
            run.diagnosis_revision = curr_rev + 1
            run.latest_diagnosis_id = diagnosis.diagnosis_id
            run.latest_diagnosis_status = diagnosis.diagnostic_status
            if not getattr(run, "agent_diagnosis", None) and diagnosis.error_summary:
                run.agent_diagnosis = (
                    f"🚨 **Grafana MCP Telemetry Diagnosis (Trace `{run.trace_id}`)**\n\n"
                    f"• **Status**: `{diagnosis.diagnostic_status}`\n"
                    f"• **Faulting Span**: `{diagnosis.faulting_span.name if diagnosis.faulting_span else 'N/A'}`\n"
                    f"• **Root Cause**: {diagnosis.error_summary}\n"
                )
            run.updated_at = datetime.now(UTC)

            self._diagnoses[diagnosis.diagnosis_id] = diagnosis.model_dump(mode="json")
            self._run_diagnoses[(space_id, run_id)] = diagnosis.diagnosis_id
            self.runs[run_id] = run
            if self.persist_path:
                self._save_state_to_disk()
            return True

    def atomic_commit_diagnosis_with_claim(
        self,
        space_id: str,
        run_id: str,
        diagnosis: DiagnosisRecord,
        expected_generation: int,
        lease_owner: str,
        lease_token: str,
        expected_trace_id: Optional[str] = None,
        expected_diagnosis_revision: Optional[int] = None,
    ) -> bool:
        """
        Atomically commits DiagnosisRecord, updates Run diagnosis pointer & revision,
        AND transitions the DiagnosisClaimRecord from RUNNING to COMPLETED in a single atomic transaction.
        Strictly verifies:
          1. Run exists and space matches
          2. telemetry_generation, trace_id, diagnosis_revision match
          3. DiagnosisClaimRecord exists
          4. claim.status == RUNNING
          5. claim.lease_owner == lease_owner
          6. claim.lease_token == lease_token
          7. claim.lease_until is not None and claim.lease_until > now
        """
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                return False
            curr_gen = getattr(run, "telemetry_generation", 1) or 1
            if curr_gen != expected_generation:
                return False
            if expected_trace_id is not None:
                if run.trace_id != expected_trace_id or diagnosis.trace_id != expected_trace_id:
                    return False
            curr_rev = getattr(run, "diagnosis_revision", 0) or 0
            if expected_diagnosis_revision is not None and curr_rev != expected_diagnosis_revision:
                return False

            # Verify Claim Lease
            claim_dict = self._diagnosis_claims.get((space_id, run_id))
            if not claim_dict:
                return False
            claim = DiagnosisClaimRecord.model_validate(claim_dict)
            if claim.status != DiagnosisClaimStatus.RUNNING.value:
                return False
            if claim.lease_owner != lease_owner:
                return False
            if claim.lease_token != lease_token:
                return False
            if not claim.lease_until or claim.lease_until <= now:
                return False

            # Commit Diagnosis and update Run pointer/revision
            committed = self.atomic_commit_diagnosis(
                space_id=space_id,
                run_id=run_id,
                diagnosis=diagnosis,
                expected_generation=expected_generation,
                expected_trace_id=expected_trace_id,
                expected_diagnosis_revision=expected_diagnosis_revision,
            )
            if not committed:
                return False

            # Transition claim to COMPLETED
            claim.status = DiagnosisClaimStatus.COMPLETED.value
            claim.diagnosis_id = diagnosis.diagnosis_id
            claim.lease_until = None
            claim.updated_at = now
            self._diagnosis_claims[(space_id, run_id)] = claim.model_dump(mode="json")

            if self.persist_path:
                self._save_state_to_disk()
            return True

    def fail_run_diagnosis_claim(
        self,
        space_id: str,
        run_id: str,
        lease_owner: str,
        lease_token: str,
    ) -> bool:
        """
        Safely marks diagnosis claim as FAILED, strictly conditioned on lease ownership and token.
        """
        return self.complete_run_diagnosis_claim(
            space_id=space_id,
            run_id=run_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            status=DiagnosisClaimStatus.FAILED.value,
        )

    def claim_run_diagnosis(
        self,
        space_id: str,
        run_id: str,
        expected_generation: int,
        lease_owner: str,
        lease_duration_sec: float = 15.0,
        expected_trace_id: Optional[str] = None,
        schema_version: int = 1,
    ) -> Tuple[bool, Optional[DiagnosisClaimRecord], Optional[DiagnosisRecord]]:
        """
        Atomically attempts to claim execution rights for diagnosing a run across Cloud Run instances.
        Returns: (claimed_by_me, claim_record, completed_diagnosis_if_any)
        """
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                raise StorageConflictError("RUN_NOT_FOUND_OR_SPACE_MISMATCH")
            curr_gen = getattr(run, "telemetry_generation", 1) or 1
            if curr_gen != expected_generation:
                raise StorageConflictError("GENERATION_STALE")
            if expected_trace_id is not None and getattr(run, "trace_id", None) != expected_trace_id:
                raise StorageConflictError("TRACE_ID_MISMATCH")

            claim_dict = self._diagnosis_claims.get((space_id, run_id))
            is_same_generation = False
            if claim_dict:
                claim = DiagnosisClaimRecord.model_validate(claim_dict)
                is_same_generation = (
                    claim.telemetry_generation == expected_generation
                    and (expected_trace_id is None or claim.trace_id is None or claim.trace_id == expected_trace_id)
                    and claim.schema_version == schema_version
                )
                if is_same_generation:
                    if claim.status == DiagnosisClaimStatus.COMPLETED.value and claim.diagnosis_id:
                        diag = self.get_diagnosis(space_id, run_id)
                        if (
                            diag
                            and diag.diagnosis_id == claim.diagnosis_id
                            and diag.telemetry_generation == expected_generation
                            and (expected_trace_id is None or diag.trace_id == expected_trace_id)
                        ):
                            return False, claim, diag
                    if claim.status == DiagnosisClaimStatus.RUNNING.value and claim.lease_until and claim.lease_until > now:
                        return False, claim, None

            fresh_token = secrets.token_hex(16)
            new_claim = DiagnosisClaimRecord(
                space_id=space_id,
                run_id=run_id,
                trace_id=expected_trace_id or getattr(run, "trace_id", None),
                telemetry_generation=expected_generation,
                schema_version=schema_version,
                status=DiagnosisClaimStatus.RUNNING.value,
                lease_owner=lease_owner,
                lease_token=fresh_token,
                lease_until=now + timedelta(seconds=lease_duration_sec),
                attempts=(claim_dict.get("attempts", 0) + 1) if (claim_dict and is_same_generation) else 1,
                created_at=now,
                updated_at=now,
            )
            self._diagnosis_claims[(space_id, run_id)] = new_claim.model_dump(mode="json")
            if self.persist_path:
                self._save_state_to_disk()
            return True, new_claim, None

    def complete_run_diagnosis_claim(
        self,
        space_id: str,
        run_id: str,
        lease_owner: str,
        lease_token: str,
        diagnosis_id: Optional[str] = None,
        status: str = "completed",
    ) -> bool:
        """
        Atomically transitions diagnosis claim to COMPLETED or FAILED.
        Strictly verifies status==RUNNING, lease_owner, lease_token, and unexpired lease_until.
        """
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            claim_dict = self._diagnosis_claims.get((space_id, run_id))
            if not claim_dict:
                return False
            claim = DiagnosisClaimRecord.model_validate(claim_dict)
            if claim.status != DiagnosisClaimStatus.RUNNING.value:
                return False
            if claim.lease_owner != lease_owner:
                return False
            if claim.lease_token != lease_token:
                return False
            if not claim.lease_until or claim.lease_until <= now:
                return False

            claim.status = status
            claim.diagnosis_id = diagnosis_id
            claim.lease_until = None
            claim.updated_at = now
            self._diagnosis_claims[(space_id, run_id)] = claim.model_dump(mode="json")
            if self.persist_path:
                self._save_state_to_disk()
            return True

    def check_and_record_dual_rate_limit(
        self,
        space_id: str,
        user_id: str,
        user_limit: int = 10,
        space_limit: int = 30,
        window_seconds: float = 60.0,
    ) -> Tuple[bool, int]:
        """
        Atomic dual sliding-window rate limiter for User and Space.
        Ensures continuous 60s windows strictly allow <= user_limit and <= space_limit.
        Checked atomically so Space rejection does not consume User quota.
        """
        now_ts = time.time()
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            cutoff = now_ts - window_seconds
            u_key = f"user_{hashlib.sha256(f'{space_id}:{user_id}'.encode()).hexdigest()[:24]}"
            s_key = f"space_{hashlib.sha256(space_id.encode()).hexdigest()[:24]}"

            u_list = [t for t in self._sliding_rate_limits.get(u_key, []) if t > cutoff]
            s_list = [t for t in self._sliding_rate_limits.get(s_key, []) if t > cutoff]

            if len(u_list) >= user_limit:
                oldest = min(u_list)
                retry_after = max(1, int(oldest + window_seconds - now_ts + 0.999))
                return False, retry_after

            if len(s_list) >= space_limit:
                oldest = min(s_list)
                retry_after = max(1, int(oldest + window_seconds - now_ts + 0.999))
                return False, retry_after

            u_list.append(now_ts)
            s_list.append(now_ts)
            self._sliding_rate_limits[u_key] = u_list
            self._sliding_rate_limits[s_key] = s_list
            if self.persist_path:
                self._save_state_to_disk()
            return True, 0

    def check_and_record_rate_limit(
        self,
        key: str,
        limit: int = 10,
        window_seconds: float = 60.0,
    ) -> Tuple[bool, int]:
        """
        Thread-safe and process-safe sliding-window rate limiter for an arbitrary key.
        Returns (allowed: bool, retry_after_seconds: int).
        """
        now_ts = time.time()
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            cutoff = now_ts - window_seconds
            k_hash = hashlib.sha256(key.encode()).hexdigest()[:32]
            timestamps = [t for t in self._sliding_rate_limits.get(k_hash, []) if t > cutoff]

            if len(timestamps) >= limit:
                oldest = min(timestamps)
                retry_after = max(1, int(oldest + window_seconds - now_ts + 0.999))
                return False, retry_after

            timestamps.append(now_ts)
            self._sliding_rate_limits[k_hash] = timestamps
            if self.persist_path:
                self._save_state_to_disk()
            return True, 0

    def list_runs_for_metrics(
        self,
        space_id: str,
        since: datetime,
        project_tag: Optional[str] = None,
        limit: int = 500,
    ) -> List[Run]:
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            matching = [
                r for r in self.runs.values()
                if r.space_id == space_id
                and (not project_tag or project_tag == "all" or r.project_tag == project_tag)
                and (
                    (r.created_at and r.created_at >= since)
                    or (r.updated_at and r.updated_at >= since)
                )
            ]
            matching.sort(key=lambda x: x.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
            return matching[:limit]


    def reconcile_run_approval_atomic(
        self,
        space_id: str,
        run_id: str,
        target_status: RunStatus,
        approval_commit_status: str,
        gate_status: str,
        expected_run_status: Optional[RunStatus] = None,
        failure_code: Optional[str] = None,
        error_summary: Optional[str] = None,
        cleanup_items: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, Optional[Run]]:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                return False, None

            # Idempotent return if already in target status and approval_commit_status
            if run.status == target_status and getattr(run, "approval_commit_status", None) == approval_commit_status:
                return True, run.model_copy(deep=True)

            if expected_run_status and run.status != expected_run_status:
                return False, None

            # TOCTOU authoritative artifact & file publication check inside lock
            deliverable_ids = list(dict.fromkeys((run.output_artifact_ids or []) + ([run.manifest_file_id] if run.manifest_file_id else [])))
            if target_status == RunStatus.COMPLETED:
                for art_id in deliverable_ids:
                    art = self.artifacts.get(art_id)
                    f_rec = self.files.get(art_id)
                    if not (art and getattr(art, "visibility", None) == "published"):
                        return False, None
                    if not (f_rec and getattr(f_rec, "publication_status", None) == "published"):
                        return False, None
            elif target_status == RunStatus.FAILED:
                if deliverable_ids:
                    all_published = all(
                        (art := self.artifacts.get(aid)) and getattr(art, "visibility", None) == "published"
                        and (f_rec := self.files.get(aid)) and getattr(f_rec, "publication_status", None) == "published"
                        for aid in deliverable_ids
                    )
                    if all_published:
                        # Already published; cannot abort
                        return False, None

            run.status = target_status
            run.approval_commit_status = approval_commit_status
            run.uncertain_since = None
            run.state_version = (getattr(run, "state_version", 1) or 1) + 1
            run.updated_at = now

            if run.approval_gate:
                run.approval_gate.status = gate_status
                run.approval_gate.decided_at = now
                run.approval_gate.decision_lease_token = None
                run.approval_gate.decision_lease_until = None

            if target_status == RunStatus.FAILED:
                run.failure_code = failure_code or "APPROVAL_RECONCILE_ABORTED"
                run.is_retryable = True
                run.error_summary = error_summary

            # Synchronize linked ActionExecution
            if run.action_id and run.action_id in self.action_executions:
                act_exec = self.action_executions[run.action_id]
                if target_status == RunStatus.COMPLETED:
                    act_exec.status = ActionExecutionStatus.COMPLETED
                elif target_status == RunStatus.FAILED:
                    act_exec.status = ActionExecutionStatus.FAILED
                    act_exec.failure_code = failure_code or "APPROVAL_RECONCILE_ABORTED"
                act_exec.state_version = (getattr(act_exec, "state_version", 1) or 1) + 1
                act_exec.updated_at = now

            # Atomic Outbox Event
            evt_type = (
                ActivityEventType.RUN_COMPLETED
                if target_status == RunStatus.COMPLETED
                else ActivityEventType.RUN_FAILED
            )
            ev_id = f"run.reconciled.{target_status.value}:{run.run_id}:v{run.state_version}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=evt_type,
                space_id=space_id,
                project_tags=[run.project_tag or "general"],
                resource_type="run",
                resource_id=run.run_id,
                summary=f"Run approval reconciled as {target_status.value}",
                details={"run_id": run.run_id, "status": target_status.value, "approval_commit_status": approval_commit_status},
                actor_uid=run.created_by,
                created_at=now,
            )
            outbox_id = f"outbox:{ev.event_id}"
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            self.activity_outbox[outbox_id] = outbox_item
            if space_id not in self.activity_events:
                self.activity_events[space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[space_id]):
                self.activity_events[space_id].append(ev)

            # Atomic Cleanup Intents for Abort branch - Canonical ID only
            if target_status == RunStatus.FAILED and cleanup_items:
                for c_item in cleanup_items:
                    art_id = c_item["artifact_id"]
                    fn = c_item.get("filename", f"{art_id}.bin")
                    reason = c_item.get("reason", "APPROVAL_TRANSACTION_ABORTED")
                    ver = c_item.get("staging_version", run.state_version)
                    canonical_id = f"cleanup:{art_id}:{ver}"
                    job_data = {
                        "job_id": canonical_id,
                        "space_id": space_id,
                        "artifact_id": art_id,
                        "filename": fn,
                        "reason": reason,
                        "status": "pending",
                        "retries": 0,
                        "max_retries": 5,
                        "created_at": now,
                        "staging_version": ver,
                    }
                    self.artifact_cleanups[canonical_id] = dict(job_data)

            if self.persist_path:
                self._save_state_to_disk()
            return True, run.model_copy(deep=True)

    # -------------------------------------------------------------------------
    # Action Execution & Artifact Operations
    # -------------------------------------------------------------------------
    def create_action_execution_if_absent(
        self, execution: ActionExecutionRecord
    ) -> Tuple[bool, ActionExecutionRecord]:
        """
        Atomic Create-If-Absent for Action Execution.
        Returns (True, execution) if successfully created.
        Returns (False, existing_execution) if already exists (idempotent duplicate).
        """
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            if execution.action_id in self.action_executions:
                return False, self.action_executions[execution.action_id].model_copy(deep=True)
            self.action_executions[execution.action_id] = execution.model_copy(deep=True)
            if self.persist_path:
                self._save_state_to_disk()
            return True, execution.model_copy(deep=True)

    def get_action_execution(self, action_id: str) -> Optional[ActionExecutionRecord]:
        with self._lock:
            rec = self.action_executions.get(action_id)
            return rec.model_copy(deep=True) if rec else None

    def save_action_execution_fenced(
        self,
        execution: ActionExecutionRecord,
        expected_version: int,
        expected_owner: Optional[str] = None,
        expected_token: Optional[str] = None,
    ) -> ActionExecutionRecord:
        """
        Atomic Compare-And-Swap (CAS) write protected by expected_version, expected_owner, and lease token.
        Raises StorageConflictError on version, lease owner or token mismatch.
        """
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            curr = self.action_executions.get(execution.action_id)
            if not curr:
                raise StorageConflictError(f"ActionExecution '{execution.action_id}' does not exist")
            if curr.state_version != expected_version:
                raise StorageConflictError(
                    f"Version mismatch for ActionExecution '{execution.action_id}': expected {expected_version}, got {curr.state_version}"
                )
            if expected_owner is not None and curr.lease_owner != expected_owner:
                raise StorageConflictError(
                    f"Lease owner mismatch for ActionExecution '{execution.action_id}': expected {expected_owner}, got {curr.lease_owner}"
                )
            if expected_token is not None and curr.lease_token != expected_token:
                raise StorageConflictError(
                    f"Lease token mismatch for ActionExecution '{execution.action_id}': expected {expected_token}, got {curr.lease_token}"
                )
            if curr.lease_until is not None and curr.lease_until < now and execution.status == ActionExecutionStatus.RUNNING:
                raise StorageConflictError(
                    f"Active lease expired for ActionExecution '{execution.action_id}'"
                )

            execution.state_version = curr.state_version + 1
            execution.updated_at = now
            self.action_executions[execution.action_id] = execution.model_copy(deep=True)
            if self.persist_path:
                self._save_state_to_disk()
            return execution.model_copy(deep=True)

    def save_artifact_record(self, artifact: ArtifactDescriptor) -> ArtifactDescriptor:
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            self.artifacts[artifact.artifact_id] = artifact.model_copy(deep=True)
            if self.persist_path:
                self._save_state_to_disk()
            return artifact.model_copy(deep=True)

    def get_artifact(self, space_id: str, artifact_id: str) -> Optional[ArtifactDescriptor]:
        with self._lock:
            art = self.artifacts.get(artifact_id)
            if art and art.space_id == space_id:
                return art.model_copy(deep=True)
            return None

    def save_artifact_blob(self, space_id: str, artifact_id: str, filename: str, data: bytes) -> str:
        with self._lock:
            self.artifact_blobs[(space_id, artifact_id)] = data
            from app.core.config import settings
            if getattr(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", None) == "gcs":
                try:
                    from google.cloud import storage as gcs
                    client = gcs.Client()
                    bucket_name = settings.STUDIO_TOWER_GCS_BUCKET or "agentic-cinema-demo-2026-studiotower-artifacts"
                    bucket = client.bucket(bucket_name)
                    clean_name = re.sub(r"[^\w\.-]", "_", filename)
                    art_suffix = artifact_id[4:] if artifact_id.startswith("art_") else artifact_id
                    gcs_path = f"{space_id}/files/file_art_{art_suffix}_{clean_name}"
                    b = bucket.blob(gcs_path)
                    b.upload_from_string(data)
                except Exception as e:
                    logger.warning("GCS artifact blob save failed: %s", e)

            storage_dir = os.path.join(tempfile.gettempdir(), "studiotower_artifacts", space_id, artifact_id)
            os.makedirs(storage_dir, exist_ok=True)
            disk_path = os.path.join(storage_dir, filename)
            with open(disk_path, "wb") as f:
                f.write(data)
            return disk_path

    def get_artifact_blob(self, space_id: str, artifact_id: str, filename: str) -> Optional[bytes]:
        with self._lock:
            if (space_id, artifact_id) in self.artifact_blobs:
                return self.artifact_blobs[(space_id, artifact_id)]
            disk_path = os.path.join(tempfile.gettempdir(), "studiotower_artifacts", space_id, artifact_id, filename)
            if os.path.exists(disk_path):
                with open(disk_path, "rb") as f:
                    return f.read()

            from app.core.config import settings
            if getattr(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", None) == "gcs":
                try:
                    from google.cloud import storage as gcs
                    client = gcs.Client()
                    bucket_name = settings.STUDIO_TOWER_GCS_BUCKET or "agentic-cinema-demo-2026-studiotower-artifacts"
                    bucket = client.bucket(bucket_name)
                    clean_name = re.sub(r"[^\w\.-]", "_", filename)
                    art_suffix = artifact_id[4:] if artifact_id.startswith("art_") else artifact_id
                    candidates = [
                        f"{space_id}/files/file_art_{art_suffix}_{clean_name}",
                        f"{space_id}/files/{artifact_id}_{clean_name}",
                        f"{space_id}/files/file_{artifact_id}_{clean_name}",
                        f"{space_id}/artifacts/{artifact_id}/{filename}",
                        f"{space_id}/artifacts/{artifact_id}/{clean_name}",
                    ]
                    for cand in candidates:
                        b = bucket.blob(cand)
                        if b.exists():
                            data = b.download_as_bytes()
                            self.artifact_blobs[(space_id, artifact_id)] = data
                            return data

                    # Bucket search matching artifact_id
                    blobs = bucket.list_blobs(prefix=f"{space_id}/")
                    for b in blobs:
                        if artifact_id in b.name or art_suffix in b.name:
                            data = b.download_as_bytes()
                            self.artifact_blobs[(space_id, artifact_id)] = data
                            return data
                except Exception as e:
                    logger.warning("GCS artifact blob fetch failed: %s", e)

            return None

    def delete_artifact_blob(self, space_id: str, artifact_id: str, filename: str) -> bool:
        with self._lock:
            self.artifact_blobs.pop((space_id, artifact_id), None)
            disk_path = os.path.join(tempfile.gettempdir(), "studiotower_artifacts", space_id, artifact_id, filename)
            if os.path.exists(disk_path):
                try:
                    os.remove(disk_path)
                    return True
                except Exception:
                    return False
            return True

    def claim_action_dispatch(
        self, action_id: str, lease_token: str, lease_seconds: int = 30
    ) -> Tuple[bool, Optional[ActionExecutionRecord], str]:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.action_executions.get(action_id)
            if not rec:
                return False, None, "NOT_FOUND"

            if rec.dispatch_status == DispatchStatus.DISPATCHED:
                return False, rec.model_copy(deep=True), "ALREADY_DISPATCHED"

            was_expired_dispatching = (rec.dispatch_status == DispatchStatus.DISPATCHING)
            was_confirmation_pending = (rec.dispatch_status == DispatchStatus.DISPATCH_CONFIRMATION_PENDING)
            if rec.dispatch_status == DispatchStatus.DISPATCHING:
                if rec.dispatch_lease_until and rec.dispatch_lease_until > now:
                    return False, rec.model_copy(deep=True), "ACTIVE_DISPATCH_HELD"

            if rec.dispatch_status in (DispatchStatus.FAILED, DispatchStatus.DISPATCH_CONFIRMATION_PENDING):
                if rec.next_dispatch_at and rec.next_dispatch_at > now:
                    return False, rec.model_copy(deep=True), "BACKOFF_ACTIVE"

            rec.dispatch_status = DispatchStatus.DISPATCHING
            rec.dispatch_lease_token = lease_token
            rec.dispatch_lease_until = now + timedelta(seconds=lease_seconds)
            if not (was_expired_dispatching or was_confirmation_pending):
                rec.dispatch_generation += 1
            rec.updated_at = now
            if self.persist_path:
                self._save_state_to_disk()
            return True, rec.model_copy(deep=True), "CLAIMED"

    def mark_dispatch_confirmation_uncertain(
        self, action_id: str, lease_token: str, expected_version: int, task_name: str, error: str = ""
    ) -> bool:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.action_executions.get(action_id)
            if not rec:
                return False
            if rec.dispatch_status != DispatchStatus.DISPATCHING:
                return False
            if rec.dispatch_lease_token != lease_token:
                return False
            if rec.dispatch_lease_until is None or rec.dispatch_lease_until <= now:
                return False
            if rec.dispatch_version != expected_version:
                return False

            rec.dispatch_status = DispatchStatus.DISPATCH_CONFIRMATION_PENDING
            rec.dispatch_lease_token = None
            rec.dispatch_lease_until = None
            rec.task_name = task_name
            rec.dispatch_version += 1
            rec.failure_code = error or "DISPATCH_CONFIRMATION_UNCERTAIN"
            rec.next_dispatch_at = now
            rec.updated_at = now
            if self.persist_path:
                self._save_state_to_disk()
            return True

    def record_dispatch_success(
        self, action_id: str, lease_token: str, expected_version: int, task_name: str
    ) -> bool:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.action_executions.get(action_id)
            if not rec:
                return False
            if rec.dispatch_status != DispatchStatus.DISPATCHING:
                return False
            if rec.dispatch_lease_token != lease_token:
                return False
            if rec.dispatch_lease_until is None or rec.dispatch_lease_until <= now:
                return False
            if rec.dispatch_version != expected_version:
                return False

            rec.dispatch_status = DispatchStatus.DISPATCHED
            rec.dispatch_lease_token = None
            rec.dispatch_lease_until = None
            rec.task_name = task_name
            rec.dispatch_version += 1
            rec.updated_at = now
            if self.persist_path:
                self._save_state_to_disk()
            return True

    def record_dispatch_failure(
        self, action_id: str, lease_token: str, expected_version: int, error: str, is_retryable: bool = True, task_name: Optional[str] = None
    ) -> bool:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.action_executions.get(action_id)
            if not rec:
                return False
            if rec.dispatch_status != DispatchStatus.DISPATCHING:
                return False
            if rec.dispatch_lease_token != lease_token:
                return False
            if rec.dispatch_lease_until is None or rec.dispatch_lease_until <= now:
                return False
            if rec.dispatch_version != expected_version:
                return False

            rec.dispatch_status = DispatchStatus.FAILED
            rec.dispatch_lease_token = None
            rec.dispatch_lease_until = None
            if task_name:
                rec.task_name = task_name
            rec.dispatch_attempts += 1
            rec.dispatch_version += 1
            backoff_secs = min(300, 2 ** rec.dispatch_attempts)
            rec.next_dispatch_at = now + timedelta(seconds=backoff_secs)
            rec.failure_code = error
            rec.updated_at = now
            if self.persist_path:
                self._save_state_to_disk()
            return True

    def claim_action_execution(
        self, action_id: str, worker_id: str, lease_duration_seconds: int = 120
    ) -> Tuple[bool, Optional[ActionExecutionRecord], str]:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.action_executions.get(action_id)
            if not rec:
                return False, None, "NOT_FOUND"

            if rec.status in (ActionExecutionStatus.COMPLETED, ActionExecutionStatus.AWAITING_APPROVAL):
                return False, rec.model_copy(deep=True), "TERMINAL_NOOP"

            if rec.status == ActionExecutionStatus.RUNNING:
                if rec.lease_until and rec.lease_until > now:
                    return False, rec.model_copy(deep=True), "ACTIVE_LEASE_HELD"
                else:
                    if rec.attempts >= rec.max_attempts:
                        rec.status = ActionExecutionStatus.FAILED
                        rec.failure_code = "ERR_LEASE_EXPIRED"
                        rec.updated_at = now
                        if self.persist_path:
                            self._save_state_to_disk()
                        return False, rec.model_copy(deep=True), "TERMINAL_EXPIRED"

                    rec.lease_owner = worker_id
                    rec.lease_token = uuid.uuid4().hex
                    rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
                    rec.attempts += 1
                    rec.state_version += 1
                    rec.updated_at = now
                    if self.persist_path:
                        self._save_state_to_disk()
                    return True, rec.model_copy(deep=True), "RECLAIMED"

            if rec.status == ActionExecutionStatus.FAILED:
                if rec.is_retryable and rec.attempts < rec.max_attempts and (rec.next_retry_at is None or rec.next_retry_at <= now):
                    rec.status = ActionExecutionStatus.RUNNING
                    rec.lease_owner = worker_id
                    rec.lease_token = uuid.uuid4().hex
                    rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
                    rec.attempts += 1
                    rec.state_version += 1
                    rec.failure_code = None
                    rec.updated_at = now
                    if self.persist_path:
                        self._save_state_to_disk()
                    return True, rec.model_copy(deep=True), "RETRY_CLAIMED"
                return False, rec.model_copy(deep=True), "TERMINAL_FAILED"

            if rec.status == ActionExecutionStatus.PENDING:
                rec.status = ActionExecutionStatus.RUNNING
                rec.lease_owner = worker_id
                rec.lease_token = uuid.uuid4().hex
                rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
                rec.attempts += 1
                rec.state_version += 1
                rec.updated_at = now
                if self.persist_path:
                    self._save_state_to_disk()
                return True, rec.model_copy(deep=True), "CLAIMED"

            return False, rec.model_copy(deep=True), "UNKNOWN_STATUS"

    def heartbeat_action_execution(
        self, action_id: str, worker_id: str, lease_token: str, extension_seconds: int = 60
    ) -> Tuple[bool, int]:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.action_executions.get(action_id)
            if not rec:
                return False, 0
            if rec.status != ActionExecutionStatus.RUNNING:
                return False, rec.state_version
            if rec.lease_owner != worker_id or rec.lease_token != lease_token:
                return False, rec.state_version
            if rec.lease_until is None or rec.lease_until <= now:
                return False, rec.state_version

            rec.lease_until = now + timedelta(seconds=extension_seconds)
            rec.state_version += 1
            rec.updated_at = now
            if self.persist_path:
                self._save_state_to_disk()
            return True, rec.state_version

    def list_stalled_action_executions(self, space_id: Optional[str] = None) -> List[ActionExecutionRecord]:
        now = datetime.now(UTC)
        with self._lock:
            stalled = []
            for rec in self.action_executions.values():
                if space_id and rec.space_id != space_id:
                    continue
                if rec.status == ActionExecutionStatus.RUNNING:
                    if rec.lease_until is not None and rec.lease_until < now:
                        stalled.append(rec.model_copy(deep=True))
            return stalled

    def list_undispatched_actions(self, space_id: Optional[str] = None, timeout_seconds: int = 30) -> List[ActionExecutionRecord]:
        now = datetime.now(UTC)
        threshold = now - timedelta(seconds=timeout_seconds)
        with self._lock:
            undispatched = []
            for rec in self.action_executions.values():
                if space_id and rec.space_id != space_id:
                    continue
                if rec.status == ActionExecutionStatus.PENDING:
                    if rec.dispatch_status == DispatchStatus.FAILED and (rec.next_dispatch_at is None or rec.next_dispatch_at <= now):
                        undispatched.append(rec.model_copy(deep=True))
                    elif rec.dispatch_status == DispatchStatus.DISPATCHING and (rec.dispatch_lease_until is None or rec.dispatch_lease_until < now):
                        undispatched.append(rec.model_copy(deep=True))
                    elif rec.dispatch_status == DispatchStatus.PENDING and rec.created_at <= threshold:
                        undispatched.append(rec.model_copy(deep=True))
            return undispatched

    def reclaim_action_execution_lease(
        self,
        action_id: str,
        lease_token: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
        is_terminal: bool = False,
        failure_code: Optional[str] = None,
    ) -> bool:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            rec = self.action_executions.get(action_id)
            if not rec:
                return False
            if lease_token and rec.lease_token != lease_token:
                return False
            if not is_terminal and rec.lease_until and rec.lease_until > now:
                return False

            if is_terminal:
                rec.status = ActionExecutionStatus.FAILED
                rec.failure_code = failure_code or "ERR_LEASE_EXPIRED"
                rec.lease_owner = None
                rec.lease_token = None
                rec.lease_until = None
                rec.updated_at = now
                linked_run = self.runs.get(rec.run_id)
                if linked_run and linked_run.status == RunStatus.RUNNING:
                    linked_run.status = RunStatus.FAILED
                    linked_run.failure_code = failure_code or "ERR_LEASE_EXPIRED"
                    linked_run.updated_at = now
            else:
                rec.status = ActionExecutionStatus.PENDING
                rec.lease_owner = None
                rec.lease_token = None
                rec.lease_until = None
                rec.next_retry_at = next_retry_at or now
                rec.updated_at = now

            if self.persist_path:
                self._save_state_to_disk()
            return True

    def approve_gate_and_publish_artifacts_atomic(
        self,
        space_id: str,
        run_id: str,
        approver_uid: str,
        output_artifact_ids: List[str],
        plan: Optional[dict] = None,
        decision_lease_token: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> Tuple[Run, Optional[ActionExecutionRecord]]:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                raise StorageConflictError(f"Run '{run_id}' not found in space '{space_id}'")

            # Predicate: RUNNING and gate status strictly approving
            if run.status != RunStatus.RUNNING:
                raise StorageConflictError(f"GATE_ALREADY_DECIDED: Run status is {run.status}")
            if not run.approval_gate or run.approval_gate.status != "approving":
                raise StorageConflictError(f"GATE_NOT_APPROVING: Gate status is {run.approval_gate.status if run.approval_gate else 'None'}, expected approving")
            if not decision_lease_token or not run.approval_gate.decision_lease_token or run.approval_gate.decision_lease_token != decision_lease_token:
                raise StorageConflictError("DECISION_TOKEN_MISMATCH: Decision lease token is missing or expired")

            # Resolve action_id strictly from run
            target_act_id = run.action_id or action_id
            exec_rec = None
            if target_act_id:
                exec_rec = self.action_executions.get(target_act_id)
                if not exec_rec or exec_rec.space_id != space_id:
                    raise StorageConflictError(f"ActionExecution '{target_act_id}' not found in space '{space_id}'")

            # Target artifacts union: existing run artifacts + new output artifacts
            deliverable_artifact_ids = list(dict.fromkeys((run.output_artifact_ids or []) + output_artifact_ids))
            target_artifact_ids = list(deliverable_artifact_ids)
            if run.manifest_file_id and run.manifest_file_id not in target_artifact_ids:
                if run.manifest_file_id.startswith("art_") or not run.manifest_file_id.startswith("file_"):
                    target_artifact_ids.append(run.manifest_file_id)

            # Validate all artifacts in union strictly pending_approval
            for art_id in target_artifact_ids:
                art = self.artifacts.get(art_id)
                if not art:
                    raise StorageConflictError(f"Artifact '{art_id}' not found")
                if art.space_id != space_id:
                    raise StorageConflictError(f"Artifact '{art_id}' space mismatch")
                if art.run_id != run_id:
                    raise StorageConflictError(f"Artifact '{art_id}' run mismatch")
                if art.visibility != "pending_approval":
                    raise StorageConflictError(f"Artifact '{art_id}' has invalid visibility: {art.visibility}, expected pending_approval")

            # All validations passed -> Atomically execute all mutations
            run.status = RunStatus.COMPLETED
            if run.approval_gate:
                run.approval_gate.status = "approved"
                run.approval_gate.approved_by = approver_uid
                run.approval_gate.decided_at = now
            if plan:
                run.plan = plan
            run.output_artifact_ids = deliverable_artifact_ids
            run.failure_code = None
            run.error_summary = None
            run.state_version = (getattr(run, "state_version", 1) or 1) + 1
            run.updated_at = now

            if exec_rec:
                exec_rec.status = ActionExecutionStatus.COMPLETED
                exec_rec.output_artifact_ids = deliverable_artifact_ids
                exec_rec.updated_at = now

            for art_id in target_artifact_ids:
                art = self.artifacts.get(art_id)
                if art:
                    art.visibility = "published"
                file_rec = self.files.get(art_id)
                if file_rec:
                    file_rec.publication_status = "published"
                if art_id.startswith("art_"):
                    mapped_rec = self.files.get(f"file_art_{art_id[4:]}")
                    if mapped_rec:
                        mapped_rec.publication_status = "published"

            if run.manifest_file_id:
                m_rec = self.files.get(run.manifest_file_id)
                if m_rec:
                    m_rec.publication_status = "published"

            ev = ActivityEvent(
                space_id=space_id,
                project_tag=run.project_tag or "general",
                event_type=ActivityEventType.RUN_COMPLETED,
                resource_type="run",
                resource_id=run_id,
                summary=f"Run '{run.prompt or run_id}' completed and artifacts published by approval",
                actor_uid=approver_uid,
                details={"run_id": run_id, "action_id": target_act_id, "artifacts": target_artifact_ids},
            )
            # Deterministic Outbox ID
            outbox_id = f"outbox:run.completed:{run_id}"
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=f"run.completed:{run_id}",
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            self.activity_outbox[outbox_id] = outbox_item
            if space_id not in self.activity_events:
                self.activity_events[space_id] = []
            if not any(e.event_id == ev.event_id for e in self.activity_events[space_id]):
                self.activity_events[space_id].append(ev)

            if self.persist_path:
                self._save_state_to_disk()
            return run.model_copy(deep=True), exec_rec.model_copy(deep=True) if exec_rec else None

    def reject_gate_and_fail_artifacts_atomic(
        self,
        space_id: str,
        run_id: str,
        approver_uid: str,
        rejection_reason: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> Tuple[Run, Optional[ActionExecutionRecord]]:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            run = self.runs.get(run_id)
            if not run or run.space_id != space_id:
                raise StorageConflictError(f"Run '{run_id}' not found in space '{space_id}'")
            if run.status != RunStatus.AWAITING_APPROVAL:
                raise StorageConflictError(f"ACTIVE_DECISION_HELD: Cannot reject run in status '{run.status}'")

            target_act_id = run.action_id or action_id
            exec_rec = None
            if target_act_id:
                exec_rec = self.action_executions.get(target_act_id)
                if not exec_rec or exec_rec.space_id != space_id:
                    raise StorageConflictError(f"ActionExecution '{target_act_id}' not found in space '{space_id}'")

            target_artifact_ids = list(
                dict.fromkeys((run.output_artifact_ids or []) + ([run.manifest_file_id] if run.manifest_file_id else []))
            )
            for art_id in target_artifact_ids:
                art = self.artifacts.get(art_id)
                if art and (art.space_id != space_id or art.run_id != run_id):
                    raise StorageConflictError(f"Artifact '{art_id}' ownership mismatch")

            run.status = RunStatus.FAILED
            if run.approval_gate:
                run.approval_gate.status = "rejected"
                run.approval_gate.approved_by = approver_uid
                run.approval_gate.decided_at = now
            run.failure_code = "GATE_REJECTED"
            run.error_summary = rejection_reason or "Approval gate rejected by reviewer"
            run.state_version = (getattr(run, "state_version", 1) or 1) + 1
            run.updated_at = now

            if exec_rec:
                exec_rec.status = ActionExecutionStatus.FAILED
                exec_rec.failure_code = "GATE_REJECTED"
                exec_rec.updated_at = now

            for art_id in target_artifact_ids:
                art = self.artifacts.get(art_id)
                if art:
                    art.visibility = "rejected"
                file_rec = self.files.get(art_id)
                if file_rec:
                    file_rec.publication_status = "rejected"

            rej_ev = ActivityEvent(
                space_id=space_id,
                project_tag=run.project_tag or "general",
                event_type=ActivityEventType.RUN_FAILED,
                resource_type="gate",
                resource_id=run.approval_gate.gate_id if run.approval_gate else run_id,
                summary=f"Approval gate rejected for '{run.prompt or run_id}'",
                actor_uid=approver_uid,
                details={"run_id": run_id, "action_id": target_act_id, "reason": rejection_reason},
            )
            # Deterministic Outbox ID
            outbox_id = f"outbox_gate_rej_{run_id}_{run.state_version}_rejected"
            self.activity_outbox[outbox_id] = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=rej_ev.event_id,
                space_id=space_id,
                event=rej_ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )

            if self.persist_path:
                self._save_state_to_disk()
            return run.model_copy(deep=True), exec_rec.model_copy(deep=True) if exec_rec else None

    def claim_artifact_cleanup_job(
        self, job_id: str, worker_id: str, lease_seconds: int = 60
    ) -> Optional[dict]:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            job = self.artifact_cleanups.get(job_id)
            if not job or job.get("status") != "pending":
                return None
            lease_until_str = job.get("lease_until")
            if lease_until_str:
                lease_until = datetime.fromisoformat(lease_until_str)
                if lease_until > now and job.get("lease_owner") != worker_id:
                    return None
            next_retry_str = job.get("next_retry_at")
            if next_retry_str:
                next_retry = datetime.fromisoformat(next_retry_str)
                if next_retry > now:
                    return None

            # Mutual exclusion check: if artifact was already published, cancel job instead of claiming!
            art_id = job.get("artifact_id")
            if art_id:
                art = self.artifacts.get(art_id)
                f_rec = self.files.get(art_id)
                if (art and getattr(art, "visibility", None) == "published") or (f_rec and getattr(f_rec, "publication_status", None) == "published"):
                    job["status"] = "cancelled"
                    job["reason"] = "ALREADY_PUBLISHED"
                    if self.persist_path:
                        self._save_state_to_disk()
                    return None

            lease_token = f"clean_tok_{uuid.uuid4().hex[:8]}"
            job["status"] = "in_progress"
            job["lease_owner"] = worker_id
            job["lease_token"] = lease_token
            job["lease_until"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            job["version"] = job.get("version", 1) + 1

            # Atomically transition artifact/file to cleaning to block concurrent publishing
            if art_id:
                art = self.artifacts.get(art_id)
                if art and getattr(art, "visibility", None) != "published":
                    art.visibility = "cleaning"
                f_rec = self.files.get(art_id)
                if f_rec and getattr(f_rec, "publication_status", None) != "published":
                    f_rec.publication_status = "cleaning"

            if self.persist_path:
                self._save_state_to_disk()
            return dict(job)

    def record_cleanup_failure(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        max_retries: int = 5,
        lease_token: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> bool:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            job = self.artifact_cleanups.get(job_id)
            if not job:
                return False
            if not worker_id or not job.get("lease_owner") or job.get("lease_owner") != worker_id:
                return False
            if not lease_token or not job.get("lease_token") or job.get("lease_token") != lease_token:
                return False
            if expected_version is not None and job.get("version") != expected_version:
                return False
            lease_until_str = job.get("lease_until")
            if not lease_until_str:
                return False
            try:
                lease_until = datetime.fromisoformat(lease_until_str)
                if lease_until <= now:
                    return False
            except Exception:
                return False
            retries = job.get("retries", 0) + 1
            job["retries"] = retries
            job["status"] = "pending" if retries < max_retries else "failed"
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_until"] = None
            job["last_error"] = error
            job["version"] = job.get("version", 1) + 1
            if retries < max_retries:
                job["next_retry_at"] = (now + timedelta(seconds=min(300, 2 ** retries))).isoformat()

            # Revert cleaning state back to pending_approval on failure
            art_id = job.get("artifact_id")
            if art_id:
                art = self.artifacts.get(art_id)
                if art and getattr(art, "visibility", None) == "cleaning":
                    art.visibility = "pending_approval"
                f_rec = self.files.get(art_id)
                if f_rec and getattr(f_rec, "publication_status", None) == "cleaning":
                    f_rec.publication_status = "pending_approval"

            if self.persist_path:
                self._save_state_to_disk()
            return True

    def enqueue_artifact_cleanup(
        self, space_id: str, artifact_id: str, filename: str, reason: str, staging_version: int = 1
    ) -> str:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            canonical_job_id = f"cleanup:{artifact_id}:{staging_version}"
            job_data = {
                "job_id": canonical_job_id,
                "space_id": space_id,
                "artifact_id": artifact_id,
                "filename": filename,
                "reason": reason,
                "status": "pending",
                "version": 1,
                "staging_version": staging_version,
                "expected_publication_status": "pending_approval",
                "retries": 0,
                "next_retry_at": now.isoformat(),
                "created_at": now.isoformat(),
            }
            self.artifact_cleanups[canonical_job_id] = job_data
            if self.persist_path:
                self._save_state_to_disk()
            return canonical_job_id

    def list_pending_artifact_cleanups(self) -> List[dict]:
        with self._lock:
            seen = set()
            pending = []
            for j in self.artifact_cleanups.values():
                jid = j.get("job_id")
                if jid not in seen and j.get("status") == "pending":
                    seen.add(jid)
                    pending.append(j)
            return pending

    def complete_artifact_cleanup(
        self,
        job_id: str,
        worker_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> bool:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            job = self.artifact_cleanups.get(job_id)
            if not job:
                return False
            if worker_id is not None:
                if not job.get("lease_owner") or job.get("lease_owner") != worker_id:
                    return False
                if not lease_token or not job.get("lease_token") or job.get("lease_token") != lease_token:
                    return False
                if expected_version is not None and job.get("version") != expected_version:
                    return False
                lease_until_str = job.get("lease_until")
                if not lease_until_str:
                    return False
                try:
                    lease_until = datetime.fromisoformat(lease_until_str)
                    if lease_until <= now:
                        return False
                except Exception:
                    return False
            job["status"] = "completed"
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_until"] = None
            job["version"] = job.get("version", 1) + 1
            job["updated_at"] = now.isoformat()

            # Mark cleaned artifact as purged and file as failed
            art_id = job.get("artifact_id")
            if art_id:
                art = self.artifacts.get(art_id)
                if art and getattr(art, "visibility", None) == "cleaning":
                    art.visibility = "purged"
                f_rec = self.files.get(art_id)
                if f_rec and getattr(f_rec, "publication_status", None) == "cleaning":
                    f_rec.publication_status = "failed"

            if self.persist_path:
                self._save_state_to_disk()
            return True

    def cancel_artifact_cleanup_job(self, job_id: str, reason: str = "ALREADY_PUBLISHED") -> bool:
        now = datetime.now(UTC)
        with self._lock, file_lock(self.persist_path):
            self._sync_read_latest_from_disk()
            job = self.artifact_cleanups.get(job_id)
            if not job:
                return False
            job["status"] = "cancelled"
            job["reason"] = reason
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_until"] = None
            job["version"] = job.get("version", 1) + 1
            job["updated_at"] = now.isoformat()

            art_id = job.get("artifact_id")
            if art_id:
                art = self.artifacts.get(art_id)
                if art and getattr(art, "visibility", None) == "cleaning":
                    art.visibility = "pending_approval"
                f_rec = self.files.get(art_id)
                if f_rec and getattr(f_rec, "publication_status", None) == "cleaning":
                    f_rec.publication_status = "pending_approval"

            if self.persist_path:
                self._save_state_to_disk()
            return True

    def delete_sandbox_cascade(self, space_id: str) -> dict:
        """
        Durably deletes an ephemeral sandbox space and cascades deletion to all associated entities:
        memberships, invites, runs, messages, files, chunks, artifacts, diagnoses, claims, and metrics.
        Fails closed with CleanupAuthorizationError if space is not an authorized sandbox.
        """
        with self._lock:
            space = self.spaces.get(space_id)
            if not space:
                return {"deleted": False, "reason": "not_found", "space_id": space_id}
            if not getattr(space, "is_sandbox", False):
                raise CleanupAuthorizationError(
                    f"Space '{space_id}' is not an ephemeral sandbox space. Cascade deletion is strictly forbidden on standard spaces."
                )

            # Space cleanup_status marks as deleting
            space.cleanup_status = "deleting"

            # Cascade delete memberships
            del_memberships = []
            for k, v in self.memberships.items():
                if isinstance(k, tuple) and len(k) >= 1 and k[0] == space_id:
                    del_memberships.append(k)
                elif isinstance(k, str) and (k.startswith(f"{space_id}_") or (isinstance(v, dict) and v.get("space_id") == space_id)):
                    del_memberships.append(k)
                elif isinstance(v, dict) and v.get("space_id") == space_id:
                    del_memberships.append(k)
            for k in del_memberships:
                self.memberships.pop(k, None)

            # Cascade delete invites
            del_invites = [k for k, v in self.invites.items() if getattr(v, "space_id", None) == space_id]
            for k in del_invites:
                self.invites.pop(k, None)

            # Cascade delete messages
            del_messages = [k for k, v in self.messages.items() if getattr(v, "space_id", None) == space_id]
            for k in del_messages:
                self.messages.pop(k, None)

            # Cascade delete files and blobs
            del_files = [k for k, v in self.files.items() if getattr(v, "space_id", None) == space_id]
            for k in del_files:
                self.files.pop(k, None)
                self.file_blobs.pop(k, None)

            # Cascade delete document chunks
            del_chunks = [k for k, v in self.document_chunks.items() if getattr(v, "space_id", None) == space_id]
            for k in del_chunks:
                self.document_chunks.pop(k, None)

            # Cascade delete runs
            del_runs = [k for k, v in self.runs.items() if getattr(v, "space_id", None) == space_id]
            for k in del_runs:
                self.runs.pop(k, None)

            # Cascade delete action executions
            del_actions = [k for k, v in self.action_executions.items() if getattr(v, "space_id", None) == space_id]
            for k in del_actions:
                self.action_executions.pop(k, None)

            # Cascade delete artifacts
            del_artifacts = [k for k, v in self.artifacts.items() if getattr(v, "space_id", None) == space_id]
            for k in del_artifacts:
                self.artifacts.pop(k, None)
                self.artifact_blobs.pop((space_id, k), None)
                self.artifact_blobs.pop(k, None)
                self.artifact_cleanups.pop(k, None)
            del_art_blobs = [k for k in self.artifact_blobs.keys() if (isinstance(k, tuple) and len(k) >= 1 and k[0] == space_id)]
            for k in del_art_blobs:
                self.artifact_blobs.pop(k, None)

            # Cascade delete diagnoses and claims
            del_diagnoses = [k for k, v in self._diagnoses.items() if getattr(v, "space_id", None) == space_id]
            for k in del_diagnoses:
                self._diagnoses.pop(k, None)

            del_run_diag = [k for k in self._run_diagnoses.keys() if (isinstance(k, str) and k.startswith(f"{space_id}:")) or (isinstance(k, tuple) and k and k[0] == space_id)]
            for k in del_run_diag:
                self._run_diagnoses.pop(k, None)

            del_claims = [k for k in self._diagnosis_claims.keys() if (isinstance(k, str) and k.startswith(f"{space_id}:")) or (isinstance(k, tuple) and k and k[0] == space_id)]
            for k in del_claims:
                self._diagnosis_claims.pop(k, None)

            # Cascade delete metrics rollups
            del_rollups = [k for k in self._metric_rollups.keys() if (isinstance(k, str) and k.startswith(f"{space_id}:")) or (isinstance(k, tuple) and k and k[0] == space_id)]
            for k in del_rollups:
                self._metric_rollups.pop(k, None)

            # Cascade delete pending generation cleanups
            del_pending_gen = [k for k, v in self.pending_generation_cleanups.items() if getattr(v, "space_id", None) == space_id or (isinstance(v, dict) and v.get("space_id") == space_id)]
            for k in del_pending_gen:
                self.pending_generation_cleanups.pop(k, None)

            # Cascade delete telemetry scan cursor
            self._telemetry_scan_cursors.pop(f"scope_{space_id}", None)

            # Cascade delete activity events and outbox
            self.activity_events.pop(space_id, None)
            del_outbox = [k for k, v in self.activity_outbox.items() if getattr(v, "space_id", None) == space_id or (isinstance(v, dict) and v.get("space_id") == space_id)]
            for k in del_outbox:
                self.activity_outbox.pop(k, None)

            # Cascade delete chat idempotency
            del_idemp = [
                k for k, v in self.chat_idempotency.items()
                if (isinstance(k, str) and k.startswith(f"{space_id}:"))
                or (isinstance(k, tuple) and len(k) >= 1 and k[0] == space_id)
                or getattr(v, "space_id", None) == space_id
            ]
            for k in del_idemp:
                self.chat_idempotency.pop(k, None)

            # Finally pop space document
            self.spaces.pop(space_id, None)

            if self.persist_path:
                self._save_state_to_disk()

            return {
                "deleted": True,
                "space_id": space_id,
                "cascaded": {
                    "spaces": 1,
                    "memberships": len(del_memberships),
                    "invites": len(del_invites),
                    "messages": len(del_messages),
                    "files": len(del_files),
                    "chunks": len(del_chunks),
                    "runs": len(del_runs),
                    "actions": len(del_actions),
                    "artifacts": len(del_artifacts),
                    "diagnoses": len(del_diagnoses),
                    "rollups": len(del_rollups),
                    "pending_gen_cleanups": len(del_pending_gen),
                    "outbox": len(del_outbox),
                },
            }

    def sweep_expired_sandboxes(self, now_dt: datetime, limit: int = 50) -> int:
        """Sweeps and cascade deletes up to `limit` expired ephemeral sandbox spaces."""
        with self._lock:
            expired_ids = [
                s.space_id for s in self.spaces.values()
                if getattr(s, "is_sandbox", False)
                and getattr(s, "sandbox_expires_at", None)
                and s.sandbox_expires_at <= now_dt
            ][:limit]

        swept = 0
        for sid in expired_ids:
            try:
                res = self.delete_sandbox_cascade(sid)
                if res.get("deleted"):
                    swept += 1
            except Exception as e:
                logger.warning("Error sweeping sandbox %s: %s", sid, e)
        return swept

    def get_sandbox_cleanup_job(self, space_id: str) -> Optional[dict]:
        with self._lock:
            job = getattr(self, "_sandbox_cleanup_jobs", {}).get(space_id)
            if job:
                return dict(job)
            space = self.spaces.get(space_id)
            if space:
                return {"space_id": space_id, "phase": "pending", "status": "pending"}
            return {"space_id": space_id, "phase": "completed", "status": "completed"}

    def verify_sandbox_empty(self, space_id: str) -> dict:
        with self._lock:
            space = self.spaces.get(space_id)
            if space:
                return {"empty": False, "reason": "space_exists", "space_id": space_id}

            counts = {}
            for k in self.memberships:
                if (isinstance(k, tuple) and k[0] == space_id) or (isinstance(k, str) and k.startswith(f"{space_id}_")):
                    counts["memberships"] = counts.get("memberships", 0) + 1
            for k, v in self.invites.items():
                if getattr(v, "space_id", None) == space_id:
                    counts["invites"] = counts.get("invites", 0) + 1
            for k, v in self.messages.items():
                if getattr(v, "space_id", None) == space_id:
                    counts["messages"] = counts.get("messages", 0) + 1
            for k, v in self.files.items():
                if getattr(v, "space_id", None) == space_id:
                    counts["files"] = counts.get("files", 0) + 1
            for k, v in self.runs.items():
                if getattr(v, "space_id", None) == space_id:
                    counts["runs"] = counts.get("runs", 0) + 1
            for k, v in self.artifacts.items():
                if getattr(v, "space_id", None) == space_id:
                    counts["artifacts"] = counts.get("artifacts", 0) + 1

            if any(counts.values()):
                return {"empty": False, "remaining_counts": counts, "space_id": space_id}
            return {"empty": True, "space_id": space_id}

    def clear(self):
        """Used in test teardowns."""
        with self._lock:
            self.users.clear()
            self.spaces.clear()
            self.memberships.clear()
            self.invites.clear()
            self.messages.clear()
            self.files.clear()
            self.file_blobs.clear()
            self.runs.clear()
            self.chat_idempotency.clear()
            self.document_chunks.clear()
            self.action_executions.clear()
            self.artifacts.clear()
            self.artifact_blobs.clear()
            self.artifact_cleanups.clear()
            self._telemetry_scan_cursors.clear()
            self._metric_rollups.clear()
            self._diagnoses.clear()
            self._run_diagnoses.clear()
            self._diagnosis_claims.clear()
            self._sliding_rate_limits.clear()
            if self.persist_path and os.path.exists(self.persist_path):
                try:
                    os.remove(self.persist_path)
                except Exception:
                    pass
            lock_path = f"{self.persist_path}.lock" if self.persist_path else None
            if lock_path and os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                except Exception:
                    pass


class FirestoreStore(MemoryStore):
    """
    Cloud Firestore production storage backend with strict transactional mutations and full remote queries.
    Fails closed if client cannot be initialized or transaction fails.
    """

    def __init__(self, project_id: Optional[str] = None, client=None):
        super().__init__()
        self.project_id = project_id
        if client is not None:
            self.client = client
        else:
            try:
                from google.cloud import firestore
                self.client = firestore.Client(project=project_id if project_id else None)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to initialize Google Cloud Firestore backend: {e}. "
                    "Ensure google-cloud-firestore is installed and valid credentials/project_id are configured."
                )

    def save_user(self, user: User) -> User:
        super().save_user(user)
        self.client.collection("users").document(user.uid).set(user.model_dump(mode="json"))
        return user

    def get_user(self, uid: str) -> Optional[User]:
        doc = self.client.collection("users").document(uid).get()
        if doc.exists:
            return User.model_validate(doc.to_dict())
        return None

    def get_user_by_email(self, email: str) -> Optional[User]:
        clean_email = email.strip().lower()
        try:
            docs = self.client.collection("users").where("email", "==", clean_email).stream()
            for d in docs:
                return User.model_validate(d.to_dict())
        except Exception:
            pass
        return super().get_user_by_email(email)

    def search_users(self, query: str, limit: int = 10) -> List[User]:
        clean_q = query.strip().lower()
        if not clean_q:
            return []
        try:
            matched = []
            docs = self.client.collection("users").stream()
            for d in docs:
                u = User.model_validate(d.to_dict())
                if clean_q in u.email.lower() or clean_q in u.display_name.lower() or clean_q in u.uid.lower():
                    matched.append(u)
                    if len(matched) >= limit:
                        break
            if matched:
                return matched
        except Exception:
            pass
        return super().search_users(query, limit)

    def create_space(self, space: Space, creator_uid: str) -> Space:
        super().create_space(space, creator_uid)
        self.client.collection("spaces").document(space.space_id).set(space.to_storage_dict())
        mem_id = f"{space.space_id}_{creator_uid}"
        self.client.collection("memberships").document(mem_id).set({
            "space_id": space.space_id,
            "uid": creator_uid,
            "role": MembershipRole.OWNER.value,
        })
        return space

    def get_space(self, space_id: str) -> Optional[Space]:
        doc = self.client.collection("spaces").document(space_id).get()
        if doc.exists:
            return Space.model_validate(doc.to_dict())
        return None

    def list_spaces_for_user(self, uid: str) -> List[Space]:
        mem_query = self.client.collection("memberships").where("uid", "==", uid).stream()
        space_ids = [m.to_dict().get("space_id") for m in mem_query]
        spaces = []
        for sid in space_ids:
            if sid:
                s = self.get_space(sid)
                if s:
                    spaces.append(s)
        return spaces

    def is_member(self, space_id: str, uid: str) -> bool:
        mem_id = f"{space_id}_{uid}"
        doc = self.client.collection("memberships").document(mem_id).get()
        return doc.exists

    def get_member_role(self, space_id: str, uid: str) -> Optional[MembershipRole]:
        mem_id = f"{space_id}_{uid}"
        doc = self.client.collection("memberships").document(mem_id).get()
        if doc.exists:
            return MembershipRole(doc.to_dict().get("role", "member"))
        return None

    def count_space_members_by_role(self, space_id: str) -> Dict[MembershipRole, int]:
        mem_query = self.client.collection("memberships").where("space_id", "==", space_id).stream()
        counts = {role: 0 for role in MembershipRole}
        for m in mem_query:
            role_str = m.to_dict().get("role", "member")
            try:
                r = MembershipRole(role_str)
                counts[r] = counts.get(role_str, 0) + 1
            except Exception:
                counts[MembershipRole.MEMBER] += 1
        return counts

    def add_member(self, space_id: str, uid: str, role: MembershipRole = MembershipRole.MEMBER) -> None:
        super().add_member(space_id, uid, role)
        mem_id = f"{space_id}_{uid}"
        self.client.collection("memberships").document(mem_id).set({
            "space_id": space_id,
            "uid": uid,
            "role": role.value,
        })

    def list_members_in_space(self, space_id: str) -> List[Tuple[str, MembershipRole]]:
        mem_query = self.client.collection("memberships").where("space_id", "==", space_id).stream()
        results = []
        for m in mem_query:
            data = m.to_dict()
            uid = data.get("uid")
            role_str = data.get("role", "member")
            try:
                role = MembershipRole(role_str)
            except Exception:
                role = MembershipRole.MEMBER
            if uid:
                results.append((uid, role))
        return results

    def list_invites_for_space(self, space_id: str) -> List[Invite]:
        docs = self.client.collection("invites").where("space_id", "==", space_id).stream()
        now = datetime.now(UTC)
        invites = []
        for d in docs:
            inv = Invite.model_validate(d.to_dict())
            if inv.revoked_at is None and (not inv.expires_at or inv.expires_at > now) and inv.used_count < inv.max_uses:
                invites.append(inv)
        return invites

    def transfer_ownership_atomic(self, space_id: str, expected_current_owner_uid: str, new_owner_uid: str) -> bool:
        from google.cloud import firestore

        @firestore.transactional
        def _atomic_transfer(tx):
            old_mem_ref = self.client.collection("memberships").document(f"{space_id}_{expected_current_owner_uid}")
            new_mem_ref = self.client.collection("memberships").document(f"{space_id}_{new_owner_uid}")

            old_doc = old_mem_ref.get(transaction=tx)
            new_doc = new_mem_ref.get(transaction=tx)

            if not old_doc.exists or old_doc.to_dict().get("role") != MembershipRole.OWNER.value:
                raise ValueError("CALLER_NOT_CURRENT_OWNER")

            if not new_doc.exists:
                raise ValueError("NEW_OWNER_NOT_MEMBER")

            tx.update(new_mem_ref, {"role": MembershipRole.OWNER.value})
            tx.update(old_mem_ref, {"role": MembershipRole.ADMIN.value})

        try:
            tx = self.client.transaction()
            _atomic_transfer(tx)
            # Safely sync local cache without re-evaluating stale CAS
            with self._lock:
                self.memberships[(space_id, new_owner_uid)] = MembershipRole.OWNER
                self.memberships[(space_id, expected_current_owner_uid)] = MembershipRole.ADMIN
            return True
        except ValueError:
            raise
        except Exception as e:
            raise StorageUnavailableError(f"Atomic ownership transfer failed: {str(e)}") from e

    def remove_member(self, space_id: str, uid: str) -> bool:
        super().remove_member(space_id, uid)
        mem_id = f"{space_id}_{uid}"
        self.client.collection("memberships").document(mem_id).delete()
        return True

    def add_tag_to_space(self, space_id: str, tag: ProjectTag, actor_uid: Optional[str] = None) -> Optional[Space]:
        space_ref = self.client.collection("spaces").document(space_id)
        now = datetime.now(UTC)
        space_res = None
        from google.cloud import firestore

        @firestore.transactional
        def add_tag_tx(transaction):
            nonlocal space_res
            snapshot = space_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            s = Space.model_validate(snapshot.to_dict())
            tag.revision = getattr(tag, "revision", 1) or 1
            tag.updated_at = now
            existing_idx = next((i for i, t in enumerate(s.tags) if t.slug == tag.slug or t.id == tag.id), None)
            if existing_idx is not None:
                tag.revision = (getattr(s.tags[existing_idx], "revision", 1) or 1) + 1
                s.tags[existing_idx] = tag
                evt_type = ActivityEventType.TAG_UPDATED
            else:
                s.tags.append(tag)
                evt_type = ActivityEventType.TAG_CREATED
            
            # Atomic set space
            transaction.set(space_ref, s.model_dump(mode="json"))

            # Atomic stage outbox item in the SAME Firestore transaction
            ev_id = f"{evt_type.value}:{tag.id}:r{tag.revision}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=evt_type,
                space_id=space_id,
                project_tags=[tag.slug],
                resource_type="tag",
                resource_id=tag.id,
                summary=f"Tag '{tag.name}' {'updated' if existing_idx is not None else 'created'}",
                details={"tag_id": tag.id, "slug": tag.slug, "name": tag.name, "revision": tag.revision},
                actor_uid=actor_uid,
                created_at=now,
            )
            outbox_id = f"outbox:{ev.event_id}"
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            outbox_ref = self.client.collection("activity_outbox").document(outbox_id)
            transaction.set(outbox_ref, outbox_item.model_dump(mode="json"))
            act_ref = self.client.collection("activity_events").document(ev.event_id)
            transaction.set(act_ref, ev.model_dump(mode="json"))
            space_res = s
            return s

        try:
            tx = self.client.transaction()
            add_tag_tx(tx)
            with self._lock:
                if space_res:
                    self.spaces[space_id] = space_res
                    sp_evts = self.activity_events.setdefault(space_id, [])
                    if not any(e.event_id == ev.event_id for e in sp_evts):
                        sp_evts.append(ev)
            return space_res
        except Exception as e:
            logger.error(f"Firestore add_tag_to_space transaction error: {e}")
            raise

    def update_tag_in_space(
        self,
        space_id: str,
        tag_id_or_slug: str,
        name: Optional[str] = None,
        color: Optional[str] = None,
        description: Optional[str] = None,
        actor_uid: Optional[str] = None,
    ) -> Optional[Space]:
        space_ref = self.client.collection("spaces").document(space_id)
        now = datetime.now(UTC)
        space_res = None
        ev = None
        from google.cloud import firestore

        @firestore.transactional
        def update_tag_tx(transaction):
            nonlocal space_res, ev
            snapshot = space_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            s = Space.model_validate(snapshot.to_dict())
            target_slug = tag_id_or_slug.lower().strip()
            matched = next(
                (t for t in s.tags if t.slug == target_slug or getattr(t, "id", None) == tag_id_or_slug),
                None,
            )
            if not matched:
                return None
            if name is not None:
                matched.name = name.strip()
            if color is not None:
                matched.color = color.strip()
            if description is not None:
                matched.description = description.strip()
            matched.revision = (getattr(matched, "revision", 1) or 1) + 1
            matched.updated_at = now

            transaction.set(space_ref, s.model_dump(mode="json"))

            # Atomic stage outbox item
            ev_id = f"tag.updated:{matched.id}:r{matched.revision}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=ActivityEventType.TAG_UPDATED,
                space_id=space_id,
                project_tags=[matched.slug],
                resource_type="tag",
                resource_id=matched.id,
                summary=f"Tag '{matched.name}' updated",
                details={"tag_id": matched.id, "slug": matched.slug, "name": matched.name, "revision": matched.revision},
                actor_uid=actor_uid,
                created_at=now,
            )
            outbox_id = f"outbox:{ev.event_id}"
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            transaction.set(self.client.collection("activity_outbox").document(outbox_id), outbox_item.model_dump(mode="json"))
            transaction.set(self.client.collection("activity_events").document(ev.event_id), ev.model_dump(mode="json"))
            space_res = s
            return s

        try:
            tx = self.client.transaction()
            update_tag_tx(tx)
            with self._lock:
                if space_res:
                    self.spaces[space_id] = space_res
                    if ev:
                        sp_evts = self.activity_events.setdefault(space_id, [])
                        if not any(e.event_id == ev.event_id for e in sp_evts):
                            sp_evts.append(ev)
            return space_res
        except Exception as e:
            logger.error(f"Firestore update_tag_in_space transaction error: {e}")
            raise

    def archive_tag_in_space(
        self,
        space_id: str,
        tag_id_or_slug: str,
        archived: bool = True,
        actor_uid: Optional[str] = None,
    ) -> Optional[Space]:
        space_ref = self.client.collection("spaces").document(space_id)
        now = datetime.now(UTC)
        space_res = None
        ev = None
        from google.cloud import firestore

        @firestore.transactional
        def archive_tag_tx(transaction):
            nonlocal space_res, ev
            snapshot = space_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            s = Space.model_validate(snapshot.to_dict())
            target_slug = tag_id_or_slug.lower().strip()
            matched = next(
                (t for t in s.tags if t.slug == target_slug or getattr(t, "id", None) == tag_id_or_slug),
                None,
            )
            if not matched:
                return None
            matched.archived = archived
            matched.revision = (getattr(matched, "revision", 1) or 1) + 1
            matched.updated_at = now

            transaction.set(space_ref, s.model_dump(mode="json"))

            evt_type = ActivityEventType.TAG_ARCHIVED if archived else ActivityEventType.TAG_UNARCHIVED
            ev_id = f"{evt_type.value}:{matched.id}:r{matched.revision}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=evt_type,
                space_id=space_id,
                project_tags=[matched.slug],
                resource_type="tag",
                resource_id=matched.id,
                summary=f"Tag '{matched.name}' {'archived' if archived else 'restored'}",
                details={"tag_id": matched.id, "slug": matched.slug, "archived": archived, "revision": matched.revision},
                actor_uid=actor_uid,
                created_at=now,
            )
            outbox_id = f"outbox:{ev.event_id}"
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            transaction.set(self.client.collection("activity_outbox").document(outbox_id), outbox_item.model_dump(mode="json"))
            transaction.set(self.client.collection("activity_events").document(ev.event_id), ev.model_dump(mode="json"))
            space_res = s
            return s

        try:
            tx = self.client.transaction()
            archive_tag_tx(tx)
            with self._lock:
                if space_res:
                    self.spaces[space_id] = space_res
                    if ev:
                        sp_evts = self.activity_events.setdefault(space_id, [])
                        if not any(e.event_id == ev.event_id for e in sp_evts):
                            sp_evts.append(ev)
            return space_res
        except Exception as e:
            logger.error(f"Firestore archive_tag_in_space transaction error: {e}")
            raise

    def record_activity_event(self, event: ActivityEvent) -> ActivityEvent:
        doc_ref = self.client.collection("activity_events").document(event.event_id)
        try:
            doc_ref.create(event.model_dump(mode="json"))
        except Exception as e:
            err_str = str(e).lower()
            if "alreadyexists" in err_str or "already exists" in err_str or "409" in err_str:
                snap = doc_ref.get()
                if snap.exists:
                    existing = ActivityEvent.model_validate(snap.to_dict())
                    if (
                        existing.event_type == event.event_type
                        and existing.resource_id == event.resource_id
                        and existing.space_id == event.space_id
                    ):
                        with self._lock:
                            sp_evts = self.activity_events.setdefault(event.space_id, [])
                            if not any(e.event_id == existing.event_id for e in sp_evts):
                                sp_evts.append(existing)
                        return existing
                    raise StorageConflictError(
                        f"ACTIVITY_EVENT_ID_COLLISION: Event ID '{event.event_id}' already exists with differing payload in Firestore"
                    )
            raise e
        with self._lock:
            sp_evts = self.activity_events.setdefault(event.space_id, [])
            if not any(e.event_id == event.event_id for e in sp_evts):
                sp_evts.append(event)
        record_space_activity(event)
        return event

    def stage_outbox_event(self, event: ActivityEvent, tx=None) -> ActivityOutboxItem:
        deterministic_outbox_id = f"outbox:{event.event_id}"
        now = datetime.now(UTC)
        outbox_item = ActivityOutboxItem(
            outbox_id=deterministic_outbox_id,
            event_id=event.event_id,
            space_id=event.space_id,
            event=event,
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=5,
            created_at=now,
            updated_at=now,
            next_retry_at=now,
        )
        doc_ref = self.client.collection("activity_outbox").document(outbox_item.outbox_id)
        if tx is not None:
            tx.set(doc_ref, outbox_item.model_dump(mode="json"))
        else:
            doc_ref.set(outbox_item.model_dump(mode="json"))
            with self._lock:
                self.activity_outbox[outbox_item.outbox_id] = outbox_item
        return outbox_item

    def dispatch_outbox_events(self, limit: int = 50, worker_id: str = "firestore_dispatcher") -> int:
        now = datetime.now(UTC)
        dispatched_count = 0
        lease_seconds = 30
        try:
            candidates = list(
                self.client.collection("activity_outbox")
                .where("status", "in", [OutboxStatus.PENDING.value, OutboxStatus.IN_PROGRESS.value])
                .limit(limit)
                .stream()
            )
        except Exception as e:
            logger.error(f"Firestore dispatch_outbox_events query error: {e}")
            raise StorageUnavailableError(f"Firestore dispatch_outbox_events query failed: {e}") from e

        try:
            from google.cloud import firestore
            for d in candidates:
                data = d.to_dict()
                item = ActivityOutboxItem.model_validate(data)

                # Verify eligibility
                lease_until = item.lease_until
                if item.status == OutboxStatus.IN_PROGRESS and lease_until and lease_until > now:
                    continue
                if item.next_retry_at and item.next_retry_at > now:
                    continue
                if item.status in (OutboxStatus.PUBLISHED, OutboxStatus.FAILED):
                    continue

                # Transactional claim
                token = uuid.uuid4().hex
                doc_ref = d.reference

                @firestore.transactional
                def claim_tx(transaction):
                    snapshot = doc_ref.get(transaction=transaction)
                    if not snapshot.exists:
                        return False
                    curr = snapshot.to_dict()
                    curr_status = curr.get("status")
                    curr_lease_until = curr.get("lease_until")
                    if curr_status in (OutboxStatus.PUBLISHED.value, OutboxStatus.FAILED.value):
                        return False
                    if curr_status == OutboxStatus.IN_PROGRESS.value and curr_lease_until:
                        try:
                            l_dt = _parse_lease_timestamp_fail_closed(curr_lease_until)
                            if l_dt and l_dt > now:
                                return False
                        except Exception:
                            # Fail-closed on corrupted/unparseable lease timestamp
                            return False
                    transaction.update(doc_ref, {
                        "status": OutboxStatus.IN_PROGRESS.value,
                        "lease_owner": worker_id,
                        "lease_until": (now + timedelta(seconds=lease_seconds)).isoformat(),
                        "lease_token": token,
                        "attempts": curr.get("attempts", 0) + 1,
                        "updated_at": now.isoformat(),
                    })
                    return True

                tx = self.client.transaction()
                if not claim_tx(tx):
                    continue

                # Process event publication with transactional fencing on completion & failure
                try:
                    self.record_activity_event(item.event)

                    @firestore.transactional
                    def complete_tx(transaction):
                        snapshot = doc_ref.get(transaction=transaction)
                        if not snapshot.exists:
                            return False
                        curr = snapshot.to_dict()
                        if curr.get("status") != OutboxStatus.IN_PROGRESS.value:
                            return False
                        if curr.get("lease_owner") != worker_id or curr.get("lease_token") != token:
                            return False
                        curr_lease_until = curr.get("lease_until")
                        if curr_lease_until is None:
                            return False
                        try:
                            l_dt = _parse_lease_timestamp_fail_closed(curr_lease_until)
                            if not l_dt or l_dt <= datetime.now(UTC):
                                return False
                        except Exception:
                            return False
                        transaction.update(doc_ref, {
                            "status": OutboxStatus.PUBLISHED.value,
                            "lease_owner": None,
                            "lease_until": None,
                            "lease_token": None,
                            "updated_at": datetime.now(UTC).isoformat(),
                        })
                        return True

                    tx_done = self.client.transaction()
                    if complete_tx(tx_done):
                        dispatched_count += 1
                except Exception as e:
                    new_attempts = item.attempts + 1
                    is_failed = new_attempts >= item.max_attempts
                    backoff_delay = (2 ** min(new_attempts, 6)) * 5
                    next_retry = datetime.now(UTC) + timedelta(seconds=backoff_delay)

                    @firestore.transactional
                    def fail_tx(transaction):
                        snapshot = doc_ref.get(transaction=transaction)
                        if not snapshot.exists:
                            return False
                        curr = snapshot.to_dict()
                        if curr.get("status") != OutboxStatus.IN_PROGRESS.value:
                            return False
                        if curr.get("lease_owner") != worker_id or curr.get("lease_token") != token:
                            return False
                        curr_lease_until = curr.get("lease_until")
                        if curr_lease_until is None:
                            return False
                        try:
                            l_dt = _parse_lease_timestamp_fail_closed(curr_lease_until)
                            if not l_dt or l_dt <= datetime.now(UTC):
                                return False
                        except Exception:
                            return False
                        transaction.update(doc_ref, {
                            "status": OutboxStatus.FAILED.value if is_failed else OutboxStatus.PENDING.value,
                            "last_error": str(e),
                            "lease_owner": None,
                            "lease_until": None,
                            "lease_token": None,
                            "next_retry_at": next_retry.isoformat(),
                            "updated_at": datetime.now(UTC).isoformat(),
                        })
                        return True

                    tx_fail = self.client.transaction()
                    try:
                        fail_tx(tx_fail)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Firestore dispatch_outbox_events error: {e}")
            raise StorageUnavailableError(f"Firestore dispatch_outbox_events processing failed: {e}") from e
        return dispatched_count

    def list_activity_events(
        self,
        space_id: str,
        tag: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[List[ActivityEvent], Optional[str]]:
        from google.cloud import firestore
        query = self.client.collection("activity_events").where("space_id", "==", space_id)
        if tag and tag != "all":
            query = query.where("project_tags", "array_contains", tag)
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING).order_by("event_id", direction=firestore.Query.DESCENDING)

        if cursor:
            cursor_payload = decode_activity_cursor(cursor, space_id, tag)
            c_event_id = cursor_payload.get("event_id")
            if c_event_id:
                cursor_doc = self.client.collection("activity_events").document(c_event_id).get()
                if cursor_doc.exists:
                    query = query.start_after(cursor_doc)

        docs = list(query.limit(limit + 1).stream())
        has_more = len(docs) > limit
        page_docs = docs[:limit]
        items = [ActivityEvent.model_validate(d.to_dict()) for d in page_docs]
        next_cursor = None
        if has_more and page_docs:
            last_ev = items[-1]
            next_cursor = encode_activity_cursor({
                "created_at": last_ev.created_at.isoformat(),
                "event_id": last_ev.event_id,
                "space_id": space_id,
                "tag": tag or "",
            })
        return items, next_cursor

    def create_invite(self, invite: Invite) -> Invite:
        super().create_invite(invite)
        self.client.collection("invites").document(invite.token).set(invite.model_dump(mode="json"))
        return invite

    def get_invite(self, token: str) -> Optional[Invite]:
        doc = self.client.collection("invites").document(token).get()
        if doc.exists:
            return Invite.model_validate(doc.to_dict())
        return None

    def revoke_invite(self, token: str) -> Optional[Invite]:
        invite = self.get_invite(token)
        if invite:
            invite.revoked_at = datetime.now(UTC)
            self.client.collection("invites").document(token).set(invite.model_dump(mode="json"))
        return invite

    def accept_invite_and_join(self, token: str, user: User) -> Space:
        """
        Transactional invite consumption and membership creation directly on Cloud Firestore
        using Firestore Client Transaction without unhandled pre-reads.
        """
        try:
            from google.cloud import firestore
            invite_ref = self.client.collection("invites").document(token)
            transaction = self.client.transaction()

            @firestore.transactional
            def _atomic_join_in_tx(tx, inv_ref):
                # 1. Transactional Reads
                inv_snap = inv_ref.get(transaction=tx)
                if not inv_snap.exists:
                    raise ValueError("INVALID_INVITE")
                inv = Invite.model_validate(inv_snap.to_dict())

                if inv.revoked_at is not None or inv.used_count >= inv.max_uses:
                    raise ValueError("REVOKED_OR_EXHAUSTED")
                if inv.expires_at and inv.expires_at < datetime.now(UTC):
                    raise ValueError("EXPIRED")
                if inv.target_email and user.email.lower().strip() != inv.target_email:
                    raise ValueError("EMAIL_MISMATCH")

                space_id = inv.space_id
                sp_ref = self.client.collection("spaces").document(space_id)
                sp_snap = sp_ref.get(transaction=tx)
                if not sp_snap.exists:
                    raise ValueError("SPACE_NOT_FOUND")

                mem_id = f"{space_id}_{user.uid}"
                mem_ref = self.client.collection("memberships").document(mem_id)
                m_snap = mem_ref.get(transaction=tx)
                existing_role = MembershipRole(m_snap.to_dict()["role"]) if m_snap.exists else None

                # 2. Transactional Writes
                inv.used_count += 1
                if inv.used_count >= inv.max_uses:
                    inv.revoked_at = datetime.now(UTC)
                tx.set(inv_ref, inv.model_dump(mode="json"))

                if not existing_role or ROLE_PRECEDENCE.get(inv.role, 0) > ROLE_PRECEDENCE.get(existing_role, 0):
                    tx.set(mem_ref, {
                        "space_id": space_id,
                        "uid": user.uid,
                        "role": inv.role.value,
                    })

                return Space.model_validate(sp_snap.to_dict())

            return _atomic_join_in_tx(transaction, invite_ref)
        except Exception as e:
            if isinstance(e, ValueError):
                raise
            err_str = str(e).lower()
            if "aborted" in err_str or "conflict" in err_str or "already exists" in err_str:
                raise StorageConflictError("Concurrent transaction conflict on invitation. Please retry.") from e
            raise StorageUnavailableError("Distributed database is temporarily unavailable. Please retry shortly.") from e

    def add_message(self, message: Message) -> Message:
        msg_ref = self.client.collection("messages").document(message.message_id)
        now = datetime.now(UTC)
        from google.cloud import firestore

        tag = message.project_tag or "general"
        sender = message.sender_name or message.sender_uid or "User"
        preview = message.content[:60] + "..." if len(message.content) > 60 else message.content
        summary = f"Message sent by {sender}: {preview}"
        ev_id = f"message.created:{message.message_id}"
        ev = ActivityEvent(
            event_id=ev_id,
            event_type=ActivityEventType.MESSAGE_CREATED,
            space_id=message.space_id,
            project_tags=[tag],
            resource_type="message",
            resource_id=message.message_id,
            summary=summary,
            details={
                "message_id": message.message_id,
                "sender_uid": message.sender_uid,
                "sender_name": message.sender_name,
                "role": message.role.value if hasattr(message.role, "value") else str(message.role),
                "content_preview": preview,
                "attachment_count": len(getattr(message, "attachment_file_ids", []) or []),
            },
            actor_uid=message.sender_uid,
            created_at=now,
        )
        outbox_id = f"outbox:{ev.event_id}"
        outbox_item = ActivityOutboxItem(
            outbox_id=outbox_id,
            event_id=ev.event_id,
            space_id=message.space_id,
            event=ev,
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=5,
            created_at=now,
            updated_at=now,
            next_retry_at=now,
        )

        @firestore.transactional
        def add_msg_tx(transaction):
            snapshot = msg_ref.get(transaction=transaction)
            if not snapshot.exists:
                transaction.set(msg_ref, message.model_dump(mode="json"))
                outbox_ref = self.client.collection("activity_outbox").document(outbox_id)
                transaction.set(outbox_ref, outbox_item.model_dump(mode="json"))
                act_ref = self.client.collection("activity_events").document(ev.event_id)
                transaction.set(act_ref, ev.model_dump(mode="json"))

        try:
            tx = self.client.transaction()
            add_msg_tx(tx)
            with self._lock:
                space_msgs = self.messages.setdefault(message.space_id, [])
                if not any(m.message_id == message.message_id for m in space_msgs):
                    space_msgs.append(message)
                sp_evts = self.activity_events.setdefault(message.space_id, [])
                if not any(e.event_id == ev.event_id for e in sp_evts):
                    sp_evts.append(ev)
        except Exception as e:
            logger.error(f"Firestore add_message error: {e}")
            raise
        return message

    def list_messages(self, space_id: str, project_tag: Optional[str] = None) -> List[Message]:
        query = self.client.collection("messages").where("space_id", "==", space_id)
        if project_tag and project_tag != "all":
            query = query.where("project_tag", "==", project_tag)
        docs = query.stream()
        msgs = [Message.model_validate(d.to_dict()) for d in docs]
        return sorted(msgs, key=lambda m: (m.created_at or datetime.min.replace(tzinfo=UTC), m.message_id))

    def list_messages_page(
        self,
        space_id: str,
        project_tag: Optional[str] = None,
        limit: int = 30,
        before_cursor: Optional[str] = None,
    ) -> Tuple[List[Message], Optional[str], bool]:
        from google.cloud import firestore
        query = self.client.collection("messages").where("space_id", "==", space_id)
        if project_tag and project_tag != "all":
            query = query.where("project_tag", "==", project_tag)
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING).order_by("message_id", direction=firestore.Query.DESCENDING)

        if before_cursor:
            cursor_payload = decode_message_cursor(before_cursor, space_id, project_tag)
            c_msg_id = cursor_payload.get("message_id")
            if c_msg_id:
                cursor_doc = self.client.collection("messages").document(c_msg_id).get()
                if cursor_doc.exists:
                    query = query.start_after(cursor_doc)

        try:
            docs = list(query.limit(limit + 1).stream())
        except Exception:
            # Resilient fallback if composite index is building or missing in Firestore
            all_msgs = self.list_messages(space_id, project_tag=project_tag)
            if before_cursor:
                cursor_payload = decode_message_cursor(before_cursor, space_id, project_tag)
                c_msg_id = cursor_payload.get("message_id")
                idx = next((i for i, m in enumerate(all_msgs) if m.message_id == c_msg_id), None)
                if idx is not None:
                    all_msgs = all_msgs[:idx]
            has_more = len(all_msgs) > limit
            page_items = all_msgs[-limit:] if has_more else all_msgs
            next_cursor = None
            if has_more and page_items:
                oldest_in_page = page_items[0]
                next_cursor = encode_message_cursor({
                    "created_at": (oldest_in_page.created_at or datetime.min.replace(tzinfo=UTC)).isoformat(),
                    "message_id": oldest_in_page.message_id,
                    "space_id": space_id,
                    "tag": project_tag or "all",
                })
            return page_items, next_cursor, has_more
        has_more = len(docs) > limit
        page_docs = docs[:limit]
        items_desc = [Message.model_validate(d.to_dict()) for d in page_docs]
        next_cursor = None
        if has_more and items_desc:
            oldest_in_page = items_desc[-1]
            next_cursor = encode_message_cursor({
                "created_at": (oldest_in_page.created_at or datetime.min.replace(tzinfo=UTC)).isoformat(),
                "message_id": oldest_in_page.message_id,
                "space_id": space_id,
                "tag": project_tag or "all",
            })

        # Return ascending order for UI display
        items_asc = list(reversed(items_desc))
        return items_asc, next_cursor, has_more

    def get_message(self, message_id: str) -> Optional[Message]:
        doc = self.client.collection("messages").document(message_id).get()
        if doc.exists:
            return Message.model_validate(doc.to_dict())
        return None

    def get_message_by_client_id(
        self, space_id: str, client_message_id: str, sender_uid: Optional[str] = None
    ) -> Optional[Message]:
        query = (
            self.client.collection("messages")
            .where("space_id", "==", space_id)
            .where("client_message_id", "==", client_message_id)
        )
        if sender_uid:
            query = query.where("sender_uid", "==", sender_uid)
        docs = query.limit(1).stream()
        for d in docs:
            return Message.model_validate(d.to_dict())
        return None

    def get_user_message_by_client_id(
        self, space_id: str, sender_uid: str, client_message_id: str
    ) -> Optional[Message]:
        return self.get_message_by_client_id(space_id, client_message_id, sender_uid=sender_uid)

    def update_proposal_message_run_id(
        self, space_id: str, action_id: str, run_id: str
    ) -> Optional[Message]:
        super().update_proposal_message_run_id(space_id, action_id, run_id)
        try:
            query = self.client.collection("messages").where("space_id", "==", space_id)
            for doc in query.stream():
                d = doc.to_dict()
                proposed = d.get("proposed_action") or {}
                if proposed.get("action_id") == action_id:
                    doc.reference.update({"run_id": run_id})
                    d["run_id"] = run_id
                    return Message.model_validate(d)
        except Exception as e:
            logger.error(f"Firestore update_proposal_message_run_id error: {e}")
        return None

    # Chat Idempotency operations in Firestore
    def get_chat_idempotency(self, key: str) -> Optional[ChatIdempotencyRecord]:
        doc = self.client.collection("chat_idempotency").document(key).get()
        if doc.exists:
            return ChatIdempotencyRecord.model_validate(doc.to_dict())
        return None

    def acquire_chat_idempotency(
        self,
        key: str,
        space_id: str,
        sender_uid: str,
        client_message_id: str,
        payload_hash: str,
        lease_duration_seconds: int = 120,
        lease_owner: Optional[str] = None,
    ) -> Tuple[bool, Optional[ChatIdempotencyRecord]]:
        doc_ref = self.client.collection("chat_idempotency").document(key)
        now = datetime.now(UTC)
        owner = lease_owner or f"owner_{uuid.uuid4().hex[:12]}"
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _acquire_tx(tx, d_ref):
                snap = d_ref.get(transaction=tx)
                if snap.exists:
                    existing = ChatIdempotencyRecord.model_validate(snap.to_dict())
                    # Check if in-progress lease expired
                    if (
                        existing.status == ChatIdempotencyStatus.IN_PROGRESS
                        and existing.lease_until
                        and existing.lease_until < now
                    ):
                        existing.status = ChatIdempotencyStatus.IN_PROGRESS
                        existing.version += 1
                        existing.lease_owner = owner
                        existing.lease_until = now + timedelta(seconds=lease_duration_seconds)
                        existing.updated_at = now
                        tx.set(d_ref, existing.model_dump(mode="json"))
                        return True, existing
                    return False, existing

                record = ChatIdempotencyRecord(
                    key=key,
                    space_id=space_id,
                    sender_uid=sender_uid,
                    client_message_id=client_message_id,
                    payload_hash=payload_hash,
                    status=ChatIdempotencyStatus.IN_PROGRESS,
                    version=1,
                    lease_owner=owner,
                    lease_until=now + timedelta(seconds=lease_duration_seconds),
                )
                tx.set(d_ref, record.model_dump(mode="json"))
                return True, record

            acquired, rec = _acquire_tx(transaction, doc_ref)
            if acquired:
                super().acquire_chat_idempotency(
                    key, space_id, sender_uid, client_message_id, payload_hash, lease_duration_seconds=lease_duration_seconds, lease_owner=owner
                )
            return acquired, rec
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                existing = self.get_chat_idempotency(key)
                return False, existing
            raise StorageUnavailableError(f"Firestore idempotency acquire failed: {e}") from e

    def transition_chat_idempotency_status(
        self,
        key: str,
        expected_status: ChatIdempotencyStatus,
        new_status: ChatIdempotencyStatus,
        lease_duration_seconds: int = 120,
        lease_owner: Optional[str] = None,
    ) -> Tuple[bool, Optional[ChatIdempotencyRecord]]:
        doc_ref = self.client.collection("chat_idempotency").document(key)
        now = datetime.now(UTC)
        owner = lease_owner or f"owner_{uuid.uuid4().hex[:12]}"
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _transition_tx(tx, d_ref):
                snap = d_ref.get(transaction=tx)
                if not snap.exists:
                    return False, None
                rec = ChatIdempotencyRecord.model_validate(snap.to_dict())

                is_valid_transition = rec.status == expected_status or (
                    rec.status == ChatIdempotencyStatus.IN_PROGRESS
                    and rec.lease_until is not None
                    and rec.lease_until < now
                )
                if not is_valid_transition:
                    return False, rec

                rec.status = new_status
                rec.version += 1
                rec.updated_at = now
                if new_status == ChatIdempotencyStatus.IN_PROGRESS:
                    rec.lease_owner = owner
                    rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
                else:
                    rec.lease_owner = None
                    rec.lease_until = None

                tx.set(d_ref, rec.model_dump(mode="json"))
                return True, rec

            transitioned, rec = _transition_tx(transaction, doc_ref)
            if transitioned and rec:
                super().transition_chat_idempotency_status(
                    key, expected_status, new_status, lease_duration_seconds=lease_duration_seconds, lease_owner=owner
                )
            return transitioned, rec
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                existing = self.get_chat_idempotency(key)
                return False, existing
            raise StorageUnavailableError(f"Firestore idempotency transition failed: {e}") from e

    def update_chat_idempotency_fenced(
        self,
        key: str,
        expected_version: Optional[int] = None,
        *,
        expected_lease_owner: Optional[str] = None,
        status: Optional[ChatIdempotencyStatus] = None,
        user_message_id: Optional[str] = None,
        agent_message_id: Optional[str] = None,
        run_id: Optional[str] = None,
        error_status_code: Optional[int] = None,
        error_detail: Optional[str] = None,
        extend_lease_seconds: Optional[int] = None,
    ) -> Tuple[bool, Optional[ChatIdempotencyRecord]]:
        doc_ref = self.client.collection("chat_idempotency").document(key)
        now = datetime.now(UTC)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _fenced_update_tx(tx, d_ref):
                snap = d_ref.get(transaction=tx)
                if not snap.exists:
                    return False, None
                rec = ChatIdempotencyRecord.model_validate(snap.to_dict())

                if expected_lease_owner is not None and rec.lease_owner != expected_lease_owner:
                    return False, rec

                if expected_version is not None and rec.version != expected_version:
                    return False, rec

                if (
                    rec.status == ChatIdempotencyStatus.IN_PROGRESS
                    and status != ChatIdempotencyStatus.FAILED
                    and rec.lease_until is not None
                    and rec.lease_until < now
                ):
                    return False, rec

                if status is not None:
                    rec.status = status
                if user_message_id is not None:
                    rec.user_message_id = user_message_id
                if agent_message_id is not None:
                    rec.agent_message_id = agent_message_id
                if run_id is not None:
                    rec.run_id = run_id
                if error_status_code is not None:
                    rec.error_status_code = error_status_code
                if error_detail is not None:
                    rec.error_detail = error_detail

                rec.version += 1
                rec.updated_at = now
                if extend_lease_seconds is not None and rec.status == ChatIdempotencyStatus.IN_PROGRESS:
                    rec.lease_until = now + timedelta(seconds=extend_lease_seconds)
                elif rec.status in (ChatIdempotencyStatus.COMPLETED, ChatIdempotencyStatus.FAILED):
                    rec.lease_until = None
                    rec.lease_owner = None

                tx.set(d_ref, rec.model_dump(mode="json"))
                return True, rec

            updated, rec = _fenced_update_tx(transaction, doc_ref)
            if updated and rec:
                super().update_chat_idempotency_fenced(
                    key,
                    expected_version,
                    expected_lease_owner=expected_lease_owner,
                    status=status,
                    user_message_id=user_message_id,
                    agent_message_id=agent_message_id,
                    run_id=run_id,
                    error_status_code=error_status_code,
                    error_detail=error_detail,
                    extend_lease_seconds=extend_lease_seconds,
                )
            return updated, rec
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                existing = self.get_chat_idempotency(key)
                return False, existing
            raise StorageUnavailableError(f"Firestore fenced idempotency update failed: {e}") from e

    def update_chat_idempotency(self, record: ChatIdempotencyRecord) -> None:
        """Transactional update helper executing fenced write with record.version."""
        self.update_chat_idempotency_fenced(
            record.key,
            record.version,
            expected_lease_owner=record.lease_owner,
            status=record.status,
            user_message_id=record.user_message_id,
            agent_message_id=record.agent_message_id,
            run_id=record.run_id,
            error_status_code=record.error_status_code,
            error_detail=record.error_detail,
        )

    def save_file(self, file_rec: FileRecord) -> FileRecord:
        super().save_file(file_rec)
        self.client.collection("files").document(file_rec.file_id).set(file_rec.model_dump(mode="json"))
        return file_rec

    def get_file(self, file_id: str) -> Optional[FileRecord]:
        doc = self.client.collection("files").document(file_id).get()
        if doc.exists:
            return FileRecord.model_validate(doc.to_dict())
        return None

    def delete_file(self, file_id: str) -> bool:
        f = self.get_file(file_id)
        if f:
            self.delete_all_document_chunks(f.space_id, file_id)
        super().delete_file(file_id)
        self.client.collection("files").document(file_id).delete()
        return True

    def delete_upload_intent_if_owned(self, file_id: str, upload_fencing_token: str) -> bool:
        """
        Atomic conditional deletion in Firestore: removes upload intent ONLY if still owned by uploader.
        """
        file_ref = self.client.collection("files").document(file_id)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _del_intent_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return False
                f = FileRecord.model_validate(snap.to_dict())
                if f.upload_status != "pending_upload":
                    return False
                if f.upload_fencing_token != upload_fencing_token:
                    return False
                if f.cleanup_status in ("in_progress", "deleting"):
                    return False

                tx.delete(f_ref)
                return True

            deleted = _del_intent_tx(transaction, file_ref)
            if deleted:
                super().delete_file(file_id)
            return deleted
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                return False
            raise StorageUnavailableError(f"Firestore intent delete failed: {e}") from e

    def mark_upload_failed_if_owned(self, file_id: str, upload_fencing_token: str, error_msg: str) -> bool:
        """
        Atomic conditional transition to failed in Firestore: marks failed ONLY if still owned by uploader.
        """
        file_ref = self.client.collection("files").document(file_id)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _mark_failed_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return False
                f = FileRecord.model_validate(snap.to_dict())
                if f.upload_status != "pending_upload":
                    return False
                if f.upload_fencing_token != upload_fencing_token:
                    return False
                if f.cleanup_status in ("in_progress", "deleting"):
                    return False  # Active cleanup lease held; do not overwrite!

                f.upload_status = "failed"
                f.cleanup_pending = True
                f.cleanup_last_error = error_msg
                f.upload_fencing_token = None
                tx.set(f_ref, f.model_dump(mode="json"))
                return f

            res = _mark_failed_tx(transaction, file_ref)
            if res:
                super().save_file(res)
                return True
            return False
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                return False
            raise StorageUnavailableError(f"Firestore mark failed intent error: {e}") from e

    def list_files_in_space(self, space_id: str, project_tag: Optional[str] = None) -> List[FileRecord]:
        query = self.client.collection("files").where("space_id", "==", space_id)
        if project_tag and project_tag not in ("all", "all-tracks", "undefined"):
            query = query.where("project_tags", "array_contains", project_tag)
        docs = query.stream()
        records = [FileRecord.model_validate(d.to_dict()) for d in docs]
        return [r for r in records if not r.cleanup_pending and r.upload_status == "committed"]

    def search_accessible_files(self, accessible_space_ids: List[str], query: str) -> List[FileRecord]:
        q_lower = query.lower()
        results = []
        for sid in accessible_space_ids:
            docs = self.client.collection("files").where("space_id", "==", sid).stream()
            for d in docs:
                rec = FileRecord.model_validate(d.to_dict())
                if not rec.cleanup_pending and rec.upload_status == "committed":
                    if q_lower in rec.filename.lower() or any(q_lower in tag.lower() for tag in rec.project_tags):
                        results.append(rec)
        return results

    def list_cleanup_pending_files(self) -> List[FileRecord]:
        now = datetime.now(UTC)
        results = []
        seen_ids = set()

        def _add_if_eligible(rec: FileRecord):
            if rec.file_id in seen_ids:
                return
            if rec.cleanup_status == "failed":
                return
            # If in_progress or deleting, only list if active lease has expired
            if rec.cleanup_lease_until and rec.cleanup_lease_until > now:
                return
            seen_ids.add(rec.file_id)
            results.append(rec)

        # Query 1: cleanup_pending == True
        for d in self.client.collection("files").where("cleanup_pending", "==", True).stream():
            rec = FileRecord.model_validate(d.to_dict())
            _add_if_eligible(rec)

        # Query 2: upload_status == failed
        for d in self.client.collection("files").where("upload_status", "==", "failed").stream():
            rec = FileRecord.model_validate(d.to_dict())
            _add_if_eligible(rec)

        # Query 3: upload_status == pending_upload (filter expired)
        for d in self.client.collection("files").where("upload_status", "==", "pending_upload").stream():
            rec = FileRecord.model_validate(d.to_dict())
            if rec.upload_lease_until and rec.upload_lease_until < now:
                _add_if_eligible(rec)

        return results

    def claim_cleanup_file(self, file_id: str, lease_duration_seconds: int = 60) -> Optional[FileRecord]:
        """
        Atomically claims a pending cleanup file using Firestore transaction with fencing token.
        Allows unexpired deleting/in_progress records to be reclaimed if worker timed out/crashed.
        """
        file_ref = self.client.collection("files").document(file_id)
        now = datetime.now(UTC)
        token = uuid.uuid4().hex
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _claim_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return None
                data = snap.to_dict()
                if not isinstance(data, dict):
                    return None
                f = FileRecord.model_validate(data)

                # Protect in-flight uploads with active upload lease
                if f.upload_status == "pending_upload":
                    if not f.upload_lease_until or f.upload_lease_until >= now:
                        return None  # In-flight upload grace period active!

                is_cleanup = f.cleanup_pending is True
                is_failed_upload = f.upload_status == "failed"
                is_abandoned_upload = (
                    f.upload_status == "pending_upload"
                    and f.upload_lease_until is not None
                    and f.upload_lease_until < now
                )

                if not (is_cleanup or is_failed_upload or is_abandoned_upload):
                    return None

                # If deleting or in_progress, it MUST have expired lease to be reclaimed by a new worker
                if f.cleanup_status in ("in_progress", "deleting"):
                    if f.cleanup_lease_until and f.cleanup_lease_until > now:
                        return None  # Active lease held by another worker
                elif f.cleanup_status == "failed":
                    return None

                if f.cleanup_retries >= f.max_cleanup_retries:
                    return None
                if f.cleanup_next_retry_at and f.cleanup_next_retry_at > now:
                    return None

                f.cleanup_pending = True
                f.cleanup_status = "in_progress"
                f.lease_token = token
                f.lease_version += 1
                f.cleanup_lease_until = now + timedelta(seconds=lease_duration_seconds)
                tx.set(f_ref, f.model_dump(mode="json"))
                return f

            claimed = _claim_tx(transaction, file_ref)
            if claimed:
                super().save_file(claimed)
            return claimed
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                return None
            raise StorageUnavailableError(f"Firestore claim operation failed: {e}") from e

    def mark_cleanup_file_deleting(self, file_id: str, lease_token: str) -> bool:
        """
        Transactional transition to 'deleting' state under valid lease BEFORE irreversible physical deletion.
        """
        file_ref = self.client.collection("files").document(file_id)
        now = datetime.now(UTC)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _mark_del_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return False
                f = FileRecord.model_validate(snap.to_dict())
                if f.lease_token != lease_token:
                    return False
                if not f.cleanup_lease_until or f.cleanup_lease_until < now:
                    return False  # Lease expired!
                if f.cleanup_status not in ("in_progress", "deleting"):
                    return False
                f.cleanup_status = "deleting"
                tx.set(f_ref, f.model_dump(mode="json"))
                return True

            marked = _mark_del_tx(transaction, file_ref)
            return marked
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                return False
            raise StorageUnavailableError(f"Firestore mark deleting failed: {e}") from e

    def delete_cleanup_file_with_lease(self, file_id: str, lease_token: str) -> bool:
        """
        Transactional deletion checking active unexpired lease token directly in Firestore.
        Phase 1: Verifies lease and marks cleanup_status='deleting' in transaction (fencing).
        Phase 2: Purges all chunks from document_chunks collection.
        Phase 3: Re-verifies lease expiry and status in final transaction before deleting file document.
        """
        file_ref = self.client.collection("files").document(file_id)
        now = datetime.now(UTC)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _mark_deleting_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return (False, "")
                f = FileRecord.model_validate(snap.to_dict())
                if f.lease_token != lease_token:
                    return (False, "")
                if not f.cleanup_lease_until or f.cleanup_lease_until < now:
                    return (False, "")  # Lease expired!
                f.cleanup_status = "deleting"
                tx.set(f_ref, f.model_dump(mode="json"))
                return (True, f.space_id)

            marked, space_id = _mark_deleting_tx(transaction, file_ref)
            if not marked:
                return False

            # Purge all remote chunks
            self.delete_all_document_chunks(space_id, file_id)

            # Final transaction: delete file document with re-verified lease and status
            tx_final = self.client.transaction()

            @firestore.transactional
            def _del_final_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return False
                f = FileRecord.model_validate(snap.to_dict())
                if f.lease_token != lease_token:
                    return False
                if f.cleanup_status not in ("in_progress", "deleting"):
                    return False
                now_final = datetime.now(UTC)
                if not f.cleanup_lease_until or f.cleanup_lease_until < now_final:
                    return False  # Lease expired during chunk deletion!
                tx.delete(f_ref)
                return True

            deleted = _del_final_tx(tx_final, file_ref)
            if deleted:
                super().delete_file(file_id)
            return deleted
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                return False
            raise StorageUnavailableError(f"Firestore lease delete failed: {e}") from e

    def release_cleanup_file_with_lease(
        self,
        file_id: str,
        lease_token: str,
        next_retry_at: Optional[datetime],
        last_error: str,
        is_terminal: bool,
    ) -> bool:
        """
        Transactional lease release checking active unexpired lease token directly in Firestore.
        """
        file_ref = self.client.collection("files").document(file_id)
        now = datetime.now(UTC)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _rel_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return False
                f = FileRecord.model_validate(snap.to_dict())
                if f.lease_token != lease_token:
                    return False
                if not f.cleanup_lease_until or f.cleanup_lease_until < now:
                    return False  # Lease expired!
                f.cleanup_retries += 1
                f.lease_token = None
                f.cleanup_lease_until = None
                f.cleanup_last_error = last_error
                f.cleanup_status = "failed" if is_terminal else "pending"
                f.cleanup_next_retry_at = next_retry_at
                tx.set(f_ref, f.model_dump(mode="json"))
                return f

            released_rec = _rel_tx(transaction, file_ref)
            if released_rec:
                super().save_file(released_rec)
                return True
            return False
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                return False
            raise StorageUnavailableError(f"Firestore lease release failed: {e}") from e

    def renew_upload_lease(
        self,
        file_id: str,
        upload_fencing_token: str,
        extension_seconds: int = 60,
    ) -> bool:
        """
        Extends the in-flight upload grace period in Firestore while data streaming / upload is active.
        Strictly sets lease to (now + extension_seconds), bounding maximum grace window and preventing accumulation.
        """
        file_ref = self.client.collection("files").document(file_id)
        now = datetime.now(UTC)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _renew_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return False
                f = FileRecord.model_validate(snap.to_dict())
                if f.upload_status != "pending_upload":
                    return False
                if f.upload_fencing_token != upload_fencing_token:
                    return False
                if f.cleanup_status in ("deleting", "failed") or f.cleanup_pending:
                    return False

                f.upload_lease_until = now + timedelta(seconds=extension_seconds)
                tx.set(f_ref, f.model_dump(mode="json"))
                return f

            res = _renew_tx(transaction, file_ref)
            if res:
                super().save_file(res)
                return True
            return False
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                return False
            raise StorageUnavailableError(f"Firestore upload renewal failed: {e}") from e

    def commit_uploaded_file(
        self,
        file_id: str,
        upload_fencing_token: str,
        size_bytes: int,
        sha256: str,
    ) -> Optional[FileRecord]:
        """
        Atomic Phase 3 Upload Commit: Commits record ONLY if not expired/claimed by cleanup worker.
        """
        file_ref = self.client.collection("files").document(file_id)
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _commit_tx(tx, f_ref):
                snap = f_ref.get(transaction=tx)
                if not snap.exists:
                    return None
                f = FileRecord.model_validate(snap.to_dict())
                if f.upload_status != "pending_upload":
                    return None
                if f.upload_fencing_token != upload_fencing_token:
                    return None
                if f.cleanup_status in ("deleting", "failed") or f.cleanup_pending:
                    return None

                f.upload_status = "committed"
                f.cleanup_pending = False
                f.upload_fencing_token = None
                f.upload_lease_until = None
                f.size_bytes = size_bytes
                f.sha256 = sha256
                tx.set(f_ref, f.model_dump(mode="json"))
                return f

            res = _commit_tx(transaction, file_ref)
            if res:
                super().save_file(res)
            return res
        except Exception as e:
            raise StorageUnavailableError(f"Firestore upload commit failed: {e}") from e

    def save_run(self, run: Run) -> Run:
        run_ref = self.client.collection("runs").document(run.run_id)
        now = datetime.now(UTC)
        from google.cloud import firestore

        ev_id = f"run.started:{run.run_id}"
        ev = ActivityEvent(
            event_id=ev_id,
            event_type=ActivityEventType.RUN_STARTED,
            space_id=run.space_id,
            project_tags=[run.project_tag or "general"],
            resource_type="run",
            resource_id=run.run_id,
            summary=f"Execution run started: {run.prompt[:60]}",
            details={"run_id": run.run_id, "status": run.status.value if hasattr(run.status, "value") else str(run.status)},
            actor_uid=run.created_by,
            created_at=now,
        )
        outbox_id = f"outbox:{ev.event_id}"
        outbox_item = ActivityOutboxItem(
            outbox_id=outbox_id,
            event_id=ev.event_id,
            space_id=run.space_id,
            event=ev,
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=5,
            created_at=now,
            updated_at=now,
            next_retry_at=now,
        )

        @firestore.transactional
        def _save_tx(transaction):
            snap = run_ref.get(transaction=transaction)
            if not snap.exists:
                transaction.set(run_ref, run.model_dump(mode="json"))
                outbox_ref = self.client.collection("activity_outbox").document(outbox_id)
                transaction.set(outbox_ref, outbox_item.model_dump(mode="json"))
                act_ref = self.client.collection("activity_events").document(ev.event_id)
                transaction.set(act_ref, ev.model_dump(mode="json"))
                return run
            return Run.model_validate(snap.to_dict())

        try:
            tx = self.client.transaction()
            res = _save_tx(tx)
            with self._lock:
                self.runs[res.run_id] = res
                sp_evts = self.activity_events.setdefault(run.space_id, [])
                if not any(e.event_id == ev.event_id for e in sp_evts):
                    sp_evts.append(ev)
            return res
        except Exception as e:
            raise StorageUnavailableError(f"Firestore save_run failed: {e}") from e

    def save_run_fenced(
        self,
        run: Run,
        idemp_key: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Run:
        now = datetime.now(UTC)
        run_ref = self.client.collection("runs").document(run.run_id)
        ev_id = f"run.started:{run.run_id}"
        ev = ActivityEvent(
            event_id=ev_id,
            event_type=ActivityEventType.RUN_STARTED,
            space_id=run.space_id,
            project_tags=[run.project_tag or "general"],
            resource_type="run",
            resource_id=run.run_id,
            summary=f"Execution run started: {run.prompt[:60]}",
            details={"run_id": run.run_id, "status": run.status.value if hasattr(run.status, "value") else str(run.status)},
            actor_uid=run.created_by,
            created_at=now,
        )
        outbox_id = f"outbox:{ev.event_id}"
        outbox_item = ActivityOutboxItem(
            outbox_id=outbox_id,
            event_id=ev.event_id,
            space_id=run.space_id,
            event=ev,
            status=OutboxStatus.PENDING,
            attempts=0,
            max_attempts=5,
            created_at=now,
            updated_at=now,
            next_retry_at=now,
        )
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _save_run_tx(tx):
                if idemp_key is not None and expected_version is not None:
                    idemp_ref = self.client.collection("chat_idempotency").document(idemp_key)
                    idemp_snap = idemp_ref.get(transaction=tx)
                    if not idemp_snap.exists:
                        raise StorageConflictError("Idempotency record not found during fenced run save.")
                    rec = ChatIdempotencyRecord.model_validate(idemp_snap.to_dict())
                    if rec.version != expected_version:
                        raise StorageConflictError(
                            f"Fencing conflict: expected idempotency version {expected_version}, but found {rec.version}."
                        )
                    if rec.status == ChatIdempotencyStatus.IN_PROGRESS and rec.lease_until and rec.lease_until < now:
                        raise StorageConflictError("Fencing conflict: operation lease expired during run save.")

                snap = run_ref.get(transaction=tx)
                if snap.exists:
                    return Run.model_validate(snap.to_dict())

                tx.set(run_ref, run.model_dump(mode="json"))
                outbox_ref = self.client.collection("activity_outbox").document(outbox_id)
                tx.set(outbox_ref, outbox_item.model_dump(mode="json"))
                act_ref = self.client.collection("activity_events").document(ev.event_id)
                tx.set(act_ref, ev.model_dump(mode="json"))
                return run

            res = _save_run_tx(transaction)
            with self._lock:
                self.runs[res.run_id] = res
                sp_evts = self.activity_events.setdefault(run.space_id, [])
                if not any(e.event_id == ev.event_id for e in sp_evts):
                    sp_evts.append(ev)
            return res
        except StorageConflictError:
            raise
        except Exception as e:
            err_str = str(e).lower()
            if "conflict" in err_str or "aborted" in err_str:
                raise StorageConflictError(f"Firestore save_run_fenced transaction conflict: {e}") from e
            raise StorageUnavailableError(f"Firestore save_run_fenced failed: {e}") from e


    def get_run(self, run_id: str) -> Optional[Run]:
        doc = self.client.collection("runs").document(run_id).get()
        if doc.exists:
            return Run.model_validate(doc.to_dict())
        return None

    def record_metric_rollup_event(
        self,
        space_id: str,
        project_tag: str,
        duration_ms: int,
        status: str,
        failure_code: Optional[str] = None,
        tokens_used: Optional[int] = None,
        estimated_cost_usd: Optional[float] = None,
        pricing_version: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        from google.cloud import firestore

        now = timestamp or datetime.now(UTC)
        tag = project_tag or "general"
        hour_str = now.strftime("%Y-%m-%dT%H")
        bucket_id = f"{space_id}:{tag}:{hour_str}"
        doc_ref = self.client.collection("space_metric_rollups").document(bucket_id)

        @firestore.transactional
        def _rollup_tx(tx):
            from app.models.telemetry import get_histogram_bucket_key, normalize_failure_code

            snap = doc_ref.get(transaction=tx)
            raw = snap.to_dict() if getattr(snap, "exists", False) else None
            if raw and raw.get("rollup_schema_version") == 2:
                rec = raw
            else:
                rec = {
                    "bucket_id": bucket_id,
                    "space_id": space_id,
                    "project_tag": tag,
                    "hour_str": hour_str,
                    "rollup_schema_version": 2,
                    "total_runs": 0,
                    "completed_runs": 0,
                    "failed_runs": 0,
                    "latency_histogram": {},
                    "total_tokens": 0,
                    "priced_run_count": 0,
                    "unpriced_run_count": 0,
                    "pricing_versions": [],
                    "pricing_versions_truncated": False,
                    "estimated_cost_usd": None,
                    "failures_by_code": {},
                }
            rec["total_runs"] = (rec.get("total_runs") or 0) + 1
            if status in ("completed", RunStatus.COMPLETED, RunStatus.COMPLETED.value):
                rec["completed_runs"] = (rec.get("completed_runs") or 0) + 1
            elif status in ("failed", RunStatus.FAILED, RunStatus.FAILED.value):
                rec["failed_runs"] = (rec.get("failed_runs") or 0) + 1
                clean_code = normalize_failure_code(failure_code)
                f_codes = rec.setdefault("failures_by_code", {})
                f_codes[clean_code] = f_codes.get(clean_code, 0) + 1
            if duration_ms >= 0:
                b_key = get_histogram_bucket_key(duration_ms)
                hist = rec.setdefault("latency_histogram", {})
                hist[b_key] = hist.get(b_key, 0) + 1
            if tokens_used:
                rec["total_tokens"] = (rec.get("total_tokens") or 0) + tokens_used

            if estimated_cost_usd is not None and pricing_version:
                rec["priced_run_count"] = (rec.get("priced_run_count") or 0) + 1
                curr_cost = rec.get("estimated_cost_usd")
                base_cost = curr_cost if curr_cost is not None else 0.0
                rec["estimated_cost_usd"] = round(base_cost + estimated_cost_usd, 6)
                p_vers = rec.setdefault("pricing_versions", [])
                if pricing_version not in p_vers:
                    if len(p_vers) < 5:
                        p_vers.append(pricing_version)
                    else:
                        rec["pricing_versions_truncated"] = True
            else:
                rec["unpriced_run_count"] = (rec.get("unpriced_run_count") or 0) + 1
                if estimated_cost_usd is not None:
                    curr_cost = rec.get("estimated_cost_usd")
                    base_cost = curr_cost if curr_cost is not None else 0.0
                    rec["estimated_cost_usd"] = round(base_cost + estimated_cost_usd, 6)

            rec["updated_at"] = now.isoformat()
            tx.set(doc_ref, rec)

        try:
            tx = self.client.transaction()
            _rollup_tx(tx)
        except Exception as e:
            logger.error("Failed to record metric rollup for %s: %s", bucket_id, e)

    def get_metric_rollups(
        self,
        space_id: str,
        start_time: datetime,
        end_time: datetime,
        project_tag: Optional[str] = None,
    ) -> List[SpaceHourlyMetricRollup]:
        start_hour = start_time.strftime("%Y-%m-%dT%H")
        end_hour = end_time.strftime("%Y-%m-%dT%H")
        query = (
            self.client.collection("space_metric_rollups")
            .where("space_id", "==", space_id)
            .where("hour_str", ">=", start_hour)
            .where("hour_str", "<=", end_hour)
        )
        if project_tag and project_tag != "all":
            query = query.where("project_tag", "==", project_tag)
        docs = query.stream()
        res = []
        for d in docs:
            rec = d.to_dict()
            # Legacy policy: strictly ignore v1 records without schema version 2
            if rec.get("rollup_schema_version") == 2:
                res.append(SpaceHourlyMetricRollup.model_validate(rec))
        res.sort(key=lambda x: x.hour_str)
        return res

    def list_runs_for_metrics(
        self,
        space_id: str,
        since: datetime,
        project_tag: Optional[str] = None,
        limit: int = 500,
    ) -> List[Run]:
        query = self.client.collection("runs").where("space_id", "==", space_id)
        if project_tag and project_tag != "all":
            query = query.where("project_tag", "==", project_tag)
        if hasattr(query, "limit"):
            query = query.limit(limit)
        docs = query.stream()
        res = []
        for d in docs:
            r = Run.model_validate(d.to_dict())
            if (r.created_at and r.created_at >= since) or (r.updated_at and r.updated_at >= since):
                res.append(r)
        res.sort(key=lambda x: x.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return res[:limit]

    def list_runs_in_space(self, space_id: str, project_tag: Optional[str] = None) -> List[Run]:
        query = self.client.collection("runs").where("space_id", "==", space_id)
        if project_tag and project_tag not in ("all", "all-tracks", "undefined"):
            query = query.where("project_tag", "==", project_tag)
        docs = query.stream()
        return [Run.model_validate(d.to_dict()) for d in docs]


    def compare_and_swap_run_status(
        self,
        run_id: str,
        expected_status: RunStatus,
        new_status: RunStatus,
        mutator_fn: Optional[Callable[[Run], None]] = None,
        actor_uid: Optional[str] = None,
        outbox_event_factory: Optional[Callable[[Run], Optional[ActivityEvent]]] = None,
    ) -> Optional[Run]:
        """
        Atomic Compare-And-Swap (CAS) state transition for Run lifecycle in Firestore with integrated Transactional Outbox.
        Uses Firestore client transaction to prevent concurrent execution races and ensure atomic outbox staging.
        """
        run_ref = self.client.collection("runs").document(run_id)
        now = datetime.now(UTC)
        ev_to_cache = None
        try:
            from google.cloud import firestore
            transaction = self.client.transaction()

            @firestore.transactional
            def _cas_tx(tx, r_ref):
                nonlocal ev_to_cache
                # === PHASE 1: ALL READS FIRST (STRICT READ-BEFORE-WRITE) ===
                snap = r_ref.get(transaction=tx)
                if not snap.exists:
                    return None
                r = Run.model_validate(snap.to_dict())
                if r.status != expected_status:
                    return None

                rollup_ref = None
                rollup_rec = None
                if new_status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    from app.models.telemetry import get_histogram_bucket_key, normalize_failure_code

                    tag = r.project_tag or "general"
                    hour_str = now.strftime("%Y-%m-%dT%H")
                    bucket_id = f"{r.space_id}:{tag}:{hour_str}"
                    rollup_ref = self.client.collection("space_metric_rollups").document(bucket_id)
                    # All transaction reads occur before any writes
                    r_snap = rollup_ref.get(transaction=tx)
                    raw_rollup = r_snap.to_dict() if getattr(r_snap, "exists", False) else None
                    if raw_rollup and raw_rollup.get("rollup_schema_version") == 2:
                        rollup_rec = raw_rollup
                    else:
                        rollup_rec = {
                            "bucket_id": bucket_id,
                            "space_id": r.space_id,
                            "project_tag": tag,
                            "hour_str": hour_str,
                            "rollup_schema_version": 2,
                            "total_runs": 0,
                            "completed_runs": 0,
                            "failed_runs": 0,
                            "latency_histogram": {},
                            "total_tokens": 0,
                            "priced_run_count": 0,
                            "unpriced_run_count": 0,
                            "pricing_versions": [],
                            "pricing_versions_truncated": False,
                            "estimated_cost_usd": None,
                            "failures_by_code": {},
                        }

                # === PHASE 2: IN-MEMORY MUTATION & VALIDATION ===
                r.status = new_status
                r.state_version = (getattr(r, "state_version", 1) or 1) + 1
                r.updated_at = now
                if mutator_fn:
                    mutator_fn(r)

                if rollup_rec is not None:
                    dur = (getattr(r, "telemetry", None) and getattr(r.telemetry, "duration_ms", 0)) or 0
                    if dur == 0 and r.created_at:
                        dur = max(0, int((now - r.created_at).total_seconds() * 1000))
                    tokens = (getattr(r, "telemetry", None) and getattr(r.telemetry, "tokens_used", None)) or 0
                    cost = getattr(r.telemetry, "estimated_cost_usd", None) if getattr(r, "telemetry", None) else None
                    pricing_ver = getattr(r.telemetry, "pricing_version", None) if getattr(r, "telemetry", None) else None
                    fail_code = r.failure_code or (r.telemetry and getattr(r.telemetry, "error_code", None))

                    rollup_rec["total_runs"] = (rollup_rec.get("total_runs") or 0) + 1
                    if new_status == RunStatus.COMPLETED:
                        rollup_rec["completed_runs"] = (rollup_rec.get("completed_runs") or 0) + 1
                    else:
                        rollup_rec["failed_runs"] = (rollup_rec.get("failed_runs") or 0) + 1
                        norm_code = normalize_failure_code(fail_code)
                        rollup_rec["failures_by_code"][norm_code] = rollup_rec["failures_by_code"].get(norm_code, 0) + 1

                    if dur >= 0:
                        b_key = get_histogram_bucket_key(dur)
                        hist = rollup_rec.setdefault("latency_histogram", {})
                        hist[b_key] = hist.get(b_key, 0) + 1

                    if tokens:
                        rollup_rec["total_tokens"] = (rollup_rec.get("total_tokens") or 0) + tokens
                    if cost is not None and pricing_ver:
                        rollup_rec["priced_run_count"] = (rollup_rec.get("priced_run_count") or 0) + 1
                        curr_cost = rollup_rec.get("estimated_cost_usd")
                        base_cost = curr_cost if curr_cost is not None else 0.0
                        rollup_rec["estimated_cost_usd"] = round(base_cost + cost, 6)
                        p_vers = rollup_rec.setdefault("pricing_versions", [])
                        if pricing_ver not in p_vers:
                            if len(p_vers) < 5:
                                p_vers.append(pricing_ver)
                            else:
                                rollup_rec["pricing_versions_truncated"] = True
                    else:
                        rollup_rec["unpriced_run_count"] = (rollup_rec.get("unpriced_run_count") or 0) + 1
                        if cost is not None:
                            curr_cost = rollup_rec.get("estimated_cost_usd")
                            base_cost = curr_cost if curr_cost is not None else 0.0
                            rollup_rec["estimated_cost_usd"] = round(base_cost + cost, 6)
                    rollup_rec["updated_at"] = now.isoformat()

                # Determine Activity Event & Outbox Staging
                ev = None
                if outbox_event_factory:
                    ev = outbox_event_factory(r)
                elif expected_status == RunStatus.AWAITING_APPROVAL and new_status == RunStatus.RUNNING:
                    ev = ActivityEvent(
                        event_id=f"gate.approved:{r.run_id}:r{r.state_version}",
                        event_type=ActivityEventType.GATE_APPROVED,
                        space_id=r.space_id,
                        project_tags=[r.project_tag or "general"],
                        resource_type="gate",
                        resource_id=r.run_id,
                        summary=f"Approval gate '{r.approval_gate.title if r.approval_gate else 'Gate'}' approved",
                        details={"run_id": r.run_id, "state_version": r.state_version, "approved": True},
                        actor_uid=actor_uid,
                        created_at=now,
                    )
                elif expected_status == RunStatus.AWAITING_APPROVAL and (new_status == RunStatus.FAILED or str(new_status).lower() == "rejected"):
                    ev = ActivityEvent(
                        event_id=f"gate.rejected:{r.run_id}:r{r.state_version}",
                        event_type=ActivityEventType.GATE_REJECTED,
                        space_id=r.space_id,
                        project_tags=[r.project_tag or "general"],
                        resource_type="gate",
                        resource_id=r.run_id,
                        summary=f"Approval gate '{r.approval_gate.title if r.approval_gate else 'Gate'}' rejected",
                        details={"run_id": r.run_id, "state_version": r.state_version, "approved": False},
                        actor_uid=actor_uid,
                        created_at=now,
                    )
                elif new_status == RunStatus.COMPLETED:
                    ev = ActivityEvent(
                        event_id=f"run.completed:{r.run_id}:r{r.state_version}",
                        event_type=ActivityEventType.RUN_COMPLETED,
                        space_id=r.space_id,
                        project_tags=[r.project_tag or "general"],
                        resource_type="run",
                        resource_id=r.run_id,
                        summary=f"Execution run completed (Trace: {r.trace_id or r.run_id})",
                        details={"run_id": r.run_id, "state_version": r.state_version},
                        actor_uid=actor_uid,
                        created_at=now,
                    )
                elif new_status == RunStatus.FAILED:
                    ev = ActivityEvent(
                        event_id=f"run.failed:{r.run_id}:r{r.state_version}",
                        event_type=ActivityEventType.RUN_FAILED,
                        space_id=r.space_id,
                        project_tags=[r.project_tag or "general"],
                        resource_type="run",
                        resource_id=r.run_id,
                        summary=f"Execution run failed: {r.error_summary or 'Internal failure'}",
                        details={"run_id": r.run_id, "state_version": r.state_version, "failure_code": r.failure_code},
                        actor_uid=actor_uid,
                        created_at=now,
                    )

                # === PHASE 3: ALL WRITES AT THE END ===
                tx.set(r_ref, r.model_dump(mode="json"))

                if rollup_ref is not None:
                    tx.set(rollup_ref, rollup_rec)

                if ev:
                    ev_to_cache = ev
                    outbox_id = f"outbox:{ev.event_id}"
                    outbox_item = ActivityOutboxItem(
                        outbox_id=outbox_id,
                        event_id=ev.event_id,
                        space_id=r.space_id,
                        event=ev,
                        status=OutboxStatus.PENDING,
                        attempts=0,
                        max_attempts=5,
                        created_at=now,
                        updated_at=now,
                        next_retry_at=now,
                    )
                    outbox_ref = self.client.collection("activity_outbox").document(outbox_id)
                    tx.set(outbox_ref, outbox_item.model_dump(mode="json"))
                    act_ref = self.client.collection("activity_events").document(ev.event_id)
                    tx.set(act_ref, ev.model_dump(mode="json"))

                return r

            res = _cas_tx(transaction, run_ref)
            if res:
                with self._lock:
                    self.runs[res.run_id] = res
                    if ev_to_cache:
                        sp_evts = self.activity_events.setdefault(res.space_id, [])
                        if not any(e.event_id == ev_to_cache.event_id for e in sp_evts):
                            sp_evts.append(ev_to_cache)
            return res
        except Exception as e:
            raise StorageUnavailableError(f"Firestore CAS status transition failed: {e}") from e

    # --- Document Ingestion & Chunk Remote Persistence ---

    def save_document_chunks(
        self,
        space_id: str,
        file_id: str,
        generation: int,
        chunks: List[DocumentChunk],
        expected_job_id: Optional[str] = None,
    ) -> bool:
        file_ref = self.client.collection("files").document(file_id)
        now = datetime.now(UTC)
        committed_batches = 0
        try:
            from google.cloud import firestore

            # Batch chunks into transactions (up to 400 chunks per transaction)
            batch_size = 400
            chunk_batches = [chunks[i : i + batch_size] for i in range(0, len(chunks), batch_size)] if chunks else [[]]

            for batch_items in chunk_batches:
                tx = self.client.transaction()

                @firestore.transactional
                def _write_batch_tx(transaction, f_ref, items):
                    snap = f_ref.get(transaction=transaction)
                    if not snap.exists:
                        logger.warning(f"Firestore rejected chunk write: file {file_id} not found")
                        return False
                    f_data = snap.to_dict()
                    if f_data.get("space_id") != space_id:
                        logger.warning(f"Firestore rejected chunk write: space mismatch for {file_id}")
                        return False
                    if f_data.get("cleanup_status") in ("deleting", "deleted"):
                        logger.warning(f"Firestore rejected chunk write: file {file_id} is being deleted")
                        return False

                    # Prevent modifying an already active and ready generation
                    if (
                        f_data.get("active_generation") == generation
                        and f_data.get("ingestion_status") in ("ready", "ready_partial")
                    ):
                        logger.warning(f"Firestore rejected chunk write: generation {generation} is already active/ready")
                        return False

                    valid_statuses = (
                        IngestionStatus.EXTRACTING.value,
                        IngestionStatus.INDEXING.value,
                        "extracting",
                        "indexing",
                    )
                    if f_data.get("ingestion_status") not in valid_statuses:
                        logger.warning(
                            f"Firestore rejected chunk write for {file_id}: status '{f_data.get('ingestion_status')}' not writable"
                        )
                        return False
                    if not expected_job_id or f_data.get("ingestion_job_id") != expected_job_id:
                        logger.warning(
                            f"Firestore rejected chunk write for {file_id}: job ownership conflict "
                            f"(expected {expected_job_id}, active {f_data.get('ingestion_job_id')})"
                        )
                        return False

                    lease_until_raw = f_data.get("ingestion_lease_until")
                    if not lease_until_raw:
                        logger.warning(f"Firestore rejected chunk write for {file_id}: lease missing or null")
                        return False

                    lease_until_dt = None
                    if isinstance(lease_until_raw, str):
                        try:
                            lease_until_dt = datetime.fromisoformat(lease_until_raw.replace("Z", "+00:00"))
                        except Exception:
                            pass
                    elif isinstance(lease_until_raw, datetime):
                        lease_until_dt = lease_until_raw if lease_until_raw.tzinfo else lease_until_raw.replace(tzinfo=UTC)

                    if not lease_until_dt or lease_until_dt <= now:
                        logger.warning(f"Firestore rejected chunk write for {file_id}: lease expired or unparseable")
                        return False

                    # Reject writes to any generation currently being cleaned or permanently tombstoned.
                    # This prevents a delayed worker from writing chunks that immediately become orphaned.
                    if generation in (f_data.get("cleaning_generations") or []):
                        logger.warning(
                            f"Firestore rejected chunk write for {file_id}: "
                            f"generation {generation} is currently being cleaned"
                        )
                        return False
                    if generation in (f_data.get("deleted_generations") or []):
                        logger.warning(
                            f"Firestore rejected chunk write for {file_id}: "
                            f"generation {generation} has been permanently tombstoned"
                        )
                        return False

                    # Atomic write of chunk documents inside this transaction
                    for chunk in items:
                        c_ref = self.client.collection("document_chunks").document(chunk.chunk_id)
                        transaction.set(c_ref, chunk.model_dump(mode="json"))

                    return True

                success = _write_batch_tx(tx, file_ref, batch_items)
                if not success:
                    # ONLY run compensating cleanup if 1 or more preceding batches were committed
                    if committed_batches > 0:
                        self._safe_compensating_delete(space_id, file_id, generation, expected_job_id)
                    return False
                committed_batches += 1

            # Best-effort memory cache synchronization (if present locally)
            if file_id in self.files:
                super().save_document_chunks(space_id, file_id, generation, chunks, expected_job_id)

            return True
        except Exception as e:
            logger.exception(f"Firestore save_document_chunks failed: {e}")
            if committed_batches > 0:
                self._safe_compensating_delete(space_id, file_id, generation, expected_job_id, error_detail=str(e))
            return False

    def _safe_compensating_delete(
        self,
        space_id: str,
        file_id: str,
        generation: int,
        job_id: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> None:
        """
        Safely purges ONLY the uncommitted generation's chunks.
        Guarantees that active generation chunks are NEVER touched regardless of ingestion status.
        If cleanup encounters an exception, records a persistent pending_generation_cleanups entry in Firestore.
        """
        try:
            f = self.get_file(file_id)
            # NEVER delete if this generation is currently the active generation!
            if f and f.active_generation == generation:
                logger.warning(f"Compensating delete aborted: generation {generation} is currently active")
                return

            self.delete_generation_document_chunks(space_id, file_id, generation, job_id)
        except Exception as cleanup_err:
            logger.exception(f"Compensating delete failed for {file_id} gen {generation}: {cleanup_err}")
            try:
                cleanup_doc_id = f"pgc_{file_id}_g{generation}_{uuid.uuid4().hex[:8]}"
                cleanup_ref = self.client.collection("pending_generation_cleanups").document(cleanup_doc_id)
                cleanup_ref.set({
                    "cleanup_id": cleanup_doc_id,
                    "space_id": space_id,
                    "file_id": file_id,
                    "generation": generation,
                    "job_id": job_id,
                    "lease_token": None,
                    "lease_until": None,
                    "retry_count": 0,
                    "status": "pending",
                    "error_detail": error_detail or str(cleanup_err),
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                })
            except Exception as persist_err:
                logger.error(f"Failed to persist pending generation cleanup record: {persist_err}")
                raise

    def delete_generation_document_chunks(
        self,
        space_id: str,
        file_id: str,
        generation: int,
        job_id: Optional[str] = None,
    ) -> int:
        """
        Deletes remote chunks strictly matching (file_id, ingestion_version == generation) with atomic mutual exclusion.
        Raises CleanupAuthorizationError if the generation is protected (active) or file/space mismatch.
        Acquires cleanup authorization inside transaction, marks cleaning_generations,
        stamps deleted_generations tombstone before releasing cleaning mark post-deletion.
        """
        from google.cloud import firestore
        file_ref = self.client.collection("files").document(file_id)

        @firestore.transactional
        def _authorize_cleanup_tx(tx):
            snap = file_ref.get(transaction=tx)
            if not snap.exists:
                return (False, "file_not_found")
            f_data = snap.to_dict()
            if f_data.get("space_id") != space_id:
                return (False, "space_mismatch")
            if f_data.get("active_generation") == generation:
                logger.warning(f"Cannot clean generation {generation}: it is currently active")
                return (False, "active_generation_protected")
            current_cleaning = list(f_data.get("cleaning_generations") or [])
            if generation not in current_cleaning:
                current_cleaning.append(generation)
                f_data["cleaning_generations"] = current_cleaning
                tx.set(file_ref, f_data)
            return (True, None)

        try:
            tx_auth = self.client.transaction()
            authorized, reason = _authorize_cleanup_tx(tx_auth)
            if not authorized:
                # Structural denial: file missing, space mismatch, or active-generation protection.
                # These are permanent — retrying will produce the same result.
                raise CleanupAuthorizationError(
                    f"Cleanup authorization denied for {file_id} gen {generation}: {reason}"
                )
        except CleanupAuthorizationError:
            raise
        except Exception as e:
            # Transient backend failure (network error, Firestore unavailable, etc.).
            # The work record must NOT be marked complete; let the sweeper retry with backoff.
            logger.warning(f"Transient failure acquiring cleanup authorization for {file_id} gen {generation}: {e}")
            raise StorageUnavailableError(
                f"Cleanup authorization backend unavailable for {file_id} gen {generation}: {e}"
            ) from e

        # Phase 2: Purge chunks for this generation (also stamps tombstone in MemoryStore parent)
        try:
            super().delete_generation_document_chunks(space_id, file_id, generation, job_id)
        except CleanupAuthorizationError:
            pass  # MemoryStore may not have this file in cold-start; Firestore is authoritative

        docs = (
            self.client.collection("document_chunks")
            .where("file_id", "==", file_id)
            .where("ingestion_version", "==", generation)
            .stream()
        )
        batch = self.client.batch()
        count = 0
        deleted_count = 0
        for d in docs:
            batch.delete(d.reference)
            count += 1
            deleted_count += 1
            if count >= 400:
                batch.commit()
                batch = self.client.batch()
                count = 0
        if count > 0:
            batch.commit()

        # Phase 3: Stamp permanent deleted_generations tombstone then release cleaning mark
        @firestore.transactional
        def _release_cleanup_tx(tx):
            snap = file_ref.get(transaction=tx)
            if not snap.exists:
                return
            f_data = snap.to_dict()
            # Permanently revoke commit rights for this generation
            deleted_gens = list(f_data.get("deleted_generations") or [])
            if generation not in deleted_gens:
                deleted_gens.append(generation)
            f_data["deleted_generations"] = deleted_gens
            # Release the transient cleaning lock
            f_data["cleaning_generations"] = [g for g in (f_data.get("cleaning_generations") or []) if g != generation]
            tx.set(file_ref, f_data)

        try:
            tx_rel = self.client.transaction()
            _release_cleanup_tx(tx_rel)
        except Exception as e:
            logger.warning(f"Failed to release cleanup mark for {file_id} gen {generation}: {e}")

        return deleted_count


    def list_pending_generation_cleanups(self) -> List[Dict[str, Any]]:
        now = datetime.now(UTC)
        docs = self.client.collection("pending_generation_cleanups").limit(50).stream()
        pending = []
        for d in docs:
            data = d.to_dict()
            if data.get("status") == "completed":
                continue
            lease_until_raw = data.get("lease_until")
            if lease_until_raw:
                try:
                    if isinstance(lease_until_raw, str):
                        lease_dt = datetime.fromisoformat(lease_until_raw.replace("Z", "+00:00"))
                    else:
                        lease_dt = lease_until_raw
                    if lease_dt and lease_dt > now:
                        continue
                except Exception:
                    pass
            pending.append(data)
        return pending

    def claim_pending_generation_cleanup(self, cleanup_id: str, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        from google.cloud import firestore
        now = datetime.now(UTC)
        token = uuid.uuid4().hex
        ref = self.client.collection("pending_generation_cleanups").document(cleanup_id)

        @firestore.transactional
        def _claim_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return None
            data = snap.to_dict()
            if data.get("status") == "completed":
                return None
            lease_until_raw = data.get("lease_until")
            if lease_until_raw:
                try:
                    if isinstance(lease_until_raw, str):
                        lease_dt = datetime.fromisoformat(lease_until_raw.replace("Z", "+00:00"))
                    else:
                        lease_dt = lease_until_raw
                    if lease_dt and lease_dt > now:
                        return None
                except Exception:
                    pass
            data["lease_token"] = token
            data["lease_until"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            data["status"] = "in_progress"
            data["retry_count"] = data.get("retry_count", 0) + 1
            data["updated_at"] = now.isoformat()
            tx.set(ref, data)
            return data

        tx = self.client.transaction()
        try:
            return _claim_tx(tx)
        except Exception:
            return None

    def complete_pending_generation_cleanup(self, cleanup_id: str, lease_token: str) -> bool:
        from google.cloud import firestore
        ref = self.client.collection("pending_generation_cleanups").document(cleanup_id)

        @firestore.transactional
        def _complete_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False
            data = snap.to_dict()
            if data.get("lease_token") != lease_token:
                return False
            tx.delete(ref)
            return True

        tx = self.client.transaction()
        try:
            return bool(_complete_tx(tx))
        except Exception:
            return False

    def sweep_pending_generation_cleanups(self, limit: int = 10) -> Dict[str, int]:
        now = datetime.now(UTC)
        candidates = self.list_pending_generation_cleanups()[:limit]
        claimed_count = 0
        cleaned_count = 0
        failed_count = 0
        deferred_count = 0

        for c in candidates:
            cleanup_id = c.get("cleanup_id")
            if not cleanup_id:
                continue

            # Check deferral (exponential backoff)
            next_retry_raw = c.get("next_retry_at")
            if next_retry_raw:
                try:
                    if isinstance(next_retry_raw, str):
                        next_retry_dt = datetime.fromisoformat(next_retry_raw.replace("Z", "+00:00"))
                    else:
                        next_retry_dt = next_retry_raw
                    if next_retry_dt and next_retry_dt > now:
                        deferred_count += 1
                        continue
                except Exception:
                    pass

            # Check retry ceiling
            retries = c.get("retry_count", 0)
            if retries >= 5:
                failed_count += 1
                continue

            claimed = self.claim_pending_generation_cleanup(cleanup_id, lease_seconds=60)
            if not claimed:
                deferred_count += 1
                continue

            claimed_count += 1
            space_id = claimed["space_id"]
            file_id = claimed["file_id"]
            generation = claimed["generation"]
            job_id = claimed.get("job_id")
            lease_token = claimed.get("lease_token") or ""

            try:
                self.delete_generation_document_chunks(space_id, file_id, generation, job_id)
                if self.complete_pending_generation_cleanup(cleanup_id, lease_token):
                    cleaned_count += 1
                else:
                    failed_count += 1
            except CleanupAuthorizationError as auth_err:
                # Structural denial: active generation protected, file absent, space mismatch.
                # Retrying cannot succeed — resolve the work record permanently.
                logger.warning(f"Protected skip for cleanup {cleanup_id}: {auth_err}")
                if self.complete_pending_generation_cleanup(cleanup_id, lease_token):
                    cleaned_count += 1
                else:
                    failed_count += 1
            except (StorageUnavailableError, Exception) as item_err:
                # Transient backend failure: preserve work record and apply exponential backoff.
                logger.warning(f"Error processing pending generation cleanup {cleanup_id}: {item_err}")
                failed_count += 1
                err_msg = str(item_err)
                from google.cloud import firestore as _fs
                ref = self.client.collection("pending_generation_cleanups").document(cleanup_id)

                @_fs.transactional
                def _release_on_failure_tx(tx):
                    snap = ref.get(transaction=tx)
                    if not snap.exists:
                        return  # Already completed by another worker
                    data = snap.to_dict()
                    if data.get("lease_token") != lease_token:
                        return  # Another worker has taken over — do not overwrite
                    if data.get("status") == "completed":
                        return  # Already finished by another worker
                    backoff_sec = 2 ** min(retries + 1, 6) * 5
                    tx.set(ref, {
                        **data,
                        "status": "pending",
                        "lease_token": None,
                        "lease_until": None,
                        "next_retry_at": (now + timedelta(seconds=backoff_sec)).isoformat(),
                        "last_error": err_msg,
                        "updated_at": now.isoformat(),
                    })

                try:
                    _release_on_failure_tx(self.client.transaction())
                except Exception:
                    pass

        return {
            "scanned": len(candidates),
            "claimed": claimed_count,
            "cleaned": cleaned_count,
            "failed": failed_count,
            "deferred": deferred_count,
        }

    def get_document_chunks(
        self,
        space_id: str,
        file_id: str,
        generation: Optional[int] = None,
    ) -> List[DocumentChunk]:
        target_gen = generation
        if target_gen is None:
            f = self.get_file(file_id)
            if not f or not f.active_generation or f.active_generation < 1:
                return []
            target_gen = f.active_generation

        docs = (
            self.client.collection("document_chunks")
            .where("file_id", "==", file_id)
            .where("ingestion_version", "==", target_gen)
            .stream()
        )
        chunks = []
        for d in docs:
            chunks.append(DocumentChunk.model_validate(d.to_dict()))
        chunks.sort(key=lambda c: c.ordinal)
        return chunks

    def prune_document_chunks(
        self,
        space_id: str,
        file_id: str,
        before_generation: int,
    ) -> None:
        super().prune_document_chunks(space_id, file_id, before_generation)
        docs = (
            self.client.collection("document_chunks")
            .where("file_id", "==", file_id)
            .where("ingestion_version", "<", before_generation)
            .stream()
        )
        batch = self.client.batch()
        count = 0
        for d in docs:
            batch.delete(d.reference)
            count += 1
            if count >= 400:
                batch.commit()
                batch = self.client.batch()
                count = 0
        if count > 0:
            batch.commit()

    def delete_all_document_chunks(self, space_id: str, file_id: str) -> None:
        super().delete_all_document_chunks(space_id, file_id)
        docs = (
            self.client.collection("document_chunks")
            .where("file_id", "==", file_id)
            .stream()
        )
        batch = self.client.batch()
        count = 0
        for d in docs:
            batch.delete(d.reference)
            count += 1
            if count >= 400:
                batch.commit()
                batch = self.client.batch()
                count = 0
        if count > 0:
            batch.commit()

    def acquire_ingestion_lease(
        self,
        space_id: str,
        file_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Tuple[bool, Optional[FileRecord], int]:
        from google.cloud import firestore

        file_ref = self.client.collection("files").document(file_id)

        @firestore.transactional
        def _acquire_tx(tx):
            snap = file_ref.get(transaction=tx)
            if not snap.exists:
                return (False, None, 0)
            f = FileRecord.model_validate(snap.to_dict())
            if f.space_id != space_id or f.upload_status != "committed":
                return (False, None, 0)

            now = datetime.now(UTC)
            if (
                f.ingestion_status in (IngestionStatus.EXTRACTING, IngestionStatus.INDEXING)
                and f.ingestion_lease_until is not None
                and f.ingestion_lease_until > now
                and f.ingestion_lease_owner != worker_id
                and f.ingestion_lease_owner != "lease_reclaimer"
            ):
                return (False, f, f.active_generation)

            target_gen = max(f.max_allocated_generation or 0, f.active_generation or 0) + 1
            job_id = f"job_ingest_{file_id}_g{target_gen}_{uuid.uuid4().hex[:8]}"

            f.max_allocated_generation = target_gen
            f.ingestion_status = IngestionStatus.EXTRACTING
            f.ingestion_job_id = job_id
            f.ingestion_lease_owner = worker_id
            f.ingestion_lease_until = now + timedelta(seconds=lease_seconds)
            f.ingestion_started_at = now
            f.ingestion_version = (f.ingestion_version or 0) + 1
            f.ingestion_error_code = None
            f.ingestion_error_message = None

            tx.set(file_ref, f.model_dump(mode="json"))
            return (True, f, target_gen)

        try:
            tx = self.client.transaction()
            res = _acquire_tx(tx)
            if res[0] and res[1]:
                super().save_file(res[1])
            return res
        except Exception as e:
            logger.warning(f"Firestore acquire_ingestion_lease failed: {e}")
            return (False, None, 0)

    def commit_ingestion_generation(
        self,
        space_id: str,
        file_id: str,
        target_generation: int,
        expected_job_id: str,
        chunk_count: int,
        pages: int,
        ocr_gaps: List[int],
        status: IngestionStatus = IngestionStatus.READY,
        error_code: Optional[str] = None,
        error_msg: Optional[str] = None,
    ) -> bool:
        from google.cloud import firestore

        file_ref = self.client.collection("files").document(file_id)

        @firestore.transactional
        def _commit_tx(tx):
            snap = file_ref.get(transaction=tx)
            if not snap.exists:
                return (False, None)
            f = FileRecord.model_validate(snap.to_dict())
            if f.space_id != space_id or f.ingestion_job_id != expected_job_id:
                return (False, None)
            if f.ingestion_status not in (IngestionStatus.EXTRACTING, IngestionStatus.INDEXING):
                return (False, None)
            now = datetime.now(UTC)
            if f.ingestion_lease_until and f.ingestion_lease_until < now:
                return (False, None)
            if f.max_allocated_generation and f.max_allocated_generation != target_generation:
                return (False, None)
            if target_generation in (f.cleaning_generations or []):
                logger.warning(
                    f"Rejected commit for {file_id}: target generation {target_generation} is currently being cleaned"
                )
                return (False, None)
            if target_generation in (f.deleted_generations or []):
                logger.warning(
                    f"Rejected commit for {file_id}: target generation {target_generation} has been permanently tombstoned (already cleaned)"
                )
                return (False, None)

            f.active_generation = target_generation
            f.committed_generations = list(set((f.committed_generations or []) + [target_generation]))
            f.ingestion_status = status
            f.chunk_count = chunk_count
            f.extracted_pages = pages
            f.ocr_gap_pages = ocr_gaps
            f.has_ocr_gaps = len(ocr_gaps) > 0
            f.ingestion_lease_owner = None
            f.ingestion_lease_until = None
            f.ingestion_completed_at = now
            f.ingestion_error_code = error_code
            f.ingestion_error_message = error_msg

            tx.set(file_ref, f.model_dump(mode="json"))
            return (True, f)

        try:
            tx = self.client.transaction()
            ok, updated_file = _commit_tx(tx)
            if ok and updated_file:
                super().save_file(updated_file)
                self.prune_document_chunks(space_id, file_id, before_generation=target_generation)
                return True
            return False
        except Exception as e:
            logger.warning(f"Firestore commit_ingestion_generation failed: {e}")
            return False

    def fail_ingestion_generation(
        self,
        space_id: str,
        file_id: str,
        expected_job_id: Optional[str] = None,
        error_code: Optional[str] = "ERR_INGESTION_FAILED",
        error_msg: Optional[str] = None,
    ) -> bool:
        from google.cloud import firestore

        file_ref = self.client.collection("files").document(file_id)

        @firestore.transactional
        def _fail_tx(tx):
            snap = file_ref.get(transaction=tx)
            if not snap.exists:
                return (False, None)
            f = FileRecord.model_validate(snap.to_dict())
            if expected_job_id and f.ingestion_job_id != expected_job_id:
                return (False, None)

            now = datetime.now(UTC)
            f.ingestion_status = IngestionStatus.FAILED
            f.ingestion_lease_owner = None
            f.ingestion_lease_until = None
            f.ingestion_completed_at = now
            f.ingestion_error_code = error_code
            f.ingestion_error_message = error_msg

            tx.set(file_ref, f.model_dump(mode="json"))
            return (True, f)

        try:
            tx = self.client.transaction()
            ok, updated_file = _fail_tx(tx)
            if ok and updated_file:
                super().save_file(updated_file)
                return True
            return False
        except Exception as e:
            logger.warning(f"Firestore fail_ingestion_generation failed: {e}")
            return False

    def scan_and_reclaim_expired_ingestion_leases(
        self,
        now: Optional[datetime] = None,
    ) -> List[FileRecord]:
        from google.cloud import firestore

        curr_now = now or datetime.now(UTC)
        docs_active = list(
            self.client.collection("files")
            .where("ingestion_status", "in", [IngestionStatus.EXTRACTING.value, IngestionStatus.INDEXING.value])
            .stream()
        )
        docs_failed = list(
            self.client.collection("files")
            .where("ingestion_status", "==", IngestionStatus.FAILED.value)
            .stream()
        )
        docs_pending = list(
            self.client.collection("files")
            .where("ingestion_status", "==", IngestionStatus.PENDING.value)
            .stream()
        )
        docs = docs_active + docs_failed + docs_pending
        reclaimed = []
        for d in docs:
            file_ref = d.reference

            @firestore.transactional
            def _reclaim_single(tx, ref):
                snap = ref.get(transaction=tx)
                if not snap.exists:
                    return None
                f = FileRecord.model_validate(snap.to_dict())
                is_expired_lease = (
                    f.ingestion_status in (IngestionStatus.EXTRACTING, IngestionStatus.INDEXING)
                    and f.ingestion_lease_until
                    and f.ingestion_lease_until < curr_now
                )
                is_retryable_failure = (
                    f.ingestion_status == IngestionStatus.FAILED
                    and f.ingestion_error_code in ("ERR_ENQUEUE_FAILED", "ERR_CLOUD_TASKS_ENQUEUE_FAILED", "LEASE_EXPIRED", "ERR_LEASE_TIMEOUT")
                )
                is_stalled_pending = (
                    f.upload_status == "committed"
                    and f.ingestion_status == IngestionStatus.PENDING
                    and f.active_generation == 0
                    and (f.ingestion_started_at is None or f.ingestion_started_at < curr_now - timedelta(minutes=2))
                )
                if is_expired_lease or is_retryable_failure or is_stalled_pending:
                    target_gen = max(f.max_allocated_generation or 0, f.active_generation or 0) + 1
                    job_id = f"job_ingest_{f.file_id}_g{target_gen}_{uuid.uuid4().hex[:8]}"
                    f.max_allocated_generation = target_gen
                    f.ingestion_job_id = job_id
                    f.ingestion_status = IngestionStatus.EXTRACTING
                    f.ingestion_lease_owner = "lease_reclaimer"
                    f.ingestion_lease_until = curr_now + timedelta(seconds=120)
                    f.ingestion_started_at = curr_now
                    f.ingestion_error_code = None
                    f.ingestion_error_message = None
                    tx.set(ref, f.model_dump(mode="json"))
                    return f
                return None

            try:
                tx = self.client.transaction()
                res = _reclaim_single(tx, file_ref)
                if res:
                    super().save_file(res)
                    reclaimed.append(res)
            except Exception as e:
                logger.warning(f"Firestore failed to reclaim lease for file {d.id}: {e}")

        return reclaimed

    def create_action_execution_if_absent(
        self, execution: ActionExecutionRecord
    ) -> Tuple[bool, ActionExecutionRecord]:
        from google.cloud import firestore

        ref = self.client.collection("action_executions").document(execution.action_id)

        @firestore.transactional
        def _create_tx(tx):
            snap = ref.get(transaction=tx)
            if snap.exists:
                return (False, ActionExecutionRecord.model_validate(snap.to_dict()))
            tx.set(ref, execution.model_dump(mode="json"))
            return (True, execution)

        try:
            tx = self.client.transaction()
            ok, result = _create_tx(tx)
            return ok, result
        except Exception as e:
            logger.error(f"Firestore create_action_execution_if_absent error: {e}")
            raise StorageUnavailableError(f"Firestore create_action_execution_if_absent failed: {e}") from e

    def get_action_execution(self, action_id: str) -> Optional[ActionExecutionRecord]:
        try:
            doc = self.client.collection("action_executions").document(action_id).get()
            if doc.exists:
                return ActionExecutionRecord.model_validate(doc.to_dict())
            return None
        except Exception as e:
            logger.error(f"Firestore get_action_execution error: {e}")
            raise StorageUnavailableError(f"Firestore get_action_execution failed: {e}") from e

    def save_action_execution_fenced(
        self,
        execution: ActionExecutionRecord,
        expected_version: int,
        expected_owner: Optional[str] = None,
        expected_token: Optional[str] = None,
    ) -> ActionExecutionRecord:
        from google.cloud import firestore

        ref = self.client.collection("action_executions").document(execution.action_id)
        now = datetime.now(UTC)

        @firestore.transactional
        def _save_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                raise StorageConflictError(f"ActionExecution '{execution.action_id}' does not exist")
            curr = ActionExecutionRecord.model_validate(snap.to_dict())
            if curr.state_version != expected_version:
                raise StorageConflictError(
                    f"Version mismatch: expected {expected_version}, got {curr.state_version}"
                )
            if expected_owner is not None and curr.lease_owner != expected_owner:
                raise StorageConflictError(
                    f"Lease owner mismatch: expected {expected_owner}, got {curr.lease_owner}"
                )
            if expected_token is not None and curr.lease_token != expected_token:
                raise StorageConflictError(
                    f"Lease token mismatch: expected {expected_token}, got {curr.lease_token}"
                )
            if curr.lease_until is not None and curr.lease_until < now and execution.status == ActionExecutionStatus.RUNNING:
                raise StorageConflictError("Active lease expired")

            execution.state_version = curr.state_version + 1
            execution.updated_at = now
            tx.set(ref, execution.model_dump(mode="json"))
            return execution

        try:
            tx = self.client.transaction()
            res = _save_tx(tx)
            return res
        except StorageConflictError:
            raise
        except Exception as e:
            logger.error(f"Firestore save_action_execution_fenced error: {e}")
            raise StorageUnavailableError(f"Firestore save_action_execution_fenced failed: {e}") from e

    def save_artifact_record(self, artifact: ArtifactDescriptor) -> ArtifactDescriptor:
        try:
            self.client.collection("artifacts").document(artifact.artifact_id).set(
                artifact.model_dump(mode="json")
            )
            return artifact
        except Exception as e:
            logger.error(f"Firestore save_artifact_record error: {e}")
            raise StorageUnavailableError(f"Firestore save_artifact_record failed: {e}") from e

    def get_artifact(self, space_id: str, artifact_id: str) -> Optional[ArtifactDescriptor]:
        try:
            doc = self.client.collection("artifacts").document(artifact_id).get()
            if doc.exists:
                art = ArtifactDescriptor.model_validate(doc.to_dict())
                if art.space_id == space_id:
                    return art
                return None
            return None
        except Exception as e:
            logger.error(f"Firestore get_artifact error: {e}")
            raise StorageUnavailableError(f"Firestore get_artifact failed: {e}") from e

    def list_stalled_action_executions(self, space_id: Optional[str] = None) -> List[ActionExecutionRecord]:
        now = datetime.now(UTC)
        try:
            query = self.client.collection("action_executions").where("status", "==", "running")
            if space_id:
                query = query.where("space_id", "==", space_id)
            docs = query.stream()
            stalled = []
            for doc in docs:
                rec = ActionExecutionRecord.model_validate(doc.to_dict())
                if rec.lease_until is not None and rec.lease_until < now:
                    stalled.append(rec)
            return stalled
        except Exception as e:
            logger.error(f"Firestore list_stalled_action_executions error: {e}")
            raise StorageUnavailableError(f"Firestore list_stalled_action_executions failed: {e}") from e

    def claim_action_dispatch(
        self, action_id: str, lease_token: str, lease_seconds: int = 30
    ) -> Tuple[bool, Optional[ActionExecutionRecord], str]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("action_executions").document(action_id)

        @firestore.transactional
        def _claim_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False, None, "NOT_FOUND"
            rec = ActionExecutionRecord.model_validate(snap.to_dict())

            if rec.dispatch_status == DispatchStatus.DISPATCHED:
                return False, rec, "ALREADY_DISPATCHED"

            was_expired_dispatching = (rec.dispatch_status == DispatchStatus.DISPATCHING)
            was_confirmation_pending = (rec.dispatch_status == DispatchStatus.DISPATCH_CONFIRMATION_PENDING)
            has_existing_task = (rec.task_name is not None)
            if rec.dispatch_status == DispatchStatus.DISPATCHING:
                if rec.dispatch_lease_until and rec.dispatch_lease_until > now:
                    return False, rec, "ACTIVE_DISPATCH_HELD"

            if rec.dispatch_status in (DispatchStatus.FAILED, DispatchStatus.DISPATCH_CONFIRMATION_PENDING):
                if rec.next_dispatch_at and rec.next_dispatch_at > now:
                    return False, rec, "BACKOFF_ACTIVE"

            rec.dispatch_status = DispatchStatus.DISPATCHING
            rec.dispatch_lease_token = lease_token
            rec.dispatch_lease_until = now + timedelta(seconds=lease_seconds)
            if not (was_expired_dispatching or was_confirmation_pending or has_existing_task):
                rec.dispatch_generation += 1
            rec.updated_at = now
            tx.set(ref, rec.model_dump(mode="json"))
            return True, rec, "CLAIMED"

        try:
            tx = self.client.transaction()
            return _claim_tx(tx)
        except Exception as e:
            logger.error(f"Firestore claim_action_dispatch error: {e}")
            raise StorageUnavailableError(f"Firestore claim_action_dispatch failed: {e}") from e

    def mark_dispatch_confirmation_uncertain(
        self, action_id: str, lease_token: str, expected_version: int, task_name: str, error: str = ""
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("action_executions").document(action_id)

        @firestore.transactional
        def _uncertain_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False
            rec = ActionExecutionRecord.model_validate(snap.to_dict())
            if rec.dispatch_status != DispatchStatus.DISPATCHING:
                return False
            if rec.dispatch_lease_token != lease_token:
                return False
            if rec.dispatch_lease_until is None or rec.dispatch_lease_until <= now:
                return False
            if rec.dispatch_version != expected_version:
                return False

            rec.dispatch_status = DispatchStatus.DISPATCH_CONFIRMATION_PENDING
            rec.dispatch_lease_token = None
            rec.dispatch_lease_until = None
            rec.task_name = task_name
            rec.dispatch_version += 1
            rec.failure_code = error or "DISPATCH_CONFIRMATION_UNCERTAIN"
            rec.next_dispatch_at = now + timedelta(seconds=5)
            rec.updated_at = now
            tx.set(ref, rec.model_dump(mode="json"))
            return True

        try:
            tx = self.client.transaction()
            return _uncertain_tx(tx)
        except Exception as e:
            logger.error(f"Firestore mark_dispatch_confirmation_uncertain error: {e}")
            raise StorageUnavailableError(f"Firestore mark_dispatch_confirmation_uncertain failed: {e}") from e

    def record_dispatch_success(
        self, action_id: str, lease_token: str, expected_version: int, task_name: str
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("action_executions").document(action_id)

        @firestore.transactional
        def _success_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False
            rec = ActionExecutionRecord.model_validate(snap.to_dict())
            if rec.dispatch_status != DispatchStatus.DISPATCHING:
                return False
            if rec.dispatch_lease_token != lease_token:
                return False
            if rec.dispatch_lease_until is None or rec.dispatch_lease_until <= now:
                return False
            if rec.dispatch_version != expected_version:
                return False

            rec.dispatch_status = DispatchStatus.DISPATCHED
            rec.dispatch_lease_token = None
            rec.dispatch_lease_until = None
            rec.task_name = task_name
            rec.dispatch_version += 1
            rec.updated_at = now
            tx.set(ref, rec.model_dump(mode="json"))
            return True

        try:
            tx = self.client.transaction()
            return _success_tx(tx)
        except Exception as e:
            logger.error(f"Firestore record_dispatch_success error: {e}")
            raise StorageUnavailableError(f"Firestore record_dispatch_success failed: {e}") from e

    def record_dispatch_failure(
        self, action_id: str, lease_token: str, expected_version: int, error: str, is_retryable: bool = True, task_name: Optional[str] = None
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("action_executions").document(action_id)

        @firestore.transactional
        def _failure_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False
            rec = ActionExecutionRecord.model_validate(snap.to_dict())
            if rec.dispatch_status != DispatchStatus.DISPATCHING:
                return False
            if rec.dispatch_lease_token != lease_token:
                return False
            if rec.dispatch_lease_until is None or rec.dispatch_lease_until <= now:
                return False
            if rec.dispatch_version != expected_version:
                return False

            rec.dispatch_status = DispatchStatus.FAILED
            rec.dispatch_lease_token = None
            rec.dispatch_lease_until = None
            if task_name:
                rec.task_name = task_name
            rec.dispatch_attempts += 1
            rec.dispatch_version += 1
            backoff_secs = min(300, 2 ** rec.dispatch_attempts)
            rec.next_dispatch_at = now + timedelta(seconds=backoff_secs)
            rec.failure_code = error
            rec.updated_at = now
            tx.set(ref, rec.model_dump(mode="json"))
            return True

        try:
            tx = self.client.transaction()
            return _failure_tx(tx)
        except Exception as e:
            logger.error(f"Firestore record_dispatch_failure error: {e}")
            raise StorageUnavailableError(f"Firestore record_dispatch_failure failed: {e}") from e

    def claim_action_execution(
        self, action_id: str, worker_id: str, lease_duration_seconds: int = 120
    ) -> Tuple[bool, Optional[ActionExecutionRecord], str]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("action_executions").document(action_id)

        @firestore.transactional
        def _claim_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False, None, "NOT_FOUND"
            rec = ActionExecutionRecord.model_validate(snap.to_dict())

            if rec.status in (ActionExecutionStatus.COMPLETED, ActionExecutionStatus.AWAITING_APPROVAL):
                return False, rec, "TERMINAL_NOOP"

            if rec.status == ActionExecutionStatus.RUNNING:
                if rec.lease_until and rec.lease_until > now:
                    return False, rec, "ACTIVE_LEASE_HELD"
                else:
                    if rec.attempts >= rec.max_attempts:
                        rec.status = ActionExecutionStatus.FAILED
                        rec.failure_code = "ERR_LEASE_EXPIRED"
                        rec.updated_at = now
                        tx.set(ref, rec.model_dump(mode="json"))
                        return False, rec, "TERMINAL_EXPIRED"

                    rec.lease_owner = worker_id
                    rec.lease_token = uuid.uuid4().hex
                    rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
                    rec.attempts += 1
                    rec.state_version += 1
                    rec.updated_at = now
                    tx.set(ref, rec.model_dump(mode="json"))
                    return True, rec, "RECLAIMED"

            if rec.status == ActionExecutionStatus.FAILED:
                if rec.is_retryable and rec.attempts < rec.max_attempts and (rec.next_retry_at is None or rec.next_retry_at <= now):
                    rec.status = ActionExecutionStatus.RUNNING
                    rec.lease_owner = worker_id
                    rec.lease_token = uuid.uuid4().hex
                    rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
                    rec.attempts += 1
                    rec.state_version += 1
                    rec.failure_code = None
                    rec.updated_at = now
                    tx.set(ref, rec.model_dump(mode="json"))
                    return True, rec, "RETRY_CLAIMED"
                return False, rec, "TERMINAL_FAILED"

            if rec.status == ActionExecutionStatus.PENDING:
                rec.status = ActionExecutionStatus.RUNNING
                rec.lease_owner = worker_id
                rec.lease_token = uuid.uuid4().hex
                rec.lease_until = now + timedelta(seconds=lease_duration_seconds)
                rec.attempts += 1
                rec.state_version += 1
                rec.updated_at = now
                tx.set(ref, rec.model_dump(mode="json"))
                return True, rec, "CLAIMED"

            return False, rec, "UNKNOWN_STATUS"

        try:
            tx = self.client.transaction()
            return _claim_tx(tx)
        except Exception as e:
            logger.error(f"Firestore claim_action_execution error: {e}")
            raise StorageUnavailableError(f"Firestore claim_action_execution failed: {e}") from e

    def heartbeat_action_execution(
        self, action_id: str, worker_id: str, lease_token: str, extension_seconds: int = 60
    ) -> Tuple[bool, int]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("action_executions").document(action_id)

        @firestore.transactional
        def _hb_tx(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False, 0
            rec = ActionExecutionRecord.model_validate(snap.to_dict())
            if rec.status != ActionExecutionStatus.RUNNING:
                return False, rec.state_version
            if rec.lease_owner != worker_id or rec.lease_token != lease_token:
                return False, rec.state_version
            if rec.lease_until is None or rec.lease_until <= now:
                return False, rec.state_version

            rec.lease_until = now + timedelta(seconds=extension_seconds)
            rec.state_version += 1
            rec.updated_at = now
            tx.set(ref, rec.model_dump(mode="json"))
            return True, rec.state_version

        try:
            tx = self.client.transaction()
            return _hb_tx(tx)
        except Exception as e:
            logger.error(f"Firestore heartbeat_action_execution error: {e}")
            raise StorageUnavailableError(f"Firestore heartbeat_action_execution failed: {e}") from e

    def list_undispatched_actions(self, space_id: Optional[str] = None, timeout_seconds: int = 30) -> List[ActionExecutionRecord]:
        now = datetime.now(UTC)
        threshold = now - timedelta(seconds=timeout_seconds)
        try:
            query = self.client.collection("action_executions").where("status", "==", "pending")
            if space_id:
                query = query.where("space_id", "==", space_id)
            docs = query.stream()
            undispatched = []
            for d in docs:
                rec = ActionExecutionRecord.model_validate(d.to_dict())
                if rec.dispatch_status == DispatchStatus.FAILED and (rec.next_dispatch_at is None or rec.next_dispatch_at <= now):
                    undispatched.append(rec)
                elif rec.dispatch_status == DispatchStatus.DISPATCHING and (rec.dispatch_lease_until is None or rec.dispatch_lease_until < now):
                    undispatched.append(rec)
                elif rec.dispatch_status == DispatchStatus.PENDING and rec.created_at <= threshold:
                    undispatched.append(rec)
            return undispatched
        except Exception as e:
            logger.error(f"Firestore list_undispatched_actions error: {e}")
            raise StorageUnavailableError(f"Firestore list_undispatched_actions failed: {e}") from e

    def reclaim_action_execution_lease(
        self,
        action_id: str,
        lease_token: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
        is_terminal: bool = False,
        failure_code: Optional[str] = None,
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        exec_ref = self.client.collection("action_executions").document(action_id)

        @firestore.transactional
        def _reclaim_tx(tx):
            snap = exec_ref.get(transaction=tx)
            if not snap.exists:
                return False
            rec = ActionExecutionRecord.model_validate(snap.to_dict())
            if lease_token and rec.lease_token != lease_token:
                return False
            if not is_terminal and rec.lease_until and rec.lease_until > now:
                return False

            if is_terminal:
                rec.status = ActionExecutionStatus.FAILED
                rec.failure_code = failure_code or "ERR_LEASE_EXPIRED"
                rec.lease_owner = None
                rec.lease_token = None
                rec.lease_until = None
                rec.updated_at = now
                tx.set(exec_ref, rec.model_dump(mode="json"))

                # Also fail linked run if in RUNNING
                run_ref = self.client.collection("runs").document(rec.run_id)
                run_snap = run_ref.get(transaction=tx)
                if run_snap.exists:
                    run_dict = run_snap.to_dict()
                    if run_dict.get("status") == "running":
                        run_dict["status"] = "failed"
                        run_dict["failure_code"] = failure_code or "ERR_LEASE_EXPIRED"
                        run_dict["updated_at"] = now.isoformat()
                        tx.set(run_ref, run_dict)
            else:
                rec.status = ActionExecutionStatus.PENDING
                rec.lease_owner = None
                rec.lease_token = None
                rec.lease_until = None
                rec.next_retry_at = next_retry_at or now
                rec.updated_at = now
                tx.set(exec_ref, rec.model_dump(mode="json"))
            return True

        try:
            tx = self.client.transaction()
            return _reclaim_tx(tx)
        except Exception as e:
            logger.error(f"Firestore reclaim_action_execution_lease error: {e}")
            raise StorageUnavailableError(f"Firestore reclaim_action_execution_lease failed: {e}") from e

    def save_run(self, run: Run) -> Run:
        super().save_run(run)
        try:
            doc_ref = self.client.collection("runs").document(run.run_id)
            doc_snap = doc_ref.get()
            if not doc_snap.exists:
                doc_ref.set(run.model_dump(mode="json"))
        except Exception as e:
            logger.error(f"Firestore save_run error: {e}")
            raise StorageUnavailableError(f"Firestore save_run failed: {e}") from e
        return run

    def get_run(self, run_id: str) -> Optional[Run]:
        try:
            doc = self.client.collection("runs").document(run_id).get()
            if doc.exists:
                r = Run.model_validate(doc.to_dict())
                with self._lock:
                    self.runs[run_id] = r
                return r
        except Exception as e:
            logger.error(f"Firestore get_run error: {e}")
            raise StorageUnavailableError(f"Firestore get_run failed: {e}") from e
    def list_stalled_approval_runs(
        self, space_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> List[Run]:
        curr_now = now or datetime.now(UTC)
        try:
            query = self.client.collection("runs").where("status", "==", "running")
            if space_id:
                query = query.where("space_id", "==", space_id)
            docs = query.stream()
            stalled = []
            for doc in docs:
                r = Run.model_validate(doc.to_dict())
                gate = r.approval_gate
                is_stalled = False
                if gate and gate.status == "approving":
                    if gate.decision_lease_until and gate.decision_lease_until <= curr_now:
                        is_stalled = True
                    elif getattr(r, "uncertain_since", None):
                        if r.uncertain_since + timedelta(seconds=30) <= curr_now:
                            is_stalled = True
                    elif getattr(r, "approval_commit_status", None) == "uncertain":
                        is_stalled = True
                elif getattr(r, "approval_commit_status", None) == "uncertain":
                    is_stalled = True
                if is_stalled:
                    stalled.append(r)
            return stalled
        except Exception as e:
            logger.error(f"Firestore list_stalled_approval_runs error: {e}")
            raise StorageUnavailableError(f"Firestore list_stalled_approval_runs failed: {e}") from e

    def update_run_telemetry_status(
        self,
        space_id: str,
        run_id: str,
        expected_trace_id: str,
        new_status: str,
        expected_generation: Optional[int] = None,
        expected_status: Optional[str] = None,
        expected_lease_token: Optional[str] = None,
        next_check_at: Optional[datetime] = None,
    ) -> Optional[Run]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        run_ref = self.client.collection("runs").document(run_id)

        @firestore.transactional
        def _update_tx(tx):
            now = datetime.now(UTC)
            snap = run_ref.get(transaction=tx)
            if not snap.exists:
                return None
            run = Run.model_validate(snap.to_dict())
            if run.space_id != space_id or run.trace_id != expected_trace_id:
                return None
            if expected_generation is not None and getattr(run, "telemetry_generation", 1) != expected_generation:
                return None
            if expected_status is not None and getattr(run, "telemetry_status", "not_instrumented") != expected_status:
                return None
            if expected_lease_token is not None:
                if getattr(run, "telemetry_lease_token", None) != expected_lease_token:
                    return None
                lease_until = getattr(run, "telemetry_lease_until", None)
                if lease_until is None or lease_until <= now:
                    return None

            run.telemetry_status = new_status
            run.telemetry_lease_token = None
            run.telemetry_lease_until = None
            run.telemetry_next_check_at = next_check_at
            if run.telemetry:
                run.telemetry.telemetry_status = new_status
                run.telemetry.has_real_telemetry = (new_status == "available")
                run.telemetry.telemetry_last_checked_at = now
                run.telemetry.telemetry_lease_token = None
                run.telemetry.telemetry_lease_until = None
                run.telemetry.telemetry_next_check_at = next_check_at
            run.telemetry_last_checked_at = now
            run.telemetry_attempts = (getattr(run, "telemetry_attempts", 0) or 0) + 1
            if run.telemetry:
                run.telemetry.telemetry_attempts = run.telemetry_attempts
            run.telemetry_generation = (getattr(run, "telemetry_generation", 1) or 1) + 1
            if run.telemetry:
                run.telemetry.telemetry_generation = run.telemetry_generation
            run.updated_at = now

            tx.set(run_ref, run.model_dump(mode="json"))
            return run

        try:
            tx = self.client.transaction()
            res = _update_tx(tx)
            if res:
                with self._lock:
                    self.runs[run_id] = res
            return res
        except Exception as e:
            logger.error(f"Firestore update_run_telemetry_status failed: {e}")
            raise StorageUnavailableError(f"Firestore update_run_telemetry_status failed: {e}") from e

    def claim_run_telemetry_verification(
        self,
        space_id: str,
        run_id: str,
        lease_seconds: float = 10.0,
        force: bool = False,
        min_interval_seconds: float = 2.0,
    ) -> Optional[Tuple[Run, str]]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        run_ref = self.client.collection("runs").document(run_id)

        @firestore.transactional
        def _claim_tx(tx):
            now = datetime.now(UTC)
            snap = run_ref.get(transaction=tx)
            if not snap.exists:
                return None
            run = Run.model_validate(snap.to_dict())
            if run.space_id != space_id:
                return None
            if run.telemetry_status == "available":
                return None

            # Terminal / Attempt budget check
            terminal_cooldown = getattr(settings, "TELEMETRY_TERMINAL_COOLDOWN_SECONDS", 60.0)
            max_attempts = getattr(settings, "TELEMETRY_VERIFICATION_MAX_ATTEMPTS", 3)
            is_terminal = (
                run.telemetry_status == "unavailable"
                or (getattr(run, "telemetry_attempts", 0) or 0) >= max_attempts
            )
            if is_terminal:
                if run.telemetry_last_checked_at:
                    elapsed = (now - run.telemetry_last_checked_at).total_seconds()
                    if elapsed < terminal_cooldown:
                        return None
                if not force:
                    return None
                run.telemetry_attempts = 0
                if run.telemetry:
                    run.telemetry.telemetry_attempts = 0

            if run.telemetry_lease_until and run.telemetry_lease_until > now:
                return None

            if run.telemetry_last_checked_at:
                elapsed = (now - run.telemetry_last_checked_at).total_seconds()
                hard_floor = 1.0 if force else min_interval_seconds
                if elapsed < hard_floor:
                    return None

            if run.telemetry_next_check_at and run.telemetry_next_check_at > now and not force:
                return None

            token = f"tl_{uuid.uuid4().hex[:12]}"
            lease_until = now + timedelta(seconds=lease_seconds)
            run.telemetry_lease_token = token
            run.telemetry_lease_until = lease_until
            run.telemetry_last_checked_at = now
            if run.telemetry:
                run.telemetry.telemetry_lease_token = token
                run.telemetry.telemetry_lease_until = lease_until
                run.telemetry.telemetry_last_checked_at = now
            run.updated_at = now

            tx.set(run_ref, run.model_dump(mode="json"))
            return run, token

        try:
            tx = self.client.transaction()
            res = _claim_tx(tx)
            if res:
                run, token = res
                with self._lock:
                    self.runs[run_id] = run
            return res
        except Exception as e:
            logger.error(f"Firestore claim_run_telemetry_verification failed: {e}")
            raise StorageUnavailableError(f"Firestore claim_run_telemetry_verification failed: {e}") from e

    def get_telemetry_scan_cursor(self, space_id: Optional[str] = None) -> Optional[str]:
        scope_key = f"scope_{space_id}" if space_id else "scope___all__"
        try:
            doc = self.client.collection("telemetry_scan_cursors").document(scope_key).get()
            if doc and getattr(doc, "exists", False):
                return (doc.to_dict() or {}).get("cursor_run_id")
        except Exception as e:
            logger.debug("Failed to get scan cursor for %s: %s", scope_key, e)
        return None

    def list_pending_telemetry_runs(
        self,
        space_id: Optional[str] = None,
        limit: int = 50,
        now: Optional[datetime] = None,
        cursor: Optional[str] = None,
        max_scan: Optional[int] = None,
    ) -> List[Run]:
        curr_now = now or datetime.now(UTC)
        scope_key = f"scope_{space_id}" if space_id else "scope___all__"
        cursor_ref = self.client.collection("telemetry_scan_cursors").document(scope_key)

        start_cursor = cursor
        lease_token = None
        expected_version = 0

        if cursor is None:
            # Atomic lease claim / cursor retrieval via transaction
            from google.cloud import firestore

            @firestore.transactional
            def _claim_cursor_tx(tx):
                now_dt = datetime.now(UTC)
                snap = cursor_ref.get(transaction=tx)
                rec = snap.to_dict() if getattr(snap, "exists", False) else {}
                l_until_str = rec.get("lease_until")
                l_until = datetime.fromisoformat(l_until_str) if l_until_str else None
                if l_until and l_until > now_dt and rec.get("lease_token"):
                    return (None, None, 0, True)
                token = secrets.token_hex(8)
                lease_exp = now_dt + timedelta(seconds=15)
                ver = (rec.get("version", 0) or 0) + 1
                tx.set(cursor_ref, {
                    "scope_key": scope_key,
                    "cursor_run_id": rec.get("cursor_run_id"),
                    "lease_token": token,
                    "lease_until": lease_exp.isoformat(),
                    "version": ver,
                    "updated_at": now_dt.isoformat(),
                })
                return (rec.get("cursor_run_id"), token, ver, False)

            try:
                tx = self.client.transaction()
                start_cursor, lease_token, expected_version, is_busy = _claim_cursor_tx(tx)
                if is_busy:
                    logger.info("Scan for scope %s is leased by another worker; skipping", scope_key)
                    return []
            except Exception as e:
                logger.error("Firestore claim_cursor_tx failed for %s: %s", scope_key, e)
                raise StorageUnavailableError(f"Failed to claim telemetry scan cursor for {scope_key}: {e}") from e

        try:
            query = self.client.collection("runs").where(
                "telemetry_status", "in", ["exporting", "delayed"]
            )
            if space_id:
                query = query.where("space_id", "==", space_id)

            if hasattr(query, "order_by"):
                query = query.order_by("__name__")

            if start_cursor:
                cursor_snap = self.client.collection("runs").document(start_cursor).get()
                if cursor_snap and getattr(cursor_snap, "exists", False) and hasattr(query, "start_after"):
                    query = query.start_after(cursor_snap)
                else:
                    logger.info("Cursor run %s was deleted or not found; resetting cursor for %s", start_cursor, scope_key)
                    start_cursor = None

            scan_budget = max_scan if max_scan is not None else max(limit * 10, 500)
            candidates = []
            scanned = 0
            last_scanned_id = None
            reached_stream_end = True

            for doc in query.stream():
                scanned += 1
                last_scanned_id = doc.id
                run = Run.model_validate(doc.to_dict())
                if run.telemetry_lease_until and run.telemetry_lease_until > curr_now:
                    if scanned >= scan_budget:
                        reached_stream_end = False
                        break
                    continue
                if run.telemetry_next_check_at and run.telemetry_next_check_at > curr_now:
                    if scanned >= scan_budget:
                        reached_stream_end = False
                        break
                    continue
                candidates.append(run)
                if len(candidates) >= limit:
                    reached_stream_end = False
                    break
                if scanned >= scan_budget:
                    reached_stream_end = False
                    break

            new_cursor = None if reached_stream_end else last_scanned_id

            # Only commit updated cursor state if this was a leased scan (explicit cursor is strictly read-only)
            if cursor is None and lease_token:
                from google.cloud import firestore

                @firestore.transactional
                def _commit_cursor_tx(tx):
                    now_dt = datetime.now(UTC)
                    snap = cursor_ref.get(transaction=tx)
                    if not getattr(snap, "exists", False):
                        logger.warning("Commit rejected: cursor document %s does not exist", scope_key)
                        return False
                    rec = snap.to_dict() or {}

                    # 1. Holding valid token
                    if not lease_token:
                        logger.warning("Commit rejected: missing caller lease_token")
                        return False

                    # 2. Database token exact match
                    db_token = rec.get("lease_token")
                    if not db_token or db_token != lease_token:
                        logger.warning(
                            "Commit rejected: lease token mismatch for %s (expected %s, got %s)",
                            scope_key, lease_token, db_token
                        )
                        return False

                    # 3. Database version exact match (CAS)
                    db_version = rec.get("version")
                    if db_version != expected_version:
                        logger.warning(
                            "Commit rejected: version mismatch for %s (expected %s, got %s)",
                            scope_key, expected_version, db_version
                        )
                        return False

                    # 4. lease_until exists and is strictly in the future
                    l_until_str = rec.get("lease_until")
                    if not l_until_str:
                        logger.warning("Commit rejected: lease_until missing for %s", scope_key)
                        return False
                    l_until = datetime.fromisoformat(l_until_str)
                    if l_until <= now_dt:
                        logger.warning(
                            "Commit rejected: lease expired for %s (lease_until %s <= now %s)",
                            scope_key, l_until, now_dt
                        )
                        return False

                    ver = (db_version or 0) + 1
                    tx.set(cursor_ref, {
                        "scope_key": scope_key,
                        "cursor_run_id": new_cursor,
                        "lease_token": None,
                        "lease_until": None,
                        "version": ver,
                        "updated_at": now_dt.isoformat(),
                    })
                    return True

                try:
                    tx = self.client.transaction()
                    committed = _commit_cursor_tx(tx)
                    if not committed:
                        logger.warning(
                            "Cursor commit rejected for %s (preempted or expired); results preserved", scope_key
                        )
                except Exception as e:
                    logger.error("Failed to commit telemetry scan cursor for %s: %s", scope_key, e)
                    raise StorageUnavailableError(f"Failed to commit telemetry scan cursor for {scope_key}: {e}") from e

            candidates.sort(
                key=lambda x: x.telemetry_last_checked_at.timestamp() if x.telemetry_last_checked_at else 0
            )
            return candidates[:limit]
        except StorageUnavailableError:
            raise
        except Exception as e:
            logger.error(f"Firestore list_pending_telemetry_runs failed: {e}")
            raise StorageUnavailableError(f"Firestore list_pending_telemetry_runs failed: {e}") from e

    def reconcile_run_approval_atomic(
        self,
        space_id: str,
        run_id: str,
        target_status: RunStatus,
        approval_commit_status: str,
        gate_status: str,
        expected_run_status: Optional[RunStatus] = None,
        failure_code: Optional[str] = None,
        error_summary: Optional[str] = None,
        cleanup_items: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, Optional[Run]]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        run_ref = self.client.collection("runs").document(run_id)

        @firestore.transactional
        def _reconcile_tx(tx):
            # Phase 1: Reads
            snap = run_ref.get(transaction=tx)
            if not snap.exists:
                return False, None
            run_dict = snap.to_dict()
            run = Run.model_validate(run_dict)
            if run.space_id != space_id:
                return False, None

            # Idempotent return if already in target status and approval_commit_status
            if run.status == target_status and getattr(run, "approval_commit_status", None) == approval_commit_status:
                return True, run

            if expected_run_status and run.status != expected_run_status:
                return False, None

            # Synchronize linked ActionExecution read
            target_act_id = run.action_id
            exec_ref = None
            exec_snap = None
            if target_act_id:
                exec_ref = self.client.collection("action_executions").document(target_act_id)
                exec_snap = exec_ref.get(transaction=tx)

            # TOCTOU read all deliverables in transaction
            deliverable_ids = list(dict.fromkeys((run.output_artifact_ids or []) + ([run.manifest_file_id] if run.manifest_file_id else [])))
            art_snaps = {}
            file_snaps = {}
            for art_id in deliverable_ids:
                a_ref = self.client.collection("artifacts").document(art_id)
                art_snaps[art_id] = a_ref.get(transaction=tx)
                f_ref = self.client.collection("files").document(art_id)
                file_snaps[art_id] = f_ref.get(transaction=tx)

            # Phase 2: Validations
            if target_status == RunStatus.COMPLETED:
                for art_id in deliverable_ids:
                    a_snap = art_snaps.get(art_id)
                    f_snap = file_snaps.get(art_id)
                    if not (a_snap and a_snap.exists and a_snap.to_dict().get("visibility") == "published"):
                        return False, None
                    if not (f_snap and f_snap.exists and f_snap.to_dict().get("publication_status") == "published"):
                        return False, None
            elif target_status == RunStatus.FAILED:
                if deliverable_ids:
                    all_pub = all(
                        (a := art_snaps.get(aid)) and a.exists and a.to_dict().get("visibility") == "published"
                        and (f := file_snaps.get(aid)) and f.exists and f.to_dict().get("publication_status") == "published"
                        for aid in deliverable_ids
                    )
                    if all_pub:
                        # Already published; cannot abort
                        return False, None

            # Phase 3: Writes
            run.status = target_status
            run.approval_commit_status = approval_commit_status
            run.uncertain_since = None
            run.state_version = (getattr(run, "state_version", 1) or 1) + 1
            run.updated_at = now

            if run.approval_gate:
                run.approval_gate.status = gate_status
                run.approval_gate.decided_at = now
                run.approval_gate.decision_lease_token = None
                run.approval_gate.decision_lease_until = None

            if target_status == RunStatus.FAILED:
                run.failure_code = failure_code or "APPROVAL_RECONCILE_ABORTED"
                run.is_retryable = True
                run.error_summary = error_summary

            if exec_ref and exec_snap and exec_snap.exists:
                act_exec = ActionExecutionRecord.model_validate(exec_snap.to_dict())
                if target_status == RunStatus.COMPLETED:
                    act_exec.status = ActionExecutionStatus.COMPLETED
                elif target_status == RunStatus.FAILED:
                    act_exec.status = ActionExecutionStatus.FAILED
                    act_exec.failure_code = failure_code or "APPROVAL_RECONCILE_ABORTED"
                act_exec.state_version = (getattr(act_exec, "state_version", 1) or 1) + 1
                act_exec.updated_at = now
                tx.set(exec_ref, act_exec.model_dump(mode="json"))

            # Atomic Outbox Event
            evt_type = ActivityEventType.RUN_COMPLETED if target_status == RunStatus.COMPLETED else ActivityEventType.RUN_FAILED
            ev_id = f"run.reconciled.{target_status.value}:{run.run_id}:v{run.state_version}"
            ev = ActivityEvent(
                event_id=ev_id,
                event_type=evt_type,
                space_id=space_id,
                project_tags=[run.project_tag or "general"],
                resource_type="run",
                resource_id=run.run_id,
                summary=f"Run approval reconciled as {target_status.value}",
                details={"run_id": run.run_id, "status": target_status.value, "approval_commit_status": approval_commit_status},
                actor_uid=run.created_by,
                created_at=now,
            )
            outbox_id = f"outbox:{ev.event_id}"
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            tx.set(self.client.collection("activity_outbox").document(outbox_id), outbox_item.model_dump(mode="json"))
            tx.set(self.client.collection("activity_events").document(ev.event_id), ev.model_dump(mode="json"))

            # Atomic Cleanup Intents for Abort branch - write to artifact_cleanups collection, canonical_id only
            if target_status == RunStatus.FAILED and cleanup_items:
                for c_item in cleanup_items:
                    art_id = c_item["artifact_id"]
                    fn = c_item.get("filename", f"{art_id}.bin")
                    reason = c_item.get("reason", "APPROVAL_TRANSACTION_ABORTED")
                    ver = c_item.get("staging_version", run.state_version)
                    canonical_id = f"cleanup:{art_id}:{ver}"
                    job_dict = {
                        "job_id": canonical_id,
                        "space_id": space_id,
                        "artifact_id": art_id,
                        "filename": fn,
                        "reason": reason,
                        "status": "pending",
                        "retries": 0,
                        "max_retries": 5,
                        "created_at": now.isoformat(),
                        "staging_version": ver,
                    }
                    tx.set(self.client.collection("artifact_cleanups").document(canonical_id), job_dict)

            tx.set(run_ref, run.model_dump(mode="json"))
            return True, run

        try:
            tx = self.client.transaction()
            ok, updated_run = _reconcile_tx(tx)
            if ok and updated_run:
                with self._lock:
                    self.runs[run_id] = updated_run
            return ok, updated_run
        except Exception as e:
            logger.error(f"Firestore reconcile_run_approval_atomic error: {e}")
            raise StorageUnavailableError(f"Firestore reconcile_run_approval_atomic failed: {e}") from e

    def approve_gate_and_publish_artifacts_atomic(
        self,
        space_id: str,
        run_id: str,
        approver_uid: str,
        output_artifact_ids: List[str],
        plan: Optional[dict] = None,
        decision_lease_token: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> Tuple[Run, Optional[ActionExecutionRecord]]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        run_ref = self.client.collection("runs").document(run_id)

        @firestore.transactional
        def _approve_tx(tx):
            # =========================================================
            # PHASE 1: READ ALL PARTICIPATING DOCUMENTS FIRST (NO WRITES)
            # =========================================================
            run_snap = run_ref.get(transaction=tx)
            if not run_snap.exists:
                raise StorageConflictError(f"Run '{run_id}' not found")
            run = Run.model_validate(run_snap.to_dict())

            target_act_id = run.action_id or action_id
            exec_snap = None
            if target_act_id:
                exec_ref = self.client.collection("action_executions").document(target_act_id)
                exec_snap = exec_ref.get(transaction=tx)

            deliverable_artifact_ids = list(dict.fromkeys((run.output_artifact_ids or []) + output_artifact_ids))
            target_artifact_ids = list(deliverable_artifact_ids)
            if run.manifest_file_id and run.manifest_file_id not in target_artifact_ids:
                if run.manifest_file_id.startswith("art_") or not run.manifest_file_id.startswith("file_"):
                    target_artifact_ids.append(run.manifest_file_id)

            art_snaps = {}
            file_snaps = {}
            for art_id in target_artifact_ids:
                art_ref = self.client.collection("artifacts").document(art_id)
                art_snaps[art_id] = (art_ref, art_ref.get(transaction=tx))
                f_ref = self.client.collection("files").document(art_id)
                file_snaps[art_id] = (f_ref, f_ref.get(transaction=tx))
                if art_id.startswith("art_"):
                    mapped_fid = f"file_art_{art_id[4:]}"
                    if mapped_fid not in file_snaps:
                        f_m_ref = self.client.collection("files").document(mapped_fid)
                        file_snaps[mapped_fid] = (f_m_ref, f_m_ref.get(transaction=tx))

            if run.manifest_file_id and run.manifest_file_id not in file_snaps:
                m_f_ref = self.client.collection("files").document(run.manifest_file_id)
                file_snaps[run.manifest_file_id] = (m_f_ref, m_f_ref.get(transaction=tx))

            # =========================================================
            # PHASE 2: VALIDATE ALL INVARIANTS BEFORE ANY WRITES
            # =========================================================
            if run.space_id != space_id:
                raise StorageConflictError(f"Run '{run_id}' not in space '{space_id}'")
            if run.status != RunStatus.RUNNING:
                raise StorageConflictError(f"GATE_ALREADY_DECIDED: Run status is {run.status}")
            if not run.approval_gate or run.approval_gate.status != "approving":
                raise StorageConflictError(f"GATE_NOT_APPROVING: Gate status is {run.approval_gate.status if run.approval_gate else 'None'}, expected approving")
            if not decision_lease_token or not run.approval_gate.decision_lease_token or run.approval_gate.decision_lease_token != decision_lease_token:
                raise StorageConflictError("DECISION_TOKEN_MISMATCH: Decision lease token is missing or expired")

            exec_rec = None
            if target_act_id:
                if not exec_snap or not exec_snap.exists:
                    raise StorageConflictError(f"ActionExecution '{target_act_id}' not found")
                exec_rec = ActionExecutionRecord.model_validate(exec_snap.to_dict())
                if exec_rec.space_id != space_id:
                    raise StorageConflictError(f"ActionExecution '{target_act_id}' space mismatch")

            validated_arts = {}
            for art_id, (art_ref, art_snap) in art_snaps.items():
                if not art_snap.exists:
                    raise StorageConflictError(f"Artifact '{art_id}' not found")
                art = ArtifactDescriptor.model_validate(art_snap.to_dict())
                if art.space_id != space_id:
                    raise StorageConflictError(f"Artifact '{art_id}' space mismatch")
                if art.run_id != run_id:
                    raise StorageConflictError(f"Artifact '{art_id}' run mismatch")
                if art.visibility != "pending_approval":
                    raise StorageConflictError(f"Artifact '{art_id}' has invalid visibility: {art.visibility}, expected pending_approval")
                validated_arts[art_id] = (art_ref, art)

            # =========================================================
            # PHASE 3: WRITE ALL MUTATIONS (STRICTLY AFTER ALL READS)
            # =========================================================
            run.status = RunStatus.COMPLETED
            if run.approval_gate:
                run.approval_gate.status = "approved"
                run.approval_gate.approved_by = approver_uid
                run.approval_gate.decided_at = now
            if plan:
                run.plan = plan
            run.output_artifact_ids = deliverable_artifact_ids
            run.failure_code = None
            run.error_summary = None
            run.state_version = (getattr(run, "state_version", 1) or 1) + 1
            run.updated_at = now
            tx.set(run_ref, run.model_dump(mode="json"))

            if exec_rec:
                exec_rec.status = ActionExecutionStatus.COMPLETED
                exec_rec.output_artifact_ids = deliverable_artifact_ids
                exec_rec.updated_at = now
                tx.set(exec_ref, exec_rec.model_dump(mode="json"))

            for art_id, (art_ref, art) in validated_arts.items():
                art.visibility = "published"
                tx.set(art_ref, art.model_dump(mode="json"))

            for art_id, (f_ref, f_snap) in file_snaps.items():
                if f_snap.exists:
                    f_data = f_snap.to_dict()
                    f_data["publication_status"] = "published"
                    f_data["updated_at"] = now.isoformat()
                    tx.set(f_ref, f_data)

            outbox_id = f"outbox_gate_{run_id}_{run.state_version}_approved"
            outbox_ref = self.client.collection("activity_outbox").document(outbox_id)
            ev = ActivityEvent(
                space_id=space_id,
                project_tag=run.project_tag or "general",
                event_type=ActivityEventType.RUN_COMPLETED,
                resource_type="run",
                resource_id=run_id,
                summary=f"Run '{run.prompt or run_id}' completed and artifacts published by approval",
                actor_uid=approver_uid,
                details={"run_id": run_id, "action_id": target_act_id, "artifacts": target_artifact_ids},
            )
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=ev.event_id,
                space_id=space_id,
                event=ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            tx.set(outbox_ref, outbox_item.model_dump(mode="json"))

            return run, exec_rec

        try:
            tx = self.client.transaction()
            return _approve_tx(tx)
        except StorageConflictError:
            raise
        except Exception as e:
            logger.error(f"Firestore approve_gate_and_publish_artifacts_atomic error: {e}")
            raise StorageUnavailableError(f"Firestore approve_gate_and_publish_artifacts_atomic failed: {e}") from e

    def reject_gate_and_fail_artifacts_atomic(
        self,
        space_id: str,
        run_id: str,
        approver_uid: str,
        rejection_reason: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> Tuple[Run, Optional[ActionExecutionRecord]]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        run_ref = self.client.collection("runs").document(run_id)

        @firestore.transactional
        def _reject_tx(tx):
            # Phase 1: Reads
            run_snap = run_ref.get(transaction=tx)
            if not run_snap.exists:
                raise StorageConflictError(f"Run '{run_id}' not found")
            run = Run.model_validate(run_snap.to_dict())

            target_act_id = run.action_id or action_id
            exec_snap = None
            if target_act_id:
                exec_ref = self.client.collection("action_executions").document(target_act_id)
                exec_snap = exec_ref.get(transaction=tx)

            deliverable_art_ids = list(dict.fromkeys(run.output_artifact_ids or []))
            target_art_ids = list(deliverable_art_ids)
            if run.manifest_file_id and run.manifest_file_id not in target_art_ids:
                if run.manifest_file_id.startswith("art_") or not run.manifest_file_id.startswith("file_"):
                    target_art_ids.append(run.manifest_file_id)

            art_snaps = {}
            file_snaps = {}
            for art_id in target_art_ids:
                art_ref = self.client.collection("artifacts").document(art_id)
                art_snaps[art_id] = (art_ref, art_ref.get(transaction=tx))
                f_ref = self.client.collection("files").document(art_id)
                file_snaps[art_id] = (f_ref, f_ref.get(transaction=tx))
                if art_id.startswith("art_"):
                    mapped_fid = f"file_art_{art_id[4:]}"
                    if mapped_fid not in file_snaps:
                        f_m_ref = self.client.collection("files").document(mapped_fid)
                        file_snaps[mapped_fid] = (f_m_ref, f_m_ref.get(transaction=tx))

            if run.manifest_file_id and run.manifest_file_id not in file_snaps:
                m_f_ref = self.client.collection("files").document(run.manifest_file_id)
                file_snaps[run.manifest_file_id] = (m_f_ref, m_f_ref.get(transaction=tx))

            # Phase 2: Validations
            if run.space_id != space_id:
                raise StorageConflictError(f"Run '{run_id}' not in space '{space_id}'")
            if run.status != RunStatus.AWAITING_APPROVAL:
                raise StorageConflictError(f"ACTIVE_DECISION_HELD: Cannot reject run in status '{run.status}'")

            exec_rec = None
            if target_act_id:
                if not exec_snap or not exec_snap.exists:
                    raise StorageConflictError(f"ActionExecution '{target_act_id}' not found")
                exec_rec = ActionExecutionRecord.model_validate(exec_snap.to_dict())
                if exec_rec.space_id != space_id:
                    raise StorageConflictError(f"ActionExecution '{target_act_id}' space mismatch")

            validated_arts = {}
            for art_id, (art_ref, art_snap) in art_snaps.items():
                if art_snap.exists:
                    art = ArtifactDescriptor.model_validate(art_snap.to_dict())
                    if art.space_id != space_id or art.run_id != run_id:
                        raise StorageConflictError(f"Artifact '{art_id}' ownership mismatch")
                    validated_arts[art_id] = (art_ref, art)

            # Phase 3: Writes
            run.status = RunStatus.FAILED
            if run.approval_gate:
                run.approval_gate.status = "rejected"
                run.approval_gate.approved_by = approver_uid
                run.approval_gate.decided_at = now
            run.failure_code = "GATE_REJECTED"
            run.error_summary = rejection_reason or "Approval gate rejected by reviewer"
            run.state_version = (getattr(run, "state_version", 1) or 1) + 1
            run.updated_at = now
            tx.set(run_ref, run.model_dump(mode="json"))

            if exec_rec:
                exec_rec.status = ActionExecutionStatus.FAILED
                exec_rec.failure_code = "GATE_REJECTED"
                exec_rec.updated_at = now
                tx.set(exec_ref, exec_rec.model_dump(mode="json"))

            for art_id, (art_ref, art) in validated_arts.items():
                art.visibility = "rejected"
                tx.set(art_ref, art.model_dump(mode="json"))

            for art_id, (f_ref, f_snap) in file_snaps.items():
                if f_snap.exists:
                    f_data = f_snap.to_dict()
                    f_data["publication_status"] = "rejected"
                    f_data["updated_at"] = now.isoformat()
                    tx.set(f_ref, f_data)

            outbox_id = f"outbox_gate_rej_{run_id}_{run.state_version}_rejected"
            outbox_ref = self.client.collection("activity_outbox").document(outbox_id)
            rej_ev = ActivityEvent(
                space_id=space_id,
                project_tag=run.project_tag or "general",
                event_type=ActivityEventType.RUN_FAILED,
                resource_type="gate",
                resource_id=run.approval_gate.gate_id if run.approval_gate else run_id,
                summary=f"Approval gate rejected for '{run.prompt or run_id}'",
                actor_uid=approver_uid,
                details={"run_id": run_id, "action_id": target_act_id, "reason": rejection_reason},
            )
            outbox_item = ActivityOutboxItem(
                outbox_id=outbox_id,
                event_id=rej_ev.event_id,
                space_id=space_id,
                event=rej_ev,
                status=OutboxStatus.PENDING,
                attempts=0,
                max_attempts=5,
                created_at=now,
                updated_at=now,
                next_retry_at=now,
            )
            tx.set(outbox_ref, outbox_item.model_dump(mode="json"))

            return run, exec_rec

        try:
            tx = self.client.transaction()
            return _reject_tx(tx)
        except StorageConflictError:
            raise
        except Exception as e:
            logger.error(f"Firestore reject_gate_and_fail_artifacts_atomic error: {e}")
            raise StorageUnavailableError(f"Firestore reject_gate_and_fail_artifacts_atomic failed: {e}") from e

    def enqueue_artifact_cleanup(
        self, space_id: str, artifact_id: str, filename: str, reason: str, staging_version: int = 1
    ) -> str:
        now = datetime.now(UTC)
        try:
            job_id = f"cleanup:{artifact_id}:{staging_version}"
            self.client.collection("artifact_cleanups").document(job_id).set({
                "job_id": job_id,
                "space_id": space_id,
                "artifact_id": artifact_id,
                "filename": filename,
                "reason": reason,
                "status": "pending",
                "version": 1,
                "staging_version": staging_version,
                "expected_publication_status": "pending_approval",
                "retries": 0,
                "next_retry_at": now.isoformat(),
                "created_at": now.isoformat(),
            })
            return job_id
        except Exception as e:
            logger.error(f"Firestore enqueue_artifact_cleanup error: {e}")
            raise StorageUnavailableError(f"Firestore enqueue_artifact_cleanup failed: {e}") from e

    def list_pending_artifact_cleanups(self) -> List[dict]:
        try:
            docs = self.client.collection("artifact_cleanups").where("status", "==", "pending").stream()
            return [d.to_dict() for d in docs]
        except Exception as e:
            logger.error(f"Firestore list_pending_artifact_cleanups error: {e}")
            raise StorageUnavailableError(f"Firestore list_pending_artifact_cleanups failed: {e}") from e

    def claim_artifact_cleanup_job(
        self, job_id: str, worker_id: str, lease_seconds: int = 60
    ) -> Optional[dict]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("artifact_cleanups").document(job_id)

        @firestore.transactional
        def _claim_tx(tx):
            # Phase 1: Reads ONLY
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return None
            job = snap.to_dict()

            art_id = job.get("artifact_id")
            art_ref = None
            art_snap = None
            file_ref = None
            file_snap = None
            if art_id:
                art_ref = self.client.collection("artifacts").document(art_id)
                art_snap = art_ref.get(transaction=tx)
                file_ref = self.client.collection("files").document(art_id)
                file_snap = file_ref.get(transaction=tx)

            # Phase 2: Validation and in-memory preparation
            if job.get("status") != "pending":
                return None
            lease_until_str = job.get("lease_until")
            if lease_until_str:
                lease_until = datetime.fromisoformat(lease_until_str)
                if lease_until > now and job.get("lease_owner") != worker_id:
                    return None
            next_retry_str = job.get("next_retry_at")
            if next_retry_str:
                next_retry = datetime.fromisoformat(next_retry_str)
                if next_retry > now:
                    return None

            # Mutual exclusion: if artifact was already published, cancel job instead of claiming!
            if art_id:
                is_art_pub = art_snap.exists and art_snap.to_dict().get("visibility") == "published"
                is_file_pub = file_snap.exists and file_snap.to_dict().get("publication_status") == "published"
                if is_art_pub or is_file_pub:
                    job["status"] = "cancelled"
                    job["reason"] = "ALREADY_PUBLISHED"
                    # Phase 3: Write cancellation and return None
                    tx.set(ref, job)
                    return None

            job["status"] = "in_progress"
            job["lease_owner"] = worker_id
            job["lease_token"] = f"clean_tok_{uuid.uuid4().hex[:8]}"
            job["lease_until"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            job["version"] = job.get("version", 1) + 1

            # Phase 3: Writes ONLY
            tx.set(ref, job)
            if art_id:
                if art_snap.exists and art_snap.to_dict().get("visibility") != "published":
                    tx.update(art_ref, {"visibility": "cleaning"})
                if file_snap.exists and file_snap.to_dict().get("publication_status") != "published":
                    tx.update(file_ref, {"publication_status": "cleaning"})
            return job

        try:
            tx = self.client.transaction()
            return _claim_tx(tx)
        except Exception as e:
            logger.error(f"Firestore claim_artifact_cleanup_job error: {e}")
            raise StorageUnavailableError(f"Firestore claim_artifact_cleanup_job failed: {e}") from e

    def record_cleanup_failure(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        max_retries: int = 5,
        lease_token: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("artifact_cleanups").document(job_id)

        @firestore.transactional
        def _fail_tx(tx):
            # Phase 1: Reads ONLY
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False
            job = snap.to_dict()

            art_id = job.get("artifact_id")
            art_ref = None
            art_snap = None
            file_ref = None
            file_snap = None
            if art_id:
                art_ref = self.client.collection("artifacts").document(art_id)
                art_snap = art_ref.get(transaction=tx)
                file_ref = self.client.collection("files").document(art_id)
                file_snap = file_ref.get(transaction=tx)

            # Phase 2: Validation and in-memory preparation
            if not worker_id or not job.get("lease_owner") or job.get("lease_owner") != worker_id:
                return False
            if not lease_token or not job.get("lease_token") or job.get("lease_token") != lease_token:
                return False
            if expected_version is not None and job.get("version") != expected_version:
                return False
            lease_until_str = job.get("lease_until")
            if not lease_until_str:
                return False
            try:
                lease_until = datetime.fromisoformat(lease_until_str)
                if lease_until <= now:
                    return False
            except Exception:
                return False

            retries = job.get("retries", 0) + 1
            job["retries"] = retries
            job["status"] = "pending" if retries < max_retries else "failed"
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_until"] = None
            job["last_error"] = error
            job["version"] = job.get("version", 1) + 1
            if retries < max_retries:
                job["next_retry_at"] = (now + timedelta(seconds=min(300, 2 ** retries))).isoformat()

            # Phase 3: Writes ONLY
            tx.set(ref, job)
            if art_id:
                if art_snap and art_snap.exists and art_snap.to_dict().get("visibility") == "cleaning":
                    tx.update(art_ref, {"visibility": "pending_approval"})
                if file_snap and file_snap.exists and file_snap.to_dict().get("publication_status") == "cleaning":
                    tx.update(file_ref, {"publication_status": "pending_approval"})
            return True

        try:
            tx = self.client.transaction()
            return _fail_tx(tx)
        except Exception as e:
            logger.error(f"Firestore record_cleanup_failure error: {e}")
            raise StorageUnavailableError(f"Firestore record_cleanup_failure failed: {e}") from e

    def complete_artifact_cleanup(
        self,
        job_id: str,
        worker_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("artifact_cleanups").document(job_id)

        @firestore.transactional
        def _comp_tx(tx):
            # Phase 1: Reads ONLY
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False
            job = snap.to_dict()

            art_id = job.get("artifact_id")
            art_ref = None
            art_snap = None
            file_ref = None
            file_snap = None
            if art_id:
                art_ref = self.client.collection("artifacts").document(art_id)
                art_snap = art_ref.get(transaction=tx)
                file_ref = self.client.collection("files").document(art_id)
                file_snap = file_ref.get(transaction=tx)

            # Phase 2: Validation and in-memory preparation
            if worker_id is not None:
                if not job.get("lease_owner") or job.get("lease_owner") != worker_id:
                    return False
                if not lease_token or not job.get("lease_token") or job.get("lease_token") != lease_token:
                    return False
                if expected_version is not None and job.get("version") != expected_version:
                    return False
                lease_until_str = job.get("lease_until")
                if not lease_until_str:
                    return False
                try:
                    lease_until = datetime.fromisoformat(lease_until_str)
                    if lease_until <= now:
                        return False
                except Exception:
                    return False

            job["status"] = "completed"
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_until"] = None
            job["version"] = job.get("version", 1) + 1
            job["updated_at"] = now.isoformat()

            # Phase 3: Writes ONLY
            tx.set(ref, job)
            if art_id:
                if art_snap and art_snap.exists and art_snap.to_dict().get("visibility") == "cleaning":
                    tx.update(art_ref, {"visibility": "purged"})
                if file_snap and file_snap.exists and file_snap.to_dict().get("publication_status") == "cleaning":
                    tx.update(file_ref, {"publication_status": "failed"})
            return True

        try:
            tx = self.client.transaction()
            return _comp_tx(tx)
        except Exception as e:
            logger.error(f"Firestore complete_artifact_cleanup error: {e}")
            raise StorageUnavailableError(f"Firestore complete_artifact_cleanup failed: {e}") from e

    def cancel_artifact_cleanup_job(self, job_id: str, reason: str = "ALREADY_PUBLISHED") -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        ref = self.client.collection("artifact_cleanups").document(job_id)

        @firestore.transactional
        def _cancel_tx(tx):
            # Phase 1: Reads ONLY
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False
            job = snap.to_dict()

            art_id = job.get("artifact_id")
            art_ref = None
            art_snap = None
            file_ref = None
            file_snap = None
            if art_id:
                art_ref = self.client.collection("artifacts").document(art_id)
                art_snap = art_ref.get(transaction=tx)
                file_ref = self.client.collection("files").document(art_id)
                file_snap = file_ref.get(transaction=tx)

            # Phase 2: In-memory preparation
            job["status"] = "cancelled"
            job["reason"] = reason
            job["lease_owner"] = None
            job["lease_token"] = None
            job["lease_until"] = None
            job["version"] = job.get("version", 1) + 1
            job["updated_at"] = now.isoformat()

            # Phase 3: Writes ONLY
            tx.set(ref, job)
            if art_id:
                if art_snap and art_snap.exists and art_snap.to_dict().get("visibility") == "cleaning":
                    tx.update(art_ref, {"visibility": "pending_approval"})
                if file_snap and file_snap.exists and file_snap.to_dict().get("publication_status") == "cleaning":
                    tx.update(file_ref, {"publication_status": "pending_approval"})
            return True

        try:
            tx = self.client.transaction()
            return _cancel_tx(tx)
        except Exception as e:
            logger.error(f"Firestore cancel_artifact_cleanup_job error: {e}")
            raise StorageUnavailableError(f"Firestore cancel_artifact_cleanup_job failed: {e}") from e

    def save_diagnosis(self, diagnosis: DiagnosisRecord) -> None:
        super().save_diagnosis(diagnosis)
        try:
            self.client.collection("diagnoses").document(diagnosis.diagnosis_id).set(diagnosis.model_dump(mode="json"))
        except Exception as e:
            logger.warning("Firestore save_diagnosis failed: %s", e)

    def get_diagnosis(self, space_id: str, run_id: str) -> Optional[DiagnosisRecord]:
        try:
            run_ref = self.client.collection("runs").document(run_id)
            snap = run_ref.get()
            if snap.exists:
                r_data = snap.to_dict()
                if r_data.get("space_id") == space_id and r_data.get("latest_diagnosis_id"):
                    diag_snap = self.client.collection("diagnoses").document(r_data["latest_diagnosis_id"]).get()
                    if diag_snap.exists:
                        return DiagnosisRecord.model_validate(diag_snap.to_dict())
        except Exception as e:
            logger.warning("Firestore get_diagnosis error: %s", e)
        return super().get_diagnosis(space_id, run_id)

    def atomic_commit_diagnosis(
        self,
        space_id: str,
        run_id: str,
        diagnosis: DiagnosisRecord,
        expected_generation: int,
        expected_trace_id: Optional[str] = None,
        expected_diagnosis_revision: Optional[int] = None,
    ) -> bool:
        from google.cloud import firestore

        run_ref = self.client.collection("runs").document(run_id)
        diag_ref = self.client.collection("diagnoses").document(diagnosis.diagnosis_id)

        @firestore.transactional
        def _commit_tx(tx):
            snap = run_ref.get(transaction=tx)
            if not snap.exists:
                return False
            r_data = snap.to_dict()
            if r_data.get("space_id") != space_id:
                return False
            curr_gen = r_data.get("telemetry_generation", 1) or 1
            if curr_gen != expected_generation:
                return False
            if expected_trace_id is not None:
                curr_trace_id = r_data.get("trace_id")
                if curr_trace_id != expected_trace_id or diagnosis.trace_id != expected_trace_id:
                    return False
            curr_rev = r_data.get("diagnosis_revision", 0) or 0
            if expected_diagnosis_revision is not None and curr_rev != expected_diagnosis_revision:
                return False

            now = datetime.now(UTC)
            run = Run.model_validate(r_data)
            run.diagnosis_revision = curr_rev + 1
            run.latest_diagnosis_id = diagnosis.diagnosis_id
            run.latest_diagnosis_status = diagnosis.diagnostic_status
            if not getattr(run, "agent_diagnosis", None) and diagnosis.error_summary:
                run.agent_diagnosis = (
                    f"🚨 **Grafana MCP Telemetry Diagnosis (Trace `{run.trace_id}`)**\n\n"
                    f"• **Status**: `{diagnosis.diagnostic_status}`\n"
                    f"• **Faulting Span**: `{diagnosis.faulting_span.name if diagnosis.faulting_span else 'N/A'}`\n"
                    f"• **Root Cause**: {diagnosis.error_summary}\n"
                )
            run.updated_at = now

            tx.set(diag_ref, diagnosis.model_dump(mode="json"))
            tx.set(run_ref, run.model_dump(mode="json"))
            return True

        try:
            tx = self.client.transaction()
            res = _commit_tx(tx)
            if res:
                super().atomic_commit_diagnosis(
                    space_id=space_id,
                    run_id=run_id,
                    diagnosis=diagnosis,
                    expected_generation=expected_generation,
                    expected_trace_id=expected_trace_id,
                    expected_diagnosis_revision=expected_diagnosis_revision,
                )
            return bool(res)
        except StorageConflictError:
            raise
        except Exception as e:
            logger.warning("Firestore atomic_commit_diagnosis error: %s", e)
            raise StorageUnavailableError(f"Firestore atomic_commit_diagnosis failed: {e}") from e

    def atomic_commit_diagnosis_with_claim(
        self,
        space_id: str,
        run_id: str,
        diagnosis: DiagnosisRecord,
        expected_generation: int,
        lease_owner: str,
        lease_token: str,
        expected_trace_id: Optional[str] = None,
        expected_diagnosis_revision: Optional[int] = None,
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        run_ref = self.client.collection("runs").document(run_id)
        diag_ref = self.client.collection("diagnoses").document(diagnosis.diagnosis_id)
        claim_ref = self.client.collection("diagnosis_claims").document(f"{space_id}_{run_id}")

        @firestore.transactional
        def _commit_tx(tx):
            snap = run_ref.get(transaction=tx)
            if not snap.exists:
                return False
            r_data = snap.to_dict()
            if r_data.get("space_id") != space_id:
                return False
            curr_gen = r_data.get("telemetry_generation", 1) or 1
            if curr_gen != expected_generation:
                return False
            if expected_trace_id is not None:
                curr_trace_id = r_data.get("trace_id")
                if curr_trace_id != expected_trace_id or diagnosis.trace_id != expected_trace_id:
                    return False
            curr_rev = r_data.get("diagnosis_revision", 0) or 0
            if expected_diagnosis_revision is not None and curr_rev != expected_diagnosis_revision:
                return False

            # Read and verify claim
            claim_snap = claim_ref.get(transaction=tx)
            if not claim_snap.exists:
                return False
            c_data = claim_snap.to_dict()
            claim = DiagnosisClaimRecord.model_validate(c_data)
            if claim.status != DiagnosisClaimStatus.RUNNING.value:
                return False
            if claim.lease_owner != lease_owner or claim.lease_token != lease_token:
                return False
            lease_until = _parse_lease_timestamp_fail_closed(c_data.get("lease_until"))
            if not lease_until or lease_until <= now:
                return False

            run = Run.model_validate(r_data)
            run.diagnosis_revision = curr_rev + 1
            run.latest_diagnosis_id = diagnosis.diagnosis_id
            run.latest_diagnosis_status = diagnosis.diagnostic_status
            if not getattr(run, "agent_diagnosis", None) and diagnosis.error_summary:
                run.agent_diagnosis = (
                    f"🚨 **Grafana MCP Telemetry Diagnosis (Trace `{run.trace_id}`)**\n\n"
                    f"• **Status**: `{diagnosis.diagnostic_status}`\n"
                    f"• **Faulting Span**: `{diagnosis.faulting_span.name if diagnosis.faulting_span else 'N/A'}`\n"
                    f"• **Root Cause**: {diagnosis.error_summary}\n"
                )
            run.updated_at = now

            tx.set(diag_ref, diagnosis.model_dump(mode="json"))
            tx.set(run_ref, run.model_dump(mode="json"))
            tx.update(claim_ref, {
                "status": DiagnosisClaimStatus.COMPLETED.value,
                "diagnosis_id": diagnosis.diagnosis_id,
                "lease_until": None,
                "updated_at": now.isoformat(),
            })
            return True

        try:
            tx = self.client.transaction()
            res = _commit_tx(tx)
            return bool(res)
        except StorageConflictError:
            raise
        except Exception as e:
            logger.warning("Firestore atomic_commit_diagnosis_with_claim error: %s", e)
            raise StorageUnavailableError(f"Firestore atomic_commit_diagnosis_with_claim failed: {e}") from e

    def fail_run_diagnosis_claim(
        self,
        space_id: str,
        run_id: str,
        lease_owner: str,
        lease_token: str,
    ) -> bool:
        return self.complete_run_diagnosis_claim(
            space_id=space_id,
            run_id=run_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            status=DiagnosisClaimStatus.FAILED.value,
        )

    def claim_run_diagnosis(
        self,
        space_id: str,
        run_id: str,
        expected_generation: int,
        lease_owner: str,
        lease_duration_sec: float = 15.0,
        expected_trace_id: Optional[str] = None,
        schema_version: int = 1,
    ) -> Tuple[bool, Optional[DiagnosisClaimRecord], Optional[DiagnosisRecord]]:
        from google.cloud import firestore

        now = datetime.now(UTC)
        claim_ref = self.client.collection("diagnosis_claims").document(f"{space_id}_{run_id}")
        run_ref = self.client.collection("runs").document(run_id)

        @firestore.transactional
        def _claim_tx(tx):
            run_snap = run_ref.get(transaction=tx)
            if not run_snap.exists:
                raise StorageConflictError("RUN_NOT_FOUND")
            r_data = run_snap.to_dict()
            if r_data.get("space_id") != space_id:
                raise StorageConflictError("SPACE_MISMATCH")
            curr_gen = r_data.get("telemetry_generation", 1) or 1
            if curr_gen != expected_generation:
                raise StorageConflictError("GENERATION_STALE")
            if expected_trace_id is not None and r_data.get("trace_id") != expected_trace_id:
                raise StorageConflictError("TRACE_ID_MISMATCH")

            claim_snap = claim_ref.get(transaction=tx)
            is_same_generation = False
            if claim_snap.exists:
                c_data = claim_snap.to_dict()
                claim = DiagnosisClaimRecord.model_validate(c_data)
                is_same_generation = (
                    claim.telemetry_generation == expected_generation
                    and (expected_trace_id is None or claim.trace_id is None or claim.trace_id == expected_trace_id)
                    and claim.schema_version == schema_version
                )
                if is_same_generation:
                    if claim.status == DiagnosisClaimStatus.COMPLETED.value and claim.diagnosis_id:
                        diag_snap = self.client.collection("diagnoses").document(claim.diagnosis_id).get(transaction=tx)
                        diag = DiagnosisRecord.model_validate(diag_snap.to_dict()) if diag_snap.exists else None
                        if (
                            diag
                            and diag.telemetry_generation == expected_generation
                            and (expected_trace_id is None or diag.trace_id == expected_trace_id)
                        ):
                            return False, claim, diag
                    lease_until = _parse_lease_timestamp_fail_closed(c_data.get("lease_until"))
                    if claim.status == DiagnosisClaimStatus.RUNNING.value and lease_until and lease_until > now:
                        return False, claim, None

            fresh_token = secrets.token_hex(16)
            new_claim = DiagnosisClaimRecord(
                space_id=space_id,
                run_id=run_id,
                trace_id=expected_trace_id or r_data.get("trace_id"),
                telemetry_generation=expected_generation,
                schema_version=schema_version,
                status=DiagnosisClaimStatus.RUNNING.value,
                lease_owner=lease_owner,
                lease_token=fresh_token,
                lease_until=now + timedelta(seconds=lease_duration_sec),
                attempts=(claim_snap.to_dict().get("attempts", 0) + 1) if (claim_snap.exists and is_same_generation) else 1,
                created_at=now,
                updated_at=now,
            )
            tx.set(claim_ref, new_claim.model_dump(mode="json"))
            return True, new_claim, None

        try:
            tx = self.client.transaction()
            res = _claim_tx(tx)
            return res
        except StorageConflictError:
            raise
        except Exception as e:
            logger.warning("Firestore claim_run_diagnosis error: %s", e)
            raise StorageUnavailableError(f"Firestore claim_run_diagnosis failed: {e}") from e

    def complete_run_diagnosis_claim(
        self,
        space_id: str,
        run_id: str,
        lease_owner: str,
        lease_token: str,
        diagnosis_id: Optional[str] = None,
        status: str = "completed",
    ) -> bool:
        from google.cloud import firestore

        now = datetime.now(UTC)
        claim_ref = self.client.collection("diagnosis_claims").document(f"{space_id}_{run_id}")

        @firestore.transactional
        def _complete_tx(tx):
            claim_snap = claim_ref.get(transaction=tx)
            if not claim_snap.exists:
                return False
            c_data = claim_snap.to_dict()
            claim = DiagnosisClaimRecord.model_validate(c_data)
            if claim.status != DiagnosisClaimStatus.RUNNING.value:
                return False
            if claim.lease_owner != lease_owner or claim.lease_token != lease_token:
                return False
            lease_until = _parse_lease_timestamp_fail_closed(c_data.get("lease_until"))
            if not lease_until or lease_until <= now:
                return False

            tx.update(claim_ref, {
                "status": status,
                "diagnosis_id": diagnosis_id,
                "lease_until": None,
                "updated_at": now.isoformat(),
            })
            return True

        try:
            tx = self.client.transaction()
            res = bool(_complete_tx(tx))
            return res
        except Exception as e:
            logger.warning("Firestore complete_run_diagnosis_claim error: %s", e)
            return False

    def check_and_record_dual_rate_limit(
        self,
        space_id: str,
        user_id: str,
        user_limit: int = 10,
        space_limit: int = 30,
        window_seconds: float = 60.0,
    ) -> Tuple[bool, int]:
        from google.cloud import firestore

        now_ts = time.time()
        now_dt = datetime.now(UTC)
        cutoff = now_ts - window_seconds
        u_id = f"user_{hashlib.sha256(f'{space_id}:{user_id}'.encode()).hexdigest()[:24]}"
        s_id = f"space_{hashlib.sha256(space_id.encode()).hexdigest()[:24]}"
        u_ref = self.client.collection("rate_limits").document(u_id)
        s_ref = self.client.collection("rate_limits").document(s_id)

        @firestore.transactional
        def _rate_tx(tx):
            u_snap = u_ref.get(transaction=tx)
            s_snap = s_ref.get(transaction=tx)

            u_ts = [t for t in (u_snap.to_dict().get("timestamps", []) if u_snap.exists else []) if t > cutoff]
            s_ts = [t for t in (s_snap.to_dict().get("timestamps", []) if s_snap.exists else []) if t > cutoff]

            if len(u_ts) >= user_limit:
                oldest = min(u_ts)
                return False, max(1, int(oldest + window_seconds - now_ts + 0.999))
            if len(s_ts) >= space_limit:
                oldest = min(s_ts)
                return False, max(1, int(oldest + window_seconds - now_ts + 0.999))

            u_ts.append(now_ts)
            s_ts.append(now_ts)
            ttl_time = now_dt + timedelta(minutes=5)
            tx.set(u_ref, {"timestamps": u_ts, "expires_at": ttl_time})
            tx.set(s_ref, {"timestamps": s_ts, "expires_at": ttl_time})
            return True, 0

        try:
            tx = self.client.transaction()
            res, retry_after = _rate_tx(tx)
            return res, retry_after
        except Exception as e:
            logger.warning("Firestore check_and_record_dual_rate_limit error: %s", e)
            raise StorageUnavailableError(f"Firestore rate limiting unavailable: {e}") from e

    def check_and_record_rate_limit(
        self,
        key: str,
        limit: int = 10,
        window_seconds: float = 60.0,
    ) -> Tuple[bool, int]:
        from google.cloud import firestore

        now_ts = time.time()
        now_dt = datetime.now(UTC)
        cutoff = now_ts - window_seconds
        doc_id = f"rl_{hashlib.sha256(key.encode()).hexdigest()[:32]}"
        ref = self.client.collection("rate_limits").document(doc_id)

        @firestore.transactional
        def _single_rate_tx(tx):
            snap = ref.get(transaction=tx)
            timestamps = [t for t in (snap.to_dict().get("timestamps", []) if snap.exists else []) if t > cutoff]

            if len(timestamps) >= limit:
                oldest = min(timestamps)
                return False, max(1, int(oldest + window_seconds - now_ts + 0.999))

            timestamps.append(now_ts)
            ttl_time = now_dt + timedelta(minutes=5)
            tx.set(ref, {"timestamps": timestamps, "expires_at": ttl_time})
            return True, 0

        try:
            tx = self.client.transaction()
            res, retry_after = _single_rate_tx(tx)
            return res, retry_after
        except Exception as e:
            logger.warning("Firestore check_and_record_rate_limit error: %s", e)
            raise StorageUnavailableError(f"Firestore rate limiting unavailable: {e}") from e

    def _claim_cleanup_lease(self, space_id: str, worker_id: str, lease_duration_seconds: int = 300) -> Tuple[bool, dict, str]:
        """
        Transactionally claims or resumes the sandbox cleanup lease.
        Atomically:
        1. Checks spaces/{space_id} exists and is_sandbox.
        2. Sets spaces/{space_id}.cleanup_status = 'deleting'.
        3. Reads sandbox_cleanup_jobs/{space_id} and evaluates lease expiration / contention.
        4. Writes updated lease token, owner, lease_until, state_version, and increments attempts.
        Returns (claimed: bool, job_data: dict, reason: str).
        """
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_duration_seconds)
        new_token = str(uuid.uuid4())

        from google.cloud import firestore

        @firestore.transactional
        def _claim_tx(tx):
            space_ref = self.client.collection("spaces").document(space_id)
            space_snap = space_ref.get(transaction=tx)
            if not space_snap.exists:
                return False, {}, "NOT_FOUND"
            space_data = space_snap.to_dict() or {}
            if not space_data.get("is_sandbox"):
                raise CleanupAuthorizationError(
                    f"Space '{space_id}' is not an ephemeral sandbox space. Cascade deletion is strictly forbidden on standard spaces."
                )

            job_ref = self.client.collection("sandbox_cleanup_jobs").document(space_id)
            job_snap = job_ref.get(transaction=tx)
            job_data = job_snap.to_dict() if job_snap.exists else {}

            if job_data:
                phase = job_data.get("phase") or job_data.get("current_phase")
                if phase == SandboxCleanupPhase.COMPLETED.value:
                    if space_snap.exists:
                        # Space document was stranded despite completed flag. Force atomic finalization!
                        current_phase = SandboxCleanupPhase.FINALIZING.value
                        state_version = (job_data.get("state_version") or 0) + 1
                        attempts = (job_data.get("attempts") or 0) + 1
                        collection_index = 0
                        cursor_id = None
                    else:
                        return True, job_data, "ALREADY_COMPLETED"
                else:
                    stored_lease_until = job_data.get("lease_until") or job_data.get("lease_expires_at")
                    if isinstance(stored_lease_until, str):
                        try:
                            stored_lease_until = datetime.fromisoformat(stored_lease_until)
                        except Exception:
                            stored_lease_until = None
                    if stored_lease_until and stored_lease_until > now and job_data.get("lease_owner") != worker_id:
                        return False, job_data, "ACTIVE_LEASE_HELD"

                    stored_next_retry = job_data.get("next_retry_at")
                    if isinstance(stored_next_retry, str):
                        try:
                            stored_next_retry = datetime.fromisoformat(stored_next_retry)
                        except Exception:
                            stored_next_retry = None
                    if stored_next_retry and stored_next_retry > now:
                        return False, job_data, "RETRY_BACKOFF"

                    current_phase = phase or SandboxCleanupPhase.PENDING.value
                    state_version = (job_data.get("state_version") or 0) + 1
                    attempts = (job_data.get("attempts") or 0) + 1
                    collection_index = job_data.get("collection_index") or 0
                    cursor_id = job_data.get("cursor_document_id")
            else:
                current_phase = SandboxCleanupPhase.PENDING.value
                state_version = 1
                attempts = 1
                collection_index = 0
                cursor_id = None

            updated_job = {
                "space_id": space_id,
                "phase": current_phase,
                "current_phase": current_phase,
                "collection_index": collection_index,
                "cursor_document_id": cursor_id,
                "lease_owner": worker_id,
                "lease_token": new_token,
                "lease_until": lease_until,
                "lease_expires_at": lease_until,
                "state_version": state_version,
                "attempts": attempts,
                "next_retry_at": None,
                "safe_error_code": None,
                "created_at": job_data.get("created_at") or now,
                "updated_at": now,
            }
            tx.set(job_ref, updated_job)
            tx.update(space_ref, {"cleanup_status": "deleting", "updated_at": now})
            return True, updated_job, "CLAIMED"

        try:
            tx = self.client.transaction()
            return _claim_tx(tx)
        except CleanupAuthorizationError:
            raise
        except Exception as e:
            logger.error("Error in _claim_cleanup_lease for space %s: %s", space_id, e)
            raise StorageUnavailableError(f"Firestore unavailable for cleanup lease claim: {e}") from e

    def _fenced_update_cleanup_job(self, space_id: str, worker_id: str, lease_token: str, expected_version: int, updates: dict) -> int:
        """
        CAS-fenced update of sandbox cleanup job in Firestore.
        Ensures worker still holds valid lease_token, matches expected state_version,
        matches lease_owner, and lease_until has not expired.
        Increments state_version on success.
        """
        from google.cloud import firestore

        @firestore.transactional
        def _fenced_tx(tx):
            job_ref = self.client.collection("sandbox_cleanup_jobs").document(space_id)
            snap = job_ref.get(transaction=tx)
            if not snap.exists:
                raise StorageConflictError("Cleanup job vanished during execution")
            data = snap.to_dict() or {}

            # Strict owner validation
            if data.get("lease_owner") != worker_id:
                raise StorageConflictError(f"Cleanup job CAS fencing violation: worker '{worker_id}' is not lease owner '{data.get('lease_owner')}'")

            # Strict token & version validation
            if data.get("lease_token") != lease_token or data.get("state_version") != expected_version:
                raise StorageConflictError("Cleanup job CAS fencing violation: lease preempted or version mismatch")

            # Strict unexpired lease validation
            now = datetime.now(UTC)
            stored_lease_until = data.get("lease_until") or data.get("lease_expires_at")
            if isinstance(stored_lease_until, str):
                try:
                    stored_lease_until = datetime.fromisoformat(stored_lease_until)
                except Exception:
                    stored_lease_until = None
            if stored_lease_until is None:
                raise StorageConflictError("Cleanup job CAS fencing violation: lease_until missing")
            if stored_lease_until.tzinfo is None:
                stored_lease_until = stored_lease_until.replace(tzinfo=timezone.utc)
            if stored_lease_until <= now:
                raise StorageConflictError(f"Cleanup job CAS fencing violation: lease expired at {stored_lease_until.isoformat()} (now {now.isoformat()})")

            new_version = expected_version + 1
            mod_updates = dict(updates)
            mod_updates["state_version"] = new_version
            mod_updates["updated_at"] = now
            if "phase" in mod_updates:
                mod_updates["current_phase"] = mod_updates["phase"]
            if "lease_until" in mod_updates:
                mod_updates["lease_expires_at"] = mod_updates["lease_until"]
            tx.update(job_ref, mod_updates)
            return new_version

        try:
            tx = self.client.transaction()
            return _fenced_tx(tx)
        except StorageConflictError:
            raise
        except Exception as e:
            logger.error("Error in _fenced_update_cleanup_job for space %s: %s", space_id, e)
            raise StorageUnavailableError(f"Firestore unavailable during fenced cleanup update: {e}") from e

    def _record_cleanup_failure(self, space_id: str, worker_id: str, lease_token: str, expected_version: int, safe_code: str, attempts: int):
        """Records a retryable failure in the cleanup job with exponential backoff and releases lease."""
        backoff_sec = min(300, 5 * (2 ** min(attempts, 6)))
        next_retry = datetime.now(UTC) + timedelta(seconds=backoff_sec)
        try:
            self._fenced_update_cleanup_job(
                space_id=space_id,
                worker_id=worker_id,
                lease_token=lease_token,
                expected_version=expected_version,
                updates={
                    "safe_error_code": safe_code,
                    "next_retry_at": next_retry,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_until": None,
                },
            )
        except Exception as e:
            logger.warning("Failed to record cleanup failure record for space %s: %s", space_id, e)

    def _validate_blob_storage_path(
        self, space_id: str, storage_path: str, is_gcs: bool, data_dir: Optional[str] = None
    ) -> str:
        """
        Validates that a blob storage path is strictly scoped to the authorized space
        and does not perform directory traversal, absolute breakouts, or cross-space access.
        Returns the resolved path string for local disk, or verified relative path for GCS.
        Raises ValueError if the path violates security constraints.
        """
        if not storage_path or not isinstance(storage_path, str):
            raise ValueError(f"Invalid empty or non-string storage path: {storage_path!r}")

        clean_p = storage_path.replace("\\", "/")
        norm_parts = [p for p in clean_p.split("/") if p]
        if ".." in norm_parts or "." in norm_parts:
            raise ValueError(f"Directory traversal detected in storage path: '{storage_path}'")

        if (
            os.path.isabs(storage_path)
            or clean_p.startswith("/")
            or clean_p.startswith("\\")
            or (len(storage_path) > 1 and storage_path[1] == ":")
        ):
            raise ValueError(f"Absolute or rooted storage path disallowed: '{storage_path}'")

        valid_prefixes = (f"spaces/{space_id}/", f"{space_id}/")
        if not any(clean_p.startswith(vp) for vp in valid_prefixes):
            raise ValueError(
                f"Storage path '{storage_path}' violates space prefix containment (must start with 'spaces/{space_id}/' or '{space_id}/')"
            )

        if is_gcs:
            return clean_p

        base_dir = data_dir or "./data"
        resolved_data_dir = os.path.realpath(os.path.abspath(base_dir))
        resolved_full_p = os.path.realpath(os.path.abspath(os.path.join(resolved_data_dir, clean_p)))

        in_data_dir = False
        try:
            if os.path.commonpath([resolved_data_dir, resolved_full_p]) == resolved_data_dir:
                in_data_dir = True
        except ValueError:
            in_data_dir = False

        if in_data_dir:
            rel_inside = os.path.relpath(resolved_full_p, resolved_data_dir).replace("\\", "/")
            if not any(rel_inside.startswith(vp) for vp in valid_prefixes):
                raise ValueError(
                    f"Resolved path '{resolved_full_p}' escapes space '{space_id}' within data directory"
                )
            return resolved_full_p

        sandbox_artifact_root = os.path.realpath(
            os.path.abspath(os.path.join(tempfile.gettempdir(), "studiotower_artifacts", space_id))
        )
        try:
            if os.path.commonpath([sandbox_artifact_root, resolved_full_p]) == sandbox_artifact_root:
                return resolved_full_p
        except ValueError:
            pass

        raise ValueError(
            f"Resolved path '{resolved_full_p}' is outside authorized data and artifact directories"
        )

    def delete_sandbox_cascade(self, space_id: str, batch_size: int = 200, worker_id: Optional[str] = None) -> dict:
        """
        Durably deletes an ephemeral sandbox space and cascades deletion across all 17 child collections
        via a truly resumable, transactionally leased 5-phase state machine:
        1. pending -> marks space cleanup_status='deleting' and claims lease atomically.
        2. deleting_blobs -> deletes GCS physical blobs FIRST across files and artifacts.
           If any blob fails, halts BEFORE metadata removal!
        3. deleting_children -> cascades deletion across all 17 collections with persisted batch cursors.
        4. verifying_empty -> verifies zero records remain across child collections.
        5. finalizing -> atomically deletes space document AND updates job to completed in a single transaction.
        Fails closed with CleanupAuthorizationError if target space is not an authorized sandbox.
        Never falls back to MemoryStore on Firestore errors.
        """
        worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        claimed, job, reason = self._claim_cleanup_lease(space_id, worker_id)
        if not claimed:
            if reason == "NOT_FOUND":
                return {"deleted": False, "reason": "not_found", "space_id": space_id}
            if reason in ("ACTIVE_LEASE_HELD", "RETRY_BACKOFF"):
                return {"deleted": False, "reason": reason.lower(), "space_id": space_id}
            return {"deleted": False, "reason": reason, "space_id": space_id}

        if reason == "ALREADY_COMPLETED":
            return {"deleted": True, "space_id": space_id, "already_completed": True}

        lease_token = job["lease_token"]
        curr_ver = job["state_version"]
        phase = job["phase"]
        attempts = job["attempts"]

        # Phase 1: pending -> deleting_blobs
        if phase == SandboxCleanupPhase.PENDING.value or phase == "pending":
            curr_ver = self._fenced_update_cleanup_job(
                space_id, worker_id, lease_token, curr_ver, {"phase": SandboxCleanupPhase.DELETING_BLOBS.value}
            )
            phase = SandboxCleanupPhase.DELETING_BLOBS.value

        # Phase 2: deleting_blobs (Physical GCS/Disk blobs FIRST before metadata removal)
        if phase == SandboxCleanupPhase.DELETING_BLOBS.value or phase == "deleting_blobs":
            try:
                file_storage_paths = []
                for coll_name in ("files", "artifacts"):
                    try:
                        for fd in self.client.collection(coll_name).where("space_id", "==", space_id).stream():
                            sp = (fd.to_dict() or {}).get("storage_path")
                            if sp and sp not in file_storage_paths:
                                file_storage_paths.append(sp)
                    except Exception as fe:
                        logger.error("Failed to query %s collection for blobs in space %s: %s", coll_name, space_id, fe)
                        self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "BLOB_INVENTORY_QUERY_FAILED", attempts)
                        raise StorageUnavailableError(f"Failed to inventory blobs in {coll_name} for space {space_id}") from fe

                from app.core.config import settings
                is_gcs = settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs"
                data_dir = getattr(settings, "STUDIO_TOWER_DATA_DIR", None)

                # Scope-validate all storage paths before executing any destructive deletions
                validated_blob_targets = []
                for sp in file_storage_paths:
                    try:
                        target = self._validate_blob_storage_path(space_id, sp, is_gcs, data_dir)
                        validated_blob_targets.append((sp, target))
                    except ValueError as val_err:
                        logger.error("Destructive cleanup rejected invalid blob storage path %r for space %s: %s", sp, space_id, val_err)
                        self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "INVALID_BLOB_STORAGE_PATH", attempts)
                        raise StorageUnavailableError(f"INVALID_BLOB_STORAGE_PATH: Destructive cleanup aborted for space {space_id}: {val_err}") from val_err

                if validated_blob_targets:
                    if is_gcs:
                        from google.cloud import storage as gcs
                        gcs_client = gcs.Client(project=settings.STUDIO_TOWER_FIREBASE_PROJECT_ID or None)
                        bucket = gcs_client.bucket(settings.STUDIO_TOWER_GCS_BUCKET)
                        for sp, _ in validated_blob_targets:
                            blob = bucket.blob(sp)
                            if blob.exists():
                                blob.delete()
                    else:
                        for _, full_p in validated_blob_targets:
                            if os.path.exists(full_p):
                                os.remove(full_p)

                # Scope-validate artifact blobs temp dir containment
                temp_root = os.path.realpath(os.path.abspath(os.path.join(tempfile.gettempdir(), "studiotower_artifacts")))
                temp_artifact_dir = os.path.realpath(os.path.abspath(os.path.join(temp_root, space_id)))
                try:
                    if os.path.commonpath([temp_root, temp_artifact_dir]) != temp_root or temp_artifact_dir == temp_root:
                        raise ValueError(f"Invalid temp artifact dir for space {space_id}")
                except ValueError as tv_err:
                    logger.error("Invalid temp artifact directory for space %s: %s", space_id, tv_err)
                    self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "INVALID_BLOB_STORAGE_PATH", attempts)
                    raise StorageUnavailableError(f"INVALID_BLOB_STORAGE_PATH: Temp artifact directory violation for space {space_id}") from tv_err

                if os.path.exists(temp_artifact_dir):
                    import shutil
                    shutil.rmtree(temp_artifact_dir, ignore_errors=False)

            except Exception as blob_err:
                if isinstance(blob_err, StorageUnavailableError):
                    raise
                logger.error("Physical blob deletion failed for space %s: %s", space_id, blob_err)
                self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "BLOB_DELETION_FAILED", attempts)
                raise StorageUnavailableError("Physical blob deletion failed; aborting cascade before metadata deletion") from blob_err

            # Heartbeat and advance to deleting_children
            curr_ver = self._fenced_update_cleanup_job(
                space_id,
                worker_id,
                lease_token,
                curr_ver,
                {
                    "phase": SandboxCleanupPhase.DELETING_CHILDREN.value,
                    "collection_index": 0,
                    "cursor_document_id": None,
                    "lease_until": datetime.now(UTC) + timedelta(minutes=5),
                },
            )
            phase = SandboxCleanupPhase.DELETING_CHILDREN.value

        # Phase 3: deleting_children across 17 collections
        collections_to_sweep = [
            "memberships",
            "invites",
            "runs",
            "action_executions",
            "messages",
            "files",
            "document_chunks",
            "pending_generation_cleanups",
            "artifacts",
            "artifact_cleanups",
            "diagnoses",
            "diagnosis_claims",
            "space_metric_rollups",
            "hourly_rollups",
            "activity_events",
            "activity_outbox",
            "chat_idempotency",
        ]

        if phase == SandboxCleanupPhase.DELETING_CHILDREN.value or phase == "deleting_children":
            coll_idx = job.get("collection_index") or 0
            cursor_id = job.get("cursor_document_id")

            while coll_idx < len(collections_to_sweep):
                coll_name = collections_to_sweep[coll_idx]
                try:
                    coll_ref = self.client.collection(coll_name)
                    q = coll_ref.where("space_id", "==", space_id).order_by("__name__")
                    if cursor_id:
                        cursor_ref = coll_ref.document(cursor_id)
                        try:
                            cursor_snap = cursor_ref.get()
                            if cursor_snap.exists:
                                q = q.start_after(cursor_snap)
                            else:
                                q = q.start_after({"__name__": cursor_ref})
                        except Exception:
                            q = q.start_after({"__name__": cursor_ref})
                    q = q.limit(batch_size)
                    docs = list(q.stream())

                    if docs:
                        batch = self.client.batch()
                        for d in docs:
                            batch.delete(d.reference)
                        batch.commit()
                        new_cursor = docs[-1].id
                        curr_ver = self._fenced_update_cleanup_job(
                            space_id,
                            worker_id,
                            lease_token,
                            curr_ver,
                            {
                                "cursor_document_id": new_cursor,
                                "lease_until": datetime.now(UTC) + timedelta(minutes=5),
                            },
                        )
                        cursor_id = new_cursor
                        if len(docs) == batch_size:
                            continue

                    coll_idx += 1
                    cursor_id = None
                    curr_ver = self._fenced_update_cleanup_job(
                        space_id,
                        worker_id,
                        lease_token,
                        curr_ver,
                        {
                            "collection_index": coll_idx,
                            "cursor_document_id": None,
                            "lease_until": datetime.now(UTC) + timedelta(minutes=5),
                        },
                    )
                except Exception as coll_err:
                    logger.error("Error deleting collection %s for space %s: %s", coll_name, space_id, coll_err)
                    self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "CHILD_DELETION_FAILED", attempts)
                    raise StorageUnavailableError(f"Failed deleting child collection {coll_name}") from coll_err

            # Delete deterministic cursor in telemetry_scan_cursors: scope_{space_id}
            try:
                self.client.collection("telemetry_scan_cursors").document(f"scope_{space_id}").delete()
            except Exception as cur_err:
                logger.error("Error deleting telemetry_scan_cursor for space %s: %s", space_id, cur_err)
                self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "CURSOR_DELETION_FAILED", attempts)
                raise StorageUnavailableError("Failed deleting telemetry scan cursor") from cur_err

            # Clear in-memory caches if present
            try:
                super().delete_sandbox_cascade(space_id)
            except Exception:
                pass

            curr_ver = self._fenced_update_cleanup_job(
                space_id,
                worker_id,
                lease_token,
                curr_ver,
                {
                    "phase": SandboxCleanupPhase.VERIFYING_EMPTY.value,
                    "lease_until": datetime.now(UTC) + timedelta(minutes=5),
                },
            )
            phase = SandboxCleanupPhase.VERIFYING_EMPTY.value

        # Phase 4: verifying_empty
        if phase == SandboxCleanupPhase.VERIFYING_EMPTY.value or phase == "verifying_empty":
            for idx, c_name in enumerate(collections_to_sweep):
                try:
                    remaining = list(self.client.collection(c_name).where("space_id", "==", space_id).limit(1).stream())
                    if remaining:
                        logger.error("Verification failed: %s still has records for space %s", c_name, space_id)
                        self._fenced_update_cleanup_job(
                            space_id,
                            worker_id,
                            lease_token,
                            curr_ver,
                            {
                                "phase": SandboxCleanupPhase.DELETING_CHILDREN.value,
                                "collection_index": idx,
                                "cursor_document_id": None,
                                "safe_error_code": "VERIFICATION_FAILED",
                            },
                        )
                        raise StorageUnavailableError(f"Verification failed: records remain in {c_name}")
                except Exception as ve:
                    if isinstance(ve, StorageUnavailableError):
                        raise
                    logger.error("Verification query error for %s: %s", c_name, ve)
                    self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "VERIFICATION_QUERY_FAILED", attempts)
                    raise StorageUnavailableError(f"Verification query failed for {c_name}") from ve

            curr_ver = self._fenced_update_cleanup_job(
                space_id,
                worker_id,
                lease_token,
                curr_ver,
                {
                    "phase": SandboxCleanupPhase.FINALIZING.value,
                    "lease_until": datetime.now(UTC) + timedelta(minutes=5),
                },
            )
            phase = SandboxCleanupPhase.FINALIZING.value

        # Phase 5: finalizing -> Atomically delete space document AND set job to COMPLETED in one transaction
        if phase == SandboxCleanupPhase.FINALIZING.value or phase == "finalizing":
            from google.cloud import firestore

            @firestore.transactional
            def _finalize_tx(tx):
                job_ref = self.client.collection("sandbox_cleanup_jobs").document(space_id)
                space_ref = self.client.collection("spaces").document(space_id)
                job_snap = job_ref.get(transaction=tx)
                if not job_snap.exists:
                    raise StorageConflictError("Cleanup job vanished during finalization")
                data = job_snap.to_dict() or {}

                # Validate owner
                if data.get("lease_owner") != worker_id:
                    raise StorageConflictError(f"Cleanup job CAS fencing violation during finalize: worker '{worker_id}' is not lease owner")

                # Validate token & version
                if data.get("lease_token") != lease_token or data.get("state_version") != curr_ver:
                    raise StorageConflictError("Cleanup job CAS fencing violation during finalize: lease preempted or version mismatch")

                # Validate lease unexpired
                now = datetime.now(UTC)
                stored_lease_until = data.get("lease_until") or data.get("lease_expires_at")
                if isinstance(stored_lease_until, str):
                    try:
                        stored_lease_until = datetime.fromisoformat(stored_lease_until)
                    except Exception:
                        stored_lease_until = None
                if stored_lease_until is None:
                    raise StorageConflictError("Cleanup job CAS fencing violation during finalize: lease_until missing")
                if stored_lease_until.tzinfo is None:
                    stored_lease_until = stored_lease_until.replace(tzinfo=timezone.utc)
                if stored_lease_until <= now:
                    raise StorageConflictError(f"Cleanup job CAS fencing violation during finalize: lease expired at {stored_lease_until}")

                # Delete space document
                tx.delete(space_ref)

                # Commit completed job state atomically in the exact same transaction
                tx.update(
                    job_ref,
                    {
                        "phase": SandboxCleanupPhase.COMPLETED.value,
                        "current_phase": SandboxCleanupPhase.COMPLETED.value,
                        "state_version": curr_ver + 1,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_until": None,
                        "lease_expires_at": None,
                        "completed_at": now,
                        "updated_at": now,
                        "safe_error_code": None,
                    },
                )

            try:
                tx = self.client.transaction()
                _finalize_tx(tx)
            except StorageConflictError:
                raise
            except Exception as sp_err:
                logger.error("Error in atomic _finalize_tx for space %s: %s", space_id, sp_err)
                self._record_cleanup_failure(space_id, worker_id, lease_token, curr_ver, "SPACE_DELETION_FAILED", attempts)
                raise StorageUnavailableError("Failed atomically finalizing space deletion") from sp_err
            phase = SandboxCleanupPhase.COMPLETED.value

        return {"deleted": True, "space_id": space_id}

    def sweep_expired_sandboxes(self, now_dt: datetime, limit: int = 50) -> int:
        """Queries Google Cloud Firestore for expired sandbox spaces and cascades durable cleanup."""
        try:
            query_expired = (
                self.client.collection("spaces")
                .where("is_sandbox", "==", True)
                .where("sandbox_expires_at", "<=", now_dt)
                .limit(limit)
            )
            docs_expired = list(query_expired.stream())

            query_stuck = (
                self.client.collection("spaces")
                .where("is_sandbox", "==", True)
                .where("cleanup_status", "==", "deleting")
                .limit(limit)
            )
            docs_stuck = list(query_stuck.stream())

            all_docs = {d.id: d for d in (docs_expired + docs_stuck)}
            swept = 0
            for sid, doc in all_docs.items():
                try:
                    res = self.delete_sandbox_cascade(sid)
                    if res.get("deleted"):
                        swept += 1
                except Exception as e:
                    logger.warning("Error sweeping Firestore sandbox %s: %s", sid, e)
            return swept
        except Exception as e:
            logger.error("Firestore sweep_expired_sandboxes query error: %s", e)
            raise StorageUnavailableError(f"Firestore sweep_expired_sandboxes unavailable: {e}") from e



def create_storage_backend(cfg=None) -> MemoryStore:
    from app.core.config import settings as default_settings
    target_cfg = cfg or default_settings
    if target_cfg.STUDIO_TOWER_STORE == "json":
        state_path = os.path.join(target_cfg.STUDIO_TOWER_DATA_DIR, "studiotower_state.json")
        return MemoryStore(persist_path=state_path)
    elif target_cfg.STUDIO_TOWER_STORE == "firestore":
        return FirestoreStore(project_id=target_cfg.STUDIO_TOWER_FIREBASE_PROJECT_ID)
    return MemoryStore()


# Global storage instance
store = create_storage_backend()


def check_storage_readiness() -> dict:
    from app.core.config import settings
    store_type = settings.STUDIO_TOWER_STORE
    try:
        if store_type == "firestore":
            if not isinstance(store, FirestoreStore) or not hasattr(store, "client") or store.client is None:
                return {"status": "degraded", "type": "firestore", "error": "Firestore client is uninitialized or invalid"}
            # Shallow connectivity verification with timeout
            store.client.collection("_healthz").document("ping").get(timeout=2.0)
            return {"status": "ok", "type": "firestore"}
        elif store_type == "json":
            state_path = os.path.join(settings.STUDIO_TOWER_DATA_DIR, "studiotower_state.json")
            dir_ok = os.path.exists(settings.STUDIO_TOWER_DATA_DIR) or os.access(os.path.dirname(state_path) or ".", os.W_OK)
            return {"status": "ok" if dir_ok else "degraded", "type": "json"}
        else:
            return {"status": "ok", "type": "memory"}
    except Exception:
        return {"status": "degraded", "type": store_type, "error": "Database storage backend unreachable"}

    def get_sandbox_cleanup_job(self, space_id: str) -> Optional[dict]:
        try:
            doc = self.client.collection("sandbox_cleanup_jobs").document(space_id).get()
            if doc.exists:
                return doc.to_dict()
            space_doc = self.client.collection("spaces").document(space_id).get()
            if space_doc.exists:
                return {"space_id": space_id, "phase": "pending", "status": "pending"}
            return {"space_id": space_id, "phase": "completed", "status": "completed"}
        except Exception as e:
            logger.error("Failed to query sandbox cleanup job %s: %s", space_id, e)
            raise StorageUnavailableError(f"Failed to query sandbox cleanup job: {e}") from e

    def verify_sandbox_empty(self, space_id: str) -> dict:
        try:
            space_doc = self.client.collection("spaces").document(space_id).get()
            if space_doc.exists:
                return {"empty": False, "reason": "space_exists", "space_id": space_id}

            collections_to_check = [
                "memberships", "invites", "runs", "action_executions", "messages",
                "files", "document_chunks", "pending_generation_cleanups",
                "artifacts", "artifact_cleanups", "diagnoses", "diagnosis_claims",
                "space_metric_rollups", "hourly_rollups", "activity_events",
                "activity_outbox", "chat_idempotency",
            ]
            remaining_counts = {}
            for c_name in collections_to_check:
                docs = list(self.client.collection(c_name).where("space_id", "==", space_id).limit(1).stream())
                if docs:
                    remaining_counts[c_name] = len(docs)

            if remaining_counts:
                return {"empty": False, "remaining_counts": remaining_counts, "space_id": space_id}
            return {"empty": True, "space_id": space_id}
        except Exception as e:
            logger.error("Failed to verify sandbox empty for %s: %s", space_id, e)
            raise StorageUnavailableError(f"Failed to verify sandbox empty: {e}") from e

