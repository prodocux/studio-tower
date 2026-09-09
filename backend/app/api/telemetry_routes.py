from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.integrations.grafana_visual import GrafanaVisualBoard
from app.models.telemetry import RunTraceResponse, SpanWaterfallNode, TelemetryMetricsSummary
from app.models.user import User
from app.services.space_service import SpaceService
from app.services.telemetry_service import TelemetryService, TelemetryVerificationResult

router = APIRouter(prefix="/v1", tags=["telemetry"])


class VerifyTelemetryRequest(BaseModel):
    force: bool = Field(default=False, description="Force verification bypassing throttle window")


class ReconcileTelemetryRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


@router.post("/spaces/{space_id}/runs/{run_id}/verify-telemetry", response_model=TelemetryVerificationResult)
def verify_run_telemetry_endpoint(
    space_id: str,
    run_id: str,
    payload: Optional[VerifyTelemetryRequest] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Verify and advance telemetry status for a specific Run.
    Enforces tenant access control, rate-limiting, and CAS version fencing.
    """
    SpaceService.get_space_with_auth(space_id, current_user)
    force = payload.force if payload else False
    try:
        result = TelemetryService.verify_run_telemetry(space_id=space_id, run_id=run_id, force=force)
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        import uuid
        import logging
        corr_id = f"corr_{uuid.uuid4().hex[:12]}"
        logging.getLogger("studiotower.telemetry_routes").error(
            "Telemetry verification unexpected error (correlation_id=%s): %s",
            corr_id,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"TELEMETRY_VERIFICATION_ERROR: An internal error occurred (correlation_id={corr_id})",
        )


@router.post("/spaces/{space_id}/telemetry/reconcile")
def reconcile_space_telemetry_endpoint(
    space_id: str,
    payload: Optional[ReconcileTelemetryRequest] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Scan and reconcile pending runs ('exporting' and 'delayed') for the space.
    """
    SpaceService.get_space_with_auth(space_id, current_user)
    limit = payload.limit if payload else 50
    stats = TelemetryService.reconcile_pending_telemetry_runs(space_id=space_id, limit=limit)
    return {
        "status": "success",
        "space_id": space_id,
        "reconciliation": stats,
    }


@router.get("/spaces/{space_id}/runs/{run_id}/trace", response_model=RunTraceResponse)
def get_run_trace_endpoint(
    space_id: str,
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Query-layer multi-tenant trace proxy (E3 Requirement 1).
    Fails closed if the trace violates tenant boundary or Run identity contract.
    Returns sanitized span waterfall hierarchy with relative offsets and allowed attributes only.
    """
    SpaceService.get_space_with_auth(space_id, current_user)
    try:
        bundle = TelemetryService.get_run_trace_bundle(space_id=space_id, run_id=run_id)
        if not bundle or not bundle.spans:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trace for run {run_id} not found or unavailable",
            )
        return bundle
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        import uuid
        import logging
        corr_id = f"corr_{uuid.uuid4().hex[:12]}"
        logging.getLogger("studiotower.telemetry_routes").error(
            "Trace query proxy error (correlation_id=%s): %s",
            corr_id,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"TRACE_QUERY_ERROR: An internal error occurred (correlation_id={corr_id})",
        )


@router.get("/spaces/{space_id}/telemetry/metrics", response_model=TelemetryMetricsSummary)
def get_space_metrics_endpoint(
    space_id: str,
    time_window_hours: int = Query(24, description="Time window in hours (1 to 168)"),
    project_tag: Optional[str] = Query(None, description="Filter by project tag"),
    current_user: User = Depends(get_current_user),
):
    """
    Low-cardinality pre-aggregated telemetry metrics rollup (E3 Requirement 2).
    Enforces time boundaries (1 <= time_window_hours <= 168) and anti-abuse protection.
    Reads pre-aggregated hourly buckets rather than scanning full Run collections.
    """
    SpaceService.get_space_with_auth(space_id, current_user)
    if time_window_hours < 1 or time_window_hours > 168:
        raise HTTPException(
            status_code=422,
            detail="time_window_hours must be between 1 and 168 (up to 7 days)",
        )
    try:
        summary = TelemetryService.get_space_metrics_summary(
            space_id=space_id,
            time_window_hours=time_window_hours,
            project_tag=project_tag,
        )
        return summary
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        import uuid
        import logging
        corr_id = f"corr_{uuid.uuid4().hex[:12]}"
        logging.getLogger("studiotower.telemetry_routes").error(
            "Metrics query error (correlation_id=%s): %s",
            corr_id,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"METRICS_QUERY_ERROR: An internal error occurred (correlation_id={corr_id})",
        )


@router.get("/spaces/{space_id}/grafana/visual", response_model=GrafanaVisualBoard)
def get_grafana_visual_board(
    space_id: str,
    current_user: User = Depends(get_current_user),
):
    """Live Grafana Cloud dashboard panels for the Telemetry tab. Fail-closed, never invents charts."""
    SpaceService.get_space_with_auth(space_id, current_user)
    from app.integrations.grafana_visual import load_grafana_visual_board

    return load_grafana_visual_board(space_id=space_id)

