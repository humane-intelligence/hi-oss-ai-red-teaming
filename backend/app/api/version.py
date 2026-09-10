"""Deployed-version probe — top-level, unversioned, unauthenticated."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.dependencies import SettingsDep
from app.core.openapi import display_version

router = APIRouter(tags=["health"])


class VersionResponse(BaseModel):
    """Deployed build identity — commit SHA plus the runtime environment."""

    version: str
    environment: str


@router.get("/version")
def version(settings: SettingsDep) -> VersionResponse:
    """Deployed commit SHA and environment.

    Public (no auth) — backs the frontend version readout, including the
    pre-login page.
    """
    return VersionResponse(
        version=display_version(settings.git_sha),
        environment=settings.environment,
    )
