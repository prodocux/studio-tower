from app.core.auth import get_current_user
from app.models.user import User
from app.services.storage import store
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/v1", tags=["users"])


@router.get("/users/search", response_model=list[User])
def search_users(
    q: str = Query(..., min_length=1, max_length=100, description="Search by email, display name, or UID"),
    current_user: User = Depends(get_current_user),
):
    """Search registered users on the site by email, display name, or UID."""
    return store.search_users(query=q, limit=10)
