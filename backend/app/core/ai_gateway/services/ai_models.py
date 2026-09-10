"""AI-model CRUD service — pure async functions over an `AsyncSession`.

Routers stay thin: they raise `APIError` subclasses for known failure modes
and let the error handler in `app.core.error_handlers` translate them into
RFC 7807 responses. Soft-deleted rows are filtered per-statement via the
`AiModel.live_*` factories on `BaseModel` — except `list_labels`, which selects a
scalar function rather than the entity and so spells the predicate out.

Secrets are wrapped via `app.core.ai_gateway.crypto` at the service boundary
so callers — including future bulk-import paths — never see a plaintext key
sitting on a row attribute.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.ai_gateway.crypto import decrypt_secret
from app.core.ai_gateway.crypto import encrypt_secret
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.filters import AiModelFilters
from app.core.ai_gateway.models import PROBED_CALL_FIELDS
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.schemas import AiModelUpdateChanges
from app.core.config import Settings
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.schemas import ProblemErrorItem

# Both vendors are missing from `_PROVIDER_DEFAULT_KEY_FIELD` for opposite reasons, and only this
# one means "a credential exists, just not on `Settings`". A test pins the split against
# `ProviderVendor`, so a new vendor has to declare which side it is on.
_OUT_OF_BAND_CREDENTIAL_VENDORS = frozenset({ProviderVendor.AWS_BEDROCK})

# Providers whose default credential is read from a `<provider>_api_key`
# field on `Settings`. Providers missing here have no env-var fallback —
# their `AiModel` row must carry its own `api_key_encrypted`, or the
# credential is supplied out-of-band (IAM for `aws_bedrock`, ad-hoc for
# `generic`). Keep in sync with the matching fields on `Settings`.
_PROVIDER_DEFAULT_KEY_FIELD: dict[ProviderVendor, str] = {
    ProviderVendor.HUGGINGFACE: "huggingface_api_key",
    ProviderVendor.GOOGLE: "google_api_key",
    ProviderVendor.OPENAI: "openai_api_key",
    ProviderVendor.ANTHROPIC: "anthropic_api_key",
    ProviderVendor.AZURE: "azure_api_key",
    ProviderVendor.COHERE: "cohere_api_key",
}

_DUPLICATE_FIELD_BY_CONSTRAINT: dict[str, str] = {
    "ix_ai_models_name": "name",
    "ix_ai_models_model_alias": "model_alias",
}

_UNIQUE_VIOLATION_SQLSTATE = "23505"


def _duplicate_conflict(exc: IntegrityError) -> ConflictError:
    """Build a `ConflictError` naming the specific column that collided.

    `exc.orig` is SQLAlchemy's asyncpg DBAPI wrapper (this codebase's async
    runtime is asyncpg, not psycopg) — it carries `sqlstate`/`pgcode` directly,
    but not `constraint_name`. The dialect chains the real `asyncpg.exceptions.*`
    object via `raise translated_error from error`, so `constraint_name`
    lives on `exc.orig.__cause__`. A name match alone isn't enough to trust:
    Postgres scopes constraint names and index names to separate namespaces,
    so a future CHECK/FK constraint could collide with one of this map's keys
    without either creation failing — gating on `sqlstate` first keeps such a
    constraint on the generic fallback instead of a name it never violated.
    A recognized unique-constraint name gets a field-addressable `errors[]`
    entry (mirrors `PasswordPolicyError`) so a form can show an inline error
    instead of a generic toast; anything else (unrecognized constraint, a
    non-unique violation, or a future constraint this map hasn't caught up
    with) falls back to the old undifferentiated message.
    """
    driver_exc = exc.orig.__cause__ if exc.orig is not None else None
    constraint = getattr(driver_exc, "constraint_name", None)
    is_unique_violation = getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE
    if (
        not is_unique_violation
        or constraint is None
        or (field := _DUPLICATE_FIELD_BY_CONSTRAINT.get(constraint)) is None
    ):
        return ConflictError("A model with this name, or this model_alias, already exists.")
    detail = f"A model with this {field} already exists."
    conflict = ConflictError(detail)
    conflict.errors = [ProblemErrorItem(loc=["body", field], msg=detail, type=f"duplicate_{field}")]
    return conflict


class MissingInferenceEndpointError(BadRequestError):
    """A `generic` row carries no `inference_endpoint` — 400.

    `generic` routes through litellm's `openai` prefix with a caller-supplied
    `api_base` (see `ai_gateway.dispatch`), so without the URL the row can
    never dispatch. The other vendors resolve a base from the prefix alone, or
    outside `Settings` entirely — `azure`'s base is read from `AZURE_API_BASE`
    by litellm itself, not by us — so the rule is scoped to `generic`.

    **The rule is advisory, not an invariant.** It is enforced on writes and at
    dispatch; there is no `CHECK` constraint and no backfill, so a row predating
    it may still hold a NULL endpoint. Do not read it as a guarantee about the
    table.

    **Presence is the whole rule; the URL is only scheme-checked** (`http(s)`,
    at the edge schemas). Deliberately not the `data:`-pinning `chat.ImageUrl`
    gets: that one is reachable by any caller of `/chat/stream`, while writing
    here needs `models:create`/`models:update`, and `generic` has no
    `_PROVIDER_DEFAULT_KEY_FIELD` entry — so a named host receives no platform
    credential, only the key an admin stored on that row.

    Carries a field-addressable `errors[]` entry (the shape a 422 uses) so a
    client maps it onto the form field, mirroring `PasswordPolicyError`.
    """

    _DETAIL = "A generic provider has no vendor-hosted base URL, so inference_endpoint is required."

    def __init__(self) -> None:
        super().__init__(self._DETAIL)
        self.errors = [
            ProblemErrorItem(loc=["body", "inference_endpoint"], msg=self._DETAIL, type="inference_endpoint_required")
        ]


def missing_inference_endpoint(provider: ProviderVendor, inference_endpoint: str | None) -> bool:
    """Whether the pair leaves the row with no base URL to call.

    Shared with the dispatch gate so the write rule and the runtime one cannot
    drift apart. Whitespace is not a URL — a blank-but-present value is as
    unusable as a NULL one.
    """
    return provider is ProviderVendor.GENERIC and not (inference_endpoint or "").strip()


def _require_inference_endpoint(provider: ProviderVendor, inference_endpoint: str | None) -> None:
    if missing_inference_endpoint(provider, inference_endpoint):
        raise MissingInferenceEndpointError


async def create_model(  # noqa: PLR0913 — keyword-only args mirror the row's column set; bundling them into a Pydantic carrier would just shift the surface area
    session: AsyncSession,
    settings: Settings,
    *,
    name: str,
    description: str | None = None,
    model_alias: str,
    provider: ProviderVendor,
    provider_model_id: str,
    input_modalities: list[Modality] | None = None,
    output_modalities: list[Modality] | None = None,
    endpoint_name: str | None = None,
    inference_endpoint: str | None = None,
    icon_file: str | None = None,
    labels: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
    is_disabled: bool = False,
    warmup_enabled: bool = False,
    advanced_params_disabled: bool = False,
    inactivity_alert_hours: int | None = None,
    api_key: SecretStr | None = None,
) -> AiModel:
    """Create and persist a new `AiModel`.

    Args:
        session: Async DB session bound to the request.
        settings: Application settings — used to derive the at-rest
            encryption key when ``api_key`` is supplied.
        name: Display name. Must be unique among live (non-soft-deleted) rows.
        model_alias: Stable slug. Must be unique among live rows.
        provider: Vendor whose API this row routes to.
        provider_model_id: Identifier the provider's API expects.
        description: Admin-authored note carried on the registry row.
        input_modalities: What the model accepts in a prompt; defaults to text only.
        output_modalities: What the model can return; defaults to text only.
        endpoint_name: Provider-side inference-endpoint admin name.
        inference_endpoint: Custom inference URL, when the row points at one.
        icon_file: FE icon hint.
        labels: Operator annotations, already canonicalised by the request schema.
        parameters: Default inference parameters (dict-typed; encoded as
            JSONB on the row).
        extras: Provider-specific blob (encoded as JSONB).
        is_disabled: Set true to mark the row disabled at creation — stamps
            ``disabled_at`` so the gateway will not dispatch to it.
        warmup_enabled: Set true for a scale-to-zero endpoint so clients warm it
            before the first message.
        advanced_params_disabled: Set true to opt the model out of the inference-params
            cascade — stored knobs are kept but never dispatched.
        inactivity_alert_hours: Quiet hours after which admins are alerted the model
            may be idling warm; ``None`` disables the alert.
        api_key: Optional plaintext credential — encrypted before persisting.

    Raises:
        ConflictError: If ``name`` or ``model_alias`` is already taken.
        MissingInferenceEndpointError: If ``provider`` is ``generic`` and no
            ``inference_endpoint`` is given.
    """
    _require_inference_endpoint(provider, inference_endpoint)
    model = AiModel(
        name=name,
        description=description,
        model_alias=model_alias,
        provider=provider,
        input_modalities=input_modalities or [Modality.TEXT],
        output_modalities=output_modalities or [Modality.TEXT],
        provider_model_id=provider_model_id,
        endpoint_name=endpoint_name,
        inference_endpoint=inference_endpoint,
        icon_file=icon_file,
        labels=list(labels) if labels is not None else [],
        parameters=dict(parameters) if parameters is not None else {},
        extras=dict(extras) if extras is not None else {},
        warmup_enabled=warmup_enabled,
        advanced_params_disabled=advanced_params_disabled,
        inactivity_alert_hours=inactivity_alert_hours,
        api_key_encrypted=encrypt_secret(api_key.get_secret_value(), settings) if api_key is not None else None,
    )
    if is_disabled:
        model.disable()
    session.add(model)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _duplicate_conflict(exc) from exc
    await session.refresh(model, attribute_names=["created_at", "updated_at"])
    return model


async def get_model(
    session: AsyncSession,
    model_id: UUID,
    *,
    for_update: bool = False,
    include_disabled: bool = True,
) -> AiModel:
    """Fetch one live (non-soft-deleted) `AiModel` by id.

    Args:
        session: Async DB session bound to the request.
        model_id: Primary key of the row to fetch.
        for_update: Append `FOR UPDATE` so the row lock is taken with the
            read — use when the caller will mutate this row later in the
            same transaction.
        include_disabled: When False, a disabled row reads as missing
            (raises `NotFoundError`) — read endpoints pass False to hide
            disabled models from callers who can't manage them. Defaults to
            True so management paths (update, delete, key rotation) can
            always resolve a disabled row to re-enable it.

    Raises:
        NotFoundError: If no live row matches ``model_id`` (or it is disabled
            and ``include_disabled`` is False).
    """
    statement = AiModel.live_select().where(col(AiModel.id) == model_id)
    if not include_disabled:
        statement = statement.where(col(AiModel.disabled_at).is_(None))
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    model = result.scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"Model {model_id} not found.")
    return model


async def get_model_by_name(
    session: AsyncSession,
    *,
    name: str,
    for_update: bool = False,
) -> AiModel:
    """Fetch one live `AiModel` by name.

    `name` alone is unique among live rows (`ix_ai_models_name`). Disabled rows
    are included — a credential may be pre-loaded before a model is enabled,
    mirroring `get_model`'s default.

    Args:
        session: Async DB session bound to the request.
        name: Display name of the target row.
        for_update: Append `FOR UPDATE` to lock the row for a follow-up write in
            the same transaction (e.g. a key rotation).

    Raises:
        NotFoundError: If no live row matches ``name``.
    """
    statement = AiModel.live_select().where(col(AiModel.name) == name)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    model = result.scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"Model {name!r} not found.")
    return model


def _apply_ai_model_filters(statement: Select[tuple[AiModel]], filters: AiModelFilters) -> Select[tuple[AiModel]]:
    """Append a `WHERE` per supplied filter; leave omitted ones alone.

    `include_disabled` is the inverse of a constraint: the default (False)
    *adds* a `disabled_at IS NULL` clause; True drops it so disabled rows are
    returned alongside enabled ones.
    """
    if not filters.include_disabled:
        statement = statement.where(col(AiModel.disabled_at).is_(None))
    return statement


async def list_models(
    session: AsyncSession,
    *,
    filters: AiModelFilters,
    extra_conditions: Sequence[ColumnElement[bool]] = (),
    order_by: str = "created_at",
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[AiModel], int]:
    """Return one page of live `AiModel` rows matching ``filters``.

    ``extra_conditions`` are additional WHERE predicates the caller supplies for
    cross-domain filters this module can't own without importing other domains
    (e.g. `assignable_to_evaluation`, built by `evaluations.services.group_models`).

    ``filters.deleted`` swaps the live set for the tombstones still inside the
    restore window, most-recently-deleted first (this list has no client `order_by`,
    so creation order would be the wrong reading). Not scoped to one deleter: the
    registry has no per-user ownership, and the route reserves the flag for
    `models:delete` holders — every caller who can see a tombstone can restore it.
    ``deleted_cutoff`` comes from the route, like `get_restorable_model`'s — reaching for
    the global settings here would let the listing and the restore disagree on the window.
    """
    if filters.deleted:
        statement = deleted_select(AiModel, deleted_cutoff, deleted_by=None)
        order_by = "-deleted_at"
    else:
        statement = _apply_ai_model_filters(AiModel.live_select(), filters)
    for condition in extra_conditions:
        statement = statement.where(condition)
    statement = apply_order_by(statement, AiModel, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def get_restorable_model(session: AsyncSession, model_id: UUID, *, deleted_cutoff: datetime) -> AiModel:
    """Fetch the tombstoned model ``model_id`` a caller may restore.

    Outside the window or never deleted reads as missing, mirroring `get_model`
    over the tombstones. No deleter scope — see `list_models`.

    Raises:
        NotFoundError: If no such restorable model exists.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(AiModel, deleted_cutoff, deleted_by=None)
        .where(col(AiModel.id) == model_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    model = (await session.execute(statement)).scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"No restorable model {model_id} was deleted within the restore window.")
    return model


