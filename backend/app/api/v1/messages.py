"""Conversation message endpoints — stream a model reply over SSE while persisting it.

Nested under a conversation. `POST .../messages` opens a turn and streams the assistant
reply with the A/B split: Transaction A opens the turn (user message + streaming
placeholder) and commits, the request connection is released, generation streams to the
client, and a detached Transaction B finalizes the placeholder (`persist_stream`).
`regenerate`/`continue` supersede an existing reply (the conversation's last turn only)
and stream a fresh one. A success body is always SSE — never a JSON success body;
failures use the `application/problem+json` error envelope.

Committing Transaction A **before** releasing the connection is load-bearing: the
detached finalize runs on a fresh connection and would otherwise never see the
placeholder. The row lock from `get_conversation(..., for_update=True)` makes the
`turn_index` race-free.
"""

from collections.abc import AsyncGenerator
from collections.abc import Sequence
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse

from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.dispatch import dispatch_stream
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.inference_params import merge_inference_params
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.config import Settings
from app.core.conversations.content_crypto import ContentDecryptError
from app.core.conversations.content_crypto import unseal_content
from app.core.conversations.dependencies import require_conversation_permission
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.schemas import ChatStreamParams
from app.core.conversations.schemas import StreamDelta
from app.core.conversations.schemas import StreamDone
from app.core.conversations.schemas import StreamError
from app.core.conversations.schemas import UserMessageIn
from app.core.conversations.services.conversations import get_conversation
from app.core.conversations.services.conversations import resolve_dispatch_target
from app.core.conversations.services.generation import MASKED_ERROR_DETAIL
from app.core.conversations.services.generation import persist_stream
from app.core.conversations.services.messages import conversation_history
from app.core.conversations.services.messages import finalize_message
from app.core.conversations.services.messages import live_turn_messages
from app.core.conversations.services.messages import open_replacement
from app.core.conversations.services.messages import open_turn
from app.core.conversations.streaming import provider_error_to_status
from app.core.conversations.tags import fold_tag_context
from app.core.database import standalone_session
from app.core.dependencies import DbSession
from app.core.dependencies import SettingsDep
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.evaluations.services.tag_keys import allowed_tags_only
from app.core.evaluations.services.tag_keys import assert_tags_allowed
from app.core.exceptions import APIError
from app.core.exceptions import BadGatewayError
from app.core.exceptions import BadRequestError
from app.core.logging import get_logger
from app.core.media.services.images import get_assets_by_keys
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import SSE_RESPONSE
from app.core.openapi import problem_response

logger = get_logger(__name__)

router = APIRouter(tags=["conversations"])

# Coarse capability gate only. The write-path persists into a specific user's
# conversation, so it gates on `conversations:update` (a write to that resource) —
# deliberately distinct from the stateless `/chat/stream` sandbox, which gates on
# `conversations:participate`. (The `_Participant` name is the actor — the red-teamer
# taking part — not the `participate` permission.) Per-conversation authority (owner
# scope + parent-group visibility + the `evaluation_groups:manage` break-glass) is
# enforced in `get_conversation`, not by this gate, and the gate accepts the permission from
# an in-group role as readily as from the JWT.
_Participant = Annotated[SessionUser, Depends(require_conversation_permission(Permission.CONVERSATIONS_UPDATE))]

