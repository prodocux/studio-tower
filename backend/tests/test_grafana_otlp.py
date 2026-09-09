from app.core.space_metrics import SPACE_EVENTS_PROMQL_NAME
from app.integrations.grafana_otlp import (
    otlp_gateway_from_metrics_url,
    pick_metrics_datasource,
    pick_tempo_datasource,
    studiotower_dashboard_model,
)


def test_otlp_gateway_from_prometheus_url():
    assert (
        otlp_gateway_from_metrics_url("https://prometheus-prod-36-prod-us-central-0.grafana.net/api/prom")
        == "https://otlp-gateway-prod-us-central-0.grafana.net/otlp"
    )
    assert (
        otlp_gateway_from_metrics_url("https://prometheus-prod-56-prod-us-east-2.grafana.net/api/prom")
        == "https://otlp-gateway-prod-us-east-2.grafana.net/otlp"
    )


def test_otlp_endpoint_from_glc_region_claim():
    from app.integrations.grafana_otlp import _otlp_endpoint_from_write_token
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"m": {"r": "prod-us-east-0"}}).encode()).decode().rstrip("=")
    assert _otlp_endpoint_from_write_token(f"glc_{payload}") == "https://otlp-gateway-prod-us-east-0.grafana.net/otlp"


def test_instance_id_from_glc_org_claim():
    from app.integrations.grafana_otlp import _instance_id_from_write_token
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"o": 1861079, "m": {"r": "prod-us-east-0"}}).encode()).decode().rstrip("=")
    assert _instance_id_from_write_token(f"glc_{payload}") == "1861079"


def test_instance_ids_from_token_name_stack_claim():
    from app.integrations.grafana_otlp import _instance_ids_from_token_name
    import base64
    import json

    payload = base64.urlsafe_b64encode(
        json.dumps({"n": "stack-39812-otlp-write", "o": 1861079}).encode()
    ).decode().rstrip("=")
    assert _instance_ids_from_token_name(f"glc_{payload}") == ["39812"]


def test_resolve_otlp_falls_back_to_glc_org_id_when_datasource_has_no_user():
    import base64
    import json
    from unittest.mock import patch

    from app.core.config import settings
    from app.integrations import grafana_otlp

    payload = (
        base64.urlsafe_b64encode(json.dumps({"o": 1861079, "m": {"r": "prod-us-east-0"}}).encode())
        .decode()
        .rstrip("=")
    )
    settings.GRAFANA_OTLP_TOKEN = f"glc_{payload}"
    settings.GRAFANA_OTLP_ENDPOINT = "https://otlp-gateway-prod-us-east-0.grafana.net/otlp"
    with patch.object(grafana_otlp, "_instance_ids_from_grafana_cloud_api", return_value=[]), patch.object(
        grafana_otlp, "_grafana_read_token", return_value=""
    ), patch.object(grafana_otlp, "probe_otlp_auth", return_value=True):
        result = grafana_otlp.resolve_grafana_otlp()
    assert result is not None
    assert result.instance_id == "1861079"
    assert result.headers.get("Authorization", "").startswith("Basic ")
    assert grafana_otlp.get_ingest_status()[0] == "exporting"


def test_resolve_otlp_skips_org_id_when_probe_rejects_it():
    import base64
    import json
    from unittest.mock import patch

    from app.core.config import settings
    from app.integrations import grafana_otlp

    payload = (
        base64.urlsafe_b64encode(json.dumps({"o": 1861079, "n": "stack-39812-write", "m": {"r": "prod-us-east-0"}}).encode())
        .decode()
        .rstrip("=")
    )
    settings.GRAFANA_OTLP_TOKEN = f"glc_{payload}"
    settings.GRAFANA_OTLP_ENDPOINT = "https://otlp-gateway-prod-us-east-0.grafana.net/otlp"

    def _probe(_endpoint, headers):
        blob = headers.get("Authorization") or ""
        return "Mzk4MTI6" in blob  # base64('39812:') prefix

    with patch.object(grafana_otlp, "_instance_ids_from_grafana_cloud_api", return_value=[]), patch.object(
        grafana_otlp, "_grafana_read_token", return_value=""
    ), patch.object(grafana_otlp, "probe_otlp_auth", side_effect=_probe):
        result = grafana_otlp.resolve_grafana_otlp()
    assert result is not None
    assert result.instance_id == "39812"
    assert grafana_otlp.get_ingest_status()[0] == "exporting"


def test_resolve_otlp_unavailable_when_all_instance_ids_are_rejected():
    import base64
    import json
    from unittest.mock import patch

    from app.core.config import settings
    from app.integrations import grafana_otlp

    payload = (
        base64.urlsafe_b64encode(json.dumps({"o": 1861079, "m": {"r": "prod-us-east-0"}}).encode())
        .decode()
        .rstrip("=")
    )
    settings.GRAFANA_OTLP_TOKEN = f"glc_{payload}"
    settings.GRAFANA_OTLP_ENDPOINT = "https://otlp-gateway-prod-us-east-0.grafana.net/otlp"
    with patch.object(grafana_otlp, "_instance_ids_from_grafana_cloud_api", return_value=[]), patch.object(
        grafana_otlp, "_grafana_read_token", return_value=""
    ), patch.object(grafana_otlp, "probe_otlp_auth", return_value=False):
        result = grafana_otlp.resolve_grafana_otlp()
    assert result is None
    assert grafana_otlp.get_ingest_status()[0] == "unavailable"


def test_probe_otlp_auth_rejects_wrong_region_gateway():
    from unittest.mock import MagicMock, patch

    from app.integrations import grafana_otlp

    response = MagicMock()
    response.status_code = 502
    client = MagicMock()
    client.post.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    with patch.object(grafana_otlp.httpx, "Client", return_value=client):
        assert (
            grafana_otlp.probe_otlp_auth(
                "https://otlp-gateway-prod-us-east-0.grafana.net/otlp",
                {"Authorization": "Basic x"},
            )
            is False
        )


def test_resolve_otlp_prefers_prometheus_gateway_over_env_endpoint():
    import base64
    import json
    from unittest.mock import patch

    from app.core.config import settings
    from app.integrations import grafana_otlp

    payload = (
        base64.urlsafe_b64encode(json.dumps({"o": 1861079, "m": {"r": "prod-us-east-0"}}).encode())
        .decode()
        .rstrip("=")
    )
    settings.GRAFANA_OTLP_TOKEN = f"glc_{payload}"
    settings.GRAFANA_OTLP_ENDPOINT = "https://otlp-gateway-prod-us-east-0.grafana.net/otlp"
    settings.GRAFANA_OTLP_INSTANCE_ID = "1742899"
    datasources = [
        {
            "type": "prometheus",
            "uid": "prom",
            "name": "grafanacloud-prom",
            "url": "https://prometheus-prod-56-prod-us-east-2.grafana.net/api/prom",
            "user": "",
            "basicAuthUser": "3410022",
        }
    ]
    with patch.object(grafana_otlp, "_instance_ids_from_grafana_cloud_api", return_value=[]), patch.object(
        grafana_otlp, "_grafana_read_token", return_value="glsa_x"
    ), patch.object(grafana_otlp, "_validated_base", return_value="https://loftyladybug3305.grafana.net"), patch.object(
        grafana_otlp, "_get_json", return_value=datasources
    ), patch.object(grafana_otlp, "probe_otlp_auth", return_value=True):
        result = grafana_otlp.resolve_grafana_otlp()
    assert result is not None
    assert result.endpoint == "https://otlp-gateway-prod-us-east-2.grafana.net/otlp"
    assert result.instance_id == "1742899"
    assert grafana_otlp.get_ingest_status()[0] == "exporting"


def test_pick_metrics_datasource_skips_billing_usage():
    chosen = pick_metrics_datasource(
        [
            {
                "name": "Grafana Cloud Billing/Usage",
                "type": "prometheus",
                "uid": "grafanacloud-usage",
                "url": "https://billing.example",
            },
            {
                "name": "grafanacloud-prom",
                "type": "prometheus",
                "uid": "prom",
                "url": "https://prometheus-prod-10-prod-us-east-0.grafana.net/api/prom",
            },
        ]
    )
    assert chosen is not None
    assert chosen["uid"] == "prom"


def test_pick_tempo_datasource_skips_usage():
    chosen = pick_tempo_datasource(
        [
            {"name": "Usage traces", "type": "tempo", "uid": "usage-traces"},
            {"name": "grafanacloud-traces", "type": "tempo", "uid": "grafanacloud-traces"},
        ]
    )
    assert chosen is not None
    assert chosen["uid"] == "grafanacloud-traces"


def test_studiotower_dashboard_queries_real_space_metric():
    dashboard = studiotower_dashboard_model({"type": "prometheus", "uid": "prom"})
    blob = str(dashboard)
    assert SPACE_EVENTS_PROMQL_NAME in blob
    assert "$space_id" in blob
    assert "resource_type" in blob
    assert dashboard["uid"] == "studiotower-monitor"
    assert "usage" not in blob.lower() or "studiotower" in blob.lower()


def test_studiotower_dashboard_includes_tempo_lineage_panel():
    dashboard = studiotower_dashboard_model(
        {"type": "prometheus", "uid": "prom"},
        tempo_ds={"type": "tempo", "uid": "grafanacloud-traces"},
    )
    titles = [panel["title"] for panel in dashboard["panels"]]
    assert "Space lineage traces (Tempo)" in titles
    tempo_panel = next(panel for panel in dashboard["panels"] if "Tempo" in panel["title"])
    assert tempo_panel["targets"][0]["queryType"] == "traceql"
    assert "$space_id" in tempo_panel["targets"][0]["query"]


def test_space_id_survives_span_sanitizer_for_tempo():
    from app.core.telemetry_sanitizer import sanitize_span_attributes

    clean = sanitize_span_attributes(
        {
            "space_id": "spc_sandbox3",
            "event_type": "run.started",
            "resource_type": "run",
            "resource_id": "run_act_91c6c00dcf1d",
            "authorization": "Bearer secret",
        }
    )
    assert clean["space_id"] == "spc_sandbox3"
    assert clean["event_type"] == "run.started"
    assert clean["resource_id"] == "run_act_91c6c00dcf1d"
    assert "authorization" not in clean
