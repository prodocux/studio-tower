from unittest.mock import patch, MagicMock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings, Settings
from app.api.maintenance_routes import _verify_maintenance_secret


def test_verify_maintenance_secret_with_valid_secret():
    # Timing-safe comparison with valid secret should not raise
    _verify_maintenance_secret(secret=settings.STUDIO_TOWER_MAINTENANCE_SECRET)


def test_verify_maintenance_secret_with_invalid_secret():
    with pytest.raises(HTTPException) as exc_info:
        _verify_maintenance_secret(secret="invalid-secret-key-123")
    assert exc_info.value.status_code == 403


def test_verify_maintenance_oidc_authorized_service_account_and_audience():
    project_id = "agentic-cinema-demo-2026"
    expected_sa = f"studiotower-api@{project_id}.iam.gserviceaccount.com"
    expected_aud = "https://studio-tower-api.run.app"

    valid_claim = {
        "iss": "https://accounts.google.com",
        "email": expected_sa,
        "email_verified": True,
        "aud": expected_aud,
    }

    mock_verify = MagicMock(return_value=valid_claim)

    with patch.object(settings, "STUDIO_TOWER_FIREBASE_PROJECT_ID", project_id), \
         patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", expected_sa), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch.object(settings, "STUDIO_TOWER_SCHEDULER_AUDIENCE", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", mock_verify):

        _verify_maintenance_secret(authorization="Bearer valid-google-oidc-token")

        # Verify that audience was explicitly passed to google verifier
        mock_verify.assert_called_once()
        _, kwargs = mock_verify.call_args
        assert kwargs.get("audience") == expected_aud


def test_verify_maintenance_oidc_wrong_audience_rejected():
    expected_sa = "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com"
    expected_aud = "https://studio-tower-api.run.app"

    # Verifier raises ValueError on audience mismatch (standard google-auth behavior)
    mock_verify = MagicMock(side_effect=ValueError("Token audience does not match expected audience"))

    with patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", expected_sa), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch.object(settings, "STUDIO_TOWER_SCHEDULER_AUDIENCE", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", mock_verify):

        with pytest.raises(HTTPException) as exc_info:
            _verify_maintenance_secret(authorization="Bearer token-with-wrong-audience")
        assert exc_info.value.status_code == 403


def test_verify_maintenance_oidc_claim_audience_mismatch_rejected():
    expected_sa = "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com"
    expected_aud = "https://studio-tower-api.run.app"

    # Even if verifier returned claim, internal aud check must match expected_aud
    claim_with_different_aud = {
        "iss": "https://accounts.google.com",
        "email": expected_sa,
        "email_verified": True,
        "aud": "https://different-service.run.app",
    }
    mock_verify = MagicMock(return_value=claim_with_different_aud)

    with patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", expected_sa), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch.object(settings, "STUDIO_TOWER_SCHEDULER_AUDIENCE", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", mock_verify):

        with pytest.raises(HTTPException) as exc_info:
            _verify_maintenance_secret(authorization="Bearer token-mismatched-aud")
        assert exc_info.value.status_code == 403


def test_verify_maintenance_oidc_compute_sa_rejected():
    expected_sa = "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com"
    expected_aud = "https://studio-tower-api.run.app"

    # Other project's or default compute engine SA must be rejected
    compute_claim = {
        "iss": "https://accounts.google.com",
        "email": "123456789-compute@developer.gserviceaccount.com",
        "email_verified": True,
        "aud": expected_aud,
    }
    mock_verify = MagicMock(return_value=compute_claim)

    with patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", expected_sa), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", mock_verify):

        with pytest.raises(HTTPException) as exc_info:
            _verify_maintenance_secret(authorization="Bearer token-compute-sa")
        assert exc_info.value.status_code == 403


def test_verify_maintenance_oidc_appspot_sa_rejected_when_not_exact_match():
    expected_sa = "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com"
    expected_aud = "https://studio-tower-api.run.app"

    appspot_claim = {
        "iss": "https://accounts.google.com",
        "email": "agentic-cinema-demo-2026@appspot.gserviceaccount.com",
        "email_verified": True,
        "aud": expected_aud,
    }
    mock_verify = MagicMock(return_value=appspot_claim)

    with patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", expected_sa), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", mock_verify):

        with pytest.raises(HTTPException) as exc_info:
            _verify_maintenance_secret(authorization="Bearer token-appspot-sa")
        assert exc_info.value.status_code == 403


