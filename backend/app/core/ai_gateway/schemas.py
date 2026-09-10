"""Request and response schemas for the `/v1/ai-models` router."""

from datetime import datetime
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Any
from uuid import UUID

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import ValidationError
from pydantic import field_validator
from pydantic import model_validator

from app.core.ai_gateway.enums import HealthCheckStatus
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.inference_params import InferenceParams
from app.core.bulk import BulkRequest
from app.core.helpers import sanitise_single_line
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.ai_gateway.models import AiModel

logger = get_logger(__name__)


def _canonicalise(value: list[Modality]) -> list[Modality]:
    # Order and repetition carry no meaning in a capability set, so collapse both:
    # one declaration must have exactly one stored form, or equal rows render
    # differently and export differently.
    return [member for member in Modality if member in value]


def _canonical_input(value: list[Modality]) -> list[Modality]:
    canonical = _canonicalise(value)
    if Modality.TEXT not in canonical:
        raise ValueError("must include 'text' — every dispatched call carries a text prompt")
    return canonical


InputModalities = Annotated[list[Modality], AfterValidator(_canonical_input)]
OutputModalities = Annotated[list[Modality], Field(min_length=1), AfterValidator(_canonicalise)]


def _normalise_note(value: str | None) -> str | None:
    # Blank is not a note, so the column holds one form of "unset" and a reader needs no trim.
    # Surrounding whitespace of a pasted note is noise, stripped like `inference_endpoint`'s.
    # Runs only on a supplied value, so an omitted field still means "unchanged" on a PATCH.
    if value is None:
        return None
    return value.strip() or None


# Bounded like every other string on these schemas — the column is unbounded `Text` and the value
# is copied into the append-only audit snapshot on each edit, so nothing downstream caps it.
# 2000 matches `rejection_reason`, the other free-prose field in the API.
AdminNote = Annotated[str | None, Field(max_length=2000), AfterValidator(_normalise_note)]


MAX_LABELS = 20
MAX_LABEL_LENGTH = 64


def _canonical_labels(value: list[str]) -> list[str]:
    # Same one-stored-form-per-declaration rule as the modality sets, but over free text, so it also
    # has to normalise: `sanitise_single_line` collapses whitespace and strips the invisible categories
    # (a label must read as one visible line for the same reason a chip must), then case-only
    # duplicates collapse to their first spelling.
    #
    # Both maxima are checked on what was *submitted*, which is what the published `maxItems` and
    # `maxLength` constrain — the count before any per-item work, so an oversized payload is refused
    # without running the sanitiser over every entry. Consequence: entries that would have collapsed
    # into each other still count, so 25 spellings of 18 labels is refused rather than deduped under
    # the cap. A client that dedupes its own chips never submits one.
    if len(value) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} labels allowed")
    canonical: dict[str, str] = {}
    for raw in value:
        if len(raw) > MAX_LABEL_LENGTH:
            msg = f"label {raw!r} exceeds {MAX_LABEL_LENGTH} characters"
            raise ValueError(msg)
        label = sanitise_single_line(raw)
        if not label:
            # `repr` because an entry made of invisible categories has no other readable form.
            msg = f"label {raw!r} normalises to nothing"
            raise ValueError(msg)
        canonical.setdefault(label.casefold(), label)
    return sorted(canonical.values(), key=str.casefold)


# Both maxima ride into the published schema so a client can check them before sending. Validation
# stays on the whole field rather than per item: a per-item `Field(max_length=...)` would key the
# 422 as `('labels', 0)`, and the console drops locs ending in an index, so an over-long label would
# fail silently instead of landing on the field.
Labels = Annotated[
    list[str],
    AfterValidator(_canonical_labels),
    Field(
        json_schema_extra={
            "maxItems": MAX_LABELS,
            "items": {"type": "string", "maxLength": MAX_LABEL_LENGTH},
        }
    ),
]

_RETIRED_MODALITY_FIELDS = ("modality", "supports_image_input")


