"""Tests for app.core.exports.tasks (async export generation + expiry reaper).

The tasks run their bodies inside `asyncio.run` over `async_session_scope`, so —
mirroring the streaming-reaper test — we patch that scope to a real async session
on the **test** DB (real commits, not the SAVEPOINT `db_session`) and seed/read the
graph synchronously with a plain sync session, cleaning up by reverse-FK order.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.annotations.models import FlaggedMessage
from app.core.annotations.models import MessageFlag
from app.core.audit.models import AuditLog
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserRole
from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.email.models import OutboundEmail
from app.core.email.models import OutboundEmailStatus
from app.core.email.tasks import send_email_task
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.exports import tasks as exports_tasks
from app.core.exports.enums import ExportJobStatus
from app.core.exports.models import ExportJob
from app.core.exports.tasks import reap_expired_export_jobs
from app.core.exports.tasks import run_export_job
from app.core.notifications.models import Notification
from app.workers.celery_app import app as celery_app

# Reverse-FK order for per-test cleanup (async task commits are real, not rolled back).
_CLEANUP_MODELS = (
    Notification,
    OutboundEmail,
    AuditLog,
    ExportJob,
    FlaggedMessage,
    MessageFlag,
    Message,
    Turn,
    Conversation,
    ConversationGroup,
    EvaluationAiModel,
    AiModel,
    Evaluation,
    EvaluationGroup,
    UserRole,
    Role,
    User,
)


class _CaptureStorage:
    """In-memory export storage: records saved CSV bodies and deleted refs."""

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}
        self.deleted: list[str] = []

    async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
        self.saved[key] = "".join([chunk async for chunk in chunks])
        return key

    def exists(self, file_ref: str) -> bool:
        return file_ref in self.saved

    def open(self, file_ref: str) -> Iterator[bytes]:
        yield self.saved[file_ref].encode("utf-8")

    async def delete(self, file_ref: str) -> None:
        self.deleted.append(file_ref)
        self.saved.pop(file_ref, None)


@pytest.fixture
def sync_session(_test_db: str, db_engine: AsyncEngine) -> Iterator[Session]:
    """Sync session on the test DB for seeding/reading; cleans up all export-graph rows on teardown.

    Depends on `db_engine` so the schema (Alembic migrations) is applied before we touch it.
    """
    sync_url = make_url(get_settings().database_url_sync).set(database=_test_db).render_as_string(hide_password=False)
    engine = create_engine(sync_url, poolclass=NullPool)
    factory = sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        for model in _CLEANUP_MODELS:
            session.query(model).delete()
        session.commit()
        session.close()
        engine.dispose()


@pytest.fixture
def patch_async_scope(monkeypatch: pytest.MonkeyPatch, _test_db: str, db_engine: AsyncEngine) -> None:
    """Patch the task's `async_session_scope` to a real async session on the test DB."""
    async_url = make_url(get_settings().database_url).set(database=_test_db).render_as_string(hide_password=False)

    @asynccontextmanager
    async def fake_scope() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(async_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                yield session
        finally:
            await engine.dispose()

    monkeypatch.setattr(exports_tasks, "async_session_scope", fake_scope)


@pytest.fixture
def stub_email_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `send_email_best_effort` from actually enqueuing — the `OutboundEmail` row still lands."""
    monkeypatch.setattr(send_email_task, "apply_async", MagicMock())


def _seed_flagged_evaluation(session: Session, owner: User) -> Evaluation:
    """Public, owner-visible group → evaluation → conversation (one message) → one flag."""
    group = EvaluationGroup(
        title="Eng",
        description="d",
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    session.add(group)
    session.flush()
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
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
        user_id=owner.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    session.add(conversation_group)
    session.flush()
    conversation = Conversation(
        user_id=owner.id,
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
    message = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="probe")
    session.add(message)
    session.flush()
    flag = MessageFlag(
        reason="boom",
        created_by_id=owner.id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=group.id,
    )
    session.add(flag)
    session.flush()
    session.add(FlaggedMessage(message_flag_id=flag.id, message_id=message.id))
    session.flush()
    return evaluation


def _seed_user(session: Session, *, email: str, permissions: list[str]) -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    role = Role(name=f"role-{email}", permissions=permissions, is_system=False)
    session.add(role)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_run_export_job_generates_ready_file(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exports need owner/admin authority; grant the requester the manage break-glass (the sync
    # seeder can't assign an in-group object role), so the task's re-check passes.
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id)
    sync_session.add(job)
    sync_session.commit()
    storage = _CaptureStorage()
    monkeypatch.setattr(exports_tasks, "get_export_storage", lambda: storage)

    result = run_export_job.delay(str(job.id)).get(timeout=15)

    assert result == {"status": "ready"}
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.READY
    assert job.file_ref == f"{job.id}.csv"
    assert job.expires_at is not None
    body = storage.saved[f"{job.id}.csv"]
    assert body.startswith("﻿")  # UTF-8 BOM up front so Excel renders non-ASCII
    assert body.splitlines()[0].lstrip("﻿").startswith("Flag ID")  # header
    assert "boom" in body  # the one flag's reason


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_run_export_job_generates_json_file(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # A format=json job stores a `.json` file whose body is a valid JSON array of header-keyed objects.
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(template="flags", format="json", requested_by_id=owner.id, evaluation_id=evaluation.id)
    sync_session.add(job)
    sync_session.commit()
    storage = _CaptureStorage()
    monkeypatch.setattr(exports_tasks, "get_export_storage", lambda: storage)

    result = run_export_job.delay(str(job.id)).get(timeout=15)

    assert result == {"status": "ready"}
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.READY
    assert job.file_ref == f"{job.id}.json"  # extension follows the format
    parsed = json.loads(storage.saved[f"{job.id}.json"])  # valid JSON, no BOM
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["Reason"] == "boom"  # header-keyed object, raw value (not CSV-formatted)


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_run_export_job_fails_when_target_not_visible(sync_session: Session) -> None:
    # A real evaluation in another user's invitation-only group: the FK is satisfied, but the
    # requester can't see it, so the worker's re-resolution 404s and records the job failed —
    # a detached job never widens past what the requester could read.
    stranger = _seed_user(sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[])
    private_group = EvaluationGroup(
        title="Private",
        description="d",
        created_by_id=stranger.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    sync_session.add(private_group)
    sync_session.flush()
    hidden = Evaluation(title="E", description="d", evaluation_group_id=private_group.id, created_by_id=stranger.id)
    sync_session.add(hidden)
    sync_session.flush()

    requester = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.FLAGS_READ.value]
    )
    job = ExportJob(template="flags", requested_by_id=requester.id, evaluation_id=hidden.id)
    sync_session.add(job)
    sync_session.commit()

    result = run_export_job.delay(str(job.id)).get(timeout=15)

    assert result == {"status": "failed"}
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.FAILED
    assert job.error == "The export target is no longer available."
    assert job.expires_at is not None  # failed jobs get a TTL too, so the reaper eventually sweeps them


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_run_export_job_fails_when_authority_revoked_after_queue(sync_session: Session) -> None:
    # The headline security property: a requester who held authority when the job was queued but
    # whose role is revoked before the worker runs is denied — the worker re-resolves visibility +
    # owner authority under the requester's *live* identity, so a detached job can't ride stale power.
    requester = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    stranger = _seed_user(sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[])
    private_group = EvaluationGroup(
        title="Private",
        description="d",
        created_by_id=stranger.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    sync_session.add(private_group)
    sync_session.flush()
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=private_group.id, created_by_id=stranger.id)
    sync_session.add(evaluation)
    sync_session.flush()
    job = ExportJob(template="flags", requested_by_id=requester.id, evaluation_id=evaluation.id)
    sync_session.add(job)
    sync_session.commit()
    # Revoke the requester's roles after queuing — the worker rebuilds their permissions live.
    sync_session.query(UserRole).filter_by(user_id=requester.id).delete()
    sync_session.commit()

    result = run_export_job.delay(str(job.id)).get(timeout=15)

    assert result == {"status": "failed"}
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.FAILED


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_reaper_sweeps_expired_terminal_jobs(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _seed_user(sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.FLAGS_READ.value])
    now = datetime.now(UTC)
    expired = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.READY,
        file_ref="old.csv",
        expires_at=now - timedelta(hours=1),
    )
    failed_expired = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.FAILED,
        error="boom",
        expires_at=now - timedelta(hours=1),  # no file_ref — a failed job wrote nothing
    )
    fresh = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.READY,
        file_ref="new.csv",
        expires_at=now + timedelta(hours=1),
    )
    pending = ExportJob(template="flags", requested_by_id=owner.id, status=ExportJobStatus.PENDING)
    sync_session.add_all([expired, failed_expired, fresh, pending])
    sync_session.commit()

    storage = _CaptureStorage()
    storage.saved["old.csv"] = "x"
    monkeypatch.setattr(exports_tasks, "get_export_storage", lambda: storage)

    result = reap_expired_export_jobs.delay().get(timeout=15)

    assert result == {"reaped": 2, "failed_stuck": 0}  # expired READY + expired FAILED; nothing stuck
    assert storage.deleted == ["old.csv"]  # only the READY job had a file; FAILED wrote nothing
    for job in (expired, failed_expired, fresh, pending):
        sync_session.refresh(job)
    assert expired.deleted_at is not None  # ready + expired → swept
    assert failed_expired.deleted_at is not None  # failed + expired → swept (no longer accumulates)
    assert fresh.deleted_at is None  # not yet expired → spared
    assert pending.deleted_at is None  # no expires_at (not terminal) → spared


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_run_export_job_skips_a_non_pending_job(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Compare-and-swap: a job already `running` (e.g. a concurrent redelivery after the broker's
    # visibility_timeout expired) is a no-op — it must not re-generate/overwrite the file.
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(
        template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id, status=ExportJobStatus.RUNNING
    )
    sync_session.add(job)
    sync_session.commit()
    storage = _CaptureStorage()
    monkeypatch.setattr(exports_tasks, "get_export_storage", lambda: storage)

    result = run_export_job.delay(str(job.id)).get(timeout=15)

    assert result == {"status": "running"}  # CAS matched no pending row → bailed
    assert storage.saved == {}  # never wrote a file
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.RUNNING  # untouched


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_reaper_fails_stuck_jobs(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # A pending/running job older than the stuck TTL (worker crash, or the broker dropped the
    # message) carries no expires_at, so the TTL sweep never touches it — the stuck sweep fails it.
    owner = _seed_user(sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.FLAGS_READ.value])
    now = datetime.now(UTC)
    ttl = get_settings().export_stuck_ttl_seconds
    stuck_running = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.RUNNING,
        created_at=now - timedelta(seconds=ttl + 60),
    )
    stuck_pending = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.PENDING,
        created_at=now - timedelta(seconds=ttl + 60),
    )
    fresh_running = ExportJob(template="flags", requested_by_id=owner.id, status=ExportJobStatus.RUNNING)
    # An OLD but already-finished job: the stuck sweep must NOT clobber it (its atomic UPDATE only
    # matches rows still pending/running), guarding the reaper-vs-finish race.
    finished_old = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.READY,
        file_ref="done.csv",
        created_at=now - timedelta(seconds=ttl + 60),
        expires_at=now + timedelta(hours=1),  # not expired → the expiry sweep also spares it
    )
    sync_session.add_all([stuck_running, stuck_pending, fresh_running, finished_old])
    sync_session.commit()
    storage = _CaptureStorage()
    monkeypatch.setattr(exports_tasks, "get_export_storage", lambda: storage)

    result = reap_expired_export_jobs.delay().get(timeout=15)

    assert result == {"reaped": 0, "failed_stuck": 2}  # only the two pending/running rows
    for job in (stuck_running, stuck_pending, fresh_running, finished_old):
        sync_session.refresh(job)
    assert stuck_running.status is ExportJobStatus.FAILED
    assert stuck_running.expires_at is not None  # stamped so the expiry sweep later cleans it up
    assert stuck_pending.status is ExportJobStatus.FAILED
    assert fresh_running.status is ExportJobStatus.RUNNING  # young → spared
    assert finished_old.status is ExportJobStatus.READY  # old but finished → NOT clobbered


class _BoomStorage:
    """Storage whose save raises a transient error, to drive the retry branch."""

    async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
        raise OperationalError("SELECT 1", {}, Exception("db connection reset"))

    def exists(self, file_ref: str) -> bool:
        return False

    def open(self, file_ref: str) -> Iterator[bytes]:
        yield b""

    async def delete(self, file_ref: str) -> None:
        return None


def _seed_manager_job(sync_session: Session) -> ExportJob:
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id)
    sync_session.add(job)
    sync_session.commit()
    return job


@pytest.mark.integration
@pytest.mark.usefixtures("patch_async_scope")
def test_run_export_job_reraises_transient_and_resets_to_pending(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient failure with retry budget left re-raises (so Celery's autoretry fires) and
    # resets the row to `pending` so the retry re-claims it (the CAS only advances `pending`).
    job = _seed_manager_job(sync_session)
    monkeypatch.setattr(exports_tasks, "get_export_storage", _BoomStorage)

    with pytest.raises(OperationalError):
        asyncio.run(exports_tasks._run_export_job(job.id, retries=0, max_retries=3))

    sync_session.refresh(job)
    assert job.status is ExportJobStatus.PENDING
    # The transient-retry path must announce NOTHING — only terminal transitions emit (no "failed"
    # notice spammed on every retry before the job actually terminates).
    assert sync_session.query(Notification).filter_by(user_id=job.requested_by_id).count() == 0
    assert sync_session.query(AuditLog).filter_by(object_id=job.id).count() == 0


@pytest.mark.integration
@pytest.mark.usefixtures("patch_async_scope")
def test_run_export_job_records_failed_when_retry_budget_exhausted(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same transient failure, but the budget is spent: no re-raise — the row is recorded `failed`
    # (the requester sees it and re-requests), rather than looping forever.
    job = _seed_manager_job(sync_session)
    monkeypatch.setattr(exports_tasks, "get_export_storage", _BoomStorage)

    result = asyncio.run(exports_tasks._run_export_job(job.id, retries=3, max_retries=3))

    assert result == {"status": "failed"}
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.FAILED


@pytest.mark.integration
@pytest.mark.usefixtures("patch_async_scope")
def test_run_export_job_retry_reset_does_not_resurrect_a_reaper_failed_job(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reaper race: the stuck-job sweep flips the row running -> failed *while* we generate. The
    # transient-retry reset is CAS-guarded on `running`, so it must NOT write the row back to
    # `pending` — resurrecting a terminated job (and inheriting the reaper's stale expires_at).
    job = _seed_manager_job(sync_session)

    def reaper_flip() -> None:
        job.status = ExportJobStatus.FAILED
        job.expires_at = datetime.now(UTC) + timedelta(seconds=3600)
        sync_session.commit()

    class _ReaperRaceStorage:
        async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
            reaper_flip()  # simulate the concurrent reaper terminating the row mid-generation
            raise OperationalError("SELECT 1", {}, Exception("db connection reset"))

        def exists(self, file_ref: str) -> bool:
            return False

        def open(self, file_ref: str) -> Iterator[bytes]:
            yield b""

        async def delete(self, file_ref: str) -> None:
            return None

    monkeypatch.setattr(exports_tasks, "get_export_storage", _ReaperRaceStorage)

    # Budget left: without the CAS guard this would blindly reset to pending and re-raise.
    result = asyncio.run(exports_tasks._run_export_job(job.id, retries=0, max_retries=3))

    assert result == {"status": "failed"}  # superseded — reported, not resurrected
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.FAILED


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope", "stub_email_dispatch")
def test_run_export_job_success_emits_completion(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # On READY the requester gets an audit row (system actor), an in-app notification pointing at the
    # evaluation, and a queued export_ready email — all in the same transaction as the status flip.
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id)
    sync_session.add(job)
    sync_session.commit()
    monkeypatch.setattr(exports_tasks, "get_export_storage", _CaptureStorage)

    assert run_export_job.delay(str(job.id)).get(timeout=15) == {"status": "ready"}

    audit = sync_session.query(AuditLog).filter_by(action="export.ready", object_id=job.id).one()
    assert audit.actor_id is None  # system actor — no interactive caller
    notif = sync_session.query(Notification).filter_by(user_id=owner.id, name="Export ready").one()
    assert notif.object_type == "evaluation"
    assert notif.object_id == evaluation.id
    email = sync_session.query(OutboundEmail).filter_by(template_name="export_ready", recipient=owner.email).one()
    assert email.status is OutboundEmailStatus.QUEUED


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope", "stub_email_dispatch")
def test_run_export_job_group_scope_emits_group_notification(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A group-scoped export points the notification at the evaluation *group* (its own object type
    # + id + FE route), exercising the evaluation_group_id branch of _scope / target_url / title.
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_group_id=evaluation.evaluation_group_id)
    sync_session.add(job)
    sync_session.commit()
    monkeypatch.setattr(exports_tasks, "get_export_storage", _CaptureStorage)

    assert run_export_job.delay(str(job.id)).get(timeout=15) == {"status": "ready"}

    notif = sync_session.query(Notification).filter_by(user_id=owner.id, name="Export ready").one()
    assert notif.object_type == "evaluation_group"
    assert notif.object_id == evaluation.evaluation_group_id


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope", "stub_email_dispatch")
def test_success_notify_failure_does_not_roll_back_the_export(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A notify failure on the success path must NOT undo the generated export. `_announce` wraps the
    # emit in a SAVEPOINT + swallows, so the READY finalize still commits. (Mutation check: calling
    # emit_export_completion un-wrapped lets the error hit `_run_export_job`'s except → rollback of
    # the READY flip + file delete → the job would not be READY and this fails.)
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id)
    sync_session.add(job)
    sync_session.commit()
    monkeypatch.setattr(exports_tasks, "get_export_storage", _CaptureStorage)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("notify boom")

    monkeypatch.setattr(exports_tasks, "emit_export_completion", _boom)

    assert run_export_job.delay(str(job.id)).get(timeout=15) == {"status": "ready"}
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.READY  # export survived the notify failure
    assert sync_session.query(Notification).filter_by(user_id=owner.id).count() == 0  # announcement lost (logged)


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope", "stub_email_dispatch")
def test_run_export_job_failure_emits_completion(sync_session: Session) -> None:
    # A terminal failure (target not visible to the requester) emits the failure trio.
    stranger = _seed_user(sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[])
    private_group = EvaluationGroup(
        title="Private",
        description="d",
        created_by_id=stranger.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    sync_session.add(private_group)
    sync_session.flush()
    hidden = Evaluation(title="E", description="d", evaluation_group_id=private_group.id, created_by_id=stranger.id)
    sync_session.add(hidden)
    sync_session.flush()
    requester = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.FLAGS_READ.value]
    )
    job = ExportJob(template="flags", requested_by_id=requester.id, evaluation_id=hidden.id)
    sync_session.add(job)
    sync_session.commit()

    assert run_export_job.delay(str(job.id)).get(timeout=15) == {"status": "failed"}

    sync_session.query(AuditLog).filter_by(action="export.failed", object_id=job.id).one()
    notif = sync_session.query(Notification).filter_by(user_id=requester.id, name="Export failed").one()
    assert notif.object_type == "evaluation"  # failure notice points at the scope too, like success
    assert notif.object_id == hidden.id
    sync_session.query(OutboundEmail).filter_by(template_name="export_failed", recipient=requester.email).one()


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope", "stub_email_dispatch")
def test_reaper_timeout_emits_completion(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stuck job the reaper times out also notifies its requester (via UPDATE ... RETURNING).
    owner = _seed_user(sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.FLAGS_READ.value])
    ttl = get_settings().export_stuck_ttl_seconds
    stuck = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(seconds=ttl + 60),
    )
    sync_session.add(stuck)
    sync_session.commit()
    monkeypatch.setattr(exports_tasks, "get_export_storage", _CaptureStorage)

    assert reap_expired_export_jobs.delay().get(timeout=15) == {"reaped": 0, "failed_stuck": 1}

    sync_session.query(AuditLog).filter_by(action="export.failed", object_id=stuck.id).one()
    sync_session.query(Notification).filter_by(user_id=owner.id, name="Export failed").one()
    sync_session.query(OutboundEmail).filter_by(template_name="export_failed", recipient=owner.email).one()


@pytest.mark.integration
@pytest.mark.usefixtures("patch_async_scope", "stub_email_dispatch")
def test_terminal_failure_after_reaper_does_not_double_emit(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reaper races the worker: the stuck sweep flips running -> failed (and already announced it),
    # then the worker's generation raises a *terminal* error. The terminal write is CAS-guarded on
    # `running`, so it no-ops and emits NOTHING — no second failure notification. (Mutation check:
    # dropping the CAS guard makes the worker re-emit, and these asserts fail.)
    job = _seed_manager_job(sync_session)

    def reaper_flip() -> None:
        job.status = ExportJobStatus.FAILED  # simulate the reaper terminating the row mid-generation
        job.expires_at = datetime.now(UTC) + timedelta(seconds=3600)
        sync_session.commit()

    class _ReaperThenTerminalStorage:
        async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
            reaper_flip()
            raise ValueError("permanent boom")  # non-transient → terminal failure path

        def exists(self, file_ref: str) -> bool:
            return False

        def open(self, file_ref: str) -> Iterator[bytes]:
            yield b""

        async def delete(self, file_ref: str) -> None:
            return None

    monkeypatch.setattr(exports_tasks, "get_export_storage", _ReaperThenTerminalStorage)

    result = asyncio.run(exports_tasks._run_export_job(job.id, retries=0, max_retries=3))

    assert result == {"status": "failed"}
    # The worker did NOT announce (the reaper owns this transition) — nothing was emitted here.
    assert sync_session.query(Notification).filter_by(user_id=job.requested_by_id).count() == 0
    assert sync_session.query(AuditLog).filter_by(action="export.failed", object_id=job.id).count() == 0
    assert sync_session.query(OutboundEmail).filter_by(template_name="export_failed").count() == 0


@pytest.mark.integration
@pytest.mark.usefixtures("patch_async_scope", "stub_email_dispatch")
def test_success_supersede_after_reaper_emits_nothing(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Reaper races the worker on the *success* path: generation succeeds, but the stuck sweep flipped
    # the row running -> failed before the finalize CAS. The CAS then no-ops (rowcount == 0), so the
    # worker drops the file it just wrote and announces NOTHING — no spurious "ready" notice for a row
    # the reaper already terminated (and announced). (Mutation check: hoisting `_announce` out of the
    # `finalized.rowcount != 0` guard makes the superseded row emit a ready trio, and these asserts fail.)
    job = _seed_manager_job(sync_session)

    def reaper_flip() -> None:
        job.status = ExportJobStatus.FAILED  # simulate the reaper terminating the row mid-generation
        job.expires_at = datetime.now(UTC) + timedelta(seconds=3600)
        sync_session.commit()

    class _ReaperThenSuccessStorage(_CaptureStorage):
        async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
            reaper_flip()  # reaper wins before the finalize CAS
            return await super().save(key, chunks, content_type)  # generation itself succeeds

    storage = _ReaperThenSuccessStorage()
    monkeypatch.setattr(exports_tasks, "get_export_storage", lambda: storage)

    result = asyncio.run(exports_tasks._run_export_job(job.id, retries=0, max_retries=3))

    assert result == {"status": "failed"}  # superseded — reports the reaper's terminal state
    sync_session.refresh(job)
    assert job.status is ExportJobStatus.FAILED
    assert storage.deleted == [f"{job.id}.csv"]  # the file we wrote is dropped (nothing dangles)
    assert sync_session.query(Notification).filter_by(user_id=job.requested_by_id).count() == 0
    assert sync_session.query(AuditLog).filter_by(action="export.ready", object_id=job.id).count() == 0
    assert sync_session.query(OutboundEmail).filter_by(template_name="export_ready").count() == 0


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope", "stub_email_dispatch")
def test_completion_skips_notification_for_deleted_requester(
    sync_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A requester soft-deleted before the job finishes: the audit row is still written (system
    # actor), but the notification + email are skipped (no live recipient) — no crash.
    owner = _seed_user(
        sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
    )
    evaluation = _seed_flagged_evaluation(sync_session, owner)
    job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id)
    sync_session.add(job)
    sync_session.commit()
    owner.deleted_at = datetime.now(UTC)  # requester gone before the worker runs
    sync_session.commit()
    monkeypatch.setattr(exports_tasks, "get_export_storage", _CaptureStorage)

    run_export_job.delay(str(job.id)).get(timeout=15)

    assert sync_session.query(AuditLog).filter_by(object_type="export_job", object_id=job.id).count() == 1
    assert sync_session.query(Notification).filter_by(user_id=owner.id).count() == 0
    assert sync_session.query(OutboundEmail).filter_by(recipient=owner.email).count() == 0


@pytest.mark.integration
@pytest.mark.usefixtures("eager_celery", "patch_async_scope")
def test_reaper_survives_a_notification_failure(sync_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # A raising `emit_export_completion` must NOT abort the sweep: the per-row SAVEPOINT rolls that
    # row back and the except logs it, so the stuck-job UPDATE still commits. (Mutation check:
    # dropping the try/except lets the exception propagate → the whole transaction rolls back →
    # the job stays RUNNING and this fails.)
    owner = _seed_user(sync_session, email=f"{uuid4().hex[:8]}@example.com", permissions=[Permission.FLAGS_READ.value])
    ttl = get_settings().export_stuck_ttl_seconds
    stuck = ExportJob(
        template="flags",
        requested_by_id=owner.id,
        status=ExportJobStatus.RUNNING,
        created_at=datetime.now(UTC) - timedelta(seconds=ttl + 60),
    )
    sync_session.add(stuck)
    sync_session.commit()

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("notify boom")

    monkeypatch.setattr(exports_tasks, "emit_export_completion", _boom)

    assert reap_expired_export_jobs.delay().get(timeout=15) == {"reaped": 0, "failed_stuck": 1}
    sync_session.refresh(stuck)
    assert stuck.status is ExportJobStatus.FAILED  # sweep committed despite the notify failure


@pytest.mark.unit
def test_is_transient_discriminates_client_error_by_status_and_code() -> None:
    # boto3 raises one ClientError for everything; only 5xx / throttle codes are worth a retry.
    def client_error(code: str, http_status: int) -> ClientError:
        return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": http_status}}, "PutObject")

    assert exports_tasks._is_transient(OperationalError("x", {}, Exception())) is True
    assert exports_tasks._is_transient(client_error("InternalError", 500)) is True  # 5xx server fault
    assert exports_tasks._is_transient(client_error("Throttling", 400)) is True  # throttle code, 4xx status
    assert exports_tasks._is_transient(client_error("AccessDenied", 403)) is False  # permanent misconfig
    assert exports_tasks._is_transient(client_error("NoSuchBucket", 404)) is False  # permanent misconfig
    assert exports_tasks._is_transient(ValueError("nope")) is False


@pytest.mark.unit
def test_run_export_job_retry_policy_is_declared() -> None:
    task = celery_app.tasks["app.core.exports.tasks.run_export_job"]
    assert OperationalError in task.autoretry_for
    assert ClientError in task.autoretry_for
    assert task.max_retries == 3
    assert task.retry_backoff is True


@pytest.mark.unit
def test_tasks_are_discovered_by_celery_app() -> None:
    assert "app.core.exports.tasks.run_export_job" in celery_app.tasks
    assert "app.core.exports.tasks.reap_expired_export_jobs" in celery_app.tasks


@pytest.mark.unit
def test_reaper_is_scheduled() -> None:
    entry = celery_app.conf.beat_schedule["reap-expired-export-jobs"]
    assert entry["task"] == "app.core.exports.tasks.reap_expired_export_jobs"
    assert entry["schedule"] == float(get_settings().export_reap_interval_seconds)