# Shared SSE-route responses; 404/409 are narrowed per route below (send keys on
# `client_message_id`, regenerate/continue on a message target).
_STREAM_RESPONSES = COMMON_ERROR_RESPONSES | {
    status.HTTP_200_OK: SSE_RESPONSE,
    status.HTTP_400_BAD_REQUEST: problem_response(
        "Model cannot serve this request (it cannot answer in text, or an image went "
        "to a model that does not accept image input), "
        "or an image_keys entry is unknown, not the caller's own upload, or not private."
    ),
    status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
    status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the conversations:update permission."),
    status.HTTP_404_NOT_FOUND: problem_response("Conversation does not exist or is not visible."),
    status.HTTP_502_BAD_GATEWAY: problem_response("Upstream model/credential failure before the stream started."),
}
_SEND_RESPONSES = _STREAM_RESPONSES | {
    # Send additionally validates the tags the request itself carries against the evaluation's
    # tagging policy. The conversation's stored tags are filtered at the prompt instead, so they
    # never turn a later turn into a 400.
    status.HTTP_400_BAD_REQUEST: problem_response(
        "Model cannot serve this request (it cannot answer in text, or an image went "
        "to a model that does not accept image input), "
        "an image_keys entry is unknown/not the caller's own private upload, or this request's `tags` "
        "carry a key the evaluation disallows (tagging disabled, or outside its restricted tag schema)."
    ),
    status.HTTP_409_CONFLICT: problem_response("client_message_id already used in another conversation."),
}
_REPLACE_RESPONSES = _STREAM_RESPONSES | {
    status.HTTP_404_NOT_FOUND: problem_response("Conversation or target message does not exist or is not visible."),
    status.HTTP_409_CONFLICT: problem_response(
        "Target message is already superseded or not in the conversation's last turn."
    ),
}


async def _validate_attachments(db: DbSession, image_keys: Sequence[str], *, caller_id: UUID) -> None:
    """Reject attachments that aren't the caller's own private uploads.

    A foreign upload reads as unknown — the same 400 as a bad key — so the check
    leaks nothing about other users' assets.
    """
    if not image_keys:
        return
    assets = await get_assets_by_keys(db, image_keys)
    for image_key in image_keys:
        asset = assets.get(image_key)
        if asset is None or asset.created_by_id != caller_id:
            raise BadRequestError(f"Unknown image_key {image_key!r}.")
        if not asset.is_private:
            raise BadRequestError(f"Attachment {image_key!r} must be uploaded with is_private=true.")


async def _replay_events(messages: list[Message], *, content: str) -> AsyncGenerator[dict[str, str]]:
    """Re-stream an already-persisted reply (idempotent replay) — no model call.

    The caller opens the stored row and hands the plaintext in, so a key that cannot open it fails
    before the response is built rather than mid-body.

    Replays the stored terminal state faithfully so a retry can't mistake a failed (or
    in-flight) original for a fresh success: an `error` reply replays as an `error` event
    (rebuilt from the masked-safe stored `extra`, never the raw provider detail), an
    `interrupted` reply ends `finish_reason="interrupted"`, a `complete` reply re-streams
    its content then ends `"replayed"`, and a still-`streaming` reply (a concurrent
    double-submit replaying before the original's detached finalize lands) ends
    `finish_reason="streaming"` with no content — not a success-shaped blank `done`.
    """
    seq = 0
    reply = next((m for m in messages if m.role is MessageRole.ASSISTANT), None)
    if reply is not None and reply.content:
        seq += 1
        yield {"event": "delta", "id": str(seq), "data": StreamDelta(content=content).model_dump_json()}
    if reply is not None and reply.status is MessageStatus.ERROR:
        stored = reply.extra.get("error", {})
        payload = StreamError(
            title=stored.get("title", HTTPStatus.BAD_GATEWAY.phrase),
            status=stored.get("status", HTTPStatus.BAD_GATEWAY),
            detail=MASKED_ERROR_DETAIL,
            retryable=stored.get("retryable", True),
        )
        seq += 1
        yield {"event": "error", "id": str(seq), "data": payload.model_dump_json()}
        return
    finish = reply.status.value if reply is not None and reply.status is not MessageStatus.COMPLETE else "replayed"
    seq += 1
    yield {"event": "done", "id": str(seq), "data": StreamDone(finish_reason=finish).model_dump_json()}


