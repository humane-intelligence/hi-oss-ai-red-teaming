"""Conversation endpoints — reads nested under the evaluation, create posted to the scenario.

A conversation is a red-teamer's own session against one model assigned to an
evaluation, so its reads live as a sub-resource of the evaluation (like
scenarios). **Create is the exception**: a conversation always targets one
scenario, so it is posted to `/scenarios/{scenario_id}/conversations` and the
route derives the evaluation from the scenario — the URL mirrors the taxonomy
(evaluation group → evaluation → scenario) the conversation actually sits in.
Reads gate on `conversations:read`, writes on the matching
`conversations:{create,update,delete}`, and the service scopes every read/write
to the caller's own rows *and* the parent group's visibility — the
`evaluation_groups:manage` break-glass lifts both so an admin can reach any
user's conversation. Reads additionally admit every member's rows in a group
where the caller holds `conversations:read_any`; writes never do.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.ai_gateway.inference_params import dump_inference_params
from app.core.annotations.services.message_flags import flag_counts_by_message
from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.dependencies import ConversationFiltersDep
from app.core.conversations.dependencies import group_of_path_scenario
from app.core.conversations.dependencies import require_conversation_permission
from app.core.conversations.filters import ConversationFilters
from app.core.conversations.filters import ConversationOrderBy
from app.core.conversations.filters import ScenarioIdFilter
from app.core.conversations.filters import TitleFilter
from app.core.conversations.schemas import ConversationCreate
from app.core.conversations.schemas import ConversationResponse
from app.core.conversations.schemas import ConversationUpdate
from app.core.conversations.schemas import MessageResponse
from app.core.conversations.services.conversations import create_conversation
from app.core.conversations.services.conversations import get_conversation
from app.core.conversations.services.conversations import get_restorable_conversation
from app.core.conversations.services.conversations import list_conversations
from app.core.conversations.services.conversations import restore_conversation
from app.core.conversations.services.conversations import soft_delete_conversation
from app.core.conversations.services.conversations import update_conversation
from app.core.conversations.services.groups import acquire_group_for_conversation
from app.core.conversations.services.messages import image_keys_for_messages
from app.core.conversations.services.messages import list_conversation_messages
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.evaluations.dependencies import require_object_or_global
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.evaluations import resolve_effective_licenses
from app.core.evaluations.services.scenarios import get_scenario_by_id
from app.core.exceptions import ConflictError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import DeletedFilter
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(tags=["conversations"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response(
    "Evaluation or conversation does not exist (or is outside the caller's scope / not visible), "
    "or the target group is not part of the evaluation."
)
_CREATE_NOT_FOUND = problem_response(
    "The path scenario does not exist (or is outside the caller's scope / not visible), "
    "or the model assignment / group is not part of its evaluation."
)
_CONVERSATION_NOT_FOUND = problem_response(
    "Conversation does not exist in the evaluation (or is outside the caller's scope / not visible)."
)
_GROUP_FULL = problem_response("The target group already holds the maximum number of conversations.")
_GROUP_CONFLICT = problem_response(
    "The target group already holds the maximum number of conversations, "
    "or its scenario does not match the conversation's."
)
_NOT_RESTORABLE = problem_response(
    "No restorable conversation with this id: never deleted, deleted longer than the restore window ago, "
    "deleted by another user, behind a soft-deleted evaluation or evaluation group, or deleted by the "
    "model-unassign cascade rather than by a user."
)
_TAG_NOT_ALLOWED = problem_response(
    "A conversation tag key is not allowed for this evaluation's restricted tag schema."
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
    ConversationOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


@router.post(
    "/scenarios/{scenario_id}/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation",
    description=(
        "Start a fresh, empty conversation against a model assigned to the scenario's evaluation. "
        "The scenario comes from the path and carries that evaluation; the owner is the "
        "authenticated caller. The model assignment must belong to the evaluation, and the "
        "scenario must be the target group's scenario "
        "(a group's conversations all share it). Optional `tags` are free-form key/value pairs stored on the "
        "conversation and folded into the model's prompt as context, minus any the evaluation's "
        "tagging policy no longer allows — those stay on the row but are not sent. When the "
        "evaluation restricts tags, every key must be in its allowed set; when it disables tagging, "
        "every key is rejected. The `Location` header points at the conversation's read route, "
        "which stays nested under the evaluation."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _TAG_NOT_ALLOWED,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _CREATE_NOT_FOUND,
        status.HTTP_409_CONFLICT: _GROUP_CONFLICT,
    },
)
@transactional
async def create_conversation_endpoint(
    scenario_id: UUID,
    payload: ConversationCreate,
    caller: _CreateCaller,
    db: DbSession,
    response: Response,
) -> ConversationResponse:
    """Start a conversation owned by the caller against one scenario.

    The scenario is resolved under the caller's visibility first — that both gates
    the write and yields the `evaluation_id` the conversation denormalises, so an
    invisible or tombstoned scenario is a 404 before anything is created.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **400 Bad Request** — a tag key isn't allowed by the evaluation's restricted tag schema.
    * **403 Forbidden** — caller lacks `conversations:create`.
    * **404 Not Found** — the scenario isn't visible, or the model assignment / group isn't part
      of its evaluation.
    * **409 Conflict** — the target group already holds the maximum number of conversations,
      or the scenario is not the group's scenario.
    """
    can_manage = _can_manage(caller)
    scenario = await get_scenario_by_id(db, scenario_id, caller_id=caller.id, can_manage=can_manage)
    conversation = await create_conversation(
        db,
        user_id=caller.id,
        evaluation_id=scenario.evaluation_id,
        evaluation_ai_model_id=payload.evaluation_ai_model_id,
        scenario_id=scenario_id,
        parameters=dump_inference_params(payload.parameters),
        conversation_group_id=payload.conversation_group_id,
        title=payload.title,
        tags=payload.tags,
        can_manage=can_manage,
    )
    response.headers["Location"] = f"/api/v1/evaluations/{conversation.evaluation_id}/conversations/{conversation.id}"
    license_str = await resolve_effective_license(db, conversation.evaluation_id)
    return ConversationResponse.from_model(conversation, effective_license=license_str)


@router.get(
    "/evaluations/{evaluation_id}/conversations",
    response_model=Page[ConversationResponse],
    status_code=status.HTTP_200_OK,
    summary="List conversations in an evaluation",
    description=(
        "List conversations within the evaluation — the caller's own, plus every member's in a "
        "group where the caller holds `conversations:read_any`. Most recent first (`-created_at`)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def list_evaluation_conversations_endpoint(  # noqa: PLR0913, PLR0917 — args are the HTTP surface; the shared filters dep would expose the path's evaluation_id as a query param
    evaluation_id: UUID,
    caller: _ReadCaller,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
    scenario_id: ScenarioIdFilter = None,
    title: TitleFilter = None,
    deleted: DeletedFilter = False,
    order_by: _OrderBy = "-created_at",
) -> Page[ConversationResponse]:
    """List conversations within one evaluation.

    Resolves the evaluation under the visibility rule first, so an unknown/hidden
    parent is a 404 (not a 200 empty page). Admins (`evaluation_groups:manage`) and
    holders of `conversations:read_any` on the parent group see every member's
    conversations, not just their own.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    * **404 Not Found** — the evaluation does not exist or its parent group isn't visible.
    """
    can_manage = _can_manage(caller)
    evaluation = await get_evaluation(db, evaluation_id, caller_id=caller.id, can_manage=can_manage, with_models=False)
    filters = ConversationFilters(evaluation_id=evaluation_id, scenario_id=scenario_id, title=title, deleted=deleted)
    conversations, total = await list_conversations(
        db,
        caller_id=caller.id,
        can_manage=can_manage,
        read_any=True,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    # Every conversation shares the path evaluation — resolve the effective license once
    # (the resolver owns the evaluation → group → platform-default cascade).
    license_str = await resolve_effective_license(db, evaluation.id)
    return Page[ConversationResponse](
        items=[
            ConversationResponse.from_model(conversation, effective_license=license_str)
            for conversation in conversations
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get conversation",
    description=(
        "Fetch a conversation within an evaluation by id — the caller's own, or any member's in a "
        "group where the caller holds `conversations:read_any`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_conversation_endpoint(
    evaluation_id: UUID,
    conversation_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> ConversationResponse:
    """Fetch one conversation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    * **404 Not Found** — no such conversation in this evaluation within the caller's read scope.
    """
    conversation = await get_conversation(
        db, evaluation_id, conversation_id, caller_id=caller.id, can_manage=_can_manage(caller), read_any=True
    )
    license_str = await resolve_effective_license(db, conversation.evaluation_id)
    return ConversationResponse.from_model(conversation, effective_license=license_str)


@router.get(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}/messages",
    response_model=Page[MessageResponse],
    status_code=status.HTTP_200_OK,
    summary="List conversation messages",
    description=(
        "List the live messages of a conversation — the caller's own, or any member's in a group "
        "where the caller holds `conversations:read_any` — oldest first (by turn, then user "
        "message before the assistant reply). Superseded messages (replaced by a "
        "regenerate/continue) are excluded."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _CONVERSATION_NOT_FOUND,
    },
)
async def list_conversation_messages_endpoint(
    evaluation_id: UUID,
    conversation_id: UUID,
    caller: _ReadCaller,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
) -> Page[MessageResponse]:
    """List the live message history of one conversation.

    Resolves the conversation under the read scope first, so one that is unknown,
    hidden, or outside that scope is a 404 (not a 200 empty page). Admins
    (`evaluation_groups:manage`) and holders of `conversations:read_any` on the
    parent group can read any member's conversation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    * **404 Not Found** — no such conversation in this evaluation within the caller's read scope.
    """
    can_manage = _can_manage(caller)
    await get_conversation(
        db, evaluation_id, conversation_id, caller_id=caller.id, can_manage=can_manage, read_any=True
    )
    messages, total = await list_conversation_messages(
        db, conversation_id, limit=pagination.limit, offset=pagination.offset
    )
    flag_counts = await flag_counts_by_message(
        db,
        conversation_id=conversation_id,
        message_ids=[message.id for message in messages],
        caller_id=caller.id,
        can_manage=can_manage,
    )
    image_keys = await image_keys_for_messages(db, [message.id for message in messages])
    return Page[MessageResponse](
        items=[
            MessageResponse.from_model(
                message,
                settings=settings,
                flag_count=flag_counts.get(message.id, 0),
                image_keys=image_keys.get(message.id, ()),
            )
            for message in messages
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.patch(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
    summary="Update conversation",
    description=(
        "Update a conversation's inference-parameter overrides or its stored tags, move it to "
        "another group, and/or change its title. `parameters` replaces the whole override layer rather "
        "than merging — sending `{}` clears it. `tags` behaves the same way: the map replaces the stored "
        "tag set wholesale and `{}` clears it; when the evaluation restricts tags, every key must be in "
        "its allowed set, and when it disables tagging every key is rejected. `parameters`, `tags` and "
        "`conversation_group_id` all reject an explicit `null` (they back NOT NULL columns) — omit the "
        "field to leave it unchanged. `conversation_group_id` moves the conversation to another group of "
        "the **same evaluation and the same scenario**; if the move empties the previous group, that "
        "group is removed. `title` "
        "is tri-state: omit to leave unchanged, a string to set it, explicit `null` to clear it. The "
        "evaluation / model / scenario links are not editable; start a new conversation instead."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _TAG_NOT_ALLOWED,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _GROUP_CONFLICT,
    },
)
@transactional
async def update_conversation_endpoint(
    evaluation_id: UUID,
    conversation_id: UUID,
    payload: ConversationUpdate,
    caller: _UpdateCaller,
    db: DbSession,
) -> ConversationResponse:
    """Update a conversation's parameters, tags, group, and/or title.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **400 Bad Request** — a tag key isn't allowed by the evaluation's restricted tag schema.
    * **403 Forbidden** — caller lacks `conversations:update`.
    * **404 Not Found** — no such conversation owned by the caller in this evaluation,
      or the target `conversation_group_id` is not the caller's group within this evaluation.
    * **409 Conflict** — the target group already holds the maximum number of conversations,
      or targets a different scenario than the conversation.
    """
    can_manage = _can_manage(caller)
    conversation = await get_conversation(
        db, evaluation_id, conversation_id, caller_id=caller.id, can_manage=can_manage, for_update=True
    )
    new_conversation_group_id = None
    if (
        payload.conversation_group_id is not None
        and payload.conversation_group_id != conversation.conversation_group_id
    ):
        # Validate the target group is the caller's own, in this same evaluation
        # (so a move can't smuggle the conversation into another evaluation), and
        # row-lock it + enforce its size cap — the conversation row is already
        # locked above, so the move is serialised against concurrent delete/assign.
        target_group = await acquire_group_for_conversation(
            db, evaluation_id, payload.conversation_group_id, caller_id=caller.id, can_manage=can_manage
        )
        if target_group.scenario_id != conversation.scenario_id:
            raise ConflictError(
                f"Conversation group {payload.conversation_group_id} targets a different scenario; "
                "a conversation only moves between groups of its own scenario."
            )
        new_conversation_group_id = payload.conversation_group_id
    parameters = dump_inference_params(payload.parameters) if payload.parameters is not None else None
    title_provided = "title" in payload.model_fields_set
    tags_before = dict(conversation.tags)
    if parameters is not None or payload.tags is not None or new_conversation_group_id is not None or title_provided:
        conversation = await update_conversation(
            db,
            conversation,
            by_id=caller.id,
            parameters=parameters,
            tags=payload.tags,
            new_conversation_group_id=new_conversation_group_id,
            title=payload.title,
            title_provided=title_provided,
        )
    if payload.tags is not None and conversation.tags != tags_before:
        # Only on an actual change: a PATCH that re-sends the same map (the FE sends the whole map)
        # would otherwise fill the log with no-op rows.
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.CONVERSATION_TAGS_UPDATE,
            object_type="conversation",
            object_id=conversation.id,
            before={"tags": tags_before},
            after={"tags": dict(conversation.tags)},
            context={"evaluation_id": str(evaluation_id)},
        )
    license_str = await resolve_effective_license(db, conversation.evaluation_id)
    return ConversationResponse.from_model(conversation, effective_license=license_str)


@router.delete(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete conversation",
    description="Soft-delete a conversation (sets `deleted_at`); subsequent reads exclude it.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def delete_conversation_endpoint(
    evaluation_id: UUID,
    conversation_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
) -> Response:
    """Soft-delete a conversation.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:delete`.
    * **404 Not Found** — no such conversation owned by the caller in this evaluation.
    """
    conversation = await get_conversation(
        db, evaluation_id, conversation_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True
    )
    await soft_delete_conversation(db, conversation, by_id=caller.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/conversations",
    response_model=Page[ConversationResponse],
    status_code=status.HTTP_200_OK,
    summary="List conversations across evaluations",
    description=(
        "Flat, paginated list of conversations across all visible evaluations — the caller's own, "
        "plus every member's in groups where the caller holds `conversations:read_any`; "
        "filter with `evaluation_id`, `scenario_id`, `conversation_group_id`, and/or `title`. "
        "Most recent first (`-created_at`)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_conversations_endpoint(
    caller: _FlatReadCaller,
    pagination: PaginationDep,
    filters: ConversationFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: _OrderBy = "-created_at",
) -> Page[ConversationResponse]:
    """List conversations across all evaluations the caller can see.

    Unlike the nested list, an `evaluation_id` filter pointing at an evaluation the
    caller can't see yields an empty page, not a 404 — the scope is applied to the
    query rather than to a resolved parent, and an empty result leaks nothing about
    the target's existence.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:read`.
    """
    conversations, total = await list_conversations(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        read_any=True,
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    licenses = await resolve_effective_licenses(db, [c.evaluation_id for c in conversations])
    return Page[ConversationResponse](
        items=[
            ConversationResponse.from_model(conversation, effective_license=licenses[conversation.evaluation_id])
            for conversation in conversations
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}/restore",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore conversation",
    description=(
        "Clear a soft-deleted conversation's `deleted_at`, bringing it back into the live listings. "
        "Restorable for a fixed window after the delete (set per deployment), and only by the user "
        "who deleted it — the `evaluation_groups:manage` break-glass restores anyone's. If the delete "
        "left the conversation's group empty and pruned it, the group is revived too — including when "
        "the group itself was deleted, which revives it around this one conversation. Conflicts when the "
        "group has since filled the slot the delete freed. A conversation the model-unassign cascade "
        "deleted is **not** restorable: its chosen model is gone, so it could no longer run."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: _GROUP_FULL,
    },
)
@transactional
async def restore_conversation_endpoint(
    evaluation_id: UUID,
    conversation_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
    settings: SettingsDep,
) -> ConversationResponse:
    """Restore a soft-deleted conversation, reviving its group if the delete pruned it.

    Gated on `conversations:delete` — undoing your own delete needs no authority
    beyond the delete itself.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `conversations:delete`.
    * **404 Not Found** — no restorable conversation: never deleted, deleted longer
      than the restore window ago, deleted by someone else, behind a soft-deleted
      evaluation or evaluation group, or deleted by the model-unassign cascade.
    * **409 Conflict** — the conversation's group has since filled the freed slot.
    """
    conversation = await get_restorable_conversation(
        db,
        evaluation_id,
        conversation_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        deleted_cutoff=restore_cutoff(settings),
    )
    restored = await restore_conversation(db, conversation)
    license_str = await resolve_effective_license(db, restored.evaluation_id)
    return ConversationResponse.from_model(restored, effective_license=license_str)
