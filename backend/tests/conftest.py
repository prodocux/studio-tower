import pytest
from app.core.config import settings

@pytest.fixture(autouse=True)
def clean_test_environment(monkeypatch):
    """Ensure tests run in clean, deterministic environment isolated from ambient host credentials."""
    monkeypatch.setenv("STUDIO_TOWER_AUTH_MODE", "dev")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GRAFANA_MCP_ENDPOINT", raising=False)
    monkeypatch.delenv("GRAFANA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_OTLP_TOKEN", raising=False)
    monkeypatch.delenv("GRAFANA_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("GRAFANA_OTLP_INSTANCE_ID", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    settings.STUDIO_TOWER_AUTH_MODE = "dev"
    settings.ENV = "development"
    settings.AI_FALLBACK_ALLOWED = False
    settings.GRAFANA_MCP_ENDPOINT = None
    settings.GRAFANA_CLOUD_API_KEY = None
    settings.GRAFANA_SERVICE_ACCOUNT_TOKEN = None
    settings.GRAFANA_MCP_ACCESS_TOKEN = None
    settings.GRAFANA_OTLP_TOKEN = None
    settings.GRAFANA_OTLP_ENDPOINT = None
    settings.GRAFANA_OTLP_INSTANCE_ID = None
    from app.integrations.grafana_mcp import grafana_mcp
    grafana_mcp.reset_runtime_state()
    yield


@pytest.fixture(autouse=True)
def stub_gemini_deliverable_content(monkeypatch):
    """Execute-action tests never call live Gemini; production file content does."""
    from app.agent.brain import AgentBrain
    from app.agent.schemas import DeliverableContent, DeliverableScene

    def _compose(cls, **_kwargs):
        return DeliverableContent(
            title="Test deliverable",
            summary="Gemini test double",
            scenes=[
                DeliverableScene(
                    scene_number="1",
                    slugline="EXT. TEST STAGE - DAY",
                    day_night="DAY",
                    location="TEST STAGE",
                    characters=["MAYA"],
                )
            ],
            characters=["MAYA"],
        )

    monkeypatch.setattr(AgentBrain, "compose_deliverable_content", classmethod(_compose))
