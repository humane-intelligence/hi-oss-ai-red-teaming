"""Shared machinery for the CSV export catalog: the types and the pagination drainer.

The concrete export templates build on these. No concrete templates and no registry live
here, so a template module (`templates/flags.py`, …) can import this without importing the
catalog (which imports the templates) — the dependency flow is one-way (`template → base`,
`catalog → template`) and cycle-free.
"""

from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.csv_generator import Column
from app.core.evaluations.models import Evaluation
from app.core.exports.filters import ExportFilters
from app.core.licenses.models import DataLicense


@dataclass(frozen=True, slots=True)
class ExportScope:
    """Which evaluation is being exported, the caller doing it, and whether the read is group-wide.

    `full_group_access` lets a fetcher read the **whole** evaluation's rows — every red-teamer's
    conversations / flags / reviews — not just the caller's own. It is set True in
    `_iter_export_rows` (generation.py), which runs only after the endpoint/worker authorised the
    caller as the group's `owner` (via the object-role layer) or a break-glass `evaluation_groups:manage` admin.
    So every authorised export is a whole-group extract — the `engagement_report` deliverable and
    the "export a whole evaluation group" requirement — regardless of whether the caller happens to hold
    the *global* `evaluation_groups:manage` permission. The row-level scope is uniform across all
    templates (flags/conversations/conversation-groups/transcript/engagement_report AND reviews),
    so they never diverge. It defaults False (**fail-safe**: a scope built without stated authority
    reads self-only), so group-wide authority is granted explicitly, never assumed.

    `caller` is carried whole (not just its id) so fetchers can resolve labels/emails.

    `evaluation` is the already-visibility-checked evaluation row when the caller has it
    (both export endpoints load it to enforce visibility, and the group export loads each
    one to drive the loop); a fetcher that needs the evaluation reuses it instead of
    re-issuing `get_evaluation` per row.

    `effective_license` is the evaluation's resolved data license (evaluation override → group
    override → platform default), batch-resolved once for the whole group by `_iter_export_rows`
    and threaded in — so a per-evaluation fetcher reads it off the scope instead of issuing its own
    license lookup per
    evaluation (an N+1 across a group). `None` means "not pre-resolved" (a scope built directly,
    e.g. in a test), and a fetcher that needs it falls back to the single-id resolver.

    `filters` are the caller-chosen row filters (`ExportFilters`), threaded by `stream_export` from
    the job. Each fetcher maps them onto its domain `*Filters` where the dimension exists, ignoring
    the rest (a flags-only `task_id` is a no-op for the reviews export). Defaults to no constraints.
    """

    evaluation_id: UUID
    caller: SessionUser
    evaluation: Evaluation | None = None
    full_group_access: bool = False
    effective_license: DataLicense | None = None
    filters: ExportFilters = field(default_factory=ExportFilters)


@dataclass(frozen=True, slots=True)
class CsvExport[T]:
    """One named evaluation export template — picker metadata, column spec, and row fetcher.

    Renders to CSV or JSON (name retained for continuity; `stream_export` drives both formats off
    the same `columns` + `fetch`). `permission` is the data-read permission this export corresponds
    to — informational picker metadata; running an export is gated on owner/admin group authority,
    not this.
    """

    key: str
    name: str
    description: str
    permission: Permission
    columns: Sequence[Column[T]]
    # An async generator: `fetch(session, scope)` yields rows one at a time so the export
    # streams end-to-end (peak memory ~one page of rows, not the whole result set). Templates
    # build theirs off `iter_pages`; the consumer (`_iter_export_rows`) does `async for`.
    fetch: Callable[[AsyncSession, ExportScope], AsyncIterator[T]]
    # True for templates that emit the effective data license (transcript / engagement_report):
    # `_iter_export_rows` then batch-resolves it once for the whole group and threads it through
    # `ExportScope.effective_license`, so the fetcher doesn't do a per-evaluation lookup (N+1).
    needs_effective_license: bool = False
    # `ExportFilters` dimensions this template actually honors (field names). The create endpoint
    # rejects a filter dimension outside this set (400) rather than accepting-then-silently-dropping
    # it — symmetric with the request-time `status` validation, and keeps the stored/audited/
    # fingerprinted filter set honest (no "looks scoped but wasn't", no surprising idempotency 409).
    supported_filters: frozenset[str] = frozenset()


_PAGE = 500


async def iter_pages[T](page: Callable[[int, int], Awaitable[tuple[list[T], int]]]) -> AsyncIterator[list[T]]:
    """Yield successive pages of a paginated `(rows, total)` list service (lazy — never accumulates).

    The streaming counterpart to `drain_pages`: a template loops `async for batch in
    iter_pages(...)`, resolves that batch's labels, and yields its rows — so only one page
    is ever in memory. Termination mirrors `drain_pages`: stop on a short/empty page (the
    robust terminator — a COUNT that overcounts filtered rows can't stall it) or once
    `offset` reaches the reported total.

    Known limitation: OFFSET paging across separate statements under READ COMMITTED has no
    stable snapshot, so a row inserted/deleted in the scope *while the export streams* can
    shift the window and duplicate or drop a boundary row. This is rare (needs the exported
    evaluation modified mid-export), non-corrupting, and non-security; export a stable
    evaluation for an exact snapshot. A repeatable-read snapshot was rejected (it regressed
    the worker's write paths, see `build_async_engine`); keyset pagination is the proper fix
    but would require reworking the shared `list_*` services — deferred as disproportionate.
    """
    offset = 0
    while True:
        batch, total = await page(_PAGE, offset)
        if batch:
            yield batch
        offset += len(batch)
        if not batch or len(batch) < _PAGE or offset >= total:
            return


async def drain_pages[T](page: Callable[[int, int], Awaitable[tuple[list[T], int]]]) -> list[T]:
    """Drain a paginated `(rows, total)` list service into its full result set (materialized).

    For the few callers that genuinely need the whole set at once (e.g. enumerating a
    group's evaluations to drive the export loop — a small, bounded set). Row-level export
    fetchers use `iter_pages` instead to stay flat-memory.
    """
    rows: list[T] = []
    async for batch in iter_pages(page):
        rows.extend(batch)
    return rows
