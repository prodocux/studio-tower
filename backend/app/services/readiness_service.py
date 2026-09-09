import logging
import threading
import time
from typing import Any, Dict

from app.agent.brain import AgentBrain
from app.core.config import settings
from app.services.file_service import check_artifact_storage_readiness
from app.services.storage import check_storage_readiness

logger = logging.getLogger(__name__)


class ReadinessService:
    _lock = threading.Lock()
    _last_probe_time: float = 0.0
    _ttl_seconds: float = 60.0
    _cached_result: Dict[str, Any] = {}

    @classmethod
    def get_readiness(cls, force: bool = False) -> Dict[str, Any]:
        """
        Evaluate full system readiness with single-flight locking and 60-second TTL caching.
        Prevents external quota exhaustion / DoS against Firestore, GCS, and Gemini.
        """
        now = time.time()
        # Fast path: return cached snapshot if fresh
        if not force and (now - cls._last_probe_time < cls._ttl_seconds) and cls._cached_result:
            return dict(cls._cached_result)

        # Single-flight lock
        with cls._lock:
            now = time.time()
            if not force and (now - cls._last_probe_time < cls._ttl_seconds) and cls._cached_result:
                return dict(cls._cached_result)

            storage_status = check_storage_readiness()
            artifact_status = check_artifact_storage_readiness()
            ai_status = AgentBrain.check_readiness(force_probe=force)

            is_storage_ok = storage_status.get("status") == "ok"
            is_artifact_ok = artifact_status.get("status") == "ok"
            is_ai_ok = ai_status.get("status") == "ok"

            is_healthy = is_storage_ok and is_artifact_ok and is_ai_ok
            overall_status = "ok" if is_healthy else "degraded"

            cls._cached_result = {
                "status": overall_status,
                "is_healthy": is_healthy,
                "app": settings.APP_NAME,
                "version": settings.APP_VERSION,
                "auth_mode": settings.STUDIO_TOWER_AUTH_MODE,
                "store": settings.STUDIO_TOWER_STORE,
                "components": {
                    "database": storage_status,
                    "artifacts": artifact_status,
                    "ai": {
                        "status": ai_status.get("status", "unknown"),
                        "mode": ai_status.get("ai_mode", "deterministic_fallback"),
                        "model": ai_status.get("ai_model", settings.GEMINI_MODEL),
                        "ready": ai_status.get("ai_ready", False),
                        "fallback_allowed": settings.AI_FALLBACK_ALLOWED,
                        "last_probe_time": ai_status.get("last_probe_time", 0.0),
                        "last_latency_ms": ai_status.get("last_latency_ms", 0),
                        "last_error_code": ai_status.get("last_error_code"),
                        "last_error_message": ai_status.get("last_error_message"),
                    },
                },
                "ai_model": ai_status.get("ai_model", settings.GEMINI_MODEL),
                "ai_mode": ai_status.get("ai_mode", "deterministic_fallback"),
                "ai_ready": ai_status.get("ai_ready", False),
                "ai_fallback_allowed": settings.AI_FALLBACK_ALLOWED,
                "fallback_allowed": settings.AI_FALLBACK_ALLOWED,
                "last_probe_time": now,
                "last_latency_ms": ai_status.get("last_latency_ms", 0),
                "last_error_code": ai_status.get("last_error_code"),
            }

            if ai_status.get("last_error_message"):
                cls._cached_result["last_error_message"] = ai_status["last_error_message"]

            cls._last_probe_time = now
            return dict(cls._cached_result)

    @classmethod
    def mark_component_degraded(
        cls,
        component: str,
        error_code: Any = None,
        error_message: str = "",
    ) -> None:
        """
        Immediately marks a specific component as degraded in the cached snapshot.
        Ensures subsequent /readyz calls return HTTP 503 instantly without waiting for TTL expiration.
        """
        with cls._lock:
            now = time.time()
            if not cls._cached_result:
                cls._cached_result = {
                    "app": settings.APP_NAME,
                    "version": settings.APP_VERSION,
                    "auth_mode": settings.STUDIO_TOWER_AUTH_MODE,
                    "store": settings.STUDIO_TOWER_STORE,
                    "components": {},
                }

            cls._cached_result["status"] = "degraded"
            cls._cached_result["is_healthy"] = False
            cls._cached_result["fallback_allowed"] = settings.AI_FALLBACK_ALLOWED
            cls._cached_result["ai_fallback_allowed"] = settings.AI_FALLBACK_ALLOWED
            cls._cached_result["last_probe_time"] = now

            if "components" not in cls._cached_result:
                cls._cached_result["components"] = {}

            if component == "ai":
                cls._cached_result["components"]["ai"] = {
                    "status": "degraded",
                    "mode": "live",
                    "model": settings.GEMINI_MODEL,
                    "ready": False,
                    "fallback_allowed": settings.AI_FALLBACK_ALLOWED,
                    "last_probe_time": now,
                    "last_latency_ms": 0,
                    "last_error_code": error_code,
                    "last_error_message": error_message,
                }
                cls._cached_result["ai_ready"] = False
                cls._cached_result["last_error_code"] = error_code
                if error_message:
                    cls._cached_result["last_error_message"] = error_message
            else:
                cls._cached_result["components"][component] = {
                    "status": "degraded",
                    "error": error_message or "Component failure",
                }

            cls._last_probe_time = now

    @classmethod
    def invalidate_cache(cls) -> None:
        """Force cache invalidation."""
        with cls._lock:
            cls._last_probe_time = 0.0
            cls._cached_result = {}
