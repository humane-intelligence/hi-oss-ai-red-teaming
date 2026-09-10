"""Tests for app.core.conversations.tasks.reap_orphaned_streaming_messages.

The reaper runs in a sync worker (`session_scope`), so the async `db_session`
fixture (SAVEPOINT rollback) is unusable — we mirror the email-task pattern: a sync
engine bound to the **test** DB, with the task's `session_scope` patched to it, and
the message graph built synchronously via plain model constructors.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
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
from app.core.conversations import tasks as tasks_module
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.conversations.tasks import reap_orphaned_streaming_messages
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.workers.celery_app import app as celery_app

# Reverse-FK order for per-test cleanup (sync commits are real, not rolled back).
_CLEANUP_MODELS = (
    Message,
    Turn,
    Conversation,
    ConversationGroup,
    EvaluationAiModel,
    AiModel,
    Evaluation,
    EvaluationGroup,
    User,
)


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


def _new_turn(session: Session) -> Turn:
    """Build the FK chain down to a conversation (incl. its required conversation group) plus one turn."""
    user = User(email=f"{uuid4().hex[:8]}@example.com")
    session.add(user)
    session.flush()
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
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    session.add(assignment)
    session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    session.add(scenario)
    session.flush()
    conversation_group = ConversationGroup(
        user_id=user.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    session.add(conversation_group)
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
    return turn


def _add_message(session: Session, turn: Turn, *, status: MessageStatus, age: timedelta) -> Message:
    ts = datetime.now(UTC) - age  # created_at == updated_at at insert, so a later onupdate bump is detectable
    message = Message(
        turn_id=turn.id,
        role=MessageRole.ASSISTANT,
        status=status,
        created_at=ts,
        updated_at=ts,
    )
    session.add(message)
    session.flush()
    return message


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_reaper_sweeps_only_stale_streaming_placeholders(sync_session: Session) -> None:
    ttl = get_settings().streaming_reap_ttl_seconds
    turn = _new_turn(sync_session)
    stale = _add_message(sync_session, turn, status=MessageStatus.STREAMING, age=timedelta(seconds=ttl + 3600))
    fresh = _add_message(sync_session, turn, status=MessageStatus.STREAMING, age=timedelta(seconds=0))
    done = _add_message(sync_session, turn, status=MessageStatus.COMPLETE, age=timedelta(seconds=ttl + 3600))
    sync_session.commit()

    result = reap_orphaned_streaming_messages.delay().get(timeout=5)

    assert result == {"reaped": 1}
    for message in (stale, fresh, done):
        sync_session.refresh(message)
    assert stale.status is MessageStatus.INTERRUPTED  # past TTL → swept
    assert stale.extra == {"interrupted_by": "reaper"}  # provenance marker distinguishes a crash-orphan
    assert fresh.status is MessageStatus.STREAMING  # within TTL → an in-flight stream is spared
    assert done.status is MessageStatus.COMPLETE  # terminal → untouched
    assert stale.updated_at > stale.created_at  # the sweep bumped updated_at via the column's onupdate
    assert done.updated_at == done.created_at  # an untouched row keeps its original timestamps


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_reaper_is_idempotent(sync_session: Session) -> None:
    ttl = get_settings().streaming_reap_ttl_seconds
    turn = _new_turn(sync_session)
    _add_message(sync_session, turn, status=MessageStatus.STREAMING, age=timedelta(seconds=ttl + 3600))
    sync_session.commit()

    first = reap_orphaned_streaming_messages.delay().get(timeout=5)
    second = reap_orphaned_streaming_messages.delay().get(timeout=5)

    assert first == {"reaped": 1}
    assert second == {"reaped": 0}  # re-run is a no-op (guarded WHERE status='streaming')


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_reaper_sweeps_orphans_across_conversations(sync_session: Session) -> None:
    # The sweep is a global system task, not scoped to one conversation — stale orphans
    # in separate conversations are all swept in one pass.
    ttl = get_settings().streaming_reap_ttl_seconds
    stale = [
        _add_message(
            sync_session, _new_turn(sync_session), status=MessageStatus.STREAMING, age=timedelta(seconds=ttl + 3600)
        )
        for _ in range(2)
    ]
    sync_session.commit()

    result = reap_orphaned_streaming_messages.delay().get(timeout=5)

    assert result == {"reaped": 2}
    for message in stale:
        sync_session.refresh(message)
        assert message.status is MessageStatus.INTERRUPTED


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery")
def test_reaper_honours_configured_ttl(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # The cutoff is read from settings, not hardcoded: a message younger than the default
    # TTL (900s) but older than a tiny override is swept.
    monkeypatch.setattr(tasks_module, "get_settings", lambda: SimpleNamespace(streaming_reap_ttl_seconds=1))
    recent = _add_message(
        sync_session, _new_turn(sync_session), status=MessageStatus.STREAMING, age=timedelta(seconds=30)
    )
    sync_session.commit()

    reap_orphaned_streaming_messages.delay().get(timeout=5)

    sync_session.refresh(recent)
    assert recent.status is MessageStatus.INTERRUPTED  # 30s > 1s override (would survive the 900s default)


@pytest.mark.unit
def test_reaper_is_discovered_by_celery_app() -> None:
    assert "app.core.conversations.tasks.reap_orphaned_streaming_messages" in celery_app.tasks


@pytest.mark.unit
def test_reaper_is_scheduled() -> None:
    entry = celery_app.conf.beat_schedule["reap-orphaned-streaming-messages"]
    assert entry["task"] == "app.core.conversations.tasks.reap_orphaned_streaming_messages"
    assert entry["schedule"] == float(get_settings().streaming_reap_interval_seconds)
