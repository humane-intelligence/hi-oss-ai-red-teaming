"""Request/response schemas for the evaluation endpoints — model assignments and evaluation-group endpoints."""

from datetime import date
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.core.ai_gateway import InferenceParams
from app.core.ai_gateway import ProviderVendor
from app.core.ai_gateway import WarmupStatus
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.inference_params import mask_inference_params
from app.core.ai_gateway.inference_params import merge_inference_params
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import SystemRole
from app.core.auth.schemas import MAX_INVITE_ROWS
from app.core.auth.schemas import NormalizedEmail
from app.core.auth.schemas import RoleSummary
from app.core.auth.schemas import UserBase
from app.core.auth.schemas import assert_unique_invite_emails
from app.core.bulk import BulkRequest
from app.core.bulk import BulkRow
from app.core.conversations.tags import TAG_KEY_PATTERN
from app.core.conversations.tags import is_valid_tag_key
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Task
from app.core.licenses.catalog import effective_license
from app.core.licenses.models import DataLicense
from app.core.licenses.schemas import DataLicenseSummary

if TYPE_CHECKING:
    from app.core.auth.models import User
    from app.core.evaluations.models import Evaluation
    from app.core.evaluations.models import EvaluationAiModel
    from app.core.evaluations.models import EvaluationGroupAiModel
    from app.core.evaluations.models import EvaluationTagKey
    from app.core.evaluations.models import Scenario
    from app.core.evaluations.services.group_invitations import GroupInvitationResult

# A resolved `effective_license` object as it appears on reads (matches the `DataLicenseSummary`
# example) — the responses embed the full licence object, not a bare SPDX string.
_EXAMPLE_EFFECTIVE_LICENSE: dict[str, Any] = {
    "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "spdx_id": "CC-BY-4.0",
    "name": "Creative Commons Attribution 4.0 International",
    "version": "4.0",
    "short_description": "Share and adapt for any purpose, with attribution.",
    "reference_url": "https://creativecommons.org/licenses/by/4.0/legalcode",
    "is_curated": True,
    "is_default": True,
    "has_content": False,
    "is_no_license": False,
    "text_managed_in_code": False,
    "protects_conversation_data": False,
    "created_by_id": None,
}

_EXAMPLE_EVALUATION_AI_MODEL_RESPONSE: dict[str, Any] = {
    "id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "evaluation_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "model_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "model_display_mask": "Model A",
    "parameters": {"temperature": 0.2},
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}

_EXAMPLE_EVALUATION_GROUP_AI_MODEL_RESPONSE: dict[str, Any] = {
    "id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "evaluation_group_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "model_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "name": "GPT-4o",
    "description": "Client Acme only — do not assign to other engagements.",
    "provider": "openai",
    "provider_model_id": "gpt-4o-2024-08-06",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "is_disabled": False,
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}

_EXAMPLE_EVALUATION_GROUP_RESPONSE: dict[str, Any] = {
    "id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "title": "Spring 2026 jailbreak challenge",
    "description": "Open red-teaming engagement against the frontier model fleet.",
    "created_by_id": "a1b2c3d4-1111-2222-3333-444455556666",
    "status": "published",
    "rejection_reason": None,
    "access_level": "public",
    "metrics_access_during": "all_members",
    "metrics_access_after": "owner_only",
    "start_date": "2026-03-01",
    "end_date": "2026-03-31",
    "data_license_id": None,
    "effective_license": _EXAMPLE_EFFECTIVE_LICENSE,
    "created_at": "2026-02-01T12:00:00Z",
    "updated_at": "2026-02-02T09:30:00Z",
}

_EXAMPLE_EVALUATION_RESPONSE: dict[str, Any] = {
    "id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "evaluation_group_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "created_by_id": "a1b2c3d4-1111-2222-3333-444455556666",
    "title": "Prompt-injection gauntlet",
    "description": "Coordinate jailbreak attempts against the masked fleet.",
    "cover_image": None,
    "status": "draft",
    "mask_models_enabled": True,
    "data_license_id": None,
    "effective_license": _EXAMPLE_EFFECTIVE_LICENSE,
    "rejection_reason": None,
    "models": [
        {
            "assignment_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "name": "Model A",
            "provider": None,
            "provider_model_id": None,
            "warmup_enabled": True,
            "input_modalities": ["text"],
        }
    ],
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}

# Detail read = the group shape plus the embedded full child evaluations. The
# embedded child gets its own id and points its `evaluation_group_id` back at the
# enclosing group, so the example reads as a real parent→child pair.
_EXAMPLE_EVALUATION_GROUP_DETAIL_RESPONSE: dict[str, Any] = {
    **_EXAMPLE_EVALUATION_GROUP_RESPONSE,
    # The caller's effective permissions here — an in-group `owner`'s full role set
    # (the union includes platform-wide entries the role carries, e.g. `users:invite`).
    # Derived from the registry so the example can't drift from the `owner` role.
    "user_permissions": sorted(ROLE_PERMISSIONS[SystemRole.OWNER]),
    "evaluations": [
        {
            **_EXAMPLE_EVALUATION_RESPONSE,
            "id": "c0ffee00-0000-4000-8000-000000000001",
            "evaluation_group_id": _EXAMPLE_EVALUATION_GROUP_RESPONSE["id"],
        }
    ],
    "allowed_models": [_EXAMPLE_EVALUATION_GROUP_AI_MODEL_RESPONSE],
    # Empty because the example group is already `published` — a live group has no
    # next transition left to be blocked.
    "publication_blockers": [],
}

_EXAMPLE_EVALUATION_GROUP_CREATE: dict[str, Any] = {
    "title": "Spring 2026 jailbreak challenge",
    "description": "Open red-teaming engagement against the frontier model fleet.",
    "access_level": "public",
    "metrics_access_during": "all_members",
    "metrics_access_after": "owner_only",
    "start_date": "2026-09-01",
    "end_date": "2026-09-30",
    "data_license_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "allowed_model_ids": ["7c9e6679-7425-40de-944b-e07fc1f90ae7"],
}

_EXAMPLE_SCENARIO_RESPONSE: dict[str, Any] = {
    "id": "9a7b6c5d-1e2f-4a3b-8c9d-0e1f2a3b4c5d",
    "name": "Prompt injection via system override",
    "description": "Coax the model into ignoring its system prompt.",
    "evaluation_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "position": 0,
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}

