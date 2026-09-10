"""Conversation-group endpoints — the grouping bucket every conversation lives in.

A *group* is the required parent of every conversation. It is created with one
or more conversations and gains/loses members over its life: a conversation is
added via the conversation create endpoint, moved between groups via the
conversation PATCH, and removed via the conversation delete. Comparing several
models on one prompt (one conversation per model, grouped here) is the motivating
case, but a group is just a named group — it owns a `name`, the evaluation and
the shared scenario, while each member conversation keeps its own
inference-params layer and message history.

A group is removed automatically when its last live conversation leaves, and
deleting a group cascade-soft-deletes its members — so a live conversation never
dangles under a dead group, and an empty group never lingers.

Reads are nested under the evaluation (like conversations), plus a flat
cross-evaluation list; **create** is posted to `/scenarios/{scenario_id}/conversation-groups`
and derives the evaluation from the scenario, so the URL mirrors the taxonomy the
group sits in. Reads gate on `conversations:read`, writes on the matching
`conversations:{create,update,delete}`; the service scopes every read/write to the
caller's own rows *and* the parent group's visibility, with the
`evaluation_groups:manage` break-glass lifting both. Reads additionally admit every
member's rows in a group where the caller holds `conversations:read_any`; writes never do.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.ai_gateway.inference_params import dump_inference_params
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.dependencies import ConversationGroupFiltersDep
from app.core.conversations.dependencies import group_of_path_scenario
from app.core.conversations.dependencies import require_conversation_permission
from app.core.conversations.filters import ConversationGroupFilters
from app.core.conversations.filters import ConversationGroupOrderBy
from app.core.conversations.filters import GroupScenarioIdFilter
from app.core.conversations.schemas import ConversationGroupCreate
from app.core.conversations.schemas import ConversationGroupResponse
from app.core.conversations.schemas import ConversationGroupUpdate
from app.core.conversations.services.groups import ConversationSpec
from app.core.conversations.services.groups import create_conversation_group
from app.core.conversations.services.groups import get_conversation_group
from app.core.conversations.services.groups import list_conversation_groups
from app.core.conversations.services.groups import rename_conversation_group
from app.core.conversations.services.groups import soft_delete_conversation_group
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import transactional
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.evaluations.dependencies import require_object_or_global
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.evaluations import resolve_effective_licenses
from app.core.evaluations.services.scenarios import get_scenario_by_id
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page

router = APIRouter(tags=["conversations"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Evaluation does not exist (or is outside the caller's scope / not visible).")
_CREATE_NOT_FOUND = problem_response(
    "The path scenario does not exist (or is outside the caller's scope / not visible), "
    "or a model assignment is not part of its evaluation."
)
_GROUP_NOT_FOUND = problem_response(
    "Group does not exist in the evaluation (or is outside the caller's scope / not visible)."
)

# Every route with an object in its path honours in-group authority as well as the JWT;
# the flat listing has none and stays global-only. Create's object is the scenario, so it
# resolves the parent group through that instead of an `evaluation_id`.
_ReadCaller = Annotated[SessionUser, Depends(require_conversation_permission(Permission.CONVERSATIONS_READ))]
_CreateCaller = Annotated[
    SessionUser, Depends(require_object_or_global(Permission.CONVERSATIONS_CREATE, group_of_path_scenario))
]
_UpdateCaller = Annotated[SessionUser, Depends(require_conversation_permission(Permission.CONVERSATIONS_UPDATE))]
_DeleteCaller = Annotated[SessionUser, Depends(require_conversation_permission(Permission.CONVERSATIONS_DELETE))]
_FlatReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.CONVERSATIONS_READ))]

_OrderBy = Annotated[
    ConversationGroupOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


@router.post(
    "/scenarios/{scenario_id}/conversation-groups",
    response_model=ConversationGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a group",
    description=(
        "Create a group against the scenario in the path, plus one conversation per entry in "
        "`models` (at least one), all owned by the caller and sharing that scenario. The scenario "
        "carries the evaluation the group belongs to. The same model may be listed more than "
        "once — each entry becomes its own conversation. Every model assignment must belong to "
        "the scenario's evaluation. The `Location` header points at the group's read route, which "
        "stays nested under the evaluation."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _CREATE_NOT_FOUND,
    },
)
@transactional
async def create_conversation_group_endpoint(
    scenario_id: UUID,
    payload: ConversationGroupCreate,
    caller: _CreateCaller,
    db: DbSession,
    response: Response,
) -> ConversationGroupResponse:
    """Create a group (with its conversations) owned by the caller against one scenario.

    The scenario is resolved under the caller's visibility first — that both gates
    the write and yields the `evaluation_id` the group denormalises, so an invisible
    or tombstoned scenario is a 404 before anything is created.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:create`.
    * **404 Not Found** — the scenario isn't visible, or a model assignment isn't part of its evaluation.
    """
    models = [
        ConversationSpec(
            assignment_id=entry.evaluation_ai_model_id,
            parameters=dump_inference_params(entry.parameters),
            title=entry.title,
        )
        for entry in payload.models
    ]
    can_manage = _can_manage(caller)
    scenario = await get_scenario_by_id(db, scenario_id, caller_id=caller.id, can_manage=can_manage)
    conversation_group, conversations = await create_conversation_group(
        db,
        user_id=caller.id,
        evaluation_id=scenario.evaluation_id,
        scenario_id=scenario_id,
        name=payload.name,
        models=models,
        can_manage=can_manage,
    )
    response.headers["Location"] = (
        f"/api/v1/evaluations/{conversation_group.evaluation_id}/conversation-groups/{conversation_group.id}"
    )
    license_str = await resolve_effective_license(db, conversation_group.evaluation_id)
    return ConversationGroupResponse.from_model(conversation_group, conversations, effective_license=license_str)


@router.get(
    "/evaluations/{evaluation_id}/conversation-groups",
    response_model=Page[ConversationGroupResponse],
    status_code=status.HTTP_200_OK,
    summary="List groups in an evaluation",
    description=(
        "List groups within the evaluation — the caller's own, plus every member's in a group "
        "where the caller holds `conversations:read_any`; filter with `scenario_id`. "
        "Most recent first (`-created_at`)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def list_evaluation_groups_endpoint(
    evaluation_id: UUID,
    caller: _ReadCaller,
    pagination: PaginationDep,
    db: DbSession,
    scenario_id: GroupScenarioIdFilter = None,
    order_by: _OrderBy = "-created_at",
) -> Page[ConversationGroupResponse]:
    """List groups within one evaluation.

    Resolves the evaluation under the visibility rule first, so an unknown/hidden
    parent is a 404 (not a 200 empty page). Admins (`evaluation_groups:manage`) and
    holders of `conversations:read_any` on the parent group see every member's groups,
    not just their own.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    * **404 Not Found** — the evaluation does not exist or its parent group isn't visible.
    """
    can_manage = _can_manage(caller)
    evaluation = await get_evaluation(db, evaluation_id, caller_id=caller.id, can_manage=can_manage, with_models=False)
    groups, total = await list_conversation_groups(
        db,
        caller_id=caller.id,
        can_manage=can_manage,
        read_any=True,
        filters=ConversationGroupFilters(evaluation_id=evaluation_id, scenario_id=scenario_id),
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    # All groups share the path evaluation — resolve the effective license once
    # (the resolver owns the evaluation → group → platform-default cascade).
    license_str = await resolve_effective_license(db, evaluation.id)
    return Page[ConversationGroupResponse](
        items=[
            ConversationGroupResponse.from_model(group, group.conversations, effective_license=license_str)
            for group in groups
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/evaluations/{evaluation_id}/conversation-groups/{conversation_group_id}",
    response_model=ConversationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Get group",
    description=(
        "Fetch a group within an evaluation, with its member conversations — the caller's own, or "
        "any member's in a group where the caller holds `conversations:read_any`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _GROUP_NOT_FOUND,
    },
)
async def get_conversation_group_endpoint(
    evaluation_id: UUID,
    conversation_group_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> ConversationGroupResponse:
    """Fetch one group and its member conversations.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    * **404 Not Found** — no such group in this evaluation within the caller's read scope.
    """
    conversation_group = await get_conversation_group(
        db,
        evaluation_id,
        conversation_group_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        with_conversations=True,
        read_any=True,
    )
    license_str = await resolve_effective_license(db, conversation_group.evaluation_id)
    return ConversationGroupResponse.from_model(
        conversation_group, conversation_group.conversations, effective_license=license_str
    )


@router.patch(
    "/evaluations/{evaluation_id}/conversation-groups/{conversation_group_id}",
    response_model=ConversationGroupResponse,
    status_code=status.HTTP_200_OK,
    summary="Rename group",
    description=(
        "Rename a group. The evaluation / scenario links and the member set are not "
        "editable — start a new group, or add a conversation via the conversation create endpoint."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _GROUP_NOT_FOUND,
    },
)
@transactional
async def update_conversation_group_endpoint(
    evaluation_id: UUID,
    conversation_group_id: UUID,
    payload: ConversationGroupUpdate,
    caller: _UpdateCaller,
    db: DbSession,
) -> ConversationGroupResponse:
    """Rename a group.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:update`.
    * **404 Not Found** — no such group owned by the caller in this evaluation.
    """
    conversation_group = await get_conversation_group(
        db,
        evaluation_id,
        conversation_group_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        for_update=True,
        with_conversations=True,
    )
    conversation_group = await rename_conversation_group(db, conversation_group, name=payload.name)
    license_str = await resolve_effective_license(db, conversation_group.evaluation_id)
    return ConversationGroupResponse.from_model(
        conversation_group, conversation_group.conversations, effective_license=license_str
    )


@router.delete(
    "/evaluations/{evaluation_id}/conversation-groups/{conversation_group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete group",
    description=(
        "Soft-delete a group (sets `deleted_at`) **and cascade** to its member conversations — "
        "every conversation belongs to a group, so deleting the group deletes its conversations. "
        "Subsequent reads exclude all of them."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _GROUP_NOT_FOUND,
    },
)
@transactional
async def delete_conversation_group_endpoint(
    evaluation_id: UUID,
    conversation_group_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
) -> Response:
    """Soft-delete a group and cascade-soft-delete its member conversations.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:delete`.
    * **404 Not Found** — no such group owned by the caller in this evaluation.
    """
    conversation_group = await get_conversation_group(
        db, evaluation_id, conversation_group_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True
    )
    await soft_delete_conversation_group(db, conversation_group, by_id=caller.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/conversation-groups",
    response_model=Page[ConversationGroupResponse],
    status_code=status.HTTP_200_OK,
    summary="List groups across evaluations",
    description=(
        "Flat, paginated list of groups across all visible evaluations — the caller's own, plus "
        "every member's in groups where the caller holds `conversations:read_any`; "
        "filter with `evaluation_id` and/or `scenario_id`. Most recent first (`-created_at`)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_conversation_groups_endpoint(
    caller: _FlatReadCaller,
    pagination: PaginationDep,
    filters: ConversationGroupFiltersDep,
    db: DbSession,
    order_by: _OrderBy = "-created_at",
) -> Page[ConversationGroupResponse]:
    """List groups across all evaluations the caller can see.

    Like the flat conversation list, an `evaluation_id` filter pointing at an
    evaluation the caller can't see yields an empty page, not a 404.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    """
    groups, total = await list_conversation_groups(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        read_any=True,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    licenses = await resolve_effective_licenses(db, [group.evaluation_id for group in groups])
    return Page[ConversationGroupResponse](
        items=[
            ConversationGroupResponse.from_model(
                group, group.conversations, effective_license=licenses[group.evaluation_id]
            )
            for group in groups
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )
