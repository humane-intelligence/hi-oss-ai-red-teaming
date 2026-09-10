"""The CSV export registry — aggregates the per-template definitions into one pick-list.

Static and code-shipped, mirroring `app/core/licenses/catalog.py`. To add an export: write a
template module under `app/core/exports/` (its columns + a scoped fetcher, see [flags.py]) and
list its `CsvExport` in `_CATALOG` below. The endpoint, the generic generator, and the picker
stay untouched.
"""

from typing import Any

from app.core.exports.base import CsvExport
from app.core.exports.templates.conversation_groups import CONVERSATION_GROUPS_EXPORT
from app.core.exports.templates.conversations import CONVERSATIONS_EXPORT
from app.core.exports.templates.engagement_report import ENGAGEMENT_REPORT_EXPORT
from app.core.exports.templates.flags import FLAGS_EXPORT
from app.core.exports.templates.reviews import REVIEWS_EXPORT
from app.core.exports.templates.transcript import TRANSCRIPT_EXPORT

_CATALOG: tuple[CsvExport[Any], ...] = (
    FLAGS_EXPORT,
    CONVERSATIONS_EXPORT,
    CONVERSATION_GROUPS_EXPORT,
    ENGAGEMENT_REPORT_EXPORT,
    REVIEWS_EXPORT,
    TRANSCRIPT_EXPORT,
)
_BY_KEY: dict[str, CsvExport[Any]] = {export.key: export for export in _CATALOG}


def list_exports() -> list[CsvExport[Any]]:
    """Return the export catalog sorted by `key` for a stable picker order."""
    return sorted(_CATALOG, key=lambda export: export.key)


def get_export(key: str) -> CsvExport[Any] | None:
    """Return the export for `key`, or `None` if it isn't in the catalog."""
    return _BY_KEY.get(key)
