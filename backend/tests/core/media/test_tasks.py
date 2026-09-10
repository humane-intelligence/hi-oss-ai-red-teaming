"""Tests for app.core.media.tasks.reap_orphaned_media.

The reaper runs in a sync worker (`session_scope`), so the async `db_session`
fixture (SAVEPOINT rollback) is unusable — mirrors the streaming-reaper test
pattern: a sync engine bound to the **test** DB, the task's `session_scope`
patched to it, and the graph built synchronously via plain model constructors.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import User
from app.core.config import get_settings
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import MessageImage
from app.core.conversations.models import Turn
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.media import tasks as tasks_module
from app.core.media.models import MediaAsset
from app.core.media.storage.local import LocalMediaStorage
from app.core.media.tasks import reap_orphaned_media
from app.workers.celery_app import app as celery_app


@pytest.mark.unit
def test_reaper_is_discovered_by_celery_app() -> None:
    assert "app.core.media.tasks.reap_orphaned_media" in celery_app.tasks


@pytest.mark.unit
def test_reaper_is_scheduled() -> None:
    entry = celery_app.conf.beat_schedule["reap-orphaned-media"]
    assert entry["task"] == "app.core.media.tasks.reap_orphaned_media"
    assert entry["schedule"] == float(get_settings().media_orphan_reap_interval_seconds)


# Reverse-FK order for per-test cleanup (sync commits are real, not rolled back).
_CLEANUP_MODELS = (
    MessageImage,
    Message,
    Turn,
    Conversation,
    ConversationGroup,
    EvaluationAiModel,
    Evaluation,
    EvaluationGroup,
    AiModel,
    MediaAsset,
    User,
)

_OLD = datetime.now(UTC) - timedelta(days=8)  # past the 168h default grace


@pytest.fixture
def sync_session(monkeypatch: pytest.MonkeyPatch, _test_db: str, db_engine: AsyncEngine) -> Iterator[Session]:
    """Patch the reaper's `session_scope` to a sync session on the test DB; yield a session to seed/read with."""
    sync_url = make_url(get_settings().database_url_sync).set(database=_test_db).render_as_string(hide_password=False)
    engine = create_engine(sync_url, poolclass=NullPool)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(tasks_module, "session_scope", fake_scope)

    test_session = session_factory()
    try:
        yield test_session
    finally:
        for model in _CLEANUP_MODELS:
            test_session.query(model).delete()
        test_session.commit()
        test_session.close()
        engine.dispose()


@pytest.fixture
def media_storage(tmp_path, monkeypatch: pytest.MonkeyPatch) -> LocalMediaStorage:
    storage = LocalMediaStorage(str(tmp_path))
    monkeypatch.setattr(tasks_module, "get_media_storage", lambda: storage)
    return storage


def _asset(session: Session, user: User, *, created_at: datetime) -> MediaAsset:
    asset = MediaAsset(
        key=f"2026/01/01/{uuid4().hex}.png",
        content_type="image/png",
        size_bytes=1,
        width=1,
        height=1,
        created_by_id=user.id,
        created_at=created_at,
    )
    session.add(asset)
    session.flush()
    return asset


def _user(session: Session) -> User:
    user = User(email=f"{uuid4().hex[:8]}@example.com")
    session.add(user)
    session.flush()
    return user


def _message_referencing(session: Session, user: User, image_key: str) -> Evaluation:
    """Build the FK chain down to one user message, attach `image_key`, return the evaluation."""
    group = EvaluationGroup(
        title="Eng",
        description="d",
        created_by_id=user.id,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status=PublicationStatus.DRAFT,
        start_date=date(2026, 3, 1),
    )
    session.add(group)
    session.flush()
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=user.id)
    session.add(evaluation)
    session.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    session.add(model)
    session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    session.add(scenario)
    session.flush()
    conversation_group = ConversationGroup(
        user_id=user.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    session.add(conversation_group)
    session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    session.add(assignment)
    session.flush()
    conversation = Conversation(
        user_id=user.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    session.add(conversation)
    session.flush()
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    session.add(turn)
    session.flush()
    message = Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content="hi")
    session.add(message)
    session.flush()
    session.add(MessageImage(message_id=message.id, position=0, image_key=image_key))
    session.flush()
    return evaluation


def _save_blob(storage: LocalMediaStorage, key: str) -> None:
    # Sync test (the task itself calls asyncio.run, which forbids a running loop).
    asyncio.run(storage.save(key, b"opaque", content_type="image/png"))


def _deleted_at(session: Session, asset_id: object) -> datetime | None:
    row = session.get(MediaAsset, asset_id)
    assert row is not None
    return row.deleted_at


@pytest.mark.integration
def test_reaper_sweeps_only_old_unreferenced_assets(sync_session: Session, media_storage: LocalMediaStorage) -> None:
    user = _user(sync_session)
    orphan = _asset(sync_session, user, created_at=_OLD)
    fresh = _asset(sync_session, user, created_at=datetime.now(UTC))
    cover_ref = _asset(sync_session, user, created_at=_OLD)
    attach_ref = _asset(sync_session, user, created_at=_OLD)
    evaluation = _message_referencing(sync_session, user, attach_ref.key)
    # The cover reference: a soft-deleted evaluation still counts (flags/reviews history).
    evaluation.cover_image = cover_ref.key
    evaluation.soft_delete(None)
    sync_session.commit()
    for asset in (orphan, fresh, cover_ref, attach_ref):
        _save_blob(media_storage, asset.key)

    result = reap_orphaned_media()

    assert result == {"reaped": 1, "blob_failures": 0}
    sync_session.expire_all()
    assert _deleted_at(sync_session, orphan.id) is not None
    assert not media_storage.exists(orphan.key)
    for kept in (fresh, cover_ref, attach_ref):
        assert _deleted_at(sync_session, kept.id) is None
        assert media_storage.exists(kept.key)


@pytest.mark.integration
def test_reaper_tolerates_blob_delete_failure(
    sync_session: Session, media_storage: LocalMediaStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(sync_session)
    orphan = _asset(sync_session, user, created_at=_OLD)
    sync_session.commit()

    async def _boom(_key: str) -> None:
        raise OSError("storage backend down")

    monkeypatch.setattr(media_storage, "delete", _boom)

    result = reap_orphaned_media()

    # The soft-delete is authoritative — the row is swept even though the blob remains.
    assert result == {"reaped": 1, "blob_failures": 1}
    sync_session.expire_all()
    assert _deleted_at(sync_session, orphan.id) is not None


@pytest.mark.integration
def test_reaper_keeps_attachment_of_soft_deleted_message(
    sync_session: Session, media_storage: LocalMediaStorage
) -> None:
    # Every link-table row counts, liveness ignored: a tombstoned message's attachment is
    # NOT an orphan (flags/reviews reach soft-deleted transcripts). Guards against a future
    # narrowing of the reaper's inventory to live messages only.
    user = _user(sync_session)
    attach_ref = _asset(sync_session, user, created_at=_OLD)
    _message_referencing(sync_session, user, attach_ref.key)
    sync_session.query(Message).one().soft_delete(None)
    sync_session.commit()
    _save_blob(media_storage, attach_ref.key)

    result = reap_orphaned_media()

    assert result == {"reaped": 0, "blob_failures": 0}
    sync_session.expire_all()
    assert _deleted_at(sync_session, attach_ref.id) is None
    assert media_storage.exists(attach_ref.key)
