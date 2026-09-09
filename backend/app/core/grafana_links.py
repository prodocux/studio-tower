"""
Server-templated secure Grafana deep link generator (Slice E Phase E4).

Security Contracts:
1. HTTPS protocol only. Any non-HTTPS scheme is rejected and returns None.
2. Zero default public domain trust. Deep-links strictly require explicit deployer-configured
   GRAFANA_ALLOWED_HOSTS.
3. Standard urlsplit parsing:
   - parsed.hostname used directly (rejects netloc split hacks).
   - Rejects URLs containing userinfo (username/password / '@' spoofing).
   - Rejects non-443 custom ports.
   - Rejects base URLs with queries or fragments.
   - Rejects trailing dots, control characters, or invalid hostname formats.
   - Exact hostname matching or strict configured subdomain matching against ALLOWED_HOSTS.
4. Structured URL construction via urlunsplit, urlencode, and quote(..., safe="").
5. Strict identifier validation:
   - dashboard_id regex: ^[A-Za-z0-9_-]{1,128}$ (rejects directory traversal ../).
   - trace_id, run_id, space_id regex: ^[A-Za-z0-9_.-]{1,128}$.
6. Authoritative server-generated time windows:
   - from and to are calculated from server-side datetime timestamps with buffer padding.
   - from_ms < to_ms and (to_ms - from_ms) <= MAX_GRAFANA_WINDOW_MS (24h).
   - Missing timestamps fail-close to None (no unbounded queries).
7. Pure fail-closed behavior on unconfigured or invalid state (returns None).
"""

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Optional, Tuple
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from app.core.config import settings

logger = logging.getLogger(__name__)

# Identifier regex patterns
RE_DASHBOARD_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
RE_SAFE_HOSTNAME = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")

# Window bounds
DEFAULT_BUFFER_MINUTES = 15
MAX_WINDOW_HOURS = 24
MAX_WINDOW_MS = MAX_WINDOW_HOURS * 3600 * 1000


