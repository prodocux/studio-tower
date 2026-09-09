import re
from typing import Any, Dict
from app.models.telemetry import SPAN_ALLOWED_ATTRIBUTES, TelemetryErrorCode

# Prohibited patterns regex
_PROHIBITED_PATTERNS = [
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"ey[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]+"),  # JWT
    re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),  # Email
    re.compile(r"(?:api_key|token|secret|password|key)\s*[:=]\s*[^\s]+", re.IGNORECASE),
    re.compile(r"(?:gs://|/[a-zA-Z0-9_\-]+/(?:files|blobs|runs)/|[A-Za-z]:\\[^\n]+)"),  # Storage/local paths
]

# Standard error code names
_VALID_ERROR_CODES = {c.value for c in TelemetryErrorCode}


def sanitize_span_attributes(raw_attrs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strict allowlist filtering and sensitive content scrubbing for span attributes.
    Enforces E0 Contract 4.
    """
    clean: Dict[str, Any] = {}
    for key, value in raw_attrs.items():
        if key not in SPAN_ALLOWED_ATTRIBUTES:
            continue

        if isinstance(value, str):
            # Check prohibited patterns
            sanitized_str = value
            for pat in _PROHIBITED_PATTERNS:
                sanitized_str = pat.sub("[REDACTED]", sanitized_str)

            # Cap length to prevent document text leakage
            if len(sanitized_str) > 256:
                sanitized_str = sanitized_str[:256] + "...[TRUNCATED]"

            # Standardize error codes
            if key == "error_code":
                if sanitized_str not in _VALID_ERROR_CODES:
                    sanitized_str = TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value

            clean[key] = sanitized_str
        elif isinstance(value, (int, float, bool)):
            clean[key] = value
        elif value is None:
            clean[key] = None

    return clean


def assert_attributes_scrubbed(attrs: Dict[str, Any]) -> bool:
    """
    Check whether attributes conform 100% to allowlist and contain no un-redacted sensitive patterns.
    """
    for k, v in attrs.items():
        if k not in SPAN_ALLOWED_ATTRIBUTES:
            return False
        if isinstance(v, str):
            for pat in _PROHIBITED_PATTERNS:
                if pat.search(v):
                    return False
    return True
