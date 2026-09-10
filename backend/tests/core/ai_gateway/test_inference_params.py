"""Pure-logic tests for `app.core.ai_gateway.inference_params`."""

import pytest

from app.core.ai_gateway.inference_params import InferenceParams
from app.core.ai_gateway.inference_params import dump_inference_params
from app.core.ai_gateway.inference_params import mask_inference_params
from app.core.ai_gateway.inference_params import merge_inference_params


@pytest.mark.unit
def test_dump_strips_unset_knobs() -> None:
    """Only the knobs the caller actually set survive — no `null` entries."""
    dumped = dump_inference_params(InferenceParams.model_validate({"temperature": 0.5, "top_p": None}))

    assert dumped == {"temperature": 0.5}


@pytest.mark.unit
def test_dump_empty_params_is_empty_dict() -> None:
    assert dump_inference_params(InferenceParams()) == {}


@pytest.mark.unit
def test_dump_keeps_unknown_keys() -> None:
    dumped = dump_inference_params(InferenceParams.model_validate({"logit_bias": {"50256": -100}}))

    assert dumped == {"logit_bias": {"50256": -100}}


@pytest.mark.unit
def test_merge_returns_empty_dict_when_no_levels_have_overrides() -> None:
    assert merge_inference_params() == {}
    assert merge_inference_params(None, None, {}) == {}


@pytest.mark.unit
def test_merge_treats_none_input_as_empty_level() -> None:
    """`evaluation.parameters if evaluation else None` should pass through cleanly."""
    assert merge_inference_params({"temperature": 0.5}, None) == {"temperature": 0.5}


@pytest.mark.unit
def test_merge_inherits_keys_when_downstream_omits_them() -> None:
    model = {"temperature": 0.7, "max_tokens": 1024}
    evaluation = {"temperature": 0.3}  # max_tokens omitted → inherits

    assert merge_inference_params(model, evaluation) == {"temperature": 0.3, "max_tokens": 1024}


@pytest.mark.unit
def test_merge_most_specific_wins() -> None:
    model = {"temperature": 0.7}
    evaluation = {"temperature": 0.3}
    scenario = {"temperature": 1.5}

    assert merge_inference_params(model, evaluation, scenario) == {"temperature": 1.5}


@pytest.mark.unit
def test_merge_carries_unknown_keys_through() -> None:
    """Provider-specific keys (`logit_bias`, …) ride along via `extra='allow'`."""
    model = {"temperature": 0.7, "logit_bias": {"50256": -100}}
    scenario = {"temperature": 0.3}

    merged = merge_inference_params(model, scenario)

    assert merged["logit_bias"] == {"50256": -100}
    assert merged["temperature"] == 0.3


@pytest.mark.unit
def test_mask_keeps_only_identity_safe_numeric_knobs() -> None:
    masked = mask_inference_params(
        {"temperature": 0.5, "top_p": 0.9, "system_prompt": "You are Claude.", "logit_bias": {"1": -1}}
    )

    assert masked == {"temperature": 0.5, "top_p": 0.9}


@pytest.mark.unit
def test_mask_drops_stop_sequences_and_provider_extras() -> None:
    """`stop_sequences` (operator free text) and unknown provider keys are withheld."""
    masked = mask_inference_params({"seed": 7, "stop_sequences": ["</claude>"], "deployment": "prod-eu"})

    assert masked == {"seed": 7}


@pytest.mark.unit
def test_inference_params_accepts_known_keys() -> None:
    params = InferenceParams.model_validate(
        {"temperature": 0.7, "top_p": 0.95, "max_tokens": 1024, "stop_sequences": ["</end>"]}
    )

    assert params.temperature == 0.7
    assert params.top_p == 0.95
    assert params.max_tokens == 1024
    assert params.stop_sequences == ["</end>"]


@pytest.mark.unit
def test_inference_params_allows_unknown_keys() -> None:
    params = InferenceParams.model_validate({"temperature": 0.7, "logit_bias": {"50256": -100}})

    dumped = params.model_dump()
    assert dumped["temperature"] == 0.7
    assert dumped["logit_bias"] == {"50256": -100}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("top_p", 1.1),
        ("top_p", -0.1),
        ("top_k", -1),
        ("max_tokens", 0),
        ("frequency_penalty", 2.5),
        ("presence_penalty", -2.5),
        ("repetition_penalty", -0.1),
    ],
)
def test_inference_params_rejects_out_of_range(field: str, bad_value: float) -> None:
    with pytest.raises(ValueError, match=field):
        InferenceParams.model_validate({field: bad_value})
