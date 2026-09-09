import hmac
import hashlib
import os
from typing import Dict, Optional, Tuple

from app.core.config import settings

_TEST_DEFAULT_SECRET = "studiotower-telemetry-tenant-test-key-32bytes-entropy-ok!"

_KEYS: Dict[str, bytes] = {}
_ACTIVE_KEY_ID = "k1"


def initialize_telemetry_tenant_keys(environment: Optional[str] = None):
    """
    Validate and initialize HMAC keys (P1 Requirement).
    In production/staging:
    - TELEMETRY_TENANT_KEY_PRIMARY must be set via Secret Manager.
    - Length check: Must be at least 32 bytes (generated from a cryptographically secure random source).
    - Hardcoded defaults or missing keys strictly halt application startup.
    """
    global _KEYS, _ACTIVE_KEY_ID
    env = (environment or os.environ.get("ENV") or os.environ.get("ENVIRONMENT") or settings.ENV or "development").lower()
    primary = os.environ.get("TELEMETRY_TENANT_KEY_PRIMARY")
    secondary = os.environ.get("TELEMETRY_TENANT_KEY_SECONDARY")
    active_kid = os.environ.get("TELEMETRY_TENANT_ACTIVE_KEY_ID", "k1")

    if env in ("production", "prod", "staging"):
        if not primary:
            raise RuntimeError(
                "Production startup failure: TELEMETRY_TENANT_KEY_PRIMARY must be set via Secret Manager."
            )
        if len(primary.encode("utf-8")) < 32:
            raise RuntimeError(
                f"Production startup failure: TELEMETRY_TENANT_KEY_PRIMARY length too short ({len(primary.encode('utf-8'))} bytes < 32 bytes). Secret Manager must inject at least 32 bytes from a secure random source."
            )
        _KEYS = {"k1": primary.encode("utf-8")}
        if secondary:
            if len(secondary.encode("utf-8")) < 32:
                raise RuntimeError(
                    "Production startup failure: TELEMETRY_TENANT_KEY_SECONDARY length too short (< 32 bytes)."
                )
            _KEYS["k2"] = secondary.encode("utf-8")
    else:
        _KEYS = {"k1": (primary or _TEST_DEFAULT_SECRET).encode("utf-8")}
        if secondary:
            _KEYS["k2"] = secondary.encode("utf-8")

    if active_kid not in _KEYS:
        raise RuntimeError(f"Startup failure: Active key ID '{active_kid}' not found in telemetry keyring.")
    _ACTIVE_KEY_ID = active_kid


def configure_telemetry_keys(keys: Dict[str, str], active_key_id: str):
    """
    Override keys programmatically for testing or dynamic key rotation.
    """
    global _KEYS, _ACTIVE_KEY_ID
    _KEYS = {kid: k.encode("utf-8") if isinstance(k, str) else k for kid, k in keys.items()}
    if active_key_id not in _KEYS:
        raise ValueError(f"Active key id '{active_key_id}' not found in provided keys")
    _ACTIVE_KEY_ID = active_key_id


def compute_tenant_hash(space_id: str, key_id: Optional[str] = None) -> Tuple[str, str]:
    """
    Compute keyed HMAC-SHA256 hash of space_id to prevent raw tenant ID leakage into
    OTel backends while preventing rainbow table / dictionary guessing against predictable space IDs.

    Returns: (space_id_hash, key_id)
    """
    global _KEYS, _ACTIVE_KEY_ID
    if not _KEYS:
        env = (os.environ.get("ENV") or os.environ.get("ENVIRONMENT") or settings.ENV or "development").lower()
        if env in ("production", "prod", "staging"):
            raise RuntimeError("Production startup failure: Telemetry tenant keys not initialized via Secret Manager.")
        initialize_telemetry_tenant_keys(environment="development")

    kid = key_id or _ACTIVE_KEY_ID
    key_bytes = _KEYS.get(kid)
    if not key_bytes:
        kid = _ACTIVE_KEY_ID
        key_bytes = _KEYS[kid]

    h = hmac.new(key_bytes, space_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"th_{h[:24]}", kid


def verify_tenant_hash(space_id: str, space_id_hash: str, key_id: Optional[str] = None) -> bool:
    """
    Verify whether space_id matches the provided tenant hash against the specified key or all active keys.
    """
    candidate_keys = [key_id] if key_id and key_id in _KEYS else list(_KEYS.keys())
    for kid in candidate_keys:
        expected_hash, _ = compute_tenant_hash(space_id, key_id=kid)
        if hmac.compare_digest(expected_hash, space_id_hash):
            return True
    return False
