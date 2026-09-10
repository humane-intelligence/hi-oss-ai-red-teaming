"""Saved-views FastAPI dependency aliases.

Cross-cutting aliases (DB session, pagination, current user) live in
`app/core/dependencies.py`; resource-specific ones live here, per the API skill.
"""

from typing import Annotated

from fastapi import Depends

from app.core.saved_views.filters import SavedViewFilters

SavedViewFiltersDep = Annotated[SavedViewFilters, Depends()]
