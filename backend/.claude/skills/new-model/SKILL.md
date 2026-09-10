---
name: new-model
description: Use when adding a new SQLModel table. Defines where the model file lives, how to subclass BaseModel, the SQLModel `sa_type` + `sa_column_kwargs` pattern for non-trivial columns, and how to register the model with Alembic. Trigger whenever a task introduces or renames a database table.
---

# Adding a SQLModel table

## Where the model lives

`app/core/<area>/models.py` — one file per bounded context. Don't drop models in `app/models.py`; that file is a *registry*, not a home for definitions.

## Table naming

`__tablename__` is **plural snake_case** (`users`, `roles`, `outbound_emails`, `evaluation_runs`) — class names stay singular (`User`, `Role`, `OutboundEmail`). No exceptions for "log" / "history" / "audit" tables — they're collections too.

## Pattern

```python
# app/core/<area>/models.py
from sqlmodel import Field

from app.core.base_model import BaseModel


class User(BaseModel, table=True):
    __tablename__ = "users"

    email: str = Field(unique=True, index=True, max_length=320)
    display_name: str | None = None
```

`BaseModel` (defined in `app/core/base_model.py`) already provides:

- `id: uuid.UUID` — primary key
- `created_at` / `updated_at` — UTC `TIMESTAMPTZ`, server-side `now()`, `updated_at` auto-bumps on row UPDATE
- `deleted_at: datetime | None` — soft-delete flag (see below)
- `soft_delete(by_id)` / `restore()` / `is_deleted` — instance helpers; mutate only, the caller still flushes/commits. `by_id` is the deleting user (`None` = system), and it scopes who may restore the row later

## Soft delete

`BaseModel` exposes two classmethod factories that pre-attach a `with_loader_criteria` filtering rows where `deleted_at IS NOT NULL`:

```python
from sqlalchemy import func
from sqlmodel import col

# Read live rows
users = (await db.execute(User.live_select().where(col(User.id) == user_id))).scalars().all()

# Bulk-update live rows (tombstoned rows are skipped automatically)
await db.execute(User.live_update().values(status="inactive").where(...))

# Bulk-soft-delete live rows — stamp deleted_at server-side at execute time
await db.execute(User.live_update().values(deleted_at=func.now()).where(col(User.status) == "inactive"))
```

Plus instance helpers for the single-row path: `obj.soft_delete(by_id)` / `obj.restore()` / `obj.is_deleted` — mutate only, the caller flushes/commits (`@transactional` covers this for endpoints). Bulk soft-deletes must stamp `deleted_by_id` alongside `deleted_at`, or the row is restorable by nobody but an admin.

### Rules

- **Plain `select(Model)` / `update(Model)` / `delete(Model)` do NOT filter.** That's the deliberate escape hatch for ops tooling and "I want to see tombstones" queries — there is no second API. The `live_*` factories are the *only* filtered path.
- **Eager-loading a second soft-delete-aware model needs an extra option.** `live_select` only attaches criteria for its own class. To filter a related model loaded in the same statement, add `with_live(OtherModel)` to `.options(...)`:

  ```python
  from app.core.soft_delete import with_live

  stmt = User.live_select().options(
      selectinload(User.roles),
      with_live(Role),
  )
  ```

  Forgetting `with_live(OtherModel)` does not raise — the load silently includes tombstoned children. There is no model-level safety net (no relationship `secondaryjoin`, no global listener); filtering related rows is the call site's responsibility.

- **Raw Core paths bypass the ORM execute pipeline and are NOT filtered.** `connection.execute(text("..."))` or `Table.update()` against a plain `sqlalchemy.Table` won't see the loader option — add the `deleted_at IS NULL` predicate by hand.
- **Unique constraints that must survive soft-delete need a partial unique index** keyed on `deleted_at IS NULL` — see `User.email` / `Role.name` in [auth/models.py](../../../app/core/auth/models.py). Without it, you can't re-register an email after the original account is tombstoned.

Constraint names come from the project naming convention installed on `SQLModel.metadata` (`pk_<table>`, `uq_<table>_<col>`, `fk_<table>_<col>_<ref>`, `ck_<table>_<name>`, `ix_<col-label>`). Don't hand-name constraints — let the convention do it.

## When SQLModel's `Field(...)` is not enough

For columns that need a specific SQL type or column-level options — timezone-aware `DateTime`, server defaults, check constraints, partial indexes, JSONB, PG enums — use **`sa_type=` + `sa_column_kwargs=`**, not `sa_column=Column(...)`.

```python
from datetime import datetime
from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

last_seen_at: datetime | None = Field(
    default=None,
    sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
    sa_column_kwargs={"nullable": True, "server_default": func.now()},
)

permissions: list[str] = Field(
    default_factory=list,
    sa_type=JSONB,
    sa_column_kwargs={"nullable": False, "server_default": text("'[]'::jsonb")},
)
```

### Why not `sa_column=Column(...)`

A `Column` instance can only be attached to one `Table`. If two models inherit a field built with `sa_column=Column(...)` from a shared base, the second `table=True` subclass blows up with `ArgumentError: Column object already assigned`. `sa_type` + `sa_column_kwargs` makes SQLModel construct a **fresh** `Column` per concrete subclass — see [base_model.py](../../../app/core/base_model.py).

Even for a column that's not on the shared base, keep the convention uniform across the codebase so columns can be moved between models without a syntactic rewrite.

### Typing note

`sa_type=DateTime(timezone=True)` (and other type *instances*) trips `ty`'s argument-type check — SQLModel's signature wants a `type[TypeEngine]` but accepts the instance at runtime. Suppress per-line with `# ty: ignore[invalid-argument-type]`. Type *classes* (e.g. `sa_type=JSONB`) need no suppression.

### Rare escape hatch

`sa_column=Column(...)` is reserved for the cases where `sa_type` + `sa_column_kwargs` genuinely can't express the column — e.g. `Computed(...)` columns, `Identity(...)`, or other ctor-only `Column` features. If you reach for it, drop a one-line comment noting why.

All datetimes are UTC — `DateTime(timezone=True)` on the DB side, `datetime.now(UTC)` in Python.

## Registering the model with Alembic

Add **one line** to `app/models.py`:

```python
import app.core.<area>.models  # noqa: F401
```

Nothing else — Alembic loads `app.models` and that pulls every table into `SQLModel.metadata`.

## Then

1. Generate the migration — see [new-migration](../new-migration/SKILL.md). Don't write CREATE TABLE by hand.
2. Run `make erddump` and commit the refreshed [docs/erd.md](../../../docs/erd.md) (Mermaid ERD) in the same diff. The pre-push hook does this automatically; CI fails on drift.

## Don'ts

- No `Base.metadata.create_all` anywhere — schema only ever lands via Alembic.
- No `datetime.utcnow()` — deprecated and naive. Use `datetime.now(UTC)`.
- No bare `String` length — set `max_length=` (Pydantic side) and Postgres will use `VARCHAR(N)`.
- No hand-named constraints unless you need a real reason (e.g. a check constraint with a domain-meaningful name).
