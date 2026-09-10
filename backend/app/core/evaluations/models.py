"""Evaluation-domain tables: `EvaluationGroup`, `Evaluation`, `EvaluationAiModel`, and `Scenario`.

`EvaluationGroup` is the top-level red-teaming engagement (the container the
legacy Prisma project shipped as `Event`). `EvaluationAiModel` is the
association object joining `Evaluation` to `AiModel` (one row per model assigned
to an evaluation), carrying the per-assignment inference-params override and the
display alias used when the parent evaluation masks model identities; `Scenario`
holds an evaluation's reorderable challenges. Keeping them in one module lets the
`Evaluation.models` ⇄ `EvaluationAiModel.evaluation` relationship resolve without
a cross-module import cycle (same pattern as `auth/models.py`). Per-object role
assignment is generic and lives in `app/core/auth/object_roles/`.
"""

import uuid
from datetime import date

from sqlalchemy import CheckConstraint
from sqlalchemy import Date
from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import Text
from sqlalchemy import text
from sqlmodel import Field
from sqlmodel import Relationship
from sqlmodel import col

from app.core.ai_gateway import AiModel
from app.core.ai_gateway import InferenceParamsMixin
from app.core.base_model import BaseModel
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.licenses.models import DataLicense
from app.core.organizations.models import Organization


