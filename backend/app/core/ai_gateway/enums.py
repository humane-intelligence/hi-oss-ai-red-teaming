"""Closed-set enums for `AiModel` rows — surfaced on the wire, and in DB enum columns where noted."""

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