def validate_grafana_base_url(
    base_url: str,
    allowed_hosts: list[str],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validates base_url against security rules.
    Returns (is_valid, sanitized_base_prefix, error_reason).
    """
    if not base_url or not base_url.strip():
        return False, None, "Base URL is empty"

    stripped = base_url.strip()
    try:
        parsed = urlsplit(stripped)
        scheme = parsed.scheme
        username = parsed.username
        password = parsed.password
        port = parsed.port
        hostname = parsed.hostname
        query = parsed.query
        fragment = parsed.fragment
        path = parsed.path
    except (ValueError, Exception) as e:
        return False, None, f"Failed to parse URL or invalid port/host: {e}"

    if scheme != "https":
        return False, None, f"Scheme is not https (got {scheme})"

    # Userinfo spoofing protection
    if username or password:
        return False, None, "URL contains user credentials (userinfo not allowed)"

    # Port restriction: only 443 or default
    if port and port != 443:
        return False, None, f"Non-standard HTTPS port not permitted: {port}"

    # No queries or fragments on base URL
    if query:
        return False, None, "Base URL contains query parameters"
    if fragment:
        return False, None, "Base URL contains fragment"

    if not hostname:
        return False, None, "URL hostname is empty"

    # Reject trailing dots, control characters
    if hostname.endswith(".") or ".." in hostname or not RE_SAFE_HOSTNAME.match(hostname):
        return False, None, f"Hostname has invalid format: {hostname}"

    # Zero default trust: allowed_hosts must be explicitly provided
    if not allowed_hosts:
        return False, None, "No allowed Grafana hosts configured (zero-trust default)"

    normalized_hosts = [h.strip().lower() for h in allowed_hosts if h.strip()]
    host_lower = hostname.lower()

    if host_lower not in normalized_hosts:
        return False, None, f"Hostname '{hostname}' not in allowed hosts list: {normalized_hosts}"

    # Clean path prefix (without trailing slash)
    path_prefix = path.rstrip("/")
    # Reconstruct clean base: https://hostname[:443][/path_prefix]
    netloc = hostname if not port or port == 443 else f"{hostname}:{port}"
    clean_base = urlunsplit(("https", netloc, path_prefix, "", ""))
    return True, clean_base, None


def build_grafana_dashboard_url(
    trace_id: str,
    run_id: str,
    space_id: Optional[str] = None,
    dashboard_id: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    base_url: Optional[str] = None,
    allowed_hosts: Optional[list[str]] = None,
    buffer_minutes: Optional[int] = None,
    max_window_hours: Optional[int] = None,
) -> Optional[str]:
    """
    Constructs an authoritative, structured, and bounded Grafana dashboard deep link.
    Returns None if Grafana is unconfigured, invalid, or violates security contracts.
    """
    # 1. Resolve configuration
    raw_base = (base_url or getattr(settings, "GRAFANA_BASE_URL", None) or os.environ.get("GRAFANA_BASE_URL", "")).strip()
    if not raw_base:
        return None

    # Resolve allowed hosts (strictly from settings or explicit argument, no public defaults)
    hosts = allowed_hosts
    if hosts is None:
        hosts = list(getattr(settings, "GRAFANA_ALLOWED_HOSTS", []))
        if not hosts and os.environ.get("GRAFANA_ALLOWED_HOSTS"):
            hosts = [h.strip() for h in os.environ.get("GRAFANA_ALLOWED_HOSTS", "").split(",") if h.strip()]

    # 2. Validate base URL
    is_valid, clean_base, err = validate_grafana_base_url(raw_base, hosts)
    if not is_valid or not clean_base:
        logger.warning("Grafana base URL validation failed: %s", err)
        return None

    # 3. Validate and encode identifiers
    dash_uid = (dashboard_id or getattr(settings, "GRAFANA_DASHBOARD_ID", None) or os.environ.get("GRAFANA_DASHBOARD_ID", "studiotower-monitor")).strip()
    if not RE_DASHBOARD_ID.match(dash_uid):
        logger.warning("Invalid dashboard UID: %r (rejected path traversal or illegal characters)", dash_uid)
        return None

    if not trace_id or not RE_IDENTIFIER.match(trace_id):
        logger.warning("Invalid trace_id: %r", trace_id)
        return None

    if not run_id or not RE_IDENTIFIER.match(run_id):
        logger.warning("Invalid run_id: %r", run_id)
        return None

    if space_id and not RE_IDENTIFIER.match(space_id):
        logger.warning("Invalid space_id: %r", space_id)
        return None

    # 4. Authoritative Server-Generated Time Range Bounds
    # Deep links MUST have bounded time queries derived from server timestamps.
    if start_time is None:
        logger.warning("Grafana deep-link generation rejected: start_time is required for time-bounding")
        return None

    buf_mins_val = buffer_minutes if buffer_minutes is not None else getattr(settings, "GRAFANA_TIME_BUFFER_MINUTES", DEFAULT_BUFFER_MINUTES)
    max_hrs_val = max_window_hours if max_window_hours is not None else getattr(settings, "GRAFANA_MAX_TIME_WINDOW_HOURS", MAX_WINDOW_HOURS)
    try:
        buf_mins = max(1, min(int(buf_mins_val), 60))
    except Exception:
        buf_mins = DEFAULT_BUFFER_MINUTES
    try:
        max_hrs = max(1, min(int(max_hrs_val), 24))
    except Exception:
        max_hrs = MAX_WINDOW_HOURS
    max_ms = max_hrs * 3600 * 1000

    start_dt = start_time.astimezone(UTC) if start_time.tzinfo else start_time.replace(tzinfo=UTC)
    end_dt = (end_time or start_time).astimezone(UTC) if (end_time and end_time.tzinfo) else ((end_time or start_time).replace(tzinfo=UTC))

    if end_dt < start_dt:
        end_dt = start_dt

    buffered_from = start_dt - timedelta(minutes=buf_mins)
    buffered_to = end_dt + timedelta(minutes=buf_mins)

    from_epoch_ms = int(buffered_from.timestamp() * 1000)
    to_epoch_ms = int(buffered_to.timestamp() * 1000)

    if from_epoch_ms >= to_epoch_ms:
        to_epoch_ms = from_epoch_ms + (buf_mins * 60 * 1000)

    # Window span cap
    if (to_epoch_ms - from_epoch_ms) > max_ms:
        to_epoch_ms = from_epoch_ms + max_ms

    # 5. Assemble Query Parameters using urllib.parse.urlencode
    query_params = [
        ("var-trace_id", trace_id),
        ("var-run_id", run_id),
    ]
    if space_id:
        query_params.append(("var-space_id", space_id))
    query_params.extend([
        ("from", str(from_epoch_ms)),
        ("to", str(to_epoch_ms)),
    ])
    encoded_query = urlencode(query_params)

    # 6. Structured URL Assembly
    parsed_base = urlsplit(clean_base)
    dash_path = f"{parsed_base.path.rstrip('/')}/d/{quote(dash_uid, safe='')}"
    return urlunsplit((
        parsed_base.scheme,
        parsed_base.netloc,
        dash_path,
        encoded_query,
        "",
    ))