class Evaluation(BaseModel, table=True):
    __tablename__ = "evaluations"

    title: str = Field(max_length=255, nullable=False)
    description: str | None = Field(default=None, sa_type=Text, sa_column_kwargs={"nullable": True})
    # Master toggle for model-identity masking. When true, evaluation-detail
    # responses hide each assigned model's name/provider/provider_model_id and
    # surface `EvaluationAiModel.model_display_mask` instead.
    mask_models_enabled: bool = Field(
        default=True,
        sa_column_kwargs={"nullable": False, "server_default": text("true")},
    )
    # Whether conversations in this evaluation may carry tags at all. False hides every tagging
    # surface in the console and rejects tag writes, so "no tagging here" is an explicit state
    # rather than something inferred from a restriction with an empty allow-list. Default true:
    # evaluations that predate the flag keep behaving as before.
    tags_enabled: bool = Field(
        default=True,
        sa_column_kwargs={"nullable": False, "server_default": text("true")},
    )
    # When true, a conversation's tag keys must be in this evaluation's allowed-key set
    # (`evaluation_tag_keys`); when false, tags are unrestricted (free-form). Default false so
    # existing/new evaluations stay unrestricted unless an admin opts in.
    tags_restricted: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    cover_image: str | None = Field(default=None, max_length=1024)
    # Per-evaluation data-license override → `data_licenses.id`. NULL means "inherit" — the
    # effective license resolves at the projection layer as evaluation → group → platform default.
    # No DB cascade: a soft-deleted license must still resolve for rows that reference it (lineage
    # doesn't lapse), so `ondelete` stays unset. See app/core/licenses/.
    data_license_id: uuid.UUID | None = Field(default=None, foreign_key="data_licenses.id", index=True)
    # `lazy="raise"`: this relationship is read ONLY by the group-detail embed's in-memory cascade
    # (`EvaluationGroupDetailResponse.from_model_with_evaluations`), which eager-loads it via an
    # explicit `selectinload(Evaluation.data_license)` on the detail loader. Standalone evaluation
    # reads (list/get/update, exports, conversation projections) resolve the licence from the scalar
    # `data_license_id` through `resolve_effective_licenses`, so a `selectin` here would fire a
    # discarded batched query on each of them — `raise` makes any unintended access loud instead.
    data_license: DataLicense | None = Relationship(sa_relationship_kwargs={"lazy": "raise"})
    status: EvaluationStatus = Field(
        default=EvaluationStatus.NEW,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            EvaluationStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="evaluationstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True, "server_default": text("'new'")},
    )
    rejection_reason: str | None = Field(default=None, sa_type=Text)
    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    evaluation_group_id: uuid.UUID = Field(
        foreign_key="evaluation_groups.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )

    evaluation_group: EvaluationGroup = Relationship(back_populates="evaluations")

    # Loads via this relationship do NOT filter soft-deleted assignment rows —
    # attach `with_live(EvaluationAiModel)` to `.options(...)` (see
    # app/core/soft_delete.py).
    models: list[EvaluationAiModel] = Relationship(back_populates="evaluation")
    # Per-evaluation scenarios, ordered by `Scenario.position` (the service keeps
    # positions dense). Loads do NOT filter soft-deleted rows — attach
    # `with_live(Scenario)` to `.options(...)` when eager-loading.
    scenarios: list[Scenario] = Relationship(back_populates="evaluation")


# Canonical evaluation ordering — the single source of truth shared by the
# `EvaluationGroup.evaluations` embed and any list read. Evaluations carry no
# `position` (unlike scenarios): they're an unordered set, so siblings sharing a
# transaction-time `created_at` fall back to `id` for a stable (if arbitrary) order.
EVALUATION_DEFAULT_ORDER = (col(Evaluation.created_at), col(Evaluation.id))

# One shared type object, unlike the inline per-column enums: two columns ride
# the same PG enum type, and separate `Enum(...)` instances under one name
# would double-CREATE the type on a metadata create_all.
_METRICS_ACCESS_LEVEL_ENUM = Enum(
    MetricsAccessLevel,
    values_callable=lambda enum: [m.value for m in enum],
    name="metricsaccesslevel",
)


class EvaluationGroup(BaseModel, table=True):
    __tablename__ = "evaluation_groups"

    # title/description/start_date are nullable so a partial *draft* can be saved
    # with only a title; the full create path still requires them, and a
    # future submit gate re-checks completeness. `access_level` stays NOT NULL:
    # it has a safe default (`invitation_only`) so a draft is never accidentally
    # exposed, and a null level would behave identically to `invitation_only`
    # everywhere — an undecided state worth nothing but the null-handling.
    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(
        default=None,
        sa_type=Text,
        sa_column_kwargs={"nullable": True},
    )
    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    status: PublicationStatus = Field(
        default=PublicationStatus.DRAFT,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            PublicationStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="publicationstatus",
        ),
        sa_column_kwargs={"nullable": False},
    )
    rejection_reason: str | None = Field(default=None, sa_type=Text)
    access_level: EvaluationGroupAccessLevel = Field(
        default=EvaluationGroupAccessLevel.INVITATION_ONLY,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            EvaluationGroupAccessLevel,
            values_callable=lambda enum: [m.value for m in enum],
            name="evaluationgroupaccesslevel",
        ),
        sa_column_kwargs={"nullable": False, "index": True},
    )
    # server_default ('owner_only') is deliberately narrower than the Python default
    # (the product default: members read their own metrics): existing rows backfill
    # fail-closed, while new ORM inserts always carry the column and get the product
    # default. Don't re-align the two — it would widen the backfill.
    metrics_access_during: MetricsAccessLevel = Field(
        default=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
        sa_type=_METRICS_ACCESS_LEVEL_ENUM,  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False, "server_default": text("'owner_only'")},
    )
    metrics_access_after: MetricsAccessLevel = Field(
        default=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
        sa_type=_METRICS_ACCESS_LEVEL_ENUM,  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False, "server_default": text("'owner_only'")},
    )
    # Tenancy: the organization this group *belongs to* — a separate axis from
    # `access_level` (visibility). Nullable so a group can be org-less; `SET NULL`
    # so hard-deleting an org never orphans a group. The `organization` access
    # level requires this to be set + live (enforced on create/update), and scopes
    # the group's read-visibility to that org's members, fail-closed on a
    # soft-deleted org (`access.group_visible_to` / `group_is_visible`).
    organization_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="organizations.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
    start_date: date | None = Field(
        default=None,
        sa_type=Date,
        sa_column_kwargs={"nullable": True},
    )
    end_date: date | None = Field(
        default=None,
        sa_type=Date,
        sa_column_kwargs={"nullable": True},
    )
    # Group-level data-license override → `data_licenses.id`. NULL means "inherit the platform
    # default"; a child evaluation's effective license resolves evaluation → this → platform
    # default. No DB cascade (license lineage survives a soft-deleted license). See app/core/licenses/.
    data_license_id: uuid.UUID | None = Field(default=None, foreign_key="data_licenses.id", index=True)
    # Eager-loaded (including a soft-deleted licence) for the in-memory effective-license
    # cascade in the projection.
    data_license: DataLicense | None = Relationship(sa_relationship_kwargs={"lazy": "selectin"})

    # Child evaluations, surfaced by the group-detail read. Loads via this
    # relationship do NOT filter soft-deleted rows — attach `with_live(Evaluation)`
    # to `.options(...)` when eager-loading (see services.evaluation_groups).
    # `passive_deletes` is required, not speculative: adding this relationship
    # makes the ORM aware of the children, so on `session.delete(group)` it would
    # default to NULL-ing their NOT-NULL FK first. Deferring to the DB
    # `ON DELETE CASCADE` (FK on `evaluations.evaluation_group_id`) keeps the
    # pre-existing `test_deleting_group_cascades_to_evaluations` green.
    # `order_by` makes the load deterministic via `EVALUATION_DEFAULT_ORDER`:
    # `created_at` alone ties for siblings sharing a transaction-time `now()` default,
    # so `id` breaks the tie (mirrors `Scenario.tasks` / `TASK_DEFAULT_ORDER`).
    evaluations: list[Evaluation] = Relationship(
        back_populates="evaluation_group",
        sa_relationship_kwargs={
            "passive_deletes": True,
            "order_by": lambda: EVALUATION_DEFAULT_ORDER,
        },
    )

    # Owning organization (many-to-one). Eager-loaded by `get_evaluation_group`
    # for the visibility gate + response embed; a soft-deleted org is re-guarded
    # in-Python via `Organization.live` (a many-to-one `with_live` does not
    # reliably filter across identity-map states).
    organization: Organization | None = Relationship()

    # The group's allowed-model subset (see `EvaluationGroupAiModel`); an empty
    # subset is incomplete state — nothing is assignable until it is configured.
    # Loads here do NOT filter soft-deleted rows — attach
    # `with_live(EvaluationGroupAiModel)` when eager-loading. `passive_deletes`
    # defers to the DB `ON DELETE CASCADE` (mirrors `evaluations`).
    allowed_models: list[EvaluationGroupAiModel] = Relationship(
        sa_relationship_kwargs={"passive_deletes": True},
    )


