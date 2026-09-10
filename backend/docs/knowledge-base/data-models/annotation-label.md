---
tags: [model, annotations, reference-data]
aliases: [AnnotationLabel, annotation_labels, label catalog, annotation labels]
---

# AnnotationLabel

The vocabulary a per-message **label picker** suggests. Flat reference data — anchored to no conversation, no evaluation, nothing.

Table `annotation_labels`, model in `app/core/annotations/models.py`, curated catalog in `app/core/annotations/label_catalog.py`, sync + reads in `app/core/annotations/services/annotation_labels.py`, endpoint `GET /api/v1/annotation-labels`.

> The **label** is not the [Note](note.md). A note is prose about a selection of messages; a label is a token you aggregate over. The annotation entity that *carries* a label doesn't exist yet — this table is the vocabulary it will point at, landed early so the table is created in its final shape rather than altered by the very next migration.

## Two kinds, as a database fact

`created_by_id` distinguishes them, exactly as [DataLicense](data-license.md) distinguishes curated from user-authored licences — and here a `CHECK` makes it a constraint rather than a convention:

```python
CheckConstraint("(created_by_id IS NULL) = (key IS NOT NULL)", name="kind_is_curated_xor_authored")
```

| Kind | `created_by_id` | `key` | Where it comes from |
|---|---|---|---|
| **curated** | `NULL` | set | code-shipped in `label_catalog.py`, reconciled by `sync_annotation_labels` |
| **user-scoped** | the author | `NULL` | a label an annotator names while annotating; never joins the shared list |

The CHECK matters because the two partial-unique indexes below don't span the kinds: without it, a hybrid row (both columns set, or neither) would slip past every uniqueness rule and belong to neither vocabulary. It is also what keeps `key` curated-only, so a future user-label writer cannot collide with a curated key that the id-keyed upsert can't see.

## Columns

Beyond `BaseModel` (`id`, `created_at`, `updated_at`, `deleted_at`, `deleted_by_id`):

| Column | Type | Notes |
|---|---|---|
| `created_by_id` | UUID NULL FK → `users.id`, idx | author of a user-scoped label; NULL = curated. **No delete rule** — `SET NULL` isn't even available, it would leave a row with neither author nor key and break the CHECK |
| `key` | VARCHAR(64) NULL | stable identity of a curated entry; NULL for user-scoped |
| `name` | VARCHAR(128) NOT NULL | the wording the picker shows |

Indexes, both partial:

- `ix_annotation_labels_key` — unique on `key` `WHERE created_by_id IS NULL AND deleted_at IS NULL`. One live row per key, so a retired key can be re-added after a tombstone.
- `ix_annotation_labels_author_name` — unique on `(created_by_id, lower(name))` `WHERE created_by_id IS NOT NULL AND deleted_at IS NULL`. Folded **in Postgres, never in Python** — the two disagree on dotted capitals.

## `key` is the identity, `name` is the display

Rewording an entry keeps whatever points at it. The row id is deterministic:

```python
# app/core/annotations/label_catalog.py
_CURATED_NAMESPACE = uuid.UUID("4d9f2c17-8b3e-4a6d-9c05-1f7e8a2b6d34")
# curated_label_id(key) = uuid5(_CURATED_NAMESPACE, key)
```

Identical in every environment and across reseeds, with no UUID literals to keep in sync — the same pattern as `curated_license_id` and the system roles.

## Sync is upsert-only

`sync_annotation_labels` is a deploy step after `migrate` (`make syncannotationlabels`, also part of `make seedlocal`). It is an idempotent upsert keyed on `curated_label_id`, whose conflict arm re-asserts `created_by_id = NULL`.

Because it **only ever upserts**, it applies additions and rewordings but never a removal:

- dropping an entry from the tuple leaves its row live and still suggested;
- re-keying an entry leaves the old one alongside the new.

Both have to be retired by hand until something owns pruning. A prune *is* scopable (`created_by_id IS NULL` tells curated rows from an annotator's), and is deliberately not done: retiring a label is a product decision about live data, not a deploy step, and a tombstone silently retires labels that existing rows still point at.

## Why a table and not a `DISTINCT` query

[AiModel](ai-model.md) builds its label suggestions the other way — `DISTINCT unnest(labels)` over the live rows, so an AI-model label exists exactly as long as some model carries it. That is an *emergent* vocabulary.

This one has to be prepared up front and carry a **stable id** an annotation can point at and an external taxonomy can later be reconciled against. Hence a table, and hence `key`.

## The endpoint

`GET /api/v1/annotation-labels` serves the **curated** rows only, ordered by display name, paginated like every list. Gated on `annotations:read` — deliberately not open to any authenticated caller the way the licence pick-list is, so the vocabulary stays out of reach of the role kept clear of the annotation surface (the red teamer) while a role that reads annotations without authoring them (the owner) does hold it.

An empty **first** page means the deploy's sync step has not run. Nothing else depends on these rows, so the picker simply has nothing to suggest.

## Related

- [Notes](../components/notes.md) — the prose sibling in the same `annotations` package
- [DataLicense](data-license.md) — the reference-data pattern this copies (curated vs user-authored, deterministic id, deploy-step sync)
- [AiModel](ai-model.md) — `labels`, the emergent vocabulary this is deliberately *not*
- [RBAC - global roles](../components/rbac-global-roles.md) — `annotations:read`
- [Data model overview](data-model-overview.md) — the full ERD