def _reject_retired_modality_fields(data: Any) -> Any:
    # These two described the same capability before the modality sets replaced them.
    # Ignoring them (this model accepts extras, like every other create/update payload
    # here) would apply the permissive text/text default to a row whose author declared
    # image output — i.e. dispatch a chat completion to an image endpoint. The console's
    # bulk import forwards a pasted object verbatim, so last cycle's config file reaches
    # this unchanged.
    if isinstance(data, dict):
        retired = [name for name in _RETIRED_MODALITY_FIELDS if name in data]
        if retired:
            msg = (
                f"{', '.join(retired)}: retired — declare capabilities with `input_modalities` "
                "and `output_modalities` instead"
            )
            raise ValueError(msg)
    return data


_EXAMPLE_RESPONSE: dict[str, Any] = {
    "id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "name": "Claude 3.5 Sonnet",
    "description": "Client Acme only — do not assign to other engagements.",
    "model_alias": "claude-3-5-sonnet",
    "provider": "anthropic",
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "provider_model_id": "claude-3-5-sonnet-20240620",
    "endpoint_name": None,
    "inference_endpoint": None,
    "icon_file": "anthropic.svg",
    "labels": ["self-hosted"],
    "parameters": {"temperature": 0.7, "max_tokens": 1024},
    "extras": {},
    "is_disabled": False,
    "warmup_enabled": True,
    "advanced_params_disabled": False,
    "inactivity_alert_hours": 48,
    "last_used_at": "2026-01-03T08:00:00Z",
    "last_warmup_at": "2026-01-05T09:30:00Z",
    "inactivity_alerted_at": "2026-01-05T10:00:00Z",
    "has_api_key": True,
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}

_EXAMPLE_CREATE: dict[str, Any] = {
    "name": "Claude 3.5 Sonnet",
    "description": "Client Acme only — do not assign to other engagements.",
    "model_alias": "claude-3-5-sonnet",
    "provider": "anthropic",
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "provider_model_id": "claude-3-5-sonnet-20240620",
    "icon_file": "anthropic.svg",
    "labels": ["self-hosted"],
    "parameters": {"temperature": 0.7, "max_tokens": 1024},
    "extras": {},
    "is_disabled": False,
    "warmup_enabled": True,
    "advanced_params_disabled": False,
    "inactivity_alert_hours": 48,
    "api_key": "sk-ant-…",
}


