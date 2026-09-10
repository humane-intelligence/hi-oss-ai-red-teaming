"""Curated annotation-label catalog — code-shipped seed data for the `annotation_labels` table.

Placeholder reference data: a starter red-teaming vocabulary, so the label picker suggests
something useful on a fresh install rather than nothing. `sync_annotation_labels` (see
`services/annotation_labels.py`) upserts these rows keyed by the deterministic
`curated_label_id`, the same reference-data pattern as the curated data licenses and the
system roles.

The sync only ever upserts, so it applies **additions and rewordings** on the next deploy —
never a removal. Dropping an entry from this tuple leaves its row live and still suggested
until something owns pruning. That matters most for the wholesale replacement below, which
would otherwise yield the old vocabulary *plus* the new one.

Re-keying an entry **without changing its wording** aborts the next deploy: the row under the old
key stays live holding that wording, and `sync_annotation_labels` refuses a wording another live
row already holds. Re-keying *and* rewording collides with nothing, so it succeeds and leaves the
old row live alongside the new; both have to be retired by hand until something owns pruning.
Either way the sync will not choose — which row survives, and where the annotations already
pointing at it go, is a data decision.

A prune is *scopable* — `created_by_id IS NULL` tells the curated rows from an annotator's, so
one could tombstone every curated row this tuple no longer names without touching a user's.
Deliberately not done: retiring a label is a product decision about live data, not a deploy
step, and a tombstone silently retires labels that existing rows still point at.

Deliberately *not* a distinct-over-usage query, which is how AI-model labels build their
suggestion set: that vocabulary is emergent (a label exists while some model wears it),
whereas this one has to be prepared up front and carry a stable id an annotation can point
at and an external taxonomy can later be reconciled against.

This tuple is the **curated** half of the vocabulary, not the whole of it and not a cache of
what has been used: a wording this list does not hold, named while annotating, becomes a row of
its own scoped to that annotator (`created_by_id` set, no `key`); one it does hold resolves to
the curated row instead. Neither path ever adds to this list.
"""

import uuid
from dataclasses import dataclass

# Fixed namespace so a curated label's row id derives from its `key`, identically in every
# environment and across reseeds, with no UUID literals to keep in sync.
_CURATED_NAMESPACE = uuid.UUID("4d9f2c17-8b3e-4a6d-9c05-1f7e8a2b6d34")


@dataclass(frozen=True, slots=True)
class CuratedAnnotationLabel:
    """One catalog entry: its stable `key` and the wording the picker shows."""

    key: str
    name: str


def curated_label_id(key: str) -> uuid.UUID:
    """Deterministic `annotation_labels.id` for a curated label (stable across envs and reseeds)."""
    return uuid.uuid5(_CURATED_NAMESPACE, key)


# Placeholder taxonomy, expected to be replaced wholesale once one is pulled from the
# external annotation platform. `key` is the identity, so rewording a `name` keeps the
# annotations already pointing at it; renaming a `key` is a new entry and orphans them.
CURATED_ANNOTATION_LABELS: tuple[CuratedAnnotationLabel, ...] = (
    CuratedAnnotationLabel(key="harmful-instructions", name="Harmful instructions"),
    CuratedAnnotationLabel(key="pii-leak", name="PII leak"),
    CuratedAnnotationLabel(key="jailbreak", name="Jailbreak"),
    CuratedAnnotationLabel(key="hallucination", name="Hallucination"),
    CuratedAnnotationLabel(key="bias", name="Bias"),
    CuratedAnnotationLabel(key="refusal", name="Refusal"),
    CuratedAnnotationLabel(key="off-policy", name="Off-policy"),
)
