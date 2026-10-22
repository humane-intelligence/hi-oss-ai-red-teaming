"""Closed-set enums for `AiModel` rows (on the wire, in DB enum columns where noted) and litellm's routing table."""

from enum import StrEnum


class ProviderVendor(StrEnum):
    """Vendor whose API a given `AiModel` row routes to.

    The set is intentionally closed — adding a provider requires a migration
    (`ALTER TYPE ... ADD VALUE`) so the gateway-side strategy table stays in
    sync with what the DB can hold.
    """

    HUGGINGFACE = "huggingface"
    GOOGLE = "google"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    AZURE = "azure"
    GENERIC = "generic"
    AWS_BEDROCK = "aws_bedrock"
    COHERE = "cohere"


# litellm's routing prefix per vendor: the adapter builds `<prefix>/<provider_model_id>` from it,
# and the save-time rule in `services.ai_models` refuses an id that already carries one. Kept
# beside the enum rather than in the adapter so the CRUD path reads it without importing litellm.
# `generic` = OpenAI-compatible endpoint, so it rides the openai prefix with a caller-supplied
# api_base.
LITELLM_PREFIX: dict[ProviderVendor, str] = {
    ProviderVendor.OPENAI: "openai",
    ProviderVendor.ANTHROPIC: "anthropic",
    ProviderVendor.GOOGLE: "gemini",
    ProviderVendor.AZURE: "azure",
    ProviderVendor.AWS_BEDROCK: "bedrock",
    ProviderVendor.COHERE: "cohere",
    ProviderVendor.HUGGINGFACE: "huggingface",
    ProviderVendor.GENERIC: "openai",
}


class Modality(StrEnum):
    """A kind of content a model takes in or gives back.

    A declaration is a set: the server dedupes it and re-sorts it into the order
    below, so equal declarations store and render identically. An input set must
    include `text` (every dispatched call carries a text prompt) and an output set
    must be non-empty — on create and on PATCH alike, which replaces a set wholesale
    rather than merging into it.

    Held in the `input_modalities` / `output_modalities` array columns as plain text
    rather than a DB enum, so adding a modality (audio, video) is a code change
    instead of an `ALTER TYPE`. The cost is that the column no longer refuses an
    out-of-set value at write time while every read path stays strict — unlike
    `AuditAction`, whose column and wire type are both `str` and which therefore
    passes an unknown value through harmlessly.
    """

    TEXT = "text"
    IMAGE = "image"


class WarmupStatus(StrEnum):
    """Result of probing a model endpoint for readiness (see `dispatch_probe`).

    `starting` is the scale-to-zero case: the endpoint is unreachable/initializing
    and the probe just triggered (or is awaiting) its wake — retry. `error` is a
    terminal fault (auth, bad request, a model that cannot answer in text) a retry won't fix.
    """

    READY = "ready"
    STARTING = "starting"
    ERROR = "error"


class HealthCheckStatus(StrEnum):
    """Result of a manual endpoint health check (see `ai_gateway.services.health`).

    `checking` is in flight (a Celery task is probing, possibly waiting out a cold
    start); `alive`/`dead` are settled. A `null` column means never checked.
    """

    CHECKING = "checking"
    ALIVE = "alive"
    DEAD = "dead"
