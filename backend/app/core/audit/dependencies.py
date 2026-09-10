"""DI aliases for the audit-log endpoint."""

from typing import Annotated

from fastapi import Depends

from app.core.audit.filters import AuditLogFilters

AuditLogFiltersDep = Annotated[AuditLogFilters, Depends()]
