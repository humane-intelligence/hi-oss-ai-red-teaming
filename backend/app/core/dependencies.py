"""Cross-cutting `Annotated[X, Depends(...)]` aliases for FastAPI dependency injection.

Only aliases that apply to multiple bounded contexts (DB session, settings,
pagination, current user) live here. Resource-specific aliases — e.g.
filter deps for a single domain — live in that module's own
`dependencies.py` (see `app/core/auth/dependencies.py`).
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dependencies import current_user
from app.core.auth.dependencies import optional_current_user
from app.core.auth.schemas import SessionUser
from app.core.config import Settings
from app.core.config import get_settings
from app.core.database import get_db
from app.core.database import transactional
from app.core.schemas import PaginationParams

SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentUserDep = Annotated[SessionUser, Depends(current_user)]
OptionalUserDep = Annotated[SessionUser | None, Depends(optional_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
PaginationDep = Annotated[PaginationParams, Depends()]

__all__ = [
    "CurrentUserDep",
    "DbSession",
    "OptionalUserDep",
    "PaginationDep",
    "SettingsDep",
    "transactional",
]
