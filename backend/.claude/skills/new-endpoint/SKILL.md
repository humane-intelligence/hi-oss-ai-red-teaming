---
name: new-endpoint
description: Use when adding a new FastAPI endpoint to this repo. Defines where the router, schemas, service, and test live, and the DI / async conventions to follow. Trigger whenever a task involves creating a new route or extending app/api/.
---

# Adding a FastAPI endpoint

## Where things live

| Piece | Location | Notes |
|---|---|---|
| Router | `app/api/v1/<resource>.py` | One file per resource area. |
| Request/response schemas | same file as the router | Move to `app/core/<area>/schemas.py` only if they grow or are shared. |
| Domain logic | `app/core/<area>/service.py` | Pure async functions; no FastAPI imports here. |
| Test | `tests/api/v1/<resource>/test_*.py` | Mirror source layout. |

Bounded-context packages already listed in the CLAUDE.md Architecture map may exist as empty `__init__.py` placeholders; add concrete files into them when the first real piece lands. Don't invent new `app/core/<area>/` packages off-map.

## Wiring

1. Define the router in `app/api/v1/<resource>.py`.
2. Include it from `app/api/v1/__init__.py` (not `app/main.py`).
3. Inject dependencies via the aliases in `app/core/dependencies.py` — add a new `Annotated[...]` alias there, not inline in the handler.

## Conventions

- Handler signatures: `async def` unless the path is purely CPU-bound.
- Use `from fastapi import status` and the named constants (`status.HTTP_201_CREATED`) — never bare integers.
- Set `response_model=`, `status_code=`, `summary=`, `description=`, and `responses=` on every decorator.
- Errors: raise `APIError` subclasses from `app.core.exceptions` (`NotFoundError`, `ConflictError`, `UnauthorizedError`, `ForbiddenError`, `BadRequestError`). Never `raise HTTPException(...)`.
- All datetimes UTC — `datetime.now(UTC)`, never `utcnow()`.
- Name the `SessionUser` dep `caller` (or `_caller` when unused) — never `user`, which is the target entity.

## Minimal example

```python
# app/api/v1/users.py
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.dependencies import DbSession
from app.core.exceptions import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES, problem_response

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    email: str


class UserOut(BaseModel):
    id: UUID
    email: str


@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create user",
    description="Create a new user account.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_409_CONFLICT: problem_response("Email already taken."),
    },
)
async def create_user(payload: UserCreate, db: DbSession, response: Response) -> UserOut:
    ...
    response.headers["Location"] = f"/api/v1/users/{user.id}"
    return user


@router.get(
    "/{user_id}",
    response_model=UserOut,
    status_code=status.HTTP_200_OK,
    summary="Get user",
    description="Fetch one user by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_404_NOT_FOUND: problem_response("User does not exist."),
    },
)
async def get_user(user_id: UUID, db: DbSession) -> UserOut:
    user = ...  # look up from db
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return user
```

```python
# app/api/v1/__init__.py — one line added
from app.api.v1.users import router as users_router

v1_router.include_router(users_router)
```

## Test

Write the test in `tests/api/v1/<resource>/test_*.py` per [.claude/skills/tests/SKILL.md](../tests/SKILL.md). Endpoints that hit the DB use `async_client_with_db` and the `integration` marker; fully-mocked endpoints use `async_client` and the `unit` marker.