async def _settle_streaming_error(db: AsyncSession, placeholder_id: UUID, *, status_code: int, retryable: bool) -> None:
    """Finalize a stranded streaming placeholder as `error` after a post-Transaction-A failure.

    Every failure between Transaction A and the stream start routes through here so the turn
    settles on a terminal row instead of a `streaming` one the reaper must later sweep. Stores
    only the masked-safe `{status, title, retryable}` (never a raw provider detail), so the HTTP
    status and any idempotent replay derive from the same mapping.
    """
    error_extra = {"error": {"status": status_code, "title": HTTPStatus(status_code).phrase, "retryable": retryable}}
    await finalize_message(
        db, placeholder_id, content="", status=MessageStatus.ERROR, encrypted=False, extra=error_extra
    )
    await db.commit()


async def _stream_into(
    db: AsyncSession,
    settings: Settings,
    conversation: Conversation,
    placeholder_id: UUID,
    override: ChatStreamParams | None,
    prefix_source: Message | None = None,
    message_tags: dict[str, str] | None = None,
) -> EventSourceResponse:
    """Resolve the model + params, build history, open the provider stream, release the DB, and persist.

    Runs after Transaction A committed the placeholder, so every failure before the stream
    starts settles it as `error` (via `_settle_streaming_error`) instead of stranding it
    `streaming` on a raw 500 for the reaper to sweep — model resolution and the provider
    open both route through that. A vanished image blob no longer surfaces here: history
    drops the missing attachment with a warning, so generation proceeds without it.
    `dispatch_stream` is awaited (resolution + credential) on the request session *before*
    `db.close()`, matching the stateless chat endpoint; the actual provider call fires lazily
    while `persist_stream` iterates. ``prefix_source`` (continue) is the prior reply row, unsealed
    inside this function's guard so a key that cannot open it settles the placeholder. The folded tag
    map travels out-of-band (`dispatch_stream`'s `system_suffix`, never the dispatch params — see
    rule 13) and is handed to `persist_stream` from that same fold, so the reply records the map its
    prompt carried.
    """
    try:
        alias, params, mask = await resolve_dispatch_target(db, conversation)
        if override is not None:
            params = merge_inference_params(params, override.model_dump(exclude_none=True))
        # The conversation's tags (plus this request's per-message tags, which override per key),
        # filtered by the evaluation's policy: this map carries stored tags, and every entry point
        # here (send, regenerate, continue) replays them, so a tightened policy has to stop them
        # reaching the model rather than fail the turn.
        folded = await allowed_tags_only(db, conversation.evaluation_id, {**conversation.tags, **(message_tags or {})})
        # Fold and record from one map, so the reply cannot claim context its prompt never carried —
        # and in one pass, so the block's line order follows the same sort the record was built on.
        sent_tags, tag_block = fold_tag_context(folded)
    except APIError as exc:
        # The two `APIError` sources in this block: model/credential resolution (the assignment
        # vanished) and the tagging-policy read (the evaluation was soft-deleted after Transaction A).
        # Settle the placeholder — otherwise it sits `streaming` until the reaper's TTL — then
        # re-raise so the already-mapped HTTP status is unchanged.
        await _settle_streaming_error(db, placeholder_id, status_code=exc.status_code, retryable=False)
        raise
    try:
        history = await conversation_history(db, conversation.id, settings=settings)
        prefix = ""
        if prefix_source is not None:
            # continue: the prior reply is prompted as context and then re-sealed with the
            # continuation, so it has to go in as text both times.
            prefix = unseal_content(prefix_source.content, encrypted=prefix_source.content_encrypted, settings=settings)
            if prefix:
                history.append(ChatMessage(role="assistant", content=prefix))
        chunks = await dispatch_stream(
            db,
            settings,
            model_alias=alias,
            messages=history,
            params=params or None,
            # Out-of-band so params suppression cannot drop it (rule 13).
            system_suffix=tag_block,
            session_provider=standalone_session,
        )
    except ContentDecryptError:
        # History no configured key opens. It fails after Transaction A, so the placeholder settles
        # here like any pre-stream failure; the log carries the ids the generic 500 body cannot.
        logger.error(
            "conversations.history.unseal_failed",
            conversation_id=str(conversation.id),
            message_id=str(placeholder_id),
        )
        await _settle_streaming_error(db, placeholder_id, status_code=HTTPStatus.INTERNAL_SERVER_ERROR, retryable=False)
        raise
    except ProviderError as exc:
        # The HTTP status, the stored `extra`, and any idempotent replay all derive from the one
        # `provider_error_to_status` mapping — so the same failure can't be 502 on the first call
        # and 400 on replay (a context-window error is 400 on both).
        code, retryable = provider_error_to_status(exc)
        await _settle_streaming_error(db, placeholder_id, status_code=code, retryable=retryable)
        detail = MASKED_ERROR_DETAIL if mask else str(exc)
        raise (BadRequestError if code < HTTPStatus.INTERNAL_SERVER_ERROR else BadGatewayError)(detail) from exc
    await db.close()  # release the pooled connection before the (possibly long) stream
    return EventSourceResponse(
        persist_stream(
            chunks,
            placeholder_id,
            mask=mask,
            settings=settings,
            protected=conversation.content_protected,
            prefix=prefix,
            tag_context=sent_tags,
        )
    )