class EvaluationAiModel(BaseModel, InferenceParamsMixin, table=True):
    """Assignment of an `AiModel` to an `Evaluation`, with per-assignment overrides.

    Carries the inference-params override layer for one model within one
    evaluation: effective params are
    `merge_inference_params(ai_model.parameters, assignment.parameters)`
    (most-specific-last; see `app.core.ai_gateway.inference_params`).

    `model_display_mask` is the alias surfaced in place of the real identity
    when the parent evaluation's `mask_models_enabled` is true (hiding the
    model's `name`). Masking is a response-projection concern — `model_id` always
    resolves the true row.
    """

    __tablename__ = "evaluation_ai_models"
    __table_args__ = (
        Index(
            "ix_evaluation_ai_models_model_id_evaluation_id",
            "model_id",
            "evaluation_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_evaluation_ai_models_evaluation_id", "evaluation_id"),
    )

    model_id: uuid.UUID = Field(foreign_key="ai_models.id", nullable=False)
    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False)
    # Display mask — distinct from `AiModel.model_alias`, which is the dispatch slug.
    model_display_mask: str | None = Field(default=None, max_length=255)
    # `parameters` JSONB is inherited from `InferenceParamsMixin` — the
    # per-assignment override layer.

    evaluation: Evaluation = Relationship(back_populates="models")
    # Loaded WITHOUT `with_live(AiModel)` on purpose: a soft-deleted model is
    # cascaded to soft-delete its live assignments (see the ai-model delete
    # path), so a live assignment always has a live model — but the projection
    # still wants the row to resolve even if that invariant is ever violated.
    ai_model: AiModel = Relationship()


class EvaluationGroupAiModel(BaseModel, table=True):
    """Membership of an `AiModel` in an `EvaluationGroup`'s allowed-model subset.

    A pure join — no per-row overrides (unlike `EvaluationAiModel`, which carries
    inference-params and a display mask). Only models with a live row here may be
    assigned to the group's evaluations; an empty subset allows none (fail-closed).
    """

    __tablename__ = "evaluation_group_ai_models"
    __table_args__ = (
        # Unique per live (model, group), leading with `model_id` so it also serves the
        # `model_id` FK + delete cascade; the separate index serves the group FK (mirrors `EvaluationAiModel`).
        Index(
            "ix_evaluation_group_ai_models_model_id_group_id",
            "model_id",
            "evaluation_group_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_evaluation_group_ai_models_evaluation_group_id", "evaluation_group_id"),
    )

    evaluation_group_id: uuid.UUID = Field(
        foreign_key="evaluation_groups.id",
        nullable=False,
        ondelete="CASCADE",
    )
    model_id: uuid.UUID = Field(foreign_key="ai_models.id", nullable=False)

    # Loaded WITHOUT `with_live(AiModel)` on purpose (same rationale as
    # `EvaluationAiModel.ai_model`): a soft-deleted model cascades to soft-delete
    # its subset rows, so a live subset row always points at a live model.
    ai_model: AiModel = Relationship()


