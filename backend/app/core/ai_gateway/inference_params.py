"""Inference-params mixin, typed view, and cascade merge.

The platform attaches the same knob vocabulary to multiple carriers:
`AiModel` provides the baseline and `Evaluation` (via its
`EvaluationAiModel` assignments) layers overrides on top; `Scenario`
(landing later) extends the chain further. At dispatch time the effective
params are the most-specific-wins merge across the chain.

The storage layer is intentionally untyped (one JSONB column) so adding
a new knob is a Pydantic edit rather than a 3-table migration. The
edge-layer `InferenceParams` model carries the documentation and
validation for the knobs we use today; `extra="allow"` keeps the door
open for provider-specific keys that haven't earned a Python field yet.
"""

from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field as PydanticField
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field
from sqlmodel import SQLModel


class InferenceParamsMixin(SQLModel):
    """SQLModel mixin adding a `parameters` JSONB column to a carrier.

    Apply to any table whose rows participate in the inference-params
    cascade. The column is NOT NULL with a server-side `'{}'::jsonb`
    default so an unset row inherits everything from upstream — no
    NULL-vs-empty-dict ambiguity at the storage layer.

    Pair with `InferenceParams` at the API edge for typed validation, and
    with `merge_inference_params` at dispatch time to collapse the chain.
    """

    parameters: dict[str, Any] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'::jsonb")},
    )


class InferenceParams(BaseModel):
    """Typed view of the `parameters` JSONB column.

    `extra="allow"` keeps the door open for provider-specific knobs that
    haven't earned a Python field yet — the structured fields below carry
    documentation and validation for the ones the platform uses today.
    """

    model_config = ConfigDict(extra="allow")

    system_prompt: str | None = PydanticField(
        default=None, description="Prepended to every conversation by the gateway."
    )
    temperature: float | None = PydanticField(default=None, ge=0, le=2, description="Sampling temperature.")
    top_p: float | None = PydanticField(default=None, ge=0, le=1, description="Nucleus-sampling probability.")
    top_k: int | None = PydanticField(default=None, ge=0, description="Top-k sampling cutoff.")
    max_tokens: int | None = PydanticField(default=None, ge=1, description="Cap on generated tokens.")
    stop_sequences: list[str] | None = PydanticField(default=None, description="Sequences that abort generation.")
    frequency_penalty: float | None = PydanticField(
        default=None, ge=-2, le=2, description="OpenAI/Cohere-style frequency penalty."
    )
    presence_penalty: float | None = PydanticField(
        default=None, ge=-2, le=2, description="OpenAI/Cohere-style presence penalty."
    )
    repetition_penalty: float | None = PydanticField(default=None, ge=0, description="HF-style repetition penalty.")
    seed: int | None = PydanticField(default=None, description="Deterministic-sampling seed where supported.")


def dump_inference_params(params: InferenceParams) -> dict[str, Any]:
    """Serialize an `InferenceParams` to the JSONB storage shape.

    The single write-path conversion for every `parameters` column (model,
    evaluation, scenario): `exclude_none` drops unset knobs so a level stores
    only the keys it actually overrides — never a `null`. `merge_inference_params`
    relies on that, reading an absent key as "inherit" with no "clear" state.
    On a partial update, dump from the model rather than the `exclude_unset`
    payload dict, so a knob explicitly set to `null` is stripped too.
    """
    return params.model_dump(exclude_none=True)


def merge_inference_params(*levels: dict[str, Any] | None) -> dict[str, Any]:
    """Merge inference-param dicts most-specific-last.

    Pass dicts in cascade order (least specific first): model → evaluation
    → scenario. Two-state per knob: a key present at a level overrides
    upstream; a key absent inherits. There is no "clear" state — the write
    path strips null-valued knobs (`exclude_none`) so stored dicts never
    carry a `None`, and a knob you want unset is simply omitted.

    `None` input is treated as an empty dict (no overrides at that level)
    so callers can pass `evaluation.parameters if evaluation else None`
    without a conditional.

    Args:
        *levels: Param dicts in cascade order, least specific first.

    Returns:
        A new dict containing the merged keys.
    """
    merged: dict[str, Any] = {}
    for level in levels:
        if level:
            merged.update(level)
    return merged


# Inference-param keys safe to surface under model masking: numeric sampling
# knobs that describe behaviour without revealing identity. Excludes
# `system_prompt` (free text) and `stop_sequences`/provider-specific keys —
# operator-entered content that can leak which model backs a masked assignment.
MASKABLE_PARAM_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "seed",
    }
)


def mask_inference_params(params: dict[str, Any]) -> dict[str, Any]:
    """Drop everything but the identity-safe numeric knobs, for masked projections."""
    return {key: value for key, value in params.items() if key in MASKABLE_PARAM_KEYS}
