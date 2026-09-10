---
name: tasks
description: Read before adding, modifying, or removing any Celery task. Defines task layout (one tasks.py per bounded context, auto-discovered), naming, signature rules, DB access via session_scope, retry policy, testing patterns, and forbidden patterns. Use whenever a task touches files under `app/workers/` or any `app/core/<domain>/tasks.py`.
---

# Background tasks (Celery)

The goal is a worker fleet where every task is JSON-safe, idempotent, and retryable. The Signature / DB access / Retry sections below are the reference patterns — copy from them, not from production tasks that may have domain-specific twists.

## Where does the task go?

| Source / nature | Module |
|---|---|
| Domain-specific task (touches one bounded context) | `app/core/<domain>/tasks.py` |
| Cross-domain / infrastructure (e.g. cleanup, sanity-check) | `app/workers/tasks.py` (create it with the first such task — it does not exist today) |

`autodiscover_tasks` in [app/workers/celery_app.py](../../../app/workers/celery_app.py) walks every listed package and imports `tasks` automatically — no manual registration. If you add a new `app/core/<new_domain>/` and want it picked up, append it to the `autodiscover_tasks([...])` list.

## Mandatory rules

1. **One task = one job.** Don't pack multiple unrelated jobs behind a `task_kind` flag; split them into separate `@shared_task`s.
2. **Use `@shared_task`, not `@celery_app.task`.** `shared_task` doesn't capture the app instance, so the task can be imported by tests before the app is configured.
3. **All arguments and return values are JSON.** No pickle. Convert `UUID`/`datetime`/`Decimal` to `str` (ISO 8601 for datetimes) at the boundary.
4. **DB access goes through `session_scope()`** (sync), or `async_session_scope()` for a task that reuses the async service layer (see "Async DB access" below). Never import the engine from `app/core/database.py` — worker modules build their own engines in `app/workers/session.py`; the app's are loop-bound singletons a worker process must not touch.
5. **Idempotency is your responsibility.** `acks_late=True` + `prefetch_multiplier=1` mean a task can be redelivered on worker crash. Design tasks so re-running them with the same input is safe (upsert, conditional writes, deduplication keys).
6. **Retries are declared, not ad-hoc.** Use `autoretry_for=(...)` + `retry_backoff=True` + `retry_jitter=True` + `max_retries=N`. Catching exceptions inside the task to call `self.retry()` is OK only when the retry decision depends on the payload.
7. **Name the task explicitly.** Pass `name="app.core.<domain>.tasks.<verb>"` (the full module path) — implicit names break when modules move.
8. **No blocking sleeps.** If you need a delay, schedule a follow-up task via `apply_async(countdown=...)`. Never `time.sleep` inside a task.

## Naming

- Module: `tasks.py` (singular `task.py` is not auto-discovered).
- Function: imperative verb phrase — `send_invitation_email`, `recompute_conversation_scores`, **not** `email_sender` or `score_recomputer`.
- Task `name=`: full dotted path matching the module — `app.core.conversations.tasks.recompute_conversation_scores`.

## Signature

```python
from celery import shared_task

from app.workers.session import session_scope


@shared_task(name="app.core.<domain>.tasks.do_thing")
def do_thing(entity_id: str) -> dict[str, int]:
    """One-line summary. Idempotent by <reason>."""
    ...
```

- Args: only JSON-serialisable (str / int / float / bool / None / list / dict).
- Return: JSON-serialisable or `None`.
- Add a one-line docstring stating **why the task is idempotent**. If you cannot, the task isn't ready to merge.

## DB access pattern

```python
from sqlalchemy import select

from app.core.conversations.models import Conversation
from app.workers.session import session_scope


@shared_task(name="app.core.conversations.tasks.touch_conversation")
def touch_conversation(conversation_id: str) -> None:
    """Bump updated_at on a conversation. Idempotent — repeated calls are no-ops."""
    with session_scope() as session:
        conversation = session.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        ).scalar_one_or_none()
        if conversation is None:
            return
        conversation.updated_at = ...  # sync code; engine is sync
```

`session_scope` commits on success, rolls back on exception. Don't call `session.commit()` yourself unless you have a deliberate mid-task checkpoint.

### Async DB access (reusing the async service layer)

Default to the **sync** `session_scope` above. The exception is a task that must reuse the existing **async** service layer unchanged (async DB-bound services, e.g. the visibility resolution + fetchers the HTTP handlers use). For those, wrap the task body in `asyncio.run(...)` and consume `async_session_scope()`:

```python
import asyncio

from app.workers.session import async_session_scope


@shared_task(name="app.core.exports.tasks.run_export_job")
def run_export_job(job_id: str) -> dict[str, str]:
    """One-line summary. Idempotent by <reason>."""
    return asyncio.run(_run_export_job(UUID(job_id)))


async def _run_export_job(job_id: UUID) -> dict[str, str]:
    async with async_session_scope() as session:
        ...  # await async services here
```

