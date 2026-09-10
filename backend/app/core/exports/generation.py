"""Shared export assembly — the one source of truth for what rows a scope yields.

The background export task drives its output off these helpers: a group's visible evaluations
are enumerated by the `list_evaluations` scope (`resolve_group_evaluations`), and the rows are
assembled by the format-aware `stream_export` loop (CSV = header-once-then-stream-per-evaluation;
JSON = a streamed array) that feeds `storage.save`.
"""

from collections.abc import AsyncIterator
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.schemas import SessionUser
from app.core.config import get_settings
from app.core.csv_generator import csv_header
from app.core.csv_generator import format_csv_row
from app.core.evaluations.filters import EvaluationFilters
from app.core.evaluations.models import Evaluation
from app.core.evaluations.services.evaluations import list_evaluations
from app.core.evaluations.services.evaluations import resolve_effective_licenses
from app.core.exports.base import CsvExport
from app.core.exports.base import ExportScope
from app.core.exports.base import drain_pages
from app.core.exports.enums import ExportFormat
from app.core.exports.filters import ExportFilters
from app.core.json_generator import json_row
from app.core.restore import restore_cutoff

# UTF-8 byte-order mark, prepended to the stream so Excel detects the encoding and renders
# non-ASCII transcript text correctly (see the csv_generator docstring). One place, so both
# storage backends (and the download) inherit it.
_UTF8_BOM = "﻿"


async def resolve_group_evaluations(
    session: AsyncSession, caller: SessionUser, group_id: UUID, *, can_manage: bool
) -> list[Evaluation]:
    """Drain the caller-visible evaluations of a group (org-scoping included).

    The security-critical enumeration: visibility (incl. organization scoping) is
    enforced by `list_evaluations`, so evaluations the caller can't see are
    omitted — no widening. Shared with the background task so a detached job re-runs
    exactly this scope.
    """
    return await drain_pages(
        lambda limit, offset: list_evaluations(
            session,
            caller_id=caller.id,
            can_manage=can_manage,
            filters=EvaluationFilters(evaluation_group_id=group_id),
            order_by="created_at",
            deleted_cutoff=restore_cutoff(get_settings()),
            limit=limit,
            offset=offset,
        )
    )


async def _iter_export_rows(
    session: AsyncSession,
    export: CsvExport[Any],
    caller: SessionUser,
    evaluations: Sequence[Evaluation],
    filters: ExportFilters,
) -> AsyncIterator[Any]:
    """Yield every export row across the evaluations — the format-neutral fetch spine.

    Fully streaming: `export.fetch` is an async generator yielding rows one page at a time, so
    peak memory is ~one page (not one evaluation's, let alone the whole group's). ``filters`` are
    carried onto each `ExportScope` so the template narrows its own query.
    """
    # Batch-resolve the effective data license once for the whole group (one platform-default read
    # + one `IN` query) when the template emits it — instead of each per-evaluation fetch issuing
    # its own lookup (an N+1 across the group). Skipped for templates that don't carry a license.
    licenses = (
        await resolve_effective_licenses(session, [evaluation.id for evaluation in evaluations])
        if export.needs_effective_license
        else {}
    )
    for evaluation in evaluations:
        # Reaching here means the caller was authorised as the group's owner/admin (endpoint +
        # worker re-check), so the export is a whole-group extract: `full_group_access=True`.
        scope = ExportScope(
            evaluation_id=evaluation.id,
            caller=caller,
            evaluation=evaluation,
            full_group_access=True,
            effective_license=licenses.get(evaluation.id),
            filters=filters,
        )
        async for row in export.fetch(session, scope):
            yield row


async def stream_export(
    session: AsyncSession,
    export: CsvExport[Any],
    caller: SessionUser,
    evaluations: Sequence[Evaluation],
    *,
    export_format: ExportFormat,
    export_filters: ExportFilters | None = None,
) -> AsyncIterator[str]:
    """Yield the export body in the requested format, rendering each row as it's fetched.

    CSV: a UTF-8 BOM + header line, then one line per row. JSON: a single array (`[`, comma-joined
    row-objects, `]`). Both reuse `export.columns` + `export.fetch` unchanged — JSON via
    `Column.value` (raw), CSV via `Column.formatter`/`format_cell`. ``export_filters`` narrow the
    rows (each template applies the dimensions it supports). Callers pass a single
    already-visibility-checked evaluation (per-evaluation export) or the drained group set from
    `resolve_group_evaluations` (group export).
    """
    filters = export_filters or ExportFilters()
    if export_format is ExportFormat.JSON:
        # Same array framing as json_generator.iter_json (canonical, unit-tested), re-expressed here
        # because this path is async over a multi-evaluation paginated fetch. The `first` flag spans
        # ALL evaluations/pages — a single array, not one per evaluation; test_group_*_json locks that.
        yield "["
        first = True
        async for row in _iter_export_rows(session, export, caller, evaluations, filters):
            yield json_row(row, export.columns) if first else "," + json_row(row, export.columns)
            first = False
        yield "]"
    else:
        yield _UTF8_BOM + csv_header(export.columns)
        async for row in _iter_export_rows(session, export, caller, evaluations, filters):
            yield format_csv_row(row, export.columns)
