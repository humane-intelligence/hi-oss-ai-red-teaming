"""Generic `ORDER BY` helper for list endpoints.

The wire format follows the JSON:API convention — a column name with an
optional leading `-` for descending. Each resource pins its whitelist of
acceptable values via a `Literal` type at the route boundary (see e.g.
`UserOrderBy` in `app.core.auth.filters`); this helper trusts that anything
reaching it already resolved through that gate and is therefore safe to
look up on the model by name.
"""

from sqlalchemy import Select
from sqlmodel import col

from app.core.base_model import BaseModel


def apply_order_by[T: BaseModel](statement: Select[tuple[T]], model: type[T], order_by: str) -> Select[tuple[T]]:
    """Append `ORDER BY <field> [DESC] NULLS LAST, id` to ``statement`` based on ``order_by``.

    ``order_by`` is a column-attribute name with an optional leading `-`
    selecting descending order. The attribute is resolved via `getattr` on
    ``model``; the route-level `Literal` is the source-of-truth whitelist,
    so no second registry is kept in sync here.

    `NULLS LAST` is forced on the primary key so `ASC` and `DESC` are
    symmetric — Postgres otherwise sorts NULLs last for `ASC` but first for
    `DESC`, which is surprising for paginated clients. No-op on
    non-nullable columns.

    The model's `id` is appended as an unconditional secondary key so
    `LIMIT`/`OFFSET` pagination stays deterministic when the primary column
    has duplicates (e.g. NULL name columns). Every caller today is a
    `BaseModel` subclass with a UUID `id` PK; revisit if that invariant
    ever loosens.
    """
    descending = order_by.startswith("-")
    column = col(getattr(model, order_by.removeprefix("-")))
    primary = (column.desc() if descending else column.asc()).nulls_last()
    return statement.order_by(primary, col(model.id))
