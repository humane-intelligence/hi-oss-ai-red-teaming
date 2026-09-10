---
name: tests
description: Read before writing, modifying, or debugging any test in this project. Defines pytest-only policy, the mirror-source layout (tests/unit, api, services, workers), tier markers (unit / integration / e2e), FastAPI async patterns, fixtures, and forbidden patterns. Use whenever a task involves files under `tests/` or adding new test coverage.
---

# Writing tests

## Mandatory rules

1. **pytest only.** No `unittest.TestCase`, no nose, no custom runners. Pytest-style classes (`class TestX:` without inheritance) are allowed for grouping — see "Grouping with classes" below.
2. **Layout mirrors source.** Test for `app/<area>/foo.py` lives in `tests/<area>/test_foo.py`. The exception is `tests/unit/`, which holds pure-logic tests that don't map to one source area.
3. **Every test carries a tier marker** — `@pytest.mark.unit`, `@pytest.mark.integration`, or `@pytest.mark.e2e`. `--strict-markers` is on; typos are hard errors.
4. **Async tests need no extra marker.** `asyncio_mode = "auto"` — just `async def test_...`.
5. **No real network.** `--disable-socket` is on globally. Use in-memory ASGI transport (already wired in `async_client`). Real sockets require explicit `@pytest.mark.enable_socket`.
6. **Run via `make`.** `make test` runs everything. To narrow the run, pass extra pytest args via the **`ARGS=` variable** — do **not** fall back to raw `uv run pytest`. Examples below.

## Where does the test go?

Two questions decide everything:

1. **What source area is this exercising?** → that picks the directory.
2. **What does the test touch?** → that picks the marker.

### Directory (by source area)

| Source / nature | Test directory |
|---|---|
| Code under `app/<area>/...` | `tests/<area>/` — create the dir alongside the source if it doesn't exist yet |
| Pure logic with no obvious source mirror (prompt builders, parsers, validators, helpers) | `tests/unit/` |
| Genuine multi-component flow | `tests/e2e/` — create the dir when first needed |

Do **not** create empty `tests/<area>/` dirs in advance. They're added in the same change that introduces the matching `app/<area>/`.

### Marker (by what it touches)

| Marker | When | Typical location |
|---|---|---|
| `unit` | no I/O — pure function in / pure value out | `tests/unit/` |
| `integration` | touches at least one real service (DB / Redis / Celery), even if the rest is mocked | any source-mirrored dir (`tests/api/`, etc.) |
| `e2e` | spans multiple components — full user-visible flow | `tests/e2e/` |
| `slow` | optional, orthogonal — > 1s; combine with one of the above | anywhere |

### Edge cases

- Pure helper inside an area dir? Put the **test in the matching `tests/<area>/`** (mirror) but mark `unit`. Directory tracks origin; marker tracks scope.
- Test that mocks the DB but still calls a real Redis / Celery / external API? It's `integration`. Only pure-function tests with **everything** mocked go in `tests/unit/`.
- Two-layer test (route → service → DB) staying inside one process? Still `integration`, not `e2e`. `e2e` means *multiple components* — e.g. an API call issues a Celery task that writes to DB and Redis, and we assert the whole chain.

## API layer vs service layer — who tests what

Never test the same business branch twice across layers. The split:

- **Service tests** (`tests/core/**/services/`) own **branch logic**: visibility scoping, state transitions, conflict/validation rules, masking, ordering, soft-delete cascades, edge cases. This is the authoritative coverage of behaviour.
- **API tests** (`tests/api/`) own the **HTTP contract**: one happy path per endpoint (status code + response shape + `Location`/pagination envelope), auth/permission guards, request validation the route layer adds (422 shapes, tri-state PATCH semantics), and a status mapping **only when it is endpoint-specific** (e.g. the same service error surfacing as 404 here but 409 elsewhere). The generic `APIError` → problem-json translation is covered once in the error-handler tests — a per-endpoint "service raises X → HTTP Y" repeat adds nothing. **Keep one wiring representative per query param and per object-gate per verb** — the service can't see the route's param parsing, nor that a given verb actually calls the gate (or threads the right arguments into it); don't delete the last such test even when a service twin covers the branch.

