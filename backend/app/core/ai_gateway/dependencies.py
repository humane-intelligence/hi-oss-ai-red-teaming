"""Filter route dependencies for the ai-gateway module.

Cross-cutting aliases live in `app/core/dependencies.py`; module-specific
filter deps live here (sibling pattern: `app/core/auth/dependencies.py`).
"""

from typing import Annotated

from fastapi import Depends

from app.core.ai_gateway.filters import AiModelFilters

AiModelFiltersDep = Annotated[AiModelFilters, Depends()]