_EXAMPLE_TASK_RESPONSE: dict[str, Any] = {
    "id": "9a7b6c5d-1e2f-4a3b-8c9d-0e1f2a3b4c5d",
    "name": "Prompt injection via system override",
    "description": "Coax the model into ignoring its system prompt.",
    "scenario_id": "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}

_EXAMPLE_SCENARIO_DETAIL_RESPONSE: dict[str, Any] = {
    **_EXAMPLE_SCENARIO_RESPONSE,
    "tasks": [_EXAMPLE_TASK_RESPONSE],
}


def dates_out_of_order(start_date: date, end_date: date | None) -> bool:
    """True when `end_date` is set but not strictly after `start_date`.

    Shared by the create and update services (both → 400) — create checks the
    payload pair, update the post-merge state (where the stored row is needed).
    """
    return end_date is not None and end_date <= start_date


class EvaluationAiModelResponse(BaseModel):
    """Public view of an `EvaluationAiModel` assignment row.

    Build via `EvaluationAiModelResponse.from_model(assignment)`. The
    underlying model's identity is intentionally not echoed here — that
    masking decision belongs to the evaluation-detail projection, which reads
    `model_id` against the parent evaluation's `mask_models_enabled`.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_EVALUATION_AI_MODEL_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier of the assignment.")
    evaluation_id: UUID = Field(description="Evaluation this assignment belongs to.")
    model_id: UUID = Field(description="The assigned AI model.")
    model_display_mask: str | None = Field(
        default=None,
        description="Alias surfaced in place of the model's real identity when the evaluation masks models.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Per-assignment inference-parameter overrides."
    )
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who unassigned the model; null on a live row, or when the delete was system-initiated.",
    )

    @classmethod
    def from_model(cls, assignment: EvaluationAiModel) -> EvaluationAiModelResponse:
        """Project an `EvaluationAiModel` ORM row into the public response shape."""
        return cls(
            id=assignment.id,
            evaluation_id=assignment.evaluation_id,
            model_id=assignment.model_id,
            model_display_mask=assignment.model_display_mask,
            parameters=dict(assignment.parameters),
            created_at=assignment.created_at,
            updated_at=assignment.updated_at,
            deleted_at=assignment.deleted_at,
            deleted_by_id=assignment.deleted_by_id,
        )


class EvaluationAiModelAssign(BaseModel):
    """Payload accepted by `POST /v1/evaluations/{evaluation_id}/models`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "model_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "model_display_mask": "Model A",
                "parameters": {"temperature": 0.2},
            }
        }
    )

    model_id: UUID = Field(description="AI model to assign to the evaluation.")
    model_display_mask: str | None = Field(
        default=None,
        max_length=255,
        description="Alias surfaced in place of the model's real identity when the evaluation masks models.",
    )
    parameters: InferenceParams = Field(
        default_factory=InferenceParams, description="Per-assignment inference-parameter overrides."
    )


class EvaluationAiModelUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/evaluations/{evaluation_id}/models/{assignment_id}`.

    Omitted fields stay. `model_display_mask` accepts explicit `null` to clear
    the mask (it backs a nullable column). `parameters` rejects explicit `null`
    — it backs a NOT NULL column; omit it to leave the overrides unchanged.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"model_display_mask": "Model B"}})

    model_display_mask: str | None = Field(default=None, max_length=255)
    parameters: InferenceParams | None = Field(default=None)

    @field_validator("parameters", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # `parameters` backs a NOT NULL column with a `'{}'::jsonb` default;
        # omitting the field leaves the row untouched, but an explicit `null`
        # would crash the flush. Reject at the edge with a 422 instead.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class EvaluationAiModelUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `EvaluationAiModelUpdate` fail loudly at
    construction. Build from `payload.model_dump(exclude_unset=True)` so
    `model_fields_set` separates "omitted" from "explicit None".
    """

    model_config = ConfigDict(extra="forbid")

    model_display_mask: str | None = None
    parameters: dict[str, Any] | None = None


class EvaluationGroupAiModelResponse(BaseModel):
    """One model in an evaluation group's allowed-model subset.

    Admin-facing config — the group-subset endpoints are gated on
    `models:read` / `models:update`, so the real model identity is surfaced
    (no masking, unlike the evaluation-level `EvaluationAiModelView`).
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_EVALUATION_GROUP_AI_MODEL_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier of the subset row.")
    evaluation_group_id: UUID = Field(description="Group this subset row belongs to.")
    model_id: UUID = Field(description="The allowed AI model.")
    name: str = Field(description="Model display name.")
    description: str | None = Field(default=None, description="Admin/owner note on the model; `null` when unset.")
    provider: ProviderVendor = Field(description="Vendor whose API backs the model.")
    provider_model_id: str = Field(description="Provider-side model identifier.")
    input_modalities: list[Modality] = Field(description="What the model accepts in a prompt.")
    output_modalities: list[Modality] = Field(description="What the model can return.")
    is_disabled: bool = Field(description="True iff the gateway is blocked from dispatching to this model.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")

    @classmethod
    def from_row(cls, row: EvaluationGroupAiModel) -> EvaluationGroupAiModelResponse:
        """Project a subset row (with `ai_model` loaded) into the response shape."""
        return cls(
            id=row.id,
            evaluation_group_id=row.evaluation_group_id,
            model_id=row.model_id,
            name=row.ai_model.name,
            description=row.ai_model.description,
            provider=row.ai_model.provider,
            provider_model_id=row.ai_model.provider_model_id,
            input_modalities=row.ai_model.input_modalities,
            output_modalities=row.ai_model.output_modalities,
            is_disabled=row.ai_model.is_disabled,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class EvaluationRejectRequest(BaseModel):
    """Payload for `POST /v1/evaluations/{evaluation_id}/reject`."""

    model_config = ConfigDict(json_schema_extra={"example": {"rejection_reason": "Out of scope for this engagement."}})

    rejection_reason: str = Field(min_length=1, max_length=2000, description="Why the evaluation is being rejected.")

    @field_validator("rejection_reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class EvaluationGroupRejectRequest(BaseModel):
    """Payload for `POST /v1/evaluation-groups/{group_id}/reject`.

    Same contract as `EvaluationRejectRequest`, kept separate so each resource
    owns its OpenAPI schema and the two can diverge without a shared-model dance.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"rejection_reason": "Engagement scope too broad."}})

    rejection_reason: str = Field(min_length=1, max_length=2000, description="Why the group is being rejected.")

    @field_validator("rejection_reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class EvaluationGroupResponse(BaseModel):
    """Public view of an `EvaluationGroup` row.

    Build via `EvaluationGroupResponse.from_model(group, default_license=...)`,
    passing the platform default license (resolved once per request) for the
    effective-license fallback. This base shape carries no child evaluations —
    list/create/update return it as-is. The group-detail read returns the
    `EvaluationGroupDetailResponse` subclass, which embeds the group's full
    child evaluations.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_EVALUATION_GROUP_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    # title/description/start_date are nullable on a partial draft — they
    # are only `null` while a group is an incomplete `draft`. access_level is never
    # null (it defaults to `invitation_only`).
    title: str | None = Field(default=None, description="Human-facing display name; `null` on a partial draft.")
    description: str | None = Field(default=None, description="Free-text summary; `null` on a partial draft.")
    created_by_id: UUID = Field(description="Identifier of the user who created the group.")
    status: PublicationStatus = Field(description="Lifecycle state of the group.")
    rejection_reason: str | None = Field(default=None, description="Reason recorded when the group was rejected.")
    access_level: EvaluationGroupAccessLevel = Field(
        description="Visibility: `public` (everyone), `organization` (the owning org's members), or `invitation_only`."
    )
    metrics_access_during: MetricsAccessLevel = Field(
        description=(
            "Who may view the group's metrics dashboards while it is active: `inherit_group_access` "
            "(whoever can see the group, per its `access_level`, sees the full aggregate), "
            "`all_members` (any member sees the full aggregate), `members_personal_metrics` "
            "(members holding `evaluation_groups:view_personal_metrics` — the `red_teamer` role — see "
            "the dashboards filtered to their own contributions), or `owner_only`. The group owner "
            "and admins always see the full aggregate."
        )
    )
    metrics_access_after: MetricsAccessLevel = Field(
        description=(
            "Who may view the group's metrics dashboards once it is finished (status `inactive`); "
            "same levels as `metrics_access_during`."
        )
    )
    organization_id: UUID | None = Field(
        default=None, description="Organization the group belongs to; `null` when org-less."
    )
    start_date: date | None = Field(default=None, description="Day the engagement opens; `null` on a partial draft.")
    end_date: date | None = Field(default=None, description="Day it closes, if scheduled.")
    data_license_id: UUID | None = Field(
        default=None,
        description="Group-level data-license override (licence id); `null` inherits the platform default.",
    )
    effective_license: DataLicenseSummary = Field(
        description="Resolved data license — the group override if set, else the platform default.",
    )
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")

    @classmethod
    def _base_kwargs(cls, group: EvaluationGroup, *, default_license: DataLicense) -> dict[str, Any]:
        """Map a group row to the base-field kwargs shared with the detail subclass.

        `default_license` is the platform default licence row (resolved once per request); the
        effective licence is the group's own override row if set, else that default — the same
        two-state cascade as before, now on rows. The `data_license` relationship is eager-loaded.
        """
        return {
            "id": group.id,
            "title": group.title,
            "description": group.description,
            "created_by_id": group.created_by_id,
            "status": group.status,
            "rejection_reason": group.rejection_reason,
            "access_level": group.access_level,
            "metrics_access_during": group.metrics_access_during,
            "metrics_access_after": group.metrics_access_after,
            "organization_id": group.organization_id,
            "start_date": group.start_date,
            "end_date": group.end_date,
            "data_license_id": group.data_license_id,
            "effective_license": DataLicenseSummary.from_model(group.data_license or default_license),
            "created_at": group.created_at,
            "updated_at": group.updated_at,
        }

    @classmethod
    def from_model(cls, group: EvaluationGroup, *, default_license: DataLicense) -> EvaluationGroupResponse:
        """Project an `EvaluationGroup` ORM row into the public response shape (no child evaluations).

        `default_license` is the platform default licence row (`get_default_license`); the effective
        licence is the group's override or that default.
        """
        return cls(**cls._base_kwargs(group, default_license=default_license))


class EvaluationAiModelView(BaseModel):
    """One assigned model as surfaced on an evaluation read.

    When the parent evaluation masks models, `name` carries the assignment's
    `model_display_mask` (possibly `null` when none is set) and the identifying
    fields (`provider`, `provider_model_id`) are withheld as `null`. The
    `assignment_id` is always present — it addresses the assignment without
    correlating the underlying model across evaluations. `warmup_enabled` is
    surfaced even under masking: it's an operational flag (does this endpoint
    need warming), not a model identifier, so the client can decide whether to
    warm without learning which model it is. `input_modalities` is surfaced the
    same way — a capability set the composer needs to know whether to offer image
    attachments, not an identifier. `output_modalities` is not surfaced here: the
    composer has no use for it and it would only widen the fingerprint. `labels` is
    absent altogether, masked or not: it is free operator text, so nothing stops it
    naming the model (`llama-3-70b-box`), and the admin surfaces that need it read the
    registry directly. `effective_parameters` is likewise
    surfaced under masking but filtered to the identity-safe numeric knobs
    (`mask_inference_params`) — free-text `system_prompt` and provider-specific
    keys are withheld, as they can leak which model backs the assignment.
    `advanced_params_disabled` is surfaced too, on the `warmup_enabled` footing: the
    client has to know whether to offer the override controls at all, and an empty
    `effective_parameters` alone cannot say whether the model opted out or simply
    sets nothing.
    """

    assignment_id: UUID = Field(description="Identifier of the model assignment within the evaluation.")
    name: str | None = Field(
        default=None,
        description="Model display name, or its mask alias when the evaluation masks models.",
    )
    provider: ProviderVendor | None = Field(
        default=None,
        description="Vendor whose API backs the model; `null` when the evaluation masks models.",
    )
    provider_model_id: str | None = Field(
        default=None,
        description="Provider-side model identifier; `null` when the evaluation masks models.",
    )
    warmup_enabled: bool = Field(
        default=False,
        description="Whether this model's endpoint should be warmed before the first message. Not masked.",
    )
    input_modalities: list[Modality] = Field(
        default_factory=lambda: [Modality.TEXT],
        description="What this model accepts in a prompt — drives the composer's attachment affordance. Not masked.",
    )
    advanced_params_disabled: bool = Field(
        default=False,
        description=(
            "Whether the underlying model opts out of the inference-params cascade. When true, "
            "`effective_parameters` is empty and no layer below should offer the knobs. Not masked."
        ),
    )
    effective_parameters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Inference params inherited by conversations under this assignment: the model baseline "
            "merged with the per-assignment overrides. Under model masking, restricted to the "
            "identity-safe numeric sampling knobs — the system prompt and provider-specific keys "
            "are withheld. Empty when `advanced_params_disabled` is set — nothing below inherits "
            "params the gateway will not send."
        ),
    )
    # Not masked: when the row was unassigned is an operational fact about the
    # assignment, not a hint about which model backs it.
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the unassign; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who unassigned the model; null on a live row, or when the delete was system-initiated.",
    )

    @classmethod
    def from_assignment(cls, assignment: EvaluationAiModel, *, masked: bool) -> EvaluationAiModelView:
        """Project an assignment into its public view, applying masking.

        Reads `assignment.ai_model` for the unmasked fields *and* `warmup_enabled`,
        `advanced_params_disabled` + `effective_parameters` (all surfaced regardless of
        masking), so callers must eager-load that relationship on every projection,
        masked or not. Under masking the merged params are filtered to the
        identity-safe numeric knobs.

        A model that opts out of the cascade projects empty `effective_parameters`: the
        gateway will not send them, so showing a child layer a value to inherit would
        promise an effect that never happens.
        """
        disabled = assignment.ai_model.advanced_params_disabled
        effective_parameters = (
            {} if disabled else merge_inference_params(assignment.ai_model.parameters, assignment.parameters)
        )
        if masked:
            return cls(
                assignment_id=assignment.id,
                name=assignment.model_display_mask,
                warmup_enabled=assignment.ai_model.warmup_enabled,
                input_modalities=assignment.ai_model.input_modalities,
                advanced_params_disabled=disabled,
                effective_parameters=mask_inference_params(effective_parameters),
                deleted_at=assignment.deleted_at,
                deleted_by_id=assignment.deleted_by_id,
            )
        return cls(
            assignment_id=assignment.id,
            name=assignment.ai_model.name,
            provider=assignment.ai_model.provider,
            provider_model_id=assignment.ai_model.provider_model_id,
            warmup_enabled=assignment.ai_model.warmup_enabled,
            input_modalities=assignment.ai_model.input_modalities,
            advanced_params_disabled=disabled,
            effective_parameters=effective_parameters,
            deleted_at=assignment.deleted_at,
            deleted_by_id=assignment.deleted_by_id,
        )


class WarmupResponse(BaseModel):
    """Result of a warmup probe against a model assignment.

    A deliberately opaque enum — no provider identity, no upstream error text — so
    it is safe to return under a masked evaluation.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"status": "starting"}})

    status: WarmupStatus = Field(description="`ready` to send, `starting` (retry), or `error` (won't recover).")


class EvaluationResponse(BaseModel):
    """Public view of an `Evaluation` row, including its assigned models.

    Build via `EvaluationResponse.from_model(evaluation, effective_license=...)`. The caller must
    have eager-loaded the live `models` relationship and each assignment's `ai_model`, since
    `from_model` projects them without further DB access, and pass the already-resolved effective
    data license (evaluation override → group override → platform default; see
    `resolve_effective_licenses`) — the projection can't resolve the cascade itself without the
    parent group loaded.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_EVALUATION_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    evaluation_group_id: UUID = Field(description="Parent engagement group.")
    created_by_id: UUID = Field(description="Identifier of the user who created the evaluation.")
    title: str = Field(description="Human-facing display name.")
    description: str | None = Field(default=None, description="Free-text summary; `null` when saved title-only.")
    cover_image: str | None = Field(default=None, description="Cover image reference, if set.")
    status: EvaluationStatus = Field(description="Lifecycle state of the evaluation.")
    mask_models_enabled: bool = Field(description="Whether assigned model identities are masked on reads.")
    tags_enabled: bool = Field(description="Whether conversations in this evaluation may carry tags at all.")
    tags_restricted: bool = Field(
        description="When true, conversation tag keys must be in the evaluation's allowed-key set."
    )
    data_license_id: UUID | None = Field(
        default=None,
        description="Per-evaluation licence override (id); `null` inherits the group's, else the platform default.",
    )
    effective_license: DataLicenseSummary = Field(
        description="Resolved licence — evaluation override, else the group's, else the platform default.",
    )
    rejection_reason: str | None = Field(default=None, description="Reason recorded when the evaluation was rejected.")
    models: list[EvaluationAiModelView] = Field(default_factory=list, description="Models assigned to the evaluation.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the evaluation; null on a live row, or when the delete was system-initiated.",
    )

    @classmethod
    def from_model(cls, evaluation: Evaluation, *, effective_license: DataLicense) -> EvaluationResponse:
        """Project an `Evaluation` ORM row (with loaded `models`) into the response shape.

        `effective_license` is the already-resolved cascade licence row (evaluation → group →
        platform default) — routes get it from `resolve_effective_license(s)`, the group-detail
        embed computes it in-memory from the loaded parent group.
        """
        masked = evaluation.mask_models_enabled
        return cls(
            id=evaluation.id,
            evaluation_group_id=evaluation.evaluation_group_id,
            created_by_id=evaluation.created_by_id,
            title=evaluation.title,
            description=evaluation.description,
            cover_image=evaluation.cover_image,
            status=evaluation.status,
            mask_models_enabled=evaluation.mask_models_enabled,
            tags_enabled=evaluation.tags_enabled,
            tags_restricted=evaluation.tags_restricted,
            data_license_id=evaluation.data_license_id,
            effective_license=DataLicenseSummary.from_model(effective_license),
            rejection_reason=evaluation.rejection_reason,
            models=[EvaluationAiModelView.from_assignment(a, masked=masked) for a in evaluation.models],
            created_at=evaluation.created_at,
            updated_at=evaluation.updated_at,
            deleted_at=evaluation.deleted_at,
            deleted_by_id=evaluation.deleted_by_id,
        )


class EvaluationGroupDetailResponse(EvaluationGroupResponse):
    """`EvaluationGroupResponse` plus the group's full child evaluations.

    Only the group-detail GET returns this shape; list/create/update return the
    base `EvaluationGroupResponse`, which has no `evaluations` field at all.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_EVALUATION_GROUP_DETAIL_RESPONSE})

    evaluations: list[EvaluationResponse] = Field(
        default_factory=list,
        description="Full child evaluations (models included, masking applied), ordered by creation.",
    )
    user_permissions: list[str] = Field(
        default_factory=list,
        description=(
            "The caller's effective permissions on this group — the union of what their in-group roles grant "
            "(empty for a non-member; the object type's full vocabulary for an `evaluation_groups:manage` "
            "break-glass holder). `evaluation_groups:view_metrics` additionally appears whenever the group's "
            "configured metrics-access level grants the caller the dashboards, not only via a held role. "
            "Gate group-scoped actions on these. Per the object-role model these are "
            "whole-role permission sets, so platform-wide entries a role also carries (e.g. `users:invite`) "
            "may appear and are not group-scoped — gate only on the group-relevant keys (`evaluation_groups:*`, "
            "`evaluations:*`)."
        ),
    )
    allowed_models: list[EvaluationGroupAiModelResponse] | None = Field(
        default=None,
        description=(
            "The group's allowed-model subset — the models its evaluations may be assigned. "
            "`null` when the caller lacks `models:read`: the identities are hidden, not merely empty."
        ),
    )
    publication_blockers: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable gaps blocking the group's **next** lifecycle step, empty when it is ready: "
            "for `draft` / `changes_requested` what `submit` would refuse (missing fields, dates, "
            "organization, allowed models, evaluations without a scenario), for `approved` what `publish` "
            "would refuse (no evaluations, or one without a scenario). Always empty for the other statuses — "
            "`pending_approval` waits on a moderator, `published` on the owner finishing it, the terminal "
            "ones on nothing. Same sentences the matching 400 carries, so a UI can show the list before the "
            "owner clicks and never disagree with the refusal. The scenario entry names each offending "
            "evaluation as `title (id)` — the id is carried deliberately, since titles are not unique "
            "within a group, so this is the one place a raw id is meant to reach the UI."
        ),
        examples=[["Assign at least one allowed model."]],
    )

    @classmethod
    def from_model_with_evaluations(
        cls,
        group: EvaluationGroup,
        *,
        default_license: DataLicense,
        user_permissions: list[str],
        allowed_models: list[EvaluationGroupAiModelResponse] | None,
        publication_blockers: list[str],
    ) -> EvaluationGroupDetailResponse:
        """Project a group and embed its full child evaluations.

        The caller must have eager-loaded the live `evaluations` relationship and,
        per child, its live `models` plus each assignment's `ai_model` (the
        group-detail read does); reading them off an un-loaded group trips an async
        lazy-load. The relationship carries its own `(created_at, id)` order, so the
        children need no re-sort here. `default_license` (resolved once per request)
        anchors the cascade: each child's effective license is computed in-memory —
        child override → this group's override → platform default — since both layers
        are already loaded (no `resolve_effective_licenses` round-trip). `user_permissions`
        is the caller's resolved object-scope authority (`group_scope_permissions`).
        `allowed_models` is the projected subset the route passes (`null` when the
        caller lacks `models:read`), `publication_blockers` the readiness list the
        route collects for the group's next lifecycle step.
        """
        return cls(
            **cls._base_kwargs(group, default_license=default_license),
            user_permissions=user_permissions,
            allowed_models=allowed_models,
            publication_blockers=publication_blockers,
            evaluations=[
                EvaluationResponse.from_model(
                    e, effective_license=effective_license(e.data_license, group.data_license, default=default_license)
                )
                for e in group.evaluations
            ],
        )


class EvaluationCreate(BaseModel):
    """Payload accepted by `POST /v1/evaluations`.

    A new evaluation always starts in `new`; lifecycle transitions land via
    dedicated endpoints, so neither `status` nor `rejection_reason` is part of
    this surface.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "Prompt-injection gauntlet",
                "description": "Coordinate jailbreak attempts against the masked fleet.",
                "evaluation_group_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
                "mask_models_enabled": True,
            }
        }
    )

    title: str = Field(min_length=1, max_length=255, description="Human-facing display name.")
    description: str | None = Field(
        default=None, min_length=1, description="Free-text summary; omit to save title-only and fill in later."
    )
    evaluation_group_id: UUID = Field(description="Parent engagement group the evaluation belongs to.")
    cover_image: str | None = Field(default=None, max_length=1024, description="Cover image reference.")
    mask_models_enabled: bool = Field(default=True, description="Mask assigned model identities on reads.")
    tags_enabled: bool = Field(default=True, description="Allow conversations in this evaluation to carry tags at all.")
    tags_restricted: bool = Field(
        default=False, description="Restrict conversation tag keys to the evaluation's allowed-key set."
    )
    data_license_id: UUID | None = Field(
        default=None,
        description="Licence override (id); omit to inherit the group's licence, else the platform default.",
    )


class EvaluationUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/evaluations/{evaluation_id}`.

    Content-only: omitted fields stay, and `description` / `cover_image` accept
    explicit `null` to clear them. `status`/`rejection_reason` are owned by the
    lifecycle endpoints, and an evaluation cannot be moved between groups, so
    neither is editable here.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"title": "Prompt-injection gauntlet (v2)"}})

    title: str | None = Field(default=None, min_length=1, max_length=255, description="Human-facing display name.")
    description: str | None = Field(
        default=None, min_length=1, description="Free-text summary; explicit `null` clears it."
    )
    cover_image: str | None = Field(
        default=None, max_length=1024, description="Cover image reference; explicit `null` clears it."
    )
    mask_models_enabled: bool | None = Field(
        default=None, description="Whether assigned model identities are masked on reads."
    )
    tags_enabled: bool | None = Field(
        default=None, description="Whether conversations in this evaluation may carry tags at all."
    )
    tags_restricted: bool | None = Field(
        default=None, description="When true, restrict conversation tag keys to the evaluation's allowed-key set."
    )
    data_license_id: UUID | None = Field(
        default=None,
        description="Data-license override (licence id); explicit `null` resets it to inherit (group, else platform).",
    )

    @field_validator(
        "title",
        "mask_models_enabled",
        "tags_enabled",
        "tags_restricted",
        mode="before",
    )
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # These back NOT NULL columns; omitting the field leaves the row
        # untouched, but an explicit `null` would crash the flush. Reject at
        # the edge with a 422. `description`, `cover_image`, and `data_license_id`
        # are excluded — their columns are nullable, so `null` legitimately
        # clears them (for `data_license_id`, null = reset to inherit the platform
        # default).
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class EvaluationGroupCreate(BaseModel):
    """Payload accepted by `POST /v1/evaluation-groups`.

    The creator is granted the in-group `owner` role and the new group is submitted
    for review — it starts in `pending_approval`; partial progress goes
    through `POST /draft` (→ `draft`) instead. The later status transitions
    (approve / publish) are a separate workflow, so neither `status` nor
    `created_by_id` is accepted here. The date rules (start not before today, end
    after start) are enforced in the service (→ 400) so create and the update's
    merged-state check return the same code and envelope for them.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_EVALUATION_GROUP_CREATE})

    title: str = Field(min_length=1, max_length=255, description="Human-facing display name.")
    description: str = Field(min_length=1, description="Free-text summary of the engagement.")
    access_level: EvaluationGroupAccessLevel = Field(
        description="Visibility: `public` (everyone), `organization` (the owning org's members), or `invitation_only`."
    )
    metrics_access_during: MetricsAccessLevel = Field(
        default=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
        description=(
            "Who may view the group's metrics dashboards while it is active. "
            "Defaults to `members_personal_metrics` (each member sees their own contributions); "
            "the owner and admins always see the full aggregate."
        ),
    )
    metrics_access_after: MetricsAccessLevel = Field(
        default=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
        description=(
            "Who may view the group's metrics dashboards once it is finished (status `inactive`). "
            "Defaults to `members_personal_metrics`."
        ),
    )
    organization_id: UUID | None = Field(
        default=None,
        description="Organization the group belongs to. Required when `access_level` is `organization`; must be live.",
    )
    start_date: date = Field(description="Day the engagement opens. Must not be before today.")
    end_date: date | None = Field(default=None, description="Day it closes, if scheduled.")
    data_license_id: UUID | None = Field(
        default=None,
        description=(
            "Group-level data-license override (licence id). Omitted, it is derived from `access_level` — "
            "`invitation_only` gets `No license`, any other level inherits the platform default; an explicit "
            "`null` always inherits."
        ),
    )
    allowed_model_ids: list[UUID] = Field(
        min_length=1,
        description="Models the group's evaluations may be assigned. At least one is required; each must be live.",
    )


class EvaluationGroupDraftCreate(BaseModel):
    """Payload accepted by `POST /v1/evaluation-groups/draft` — save partial progress.

    Only `title` is required; every other field is optional so an owner can start
    a group and finish it later. The group is created in `DRAFT`. The
    completeness checks the full create enforces (required fields, `start_date` not
    before today, the `organization` invariant) are intentionally skipped here and
    re-imposed when the draft is submitted for approval (`POST /{id}/submit` rejects an
    incomplete group with 400). The only date rule kept at draft time is order, and
    only when both dates are supplied.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"title": "Q3 jailbreak engagement (draft)"}})

    title: str = Field(min_length=1, max_length=255, description="Human-facing display name (the one required field).")
    description: str | None = Field(default=None, min_length=1, description="Free-text summary. Optional for a draft.")
    access_level: EvaluationGroupAccessLevel = Field(
        default=EvaluationGroupAccessLevel.INVITATION_ONLY,
        description="Visibility. Defaults to `invitation_only` so a draft is never accidentally exposed.",
    )
    metrics_access_during: MetricsAccessLevel = Field(
        default=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
        description="Who may view metrics while the group is active. Defaults to `members_personal_metrics`.",
    )
    metrics_access_after: MetricsAccessLevel = Field(
        default=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
        description="Who may view metrics once the group is finished. Defaults to `members_personal_metrics`.",
    )
    organization_id: UUID | None = Field(default=None, description="Owning organization. Optional for a draft.")
    start_date: date | None = Field(default=None, description="Day the engagement opens. Optional for a draft.")
    end_date: date | None = Field(default=None, description="Day it closes. Optional for a draft.")
    data_license_id: UUID | None = Field(
        default=None,
        description=(
            "Group-level data-license override (licence id). Omitted, it is derived from `access_level` — "
            "`invitation_only` gets `No license`, any other level inherits the platform default; an explicit "
            "`null` always inherits."
        ),
    )
    allowed_model_ids: list[UUID] = Field(
        default_factory=list,
        description="Models the group's evaluations may be assigned. Optional for a draft; each must be live.",
    )


class EvaluationGroupUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/evaluation-groups/{id}`.

    Omitted fields stay. Explicit `null` clears the nullable columns
    (`start_date`, `description`, `end_date`, `data_license_id` — the latter resets
    the group to inherit the platform default) — a draft may legitimately blank
    them back out. Explicit `null` is rejected on `title`, `access_level`, and
    the `metrics_access_*` levels, which back NOT NULL columns (a group always
    has a name, a visibility, and a metrics audience). `status` is not on this
    surface (transitions are a separate workflow), and unlike create, a past
    `start_date` is allowed on edit.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"title": "Spring 2026 jailbreak challenge (rev. 2)", "end_date": None}}
    )

    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Human-facing display name. Omit to leave unchanged.",
    )
    description: str | None = Field(
        default=None,
        min_length=1,
        description="Free-text summary of the engagement. Omit to leave unchanged.",
    )
    access_level: EvaluationGroupAccessLevel | None = Field(
        default=None,
        description=(
            "Visibility: `public`, `organization`, or `invitation_only`. Omit to leave unchanged. "
            "Switching to `organization` requires a live `organization_id`."
        ),
    )
    metrics_access_during: MetricsAccessLevel | None = Field(
        default=None,
        description="Who may view metrics while the group is active. Omit to leave unchanged.",
    )
    metrics_access_after: MetricsAccessLevel | None = Field(
        default=None,
        description="Who may view metrics once the group is finished. Omit to leave unchanged.",
    )
    organization_id: UUID | None = Field(
        default=None,
        description=(
            "Owning organization. Omit to leave unchanged; send `null` to clear "
            "(rejected while `access_level` is `organization`). Must be live when set."
        ),
    )
    start_date: date | None = Field(
        default=None,
        description="Day the engagement opens. Omit to leave unchanged.",
    )
    end_date: date | None = Field(
        default=None,
        description="Day it closes. Omit to leave unchanged; send null to clear.",
    )
    data_license_id: UUID | None = Field(
        default=None,
        description=(
            "Group-level data-license override (licence id). Omit to leave unchanged; "
            "explicit `null` resets it to inherit the platform default."
        ),
    )
    allowed_model_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Replace the group's allowed-model subset (declarative set). Omit to leave unchanged; "
            "must be non-empty and each id must be live. A model still assigned to an evaluation in "
            "the group cannot be removed."
        ),
    )

    @field_validator(
        "title", "access_level", "metrics_access_during", "metrics_access_after", "allowed_model_ids", mode="before"
    )
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # `title`/`access_level`/`metrics_access_*` back NOT NULL columns and
        # `allowed_model_ids` is a required non-empty set; omitting any leaves the
        # row untouched, but an explicit `null` is meaningless — reject with a 422.
        # `description`/`start_date`/`end_date`/`data_license_id` are absent: nullable,
        # so `null` legitimately clears them (a null `data_license_id` resets to inherit
        # the platform default).
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class EvaluationGroupUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `EvaluationGroupUpdate` fail loudly at
    construction. Build from `payload.model_dump(exclude_unset=True)` so
    `model_fields_set` separates "omitted" from "explicit None".
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    access_level: EvaluationGroupAccessLevel | None = None
    metrics_access_during: MetricsAccessLevel | None = None
    metrics_access_after: MetricsAccessLevel | None = None
    organization_id: UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    data_license_id: UUID | None = None
    # Not a column — handled via `sync_group_models`, apart from the setattr loop.
    allowed_model_ids: list[UUID] | None = None


class GroupInvitationCreate(BaseModel):
    """One invitee row of a group-invitation bulk request, with one or more roles pre-assigned."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "ada@example.com",
                "role_ids": ["b2c3d4e5-2222-3333-4444-555566667777"],
            }
        }
    )

    email: NormalizedEmail = Field(description="Invitee email. Normalized to lowercase.", examples=["ada@example.com"])
    role_ids: list[UUID] = Field(
        min_length=1,
        description="In-group roles to pre-assign; each must be assignable within a group.",
        examples=[["b2c3d4e5-2222-3333-4444-555566667777"]],
    )


class GroupInvitationBulkRequest(BulkRequest[GroupInvitationCreate]):
    """Bulk envelope of the group invite — same row cap and duplicate rule as the platform one.

    Both feed one operator action, so a limit that differed between them would
    surface as the same dialog accepting a CSV here and rejecting it there.
    """

    rows: list[BulkRow[GroupInvitationCreate]] = Field(
        min_length=1,
        max_length=MAX_INVITE_ROWS,
        description=f"Invitees, at least one and at most {MAX_INVITE_ROWS}.",
    )

    @model_validator(mode="after")
    def _validate_unique_emails(self) -> GroupInvitationBulkRequest:
        assert_unique_invite_emails(row.data.email for row in self.rows)
        return self


class GroupInvitationResponse(BaseModel):
    """Result of a group invitation.

    The roles are assigned on the group immediately in both outcomes; `outcome`
    only distinguishes whether the account was already active (`assigned`) or a
    token was emailed to onboard it (`invited`).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "outcome": "invited",
                "user_id": "a1b2c3d4-1111-2222-3333-444455556666",
                "email": "ada@example.com",
                "roles": [
                    {"id": "b2c3d4e5-2222-3333-4444-555566667777", "name": "red_teamer", "display_name": "Red Teamer"}
                ],
                "expires_at": "2026-06-17T10:00:00Z",
            }
        }
    )

    outcome: Literal["assigned", "invited"] = Field(
        description="`assigned` — the email belonged to an active account; `invited` — an onboarding token was sent.",
    )
    user_id: UUID = Field(description="Identifier of the invited (or assigned) user.")
    email: str = Field(description="Email the invitation targets.")
    roles: list[RoleSummary] = Field(description="In-group roles assigned on the group.")
    expires_at: datetime | None = Field(
        default=None,
        description="Token expiry for the `invited` outcome; `null` when assigned immediately.",
    )

    @classmethod
    def from_result(cls, result: GroupInvitationResult) -> GroupInvitationResponse:
        """Project the service result into the response shape."""
        return cls(
            outcome=result.outcome,
            user_id=result.user_id,
            email=result.email,
            roles=[RoleSummary.from_role(role) for role in result.roles],
            expires_at=result.expires_at,
        )


class AnnotatorResponse(UserBase):
    """A user assignable as an annotator to a flag or review within a group."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4-1111-2222-3333-444455556666",
                "email": "ada@example.com",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "status": "active",
            }
        }
    )

    @classmethod
    def from_user(cls, user: User) -> AnnotatorResponse:
        """Project a `User` ORM row into the annotator response shape."""
        return cls(
            id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            status=user.status,
        )


_EXAMPLE_SCENARIO_RESPONSE: dict[str, Any] = {
    "id": "9a7b6c5d-1e2f-4a3b-8c9d-0e1f2a3b4c5d",
    "name": "Prompt injection via system override",
    "description": "Coax the model into ignoring its system prompt.",
    "evaluation_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "position": 0,
    "required_reviews": 1,
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}


class ScenarioResponse(BaseModel):
    """Public view of a `Scenario` row.

    Build via `ScenarioResponse.from_model(scenario)` so the projection stays in
    one place.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_SCENARIO_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    name: str = Field(description="Human-facing scenario name.")
    description: str = Field(description="What the scenario asks the red-teamer to attempt.")
    evaluation_id: UUID = Field(description="Evaluation this scenario belongs to.")
    position: int = Field(description="Zero-based order of the scenario within its evaluation.")
    required_reviews: int = Field(
        description="Reviews required per flag of this scenario before it is fully reviewed; usually odd (1 or 3)."
    )
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the row; null on a live row, or when the delete was system-initiated.",
    )

    @classmethod
    def _base_kwargs(cls, scenario: Scenario) -> dict[str, Any]:
        """Map a scenario row to the base-field kwargs shared with the detail subclass."""
        return {
            "id": scenario.id,
            "name": scenario.name,
            "description": scenario.description,
            "evaluation_id": scenario.evaluation_id,
            "position": scenario.position,
            "required_reviews": scenario.required_reviews,
            "created_at": scenario.created_at,
            "updated_at": scenario.updated_at,
            "deleted_at": scenario.deleted_at,
            "deleted_by_id": scenario.deleted_by_id,
        }

    @classmethod
    def from_model(cls, scenario: Scenario) -> ScenarioResponse:
        """Project a `Scenario` ORM row into the public response shape (no embedded tasks)."""
        return cls(**cls._base_kwargs(scenario))


class ScenarioCreate(BaseModel):
    """Payload accepted by `POST /v1/evaluations/{evaluation_id}/scenarios`.

    The parent `evaluation_id` comes from the path; `position` is assigned by
    the service (appended last) — reorder owns it, so it is not accepted here.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Prompt injection via system override",
                "description": "Coax the model into ignoring its system prompt.",
                "required_reviews": 3,
            }
        }
    )

    name: str = Field(min_length=1, max_length=255, description="Human-facing scenario name.")
    description: str = Field(min_length=1, description="What the scenario asks the red-teamer to attempt.")
    required_reviews: int = Field(
        default=1, ge=1, description="Required reviews per flag; usually odd (1 or 3). Defaults to 1."
    )


class ScenarioUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/evaluations/{evaluation_id}/scenarios/{id}`.

    Content-only: `name` / `description`. Omitted fields stay; explicit `null`
    is rejected — both back NOT NULL columns. `position` is not on this surface
    (reorder owns it).
    """

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Prompt injection (revised)"}})

    name: str | None = Field(default=None, min_length=1, max_length=255, description="Omit to leave unchanged.")
    description: str | None = Field(default=None, min_length=1, description="Omit to leave unchanged.")
    required_reviews: int | None = Field(default=None, ge=1, description="Omit to leave unchanged.")

    @field_validator("name", "description", "required_reviews", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # All back NOT NULL columns; omitting leaves the row untouched, but an
        # explicit `null` would crash the flush. Reject at the edge with a 422.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class ScenarioUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `ScenarioUpdate` fail loudly at
    construction. Build from `payload.model_dump(exclude_unset=True)` so
    `model_fields_set` separates "omitted" from "explicit None".
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    required_reviews: int | None = None


class ScenarioReorderRequest(BaseModel):
    """Payload accepted by `PATCH /v1/evaluations/{evaluation_id}/scenarios/order`.

    The **full** ordered set of the evaluation's live scenario ids — new
    `position` is each id's index in the list. The service rejects a set that
    doesn't match the evaluation's live scenarios exactly.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scenario_ids": [
                    "9a7b6c5d-1e2f-4a3b-8c9d-0e1f2a3b4c5d",
                    "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f",
                ]
            }
        }
    )

    scenario_ids: list[UUID] = Field(min_length=1, description="Full ordered set of the evaluation's scenario ids.")

    @field_validator("scenario_ids")
    @classmethod
    def _reject_duplicates(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("scenario_ids must not contain duplicates")
        return value


class TaskResponse(BaseModel):
    """Public view of a `Task` row.

    Build via `TaskResponse.from_model(task)` so the projection stays in
    one place.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_TASK_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    name: str = Field(description="Human-facing task name.")
    description: str = Field(description="What the task asks the red-teamer to attempt.")
    scenario_id: UUID = Field(description="Scenario this task belongs to.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the row; null on a live row, or when the delete was system-initiated.",
    )

    @classmethod
    def from_model(cls, task: Task) -> TaskResponse:
        """Project a `Task` ORM row into the public response shape."""
        return cls(
            id=task.id,
            name=task.name,
            description=task.description,
            scenario_id=task.scenario_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
            deleted_at=task.deleted_at,
            deleted_by_id=task.deleted_by_id,
        )


class TaskCreate(BaseModel):
    """Payload accepted by `POST /v1/scenarios/{scenario_id}/tasks`.

    The parent `scenario_id` comes from the path, so it is not accepted here.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Prompt injection via system override",
                "description": "Coax the model into ignoring its system prompt.",
            }
        }
    )

    name: str = Field(min_length=1, max_length=255, description="Human-facing task name.")
    description: str = Field(min_length=1, description="What the task asks the red-teamer to attempt.")


class TaskUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/scenarios/{scenario_id}/tasks/{task_id}`.

    Content-only (`name` / `description`). Omitted fields stay; explicit `null` is
    rejected — both back NOT NULL columns.
    """

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Prompt injection (revised)"}})

    name: str | None = Field(default=None, min_length=1, max_length=255, description="Omit to leave unchanged.")
    description: str | None = Field(default=None, min_length=1, description="Omit to leave unchanged.")

    @field_validator("name", "description", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # Both back NOT NULL columns; omitting leaves the row untouched, but an
        # explicit `null` would crash the flush. Reject at the edge with a 422.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class TaskUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `TaskUpdate` fail loudly at
    construction. Build from `payload.model_dump(exclude_unset=True)` so
    `model_fields_set` separates "omitted" from "explicit None".
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None


class ScenarioDetailResponse(ScenarioResponse):
    """A scenario read with its tasks embedded.

    Returned by the single-scenario GET; the list views stay on the lean
    `ScenarioResponse` to avoid a per-row task fan-out. Build via
    `ScenarioDetailResponse.from_model_with_tasks(scenario)` with `scenario.tasks`
    eager-loaded (live rows only).
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_SCENARIO_DETAIL_RESPONSE})

    tasks: list[TaskResponse] = Field(
        default_factory=list, description="Tasks belonging to the scenario, ordered by creation."
    )

    @classmethod
    def from_model_with_tasks(cls, scenario: Scenario) -> ScenarioDetailResponse:
        """Project a `Scenario` (with eager-loaded live `tasks`) into the detail shape."""
        return cls(
            **cls._base_kwargs(scenario),
            tasks=[TaskResponse.from_model(task) for task in scenario.tasks],
        )


# One rule, one definition: `tags.py` owns the charset/length core and the conversation payload
# validator compiles the same constant, so a key an admin allows is exactly one a conversation may
# use. The anchors differ by engine on purpose: Pydantic compiles with Rust regex, where `^`/`$`
# already bound the whole text, while Python's `re` needs `\A`/`\Z` because its `$` also matches
# before a trailing newline — the divergence that let a key forge a prompt line. Grouped like the
# other two anchorings (`TAG_KEY_RE`, the conversation map's `propertyNames.pattern`): a top-level
# alternation added to the shared constant later would otherwise bind looser than the anchors here.
_TAG_KEY_PATTERN = rf"^(?:{TAG_KEY_PATTERN})$"


def _require_usable_tag_key(value: str) -> str:
    """The `pattern=` above publishes the charset; this adds the rule Rust regex can't express."""
    if not is_valid_tag_key(value):
        raise ValueError("must not consist of dots alone")
    return value


class EvaluationTagKeyCreate(BaseModel):
    """Payload to allow a conversation-tag key on an evaluation."""

    model_config = ConfigDict(json_schema_extra={"example": {"key": "env"}})

    key: Annotated[str, AfterValidator(_require_usable_tag_key)] = Field(
        pattern=_TAG_KEY_PATTERN, description="Allowed tag key (letters, digits, `_.-`; ≤64 chars, not dots alone)."
    )


class EvaluationTagKeyResponse(BaseModel):
    """Public view of one allowed conversation-tag key."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "evaluation_id": "3f2a0000-0000-0000-0000-000000000000",
                "key": "env",
                "created_at": "2026-01-01T12:00:00Z",
            }
        }
    )

    id: UUID = Field(description="Server-assigned identifier.")
    evaluation_id: UUID = Field(description="Evaluation the key is allowed on.")
    key: str = Field(description="The allowed tag key.")
    created_at: datetime = Field(description="UTC timestamp the key was added.")

    @classmethod
    def from_model(cls, row: EvaluationTagKey) -> EvaluationTagKeyResponse:
        """Project an `EvaluationTagKey` row into the public shape."""
        return cls(id=row.id, evaluation_id=row.evaluation_id, key=row.key, created_at=row.created_at)