@router.post(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}/messages",
    status_code=status.HTTP_200_OK,
    summary="Send a message and stream the reply",
    description="""Append a user message to the conversation and stream the assistant reply over SSE.

Opens a turn (persisted immediately), then streams generation. A repeated
`client_message_id` replays the existing turn's reply without re-generating: a completed
reply re-streams as `delta`+`done` (`finish_reason: "replayed"`, no `usage`); a stored
`error` replays as an `error` event; an interrupted one ends `finish_reason: "interrupted"`;
and one still generating (a concurrent double-submit, before the detached finalize lands)
ends `finish_reason: "streaming"` with no content — so a retry never mistakes a past
failure or an in-flight reply for a success. Reuse of the key in another conversation is a 409.

### Events
- `delta` — `{"content": "..."}`
- `done` — `{"finish_reason": ..., "usage": ...}`, terminal
- `error` — RFC 7807 shape plus `retryable` (provider failure after the stream started)
""",
    response_class=EventSourceResponse,
    responses=_SEND_RESPONSES,
)
async def send_message(
    evaluation_id: UUID,
    conversation_id: UUID,
    body: UserMessageIn,
    caller: _Participant,
    db: DbSession,
    settings: SettingsDep,
) -> EventSourceResponse:
    conversation = await get_conversation(
        db, evaluation_id, conversation_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True
    )
    await _validate_attachments(db, body.image_keys, caller_id=caller.id)
    opened = await open_turn(
        db,
        conversation,
        settings=settings,
        content=body.content,
        client_message_id=body.client_message_id,
        image_keys=body.image_keys,
        tags=body.tags,
    )
    # Only what this request authors, and only when it authors anything: a replay is a retry of a
    # turn that was already accepted, so re-judging it against a policy an admin has tightened since
    # would turn the stored reply into a permanent 400 the client cannot tell from "never persisted".
    # The conversation's own stored tags are filtered at the fold instead — including them here would
    # 400 every later turn of a conversation tagged before the tightening, over state its owner can
    # no longer reach once the console hides the tagging surface. Rejecting an authored key still
    # names it, so the caller can fix the row. Nothing is committed yet, so a rejection persists
    # nothing — the flushed turn rolls back with the session.
    if not opened.replayed:
        await assert_tags_allowed(db, conversation.evaluation_id, body.tags)
    await db.commit()  # Transaction A — durable before the connection is released

    if opened.replayed:  # idempotent retry — return the stored reply, never re-generate
        replay = await live_turn_messages(db, opened.turn.id)
        # Unsealed here rather than inside the generator: the generator runs after the response is
        # returned and the session is closed, so a row no configured key opens would abort the body
        # mid-stream with no `error` event. Out here it is an ordinary 500 envelope.
        # Selected exactly as the generator selects it, so the two cannot drift apart later.
        stored = next((m for m in replay if m.role is MessageRole.ASSISTANT), None)
        replayed = (
            unseal_content(stored.content, encrypted=stored.content_encrypted, settings=settings)
            if stored is not None and stored.content
            else ""
        )
        await db.close()
        return EventSourceResponse(_replay_events(replay, content=replayed))

    placeholder = next(m for m in opened.messages if m.role is MessageRole.ASSISTANT)
    return await _stream_into(db, settings, conversation, placeholder.id, body.params, message_tags=body.tags)