Writing an API test that drives a service branch through HTTP (set up state → expect the branch's error code)? Move the branch test to the service file; keep an API assertion only for what the service test can't see (routing, DI wiring, response projection).

Exception: a domain deliberately tested API-first (today: conversations — the service file is thin by design) keeps its logic coverage at the API layer until a service-level suite exists; don't duplicate it downward either. Re-evaluation trigger: the next PR that adds or reworks branch logic in that domain's service layer must either start the service suite or explicitly renew this exception in its description.

## Naming

- File: `test_<thing>.py` mirroring source (`app/api/users.py` → `tests/api/test_users.py`).
- Function: `test_<scenario>_<expectation>` in snake_case.
  - `test_health_returns_ok`
  - `test_create_user_rejects_duplicate_email`

## Fixtures (in `tests/conftest.py`)

- `client: TestClient` — synchronous, for straightforward endpoint checks.
- `async_client: AsyncClient` — async, for code paths that exercise async dependencies (DB not wired).
- `async_client_with_db: AsyncClient` — async, with a real DB session wired in; use for integration tests that hit the database.
- `eager_celery` — flips the Celery app into in-process mode for tests that exercise a task. Apply explicitly via `@pytest.mark.usefixtures("eager_celery")`.

Add new **shared** fixtures to `conftest.py`. Test-local fixtures stay in the test file. Per-layer fixtures (e.g. service-specific factories) can live in `tests/<layer>/conftest.py`.

**API-test helpers — use the shared ones, don't redefine them.** `tests/api/v1/conftest.py` provides `make_token(user)` / `bearer(user)` (auth headers), `make_role(db, permissions)`, `caller_with(db, *permissions)` (fresh user holding exactly those permissions) and the `_configured_settings` / `auth_db_client` fixtures. A new API test file must import these instead of growing its own `_token` / `_caller` / `reader_role` copy — that duplication is what the shared-fixture cleanup removed.

**Session-wide perf defaults (set in `conftest.py` at import).** For speed the test process pins the Celery broker to `memory://` (so `apply_async` enqueues in-process and never reaches a real broker), uses the JSON log renderer, and runs Argon2 at cheap params. Don't assert on broker delivery, log format, or hashing cost — override in-test if you genuinely need the real thing.

**Parallel by default (pytest-xdist).** The suite runs with `--numprocesses=auto --dist=worksteal`; each worker creates its own `<db>_test_gwN` database (isolated, migrated once per worker). Redis is shared across workers — a test touching real Redis must isolate its keyspace (none do today). `--random-order` is xdist-safe: the controller's seed is pushed to every worker, so collection stays consistent. The header's seed reproduces the *collection* order only — worksteal assigns tests to workers by runtime timing, so to replay an order-dependent failure deterministically go single-process: `make test ARGS='-n 0 --random-order-seed=<seed>'`.

## Grouping with classes

Plain pytest classes (`class TestX:` — **no inheritance**, no `unittest.TestCase`) are fine when they earn their keep. Reach for one when:

- a cluster of tests shares a non-trivial setup that's natural to express as a class-scoped fixture
- you have many tests on the same subject and grouping makes the file scannable
- you want one marker / one `parametrize` applied to the whole cluster

Otherwise, prefer module-level functions — they have less ceremony.

```python
import pytest
from fastapi import status
from httpx import AsyncClient


@pytest.mark.integration
class TestCreateUser:
    @pytest.fixture
    def payload(self) -> dict:
        return {"email": "ok@example.com", "name": "Ada"}

    async def test_returns_201(self, async_client: AsyncClient, payload: dict) -> None:
        response = await async_client.post("/users", json=payload)

        assert response.status_code == status.HTTP_201_CREATED

    async def test_rejects_duplicate_email(self, async_client: AsyncClient, payload: dict) -> None:
        await async_client.post("/users", json=payload)
        response = await async_client.post("/users", json=payload)

        assert response.status_code == status.HTTP_409_CONFLICT
```

Rules for classes:

- Name: `Test<Subject>` (e.g. `TestCreateUser`, `TestTokenValidation`)
- No `__init__` — pytest won't collect such classes
- No inheritance from `unittest.TestCase` or any test base class
- Methods take `self` as first arg (pytest handles it); fixtures still inject normally
- Apply markers on the class to cover all methods, or per-method to override

## Patterns

### Async endpoint (`tests/api/`)

```python
import pytest
from fastapi import status
from httpx import AsyncClient


@pytest.mark.integration
async def test_health_returns_ok(async_client: AsyncClient) -> None:
    response = await async_client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}
```

### Parametrize instead of looping

```python
import pytest
from fastapi import status
from fastapi.testclient import TestClient


@pytest.mark.integration
@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"email": "ok@example.com"}, status.HTTP_201_CREATED),
        ({"email": "not-an-email"}, status.HTTP_422_UNPROCESSABLE_ENTITY),
        ({}, status.HTTP_422_UNPROCESSABLE_ENTITY),
    ],
)
def test_create_user(client: TestClient, payload: dict, expected_status: int) -> None:
    assert client.post("/users", json=payload).status_code == expected_status
```

### Pure-logic unit (`tests/unit/`) — polyfactory for data

```python
import pytest
from polyfactory.factories.pydantic_factory import ModelFactory

from app.schemas import User


class UserFactory(ModelFactory[User]):
    __model__ = User


@pytest.mark.unit
def test_user_roundtrips_through_json() -> None:
    user = UserFactory.build()

    assert User.model_validate(user.model_dump()) == user
```

Don't hardcode values unless the exact value matters for the assertion.

### Control the clock — `time-machine`

`time-machine` is in the `test` deps. **Use it for anything time-dependent** — `expires_at`, TTLs, reaper thresholds, scheduling — instead of asserting a `[before, after] ± slop` window or monkeypatching `datetime`/`time`. Freeze the clock, then assert an exact value:

```python
import time_machine
from datetime import UTC, datetime, timedelta


@pytest.mark.integration
async def test_invite_sets_expiry(db_session: AsyncSession, ...) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    with time_machine.travel(now, tick=False):
        result = await invite_to_platform(db_session, ...)

    assert result.invitation.expires_at == now + timedelta(hours=168)
```

- `travel(instant, tick=False)` freezes; capture the context manager (`as traveller`) and call `traveller.shift(timedelta(...))` to jump forward — e.g. past a TTL to prove a token/reaper expires.
- It patches the clock process-wide, so it also freezes time **inside an ASGI handler** driven through `async_client` — wrap just the request in the `with` block.

## Running tests — `make` only, narrow via `ARGS=`

There are exactly two test entry points:

| Command | Purpose |
|---|---|
| `make test` | run tests (no coverage) |
| `make test-cov` | run tests with coverage (term + HTML in `htmlcov/`) |

Both targets accept a free-form **`ARGS=` variable** that is appended verbatim to the underlying `pytest` invocation. **Use it whenever you want to narrow the run** — by marker, by directory, by `-k` expression, by failure mode. Do not call `uv run pytest` directly for this; `ARGS=` is the project's contract.

```bash
# Tier
make test ARGS='-m unit'                    # only @pytest.mark.unit
make test ARGS='-m integration'             # only @pytest.mark.integration
make test ARGS='-m "integration and not slow"'

# Path
make test ARGS='tests/api'                  # one layer
make test ARGS='tests/api/test_health.py'   # one file
make test ARGS='tests/api/test_health.py::test_health_returns_ok'  # one test

# Selection
make test ARGS='-k health'                  # name match (substring)
make test ARGS='-x --ff'                    # stop on first fail, run last failures first
make test ARGS='-vv --tb=long'              # more output for a hard-to-debug failure

# Combined
make test ARGS='-m unit -k "parse and not slow" -x'
make test-cov ARGS='-m integration tests/api'   # coverage but only for one slice
```

Tips:

- Quote the whole `ARGS=...` value when it contains spaces. Single quotes are safest (they survive shell + make).
- `ARGS=` works with `test-cov` too — useful for measuring coverage of a specific slice during refactoring.
- **Single-process runs:** pass `-n 0` to disable xdist when a flag needs one process — `--pdb`, `-s` (live stdout), or clean `--durations` profiling: `make test ARGS='-n 0 --pdb -k failing_test'`. Tiny selections (one file / one test) are also faster without worker spin-up.
- Need local variables in a traceback? Pass `--showlocals` per-call (`make test ARGS='-n 0 --showlocals -k failing_test'`) rather than globally — dumps can leak fixture secrets into CI logs.
- If you find yourself reaching for the same `ARGS=` string repeatedly across sessions, that's a signal to add a new Makefile target — propose one.

## Coverage

- Threshold: `fail_under = 80` in `pyproject.toml`. Below it, `make test-cov` fails.
- Branch coverage is on — cover both arms of every `if`.
- HTML report: `make test-cov`, then open `htmlcov/index.html`.

## Style

- Use `from fastapi import status` for HTTP codes — `status.HTTP_201_CREATED` reads better than `201`.
- **Arrange / Act / Assert** — separate the three with blank lines so the structure is visible.
- One concept per test. Multiple `assert` lines on the same concept are fine; bundling unrelated checks is not.

## Forbidden

- `class TestX(unittest.TestCase)` or any class inheriting from a test base — plain pytest classes are fine, inheritance-based ones are not.
- Real HTTP to external services — use in-memory ASGI, or `respx` once added.
- Order-dependent tests — `pytest-random-order` will surface them.
- Module-level shared state — use fixtures with appropriate `scope`.
- `time.sleep()` in tests — refactor, or freeze/advance the clock with `time-machine` (`time_machine.travel(...)`).
- Catching warnings silently — `filterwarnings = ["error"]` is on. Fix the warning or scope the ignore narrowly per-test.
- Hardcoded test data when a `polyfactory` factory would do.
- Putting a test in `tests/unit/` when it touches a real service — even if it feels small. If anything is real, it's `integration` and belongs in the source-mirroring directory.