async def restore_model(session: AsyncSession, model: AiModel) -> AiModel:
    """Clear ``model``'s tombstone, leaving everything its delete cascaded to deleted.

    The delete unassigned the model from every evaluation and group subset and
    soft-deleted the conversations that chose those assignments. Restore is
    deliberately **shallow** — it brings the registry row back so the model can be
    assigned again; it does not revive the old assignments, and those conversations
    stay unrestorable because their assignment is still tombstoned (see
    `conversations._join_live_assignment`). Re-assigning the model is the forward
    action, and restoring an individual assignment is its own endpoint.

    Raises:
        ConflictError: If a live model has taken this row's `name` or `model_alias`
            since the delete — both are unique among live rows only.
    """
    for field, value in (("name", model.name), ("model_alias", model.model_alias)):
        clash = await session.scalar(AiModel.live_select().where(col(getattr(AiModel, field)) == value).limit(1))
        if clash is not None:
            raise ConflictError(f"A live model already uses the {field} {value!r}; rename it and retry.")
    await restore_row(session, model, conflict_message="Model cannot be restored.")
    await session.refresh(model)
    return model


async def list_labels(session: AsyncSession) -> list[str]:
    """Every distinct label carried by a live model, sorted case-insensitively.

    There is no label catalog, so this *is* the vocabulary a console offers — which is why it counts
    disabled rows too: disabling the last model wearing a label would otherwise retire the label, and
    an operator re-typing it by hand is how near-duplicate spellings get in. Distinct is exact, so two
    spellings differing only in case do both appear (they can only arise across models — one model's
    set is deduped case-insensitively at the schema edge).
    """
    # `unnest` sits in the columns clause, not as a FROM element: `column_valued()` renders the
    # function ahead of `ai_models` in the FROM list, which Postgres rejects outright as a missing
    # FROM-clause entry. Ordering is left to Python because `SELECT DISTINCT … ORDER BY lower(label)`
    # is invalid — the sort expression is not in the select list — and the set is small enough that
    # sorting it here costs nothing.
    result = await session.execute(
        select(func.unnest(col(AiModel.labels))).where(col(AiModel.deleted_at).is_(None)).distinct()
    )
    return sorted(result.scalars().all(), key=str.casefold)


