"""Organization-scoped route dependencies — `Annotated` aliases.

Cross-cutting wrappers live in `app/core/dependencies.py`.
"""

from typing import Annotated

from fastapi import Depends

from app.core.organizations.filters import OrganizationFilters

OrganizationFiltersDep = Annotated[OrganizationFilters, Depends()]