class AiModelResponse(BaseModel):
    """Public view of an `AiModel` row.

    Build via `AiModelResponse.from_model(model)` so the encrypted-key
    column collapses into the boolean `has_api_key` flag at one site.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    name: str = Field(description="Human-facing display name.")
    description: str | None = Field(default=None, description="Admin/owner note; `null` when unset.")
    model_alias: str = Field(description="Stable slug used by gateway dispatch.")
    provider: ProviderVendor = Field(description="Vendor whose API this row routes to.")
    input_modalities: list[Modality] = Field(description="What the model accepts in a prompt.")
    output_modalities: list[Modality] = Field(description="What the model can return.")
    provider_model_id: str = Field(description="Identifier the provider's API expects.")
    endpoint_name: str | None = Field(default=None, description="Provider-side inference-endpoint admin name.")
    inference_endpoint: str | None = Field(
        default=None, description="Custom inference URL, when the row points at one."
    )
    icon_file: str | None = Field(default=None, description="UI hint — filename in the FE icon bundle.")
    labels: list[str] = Field(
        description="Operator annotations shown as badges, sorted and free of case-only duplicates."
    )
    # Deliberately a raw dict, not `InferenceParams` like `AiModelCreate.parameters`:
    # the response echoes whatever is stored in the JSONB column verbatim, including
    # provider-specific `extra` knobs, rather than re-projecting through the typed view.
    parameters: dict[str, Any] = Field(default_factory=dict, description="Default inference parameters.")
    extras: dict[str, Any] = Field(default_factory=dict, description="Provider-specific blob (auth, apiUrl, body, …).")
    is_disabled: bool = Field(description="True iff the gateway is blocked from dispatching to this row.")
    warmup_enabled: bool = Field(
        description="True iff the model's endpoint scales to zero and should be warmed before the first message."
    )
    advanced_params_disabled: bool = Field(
        description=(
            "True iff this model ignores the inference-params cascade: stored overrides are retained but "
            "never sent to the provider, and no layer below offers them."
        )
    )
    inactivity_alert_hours: int | None = Field(
        default=None,
        description=(
            "Quiet hours after which admins are alerted this model may be idling warm; `null` = no alert. "
            "Only ever acts on a `warmup_enabled` model — an always-on endpoint costs nothing idle."
        ),
    )
    last_used_at: datetime | None = Field(
        default=None, description="When a message was last dispatched to this model (UTC); warmups do not count."
    )
    last_warmup_at: datetime | None = Field(
        default=None, description="When this model was last warmed by a probe (UTC)."
    )
    inactivity_alerted_at: datetime | None = Field(
        default=None,
        description=(
            "When the current inactivity episode was alerted (UTC); cleared by new usage, or by restoring "
            "any knob that silences the sweep — re-enabling the model, re-enabling warm-up, re-opting into "
            "alerting, or restoring the model from a soft delete."
        ),
    )
    has_api_key: bool = Field(description="True iff a credential is stored. The credential itself is never returned.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    health_check_status: HealthCheckStatus | None = Field(
        default=None, description="`checking` / `alive` / `dead`; `null` = never health-checked."
    )
    last_health_check_at: datetime | None = Field(default=None, description="When the last health check started (UTC).")
    last_health_reason: str | None = Field(default=None, description="Machine reason when dead; `null` when alive.")
    last_healthy_at: datetime | None = Field(
        default=None, description="When the endpoint was last confirmed alive (UTC) — the last successful check."
    )
    capability_mismatch: str | None = Field(
        default=None,
        description=(
            "Provider's reason when a health check found the endpoint refusing the declared `image` input — "
            "the only capability probed. `null` when nothing contradicts the row. Never moves "
            "`health_check_status`: the endpoint answered, so it is the declaration that looks wrong."
        ),
    )
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the row; null on a live row, or when the delete was system-initiated.",
    )

    @classmethod
    def from_model(cls, model: AiModel) -> AiModelResponse:
        """Project an `AiModel` ORM row into the public response shape.

        Raises:
            ValidationError: The row holds a value this schema cannot represent —
                today only an out-of-set modality (the columns are plain `varchar[]`,
                see `Modality`). Logged with the row id first: on the list route this
                runs inside a page comprehension, so one such row fails every page it
                appears on, and the catch-all would otherwise report it as an anonymous
                500 naming nothing to fix.
        """
        try:
            return cls._project(model)
        except ValidationError:
            logger.exception(
                "ai_model_projection_failed",
                model_id=str(model.id),
                model_alias=model.model_alias,
                input_modalities=list(model.input_modalities),
                output_modalities=list(model.output_modalities),
            )
            raise

    @classmethod
    def _project(cls, model: AiModel) -> AiModelResponse:
        return cls(
            id=model.id,
            name=model.name,
            description=model.description,
            model_alias=model.model_alias,
            provider=model.provider,
            input_modalities=model.input_modalities,
            output_modalities=model.output_modalities,
            provider_model_id=model.provider_model_id,
            endpoint_name=model.endpoint_name,
            inference_endpoint=model.inference_endpoint,
            icon_file=model.icon_file,
            labels=list(model.labels),
            parameters=dict(model.parameters),
            extras=dict(model.extras),
            is_disabled=model.is_disabled,
            warmup_enabled=model.warmup_enabled,
            advanced_params_disabled=model.advanced_params_disabled,
            inactivity_alert_hours=model.inactivity_alert_hours,
            last_used_at=model.last_used_at,
            last_warmup_at=model.last_warmup_at,
            inactivity_alerted_at=model.inactivity_alerted_at,
            has_api_key=model.api_key_encrypted is not None,
            created_at=model.created_at,
            updated_at=model.updated_at,
            health_check_status=model.health_check_status,
            last_health_check_at=model.last_health_check_at,
            last_health_reason=model.last_health_reason,
            last_healthy_at=model.last_healthy_at,
            capability_mismatch=model.capability_mismatch,
            deleted_at=model.deleted_at,
            deleted_by_id=model.deleted_by_id,
        )


def _require_url_scheme(value: str | None) -> str | None:
    """Normalize an `inference_endpoint` and reject a non-http(s) one.

    Without the check a typo'd host is stored happily and only surfaces much
    later as an opaque provider error at dispatch, instead of as a
    field-addressable rejection on the form that wrote it. The surrounding
    whitespace of a pasted URL would reach litellm's `api_base` verbatim and
    fail the same opaque way, so it is stripped rather than rejected — and the
    stripped value is what lands in the column, keeping the write path and the
    service-side `missing_inference_endpoint` (whitespace reads as absent) from
    disagreeing about the same string. The scheme is matched case-insensitively
    per RFC 3986.
    """
    if value is None:
        return None
    endpoint = value.strip()
    if not endpoint.lower().startswith(("http://", "https://")):
        msg = "must be an absolute URL starting with http:// or https://"
        raise ValueError(msg)
    return endpoint


class AiModelCreate(BaseModel):
    """Payload accepted by `POST /v1/ai-models`.

    `api_key` is optional — rows without one are usable for providers that
    authenticate via environment-bound credentials (e.g. `aws_bedrock` with
    instance roles) or are stubbed for later configuration.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_CREATE})

    @model_validator(mode="before")
    @classmethod
    def _reject_retired_fields(cls, data: Any) -> Any:
        return _reject_retired_modality_fields(data)

    name: str = Field(min_length=1, max_length=255, description="Display name. Must be unique among live rows.")
    description: AdminNote = Field(
        default=None,
        description=(
            "Admin-authored note — which engagement the model belongs to, restrictions on reuse. "
            "Surrounding whitespace is stripped and a blank note is stored as `null`. Readable "
            "wherever `models:read` is held, globally or through an in-group role; absent from the "
            "per-evaluation model list a red-teamer reads."
        ),
    )
    model_alias: str = Field(
        min_length=1,
        max_length=128,
        description="Stable slug used by gateway dispatch. Must be unique among live rows.",
    )
    provider: ProviderVendor = Field(description="Vendor whose API this row routes to.")
    input_modalities: InputModalities = Field(
        default_factory=lambda: [Modality.TEXT],
        description="What the model accepts in a prompt. Must include `text`. Defaults to text only.",
    )
    output_modalities: OutputModalities = Field(
        default_factory=lambda: [Modality.TEXT],
        description="What the model can return. Defaults to text only.",
    )
    provider_model_id: str = Field(min_length=1, max_length=255, description="Identifier the provider's API expects.")
    endpoint_name: str | None = Field(default=None, max_length=255, description="Provider-side endpoint admin name.")
    inference_endpoint: str | None = Field(
        default=None,
        max_length=1024,
        description=(
            "Custom inference URL, absolute and http(s) — surrounding whitespace is stripped. Required when "
            "`provider` is `generic`, which has no vendor-hosted base."
        ),
    )
    icon_file: str | None = Field(default=None, max_length=255, description="UI hint.")
    labels: Labels = Field(
        default_factory=list,
        description=(
            f"Free-form operator annotations, at most {MAX_LABELS} distinct and {MAX_LABEL_LENGTH} characters each. "
            "Whitespace is collapsed, case-only duplicates dropped, the set stored sorted."
        ),
    )
    parameters: InferenceParams = Field(default_factory=InferenceParams, description="Default inference parameters.")
    extras: dict[str, Any] = Field(default_factory=dict, description="Provider-specific blob.")
    is_disabled: bool = Field(default=False, description="Set true to block the gateway from dispatching to this row.")
    warmup_enabled: bool = Field(
        default=False,
        description="Set true for a scale-to-zero endpoint so clients warm it before the first message.",
    )
    advanced_params_disabled: bool = Field(
        default=False,
        description=(
            "Set true to opt this model out of the inference-params cascade: knobs set here or at any "
            "layer below are retained but never sent to the provider, and the console stops offering them."
        ),
    )
    inactivity_alert_hours: int | None = Field(
        default=None,
        ge=1,
        le=8760,
        description=(
            "Alert admins once no message has been dispatched to this model for this many hours (1-8760) — "
            "warmups do not count, so an endpoint kept warm by conversation opens still alerts. Applies only "
            "when `warmup_enabled` is set: an always-on endpoint costs nothing while idle, so it is never "
            "swept. Omit or `null` for no alert."
        ),
    )
    api_key: SecretStr | None = Field(default=None, description="Plaintext credential — encrypted before persisting.")

    @field_validator("inference_endpoint")
    @classmethod
    def _check_inference_endpoint(cls, value: str | None) -> str | None:
        return _require_url_scheme(value)


