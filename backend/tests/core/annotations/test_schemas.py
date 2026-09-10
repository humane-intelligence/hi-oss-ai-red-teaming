"""Pure-logic tests for `app.core.annotations.schemas` — note and annotation payloads."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.annotations.schemas import NOTE_MAX_MESSAGE_IDS
from app.core.annotations.schemas import AnnotationCreate
from app.core.annotations.schemas import NoteCreate
from app.core.annotations.schemas import NoteUpdate
from app.core.annotations.schemas import NoteUpdateChanges


@pytest.mark.unit
@pytest.mark.parametrize("count", [1, NOTE_MAX_MESSAGE_IDS])
def test_note_create_accepts_a_selection_up_to_the_cap(count: int) -> None:
    payload = NoteCreate(
        conversation_id=uuid4(),
        message_ids=[uuid4() for _ in range(count)],
        text="note",
    )

    assert len(payload.message_ids) == count


@pytest.mark.unit
def test_note_create_rejects_a_selection_over_the_cap() -> None:
    """The cap bounds both the membership query and the ids echoed back in its 404."""
    with pytest.raises(ValidationError):
        NoteCreate(
            conversation_id=uuid4(),
            message_ids=[uuid4() for _ in range(NOTE_MAX_MESSAGE_IDS + 1)],
            text="note",
        )


@pytest.mark.unit
def test_note_create_rejects_a_repeated_message_id() -> None:
    """The same message twice is a caller mistake, not a two-message selection."""
    repeated = uuid4()
    with pytest.raises(ValidationError):
        NoteCreate(conversation_id=uuid4(), message_ids=[repeated, repeated], text="note")


@pytest.mark.unit
def test_note_update_changes_covers_every_updatable_field() -> None:
    """A field added to the wire schema must reach the service contract, or it is silently dropped."""
    assert set(NoteUpdate.model_fields) <= set(NoteUpdateChanges.model_fields)


@pytest.mark.unit
def test_note_update_rejects_the_immutable_message_set() -> None:
    """The selection is advertised as fixed, so sending it must 422 — not 200 having ignored it.

    This is the `extra="forbid"` divergence from `MessageFlagUpdate`, which defaults to
    dropping unknown fields.
    """
    with pytest.raises(ValidationError):
        NoteUpdate.model_validate({"text": "revised", "message_ids": [str(uuid4())]})


@pytest.mark.unit
def test_note_update_rejects_an_explicit_null_text() -> None:
    """`text` backs a NOT NULL column: omitting it leaves the row alone, `null` is a 422."""
    with pytest.raises(ValidationError):
        NoteUpdate.model_validate({"text": None})

    assert NoteUpdate.model_validate({}).model_fields_set == set()


@pytest.mark.unit
def test_annotation_create_accepts_a_catalog_pick() -> None:
    payload = AnnotationCreate(message_id=uuid4(), label_id=uuid4())

    assert payload.text is None


@pytest.mark.unit
def test_annotation_create_sanitises_an_adhoc_label() -> None:
    # Same single-visible-line rule as every chip-rendered label: whitespace runs fold,
    # invisible characters go.
    payload = AnnotationCreate(message_id=uuid4(), text="  jail\u200bbreak   attempt ")

    assert payload.text == "jailbreak attempt"


@pytest.mark.unit
def test_annotation_create_rejects_both_label_kinds() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        AnnotationCreate(message_id=uuid4(), label_id=uuid4(), text="jailbreak")


@pytest.mark.unit
def test_annotation_create_rejects_neither_label_kind() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        AnnotationCreate(message_id=uuid4())


@pytest.mark.unit
def test_annotation_create_treats_invisible_text_as_absent() -> None:
    # A payload of pure whitespace/invisibles must not slip past the exactly-one rule as a
    # "set" text and then insert an empty label.
    with pytest.raises(ValidationError, match="exactly one"):
        AnnotationCreate(message_id=uuid4(), text=" \u200b ­ ")
