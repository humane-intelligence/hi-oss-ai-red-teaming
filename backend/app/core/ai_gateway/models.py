"""AI-model registry table.

One row per "model available on the platform" — the gateway looks rows up by
`model_alias` to dispatch a chat / completion call to the provider named by
`provider` (`app.core.ai_gateway.dispatch`). The system message is the winning
`system_prompt` from the param cascade — this row's `parameters`, replaced key-wise
by any caller level, never concatenated — joined with the gateway-owned
`system_suffix` that carries tag context out-of-band. `advanced_params_disabled`
empties the cascade, row and caller alike, leaving only the suffix.

Secrets are never stored in plaintext: `api_key_encrypted` holds a JWE compact
ciphertext produced by `app.core.ai_gateway.crypto.encrypt_secret` and is
never serialized in any HTTP response.
"""

from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.core.ai_gateway.enums import HealthCheckStatus
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.inference_params import InferenceParamsMixin
from app.core.base_model import BaseModel

# Width of `AiModel.capability_mismatch`, exported because the probe truncates against it. The
# finding is written with a Core `update()`, so neither Pydantic nor SQLModel checks the length:
# that truncation is the only enforcement, with the Postgres varchar as the backstop.
MISMATCH_MAX_LEN = 255

# Row fields the image verdict is a function of: the vendor, the model id and the base URL that
# shape the call, plus the declaration that lets an image part be sent at all. A finding survives
# only while all four still hold. The credential and the merged `params` shape the call too and
# stay out — neither decides acceptance. Read by the clear in `update_model`, the settle guard in
# `run_health_check` and the re-check trigger on the routes — one definition so the three cannot
# drift apart. The trigger matches these names against audit-snapshot keys, which a test pins.
PROBED_CALL_FIELDS = ("input_modalities", "inference_endpoint", "provider", "provider_model_id")


class AiModel(BaseModel, InferenceParamsMixin, table=True):
    __tablename__ = "ai_models"
    __table_args__ = (
        Index(
            "ix_ai_models_name",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_ai_models_model_alias",
            "model_alias",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: str = Field(max_length=255, nullable=False)
    # Absent from `EvaluationAiModelView` — the per-evaluation model list a red-teamer reads.
    description: str | None = Field(default=None, sa_type=Text)
    model_alias: str = Field(max_length=128, nullable=False)
    provider: ProviderVendor = Field(
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            ProviderVendor,
            values_callable=lambda enum: [m.value for m in enum],
            name="providervendor",
        ),
        sa_column_kwargs={"nullable": False, "index": True},
    )
    # What the model takes in and gives back, as declared by an operator — nothing
    # detects it. `input_modalities` gates image attachments and `output_modalities`
    # gates dispatch (which needs a text reply). Both are validated and canonicalised
    # by the request schemas, so a row written through the API reads back deduped and
    # in enum order — the column itself enforces neither, nor set membership.
    input_modalities: list[Modality] = Field(
        default_factory=lambda: [Modality.TEXT],
        sa_type=ARRAY(String),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False, "server_default": text("'{text}'")},
    )
    output_modalities: list[Modality] = Field(
        default_factory=lambda: [Modality.TEXT],
        sa_type=ARRAY(String),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False, "server_default": text("'{text}'")},
    )
    provider_model_id: str = Field(max_length=255, nullable=False)
    endpoint_name: str | None = Field(default=None, max_length=255)
    inference_endpoint: str | None = Field(default=None, max_length=1024)
    # Opt-in flag for endpoints that scale to zero (self-hosted / serverless GPU): the
    # client warms them before the first message so a cold start doesn't fail it. An
    # explicit operator toggle, not derived from `provider`, so it can be surfaced on a
    # masked assignment without leaking the model's identity.
    warmup_enabled: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    # Stored overrides are kept, not deleted, so flipping the flag back restores them.
    advanced_params_disabled: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    icon_file: str | None = Field(default=None, max_length=255)
    # Free-form operator annotations ("self-hosted", "fine-tuning needed"), shown as badges on the
    # registry view. There is no label catalog: the set a console offers is `DISTINCT unnest(labels)`
    # over the live rows, so a label exists exactly as long as some model carries it. Canonical form
    # (sanitised, deduped case-insensitively, sorted) is enforced by the request schemas like the
    # modality sets above — the column itself enforces neither.
    labels: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'")},
    )
    # `extras` carries provider call-shape metadata (auth, apiUrl, body
    # template, …) — model-specific and not part of the inference-params
    # cascade, so it stays defined directly here instead of moving to
    # `InferenceParamsMixin`.
    extras: dict[str, Any] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'::jsonb")},
    )
    # JWE compact ciphertext from app.core.ai_gateway.crypto.encrypt_secret —
    # never round-tripped through any response schema.
    api_key_encrypted: str | None = Field(default=None, max_length=1024)
    disabled_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    # Manual per-model health check. All nullable; `null` health_check_status = never checked.
    health_check_status: HealthCheckStatus | None = Field(
        default=None,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            HealthCheckStatus,
            values_callable=lambda enum: [member.value for member in enum],
            name="healthcheckstatus",
        ),
        sa_column_kwargs={"nullable": True},
    )
    last_health_check_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    last_health_reason: str | None = Field(default=None, max_length=64)
    # Outcome of the last capability probe; `null` = nothing contradicted the row
    # (or there was nothing to probe). Deliberately not folded into
    # `health_check_status`: an endpoint that answers but refuses an image is alive,
    # and "fix the endpoint" vs "fix the declaration" are different remedies.
    capability_mismatch: str | None = Field(default=None, max_length=MISMATCH_MAX_LEN)
    last_healthy_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    # Inactivity alerting: usage and warmup stamps are kept apart because warmup-only
    # traffic is exactly the keep-warm-without-usage pattern the alert exists to catch.
    inactivity_alert_hours: int | None = Field(default=None, sa_column_kwargs={"nullable": True})
    last_used_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    last_warmup_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    inactivity_alerted_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )

    def disable(self) -> None:
        """Mark this model as disabled. Caller must flush/commit the session."""
        self.disabled_at = datetime.now(UTC)

    def restore(self) -> None:
        """Clear the tombstone and re-arm the inactivity alert. Caller must flush/commit.

        Unconditional, unlike `enable()`: the restore path resolves through
        `deleted_select`, so this only ever runs on a genuinely tombstoned row.
        """
        super().restore()
        self.inactivity_alerted_at = None

    def enable(self) -> None:
        """Clear the disabled flag and re-arm the inactivity alert. Caller must flush/commit.

        Re-arms only on a real disabled→enabled transition: the console sends `is_disabled`
        on every save, so a no-op enable from an unrelated edit must not restart the episode.
        """
        if self.disabled_at is not None:
            self.inactivity_alerted_at = None
        self.disabled_at = None

    @property
    def is_disabled(self) -> bool:
        return self.disabled_at is not None