class AiModelBulkRequest(BulkRequest[AiModelCreate]):
    """Bulk envelope that additionally rejects duplicate identities within the batch.

    Two rows sharing a `name` or a `model_alias` can never both commit — the
    second hits the live-row unique constraint and fails per-row. Rejecting the
    collision upfront (422) is clearer than surfacing it as a mid-batch 409, and
    mirrors `GroupInvitationBulkRequest`'s duplicate-email guard.
    """

    @model_validator(mode="after")
    def _reject_duplicate_identity(self) -> AiModelBulkRequest:
        seen_names: set[str] = set()
        seen_aliases: set[str] = set()
        for row in self.rows:
            if row.data.name in seen_names:
                msg = f"Duplicate name: {row.data.name!r}."
                raise ValueError(msg)
            if row.data.model_alias in seen_aliases:
                msg = f"Duplicate model_alias: {row.data.model_alias!r}."
                raise ValueError(msg)
            seen_names.add(row.data.name)
            seen_aliases.add(row.data.model_alias)
        return self


class AiModelUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/ai-models/{id}`.

    Omitted fields stay; `None` clears the nullable columns
    (`description`, `endpoint_name`, `inference_endpoint`, `icon_file`) — and a blank
    `description` normalises to `None`, so it clears too — except that a patch
    touching `provider` or `inference_endpoint` must leave a `generic` row with
    a URL, so clearing it there is a 400. A supplied `inference_endpoint` must
    be an absolute http(s) URL (422); an untouched one is never re-validated,
    so a row predating that rule stays editable. Explicit `null` is rejected on the
    others — they back NOT NULL DB columns. The API key is not on this surface
    — rotate or clear it via the dedicated `…/api-key` endpoints so the
    operation stays auditable on its own. `inactivity_alert_hours` with
    `warmup_enabled` off is representable and deliberately inert — the value
    survives toggling warm-up.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Claude 3.5 Sonnet (deprecated)",
                "is_disabled": True,
            }
        }
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_retired_fields(cls, data: Any) -> Any:
        return _reject_retired_modality_fields(data)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: AdminNote = Field(
        default=None, description="Admin/owner note; send `null` — or a blank string — to clear it."
    )
    model_alias: str | None = Field(default=None, min_length=1, max_length=128)
    provider: ProviderVendor | None = Field(default=None)
    input_modalities: InputModalities | None = Field(
        default=None,
        description="Replaces the declared prompt set wholesale. Must include `text`; deduped and re-sorted.",
    )
    output_modalities: OutputModalities | None = Field(
        default=None,
        description="Replaces the declared reply set wholesale. Must be non-empty; deduped and re-sorted.",
    )
    provider_model_id: str | None = Field(default=None, min_length=1, max_length=255)
    endpoint_name: str | None = Field(default=None, max_length=255)
    inference_endpoint: str | None = Field(default=None, max_length=1024)
    icon_file: str | None = Field(default=None, max_length=255)
    labels: Labels | None = Field(
        default=None,
        description="Replaces the label set wholesale. Normalised like on create; `[]` clears every label.",
    )
    parameters: InferenceParams | None = Field(default=None)
    extras: dict[str, Any] | None = Field(default=None)
    is_disabled: bool | None = Field(default=None)
    warmup_enabled: bool | None = Field(default=None)
    advanced_params_disabled: bool | None = Field(default=None)
    # No cross-field validator against `warmup_enabled`: the console resends both fields
    # on every save, so it would 422 unrelated patches.
    inactivity_alert_hours: int | None = Field(
        default=None,
        ge=1,
        le=8760,
        description=(
            "Quiet hours (1-8760) before admins are alerted; send `null` to stop alerting. Acts only on a "
            "`warmup_enabled` model."
        ),
    )

    @field_validator(
        "name",
        "model_alias",
        "provider",
        "input_modalities",
        "output_modalities",
        "provider_model_id",
        "labels",
        "parameters",
        "extras",
        "is_disabled",
        "warmup_enabled",
        "advanced_params_disabled",
        mode="before",
    )
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # Fields here back NOT NULL columns or have a non-null DB default;
        # omitting the field leaves the row untouched, but an explicit
        # `null` would either crash the flush with an IntegrityError (and
        # surface as a misleading 409 "uniqueness") or silently flip
        # `is_disabled` to False. Reject at the edge with a 422 instead.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value

    @field_validator("inference_endpoint")
    @classmethod
    def _check_inference_endpoint(cls, value: str | None) -> str | None:
        return _require_url_scheme(value)


class AiModelApiKeyUpdate(BaseModel):
    """Payload accepted by `PUT /v1/ai-models/{id}/api-key`."""

    model_config = ConfigDict(json_schema_extra={"example": {"api_key": "sk-ant-…"}})

    api_key: SecretStr = Field(min_length=1, description="Plaintext credential — encrypted before persisting.")


class AiModelApiKeyBulkItem(BaseModel):
    """One row of a bulk API-key upload.

    Targets its model by `name` — its unique business key — rather than by id,
    so keys can be uploaded from the same config that named the models with no
    server-assigned ids to thread back.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Claude 3.5 Sonnet", "api_key": "sk-ant-…"}})

    name: str = Field(min_length=1, max_length=255, description="Display name of the target model — its identity key.")
    api_key: SecretStr = Field(min_length=1, description="Plaintext credential — encrypted before persisting.")


class AiModelApiKeyBulkRequest(BulkRequest[AiModelApiKeyBulkItem]):
    """Bulk envelope that additionally rejects duplicate targets within the batch.

    Two rows for one `name` would set the same model's key twice — the later
    row silently wins. Reject upfront (422), mirroring `AiModelBulkRequest`'s
    duplicate guard.
    """

    @model_validator(mode="after")
    def _reject_duplicate_targets(self) -> AiModelApiKeyBulkRequest:
        seen: set[str] = set()
        for row in self.rows:
            if row.data.name in seen:
                msg = f"Duplicate target: name {row.data.name!r}."
                raise ValueError(msg)
            seen.add(row.data.name)
        return self


class AiModelUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `AiModelUpdate` fail loudly at
    construction instead of silently landing in the DB. Build from
    `payload.model_dump(exclude_unset=True)` so `model_fields_set` separates
    "omitted" from "explicit None".
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    model_alias: str | None = None
    provider: ProviderVendor | None = None
    input_modalities: InputModalities | None = None
    output_modalities: OutputModalities | None = None
    provider_model_id: str | None = None
    endpoint_name: str | None = None
    inference_endpoint: str | None = None
    icon_file: str | None = None
    labels: Labels | None = None
    parameters: dict[str, Any] | None = None
    extras: dict[str, Any] | None = None
    is_disabled: bool | None = None
    warmup_enabled: bool | None = None
    advanced_params_disabled: bool | None = None
    inactivity_alert_hours: int | None = None
