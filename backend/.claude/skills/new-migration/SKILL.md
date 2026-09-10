---
name: new-migration
description: Use when generating, reviewing, or rolling back an Alembic migration. Defines the make-only workflow, what autogenerate misses, and the round-trip check. Trigger whenever a task introduces a model change or any DDL.
---

# Adding an Alembic migration

## Workflow

1. Add or change the model (see [new-model](../new-model/SKILL.md)).
2. Make sure the model module is imported in `app/models.py` — Alembic resolves the full metadata from that single file.
3. Generate the migration:
   ```bash
   make makemigrations MSG='short imperative description'
   ```
4. **Open the generated file under `alembic/versions/` and read it.** Autogenerate is not magic — see "What autogenerate misses" below.
5. Apply it: `make migrate`.
6. Round-trip check (cheap, catches broken downgrades):
   ```bash
   make undomigration && make migrate
   ```
7. Commit `alembic/versions/<file>` together with the model change.

Never invoke `alembic` or `docker compose run ... alembic` directly — go through `make`.

## What autogenerate misses

Alembic's autogenerate handles column add/drop/rename, type changes (`compare_type=True`), and server defaults (`compare_server_default=True`) reliably. It does **not** reliably handle:

- **Renames** — they look like drop + add. Edit the migration by hand to use `op.alter_column(..., new_column_name=...)` so data isn't lost.
- **Enum value changes** — Postgres `ENUM` types need explicit `ALTER TYPE ... ADD VALUE`; autogenerate emits nothing useful.
- **Indexed expressions / functional indexes** — autogenerate sees the index name but not the expression. Hand-write `op.create_index(..., postgresql_using=...)`.
- **Data migrations** — schema-only by definition. Add a separate revision with `op.execute(...)` or a Python loop if you need to backfill.
- **Constraint name drift** — if the naming convention in `app/core/base_model.py` changes, every constraint gets a "rename" diff. Don't do that.

## Conventions

- `MSG` is a short imperative phrase: `'add users table'`, not `'Added users.'`.
- Filename format is set by `alembic.ini`: `YYYYMMDD_HHMM_<rev>_<slug>.py`. Don't override.
- Constraint names come from the convention in `app/core/base_model.py` (`pk_*`, `fk_*`, `uq_*`, `ck_*`, `ix_*`). Don't hand-name constraints.
- Always provide a real `downgrade()` — even if it's just the symmetric `op.drop_*`. The round-trip check is what catches typos before they hit prod.

## Reading the existing state

```bash
make showmigrations       # full history
make currentmigration     # what's currently applied
make migrationsql         # emit pending migrations as raw SQL (no live DB needed)
make checkmigrations      # fail if there's more than one head (no live DB needed)
```

`make migrationsql` runs `alembic upgrade --sql head` in the app container. No driver connection is made — Alembic uses the PostgreSQL dialect for SQL generation only. Useful for DBA review before applying to prod.

## After a rebase: multiple heads

Two branches each adding a migration off the same parent produce **two heads** once both land on `main` — a real, common case, not a mistake by either author. `make checkmigrations` (also a pre-push hook, triggers on any `alembic/versions/*.py` change) catches it before it surfaces as a cryptic `alembic.util.exc.CommandError: Multiple head revisions` inside pytest fixture setup. Fix by hand after rebasing onto the new `main`:

1. `make checkmigrations` (or `alembic heads` output) shows both head revision ids.
2. In **your** migration, repoint `down_revision` to the *other* head (the one that landed first) instead of the old shared parent.
3. Bump `Create Date` to now and rename the file's `YYYYMMDD_HHMM` prefix to match, so the chain and the filenames agree on order.
4. Re-run `make checkmigrations` (single head), then `make resetdata` (full chain from scratch) and the round-trip check.
