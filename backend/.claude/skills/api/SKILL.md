---
name: api
description: Read before adding, modifying, or removing any FastAPI router, request/response schema, or error path. Defines URL versioning, RFC 7807 error envelope, pagination, response_model rules, dependency-injection aliases, tag registration, and the canonical responses= boilerplate. Use whenever a task touches files under `app/api/` or `app/core/{schemas,exceptions,error_handlers,openapi}.py`.
---

# API conventions

The goal is a stable, FE-codegen-friendly OpenAPI spec. Every endpoint follows the same shape so the FE team can rely on uniform error handling, pagination, and schemas.

The closest working reference is [app/api/v1/auth/users.py](../../../app/api/v1/auth/users.py) — read it first; it demonstrates the rules below in real code (CRUD over `User`, `@transactional` on mutating routes, `require_permission` gates, `Page[T]` for list).

## Mandatory rules

1. **Versioning.** All domain endpoints mount under `/api/v1/...` (via [app/api/v1/](../../../app/api/v1/)). Health probes (`/health`, `/ready`) stay top-level and unversioned. New router files go into `app/api/v1/<resource>.py` and are included from [app/api/v1/__init__.py](../../../app/api/v1/__init__.py).
2. **Errors.** Routers raise `APIError` subclasses from [app/core/exceptions.py](../../../app/core/exceptions.py) — `NotFoundError`, `ConflictError`, `UnauthorizedError`, `ForbiddenError`, `BadRequestError`. Never `raise HTTPException(...)`. Never `raise Exception(...)`. Handlers in [app/core/error_handlers.py](../../../app/core/error_handlers.py) translate everything into RFC 7807 `Problem` responses with `application/problem+json`. Exception: a route the browser reaches by top-level navigation rather than an API call — e.g. the OIDC login and callback routes ([app/api/v1/auth/oidc.py](../../../app/api/v1/auth/oidc.py)) — may answer an in-flow failure with a redirect instead, since a JSON body would strand the user on a dead page.
3. **Response envelopes.** Every operation declares `response_model=...`, `status_code=...`, `summary=...`, `description=...`, and `responses=COMMON_ERROR_RESPONSES | {...}` covering the failure modes it can produce. `COMMON_ERROR_RESPONSES` is the **minimal** baseline (422, 500) — opt into per-route codes (404, 409, 401, 403, …) by merging `{status.HTTP_4XX: problem_response("…")}` from [app/core/openapi.py](../../../app/core/openapi.py). Don't include codes the route can't return, or the generated OpenAPI lies to the FE.
4. **Pagination.** List endpoints take `PaginationDep` (from [app/core/dependencies.py](../../../app/core/dependencies.py)) and return `Page[ResourceModel]` (from [app/core/schemas.py](../../../app/core/schemas.py)). Don't reinvent `limit`/`offset` — the dependency caps `limit` at 100.
5. **Filtering & ordering.** Per-resource — no generic framework. Reference: [app/core/auth/filters.py](../../../app/core/auth/filters.py), [app/api/v1/auth/users.py](../../../app/api/v1/auth/users.py).
   - **Filters.** Add `app/core/<area>/filters.py` with a Pydantic `XFilters` model — each field `Annotated[T | None, Query(description=...)] = None`. Register `XFiltersDep = Annotated[XFilters, Depends()]` in `app/core/<area>/dependencies.py`. Inject `XFiltersDep` on the route and pass it through to the service; the service applies it via a private `_apply_<resource>_filters(statement, filters)` helper that appends one `WHERE` per supplied filter.
   - **Substring filters.** ILIKE-style fields must escape user-supplied `LIKE` metacharacters — `?email=%` would otherwise match every row, letting the client toggle wildcards. Apply `escape_like` from [app/core/helpers.py](../../../app/core/helpers.py) inside a `@model_validator(mode="after")` on `XFilters` (**not** a per-field `AfterValidator` — FastAPI's `Depends()` runs field validators twice and would double-escape). The service composes the pattern with the explicit escape character: `col(Model.field).ilike(f"%{filters.field}%", escape="\\")`. Both halves are required — `escape_like` sanitises the value; `escape="\\"` tells Postgres which character is the escape (standard SQL has no default, so omitting it is non-portable and obscures intent).
   - **Ordering.** Define `XOrderBy = Literal["col", "-col", ...]` alongside `XFilters` (leading `-` is descending, JSON:API convention). Inject as `Annotated[XOrderBy, Query(...)] = "<default>"` parameter named `order_by`. The service calls `apply_order_by(statement, Model, order_by)` from [app/core/ordering.py](../../../app/core/ordering.py) — the `Literal` is the source-of-truth whitelist, no column-name dict needed. The helper appends `id` as an unconditional secondary sort so `LIMIT`/`OFFSET` pagination stays deterministic when the chosen column has duplicates (enum statuses, NULL name columns).
   - **m2m filters.** Filter with an `IN`-subquery, not a JOIN — JOINs duplicate rows when the filter broadens. Example: `col(User.id).in_(select(col(UserRole.user_id)).where(...))`.
6. **Path naming.** Plural, `lower-kebab-case`: `/evaluation-runs`, not `/evaluation_run`, not `/evaluationRun`. Path params use snake_case: `{example_id}`.
7. **Schemas.** Pydantic models for request and response. Every `Field(...)` has `description=` and (where it helps codegen) `examples=[...]`. Add a `model_config = ConfigDict(json_schema_extra={"example": {...}})` so `/docs` shows a full example. Models live alongside the router unless reused by ≥ 2 routers — then move to `app/core/<area>/schemas.py`.
8. **Dependency injection.** Always via named `Annotated[..., Depends(...)]` aliases — never inline `Depends(...)` in a route signature. Cross-cutting aliases (`SettingsDep`, `DbSession`, `PaginationDep`, `CurrentUserDep`) live in [app/core/dependencies.py](../../../app/core/dependencies.py); module-specific aliases (filter deps, permission-gated variants) live in that module's own `dependencies.py`, e.g. [app/core/auth/dependencies.py](../../../app/core/auth/dependencies.py).
9. **Tags.** Every router declares `tags=["<tag>"]`. The tag must be registered in `OPENAPI_TAGS` ([app/core/openapi.py](../../../app/core/openapi.py)) with a one-line description. Reuse existing tags before inventing new ones.
10. **Async by default.** Route functions are `async def` unless the work is purely CPU-bound. Matches the project-wide rule in [CLAUDE.md](../../../CLAUDE.md) (#3).
11. **Status codes via `status`.** `from fastapi import status`; use `status.HTTP_201_CREATED`, not `201`. Same in tests.

## Canonical operation skeleton

```python
from fastapi import APIRouter, Response, status

from app.core.dependencies import PaginationDep
from app.core.exceptions import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES, problem_response
from app.core.schemas import Page

router = APIRouter(prefix="/things", tags=["things"])


@router.get(
    "",
    response_model=Page[Thing],
    status_code=status.HTTP_200_OK,
    summary="List things",
    description="Return a paginated slice of things.",
    responses=COMMON_ERROR_RESPONSES,
)
async def list_things(pagination: PaginationDep) -> Page[Thing]: ...


@router.get(
    "/{thing_id}",
    response_model=Thing,
    status_code=status.HTTP_200_OK,
    summary="Get one thing",
    description="Fetch one thing by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_404_NOT_FOUND: problem_response("Thing does not exist."),
    },
)
async def get_thing(thing_id: UUID) -> Thing:
    if thing_id not in store:
        raise NotFoundError(f"Thing {thing_id} not found.")
    return store[thing_id]
```

## Pydantic model conventions

- Separate models for input vs output: `Thing`, `ThingCreate`, `ThingUpdate`.
- `ThingUpdate` has all fields `Optional` with `default=None`; routes apply with `model_copy(update=payload.model_dump(exclude_unset=True))`.
- Every `Field` carries `description=`. Add `examples=[...]` whenever the example helps disambiguate format (UUIDs, datetimes, enums).
- Top-level `model_config = ConfigDict(json_schema_extra={"example": {...}})` provides a full body example for `/docs` and downstream codegen.

## When to raise what

| Situation | Exception |
|---|---|
| Resource lookup fails | `NotFoundError("Thing <id> not found.")` |
| Unique constraint / duplicate | `ConflictError("Thing 'name' already exists.")` |
| Missing or invalid credentials | `UnauthorizedError()` |
| Authenticated but not allowed | `ForbiddenError()` |
| Request semantically wrong (not a schema issue — those are 422 automatically) | `BadRequestError("...")` |
| Anything else broke in our code | let it bubble — the handler maps to 500 and logs full traceback |

**A machine-readable `type` on the envelope.** `APIError.type` defaults to `about:blank`; set it only where a client must branch on the *reason* rather than the status. `TermsAcceptanceRequiredError` ([app/core/terms/service.py](../../../app/core/terms/service.py)) is the one instance today — `urn:redteam:error:terms-acceptance-required` — because it shares 403 with a permission refusal and means the opposite: that one is a dead end, this one is cleared by accepting, and a client that confuses them tells the reader to find an administrator. A cross-cutting refusal like it is documented once in `APP_DESCRIPTION` rather than on every route, but each route that can produce it still declares the **status** in `responses=` (see `TERMS_REFUSED` in [app/core/openapi.py](../../../app/core/openapi.py)).

**Field-level detail on a non-422 error.** An `APIError` may set `errors` (a list of `ProblemErrorItem`, the same shape a Pydantic 422 emits); `api_error_to_problem` forwards it into the `Problem` envelope. So `errors[]` is not 422-only — e.g. `PasswordPolicyError` (a 400) points at the `password` field with a machine code in `type` (`password_too_common` / `password_too_similar`), letting a client map the rejection inline and localize off the code.

## Listing endpoints — Location header on POST

`POST /things` returns 201 and sets `Location: /api/v1/things/{id}` so callers can navigate immediately without an extra GET. Use a `Response` parameter:

```python
async def create_thing(payload: ThingCreate, response: Response) -> Thing:
    thing = ...
    response.headers["Location"] = f"/api/v1/things/{thing.id}"
    return thing
```

## Bulk endpoints

Bulk endpoints share one envelope: `BulkRequest[T]` → `BulkResponse[R]` from [app/core/bulk.py](../../../app/core/bulk.py). The contract is platform-wide so the FE has uniform partial-success handling across user invites, event invites, evaluation status changes, and model imports.

**Always 200 OK** when the request is well-formed — partial failures live in the response body (`results[].status == "failed"` with an inline RFC 7807 `Problem`), not in the HTTP status. Malformed requests (duplicate `row_key`, empty `rows`, over the size limit) return 422 as usual.

**Transaction boundary belongs to `apply_bulk`, not to the handler.** Each row runs inside a `SAVEPOINT`; on the way out, the helper either commits (`dry_run=False`) or rolls back (`dry_run=True`). **Do NOT decorate a bulk handler with `@transactional`** — it would commit on success and defeat dry-run.

**Side-effect gating.** Dry-run only undoes DB writes. Anything else the processor does — Celery `.delay(...)` enqueue, `send_email(...)`, audit-log emission, outbound HTTP, file writes — leaks. Gate every external effect on `if not payload.dry_run:` — inside the row processor, or (preferred for mail, see next paragraph) at the handler level after `apply_bulk` returns. The first real consumer's integration test should assert a dry-run request enqueues zero Celery tasks.

**Side-effects after a row-level failure (commit mode).** A processor that enqueues a Celery task and then raises an `APIError` (or hits one downstream) leaves the broker holding a task referencing data that just got rolled back with the savepoint. If the side-effect must be atomic with the row, defer it until after `apply_bulk` returns, or write it to an outbox table inside the same savepoint and let a worker pick it up. **Reference implementation for deferred mail:** the bulk group-invitation route ([app/api/v1/evaluation_group_invitations.py](../../../app/api/v1/evaluation_group_invitations.py)) — the processor collects a per-row mail spec as its *last* step (so a failed row never produces one; data the response can't reconstruct, like a raw token's accept URL, must be collected here, not rebuilt from `row_keys`), the handler dispatches the specs best-effort after `apply_bulk` commits, and finishes with a route-level `await db.commit()` that lands the dispatch's `outbound_emails` audit rows. That trailing commit is part of the pattern — the handler still must NOT use `@transactional`.

**Size limit.** `Settings.bulk_max_rows` (default 1000, env `BULK_MAX_ROWS`) is the synchronous ceiling. Larger imports go through Celery, not this contract.

**Skeleton:**

```python
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.auth.dependencies import require_permission
from app.core.auth.schemas import SessionUser
from app.core.bulk import BulkRequest, BulkResponse, apply_bulk
from app.core.dependencies import DbSession
from app.core.email.tasks import send_email_task
from app.core.openapi import COMMON_ERROR_RESPONSES, problem_response

router = APIRouter(prefix="/things", tags=["things"])


@router.post(
    "/bulk",
    response_model=BulkResponse[Thing],
    status_code=status.HTTP_200_OK,
    summary="Create things in bulk",
    description="Create up to `BULK_MAX_ROWS` things. Per-row outcomes in the response.",
    responses=COMMON_ERROR_RESPONSES
    | {status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the required permission.")},
)
async def bulk_create_things(
    payload: BulkRequest[ThingCreate],
    db: DbSession,
    _caller: Annotated[SessionUser, Depends(require_permission("things:create"))],
) -> BulkResponse[Thing]:
    async def processor(session, data: ThingCreate) -> Thing:
        thing = await create_thing(session, data)  # may raise ConflictError, NotFoundError, ...
        if not payload.dry_run:
            send_email_task.delay(thing.id)  # side-effect — gated
        return thing

    return await apply_bulk(db, payload, processor)
```

## Tests

The matching test conventions live in [.claude/skills/tests/SKILL.md](../tests/SKILL.md). API-side specifics:

- Endpoint tests go in `tests/api/v1/<resource>/test_*.py`, marked `@pytest.mark.integration`.
- Always assert `Content-Type` for error responses (`application/problem+json`) and the `status`/`title` fields of the `Problem` body.
- For validation (422) tests, assert that `body["errors"]` is populated and the offending field appears in some `loc`.
- For list endpoints, exercise `limit` / `offset` and the `> 100` rejection.

## Forbidden

- `raise HTTPException(...)` inside route handlers — go through `APIError`.
- Inline `Depends(...)` in route signatures — register a `Dep` alias.
- `response_model=None` for non-204 endpoints — pick a model.
- List endpoint without `PaginationDep` / `Page[T]`.
- Path names in `snake_case` or `camelCase`.
- Inventing a new tag without registering it in `OPENAPI_TAGS`.
- `print(...)` — use `logging.getLogger(__name__)`. Ruff `T20` will catch the former; `G` rules require `%s`-style formatting, not f-strings inside `logger.info`.
