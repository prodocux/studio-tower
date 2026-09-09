from datetime import UTC, datetime

from pydantic import BaseModel, Field


class User(BaseModel):
    uid: str = Field(..., description="Unique Firebase User ID")
    email: str = Field(..., description="Verified user email")
    display_name: str = Field(default="", description="Display name")
    avatar_url: str = Field(default="", description="Avatar image URL")
    can_view_telemetry: bool = Field(default=True, description="Permission to view telemetry traces")
    can_diagnose_runs: bool = Field(default=True, description="Permission to invoke AI failure diagnosis")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
