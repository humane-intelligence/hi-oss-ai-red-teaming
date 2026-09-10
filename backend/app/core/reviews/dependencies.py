"""Review-specific FastAPI dependency aliases.

Cross-cutting aliases (DB session, pagination, current user) live in
`app/core/dependencies.py`; resource-specific ones live here, per the API skill.
"""

from typing import Annotated

from fastapi import Depends

from app.core.reviews.filters import ReviewFilters
from app.core.reviews.filters import ReviewQueueFilters

ReviewFiltersDep = Annotated[ReviewFilters, Depends()]
ReviewQueueFiltersDep = Annotated[ReviewQueueFilters, Depends()]
