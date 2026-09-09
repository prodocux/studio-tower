from typing import List, Optional

from app.core.auth import get_current_user
from app.models.activity import ActivityEvent, InvalidCursorError
from app.models.user import User
from app.services.activity_service import ActivityService
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

router = APIRouter(prefix="/v1", tags=["activity"])


class ActivityListResponse(BaseModel):
    items: List[ActivityEvent]
    next_cursor: Optional[str] = None


@router.get("/spaces/{space_id}/activity", response_model=ActivityListResponse)
def list_space_activity(
    space_id: str,
    tag: Optional[str] = Query(default=None, description="Optional project tag filter (or 'all' for space-wide overview)"),
    cursor: Optional[str] = Query(default=None, description="Pagination cursor"),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    """
    List chronological activity events (files, tags, messages, runs, gates)
    for a Space with stable cursor-based pagination.
    """
    try:
        items, next_cursor = ActivityService.list_activities(
            space_id=space_id,
            user=current_user,
            tag=tag,
            cursor=cursor,
            limit=limit,
        )
        return ActivityListResponse(items=items, next_cursor=next_cursor)
    except InvalidCursorError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination cursor",
        ) from e
