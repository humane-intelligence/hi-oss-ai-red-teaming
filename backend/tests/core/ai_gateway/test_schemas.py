"""Unit tests for the `/v1/ai-models` schema validators."""

import pytest
from pydantic import ValidationError

from app.core.ai_gateway.schemas import MAX_LABEL_LENGTH
from app.core.ai_gateway.schemas import MAX_LABELS
from app.core.ai_gateway.schemas import AiModelCreate
from app.core.ai_gateway.schemas import AiModelUpdate


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "name": "Local SLM",
        "model_alias": "local-slm",
        "provider": "generic",
        "provider_model_id": "qwen",
        "inference_endpoint": "http://slm:8080/v1",
        **overrides,
    }


@pytest.mark.unit
@pytest.mark.parametrize("endpoint", ["slm:8080/v1", "//slm:8080/v1", "ftp://slm/v1", "", "   "])
def test_create_rejects_an_endpoint_without_an_http_scheme(endpoint: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        AiModelCreate.model_validate(_payload(inference_endpoint=endpoint))

    assert exc_info.value.errors()[0]["loc"] == ("inference_endpoint",)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("supplied", "stored"),
    [
        ("http://slm:8080/v1", "http://slm:8080/v1"),
        # The scheme matches case-insensitively (RFC 3986) but is stored as typed.
        ("HTTPS://proxy.internal/v1", "HTTPS://proxy.internal/v1"),
        ("  https://proxy.internal/v1\n", "https://proxy.internal/v1"),
    ],
)
def test_create_keeps_an_absolute_url_and_strips_its_padding(supplied: str, stored: str) -> None:
    model = AiModelCreate.model_validate(_payload(inference_endpoint=supplied))

    assert model.inference_endpoint == stored


@pytest.mark.unit
def test_update_holds_the_endpoint_to_the_same_rule() -> None:
    with pytest.raises(ValidationError):
        AiModelUpdate.model_validate({"inference_endpoint": "slm:8080/v1"})


@pytest.mark.unit
def test_update_still_clears_the_endpoint_with_an_explicit_null() -> None:
    assert AiModelUpdate.model_validate({"inference_endpoint": None}).inference_endpoint is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("supplied", "stored"),
    [
        ([], []),
        (["self-hosted"], ["self-hosted"]),
        # Sorted case-insensitively, so equal sets have one stored form.
        (["self-hosted", "Fine-tuning needed"], ["Fine-tuning needed", "self-hosted"]),
        # Discriminating pair: a plain `sorted()` puts every capital first and would answer
        # ["Zebra", "apple"], so this is what holds the documented case-insensitive order.
        (["Zebra", "apple"], ["apple", "Zebra"]),
        # Whitespace runs (incl. newlines/tabs) collapse and the padding goes.
        (["  fine-tuning\t\nneeded  "], ["fine-tuning needed"]),
        # Case-only duplicates collapse to the first spelling seen.
        (["Self-Hosted", "self-hosted", "SELF-HOSTED"], ["Self-Hosted"]),
        # A zero-width joiner is invisible on a badge, so it can't be what distinguishes two labels
        # (spelled as an escape — an editor renders the literal as the pair it must not be confused with).
        (["self\u200dhosted", "selfhosted"], ["selfhosted"]),
    ],
)
def test_create_canonicalises_labels(supplied: list[str], stored: list[str]) -> None:
    model = AiModelCreate.model_validate(_payload(labels=supplied))

    assert model.labels == stored


@pytest.mark.unit
@pytest.mark.parametrize(
    ("labels", "offender"),
    [([""], ""), (["   "], "   "), (["\u200b"], "\u200b"), (["ok", "\t"], "\t")],
)
def test_create_rejects_a_label_that_normalises_to_nothing(labels: list[str], offender: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        AiModelCreate.model_validate(_payload(labels=labels))

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("labels",)
    assert repr(offender) in error["msg"]


@pytest.mark.unit
def test_create_rejects_a_label_longer_than_the_cap() -> None:
    with pytest.raises(ValidationError):
        AiModelCreate.model_validate(_payload(labels=["x" * (MAX_LABEL_LENGTH + 1)]))


@pytest.mark.unit
def test_create_measures_the_label_cap_on_the_submitted_value() -> None:
    """The cap counts what the client sent, which is what the published `maxLength` promises.

    Padding a label to exactly the maximum and relying on the sanitiser to trim it back under would
    make the published bound a lie, so it is refused — the client can check the same thing locally.
    """
    padded = f"  {'x' * MAX_LABEL_LENGTH}  "

    with pytest.raises(ValidationError):
        AiModelCreate.model_validate(_payload(labels=[padded]))

    trimmed = AiModelCreate.model_validate(_payload(labels=["x" * MAX_LABEL_LENGTH]))
    assert trimmed.labels == ["x" * MAX_LABEL_LENGTH]


@pytest.mark.unit
def test_create_rejects_more_distinct_labels_than_the_cap() -> None:
    with pytest.raises(ValidationError):
        AiModelCreate.model_validate(_payload(labels=[f"label-{i}" for i in range(MAX_LABELS + 1)]))


@pytest.mark.unit
def test_create_counts_the_label_cap_on_the_submitted_length() -> None:
    """`maxItems` bounds the array as sent, so spellings that would collapse still count.

    Deduplication is what gets *stored*, not a way under the cap: a client that dedupes its own
    chips never submits the rejected shape, and one that does not gets the same answer the published
    schema already gave it.
    """
    with pytest.raises(ValidationError):
        AiModelCreate.model_validate(_payload(labels=["self-hosted"] * (MAX_LABELS + 5)))

    under_the_cap = AiModelCreate.model_validate(_payload(labels=["self-hosted", "Self-Hosted"]))
    assert under_the_cap.labels == ["self-hosted"]


@pytest.mark.unit
def test_update_replaces_the_label_set_wholesale() -> None:
    assert AiModelUpdate.model_validate({"labels": ["b", "A"]}).labels == ["A", "b"]


@pytest.mark.unit
def test_update_clears_the_labels_with_an_empty_list() -> None:
    payload = AiModelUpdate.model_validate({"labels": []})

    assert payload.labels == []
    assert "labels" in payload.model_fields_set


@pytest.mark.unit
def test_update_rejects_explicit_null_labels() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AiModelUpdate.model_validate({"labels": None})

    assert exc_info.value.errors()[0]["loc"] == ("labels",)
