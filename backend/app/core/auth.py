import logging

from app.core.config import settings
from app.models.user import User
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> User:
    """
    Authenticate incoming request based on configured auth mode (dev or firebase).
    Fails closed with 401 if unauthenticated or token is invalid.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()

    if settings.STUDIO_TOWER_AUTH_MODE == "dev":
        if settings.ENV == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Dev authentication mode is disabled in production",
            )
        # Dev format: dev:<uid>:<email> or dev:<uid>:<email>:<display_name>
        if not token.startswith("dev:"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid dev token format. Expected 'dev:<uid>:<email>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        parts = token.split(":")
        if len(parts) < 3:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Malformed dev token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        uid = parts[1]
        email = parts[2]
        display_name = parts[3] if len(parts) > 3 else email.split("@")[0].capitalize()
        from app.services.storage import store
        existing = store.get_user(uid)
        if existing:
            return existing
        user = User(uid=uid, email=email, display_name=display_name)
        store.save_user(user)
        return user

    elif settings.STUDIO_TOWER_AUTH_MODE == "firebase":
        try:
            import firebase_admin
            from firebase_admin import auth as firebase_auth

            # Initialize firebase app if not already initialized
            if not firebase_admin._apps:
                options = {}
                if settings.STUDIO_TOWER_FIREBASE_PROJECT_ID:
                    options["projectId"] = settings.STUDIO_TOWER_FIREBASE_PROJECT_ID
                firebase_admin.initialize_app(options=options if options else None)

            decoded_token = firebase_auth.verify_id_token(token, check_revoked=False)
            uid = decoded_token.get("uid")
            email = decoded_token.get("email", "")
            display_name = decoded_token.get("name", "")
            picture = decoded_token.get("picture", "")

            if not uid or not email:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Firebase token missing required uid or email claims",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            user = User(
                uid=uid,
                email=email,
                display_name=display_name or email.split("@")[0],
                avatar_url=picture or "",
            )
            from app.services.storage import store
            store.save_user(user)
            return user
        except Exception as e:
            logger.warning(f"Firebase token verification failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unsupported authentication mode configured",
    )
