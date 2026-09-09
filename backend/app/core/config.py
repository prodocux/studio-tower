from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "StudioTower API"
    APP_VERSION: str = "0.1.0"
    ENV: str = "development"  # "development", "staging", or "production"

    # Auth Mode: "dev" or "firebase"
    STUDIO_TOWER_AUTH_MODE: Literal["dev", "firebase"] = "dev"
    STUDIO_TOWER_FIREBASE_PROJECT_ID: str = ""

    # Storage Backend: "memory", "json", or "firestore"
    STUDIO_TOWER_STORE: Literal["memory", "json", "firestore"] = "memory"
    STUDIO_TOWER_DATA_DIR: str = "./data"

    # GCS Artifact Backend
    STUDIO_TOWER_ARTIFACT_BACKEND: Literal["local", "gcs"] = "local"
    STUDIO_TOWER_GCS_BUCKET: str = ""

    # Maintenance Secret for Cloud Scheduler / background maintenance jobs
    STUDIO_TOWER_MAINTENANCE_SECRET: str = "studiotower-internal-maintenance-key-9f8a7b"

    # Maximum file upload size in bytes (default: 50MB)
    MAX_UPLOAD_SIZE_BYTES: int = 50 * 1024 * 1024

    # Document Ingestion Job Runner
    INGESTION_RUNNER: Literal["inline", "threadpool", "cloud_tasks"] = "inline"
    ACTION_RUNNER: Literal["inline", "threadpool", "cloud_tasks"] = "inline"
    STUDIO_TOWER_CLOUD_TASKS_PROJECT: str = ""
    STUDIO_TOWER_CLOUD_TASKS_LOCATION: str = "us-central1"
    STUDIO_TOWER_CLOUD_TASKS_QUEUE: str = "studiotower-ingestion-queue"
    STUDIO_TOWER_ACTION_QUEUE: str = "studiotower-action-queue"
    STUDIO_TOWER_WORKER_SERVICE_URL: str = ""
    STUDIO_TOWER_SCHEDULER_SA: str | None = None
    STUDIO_TOWER_SCHEDULER_AUDIENCE: str | None = None
    STUDIO_TOWER_ALLOWED_WORKER_HOST: str = ""
    STUDIO_TOWER_TASK_SECRET: str = "studiotower-task-secret-key-1a2b3c"
    STUDIO_TOWER_TASK_SECRET_PREVIOUS: str | None = None
    ACTION_SIGNING_SECRET: str = "default-studiotower-action-signing-secret-v2-32chars"
    ACTION_SIGNING_SECRET_PREV: str | None = None
    CURSOR_SIGNING_SECRET: str = "studiotower-cursor-signing-secret-key-prod-32b"

    # Gemini AI Model Configuration (GA Production Model)
    GEMINI_MODEL: str = "gemini-3.6-flash"
    AI_FALLBACK_ALLOWED: bool = False
    AI_INFERENCE_TIMEOUT_SECONDS: float = 45.0

    # Telemetry Verification & Tracing Backend Configuration
    TEMPO_QUERY_ENDPOINT: str | None = None
    TEMPO_QUERY_TIMEOUT_SECONDS: float = 3.0
    TELEMETRY_VERIFICATION_MAX_ATTEMPTS: int = 3
    TELEMETRY_VERIFICATION_THROTTLE_SECONDS: float = 2.0
    TELEMETRY_VERIFICATION_GRACE_PERIOD_SECONDS: float = 30.0
    TELEMETRY_TERMINAL_COOLDOWN_SECONDS: float = 60.0

    # Grafana Deep-Linking Configuration (Slice E Phase E4)
    GRAFANA_BASE_URL: str | None = None
    GRAFANA_DASHBOARD_ID: str = "studiotower-monitor"
    GRAFANA_ALLOWED_HOSTS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    GRAFANA_TIME_BUFFER_MINUTES: int = Field(15, ge=1, le=60)
    GRAFANA_MAX_TIME_WINDOW_HOURS: int = Field(24, ge=1, le=24)

    # Grafana Cloud MCP (Streamable HTTP JSON-RPC). Empty = local telemetry only.
    GRAFANA_MCP_ENDPOINT: str | None = None
    GRAFANA_CLOUD_API_KEY: str | None = None
    GRAFANA_SERVICE_ACCOUNT_TOKEN: str | None = None
    GRAFANA_MCP_ACCESS_TOKEN: str | None = None
    GRAFANA_MCP_TIMEOUT_SECONDS: float = Field(8.0, ge=1.0, le=30.0)
    GRAFANA_OTLP_ENDPOINT: str | None = None
    GRAFANA_OTLP_TOKEN: str | None = None
    GRAFANA_OTLP_INSTANCE_ID: str | None = None

    @field_validator("GRAFANA_ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_grafana_allowed_hosts(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                import json

                parsed = json.loads(stripped)
                if not isinstance(parsed, list):
                    raise ValueError("GRAFANA_ALLOWED_HOSTS JSON value must be a list")
                return parsed
            return [host.strip() for host in stripped.split(",") if host.strip()]
        return value

    # CORS
    # In development, local Vite/Preact dev servers are permitted.
    # In production, ONLY explicit HTTPS hosting domains are permitted (no localhost, no http, no wildcards).
    CORS_ORIGINS: list[str] = [
        "https://agentic-cinema-demo-2026.web.app",
        "https://agentic-cinema-demo-2026.firebaseapp.com",
    ]

    # Failure Injection Gating (Slice F)
    ENABLE_FAILURE_INJECTION: bool = False

    # Trusted Reverse Proxy Header Policy (Slice F Anti-Spoofing)
    TRUST_PROXY_HEADERS: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        # Case-insensitive environment normalization
        normalized_env = self.ENV.strip().lower()
        self.ENV = normalized_env

        # Inject dev localhost origins and local worker URL in non-production mode
        if normalized_env not in ("production", "prod"):
            if not self.STUDIO_TOWER_WORKER_SERVICE_URL:
                self.STUDIO_TOWER_WORKER_SERVICE_URL = "http://localhost:8000"

        if normalized_env == "development":
            dev_defaults = [
                "http://localhost:3000",
                "http://localhost:5173",
                "http://127.0.0.1:3000",
                "http://127.0.0.1:5173",
            ]
            for origin in dev_defaults:
                if origin not in self.CORS_ORIGINS:
                    self.CORS_ORIGINS.append(origin)

        # Production fail-closed for failure simulation
        if normalized_env in ("production", "prod") and self.ENABLE_FAILURE_INJECTION:
            raise ValueError(
                "ENABLE_FAILURE_INJECTION is strictly forbidden in production. "
                "Failure injection is only permissible in isolated staging environments."
            )

        # In production mode, validate security settings
        if normalized_env in ("production", "prod"):
            # Production CORS validation: no wildcards, no localhost/127.0.0.1, only strict HTTPS origins
            if not self.CORS_ORIGINS:
                raise ValueError("Production CORS configuration must specify at least one approved HTTPS origin.")

            for origin in self.CORS_ORIGINS:
                orig_lower = origin.strip().lower()
                if "*" in orig_lower:
                    raise ValueError(
                        "Production CORS configuration must not contain wildcard ('*') origins "
                        "when credential support is enabled."
                    )
                if "localhost" in orig_lower or "127.0.0.1" in orig_lower or "::1" in orig_lower:
                    raise ValueError(
                        f"Production CORS configuration must not permit localhost or loopback origins: '{origin}'."
                    )
                if not orig_lower.startswith("https://"):
                    raise ValueError(
                        f"Production CORS configuration must only permit HTTPS origins: '{origin}'."
                    )

            if self.STUDIO_TOWER_AUTH_MODE == "dev":
                raise ValueError(
                    "Production deployment cannot use forgeable dev auth mode. "
                    "Set STUDIO_TOWER_AUTH_MODE=firebase and provide STUDIO_TOWER_FIREBASE_PROJECT_ID."
                )
            if not self.STUDIO_TOWER_FIREBASE_PROJECT_ID:
                raise ValueError(
                    "STUDIO_TOWER_FIREBASE_PROJECT_ID is required when running in production."
                )
            if self.STUDIO_TOWER_STORE != "firestore":
                raise ValueError(
                    "Production deployment requires distributed cloud storage (STUDIO_TOWER_STORE=firestore). "
                    f"'{self.STUDIO_TOWER_STORE}' is not permitted in production."
                )
            if self.STUDIO_TOWER_ARTIFACT_BACKEND != "gcs":
                raise ValueError(
                    "Production deployment requires shared durable cloud artifact storage (STUDIO_TOWER_ARTIFACT_BACKEND=gcs). "
                    f"Ephemeral '{self.STUDIO_TOWER_ARTIFACT_BACKEND}' backend is not permitted in production."
                )
            if not self.STUDIO_TOWER_GCS_BUCKET:
                raise ValueError(
                    "STUDIO_TOWER_GCS_BUCKET is required when running in production with GCS artifact backend."
                )
            if self.INGESTION_RUNNER != "cloud_tasks":
                raise ValueError(
                    "Production deployment requires a durable queue for document ingestion (INGESTION_RUNNER=cloud_tasks). "
                    f"'{self.INGESTION_RUNNER}' is not permitted in production."
                )
            if self.INGESTION_RUNNER == "cloud_tasks":
                if not self.STUDIO_TOWER_CLOUD_TASKS_PROJECT:
                    self.STUDIO_TOWER_CLOUD_TASKS_PROJECT = self.STUDIO_TOWER_FIREBASE_PROJECT_ID
                # Require explicit, high-entropy task secret for Cloud Tasks worker in production
                insecure_task_secrets = {"", "studiotower-task-secret-key-1a2b3c", "change-me", "default", "secret"}
                if (
                    not self.STUDIO_TOWER_TASK_SECRET
                    or self.STUDIO_TOWER_TASK_SECRET in insecure_task_secrets
                    or len(self.STUDIO_TOWER_TASK_SECRET) < 32
                ):
                    raise ValueError(
                        "Production deployment requires an explicit, high-entropy STUDIO_TOWER_TASK_SECRET "
                        "(at least 32 characters) for Cloud Tasks worker authentication."
                    )

            # Require explicit, high-entropy maintenance secret in production
            insecure_secrets = {"", "studiotower-internal-maintenance-key-9f8a7b", "change-me", "default", "secret"}
            if (
                not self.STUDIO_TOWER_MAINTENANCE_SECRET
                or self.STUDIO_TOWER_MAINTENANCE_SECRET in insecure_secrets
                or len(self.STUDIO_TOWER_MAINTENANCE_SECRET) < 32
            ):
                raise ValueError(
                    "Production deployment requires an explicit, high-entropy STUDIO_TOWER_MAINTENANCE_SECRET "
                    "(at least 32 characters) for Cloud Scheduler authentication."
                )

            # Require explicit, high-entropy cursor signing secret in production
            insecure_cursor_secrets = {"", "studiotower-cursor-signing-secret-key-prod-32b", "default-dev-cursor-secret-key-32-chars-long", "change-me", "default", "secret"}
            if (
                not self.CURSOR_SIGNING_SECRET
                or self.CURSOR_SIGNING_SECRET in insecure_cursor_secrets
                or len(self.CURSOR_SIGNING_SECRET) < 32
            ):
                raise ValueError(
                    "Production deployment requires an explicit, high-entropy CURSOR_SIGNING_SECRET "
                    "(at least 32 characters) for tamper-proof activity cursor signatures."
                )

            # Require explicit, high-entropy action signing secrets in production
            insecure_action_secrets = {
                "",
                "default-studiotower-action-signing-secret-v2-32chars",
                "default-studiotower-action-signing-secret-v1-32chars",
                "change-me",
                "default",
                "secret",
            }
            if (
                not self.ACTION_SIGNING_SECRET
                or self.ACTION_SIGNING_SECRET in insecure_action_secrets
                or len(self.ACTION_SIGNING_SECRET) < 32
            ):
                raise ValueError(
                    "Production deployment requires an explicit, high-entropy ACTION_SIGNING_SECRET "
                    "(at least 32 characters) for 256-bit Action Proposal token signatures."
                )
            if self.ACTION_SIGNING_SECRET_PREV is not None:
                if (
                    self.ACTION_SIGNING_SECRET_PREV in insecure_action_secrets
                    or len(self.ACTION_SIGNING_SECRET_PREV) < 32
                ):
                    raise ValueError(
                        "When configured, ACTION_SIGNING_SECRET_PREV must be an explicit, high-entropy secret "
                        "(at least 32 characters) for dual-key rotation."
                    )
            if self.ACTION_RUNNER != "cloud_tasks":
                raise ValueError(
                    "Production deployment requires a durable queue for action execution (ACTION_RUNNER=cloud_tasks)."
                )

            # Authoritative Worker Service URL validation in production (fail closed, no synthetic URL guessing)
            if (
                not self.STUDIO_TOWER_WORKER_SERVICE_URL
                or self.STUDIO_TOWER_WORKER_SERVICE_URL == "http://localhost:8000"
                or not self.STUDIO_TOWER_WORKER_SERVICE_URL.startswith("https://")
            ):
                raise ValueError(
                    "Production deployment requires an explicit, authoritative HTTPS worker service URL (STUDIO_TOWER_WORKER_SERVICE_URL)."
                )

            from urllib.parse import urlsplit
            try:
                parsed_worker = urlsplit(self.STUDIO_TOWER_WORKER_SERVICE_URL)
                scheme = parsed_worker.scheme
                username = parsed_worker.username
                password = parsed_worker.password
                port = parsed_worker.port
                query = parsed_worker.query
                fragment = parsed_worker.fragment
                hostname = parsed_worker.hostname
            except Exception as e:
                raise ValueError(f"Invalid STUDIO_TOWER_WORKER_SERVICE_URL: {e}")

            if scheme != "https" or not hostname:
                raise ValueError("STUDIO_TOWER_WORKER_SERVICE_URL must be a valid https:// URL with a valid hostname.")
            if username or password:
                raise ValueError("STUDIO_TOWER_WORKER_SERVICE_URL must not contain userinfo/credentials.")
            if query or fragment:
                raise ValueError("STUDIO_TOWER_WORKER_SERVICE_URL must not contain query parameters or fragments.")
            if port and port != 443:
                raise ValueError(f"STUDIO_TOWER_WORKER_SERVICE_URL port must be 443 or default HTTPS port; got {port}.")

            # Authoritative Worker Host validation in production (fail closed, no empty allowlist permitted)
            if not self.STUDIO_TOWER_ALLOWED_WORKER_HOST or not self.STUDIO_TOWER_ALLOWED_WORKER_HOST.strip():
                raise ValueError(
                    "Production deployment requires an explicit STUDIO_TOWER_ALLOWED_WORKER_HOST for authoritative worker host validation."
                )
            expected_worker_host = self.STUDIO_TOWER_ALLOWED_WORKER_HOST.strip().lower()
            if hostname.lower() != expected_worker_host:
                raise ValueError(
                    f"STUDIO_TOWER_WORKER_SERVICE_URL hostname '{hostname}' does not match authorized STUDIO_TOWER_ALLOWED_WORKER_HOST '{expected_worker_host}'."
                )

            # Explicit Cloud Scheduler Service Account validation in production
            if not self.STUDIO_TOWER_SCHEDULER_SA:
                raise ValueError(
                    "Production deployment requires an explicit STUDIO_TOWER_SCHEDULER_SA for Cloud Scheduler OIDC authentication."
                )
            sa = self.STUDIO_TOWER_SCHEDULER_SA.strip()
            if "@" not in sa or not (sa.endswith(".gserviceaccount.com") or sa.endswith(".iam.gserviceaccount.com")):
                raise ValueError(
                    f"STUDIO_TOWER_SCHEDULER_SA must be a valid Google service account email; got '{self.STUDIO_TOWER_SCHEDULER_SA}'."
                )

            # Cloud Scheduler Audience validation (defaults to worker service URL if not explicitly set)
            if not self.STUDIO_TOWER_SCHEDULER_AUDIENCE:
                self.STUDIO_TOWER_SCHEDULER_AUDIENCE = self.STUDIO_TOWER_WORKER_SERVICE_URL
            elif not self.STUDIO_TOWER_SCHEDULER_AUDIENCE.startswith("https://"):
                raise ValueError("STUDIO_TOWER_SCHEDULER_AUDIENCE must start with https://.")

        if self.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs" and not self.STUDIO_TOWER_GCS_BUCKET:
            raise ValueError(
                "STUDIO_TOWER_GCS_BUCKET is required when STUDIO_TOWER_ARTIFACT_BACKEND=gcs."
            )

        # Grafana configuration validation in production/staging
        if normalized_env in ("production", "staging") and self.GRAFANA_BASE_URL:
            from urllib.parse import urlsplit
            try:
                parsed = urlsplit(self.GRAFANA_BASE_URL)
                scheme = parsed.scheme
                username = parsed.username
                password = parsed.password
                port = parsed.port
                query = parsed.query
                fragment = parsed.fragment
                hostname = parsed.hostname
            except (ValueError, Exception) as e:
                raise ValueError(f"Invalid GRAFANA_BASE_URL (malformed URL or invalid port/host): {e}")

            if scheme != "https":
                raise ValueError("GRAFANA_BASE_URL must use https:// protocol in production/staging.")
            if username or password:
                raise ValueError("GRAFANA_BASE_URL must not contain userinfo/credentials.")
            if port and port != 443:
                raise ValueError(f"GRAFANA_BASE_URL port must be 443 or default HTTPS port; got {port}.")
            if query or fragment:
                raise ValueError("GRAFANA_BASE_URL must not contain query parameters or fragments.")
            if not hostname:
                raise ValueError("GRAFANA_BASE_URL must contain a valid hostname.")
            if not self.GRAFANA_ALLOWED_HOSTS:
                raise ValueError("GRAFANA_ALLOWED_HOSTS must be explicitly configured when GRAFANA_BASE_URL is set.")
            allowed_normalized = [h.strip().lower() for h in self.GRAFANA_ALLOWED_HOSTS if h.strip()]
            if hostname.lower() not in allowed_normalized:
                raise ValueError(
                    f"GRAFANA_BASE_URL hostname '{hostname}' is not in GRAFANA_ALLOWED_HOSTS: {allowed_normalized}"
                )

        return self


settings = Settings()