async def _superseding_turn(
    db: AsyncSession,
    evaluation_id: UUID,
    conversation_id: UUID,
    caller: SessionUser,
    message_id: UUID,
) -> tuple[Conversation, Message, Message, dict[str, str]]:
    """Shared regenerate/continue setup.

    Lock the conversation, supersede the target, and commit Transaction A. History is built
    later, inside `_stream_into`'s guard (the now-superseded reply is excluded from it). Also
    returns the originating user message's tags, so re-answering the same prompt uses the same
    prompt context the transcript still shows on that message.
    """
    conversation = await get_conversation(
        db, evaluation_id, conversation_id, caller_id=caller.id, can_manage=_can_manage(caller), for_update=True
    )
    superseded, placeholder = await open_replacement(db, conversation, message_id)
    turn_messages = await live_turn_messages(db, superseded.turn_id)
    message_tags = next((dict(m.tags) for m in turn_messages if m.role is MessageRole.USER), {})
    await db.commit()  # Transaction A
    return conversation, superseded, placeholder, message_tags


@router.post(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}/messages/{message_id}/regenerate",
    status_code=status.HTTP_200_OK,
    summary="Regenerate an assistant reply",
    description="""Supersede an assistant reply and stream a fresh one for the same prompt.

Only the reply in the conversation's **last turn** may be regenerated (a 409 otherwise);
the superseded reply stays as an append-only artefact and the new reply becomes the live
one. Per-request inference overrides are not accepted here — the conversation's parameter
cascade is used. Events are identical to `POST .../messages`.
""",
    response_class=EventSourceResponse,
    responses=_REPLACE_RESPONSES,
)
async def regenerate_message(
    evaluation_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    caller: _Participant,
    db: DbSession,
    settings: SettingsDep,
) -> EventSourceResponse:
    conversation, _, placeholder, message_tags = await _superseding_turn(
        db, evaluation_id, conversation_id, caller, message_id
    )
    return await _stream_into(db, settings, conversation, placeholder.id, None, message_tags=message_tags)


@router.post(
    "/evaluations/{evaluation_id}/conversations/{conversation_id}/messages/{message_id}/continue",
    status_code=status.HTTP_200_OK,
    summary="Continue an assistant reply",
    description="""Extend an assistant reply: stream a continuation appended to the prior text.

Only the reply in the conversation's **last turn** may be continued (a 409 otherwise). The
model receives the prior reply as context; the new (live) reply is the prior text plus the
continuation and supersedes the old one. The client receives only the new continuation
deltas. No per-request inference override (the conversation cascade is used). Continuing a
reply that hasn't finished streaming yet has no prior text to carry forward. Events are
otherwise identical to `POST .../messages`.
""",
    response_class=EventSourceResponse,
    responses=_REPLACE_RESPONSES,
)
async def continue_message(
    evaluation_id: UUID,
    conversation_id: UUID,
    message_id: UUID,
    caller: _Participant,
    db: DbSession,
    settings: SettingsDep,
) -> EventSourceResponse:
    conversation, superseded, placeholder, message_tags = await _superseding_turn(
        db, evaluation_id, conversation_id, caller, message_id
    )
    return await _stream_into(
        db, settings, conversation, placeholder.id, None, prefix_source=superseded, message_tags=message_tags
    )