def _probed_call(model: AiModel) -> tuple[object, ...]:
    """The values a capability finding is evidence about, as of now."""
    return tuple(getattr(model, field) for field in PROBED_CALL_FIELDS)


async def update_model(session: AsyncSession, model: AiModel, changes: AiModelUpdateChanges) -> AiModel:
    """Apply ``changes`` to ``model`` and persist them.

    Writes only fields in `changes.model_fields_set` — omitted fields stay,
    explicit `None` clears the nullable columns. `is_disabled` is a derived
    property over `disabled_at`, so it's routed through `disable()`/`enable()`
    rather than `setattr`.

    Raises:
        ConflictError: If the change collides with another live row on
            ``name`` or ``model_alias``.
        MissingInferenceEndpointError: If the row ends up ``generic`` without an
            ``inference_endpoint`` — whether the patch set the provider, cleared
            the URL, or both.

    A patch that *changes* any of ``PROBED_CALL_FIELDS`` clears ``capability_mismatch``: the
    finding is evidence about the exact call the probe made, and only a fresh health check can
    speak for a new one. Keyed on the values, not on the fields' presence in the patch — the
    console's edit form sends the whole body, so presence would let a rename discard the finding.
    """
    probed_call_before = _probed_call(model)
    for field in changes.model_fields_set:
        if field == "is_disabled":
            if changes.is_disabled:
                model.disable()
            else:
                model.enable()
            continue
        # Restoring any knob that disarmed the sweep (`inactivity._inactive_predicate`) is a
        # fresh decision, like a re-enable: without this, a stamp left by the silenced episode
        # suppresses every future alert. Read before the `setattr` — these compare old to new.
        if (
            field == "inactivity_alert_hours"
            and model.inactivity_alert_hours is None
            and changes.inactivity_alert_hours is not None
        ):
            model.inactivity_alerted_at = None
        if field == "warmup_enabled" and not model.warmup_enabled and changes.warmup_enabled:
            model.inactivity_alerted_at = None
        setattr(model, field, getattr(changes, field))
    # Checked on the merged row (a partial patch may supply either half of the pair),
    # but only when the patch touches one — an unrelated edit must stay possible on a
    # legacy row that predates the rule.
    if {"provider", "inference_endpoint"} & changes.model_fields_set:
        _require_inference_endpoint(model.provider, model.inference_endpoint)
    # A recorded mismatch is an accusation against one specific call; once any part of it moves
    # the accusation is stale, and stale here reads as current. Not left to the follow-up check:
    # the usual reason to move an endpoint is that the old one was broken, and a probe against a
    # URL that is not up yet is inconclusive by design — so waiting for it would display endpoint
    # A's accusation as endpoint B's.
    if _probed_call(model) != probed_call_before:
        model.capability_mismatch = None
    session.add(model)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _duplicate_conflict(exc) from exc
    await session.refresh(model, attribute_names=["updated_at"])
    return model


