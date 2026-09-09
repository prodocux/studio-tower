from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.auth import get_current_user
from app.models.space import MembershipRole
from app.models.user import User
from app.services.space_service import SpaceService
from app.services.storage import store

router = APIRouter(prefix="/v1/spaces/{space_id}/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}/download")
def download_artifact(
    space_id: str,
    artifact_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Authenticated deliverable download endpoint with space tenancy and approval gate visibility enforcement.
    """
    # 1. Tenancy & Membership Verification
    SpaceService.get_space_with_auth(space_id, current_user)

    # 2. Artifact Tenancy Verification
    artifact = store.get_artifact(space_id, artifact_id)
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact '{artifact_id}' not found in space '{space_id}'",
        )

    # 3. Approval Gate Visibility Policy
    if artifact.visibility == "pending_approval":
        # Only owners, admins, and coordinators can preview/download unapproved critical deliverables
        context = SpaceService.get_space_context(space_id, current_user)
        if not context.capabilities.can_approve_runs:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ARTIFACT_AWAITING_APPROVAL: Deliverable is pending coordinator approval.",
            )
    elif artifact.visibility == "rejected":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="ARTIFACT_REJECTED: Deliverable was rejected during review.",
        )

    # 4. Stream binary data
    blob = store.get_artifact_blob(space_id, artifact_id, artifact.filename)
    if blob is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact file payload not found in storage backend",
        )

    import re
    import urllib.parse

    ascii_filename = re.sub(r"[^\x20-\x7E]", "_", artifact.filename) or "artifact"
    encoded_filename = urllib.parse.quote(artifact.filename)

    return Response(
        content=blob,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{encoded_filename}',
            "Content-Length": str(len(blob)),
            "X-Artifact-Sha256": artifact.sha256,
            "X-Artifact-Id": artifact.artifact_id,
        },
    )
