from contextlib import asynccontextmanager

from app.api.activity_routes import router as activity_router
from app.api.artifact_routes import router as artifact_router
from app.api.auth_routes import router as auth_router
from app.api.chat_routes import router as chat_router
from app.api.file_routes import router as file_router
from app.api.maintenance_routes import router as maintenance_router
from app.api.telemetry_routes import router as telemetry_router
from app.api.run_routes import router as run_router
from app.api.space_routes import router as space_router
from app.api.user_routes import router as user_router
from app.core.config import settings
import os
from app.core.otel import initialize_otel, shutdown_otel
from app.core.telemetry_tenant import initialize_telemetry_tenant_keys
from app.services.ingestion_runner import shutdown_ingestion_runner
from app.services.readiness_service import ReadinessService
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    initialize_telemetry_tenant_keys(environment=settings.ENV)
    from app.integrations.grafana_otlp import resolve_grafana_otlp

    otlp = resolve_grafana_otlp()
    initialize_otel(
        service_name="studiotower-api",
        otlp_endpoint=(otlp.endpoint if otlp else os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")),
        otlp_headers=(otlp.headers if otlp else None),
        environment=settings.ENV,
    )
    from app.integrations.grafana_mcp import grafana_mcp
    grafana_mcp.warmup()
    yield
    # Shutdown
    shutdown_otel(timeout_millis=2000)
    shutdown_ingestion_runner()
    from app.services.diagnosis_service import DiagnosisService
    DiagnosisService.shutdown_executor()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Observable Production Control for Film Prep API",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router)
app.include_router(space_router)
app.include_router(file_router)
app.include_router(chat_router)
app.include_router(run_router)
app.include_router(user_router)
app.include_router(activity_router)
app.include_router(artifact_router)
app.include_router(maintenance_router)
app.include_router(telemetry_router)


@app.get("/healthz")
def liveness():
    """Liveness probe: verifies process is responding."""
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


@app.get("/readyz")
@app.get("/healthz/ready")
@app.get("/v1/readyz")
@app.get("/v1/healthz")
def readiness(response: Response):
    """
    Readiness probe: validates database storage connectivity, artifact volume access, and AI model readiness.
    Uses unified 60s TTL caching and single-flight lock across all components to prevent API cost/quota DoS.
    Returns HTTP 503 if any required component is degraded/unhealthy.
    """
    result = ReadinessService.get_readiness(force=False)
    if not result.get("is_healthy", False):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return result