def test_verify_maintenance_oidc_unverified_email_rejected():
    expected_sa = "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com"
    expected_aud = "https://studio-tower-api.run.app"

    unverified_claim = {
        "iss": "https://accounts.google.com",
        "email": expected_sa,
        "email_verified": False,
        "aud": expected_aud,
    }
    mock_verify = MagicMock(return_value=unverified_claim)

    with patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", expected_sa), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", mock_verify):

        with pytest.raises(HTTPException) as exc_info:
            _verify_maintenance_secret(authorization="Bearer token-unverified")
        assert exc_info.value.status_code == 403


def test_verify_maintenance_oidc_unconfigured_sa_rejected():
    expected_aud = "https://studio-tower-api.run.app"

    valid_claim = {
        "iss": "https://accounts.google.com",
        "email": "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com",
        "email_verified": True,
        "aud": expected_aud,
    }
    mock_verify = MagicMock(return_value=valid_claim)

    # When STUDIO_TOWER_SCHEDULER_SA is None, OIDC should fail closed
    with patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", None), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", mock_verify):

        with pytest.raises(HTTPException) as exc_info:
            _verify_maintenance_secret(authorization="Bearer token-no-sa-configured")
        assert exc_info.value.status_code == 403


def test_reconcile_endpoint_accepts_oidc_token():
    client = TestClient(app)
    project_id = "agentic-cinema-demo-2026"
    expected_sa = f"studiotower-api@{project_id}.iam.gserviceaccount.com"
    expected_aud = "https://studio-tower-api.run.app"

    valid_claim = {
        "iss": "https://accounts.google.com",
        "email": expected_sa,
        "email_verified": True,
        "aud": expected_aud,
    }
    with patch.object(settings, "STUDIO_TOWER_FIREBASE_PROJECT_ID", project_id), \
         patch.object(settings, "STUDIO_TOWER_SCHEDULER_SA", expected_sa), \
         patch.object(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", expected_aud), \
         patch.object(settings, "STUDIO_TOWER_SCHEDULER_AUDIENCE", expected_aud), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=valid_claim):
        resp = client.post(
            "/v1/maintenance/reconcile-actions-and-cleanup",
            headers={"Authorization": "Bearer mock-google-oidc-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("success", "partial_success")
        assert "metrics" in data


def test_config_rejects_worker_url_mismatch_allowed_worker_host():
    with pytest.raises(ValueError, match="does not match authorized STUDIO_TOWER_ALLOWED_WORKER_HOST"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="custom-tenant-project-2026",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="custom-tenant-bucket",
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
            STUDIO_TOWER_WORKER_SERVICE_URL="https://foreign-service-uc.a.run.app",
            STUDIO_TOWER_ALLOWED_WORKER_HOST="authoritative-service-uc.a.run.app",
            STUDIO_TOWER_SCHEDULER_SA="studiotower-api@custom-tenant-project-2026.iam.gserviceaccount.com",
            STUDIO_TOWER_TASK_SECRET="super-secret-task-key-32chars-minimum-length-prod",
            STUDIO_TOWER_MAINTENANCE_SECRET="super-secret-maint-key-32chars-minimum-length-prod",
            ACTION_SIGNING_SECRET="super-secret-action-key-32chars-minimum-length-prod",
            CURSOR_SIGNING_SECRET="super-secret-cursor-key-32chars-minimum-length-prod",
            TELEMETRY_TENANT_KEY_PRIMARY="super-secret-telemetry-key-32chars-minimum-prod",
            CORS_ORIGINS=["https://custom-tenant-project-2026.web.app"],
        )


def test_config_rejects_empty_allowed_worker_host_in_production():
    with pytest.raises(ValueError, match="STUDIO_TOWER_ALLOWED_WORKER_HOST"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="custom-tenant-project-2026",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="custom-tenant-bucket",
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
            STUDIO_TOWER_WORKER_SERVICE_URL="https://authoritative-service-uc.a.run.app",
            STUDIO_TOWER_ALLOWED_WORKER_HOST="",
            STUDIO_TOWER_SCHEDULER_SA="studiotower-api@custom-tenant-project-2026.iam.gserviceaccount.com",
            STUDIO_TOWER_TASK_SECRET="super-secret-task-key-32chars-minimum-length-prod",
            STUDIO_TOWER_MAINTENANCE_SECRET="super-secret-maint-key-32chars-minimum-length-prod",
            ACTION_SIGNING_SECRET="super-secret-action-key-32chars-minimum-length-prod",
            CURSOR_SIGNING_SECRET="super-secret-cursor-key-32chars-minimum-length-prod",
            TELEMETRY_TENANT_KEY_PRIMARY="super-secret-telemetry-key-32chars-minimum-prod",
            CORS_ORIGINS=["https://custom-tenant-project-2026.web.app"],
        )


def test_config_requires_scheduler_sa_in_production():
    with pytest.raises(ValueError, match="STUDIO_TOWER_SCHEDULER_SA"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="custom-tenant-project-2026",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="custom-tenant-bucket",
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
            STUDIO_TOWER_WORKER_SERVICE_URL="https://authoritative-service-uc.a.run.app",
            STUDIO_TOWER_ALLOWED_WORKER_HOST="authoritative-service-uc.a.run.app",
            STUDIO_TOWER_SCHEDULER_SA=None,
            STUDIO_TOWER_TASK_SECRET="super-secret-task-key-32chars-minimum-length-prod",
            STUDIO_TOWER_MAINTENANCE_SECRET="super-secret-maint-key-32chars-minimum-length-prod",
            ACTION_SIGNING_SECRET="super-secret-action-key-32chars-minimum-length-prod",
            CURSOR_SIGNING_SECRET="super-secret-cursor-key-32chars-minimum-length-prod",
            TELEMETRY_TENANT_KEY_PRIMARY="super-secret-telemetry-key-32chars-minimum-prod",
            CORS_ORIGINS=["https://custom-tenant-project-2026.web.app"],
        )


def test_settings_real_environ_parsing(monkeypatch):
    """
    Verifies that Settings() parses production string environment variables
    exactly as injected by Cloud Run deploy scripts (no JSON list errors).
    """
    env_map = {
        "ENV": "production",
        "STUDIO_TOWER_AUTH_MODE": "firebase",
        "STUDIO_TOWER_FIREBASE_PROJECT_ID": "agentic-cinema-demo-2026",
        "STUDIO_TOWER_STORE": "firestore",
        "STUDIO_TOWER_ARTIFACT_BACKEND": "gcs",
        "STUDIO_TOWER_GCS_BUCKET": "agentic-cinema-demo-2026-studiotower-artifacts",
        "INGESTION_RUNNER": "cloud_tasks",
        "ACTION_RUNNER": "cloud_tasks",
        "STUDIO_TOWER_WORKER_SERVICE_URL": "https://studio-tower-api-mbvd6nfacq-uc.a.run.app",
        "STUDIO_TOWER_ALLOWED_WORKER_HOST": "studio-tower-api-mbvd6nfacq-uc.a.run.app",
        "STUDIO_TOWER_SCHEDULER_SA": "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com",
        "STUDIO_TOWER_SCHEDULER_AUDIENCE": "https://studio-tower-api-mbvd6nfacq-uc.a.run.app",
        "STUDIO_TOWER_TASK_SECRET": "s" * 32,
        "STUDIO_TOWER_MAINTENANCE_SECRET": "m" * 32,
        "ACTION_SIGNING_SECRET": "a" * 32,
        "CURSOR_SIGNING_SECRET": "c" * 32,
        "TELEMETRY_TENANT_KEY_PRIMARY": "t" * 32,
        "CORS_ORIGINS": '["https://agentic-cinema-demo-2026.web.app"]',
    }
    for k, v in env_map.items():
        monkeypatch.setenv(k, v)

    cfg = Settings()
    assert cfg.ENV == "production"
    assert cfg.STUDIO_TOWER_ALLOWED_WORKER_HOST == "studio-tower-api-mbvd6nfacq-uc.a.run.app"
    assert cfg.STUDIO_TOWER_WORKER_SERVICE_URL == "https://studio-tower-api-mbvd6nfacq-uc.a.run.app"
    assert cfg.STUDIO_TOWER_SCHEDULER_SA == "studiotower-api@agentic-cinema-demo-2026.iam.gserviceaccount.com"
    assert cfg.STUDIO_TOWER_SCHEDULER_AUDIENCE == "https://studio-tower-api-mbvd6nfacq-uc.a.run.app"