async def set_api_key(session: AsyncSession, settings: Settings, model: AiModel, api_key: SecretStr) -> AiModel:
    """Encrypt ``api_key`` and store it on ``model``."""
    model.api_key_encrypted = encrypt_secret(api_key.get_secret_value(), settings)
    session.add(model)
    await session.flush()
    await session.refresh(model, attribute_names=["updated_at"])
    return model


async def clear_api_key(session: AsyncSession, model: AiModel) -> AiModel:
    """Remove the stored credential from ``model`` (no-op if already absent)."""
    if model.api_key_encrypted is None:
        return model
    model.api_key_encrypted = None
    session.add(model)
    await session.flush()
    await session.refresh(model, attribute_names=["updated_at"])
    return model


def resolve_api_key(model: AiModel, settings: Settings) -> SecretStr | None:
    """Return the credential the gateway should use when calling ``model``'s provider.

    Precedence: the model's own `api_key_encrypted` wins when set — it
    decrypts here and the env-var fallback is never consulted. Only when
    the row has no stored credential do we fall back to the matching
    ``<provider>_api_key`` field on `Settings`. Providers without a
    fallback field (e.g. `aws_bedrock`) return `None`, signalling to the
    caller that the credential must be supplied out-of-band.

    Decryption failures surface verbatim — silently using the env-var key
    when the at-rest key has rotated would route traffic with the wrong
    credential, which is worse than a hard failure. The dispatch boundary
    catches `SecretDecryptError` and maps it to `ProviderAuthError`
    (credential present but unreadable — a terminal auth fault) without
    leaking the JOSE cause.
    """
    if model.api_key_encrypted is not None:
        return SecretStr(decrypt_secret(model.api_key_encrypted, settings))
    field = _PROVIDER_DEFAULT_KEY_FIELD.get(model.provider)
    if field is None:
        return None
    return getattr(settings, field)