Rules specific to `async_session_scope` (see `app/core/exports/tasks.py` for a worked example):
- It builds and disposes a **per-run** async engine (NullPool): an async engine is bound to the loop that created it, and each `asyncio.run` spins a fresh loop, so a cached singleton (like the sync one) would bind to a dead loop. This is why it still does **not** import from `app/core/database.py` (same rule as sync — see Forbidden).
- **No auto-commit.** Unlike `session_scope`, it only rolls back / disposes on exit; the task commits its own state transitions (so a long generation isn't held inside one open transaction).

## Retry pattern

```python
class RateLimitError(Exception):
    """Raised when the upstream API returns 429."""


@shared_task(
    name="app.core.ai_gateway.tasks.call_provider",
    bind=True,
    autoretry_for=(RateLimitError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def call_provider(self, prompt: str) -> str: ...
```

- `autoretry_for=(...)` — only transient errors. Bugs and 4xxs should fail immediately so they surface in metrics.
- `retry_backoff=True` — exponential delays (1s, 2s, 4s, ...).
- `retry_jitter=True` — randomises delays so retries from a fleet don't thunder-stampede the downstream.
- `max_retries` is mandatory. Default in Celery is unlimited; never rely on the default.

## Periodic tasks (Celery beat)

A task that must run on a schedule is registered in `app.conf.beat_schedule` in [app/workers/celery_app.py](../../../app/workers/celery_app.py), **by its registered `name=`** (a dotted string), not by importing the function — that keeps the schedule decoupled from import order.

```python
app.conf.beat_schedule = {
    "reap-orphaned-streaming-messages": {
        "task": "app.core.conversations.tasks.reap_orphaned_streaming_messages",
        "schedule": float(
            _settings.streaming_reap_interval_seconds
        ),  # seconds; drive cadence from Settings, never hardcode
    },
}
```

Rules:

- **The task itself stays an ordinary `@shared_task`** — nothing in its body changes because it's scheduled. Write it the same as any other (idempotent, JSON-safe).
- **Cadence comes from `Settings`**, not a literal — add a field (e.g. `*_interval_seconds`) so it's env-tunable and the schedule entry reads `float(_settings.<field>)`.
- **A schedule is a consumer-less producer until a worker runs.** Beat only *publishes* due tasks to the broker; the worker *consumes* them. In `docker-compose.yml` `beat` depends only on `redis` (it just publishes), while `worker` depends on `db` + `redis` (it executes). Starting beat without a worker just piles unconsumed tasks in the broker.
- **Prefer self-healing over retries for periodic sweeps.** If a run fails, the next tick re-runs it — so an idempotent periodic task usually wants *no* `autoretry_for` (see the Retry section). Say so in the docstring.
- **Overlap is your problem to make safe.** If a run can outlast its interval, beat will enqueue another; rely on the same idempotency (guarded `WHERE`, row locks) that protects against `acks_late` redelivery.
- Test it with two cheap unit checks: the task is discovered (`name in celery_app.tasks`) and the schedule entry points at it with the configured cadence. See [tests/core/conversations/test_tasks.py](../../../tests/core/conversations/test_tasks.py).

## Testing

`tests/conftest.py` provides an `eager_celery` fixture that switches tasks to in-process execution. Apply it explicitly — do not autouse.

```python
import pytest


@pytest.mark.unit
@pytest.mark.usefixtures("eager_celery")
def test_my_task_returns_x() -> None:
    assert my_task.delay(arg=1).get(timeout=1) == "x"
```

- Mock `session_scope` for DB-touching tasks: real-DB tests belong in `tests/<domain>/` with `@pytest.mark.integration`, not in `tests/workers/`.
- Test the **retry policy declaration** (attributes on the task) separately from invoking the retry under eager mode — eager retry behaviour varies between Celery versions.
- Real-broker smoke tests (`@pytest.mark.integration` + `@pytest.mark.slow`) are valid but require a running worker started manually via `celery -A app.workers.celery_app worker`.

## Forbidden

- `time.sleep`, `requests.get` without `timeout=`, any blocking I/O without a timeout.
- Pickle / `pickle.dumps` arguments — implicitly via passing complex objects.
- Mutating module-level state (besides the singletons in `app/workers/session.py`).
- Importing the async engine, `get_db`, or anything from `app/core/database.py`.
- Using `from __future__ import annotations` — Celery's type introspection on signatures can choke; rely on the project's Python 3.14 baseline instead.

## When you change something here

| Change | Also update |
|---|---|
| Add a new `app/core/<domain>/` that needs tasks | append the package to `autodiscover_tasks([...])` in [app/workers/celery_app.py](../../../app/workers/celery_app.py) |
| Schedule a task to run periodically | add an entry (keyed by `name=`) to `app.conf.beat_schedule` in [app/workers/celery_app.py](../../../app/workers/celery_app.py), driving the cadence from a `Settings` field |
