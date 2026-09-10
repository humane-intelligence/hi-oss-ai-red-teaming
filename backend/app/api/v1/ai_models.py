"""AI-model registry endpoints — mounted under `/api/v1/ai-models`."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status
from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.dependencies import AiModelFiltersDep
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.inference_params import dump_inference_params
from app.core.ai_gateway.models import PROBED_CALL_FIELDS
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.schemas import AiModelApiKeyBulkItem
from app.core.ai_gateway.schemas import AiModelApiKeyBulkRequest
from app.core.ai_gateway.schemas import AiModelApiKeyUpdate
from app.core.ai_gateway.schemas import AiModelBulkRequest
from app.core.ai_gateway.schemas import AiModelCreate
from app.core.ai_gateway.schemas import AiModelResponse
from app.core.ai_gateway.schemas import AiModelUpdate
from app.core.ai_gateway.schemas import AiModelUpdateChanges
from app.core.ai_gateway.services.ai_models import clear_api_key
from app.core.ai_gateway.services.ai_models import create_model
from app.core.ai_gateway.services.ai_models import get_model
from app.core.ai_gateway.services.ai_models import get_model_by_name
from app.core.ai_gateway.services.ai_models import get_restorable_model
from app.core.ai_gateway.services.ai_models import has_resolvable_credential
from app.core.ai_gateway.services.ai_models import list_labels
from app.core.ai_gateway.services.ai_models import list_models
from app.core.ai_gateway.services.ai_models import restore_model
from app.core.ai_gateway.services.ai_models import set_api_key
from app.core.ai_gateway.services.ai_models import soft_delete_model
from app.core.ai_gateway.services.ai_models import update_model
from app.core.ai_gateway.services.health import check_in_flight
from app.core.ai_gateway.services.health import start_health_check
from app.core.ai_gateway.tasks import run_model_health_check
from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.bulk import BulkResponse
from app.core.bulk import apply_bulk
from app.core.config import Settings
from app.core.conversations.services.conversations import soft_delete_conversations_for_model
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.evaluations.services.assignments import unassign_models_for_model
from app.core.evaluations.services.evaluation_groups import assert_group_write_access
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.group_models import assignable_model_conditions
from app.core.evaluations.services.group_models import unassign_group_models_for_model
from app.core.exceptions import ForbiddenError
from app.core.logging import get_logger
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(prefix="/ai-models", tags=["models"])

logger = get_logger(__name__)

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_NOT_FOUND = problem_response("Model does not exist.")
_CONFLICT = problem_response("A model with this name, or this model_alias, already exists.")
_MISSING_ENDPOINT = problem_response("A generic provider requires an inference_endpoint.")
_NOT_RESTORABLE = problem_response(
    "No restorable model with this id: never deleted, or deleted longer than the restore window ago."
)


def _ai_model_snapshot(model: AiModel) -> dict[str, object]:
    """Curated snapshot for the audit before/after — NEVER the API key (plaintext or ciphertext).

    Deliberately without `capability_mismatch`, like the other system-set health columns: the trail
    records what the operator changed, and the clear that a PATCH triggers is inferable from the
    before/after of the fields it is keyed on.
    """
    return {
        "name": model.name,
        "description": model.description,
        "model_alias": model.model_alias,
        "provider": str(model.provider),
        "input_modalities": [str(m) for m in model.input_modalities],
        "output_modalities": [str(m) for m in model.output_modalities],
        "provider_model_id": model.provider_model_id,
        "endpoint_name": model.endpoint_name,
        "inference_endpoint": model.inference_endpoint,
        "is_disabled": model.is_disabled,
        "warmup_enabled": model.warmup_enabled,
        "advanced_params_disabled": model.advanced_params_disabled,
        "inactivity_alert_hours": model.inactivity_alert_hours,
        "icon_file": model.icon_file,
        "labels": list(model.labels),
    }


async def _autocheck_image_declaration(db: AsyncSession, settings: Settings, model: AiModel) -> None:
    """Start a health check for a row that has just declared image input, without being asked.

    After the write, never gating it: the endpoint may be legitimately asleep or throttled, and
    neither is a reason to refuse a registry row. Wired into the routes rather than
    `create_model`/`update_model` so the bulk endpoints cannot turn one paste into
    `bulk_max_rows` provider calls. An in-flight check is *superseded*, not deferred to —
    `start_health_check` re-stamps the CAS token, so the older run loses on settle rather than
    settling a finding for a pair a later PATCH has already replaced.
    """
    # A disabled row is never dispatched to, so the gap this closes cannot be reached from it.
    if model.is_disabled or Modality.IMAGE not in model.input_modalities:
        return
    if not has_resolvable_credential(model, settings) and model.inference_endpoint is None:
        # Nothing to call: no key on the row, none in the deployment's settings for this vendor, and
        # no endpoint of its own. The check could only settle `dead`, which the console renders as a
        # verdict about an endpoint nobody has configured yet — and creating the row first and
        # setting the key after is a supported flow. "Never checked" is the honest state meanwhile.
        # The manual button still probes such a row, for an operator who wants the answer anyway.
        return
    # Commit first: the registry write is the operator's, the check is our side effect, and only one
    # of them may fail. `@transactional` then commits the stamp below.
    await db.commit()
    try:
        # countdown so the worker sees the committed row (@transactional commits after this returns).
        run_model_health_check.apply_async(args=[str(model.id)], countdown=1)
    except Exception:
        # Stamped only once the task is on the broker. A stamp with no task behind it reads as a
        # check in flight, and `check_in_flight` would then turn the manual button into a silent
        # 200 no-op until the staleness horizon — so a broker outage would cost the operator the
        # override as well as the check. The manual route stamps first on purpose: it does not
        # swallow, so `@transactional` takes the stamp back.
        logger.exception("ai_model.autocheck_enqueue_failed", model_id=str(model.id))
        return
    await start_health_check(db, model)


@router.get(
    "",
    response_model=Page[AiModelResponse],
    status_code=status.HTTP_200_OK,
    summary="List models",
    description="Return a paginated slice of AI-model registry entries.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: problem_response(
            "The evaluation referenced by `assignable_to_evaluation` does not exist or is not visible to the caller."
        ),
    },
)
async def list_models_endpoint(
    caller: CurrentUserDep,
    filters: AiModelFiltersDep,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
) -> Page[AiModelResponse]:
    """List models.

    Reading the registry needs `models:read` — held globally, or object-scoped via
    an in-group role (the `owner` role grants it) on the group named by `for_group`
    or on the parent group of `assignable_to_evaluation`, so an owner can curate the
    subset and pick models to assign without the global permission. Disabled models
    are excluded by default; pass `include_disabled=true` (requires the global
    `models:update` permission) to surface them.

    Pass `assignable_to_evaluation=<id>` to restrict to models assignable to that
    evaluation — those in its parent group's allowed-model subset (an empty subset
    yields none) that are not already assigned to it.

    Pass `deleted=true` (requires `models:delete`) for the registry's tombstones inside
    the restore window, most-recently-deleted first — the set `POST /{model_id}/restore`
    can act on.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `models:read` globally and holds no in-group
      role granting it on `for_group` or on `assignable_to_evaluation`'s parent
      group; or requested `include_disabled=true` without `models:update`; or
      `deleted=true` without `models:delete`.
    * **404 Not Found** — `assignable_to_evaluation` or `for_group` references a group
      that does not exist or is not visible to the caller. `for_group` is consulted
      only for callers lacking global `models:read`; a global reader's `for_group` is
      ignored, so it never triggers this 404 for them.
    """
    can_manage = Permission.EVALUATION_GROUPS_MANAGE in caller.permissions
    # `assignable_to_evaluation` is cross-domain: resolve the evaluation up front (this
    # enforces read-visibility of its parent group — an invisible group reads as 404),
    # so it can both authorize the read and build the subset predicates.
    evaluation = (
        await get_evaluation(
            db, filters.assignable_to_evaluation, caller_id=caller.id, can_manage=can_manage, with_models=False
        )
        if filters.assignable_to_evaluation is not None
        else None
    )
    # Authorize the read: global `models:read`, or object-scoped `models:read` via an
    # in-group role (the `owner` role grants it, so an owner curates/assigns without the
    # global perm). When `assignable_to_evaluation` is set the result is scoped to that
    # evaluation's parent group, so authorization must target *that* group — it wins over
    # `for_group`; otherwise a caller could authorize against a group they own while the
    # result leaks another group's masked subset. `assert_group_write_access` checks
    # role-permission membership and gives the 403/404 visibility split for free.
    if Permission.MODELS_READ not in caller.permissions:
        scope_group_id = (evaluation.evaluation_group_id if evaluation else None) or filters.for_group
        if scope_group_id is None:
            raise ForbiddenError("Listing models requires the 'models:read' permission.")
        await assert_group_write_access(
            db,
            group_id=scope_group_id,
            caller_id=caller.id,
            can_manage=can_manage,
            permission=Permission.MODELS_READ,
            missing_message=f"Evaluation group {scope_group_id} not found.",
        )
    if filters.include_disabled and Permission.MODELS_UPDATE not in caller.permissions:
        raise ForbiddenError("Listing disabled models requires the 'models:update' permission.")
    # Same privileged-flag treatment as `include_disabled`: the tombstone list is the
    # restore surface, so it is reserved for callers who can restore from it.
    assert_may_list_deleted(filters.deleted, caller, Permission.MODELS_DELETE, entity="models")
    extra_conditions: list[ColumnElement[bool]] = (
        assignable_model_conditions(evaluation) if evaluation is not None else []
    )
    items, total = await list_models(
        db,
        filters=filters,
        extra_conditions=extra_conditions,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[AiModelResponse](
        items=[AiModelResponse.from_model(m) for m in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.post(
    "",
    response_model=AiModelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create model",
    description=(
        "Register a new model in the platform. `name` and `model_alias` must each be unique among live rows. "
        "A `generic` provider must carry an `inference_endpoint`, and any `inference_endpoint` "
        "must be an absolute http(s) URL. An optional `api_key` is encrypted at rest and never echoed back. "
        "An enabled row declaring `image` input additionally starts a health check on creation, so its "
        "declaration is verified against the endpoint without being asked; the row is created either way — "
        "a sleeping endpoint, a rate-limited one, or a broker outage never blocks the write. Three cases do "
        "not start one: disabled rows, the bulk endpoint, and a row with nothing to call yet — no `api_key`, "
        "no platform credential for its `provider`, and no `inference_endpoint` — which stays "
        '"never checked" rather than being reported dead. Setting a key does not by itself start one; '
        "the next edit or the manual check does."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _MISSING_ENDPOINT,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def create_model_endpoint(
    payload: AiModelCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_CREATE))],
    settings: SettingsDep,
    db: DbSession,
    response: Response,
) -> AiModelResponse:
    """Create a new model registry entry.

    ### Errors

    * **400 Bad Request** — `provider` is `generic` and `inference_endpoint` is missing.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:create` permission.
    * **409 Conflict** — a live model with the same `name`, or the same `model_alias`, exists.
    """
    model = await create_model(
        db,
        settings,
        name=payload.name,
        description=payload.description,
        model_alias=payload.model_alias,
        provider=payload.provider,
        input_modalities=payload.input_modalities,
        output_modalities=payload.output_modalities,
        provider_model_id=payload.provider_model_id,
        endpoint_name=payload.endpoint_name,
        inference_endpoint=payload.inference_endpoint,
        icon_file=payload.icon_file,
        labels=payload.labels,
        parameters=dump_inference_params(payload.parameters),
        extras=payload.extras,
        is_disabled=payload.is_disabled,
        warmup_enabled=payload.warmup_enabled,
        advanced_params_disabled=payload.advanced_params_disabled,
        inactivity_alert_hours=payload.inactivity_alert_hours,
        api_key=payload.api_key,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.AI_MODEL_CREATE,
        object_type="ai_model",
        object_id=model.id,
        after=_ai_model_snapshot(model),
    )
    await _autocheck_image_declaration(db, settings, model)
    response.headers["Location"] = f"/api/v1/ai-models/{model.id}"
    return AiModelResponse.from_model(model)


@router.post(
    "/bulk",
    response_model=BulkResponse[AiModelResponse],
    status_code=status.HTTP_200_OK,
    summary="Create models in bulk",
    description=(
        "Register up to `BULK_MAX_ROWS` models in one request, each row a full create payload "
        "(an optional per-row `api_key` is encrypted at rest and never echoed back). Always returns "
        "200 OK when the envelope is well-formed — per-row failures (`409` when a row's `name` "
        "or `model_alias` collides with an existing live model, `400` when a `generic` row omits its "
        "`inference_endpoint`) land inside `results[].error`. A `name` "
        "or `model_alias` repeated across rows is rejected upfront (`422`), like a duplicate `row_key` or a "
        "row whose `inference_endpoint` is not an absolute http(s) URL — a schema rejection fails the whole "
        "envelope, unlike the per-row outcomes above. "
        "`dry_run=true` previews the full processing path and rolls back instead of committing."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def bulk_create_models_endpoint(
    payload: AiModelBulkRequest,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_CREATE))],
    settings: SettingsDep,
    db: DbSession,
) -> BulkResponse[AiModelResponse]:
    """Create multiple models with per-row outcomes.

    The `models:create` permission is checked once at the envelope level — every
    row creates a model the same way. No `@transactional`: `apply_bulk` owns the
    transaction boundary (commit on success, rollback on `dry_run`), running each
    row in its own savepoint so a per-row `409` isolates to that row and the batch
    continues. A per-row `api_key` is encrypted inside `create_model`, exactly as
    on the single-create path.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:create` permission.
    * **422 Unprocessable Entity** — duplicate `row_key`, a `name` or `model_alias` repeated
      across rows, a row whose `inference_endpoint` is not an absolute http(s) URL, empty `rows`, or over
      `BULK_MAX_ROWS`.
    """

    async def processor(session: AsyncSession, data: AiModelCreate) -> AiModelResponse:
        model = await create_model(
            session,
            settings,
            name=data.name,
            description=data.description,
            model_alias=data.model_alias,
            provider=data.provider,
            input_modalities=data.input_modalities,
            output_modalities=data.output_modalities,
            provider_model_id=data.provider_model_id,
            endpoint_name=data.endpoint_name,
            inference_endpoint=data.inference_endpoint,
            icon_file=data.icon_file,
            labels=data.labels,
            parameters=dump_inference_params(data.parameters),
            extras=data.extras,
            is_disabled=data.is_disabled,
            warmup_enabled=data.warmup_enabled,
            advanced_params_disabled=data.advanced_params_disabled,
            inactivity_alert_hours=data.inactivity_alert_hours,
            api_key=data.api_key,
        )
        if not payload.dry_run:
            # Rides the row's savepoint (dry-run rolls it back anyway); the guard keeps a
            # previewed batch from flushing audit rows that never commit.
            await record_audit(
                session,
                actor_id=caller.id,
                actor_email=caller.email,
                action=AuditAction.AI_MODEL_CREATE,
                object_type="ai_model",
                object_id=model.id,
                after=_ai_model_snapshot(model),
            )
        return AiModelResponse.from_model(model)

    return await apply_bulk(db, payload, processor)


@router.post(
    "/api-keys/bulk",
    response_model=BulkResponse[AiModelResponse],
    status_code=status.HTTP_200_OK,
    summary="Set model API keys in bulk",
    description=(
        "Store or rotate the encrypted API key for up to `BULK_MAX_ROWS` models in one request, each row "
        "identifying its target by `name` — its unique business key — so no server-assigned ids need threading "
        "back. Plaintext keys are encrypted at rest and never echoed. Always returns 200 OK when the envelope is "
        "well-formed — per-row failures (`404` when no live model matches the row's `name`) land inside "
        "`results[].error`. A `name` repeated across rows is rejected upfront (`422`), like a duplicate `row_key`. "
        "Disabled models are valid targets. `dry_run=true` previews the full path (resolves + encrypts every row) "
        "and rolls back instead of committing."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def bulk_set_model_api_keys_endpoint(
    payload: AiModelApiKeyBulkRequest,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_UPDATE))],
    settings: SettingsDep,
    db: DbSession,
) -> BulkResponse[AiModelResponse]:
    """Store or rotate API keys for many models with per-row outcomes.

    The `models:update` permission is checked once at the envelope level. No
    `@transactional`: `apply_bulk` owns the transaction boundary (commit on
    success, rollback on `dry_run`), each row in its own savepoint so a per-row
    `404` isolates to that row and the batch continues. Each row targets its
    model by `name` (its business key); the plaintext key is encrypted inside
    `set_api_key` and never returned.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:update` permission.
    * **422 Unprocessable Entity** — duplicate `row_key`, a `name` repeated across rows,
      empty `rows`, or over `BULK_MAX_ROWS`.
    """

    async def processor(session: AsyncSession, data: AiModelApiKeyBulkItem) -> AiModelResponse:
        model = await get_model_by_name(session, name=data.name, for_update=True)
        await set_api_key(session, settings, model, data.api_key)
        if not payload.dry_run:
            # Event-only per row: the key value is never written to the trail, and each row's audit
            # write rides its own bulk savepoint, so a row that fails rolls back its own audit too.
            await record_audit(
                session,
                actor_id=caller.id,
                actor_email=caller.email,
                action=AuditAction.AI_MODEL_CREDENTIAL_SET,
                object_type="ai_model",
                object_id=model.id,
            )
        return AiModelResponse.from_model(model)

    return await apply_bulk(db, payload, processor)


# Declared before `/{model_id}`, or the UUID converter claims "labels" and answers 422.
@router.get(
    "/labels",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    summary="List labels in use",
    description=(
        "Every distinct label carried by a live model, sorted case-insensitively — the suggestion set "
        "for the label picker. There is no separate label catalog: a label exists exactly as long as "
        "some model wears it. Disabled models count, so disabling the last user of a label does not "
        "retire it — a deliberate divergence from the list route, where surfacing disabled rows needs "
        "`models:update`: a retired label is re-typed by hand, which is how near-duplicate spellings "
        "get in. Authorised by **global** `models:read` alone, with no `for_group` escape hatch, "
        "because the only caller is the admin-gated model form; a group-scoped consumer would need one."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_model_labels_endpoint(
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_READ))],
    db: DbSession,
) -> list[str]:
    """List every label in use across the registry.

    ### Implementation Notes

    Flat, not a `Page[T]`: this is the vocabulary the label picker suggests from, so a caller needs it
    whole — a page of it would suggest the wrong set. It is bounded by the fleet size times
    `MAX_LABELS`; a registry large enough for that to matter wants a server-side search parameter
    here, not pagination.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:read` permission.
    """
    return await list_labels(db)


@router.get(
    "/{model_id}",
    response_model=AiModelResponse,
    status_code=status.HTTP_200_OK,
    summary="Get model",
    description="Fetch one model by id.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_model_endpoint(
    model_id: UUID,
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_READ))],
    db: DbSession,
) -> AiModelResponse:
    """Fetch one model by id.

    A disabled model reads as missing (404) for callers who can't manage it —
    only holders of `models:update` may fetch a disabled row.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:read` permission.
    * **404 Not Found** — no model exists with the given id, or it is disabled
      and the caller lacks `models:update`.
    """
    can_manage = Permission.MODELS_UPDATE in _caller.permissions
    model = await get_model(db, model_id, include_disabled=can_manage)
    return AiModelResponse.from_model(model)


@router.post(
    "/{model_id}/health-check",
    response_model=AiModelResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a health check",
    description=(
        "Probe the model endpoint for liveness (async — waits out a scale-to-zero cold start). "
        "A row declaring `image` input whose endpoint served the liveness probe is additionally probed "
        "with an image payload, which is a second billable inference call; a dead, still-cold or "
        "throttled endpoint is not. The outcome lands in `capability_mismatch` and never changes "
        "`health_check_status`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_200_OK: {
            "model": AiModelResponse,
            "description": "A check is already in flight; the current model is returned unchanged.",
        },
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def health_check_model_endpoint(
    model_id: UUID,
    _caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_UPDATE))],
    db: DbSession,
    settings: SettingsDep,
    response: Response,
) -> AiModelResponse:
    """Start (or return the in-flight) endpoint health check for a model.

    A fresh `checking` check is idempotent (`200`, no new task); otherwise a new check is started
    (`202`) and a Celery task probes the endpoint, waiting out a cold start. Disabled models are
    checkable (verify an endpoint before enabling it).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:update` permission.
    * **404 Not Found** — no model exists with the given id.
    """
    # FOR UPDATE so two concurrent POSTs can't both miss `check_in_flight` and enqueue duplicate probes —
    # the second blocks, then re-reads the committed `checking` row and returns idempotently.
    model = await get_model(db, model_id, for_update=True, include_disabled=True)
    if check_in_flight(model, settings):
        response.status_code = status.HTTP_200_OK  # idempotent — a check is already running
        return AiModelResponse.from_model(model)
    await start_health_check(db, model)
    # countdown so the worker sees the committed `checking` row (@transactional commits after this returns).
    run_model_health_check.apply_async(args=[str(model.id)], countdown=1)
    return AiModelResponse.from_model(model)


@router.patch(
    "/{model_id}",
    response_model=AiModelResponse,
    status_code=status.HTTP_200_OK,
    summary="Update model",
    description=(
        "Partially update a model. Omitted fields are left unchanged; explicit `null` clears the nullable columns. "
        "A patch that touches `provider` or `inference_endpoint` must leave the row with an `inference_endpoint` "
        "if it is `generic`, and a supplied `inference_endpoint` must be an absolute http(s) URL. Changing any of "
        "`input_modalities`, `inference_endpoint`, `provider` or `provider_model_id` clears `capability_mismatch` "
        "— the finding described the previous combination — and on an enabled row declaring `image` input it also "
        "starts a health check, superseding one already in flight, so the new combination is verified without "
        "being asked. Re-enabling a disabled row does the same. Re-sending an unchanged value does not, and "
        "neither does a row with nothing to call yet, as on create. "
        "The API key is rotated/cleared via the dedicated `…/api-key` endpoints, not this body."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: _MISSING_ENDPOINT,
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: _CONFLICT,
    },
)
@transactional
async def update_model_endpoint(
    model_id: UUID,
    payload: AiModelUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_UPDATE))],
    settings: SettingsDep,
    db: DbSession,
) -> AiModelResponse:
    """Partially update a model.

    ### Errors

    * **400 Bad Request** — the patch leaves a `generic` row without an `inference_endpoint`.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:update` permission.
    * **404 Not Found** — no model exists with the given id.
    * **409 Conflict** — the new `name` or `model_alias` collides with a live row.
    """
    model = await get_model(db, model_id, for_update=True)
    before = _ai_model_snapshot(model)
    # extras/parameters are auth/call-shape + large JSONB we deliberately keep out of the curated
    # snapshot; capture them so a PATCH confined to them still audits the *event* (not the values).
    # `update_model` reassigns these attributes (never mutates in place), so a plain reference is a
    # valid before-image — no copy needed.
    opaque_before = (model.extras, model.parameters)
    data = payload.model_dump(exclude_unset=True)
    if payload.parameters is not None:
        # Two-state cascade: a key present overrides, a key absent inherits.
        # `exclude_unset` recurses into the nested model, so a knob set to
        # `null` would otherwise survive into JSONB; re-dump with `exclude_none`
        # to strip it — matching the create path so storage never holds a
        # `null` knob.
        data["parameters"] = dump_inference_params(payload.parameters)
    changes = AiModelUpdateChanges.model_validate(data)
    updated = await update_model(db, model, changes)
    diff_before, diff_after = changed_fields(before, _ai_model_snapshot(updated))
    # Which opaque (kept-out-of-the-trail) fields moved — names only, never values. Carried as a
    # context signal so the trail flags that extras/parameters changed even in a *mixed* PATCH where
    # a curated diff is the row written (otherwise that row would hide the opaque change).
    opaque_changed_fields = [
        name
        for name, was in (("extras", opaque_before[0]), ("parameters", opaque_before[1]))
        if getattr(updated, name) != was
    ]
    opaque_context = {"opaque_fields_changed": opaque_changed_fields} if opaque_changed_fields else None
    if diff_before or diff_after:
        # A no-op PATCH (payload matches current state) changes nothing — skip the row
        # rather than log a misleading update with empty before/after diffs.
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.AI_MODEL_UPDATE,
            object_type="ai_model",
            object_id=updated.id,
            before=diff_before,
            after=diff_after,
            context=opaque_context,
        )
    elif opaque_changed_fields:
        # The change was confined to extras/parameters: an event-only row naming which opaque
        # field(s) moved (a change to provider auth/call-shape is security-relevant), no values.
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.AI_MODEL_UPDATE,
            object_type="ai_model",
            object_id=updated.id,
            context=opaque_context,
        )
    # `changed_fields` is value-based, so this fires on a declaration that actually moved — not on
    # the form merely resending it. Any part of the probed call moving makes it an unverified
    # combination again; the check's own result then overwrites (or clears) any finding left over
    # from the old one. A re-enable counts too: the row was skipped while disabled, so this is the
    # moment its declaration starts mattering to dispatch.
    if set(PROBED_CALL_FIELDS) & diff_after.keys() or diff_after.get("is_disabled") is False:
        await _autocheck_image_declaration(db, settings, updated)
    return AiModelResponse.from_model(updated)


@router.put(
    "/{model_id}/api-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set model API key",
    description="Store or rotate the encrypted API key for this model. The plaintext value is never returned.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def set_model_api_key_endpoint(
    model_id: UUID,
    payload: AiModelApiKeyUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_UPDATE))],
    settings: SettingsDep,
    db: DbSession,
) -> Response:
    """Store or rotate the API key.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:update` permission.
    * **404 Not Found** — no model exists with the given id.
    """
    model = await get_model(db, model_id, for_update=True)
    await set_api_key(db, settings, model, payload.api_key)
    # Event-only: the credential itself (plaintext or ciphertext) never enters the audit row.
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.AI_MODEL_CREDENTIAL_SET,
        object_type="ai_model",
        object_id=model.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{model_id}/api-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear model API key",
    description="Remove the stored credential. Idempotent — succeeds whether or not a key was set.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def clear_model_api_key_endpoint(
    model_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_UPDATE))],
    db: DbSession,
) -> Response:
    """Clear the stored API key.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:update` permission.
    * **404 Not Found** — no model exists with the given id.
    """
    model = await get_model(db, model_id, for_update=True)
    await clear_api_key(db, model)
    # Event-only: records that the credential was cleared, never a key value.
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.AI_MODEL_CREDENTIAL_CLEAR,
        object_type="ai_model",
        object_id=model.id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{model_id}/restore",
    response_model=AiModelResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore model",
    description=(
        "Clear a soft-deleted model's `deleted_at`, returning it to the registry. Restorable for a "
        "fixed window after the delete (set per deployment). **Shallow**: the "
        "evaluation assignments and group subset rows the delete removed are *not* restored, and "
        "neither are the conversations that ran against them — re-assign the model instead. "
        "Conflicts when a live model has taken this row's `name` or `model_alias` since the delete."
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
async def restore_model_endpoint(
    model_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_DELETE))],
    db: DbSession,
    settings: SettingsDep,
) -> AiModelResponse:
    """Restore a soft-deleted model.

    Gated on `models:delete` — undoing your own delete needs no authority beyond the
    delete itself. The registry has no per-user ownership, so any holder restores any
    tombstone inside the window.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:delete` permission.
    * **404 Not Found** — no restorable model: never deleted, or deleted longer than
      the restore window ago.
    * **409 Conflict** — a live model has taken this row's `name` or `model_alias`.
    """
    model = await get_restorable_model(db, model_id, deleted_cutoff=restore_cutoff(settings))
    restored = await restore_model(db, model)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.AI_MODEL_RESTORE,
        object_type="ai_model",
        object_id=restored.id,
        after=_ai_model_snapshot(restored),
    )
    return AiModelResponse.from_model(restored)


@router.delete(
    "/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete model",
    description="Soft-delete a model (sets `deleted_at`). Cascades to unassign it from any evaluations.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def delete_model_endpoint(
    model_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.MODELS_DELETE))],
    db: DbSession,
) -> Response:
    """Soft-delete a model.

    Sets `deleted_at` on the row; subsequent reads exclude it. Live assignments
    of this model to evaluations are soft-deleted in the same transaction, as are
    its evaluation-group allowed-model subset rows, so a tombstoned model never
    lingers in an evaluation or a group's subset, and conversations that chose one
    of those assignments are soft-deleted too. Returns `204 No Content`.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `models:delete` permission.
    * **404 Not Found** — no model exists with the given id.
    """
    model = await get_model(db, model_id, for_update=True)
    before = _ai_model_snapshot(model)
    await soft_delete_model(db, model, by_id=caller.id)
    await unassign_models_for_model(db, model_id, by_id=caller.id)
    await unassign_group_models_for_model(db, model_id, by_id=caller.id)
    # Cascade one level further: conversations that chose any of those assignments.
    await soft_delete_conversations_for_model(db, model_id, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.AI_MODEL_DELETE,
        object_type="ai_model",
        object_id=model_id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