def has_resolvable_credential(model: AiModel, settings: Settings) -> bool:
    """True when *something* can authenticate a call to ``model`` — without decrypting anything.

    Deliberately not `resolve_api_key(...) is not None`. That decrypts, so it raises
    `SecretDecryptError` on a rotated at-rest key, and a missing key is not a reason to refuse
    the operator's write. It also cannot tell the two vendors absent from
    `_PROVIDER_DEFAULT_KEY_FIELD` apart, and they mean opposite things: `aws_bedrock` gets its
    credential from the worker's environment, while `generic` gets none at all beyond what an
    admin stored on the row.
    """
    if model.api_key_encrypted is not None:
        return True
    if model.provider in _OUT_OF_BAND_CREDENTIAL_VENDORS:
        return True
    field = _PROVIDER_DEFAULT_KEY_FIELD.get(model.provider)
    if field is None:
        return False
    secret = getattr(settings, field)
    # A blank-but-present setting is as unusable as an absent one — the same rule
    # `missing_inference_endpoint` applies to a whitespace URL.
    return secret is not None and bool(secret.get_secret_value().strip())


async def soft_delete_model(session: AsyncSession, model: AiModel, *, by_id: UUID) -> AiModel:
    """Mark ``model`` as soft-deleted by stamping `deleted_at`.

    Idempotent in effect — a second call overwrites `deleted_at` with a newer
    timestamp rather than raising; callers that need "already deleted"
    detection should check the original value before calling.
    """
    model.soft_delete(by_id)
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model
