"""rename annotations to notes

Rewriting `audit_logs` is a deliberate one-time exception to the table's append-only
rule: leaving the old rows would make `annotation.create` mean *note* before this
migration and the per-message label entity after it — one string, two entities,
silently date-dependent.

Revision ID: d3f7b1c2a904
Revises: 3098e9b5afba
Create Date: 2026-08-25 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3f7b1c2a904"
down_revision: str | None = "3098e9b5afba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (old, new) for every index and constraint carrying the entity's name.
_INDEXES = [
    ("ix_annotations_conversation_id", "ix_notes_conversation_id"),
    ("ix_annotations_evaluation_id", "ix_notes_evaluation_id"),
    ("ix_annotations_evaluation_group_id", "ix_notes_evaluation_group_id"),
    ("ix_annotations_created_by_id", "ix_notes_created_by_id"),
    ("ix_annotated_messages_message_id", "ix_noted_messages_message_id"),
]

_CONSTRAINTS = [
    ("notes", "pk_annotations", "pk_notes"),
    ("notes", "fk_annotations_conversation_id_conversations", "fk_notes_conversation_id_conversations"),
    ("notes", "fk_annotations_created_by_id_users", "fk_notes_created_by_id_users"),
    (
        "notes",
        "fk_annotations_evaluation_group_id_evaluation_groups",
        "fk_notes_evaluation_group_id_evaluation_groups",
    ),
    ("notes", "fk_annotations_evaluation_id_evaluations", "fk_notes_evaluation_id_evaluations"),
    ("noted_messages", "pk_annotated_messages", "pk_noted_messages"),
    ("noted_messages", "fk_annotated_messages_annotation_id_annotations", "fk_noted_messages_note_id_notes"),
    ("noted_messages", "fk_annotated_messages_message_id_messages", "fk_noted_messages_message_id_messages"),
]

_PERMISSION_REWRITE = sa.text("""
    UPDATE roles
    SET permissions = (
        SELECT jsonb_agg(permission ORDER BY permission)
        FROM (
            SELECT regexp_replace(perm, :old_prefix, :new_prefix) AS permission
            FROM jsonb_array_elements_text(roles.permissions) AS perm
        ) rewritten
    )
    -- Load-bearing guard: over an *empty* `permissions` array (the column default) the
    -- subquery aggregates zero rows and `jsonb_agg` returns NULL, which the NOT NULL
    -- column would reject. A non-matching element is not the hazard — `regexp_replace`
    -- passes it through. Matches system roles too; harmless, `sync_system_roles`
    -- rewrites those from code anyway.
    WHERE permissions::text LIKE :match
""")

# Bind values for the permission rewrite, exported so the behaviour tests drive the real
# ones instead of their own copies — a drift between the two would otherwise pass.
_PERMISSIONS_TO_NOTES = {"old_prefix": "^annotations:", "new_prefix": "notes:", "match": "%annotations:%"}
_PERMISSIONS_TO_ANNOTATIONS = {"old_prefix": "^notes:", "new_prefix": "annotations:", "match": "%notes:%"}

# Anchored, like the permission rewrite above: only the leading segment moves, so a
# three-segment action survives intact if the catalog ever grows one.
#
# The pattern is inlined per direction rather than bound. Offline mode renders binds as
# literals (`literal_binds=True` in `env.py`), and the PG compiler doubles the backslash —
# `'^annotation\\.'` matches nothing under `standard_conforming_strings = on`, so
# `alembic upgrade --sql` would emit the DDL and silently skip the rewrite. A literal in the
# SQL text has nothing to escape and renders identically online and offline.
_AUDIT_ACTION_TO_NOTE = sa.text(
    r"UPDATE audit_logs SET action = regexp_replace(action, '^annotation\.', 'note.') "
    r"WHERE action ~ '^annotation\.'"
)
_AUDIT_ACTION_TO_ANNOTATION = sa.text(
    r"UPDATE audit_logs SET action = regexp_replace(action, '^note\.', 'annotation.') "
    r"WHERE action ~ '^note\.'"
)

_OBJECT_TYPE_TO_NOTE = sa.text("UPDATE audit_logs SET object_type = 'note' WHERE object_type = 'annotation'")
_OBJECT_TYPE_TO_ANNOTATION = sa.text("UPDATE audit_logs SET object_type = 'annotation' WHERE object_type = 'note'")


def upgrade() -> None:
    op.rename_table("annotations", "notes")
    op.rename_table("annotated_messages", "noted_messages")
    op.alter_column("noted_messages", "annotation_id", new_column_name="note_id")
    for old, new in _INDEXES:
        op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')
    for table, old, new in _CONSTRAINTS:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')
    op.execute(_PERMISSION_REWRITE.bindparams(**_PERMISSIONS_TO_NOTES))
    op.execute(_AUDIT_ACTION_TO_NOTE)
    op.execute(_OBJECT_TYPE_TO_NOTE)


def downgrade() -> None:
    op.execute(_OBJECT_TYPE_TO_ANNOTATION)
    op.execute(_AUDIT_ACTION_TO_ANNOTATION)
    op.execute(_PERMISSION_REWRITE.bindparams(**_PERMISSIONS_TO_ANNOTATIONS))
    for table, old, new in _CONSTRAINTS:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{new}" TO "{old}"')
    for old, new in _INDEXES:
        op.execute(f'ALTER INDEX "{new}" RENAME TO "{old}"')
    op.alter_column("noted_messages", "note_id", new_column_name="annotation_id")
    op.rename_table("noted_messages", "annotated_messages")
    op.rename_table("notes", "annotations")
