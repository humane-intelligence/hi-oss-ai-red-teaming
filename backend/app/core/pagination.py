"""Reusable paginated-query helper for list services.

Pairs with `Page[T]` / `PaginationParams` in `app.core.schemas`: routes get
the request-side `limit` / `offset`, services hand a filtered+ordered
`Select` to `paginate` and get back `(items, total)` ready to wrap.
"""

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession


async def paginate[T](
    session: AsyncSession,
    statement: Select[tuple[T]],
    *,
    limit: int,
    offset: int,
) -> tuple[list[T], int]:
    """Run count + page queries against `statement` and return (items, total).

    `statement` must already carry every filter and ordering the caller
    wants on the page — this helper only adds `LIMIT` / `OFFSET` and a
    parallel `COUNT(*)`. Ordering is stripped from the count statement so
    the database doesn't sort rows it's only going to count.

    Count swaps the columns clause for `COUNT(*)` instead of wrapping
    `statement` in a subquery: `.subquery()` strips ORM-level options
    (e.g. `with_loader_criteria`) and silently inflates the count.
    `maintain_column_froms=True` keeps the original FROM tables.

    Args:
        session: Async DB session bound to the request.
        statement: Filtered+ordered `Select` producing single-column rows
            (i.e. `select(Entity)` or `select(Entity.col)`), so `.scalars()`
            can unwrap each row.
        limit: Maximum rows to return on this page.
        offset: Zero-based offset into the unpaginated result.

    Returns:
        A `(items, total)` tuple where `total` is the unpaginated row
        count of `statement`.
    """
    count_stmt = statement.with_only_columns(func.count(), maintain_column_froms=True).order_by(None)
    total = (await session.execute(count_stmt)).scalar_one()
    page = (await session.execute(statement.limit(limit).offset(offset))).scalars().all()
    return list(page), total
