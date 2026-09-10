"""Notifications FastAPI dependency aliases.

Cross-cutting aliases (DB session, pagination, current user) live in
`app/core/dependencies.py`; resource-specific ones live here, per the API skill.
"""

from typing import Annotated

from fastapi import Depends

from app.core.notifications.filters import NotificationFilters

NotificationFiltersDep = Annotated[NotificationFilters, Depends()]
