"""Model warmup — wake a scale-to-zero endpoint before chatting with it.

`POST /evaluations/{evaluation_id}/models/{assignment_id}/warmup` fires one 1-token
probe at the assignment's model and returns a coarse readiness status. It is the
client's cue to poll until `ready` (or give up) before the first message — a cold
scale-to-zero endpoint (e.g. a HuggingFace Inference Endpoint) rejects the first
request while it wakes.

Lives in its own module rather than the evaluations CRUD router so the litellm-backed
`dispatch` import stays off the CRUD path (gateway import convention). Gated on
`conversations:update` with conversation-style evaluation visibility: any member who
can talk to the model can warm it; a non-visible evaluation is a 404 and the response
carries no provider/model identity (masking-safe).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status

from app.core.ai_gateway import get_model
from app.core.ai_gateway.dispatch import dispatch_probe
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.dependencies import require_conversation_permission
from app.core.database import standalone_session
from app.core.dependencies import DbSession
from app.core.dependencies import SettingsDep
from app.core.evaluations.schemas import WarmupResponse
from app.core.evaluations.services.assignments import get_assignment
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter(prefix="/evaluations", tags=["conversations"])

# Coarse capability gate, mirroring the message-send path: warming a model is a
# precursor to talking to it, so it rides `conversations:update` rather than the
# `evaluations:*` config permissions. Per-evaluation visibility is enforced in
# `get_assignment(..., caller_id=...)`, not by this gate, and the gate accepts the
# permission from an in-group role as readily as from the JWT — without that, a
# group-scoped red teamer never clears warmup and the composer stays disabled.
_Participant = Annotated[SessionUser, Depends(require_conversation_permission(Permission.CONVERSATIONS_UPDATE))]


@router.post(
    "/{evaluation_id}/models/{assignment_id}/warmup",
    response_model=WarmupResponse,
    status_code=status.HTTP_200_OK,
    summary="Warm up a model endpoint",
    description=(
        "Fire a single lightweight probe at an assigned model and report whether it is ready to serve. "
        "Intended for models backed by scale-to-zero endpoints, which reject the first request while a "
        "replica wakes: poll this until `ready` (or `error`) before sending the first message — the probe "
        "itself triggers the wake. Returns `ready` (send now), `starting` (still waking — retry), or "
        "`error` (a fault a retry won't fix, e.g. bad credentials)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the conversations:update permission."),
        status.HTTP_404_NOT_FOUND: problem_response("Evaluation or assignment does not exist or is not visible."),
    },
)
async def warmup_model_endpoint(
    evaluation_id: UUID,
    assignment_id: UUID,
    caller: _Participant,
    db: DbSession,
    settings: SettingsDep,
) -> WarmupResponse:
    """Probe an assigned model's endpoint and return its readiness.

    ### Implementation Notes

    Resolves the assignment under the same visibility rule as conversations (parent
    group `public` or one the caller holds a role in; `evaluation_groups:manage`
    lifts it), so a caller who can't see the evaluation gets a 404. The probe is a
    1-token chat with a short timeout; `dispatch_probe` maps transient/unreachable
    to `starting` and a terminal fault to `error`, never leaking provider detail.
    The `get_model` lookup (assignment → alias) is a second cheap resolve on this
    cold path — acceptable for a non-hot endpoint.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    assignment = await get_assignment(db, evaluation_id, assignment_id, caller_id=caller.id, can_manage=can_manage)
    model = await get_model(db, assignment.model_id)
    result = await dispatch_probe(db, settings, model_alias=model.model_alias, session_provider=standalone_session)
    return WarmupResponse(status=result)
