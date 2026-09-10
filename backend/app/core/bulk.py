"""Platform contract for bulk operations with dry-run preview.

Bulk endpoints share one envelope (`BulkRequest[T]` → `BulkResponse[R]`)
so the FE has uniform partial-success handling across user invites,
event invites, evaluation status changes, and model imports. `dry_run`
runs the full validation + processing path but rolls back on the way
out, so the response previews exactly what a commit would do.

Handlers that wrap `apply_bulk` MUST NOT use `@transactional` —
`apply_bulk` owns the transaction boundary because dry-run wants
rollback even when nothing raised. See the bulk section in
`.claude/skills/api/SKILL.md`.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.error_handlers import api_error_to_problem
from app.core.exceptions import APIError
from app.core.schemas import Problem
from app.core.schemas import ProblemErrorItem


class BulkRow[T](BaseModel):
    """One input row inside a `BulkRequest`."""

    row_key: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Client-assigned identifier echoed back on the response so FE can "
            "correlate results without relying on order."
        ),
        examples=["row-001"],
    )
    data: T = Field(description="Payload for this row; shape depends on the endpoint.")


class BulkRequest[T](BaseModel):
    """Generic bulk request envelope.

    `dry_run=True` runs the full processing path but rolls the outer
    transaction back, so callers can preview per-row outcomes without
    committing. Side-effects (Celery enqueue, `send_email`, audit log,
    outbound HTTP) MUST be gated on `dry_run` in the row processor.
    """

    rows: list[BulkRow[T]] = Field(min_length=1, description="At least one row is required.")
    dry_run: bool = Field(
        default=False,
        description="If true, the request is processed and rolled back instead of committed.",
    )

    @model_validator(mode="after")
    def _validate_unique_and_size(self) -> BulkRequest[T]:
        seen: set[str] = set()
        for row in self.rows:
            if row.row_key in seen:
                msg = f"Duplicate row_key: {row.row_key!r}."
                raise ValueError(msg)
            seen.add(row.row_key)
        # `get_settings()` is `lru_cache`d — reading it here is cheap and lets
        # tests monkeypatch `BULK_MAX_ROWS` + `cache_clear()` to exercise the
        # limit without touching the schema definition.
        max_rows = get_settings().bulk_max_rows
        if len(self.rows) > max_rows:
            msg = f"Bulk request exceeds limit of {max_rows} rows."
            raise ValueError(msg)
        return self


class BulkRowResult[R](BaseModel):
    """Per-row outcome inside a `BulkResponse`."""

    row_key: str = Field(description="Echo of the client-assigned `row_key`.")
    status: Literal["ok", "failed"] = Field(
        description="`ok` if the processor returned; `failed` if it raised an `APIError`."
    )
    data: R | None = Field(default=None, description="Server-rendered result on success; `None` on failure.")
    error: Problem | None = Field(
        default=None,
        description="RFC 7807 envelope on failure; `None` on success.",
    )


class BulkResponse[R](BaseModel):
    """Bulk-operation response envelope with aggregates + per-row results."""

    dry_run: bool = Field(description="Echo of the request flag — `true` means nothing was committed.")
    total: int = Field(description="Number of rows in the request.")
    succeeded: int = Field(description="Rows whose processor returned without raising.")
    failed: int = Field(description="Rows that raised an `APIError`.")
    results: list[BulkRowResult[R]] = Field(description="Per-row outcomes in request order.")


type Processor[T, R] = Callable[[AsyncSession, T], Awaitable[R]]
"""Per-row coroutine signature accepted by `apply_bulk`."""


def _row_relative_problem(problem: Problem, index: int) -> Problem:
    """Rewrite a per-row `Problem`'s `errors[].loc` to point into `rows[index].data`.

    `api_error_to_problem` renders every `APIError` the same way regardless of
    caller, so a service raising `loc=["body", "name"]` for the single-item
    routes is technically wrong here: the actual request body is
    `{"rows": [...], "dry_run": ...}`. Every `errors[]` entry raised anywhere in
    this codebase starts with the literal `"body"` (`PasswordPolicyError`,
    `_duplicate_conflict`, `MissingInferenceEndpointError`, and Pydantic's own
    validation errors), so splicing `rows[index].data` in right after it is
    safe and keeps the pointer resolvable against what the caller actually sent.
    """
    if problem.errors is None:
        return problem
    return problem.model_copy(
        update={
            "errors": [
                ProblemErrorItem(loc=["body", "rows", index, "data", *item.loc[1:]], msg=item.msg, type=item.type)
                for item in problem.errors
            ]
        }
    )


async def apply_bulk[T, R](
    session: AsyncSession,
    request: BulkRequest[T],
    processor: Processor[T, R],
) -> BulkResponse[R]:
    """Run `processor` over each row, returning per-row outcomes.

    Iterates sequentially. Each row runs inside a `SAVEPOINT` so an
    `APIError` rolls back only that row's writes and the loop continues.
    Unexpected (non-`APIError`) exceptions propagate untouched — a
    broken DB connection or assertion failure aborts the whole bulk and
    surfaces as a normal 500 through the global handler; the caller
    never sees a half-formed `BulkResponse`.

    Transaction boundary is owned here: on the way out, the outer
    transaction is committed (`dry_run=False`) or rolled back
    (`dry_run=True`). Handlers calling this helper must NOT decorate
    themselves with `@transactional`.

    Args:
        session: Active `AsyncSession` from the request scope.
        request: Validated bulk envelope.
        processor: Per-row coroutine. Raise an `APIError` subclass to
            record the row as failed; raise anything else to abort the
            whole bulk.
    """
    results: list[BulkRowResult[R]] = []
    succeeded = 0
    failed = 0

    # `begin_nested()` relies on `AsyncSession` autobegin to open the outer
    # transaction on the first DB-touching call inside the savepoint. If
    # autobegin is ever disabled on the session factory, this loop produces
    # non-savepoint sub-blocks and per-row rollback stops isolating writes.
    for index, row in enumerate(request.rows):
        try:
            async with session.begin_nested():
                result = await processor(session, row.data)
        # Only APIError is caught: non-APIError exceptions (broken DB,
        # assertion failures, programming bugs) intentionally propagate
        # to abort the whole bulk — see the docstring above.
        except APIError as exc:
            results.append(
                BulkRowResult(
                    row_key=row.row_key,
                    status="failed",
                    error=_row_relative_problem(api_error_to_problem(exc, instance=None), index),
                )
            )
            failed += 1
        else:
            results.append(
                BulkRowResult(
                    row_key=row.row_key,
                    status="ok",
                    data=result,
                )
            )
            succeeded += 1

    if request.dry_run:
        await session.rollback()
    else:
        await session.commit()

    return BulkResponse(
        dry_run=request.dry_run,
        total=len(request.rows),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )
