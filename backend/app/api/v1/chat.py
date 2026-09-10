"""Streaming chat endpoint — mounted under `/api/v1/chat`."""

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from sse_starlette import EventSourceResponse

from app.core.ai_gateway.dispatch import dispatch_stream
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.schemas import ChatStreamRequest
from app.core.conversations.streaming import stream_sse
from app.core.database import standalone_session
from app.core.dependencies import DbSession
from app.core.dependencies import SettingsDep
from app.core.exceptions import BadGatewayError
from app.core.exceptions import BadRequestError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import SSE_RESPONSE
from app.core.openapi import problem_response

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "/stream",
    status_code=status.HTTP_200_OK,
    summary="Stream a chat completion",
    description="""Stream a model reply token-by-token over Server-Sent Events.

Resolution failures (unknown alias, a model that cannot answer in text, undecryptable credential)
and the auth gate surface as `application/problem+json` **before** the stream
starts. A provider failure *after* the first byte arrives as an `error` event in
the body — the HTTP status is already `200` by then.

### Events
- `delta` — `{"content": "..."}`, one per slice
- `done` — `{"finish_reason": ..., "usage": ...}`, terminal
- `error` — RFC 7807 shape plus `retryable: bool`

Each event carries a monotonic `id:`. Events never include model or provider
identity.
""",
    response_class=EventSourceResponse,
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_200_OK: SSE_RESPONSE,
        status.HTTP_400_BAD_REQUEST: problem_response(
            "Model cannot serve this request (it cannot answer in text, or an image went "
            "to a model that does not accept image input)."
        ),
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the conversations:participate permission."),
        status.HTTP_404_NOT_FOUND: problem_response("Model alias does not exist."),
        status.HTTP_502_BAD_GATEWAY: problem_response("Stored credential could not be decrypted."),
    },
)
async def stream_chat(
    body: ChatStreamRequest,
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.CONVERSATIONS_PARTICIPATE))],
    db: DbSession,
    settings: SettingsDep,
) -> EventSourceResponse:
    params = body.params.model_dump(exclude_none=True) if body.params else None
    try:
        chunks = await dispatch_stream(
            db,
            settings,
            model_alias=body.model_alias,
            messages=body.messages,
            params=params,
            session_provider=standalone_session,
        )
    except ProviderBadRequestError as exc:
        raise BadRequestError(str(exc)) from exc
    except ProviderError as exc:
        raise BadGatewayError(str(exc)) from exc
    # Release the pooled connection before the (possibly long) stream — get_db
    # would otherwise pin it for the whole response. dispatch_stream is done with
    # the session after its await; stream_sse never touches the DB.
    await db.close()
    return EventSourceResponse(stream_sse(chunks))