class EvaluationTagKey(BaseModel, table=True):
    """An allowed conversation-tag key for an evaluation — the admin-defined tag schema.

    A simple allow-list, enforced only when the evaluation's ``tags_restricted`` flag is set:
    then a conversation's tag keys must all be in the live set (an empty set forbids every tag).
    While ``tags_restricted`` is false the keys are inert and tags stay free-form.
    """

    __tablename__ = "evaluation_tag_keys"
    __table_args__ = (
        # Unique per live (evaluation, key); leads with `evaluation_id` so it also serves
        # the FK + delete cascade and the per-evaluation list.
        Index(
            "ix_evaluation_tag_keys_evaluation_id_key",
            "evaluation_id",
            "key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False, ondelete="CASCADE")
    key: str = Field(max_length=64, nullable=False)


class Scenario(BaseModel, table=True):
    """A single scenario (legacy `Challenge`) within an `Evaluation`.

    Reorderable within its evaluation via `position`, which the scenario service is the sole writer of
    and keeps dense (0, 1, 2, …); there is deliberately **no** unique constraint
    on `(evaluation_id, position)` — a reorder rewrites several rows in one
    transaction and would collide with a unique on intermediate states.
    """

    __tablename__ = "scenarios"
    __table_args__ = (
        # No standalone index on `evaluation_id`: this composite already serves
        # `evaluation_id`-prefix lookups (list + cascade) and the `ORDER BY position`.
        Index("ix_scenarios_evaluation_id_position", "evaluation_id", "position"),
        CheckConstraint("required_reviews >= 1", name="required_reviews_positive"),
    )

    name: str = Field(max_length=255, nullable=False)
    description: str = Field(sa_type=Text, sa_column_kwargs={"nullable": False})
    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False, ondelete="CASCADE")
    position: int = Field(nullable=False)
    # How many completed reviews a flag of this scenario needs (the review queue
    # counts against it); usually odd (1 or 3) for consensus.
    required_reviews: int = Field(
        default=1,
        sa_column_kwargs={"nullable": False, "server_default": text("1")},
    )
    evaluation: Evaluation = Relationship(back_populates="scenarios")
    # Per-scenario tasks for the scenario-detail read. Loads do NOT filter
    # soft-deleted rows — attach `with_live(Task)` when eager-loading. Ordered by
    # `TASK_DEFAULT_ORDER` (matching `list_tasks`, so embed and list never diverge),
    # passed as a callable since `Task` is defined below. No `passive_deletes`; the
    # DB `ON DELETE CASCADE` handles deletion.
    tasks: list[Task] = Relationship(
        back_populates="scenario",
        sa_relationship_kwargs={"order_by": lambda: TASK_DEFAULT_ORDER},
    )


class Task(BaseModel, table=True):
    """A single task — the simplest (name + description) sub-level of a `Scenario`.

    Deliberately minimal — no ordering. The scenario-detail read embeds tasks
    via the `Scenario.tasks` relationship; the parent's `ON DELETE CASCADE` FK
    handles deletion.
    """

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_scenario_id", "scenario_id"),)

    name: str = Field(max_length=255, nullable=False)
    description: str = Field(sa_type=Text, sa_column_kwargs={"nullable": False})
    scenario_id: uuid.UUID = Field(foreign_key="scenarios.id", nullable=False, ondelete="CASCADE")

    scenario: Scenario = Relationship(back_populates="tasks")


# Canonical task ordering — the single source of truth shared by the
# `Scenario.tasks` embed (above) and `list_tasks`. Tasks are unordered by design;
# siblings created in one transaction share `created_at`, so `id` breaks the tie
# into a stable (if arbitrary) page order.
TASK_DEFAULT_ORDER = (col(Task.created_at), col(Task.id))
