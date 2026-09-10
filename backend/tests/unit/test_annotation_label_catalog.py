"""Unit tests for the curated annotation-label catalog (pure logic, no I/O)."""

import re
from uuid import UUID

import pytest

from app.core.annotations.label_catalog import CURATED_ANNOTATION_LABELS
from app.core.annotations.label_catalog import curated_label_id
from app.core.helpers import sanitise_single_line


@pytest.mark.unit
def test_curated_label_id_is_deterministic_and_distinct() -> None:
    # The whole point of deriving the id from the key: a resync in any environment reconciles
    # the same row rather than inserting a second one.
    assert curated_label_id("jailbreak") == curated_label_id("jailbreak")
    assert curated_label_id("jailbreak") != curated_label_id("bias")


@pytest.mark.unit
def test_curated_label_ids_are_pinned_to_the_namespace() -> None:
    # The namespace literal is load-bearing: changing it silently moves every row id, which
    # once annotations reference them is a mass orphaning with no error anywhere. Pinned
    # against a hard-coded uuid so an edit to `_CURATED_NAMESPACE` fails here instead.
    assert curated_label_id("jailbreak") == UUID("3878a313-116a-584e-8da2-a68de796d5b9")


@pytest.mark.unit
def test_curated_keys_and_names_are_unique() -> None:
    keys = [label.key for label in CURATED_ANNOTATION_LABELS]
    names = [label.name for label in CURATED_ANNOTATION_LABELS]

    # A duplicate key would collapse two entries onto one row on sync.
    assert len(keys) == len(set(keys))
    # Two entries sharing a wording — exactly, or only up to case, since that is how the wording is
    # looked up — become two curated rows for one wording. The picker hides that (it dedupes on the
    # folded name and drops the higher id) and so does the annotate path, so nothing surfaces:
    # the wording just persists split across two ids, and the sync cannot catch it because both are
    # shipped ids it skips. Matters most when the placeholder set is swapped for an imported
    # taxonomy, which may well reuse a wording across keys; this fails at the source instead.
    assert len(names) == len(set(names))
    assert len({name.lower() for name in names}) == len(names)


@pytest.mark.unit
def test_curated_keys_are_slugs() -> None:
    # The key is the stable identity an annotation points at, so it must not read as display
    # wording that someone would "fix" — that would orphan the rows referencing it.
    for label in CURATED_ANNOTATION_LABELS:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", label.key), label.key
        assert label.name


@pytest.mark.unit
def test_curated_names_survive_the_sanitiser_unchanged() -> None:
    # The reuse chain compares a *sanitised* typed string against the name written verbatim by the
    # sync, so a wording carrying a double space, an NBSP or a soft hyphen would be unreachable by
    # typing *and* invisible to the sync guard — the duplicate-label bug back, with no error
    # anywhere. Today's entries are clean; the imported taxonomy is where such a wording arrives.
    for label in CURATED_ANNOTATION_LABELS:
        assert sanitise_single_line(label.name) == label.name, label.name


@pytest.mark.unit
def test_placeholder_taxonomy_is_the_agreed_starter_set() -> None:
    # Placeholder reference data, agreed with the reporter — pinned so swapping it for a real
    # taxonomy is a deliberate edit rather than accidental drift.
    assert [label.key for label in CURATED_ANNOTATION_LABELS] == [
        "harmful-instructions",
        "pii-leak",
        "jailbreak",
        "hallucination",
        "bias",
        "refusal",
        "off-policy",
    ]
