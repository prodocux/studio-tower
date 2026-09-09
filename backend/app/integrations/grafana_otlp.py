"""Resolve Grafana Cloud OTLP write settings and upsert the StudioTower dashboard.

Read path (glsa_) can list datasources and create dashboards.
Write path (glc_ / GRAFANA_OTLP_TOKEN) is required to push Space metrics.
Tokens never appear in returned payloads or log lines.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app.core.space_metrics import SPACE_EVENTS_PROMQL_NAME
from app.integrations.grafana_mcp import redact_mcp_text
from app.integrations.grafana_visual import GrafanaVisualError, _get_json, _headers, _validated_base

logger = logging.getLogger(__name__)

STUDIO_TOWER_DASHBOARD_UID = "studiotower-monitor"
STUDIO_TOWER_DASHBOARD_TITLE = "StudioTower Space Activity"
_PROM_HOST = re.compile(
    r"^https://(?:prometheus|mimir)-prod-\d+-prod-([a-z0-9-]+)\.grafana\.net(?:/|$)",
    re.IGNORECASE,
)

_ingest_status = "unavailable"
_ingest_detail = "Grafana Cloud OTLP export is not configured"


@dataclass(frozen=True)
class GrafanaOtlpExport:
    endpoint: str
    headers: dict[str, str]
    instance_id: str | None = None
    prometheus_uid: str | None = None


def get_ingest_status() -> tuple[str, str]:
    return _ingest_status, _ingest_detail


def set_ingest_status(status: str, detail: str) -> None:
    global _ingest_status, _ingest_detail
    _ingest_status = status
    _ingest_detail = detail


def otlp_gateway_from_metrics_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    match = _PROM_HOST.match(raw)
    if not match:
        return None
    return f"https://otlp-gateway-prod-{match.group(1)}.grafana.net/otlp"


def normalize_otlp_base(endpoint: str) -> str:
    base = (endpoint or "").rstrip("/")
    for suffix in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def signal_endpoint(base: str, signal: str) -> str:
    return f"{normalize_otlp_base(base)}/v1/{signal}"


def parse_otlp_headers(raw: str | None) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    headers: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        headers[key.strip()] = unquote(value.strip())
    return headers


def basic_otlp_headers(instance_id: str, token: str) -> dict[str, str]:
    blob = base64.b64encode(f"{instance_id}:{token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {blob}"}


def _glc_claims(token: str) -> dict[str, Any]:
    """Decode a glc_ token payload. Never log the token or claims blob."""
    if not token.startswith("glc_"):
        return {}
    try:
        raw = token[4:]
        padded = raw + ("=" * ((4 - len(raw) % 4) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _otlp_endpoint_from_write_token(token: str) -> str | None:
    """Derive the Cloud OTLP gateway from a glc_ token region claim. Never logs the token."""
    claims = _glc_claims(token)
    region = (claims.get("m") or {}).get("r") if isinstance(claims.get("m"), dict) else None
    if isinstance(region, str) and re.fullmatch(r"prod-[a-z0-9-]+", region):
        return f"https://otlp-gateway-{region}.grafana.net/otlp"
    return None


def _instance_id_from_write_token(token: str) -> str | None:
    """JWT `o` is the Grafana.com org id, not the OTLP/Prometheus instance id."""
    org_id = str(_glc_claims(token).get("o") or "").strip()
    return org_id if org_id.isdigit() else None


def _instance_ids_from_token_name(token: str) -> list[str]:
    """Cloud access policy token `n` sometimes embeds stack-<id> or a numeric instance."""
    name = str(_glc_claims(token).get("n") or "")
    found: list[str] = []
    for match in re.finditer(r"stack-(\d{4,})", name, re.IGNORECASE):
        found.append(match.group(1))
    for match in re.finditer(r"(?<![A-Za-z0-9])(\d{5,})(?![A-Za-z0-9])", name):
        found.append(match.group(1))
    return _unique_ids(found)


def _unique_ids(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value.isdigit() or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _numeric_ids_from_payload(payload: Any, preferred_keys: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in preferred_keys:
                value = str(node.get(key) or "").strip()
                if value.isdigit():
                    found.append(value)
            for key, value in node.items():
                key_l = str(key).lower()
                if key_l in {
                    "hminstancepromid",
                    "prometheusinstanceid",
                    "promid",
                    "otlpinstanceid",
                    "instanceid",
                    "instance_id",
                    "hminstanceid",
                }:
                    text = str(value or "").strip()
                    if text.isdigit():
                        found.append(text)
                elif key_l in {"id", "orgid", "org_id"}:
                    text = str(value or "").strip()
                    if text.isdigit() and len(text) >= 4:
                        found.append(text)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return _unique_ids(found)


def _stack_slug() -> str | None:
    from app.core.config import settings

    host = (urlsplit(settings.GRAFANA_BASE_URL or "").hostname or "").lower()
    if host.endswith(".grafana.net"):
        slug = host.split(".", 1)[0].strip()
        return slug or None
    return None


def _instance_ids_from_grafana_cloud_api(token: str) -> list[str]:
    """Look up hosted metrics / stack instance ids. Never logs the token or response body."""
    org = _instance_id_from_write_token(token)
    slug = _stack_slug()
    urls = [
        "https://grafana.com/api/hosted-metrics",
        "https://grafana.com/api/instances",
        "https://grafana.com/api/stacks",
        "https://grafana.com/api/hosted-traces",
    ]
    if org:
        urls.extend(
            (
                f"https://grafana.com/api/orgs/{org}/instances",
                f"https://grafana.com/api/orgs/{org}/stacks",
            )
        )
    if slug:
        urls.extend(
            (
                f"https://grafana.com/api/instances/{slug}",
                f"https://grafana.com/api/stacks/{slug}",
            )
        )
    found: list[str] = []
    preferred = (
        "hmInstancePromId",
        "prometheusInstanceId",
        "otlpInstanceId",
        "promId",
        "id",
    )
    org_id = org or ""
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            for url in urls:
                try:
                    response = client.get(
                        url,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    )
                except httpx.HTTPError:
                    continue
                if response.status_code >= 400:
                    continue
                try:
                    payload = response.json()
                except Exception:
                    continue
                found.extend(_numeric_ids_from_payload(payload, preferred))
                preferred_ids = [item for item in _unique_ids(found) if item != org_id]
                if preferred_ids:
                    return preferred_ids
    except Exception as exc:
        logger.warning("Grafana Cloud instance lookup failed: %s", redact_mcp_text(str(exc)))
    preferred_ids = [item for item in _unique_ids(found) if item != org_id]
    return preferred_ids or _unique_ids(found)


def _instance_id_from_grafana_cloud_api(token: str) -> str | None:
    ids = _instance_ids_from_grafana_cloud_api(token)
    return ids[0] if ids else None


def probe_otlp_auth(endpoint: str, headers: dict[str, str]) -> bool | None:
    """POST an empty OTLP metrics body.

    Grafana Cloud returns 401/403 for a wrong instance id, and 400/415/200 when
    Basic auth is accepted. Network failures return None (unknown).
    """
    if not endpoint or not headers.get("Authorization"):
        return False
    url = signal_endpoint(endpoint, "metrics")
    try:
        with httpx.Client(timeout=4.0, follow_redirects=True) as client:
            response = client.post(
                url,
                headers={**headers, "Content-Type": "application/x-protobuf"},
                content=b"",
            )
    except httpx.HTTPError as exc:
        logger.warning("Grafana OTLP auth probe transport failed: %s", redact_mcp_text(str(exc)))
        return None
    logger.info("Grafana OTLP probe http=%s endpoint=%s", response.status_code, url)
    if response.status_code in (401, 403):
        return False
    if response.status_code in (200, 204, 400, 415):
        return True
    # Wrong-region gateways return 5xx after accepting a tenant that lives elsewhere.
    if response.status_code >= 500:
        return False
    logger.warning("Grafana OTLP auth probe returned %s", response.status_code)
    return None


def _write_token() -> str:
    from app.core.config import settings

    for value in (settings.GRAFANA_OTLP_TOKEN, os.environ.get("GRAFANA_OTLP_TOKEN")):
        if isinstance(value, str) and value.strip().startswith("glc_"):
            return value.strip()
    cloud = settings.GRAFANA_CLOUD_API_KEY
    if isinstance(cloud, str) and cloud.strip().startswith("glc_"):
        return cloud.strip()
    return ""


def _grafana_read_token() -> str:
    from app.core.config import settings

    for value in (
        settings.GRAFANA_SERVICE_ACCOUNT_TOKEN,
        settings.GRAFANA_MCP_ACCESS_TOKEN,
        settings.GRAFANA_CLOUD_API_KEY,
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def pick_tempo_datasource(datasources: list[Any]) -> dict[str, Any] | None:
    tempo: list[dict[str, Any]] = []
    for item in datasources:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").lower()
        if kind not in {"tempo", "grafana-tempo-datasource"}:
            continue
        name = str(item.get("name") or "").lower()
        uid = str(item.get("uid") or "").lower()
        if any(key in name or key in uid for key in ("usage", "billing")):
            continue
        tempo.append(item)
    if not tempo:
        return None

    def rank(item: dict[str, Any]) -> tuple[int, str]:
        blob = " ".join(str(item.get(key) or "") for key in ("name", "uid", "url")).lower()
        if "grafanacloud" in blob and "trace" in blob:
            return (0, blob)
        if "tempo" in blob or "trace" in blob:
            return (1, blob)
        return (2, blob)

    tempo.sort(key=rank)
    return tempo[0]


def pick_metrics_datasource(datasources: list[Any]) -> dict[str, Any] | None:
    prometheus: list[dict[str, Any]] = []
    for item in datasources:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "prometheus":
            continue
        name = str(item.get("name") or "").lower()
        uid = str(item.get("uid") or "").lower()
        url = str(item.get("url") or "").lower()
        if any(key in name or key in uid or key in url for key in ("usage", "billing", "cardinality")):
            continue
        prometheus.append(item)
    if not prometheus:
        return None

    def rank(item: dict[str, Any]) -> tuple[int, str]:
        blob = " ".join(str(item.get(key) or "") for key in ("name", "uid", "url")).lower()
        if "grafanacloud" in blob and "prom" in blob:
            return (0, blob)
        if "mimir" in blob or "prom" in blob:
            return (1, blob)
        return (2, blob)

    prometheus.sort(key=rank)
    return prometheus[0]


def _instance_id_from_datasource(ds: dict[str, Any]) -> str | None:
    for key in ("user", "basicAuthUser"):
        value = str(ds.get(key) or "").strip()
        if value.isdigit():
            return value
    json_data = ds.get("jsonData") if isinstance(ds.get("jsonData"), dict) else {}
    for key in ("user", "basicAuthUser", "instanceId", "instance_id"):
        value = str(json_data.get(key) or "").strip()
        if value.isdigit():
            return value
    return None


def resolve_grafana_otlp() -> GrafanaOtlpExport | None:
    """Discover OTLP gateway + auth. Returns None when write ingest cannot be configured."""
    from app.core.config import settings

    write_token = _write_token()
    configured_endpoint = (
        (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or settings.GRAFANA_OTLP_ENDPOINT or "")
        .strip()
        or None
    )
    configured_instance = (settings.GRAFANA_OTLP_INSTANCE_ID or os.environ.get("GRAFANA_OTLP_INSTANCE_ID") or "").strip() or None
    env_headers = parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"))

    prometheus_uid = None
    discovered_endpoint = None
    discovered_instance = None
    read_token = _grafana_read_token()
    if read_token:
        try:
            base = _validated_base()
            with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                datasources = _get_json(client, f"{base}/api/datasources", read_token)
                if isinstance(datasources, list):
                    ds = pick_metrics_datasource(datasources)
                    if ds:
                        prometheus_uid = str(ds.get("uid") or "") or None
                        discovered_endpoint = otlp_gateway_from_metrics_url(str(ds.get("url") or ""))
                        discovered_instance = _instance_id_from_datasource(ds)
                        if not discovered_instance and prometheus_uid:
                            try:
                                full_ds = _get_json(
                                    client,
                                    f"{base}/api/datasources/uid/{prometheus_uid}",
                                    read_token,
                                )
                            except GrafanaVisualError:
                                full_ds = None
                            if isinstance(full_ds, dict):
                                discovered_instance = _instance_id_from_datasource(full_ds)
        except (GrafanaVisualError, httpx.HTTPError) as exc:
            logger.warning("Grafana datasource discovery for OTLP failed: %s", redact_mcp_text(str(exc)))

    # Prometheus datasource URL is the stack's actual region. A hardcoded env
    # endpoint (or the glc_ `m.r` claim) can point at a different gateway.
    endpoint = discovered_endpoint or configured_endpoint or (
        _otlp_endpoint_from_write_token(write_token) if write_token else None
    )
    cloud_ids = _instance_ids_from_grafana_cloud_api(write_token) if write_token else []
    name_ids = _instance_ids_from_token_name(write_token) if write_token else []
    org_id = _instance_id_from_write_token(write_token) if write_token else None
    candidates = _unique_ids(
        [
            configured_instance,
            discovered_instance,
            *cloud_ids,
            *name_ids,
            org_id,
        ]
    )
    headers = dict(env_headers)
    instance_id = None
    accepted: bool | None = None
    if headers.get("Authorization"):
        instance_id = candidates[0] if candidates else None
        accepted = probe_otlp_auth(endpoint or "", headers) if endpoint else False
    elif write_token and endpoint:
        unknown: tuple[str, dict[str, str]] | None = None
        for candidate in candidates:
            probe_headers = basic_otlp_headers(candidate, write_token)
            result = probe_otlp_auth(endpoint, probe_headers)
            logger.info(
                "Grafana OTLP probe instance_id=%s result=%s",
                candidate,
                "accepted" if result is True else "rejected" if result is False else "unreachable",
            )
            if result is True:
                instance_id = candidate
                headers = probe_headers
                accepted = True
                break
            if result is False:
                continue
            if unknown is None:
                unknown = (candidate, probe_headers)
        if accepted is not True and unknown is not None:
            instance_id, headers = unknown
            accepted = None
        elif accepted is not True and candidates and not headers.get("Authorization"):
            accepted = False

    if endpoint and headers.get("Authorization") and accepted is not False:
        if accepted is True:
            set_ingest_status(
                "exporting",
                "StudioTower Space events export to Grafana Cloud OTLP",
            )
        else:
            set_ingest_status(
                "exporting",
                "StudioTower Space events export to Grafana Cloud OTLP (auth probe unreachable)",
            )
        logger.info(
            "Grafana OTLP ingest configured instance_id=%s probe=%s",
            instance_id,
            "accepted" if accepted is True else "unreachable",
        )
        return GrafanaOtlpExport(
            endpoint=normalize_otlp_base(endpoint),
            headers=headers,
            instance_id=instance_id,
            prometheus_uid=prometheus_uid,
        )

    if not write_token:
        set_ingest_status(
            "unavailable",
            "Grafana Cloud OTLP write token is not configured (GRAFANA_OTLP_TOKEN glc_ with metrics:write)",
        )
    elif not endpoint:
        set_ingest_status(
            "unavailable",
            "Grafana Cloud OTLP endpoint could not be discovered from a Prometheus datasource",
        )
    elif accepted is False:
        set_ingest_status(
            "unavailable",
            "Grafana Cloud OTLP rejected the write credentials (instance id or token). Metrics and traces were not exported.",
        )
    elif not headers.get("Authorization"):
        set_ingest_status(
            "unavailable",
            "Grafana Cloud OTLP instance id is missing; cannot build Basic auth for the gateway",
        )
    return None


def studiotower_dashboard_model(
    prometheus_ds: dict[str, Any],
    tempo_ds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ds_ref = {
        "type": str(prometheus_ds.get("type") or "prometheus"),
        "uid": str(prometheus_ds.get("uid") or ""),
    }
    metric = SPACE_EVENTS_PROMQL_NAME

    def panel(panel_id: int, title: str, kind: str, expr: str, grid: dict[str, int], instant: bool = False) -> dict[str, Any]:
        target: dict[str, Any] = {
            "refId": "A",
            "expr": expr,
            "datasource": ds_ref,
            "legendFormat": "{{event_type}}",
        }
        if instant:
            target["instant"] = True
            target["range"] = False
            target["format"] = "table" if kind == "table" else "time_series"
        return {
            "id": panel_id,
            "title": title,
            "type": kind,
            "datasource": ds_ref,
            "gridPos": grid,
            "targets": [target],
        }

    panels: list[dict[str, Any]] = [
        panel(
            1,
            "Space events per minute",
            "timeseries",
            f'sum(rate({metric}{{space_id="$space_id"}}[5m])) * 60',
            {"x": 0, "y": 0, "w": 24, "h": 8},
        ),
        panel(
            2,
            "Messages (24h)",
            "stat",
            f'sum(increase({metric}{{space_id="$space_id",event_type="message.created"}}[24h]))',
            {"x": 0, "y": 8, "w": 8, "h": 6},
            instant=True,
        ),
        panel(
            3,
            "File events (24h)",
            "stat",
            'sum(increase(%s{space_id="$space_id",event_type=~"file.*"}[24h]))' % metric,
            {"x": 8, "y": 8, "w": 8, "h": 6},
            instant=True,
        ),
        panel(
            4,
            "Run events (24h)",
            "stat",
            'sum(increase(%s{space_id="$space_id",event_type=~"run.*"}[24h]))' % metric,
            {"x": 16, "y": 8, "w": 8, "h": 6},
            instant=True,
        ),
        panel(
            5,
            "Events by type (24h)",
            "table",
            f'sum by (event_type) (increase({metric}{{space_id="$space_id"}}[24h]))',
            {"x": 0, "y": 14, "w": 12, "h": 8},
            instant=True,
        ),
        panel(
            6,
            "Lineage resources (24h)",
            "table",
            f'sum by (resource_type) (increase({metric}{{space_id="$space_id"}}[24h]))',
            {"x": 12, "y": 14, "w": 12, "h": 8},
            instant=True,
        ),
    ]
    if tempo_ds and str(tempo_ds.get("uid") or "").strip():
        tempo_ref = {
            "type": str(tempo_ds.get("type") or "tempo"),
            "uid": str(tempo_ds.get("uid") or ""),
        }
        panels.append(
            {
                "id": 7,
                "title": "Space lineage traces (Tempo)",
                "type": "table",
                "datasource": tempo_ref,
                "gridPos": {"x": 0, "y": 22, "w": 24, "h": 8},
                "targets": [
                    {
                        "refId": "A",
                        "datasource": tempo_ref,
                        "queryType": "traceql",
                        "limit": 30,
                        "query": '{ span.space_id = "$space_id" || .space_id = "$space_id" }',
                    }
                ],
            }
        )

    return {
        "uid": STUDIO_TOWER_DASHBOARD_UID,
        "title": STUDIO_TOWER_DASHBOARD_TITLE,
        "schemaVersion": 39,
        "timezone": "browser",
        "editable": True,
        "panels": panels,
        "templating": {
            "list": [
                {
                    "name": "space_id",
                    "type": "textbox",
                    "label": "Space",
                    "current": {"text": "", "value": ""},
                    "options": [],
                }
            ]
        },
        "tags": ["studiotower", "space-activity"],
    }


def ensure_studiotower_dashboard(
    client: httpx.Client,
    base: str,
    token: str,
    prometheus_ds: dict[str, Any],
    tempo_ds: dict[str, Any] | None = None,
) -> str | None:
    dashboard = studiotower_dashboard_model(prometheus_ds, tempo_ds=tempo_ds)
    if not dashboard["panels"][0]["datasource"].get("uid"):
        return "Prometheus datasource uid is missing; StudioTower dashboard was not created"
    try:
        existing = client.get(
            f"{base}/api/dashboards/uid/{STUDIO_TOWER_DASHBOARD_UID}",
            headers=_headers(token),
        )
        if existing.status_code == 200:
            return None
    except httpx.HTTPError:
        pass
    try:
        response = client.post(
            f"{base}/api/dashboards/db",
            headers={**_headers(token), "Content-Type": "application/json"},
            json={"dashboard": dashboard, "overwrite": True, "message": "StudioTower Space activity"},
        )
    except httpx.HTTPError as exc:
        logger.warning("Grafana dashboard upsert transport failed: %s", redact_mcp_text(str(exc)))
        return "Grafana dashboard upsert is unreachable"
    if response.status_code in (200, 201):
        return None
    if response.status_code in (401, 403):
        # Viewer / read-only service accounts cannot create dashboards. The visual
        # board still queries Prometheus with the in-app StudioTower panel model.
        logger.info("Grafana service account cannot upsert dashboards; using in-app StudioTower panels")
        return None
    return f"Grafana dashboard upsert returned {response.status_code}"


def grafana_host_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith(".grafana.net")
