"""Behavior tests for the annotation→note rename's data rewrites (revision `d3f7b1c2a904`).

The upgrade only runs once at test-session setup, over empty tables, so no rewrite ever
touches a row there. All three are exercised here against seeded rows: the JSONB permission
rebuild (custom roles only — `sync_system_roles` owns the canonical ones), the audit-trail
prefix move and the `object_type` swap, each in both directions.

Every statement *and* every bind value comes off the migration module, so a drift between
what ships and what is asserted fails here rather than passing quietly.
"""

import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.models import AuditLog
from app.core.auth.models import Role

pytestmark = pytest.mark.integration

_REVISION = "d3f7b1c2a904"


def _from_migration(name: str):
    """A rewrite statement or its bind values, off the migration module (no file-path import)."""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    return getattr(script.get_revision(_REVISION).module, name)


async def _persist_role(db_session: AsyncSession, *, name: str, permissions: list[str]) -> Role:
    role = Role(name=name, permissions=permissions)
    db_session.add(role)
    await db_session.flush()
    return role


async def _permissions_of(db_session: AsyncSession, role_id: uuid.UUID) -> list[str]:
    statement = Role.live_select().where(col(Role.id) == role_id)
    role = (await db_session.execute(statement.execution_options(populate_existing=True))).scalar_one()
    return role.permissions


async def _persist_audit_row(db_session: AsyncSession, *, action: str, object_type: str) -> AuditLog:
    row = AuditLog(action=action, object_type=object_type, object_id=uuid.uuid4())
    db_session.add(row)
    await db_session.flush()
    return row


async def _audit_row(db_session: AsyncSession, row_id: uuid.UUID) -> tuple[str, str | None]:
    statement = AuditLog.live_select().where(col(AuditLog.id) == row_id)
    row = (await db_session.execute(statement.execution_options(populate_existing=True))).scalar_one()
    return row.action, row.object_type


async def _rewrite_permissions(db_session: AsyncSession, direction: str) -> None:
    statement = _from_migration("_PERMISSION_REWRITE").bindparams(**_from_migration(direction))
    await db_session.execute(statement)


async def test_permission_rewrite_moves_only_the_annotation_prefix(db_session: AsyncSession) -> None:
    role = await _persist_role(
        db_session,
        name=f"custom-{uuid.uuid4()}",
        # `reviews:annotate` shares the word but not the prefix — it must survive untouched, and
        # `custom:annotations:read` pins the `^` anchor: unanchored, the pattern would eat it too.
        permissions=[
            "annotations:read",
            "annotations:delete",
            "flags:read",
            "reviews:annotate",
            "custom:annotations:read",
        ],
    )

    await _rewrite_permissions(db_session, "_PERMISSIONS_TO_NOTES")

    assert await _permissions_of(db_session, role.id) == [
        "custom:annotations:read",
        "flags:read",
        "notes:delete",
        "notes:read",
        "reviews:annotate",
    ]


async def test_permission_rewrite_round_trips(db_session: AsyncSession) -> None:
    original = ["annotations:read", "annotations:update", "flags:read"]
    role = await _persist_role(db_session, name=f"custom-{uuid.uuid4()}", permissions=original)

    await _rewrite_permissions(db_session, "_PERMISSIONS_TO_NOTES")
    await _rewrite_permissions(db_session, "_PERMISSIONS_TO_ANNOTATIONS")

    assert await _permissions_of(db_session, role.id) == sorted(original)


async def test_permission_rewrite_leaves_unrelated_role_alone(db_session: AsyncSession) -> None:
    # `regexp_replace` passes a non-matching element straight through, so the row would
    # survive even without the WHERE guard — the guard is for the empty array below.
    untouched = ["flags:read", "reviews:read"]
    role = await _persist_role(db_session, name=f"custom-{uuid.uuid4()}", permissions=untouched)

    await _rewrite_permissions(db_session, "_PERMISSIONS_TO_NOTES")

    assert await _permissions_of(db_session, role.id) == untouched


async def test_permission_rewrite_survives_an_empty_array(db_session: AsyncSession) -> None:
    role = await _persist_role(db_session, name=f"custom-{uuid.uuid4()}", permissions=[])

    await _rewrite_permissions(db_session, "_PERMISSIONS_TO_NOTES")

    assert await _permissions_of(db_session, role.id) == []


async def test_audit_action_rewrite_moves_the_prefix(db_session: AsyncSession) -> None:
    renamed = await _persist_audit_row(db_session, action="annotation.create", object_type="annotation")
    control = await _persist_audit_row(db_session, action="conversation.tags_update", object_type="conversation")

    await db_session.execute(_from_migration("_AUDIT_ACTION_TO_NOTE"))

    assert (await _audit_row(db_session, renamed.id))[0] == "note.create"
    assert (await _audit_row(db_session, control.id))[0] == "conversation.tags_update"


async def test_audit_action_rewrite_round_trips(db_session: AsyncSession) -> None:
    row = await _persist_audit_row(db_session, action="annotation.restore", object_type="annotation")

    await db_session.execute(_from_migration("_AUDIT_ACTION_TO_NOTE"))
    await db_session.execute(_from_migration("_AUDIT_ACTION_TO_ANNOTATION"))

    assert (await _audit_row(db_session, row.id))[0] == "annotation.restore"


async def test_audit_action_rewrite_is_anchored(db_session: AsyncSession) -> None:
    # Only a *leading* `annotation.` moves. `split_part` would have flattened a
    # three-segment action to two; the anchored regexp keeps the tail intact.
    row = await _persist_audit_row(db_session, action="annotation.label.create", object_type="annotation")

    await db_session.execute(_from_migration("_AUDIT_ACTION_TO_NOTE"))

    assert (await _audit_row(db_session, row.id))[0] == "note.label.create"


async def test_audit_action_rewrite_ignores_a_mid_string_prefix(db_session: AsyncSession) -> None:
    # Pins the `^`: unanchored, the pattern would rewrite an action that merely *contains*
    # the old prefix. Nothing emits this action today — it exists to fail if the anchor goes.
    row = await _persist_audit_row(db_session, action="export.annotation.create", object_type="export")

    await db_session.execute(_from_migration("_AUDIT_ACTION_TO_NOTE"))

    assert (await _audit_row(db_session, row.id))[0] == "export.annotation.create"


async def test_object_type_rewrite_moves_only_the_annotation_rows(db_session: AsyncSession) -> None:
    renamed = await _persist_audit_row(db_session, action="annotation.create", object_type="annotation")
    control = await _persist_audit_row(db_session, action="flag.create", object_type="message_flag")

    await db_session.execute(_from_migration("_OBJECT_TYPE_TO_NOTE"))

    assert (await _audit_row(db_session, renamed.id))[1] == "note"
    assert (await _audit_row(db_session, control.id))[1] == "message_flag"


async def test_object_type_rewrite_round_trips(db_session: AsyncSession) -> None:
    row = await _persist_audit_row(db_session, action="annotation.delete", object_type="annotation")

    await db_session.execute(_from_migration("_OBJECT_TYPE_TO_NOTE"))
    await db_session.execute(_from_migration("_OBJECT_TYPE_TO_ANNOTATION"))

    assert (await _audit_row(db_session, row.id))[1] == "annotation"
