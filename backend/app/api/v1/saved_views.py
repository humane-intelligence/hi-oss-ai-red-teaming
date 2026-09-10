"""Saved-view endpoints — flat `/saved-views` resource.

A saved view stores the filter/sort/column state of one list view under a name,
owned by the caller who created it. Strictly owner-scoped: reads gate on
`saved_views:read` and writes on the matching `saved_views:{create,update,delete}`,
and the service scopes every read/write to the caller's own views — there is no
break-glass, so one user never sees another's views. `state` is a standardized
`SavedViewState` envelope (its `filters` values and column ids stay opaque); the
frontend re-issues the normal list request on recall.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import restore_cutoff
from app.core.saved_views.dependencies import SavedViewFiltersDep
from app.core.saved_views.filters import SavedViewOrderBy
from app.core.saved_views.schemas import SavedViewCreate
from app.core.saved_views.schemas import SavedViewResponse
from app.core.saved_views.schemas import SavedViewUpdate
from app.core.saved_views.schemas import SavedViewUpdateChanges
from app.core.saved_views.services.saved_views import create_saved_view
from app.core.saved_views.services.saved_views import get_restorable_saved_view
from app.core.saved_views.services.saved_views import get_saved_view
from app.core.saved_views.services.saved_views import list_saved_views
from app.core.saved_views.services.saved_views import restore_saved_view
from app.core.saved_views.services.saved_views import soft_delete_saved_view
from app.core.saved_views.services.saved_views import update_saved_view
from app.core.schemas import Page

router = APIRouter(prefix="/saved-views", tags=["saved-views"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Saved view does not exist or is not the caller's.")
_CONFLICT = problem_response("A saved view with this name already exists for the resource.")
_NOT_RESTORABLE = problem_response(
    "No restorable saved view with this id: never deleted, deleted longer than the restore window ago, "
    "or not the caller's."
)

_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.SAVED_VIEWS_READ))]
_CreateCaller = Annotated[SessionUser, Depends(require_permission(Permission.SAVED_VIEWS_CREATE))]
_UpdateCaller = Annotated[SessionUser, Depends(require_permission(Permission.SAVED_VIEWS_UPDATE))]
_DeleteCaller = Annotated[SessionUser, Depends(require_permission(Permission.SAVED_VIEWS_DELETE))]

_OrderBy = Annotated[
    SavedViewOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


@router.post(
    "",
    response_model=SavedViewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a view",
    description=(
        "Save the current filter/sort/column state of a list view under a name. The owner is the "
        "authenticated caller; `(resource, name)` must be unique among the caller's views."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def create_saved_view_endpoint(
    payload: SavedViewCreate,
    caller: _CreateCaller,
    db: DbSession,
    response: Response,
) -> SavedViewResponse:
    """Save a list view, owned by the caller.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `saved_views:create`.
    * **409 Conflict** — the caller already has a view with this name for the resource.
    """
    view = await create_saved_view(db, payload, caller_id=caller.id)
    response.headers["Location"] = f"/api/v1/saved-views/{view.id}"
    return SavedViewResponse.from_model(view)


@router.get(
    "",
    response_model=Page[SavedViewResponse],
    status_code=status.HTTP_200_OK,
    summary="List saved views",
    description="Flat, paginated list of the caller's saved views; filter by `resource`. Ordered by `name`.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_saved_views_endpoint(
    caller: _ReadCaller,
    pagination: PaginationDep,
    filters: SavedViewFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: _OrderBy = "name",
) -> Page[SavedViewResponse]:
    """List the caller's saved views.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `saved_views:read`.
    """
    views, total = await list_saved_views(
        db,
        caller_id=caller.id,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[SavedViewResponse](
        items=[SavedViewResponse.from_model(view) for view in views],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{view_id}",
    response_model=SavedViewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get saved view",
    description="Fetch one of the caller's saved views by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_saved_view_endpoint(
    view_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> SavedViewResponse:
    """Fetch one saved view.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `saved_views:read`.
    * **404 Not Found** — no such view owned by the caller.
    """
    view = await get_saved_view(db, view_id, caller_id=caller.id)
    return SavedViewResponse.from_model(view)


@router.patch(
    "/{view_id}",
    response_model=SavedViewResponse,
    status_code=status.HTTP_200_OK,
    summary="Update saved view",
    description=(
        "Rename a saved view or replace its `state`. `resource` is immutable. Omitted fields stay; "
        "`state` is replaced wholesale when given."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def update_saved_view_endpoint(
    view_id: UUID,
    payload: SavedViewUpdate,
    caller: _UpdateCaller,
    db: DbSession,
) -> SavedViewResponse:
    """Update a saved view's name or state.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `saved_views:update`.
    * **404 Not Found** — no such view owned by the caller.
    * **409 Conflict** — the new name collides with another of the caller's views for the resource.
    """
    view = await get_saved_view(db, view_id, caller_id=caller.id, for_update=True)
    raw = payload.model_dump(exclude_unset=True)
    if payload.state is not None:
        # Store the full normalized envelope, not the partial exclude_unset nesting.
        # (Explicit null is rejected upstream, so non-None means the client set it.)
        raw["state"] = payload.state.model_dump()
    changes = SavedViewUpdateChanges.model_validate(raw)
    updated = await update_saved_view(db, view, changes)
    return SavedViewResponse.from_model(updated)


@router.delete(
    "/{view_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete saved view",
    description="Soft-delete a saved view (sets `deleted_at`); subsequent reads exclude it.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def delete_saved_view_endpoint(
    view_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
) -> Response:
    """Soft-delete a saved view.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `saved_views:delete`.
    * **404 Not Found** — no such view owned by the caller.
    """
    view = await get_saved_view(db, view_id, caller_id=caller.id, for_update=True)
    await soft_delete_saved_view(db, view, by_id=caller.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{view_id}/restore",
    response_model=SavedViewResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore saved view",
    description=(
        "Clear a soft-deleted saved view's `deleted_at`, bringing it back into the live listings. "
        "Restorable for a fixed window after the delete (set per deployment). Conflicts when "
        "the caller has since saved a live view under the same name for the same resource."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def restore_saved_view_endpoint(
    view_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
    settings: SettingsDep,
) -> SavedViewResponse:
    """Restore a soft-deleted saved view.

    Gated on `saved_views:delete` — undoing your own delete needs no authority
    beyond the delete itself.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `saved_views:delete`.
    * **404 Not Found** — no restorable view: never deleted, deleted longer than
      the restore window ago, or not the caller's.
    * **409 Conflict** — a live view already uses this name for the resource.
    """
    view = await get_restorable_saved_view(db, view_id, caller_id=caller.id, deleted_cutoff=restore_cutoff(settings))
    restored = await restore_saved_view(db, view)
    return SavedViewResponse.from_model(restored)
