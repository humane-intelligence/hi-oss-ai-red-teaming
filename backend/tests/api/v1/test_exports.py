"""Integration tests for the evaluation CSV export endpoints."""

import csv
import io
import json
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.annotations.enums import FlagStatus
from app.core.annotations.models import FlaggedMessage
from app.core.annotations.models import MessageFlag
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.models import ObjectRoleAssignment
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.auth.services.users import create_user as create_user_service
from app.core.config import get_settings
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import TAG_CONTEXT_EXTRA_KEY
from app.core.conversations.models import TAG_CONTEXT_PARTIAL_EXTRA_KEY
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.csv_generator import to_csv
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.evaluations import resolve_effective_licenses
from app.core.exports.base import ExportScope
from app.core.exports.catalog import get_export
from app.core.exports.enums import ExportFormat
from app.core.exports.enums import ExportJobStatus
from app.core.exports.filters import ExportFilters
from app.core.exports.generation import resolve_group_evaluations
from app.core.exports.generation import stream_export
from app.core.exports.models import ExportJob
from app.core.json_generator import to_json
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.models import DataLicense
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.models import Review
from tests.api.v1.conftest import PROBLEM_CT
from tests.api.v1.conftest import bearer as _auth
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


class _FakeStorage:
    """In-memory export storage stub — no filesystem, records what was asked for."""

    def __init__(self, *, present: bool = True, body: bytes = b"Flag ID\nrow\n", raise_on_delete: bool = False) -> None:
        self.present = present
        self.body = body
        self.raise_on_delete = raise_on_delete
        self.deleted: list[str] = []

    def exists(self, file_ref: str) -> bool:
        return self.present

    def open(self, file_ref: str):
        # Mirror the real backends: acquire eagerly so a gone file raises at call time (→ 404),
        # not lazily mid-stream. A generator would defer the check, so return a plain iterator.
        if not self.present:
            raise FileNotFoundError(file_ref)
        return iter([self.body])

    async def save(self, key: str, chunks) -> str:
        return key

    async def delete(self, file_ref: str) -> None:
        if self.raise_on_delete:
            raise OSError("storage unavailable")
        self.deleted.append(file_ref)


def _su(user: User, permissions: list[str]) -> SessionUser:
    """Build the SessionUser a template fetcher scopes by (id + permissions)."""
    return SessionUser(
        id=user.id,
        email=user.email,
        email_verified=True,
        first_name=None,
        last_name=None,
        provider="local",
        permissions=frozenset(permissions),
    )


async def _render_eval(
    db: AsyncSession,
    caller: User,
    perms: list[str],
    evaluation: Evaluation,
    template: str,
    *,
    group_wide: bool = True,
    filters: ExportFilters | None = None,
) -> str:
    """Render one evaluation's export at the service layer (inline endpoints were removed).

    `group_wide` mirrors the production path: `stream_export` sets `full_group_access=True`
    because reaching a fetch means the caller was authorised as the group's owner/admin. Pass
    `group_wide=False` to exercise the fail-safe self-only default. `filters` narrow the rows.
    """
    export = get_export(template)
    assert export is not None
    scope = ExportScope(
        evaluation_id=evaluation.id,
        caller=_su(caller, perms),
        evaluation=evaluation,
        full_group_access=group_wide,
        filters=filters or ExportFilters(),
    )
    rows = [row async for row in export.fetch(db, scope)]  # fetch is now a streaming async generator
    return to_csv(rows, export.columns)


async def _render_eval_json(
    db: AsyncSession, caller: User, perms: list[str], evaluation: Evaluation, template: str
) -> list[dict]:
    """Render one evaluation's export as parsed JSON at the service layer (mirrors `_render_eval`)."""
    export = get_export(template)
    assert export is not None
    scope = ExportScope(
        evaluation_id=evaluation.id, caller=_su(caller, perms), evaluation=evaluation, full_group_access=True
    )
    rows = [row async for row in export.fetch(db, scope)]
    return json.loads(to_json(rows, export.columns))


async def _render_group(db: AsyncSession, caller: User, perms: list[str], group_id: UUID, template: str) -> str:
    """Render a whole group's export at the service layer (union across visible evaluations)."""
    export = get_export(template)
    assert export is not None
    su = _su(caller, perms)
    evaluations = await resolve_group_evaluations(db, su, group_id, can_manage=False)
    return "".join(
        [chunk async for chunk in stream_export(db, export, su, evaluations, export_format=ExportFormat.CSV)]
    )


async def _render_group_json(db: AsyncSession, caller: User, perms: list[str], group_id: UUID, template: str) -> list:
    """Render a whole group's export as parsed JSON via stream_export's JSON branch (multi-evaluation)."""
    export = get_export(template)
    assert export is not None
    su = _su(caller, perms)
    evaluations = await resolve_group_evaluations(db, su, group_id, can_manage=False)
    body = "".join(
        [chunk async for chunk in stream_export(db, export, su, evaluations, export_format=ExportFormat.JSON)]
    )
    return json.loads(body)


async def _add_flagged_conversation(
    db: AsyncSession,
    *,
    evaluation: Evaluation,
    owner: User,
    reason: str,
    scenario_id: UUID | None = None,
    task_id: UUID | None = None,
) -> None:
    """Add a conversation + message + flag authored by ``owner`` to an existing evaluation.

    ``scenario_id`` / ``task_id`` tag the flag's denormalised columns so the scenario/task export
    filters have distinct rows to narrow.
    """
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db.add(model)
    await db.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db.add(assignment)
    await db.flush()
    if scenario_id is None:
        scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
        db.add(scenario)
        await db.flush()
        conversation_scenario_id = scenario.id
    else:
        conversation_scenario_id = scenario_id
    conversation_group = ConversationGroup(
        user_id=owner.id, evaluation_id=evaluation.id, name="g", scenario_id=conversation_scenario_id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=owner.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=conversation_scenario_id,
    )
    db.add(conversation)
    await db.flush()
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db.add(turn)
    await db.flush()
    message = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="x")
    db.add(message)
    await db.flush()
    flag = MessageFlag(
        reason=reason,
        created_by_id=owner.id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=evaluation.evaluation_group_id,
        scenario_id=scenario_id,
        task_id=task_id,
    )
    db.add(flag)
    await db.flush()
    db.add(FlaggedMessage(message_flag_id=flag.id, message_id=message.id))
    await db.flush()


async def _caller(db: AsyncSession, *, email: str, permissions: list[str]) -> User:
    role = Role(name=f"role-{email}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    return await create_user_service(db, email=email, roles=[role])


async def _seed_evaluation(db: AsyncSession, owner: User) -> Evaluation:
    """Build a public group → evaluation → conversation (one message) → one flag, owned by ``owner``."""
    group = await persist_evaluation_group(db, access_level=EvaluationGroupAccessLevel.PUBLIC, created_by_id=owner.id)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db.add(evaluation)
    await db.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db.add(model)
    await db.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db.add(assignment)
    await db.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db.add(scenario)
    await db.flush()
    conversation_group = ConversationGroup(
        user_id=owner.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=owner.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    db.add(conversation)
    await db.flush()
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db.add(turn)
    await db.flush()
    message = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="probe")
    db.add(message)
    await db.flush()
    flag = MessageFlag(
        reason="boom",
        created_by_id=owner.id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=group.id,
    )
    db.add(flag)
    await db.flush()
    db.add(FlaggedMessage(message_flag_id=flag.id, message_id=message.id))
    await db.flush()
    return evaluation


async def _add_evaluation_with_message(
    db: AsyncSession, *, group_id: UUID, owner: User, title: str, content: str
) -> None:
    """Add a second evaluation (with one conversation + message) to an existing group."""
    evaluation = Evaluation(title=title, description="d", evaluation_group_id=group_id, created_by_id=owner.id)
    db.add(evaluation)
    await db.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db.add(model)
    await db.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db.add(assignment)
    await db.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db.add(scenario)
    await db.flush()
    conversation_group = ConversationGroup(
        user_id=owner.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=owner.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    db.add(conversation)
    await db.flush()
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db.add(turn)
    await db.flush()
    db.add(Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content=content))
    await db.flush()


async def _add_conversation_with_message(
    db: AsyncSession, *, evaluation_id: UUID, assignment_id: UUID, owner: User, content: str
) -> None:
    """Add a conversation (one message) owned by ``owner`` to an existing evaluation."""
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation_id, position=0)
    db.add(scenario)
    await db.flush()
    conversation_group = ConversationGroup(
        user_id=owner.id, evaluation_id=evaluation_id, name="g", scenario_id=scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=owner.id,
        evaluation_id=evaluation_id,
        evaluation_ai_model_id=assignment_id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    db.add(conversation)
    await db.flush()
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db.add(turn)
    await db.flush()
    db.add(Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content=content))
    await db.flush()


class TestListExports:
    async def test_lists_flags_template(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="lister@example.com", permissions=[Permission.FLAGS_READ.value])

        response = await auth_db_client.get("/api/v1/exports", headers=_auth(caller))

        assert response.status_code == status.HTTP_200_OK
        keys = [item["key"] for item in response.json()["items"]]
        assert "flags" in keys


class TestExportTemplates:
    """Per-template content, exercised at the service layer (fetch + render).

    The inline HTTP endpoints were removed (exports are async-only now), so these drive
    `export.fetch(...)` + `to_csv`/`stream_export` directly — the same data + columns
    the background job renders — keeping the per-template content coverage.
    """

    async def test_flags(self, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="exporter@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        message = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .join(Conversation, col(Turn.conversation_id) == col(Conversation.id))
                .where(col(Conversation.evaluation_id) == evaluation.id)
            )
        ).scalar_one()

        text = await _render_eval(db_session, caller, [Permission.FLAGS_READ.value], evaluation, "flags")
        lines = text.splitlines()

        assert lines[0].startswith("Flag ID,Status,Red-flagged,Reason,")
        assert "Flagged message IDs" in lines[0]
        assert "Flagged turn IDs" in lines[0]
        assert len(lines) == 2  # header + the one flag
        assert "boom" in lines[1]
        # The flagged message + turn ids are emitted so the flag joins to the transcript export.
        assert str(message.id) in lines[1]
        assert str(message.turn_id) in lines[1]

    async def test_flags_filter_by_user_narrows(self, db_session: AsyncSession) -> None:
        # user_id maps to the flag author (created_by_id): a group-wide export narrows to one
        # red-teamer's flags.
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="filt-user@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # one flag ("boom") authored by caller
        other = await _caller(db_session, email="filt-other@example.com", permissions=perms)
        await _add_flagged_conversation(db_session, evaluation=evaluation, owner=other, reason="other-flag")

        unfiltered = (await _render_eval(db_session, caller, perms, evaluation, "flags")).splitlines()
        assert len(unfiltered) == 3  # header + both flags

        filtered = (
            await _render_eval(db_session, caller, perms, evaluation, "flags", filters=ExportFilters(user_id=caller.id))
        ).splitlines()
        assert len(filtered) == 2  # header + only the caller's flag
        assert "boom" in filtered[1]
        assert "other-flag" not in "\n".join(filtered)

    async def test_flags_filter_by_status_narrows_and_rejects_invalid(self, db_session: AsyncSession) -> None:
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="filt-status@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        flag = (
            await db_session.execute(select(MessageFlag).where(col(MessageFlag.evaluation_id) == evaluation.id))
        ).scalar_one()
        flag.status = FlagStatus.APPROVED
        await db_session.flush()

        approved = (
            await _render_eval(db_session, caller, perms, evaluation, "flags", filters=ExportFilters(status="approved"))
        ).splitlines()
        assert len(approved) == 2  # header + the approved flag

        rejected = (
            await _render_eval(db_session, caller, perms, evaluation, "flags", filters=ExportFilters(status="rejected"))
        ).splitlines()
        assert len(rejected) == 1  # header only — none rejected

        # An unknown status is rejected at construction (→ 422 at the API), symmetric with the list
        # endpoints whose status query param is enum-typed — not silently dropped to a full export.
        with pytest.raises(ValidationError):
            ExportFilters(status="bogus")

    async def test_flags_filter_cannot_surface_row_under_soft_deleted_parent(self, db_session: AsyncSession) -> None:
        # A filter narrows WITHIN the live-visibility scope; it can never surface a row whose parent
        # conversation is soft-deleted (the service joins the live parent chain; filters apply after).
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="filt-softdel@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # one flag authored by caller
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.soft_delete(None)
        await db_session.flush()

        # A filter that WOULD match the flag (its own author) must still not surface it.
        filtered = (
            await _render_eval(db_session, caller, perms, evaluation, "flags", filters=ExportFilters(user_id=caller.id))
        ).splitlines()
        assert len(filtered) == 1  # header only — the soft-deleted parent hides the flag despite the match

    async def test_flags_filter_by_date_range_narrows(self, db_session: AsyncSession) -> None:
        # created_from (>=) / created_to (<=) narrow on the flag's created_at.
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="filt-date@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # one flag, created "now"
        old_flag = (
            await db_session.execute(select(MessageFlag).where(col(MessageFlag.evaluation_id) == evaluation.id))
        ).scalar_one()
        old_flag.created_at = datetime(2020, 1, 1, tzinfo=UTC)  # backdate it well into the past
        await db_session.flush()
        await _add_flagged_conversation(db_session, evaluation=evaluation, owner=caller, reason="recent")

        recent = (
            await _render_eval(
                db_session,
                caller,
                perms,
                evaluation,
                "flags",
                filters=ExportFilters(created_from=datetime(2023, 1, 1, tzinfo=UTC)),
            )
        ).splitlines()
        assert len(recent) == 2  # header + the recent flag only (backdated one excluded)
        # Assert WHICH flag survived, not just the count — a >= / <= (from/to) swap would keep the
        # wrong flag yet still be length 2, so a count-only check can't see the boundary direction.
        assert "recent" in "\n".join(recent)
        assert "boom" not in "\n".join(recent)

        old = (
            await _render_eval(
                db_session,
                caller,
                perms,
                evaluation,
                "flags",
                filters=ExportFilters(created_to=datetime(2023, 1, 1, tzinfo=UTC)),
            )
        ).splitlines()
        assert len(old) == 2  # header + the backdated flag only (recent one excluded)
        assert "boom" in "\n".join(old)
        assert "recent" not in "\n".join(old)

    async def test_flags_filter_by_scenario_narrows(self, db_session: AsyncSession) -> None:
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="filt-scenario@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # one untagged flag ("boom")
        s_in = Scenario(evaluation_id=evaluation.id, name="in", description="d", position=0)
        s_out = Scenario(evaluation_id=evaluation.id, name="out", description="d", position=1)
        db_session.add_all([s_in, s_out])
        await db_session.flush()
        await _add_flagged_conversation(
            db_session, evaluation=evaluation, owner=caller, reason="in-scope", scenario_id=s_in.id
        )
        await _add_flagged_conversation(
            db_session, evaluation=evaluation, owner=caller, reason="out-scope", scenario_id=s_out.id
        )

        filtered = (
            await _render_eval(
                db_session, caller, perms, evaluation, "flags", filters=ExportFilters(scenario_id=s_in.id)
            )
        ).splitlines()
        joined = "\n".join(filtered)
        assert len(filtered) == 2  # header + only the s_in flag
        assert "in-scope" in joined
        assert "out-scope" not in joined
        assert "boom" not in joined  # the untagged seed flag is excluded too

    async def test_flags_filter_by_task_narrows(self, db_session: AsyncSession) -> None:
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="filt-task@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        scenario = Scenario(evaluation_id=evaluation.id, name="s", description="d", position=0)
        db_session.add(scenario)
        await db_session.flush()
        t_in = Task(scenario_id=scenario.id, name="t-in", description="d")
        t_out = Task(scenario_id=scenario.id, name="t-out", description="d")
        db_session.add_all([t_in, t_out])
        await db_session.flush()
        await _add_flagged_conversation(
            db_session, evaluation=evaluation, owner=caller, reason="task-in", scenario_id=scenario.id, task_id=t_in.id
        )
        await _add_flagged_conversation(
            db_session,
            evaluation=evaluation,
            owner=caller,
            reason="task-out",
            scenario_id=scenario.id,
            task_id=t_out.id,
        )

        filtered = (
            await _render_eval(db_session, caller, perms, evaluation, "flags", filters=ExportFilters(task_id=t_in.id))
        ).splitlines()
        joined = "\n".join(filtered)
        assert len(filtered) == 2
        assert "task-in" in joined
        assert "task-out" not in joined

    async def test_flags_filter_date_boundary_inclusive_and_inverted(self, db_session: AsyncSession) -> None:
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="filt-boundary@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        flag = (
            await db_session.execute(select(MessageFlag).where(col(MessageFlag.evaluation_id) == evaluation.id))
        ).scalar_one()
        at = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        flag.created_at = at
        await db_session.flush()

        # Both bounds are inclusive (>= / <=): a row exactly at the instant survives from == to == at.
        exact = (
            await _render_eval(
                db_session, caller, perms, evaluation, "flags", filters=ExportFilters(created_from=at, created_to=at)
            )
        ).splitlines()
        assert len(exact) == 2  # header + the flag

        # An inverted range (from > to) is an empty intersection, not an error — header only.
        inverted = (
            await _render_eval(
                db_session,
                caller,
                perms,
                evaluation,
                "flags",
                filters=ExportFilters(created_from=at + timedelta(seconds=1), created_to=at - timedelta(seconds=1)),
            )
        ).splitlines()
        assert len(inverted) == 1  # header only

    async def test_reviews_filter_by_status_narrows(self, db_session: AsyncSession) -> None:
        # The reviews export runs the distinct coerce_status(ReviewStatus, …) path (not FlagStatus).
        perms = [Permission.REVIEWS_READ.value]
        caller = await _caller(db_session, email="filt-reviews@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # seeds a flag
        flag = (
            await db_session.execute(select(MessageFlag).where(col(MessageFlag.evaluation_id) == evaluation.id))
        ).scalar_one()
        r1 = await _caller(db_session, email="reviewer-a@example.com", permissions=perms)
        r2 = await _caller(db_session, email="reviewer-b@example.com", permissions=perms)
        db_session.add_all(
            [
                Review(
                    message_flag_id=flag.id,
                    reviewer_id=r1.id,
                    assigned_by_id=caller.id,
                    evaluation_id=evaluation.id,
                    status=ReviewStatus.APPROVED,
                ),
                Review(
                    message_flag_id=flag.id,
                    reviewer_id=r2.id,
                    assigned_by_id=caller.id,
                    evaluation_id=evaluation.id,
                    status=ReviewStatus.REJECTED,
                ),
            ]
        )
        await db_session.flush()

        approved = (
            await _render_eval(
                db_session, caller, perms, evaluation, "reviews", filters=ExportFilters(status="approved")
            )
        ).splitlines()
        assert len(approved) == 2  # header + only the approved review
        assert "approved" in approved[1]
        assert "rejected" not in approved[1]

    async def test_flags_export_includes_message_content_for_all_flagged_messages(
        self, db_session: AsyncSession
    ) -> None:
        # The flag's messages (ids + content) ride one JSON cell, so a multi-message flag shows
        # every id paired with its content, robust to commas/newlines in the text.
        caller = await _caller(db_session, email="content@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)  # flag over one message, content "probe"
        flag = (
            await db_session.execute(select(MessageFlag).where(col(MessageFlag.evaluation_id) == evaluation.id))
        ).scalar_one()
        # Add a second flagged message whose content has a comma AND a newline — the case the JSON
        # cell must survive through CSV quoting.
        tricky = "has a comma, and a\nnewline"
        turn = Turn(conversation_id=flag.conversation_id, turn_index=1)
        db_session.add(turn)
        await db_session.flush()
        second = Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content=tricky)
        db_session.add(second)
        await db_session.flush()
        db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=second.id))
        await db_session.flush()

        text = await _render_eval(db_session, caller, [Permission.FLAGS_READ.value], evaluation, "flags")

        # The JSON cell embeds a literal comma (from the content), so parse via csv.reader — a naive
        # split on commas would mis-slice the columns; csv.reader honors the RFC-4180 quoting. (The
        # content's newline is JSON-escaped inside the cell, so the row itself stays single-line.)
        rows = list(csv.reader(io.StringIO(text)))
        header, data = rows[0], rows[1]
        assert "Flagged messages" in header
        messages = json.loads(data[header.index("Flagged messages")])
        by_id = {m["message_id"]: m["content"] for m in messages}
        assert len(messages) == 2  # one JSON entry per flagged message
        # Each id is paired with its OWN content (a pairing swap would fail this), and the
        # comma+newline text round-trips intact through CSV quoting + JSON encoding.
        assert by_id[str(second.id)] == tricky
        assert set(by_id.values()) == {"probe", tricky}

    async def test_conversations(self, db_session: AsyncSession) -> None:
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="convexp@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)

        lines = (await _render_eval(db_session, caller, perms, evaluation, "conversations")).splitlines()

        assert lines[0].startswith("Conversation ID,Evaluation ID,Evaluation title,Model assignment ID,")
        assert "Owner email" in lines[0]
        assert len(lines) == 2  # header + the one conversation seeded
        # Denormalised, human-readable context: the evaluation id disambiguates group
        # exports, the owner email resolves the raw UUID.
        assert str(evaluation.id) in lines[1]
        assert "convexp@example.com" in lines[1]

    async def test_conversation_groups(self, db_session: AsyncSession) -> None:
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="cgexp@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)

        lines = (await _render_eval(db_session, caller, perms, evaluation, "conversation-groups")).splitlines()

        assert lines[0].startswith("Group ID,Name,")
        assert len(lines) == 2  # header + the one conversation group seeded

    async def test_conversation_groups_filter_by_scenario_narrows(self, db_session: AsyncSession) -> None:
        # A group targets exactly one scenario, so the dimension the conversations export
        # already honors applies one level up — and is accepted rather than rejected as
        # unsupported (400).
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="cgfilt@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # seeds group "g" on scenario "s"
        other = Scenario(evaluation_id=evaluation.id, name="out", description="d", position=1)
        db_session.add(other)
        await db_session.flush()
        db_session.add(
            ConversationGroup(user_id=caller.id, evaluation_id=evaluation.id, name="out-scope", scenario_id=other.id)
        )
        await db_session.flush()

        unfiltered = (await _render_eval(db_session, caller, perms, evaluation, "conversation-groups")).splitlines()
        assert len(unfiltered) == 3  # header + both groups

        seeded = (
            await db_session.execute(select(ConversationGroup).where(col(ConversationGroup.name) == "g"))
        ).scalar_one()
        filtered = (
            await _render_eval(
                db_session,
                caller,
                perms,
                evaluation,
                "conversation-groups",
                filters=ExportFilters(scenario_id=seeded.scenario_id),
            )
        ).splitlines()

        assert len(filtered) == 2  # header + the seeded group only
        assert "out-scope" not in "\n".join(filtered)

    async def test_reviews_header_only_when_none(self, db_session: AsyncSession) -> None:
        perms = [Permission.REVIEWS_READ.value]
        caller = await _caller(db_session, email="revexp@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)

        lines = (await _render_eval(db_session, caller, perms, evaluation, "reviews")).splitlines()

        assert lines[0].startswith("Review ID,")
        assert len(lines) == 1  # header only — no reviews seeded

    async def test_reviews_body_renders_verdict_cells(self, db_session: AsyncSession) -> None:
        # Exercise the ReviewRow value lambdas (a mistyped attribute would ship green otherwise):
        # seed a recorded verdict and assert the reviewer email + verdict cells render.
        perms = [Permission.REVIEWS_READ.value]
        caller = await _caller(db_session, email="revbody@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # caller authored the seeded flag
        reviewer = await _caller(db_session, email="reviewer@example.com", permissions=[])
        flag = (
            await db_session.execute(select(MessageFlag).where(col(MessageFlag.evaluation_id) == evaluation.id))
        ).scalar_one()
        review = Review(
            message_flag_id=flag.id,
            reviewer_id=reviewer.id,
            assigned_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ReviewStatus.APPROVED,
            successful_exploit=True,
            notes="clear exploit",
        )
        db_session.add(review)
        await db_session.flush()

        lines = (await _render_eval(db_session, caller, perms, evaluation, "reviews")).splitlines()

        assert lines[0].startswith("Review ID,")
        assert len(lines) == 2  # header + the one verdict
        row = lines[1]
        assert "reviewer@example.com" in row  # resolved reviewer email
        assert "approved" in row  # status
        assert "clear exploit" in row  # notes
        assert "true" in row  # successful_exploit

    async def test_flags_export_is_group_wide(self, db_session: AsyncSession) -> None:
        # An authorised export is whole-group: an in-group owner (no global manage) exporting a
        # group that also holds a colleague's flags gets BOTH — not a falsified owner-only view.
        perms = [Permission.FLAGS_READ.value]
        alice = await _caller(db_session, email="alice-flags@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, alice)  # alice owns the group + has flag "boom"
        bob = await _caller(db_session, email="bob-flags@example.com", permissions=perms)
        await _add_flagged_conversation(db_session, evaluation=evaluation, owner=bob, reason="bob-exploit")

        body = await _render_eval(db_session, alice, perms, evaluation, "flags")  # group_wide=True (authorised)

        assert "boom" in body  # alice's own flag
        assert "bob-exploit" in body  # and the colleague's — the whole group

    async def test_flags_export_self_only_without_group_access(self, db_session: AsyncSession) -> None:
        # Fail-safe default: a scope built WITHOUT group authority (full_group_access=False) reads
        # only the caller's own rows, so a forgotten flag can never over-share.
        perms = [Permission.FLAGS_READ.value]
        alice = await _caller(db_session, email="alice-scoped@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, alice)
        bob = await _caller(db_session, email="bob-scoped@example.com", permissions=perms)
        await _add_flagged_conversation(db_session, evaluation=evaluation, owner=bob, reason="bob-exploit")

        body = await _render_eval(db_session, alice, perms, evaluation, "flags", group_wide=False)

        assert "boom" in body  # alice's own flag
        assert "bob-exploit" not in body  # the colleague's flag is scoped out when not group-wide

    async def test_transcript(self, db_session: AsyncSession) -> None:
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="trexp@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.data_license_id = curated_license_id(
            "CC0-1.0"
        )  # explicit override → surfaces in the Data license column
        await db_session.flush()

        lines = (await _render_eval(db_session, caller, perms, evaluation, "transcript")).splitlines()

        # Message + Turn + Replaces message ids carry through so the row is reconstructable;
        # the effective data license is embedded per row.
        assert lines[0].startswith(
            "Evaluation ID,Evaluation title,Data license,Conversation ID,Message ID,Turn ID,Replaces message ID,Role,"
        )
        assert len(lines) == 2  # header + the one message seeded
        assert "probe" in lines[1]
        assert "CC0-1.0" in lines[1]

    async def test_transcript_exports_plaintext_from_a_protected_conversation(self, db_session: AsyncSession) -> None:
        # The column callables have no settings, so the seal has to be opened where the row is built.
        # A miss here ships a CSV of ciphertext to the operator who is allowed to read it.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="trexp-sealed@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        message = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation.id)
            )
        ).scalar_one()
        # The template reads the row's own discriminator, so sealing the message is what this covers;
        # the conversation-level flag decides writes, which happen well before an export runs.
        stored, encrypted = seal_content("probe", protected=True, settings=get_settings())
        message.content, message.content_encrypted = stored, encrypted
        await db_session.flush()

        lines = (await _render_eval(db_session, caller, perms, evaluation, "transcript")).splitlines()

        assert len(lines) == 2
        assert "probe" in lines[1]

    async def test_flags_and_engagement_report_embed_plaintext_from_a_sealed_message(
        self, db_session: AsyncSession
    ) -> None:
        # Both templates embed the shared `message_dicts` shape rather than reading the column
        # themselves, so a regression there reaches an operator through two reports at once.
        perms = [Permission.FLAGS_READ.value, Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="embed-sealed@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        message = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .join(Conversation, col(Turn.conversation_id) == col(Conversation.id))
                .where(col(Conversation.evaluation_id) == evaluation.id)
            )
        ).scalar_one()
        stored, encrypted = seal_content("sealed prompt", protected=True, settings=get_settings())
        message.content, message.content_encrypted = stored, encrypted
        await db_session.flush()

        flags = await _render_eval(db_session, caller, perms, evaluation, "flags")
        report = await _render_eval(db_session, caller, perms, evaluation, "engagement_report")

        assert "sealed prompt" in flags
        assert "sealed prompt" in report

    async def test_transcript_and_conversations_carry_both_tag_layers(self, db_session: AsyncSession) -> None:
        # Tags are prompt context (the conversation's on every turn, the message's on one turn), so
        # an export without them can't show what the model was told. Both layers ride JSON cells,
        # keyed per layer — a swap or a dropped layer fails here.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-exp@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"env": "prod"}
        message = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation.id)
            )
        ).scalar_one()
        # A comma + newline in the value: the case JSON-in-CSV quoting has to survive.
        message.tags = {"turn": "1, then\nmore"}
        db_session.add_all([conversation, message])
        await db_session.flush()

        transcript = list(
            csv.reader(io.StringIO(await _render_eval(db_session, caller, perms, evaluation, "transcript")))
        )
        header, row = transcript[0], transcript[1]
        assert json.loads(row[header.index("Conversation tags")]) == {"env": "prod"}
        assert json.loads(row[header.index("Message tags")]) == {"turn": "1, then\nmore"}

        conversations = list(
            csv.reader(io.StringIO(await _render_eval(db_session, caller, perms, evaluation, "conversations")))
        )
        conv_header, conv_row = conversations[0], conversations[1]
        assert json.loads(conv_row[conv_header.index("Tags")]) == {"env": "prod"}

    async def test_exports_name_the_tags_the_prompt_fold_leaves_out(self, db_session: AsyncSession) -> None:
        # Every tag column is the map *as stored*, which is not the map the model got: a restricted
        # evaluation folds only its allowed keys. Without naming the difference, the artefact used as
        # evidence over-attributes context to the model — so each template carries the dropped keys.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-unsent@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.tags_restricted = True
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"env": "prod", "legacy": "x"}
        db_session.add_all([evaluation, conversation, EvaluationTagKey(evaluation_id=evaluation.id, key="env")])
        await db_session.flush()

        transcript = list(
            csv.reader(io.StringIO(await _render_eval(db_session, caller, perms, evaluation, "transcript")))
        )
        header, row = transcript[0], transcript[1]
        # JSON-encoded in CSV like the tag maps beside it: a bare join is ambiguous for a key with a
        # comma, and a key may start with `-`, which a spreadsheet reads as a formula.
        assert json.loads(row[header.index("Tags not sent")]) == ["legacy"]
        assert json.loads(row[header.index("Conversation tags")]) == {"env": "prod", "legacy": "x"}

        conversations = list(
            csv.reader(io.StringIO(await _render_eval(db_session, caller, perms, evaluation, "conversations")))
        )
        conv_header, conv_row = conversations[0], conversations[1]
        assert json.loads(conv_row[conv_header.index("Tags not sent")]) == ["legacy"]

        report = list(
            csv.reader(io.StringIO(await _render_eval(db_session, caller, perms, evaluation, "engagement_report")))
        )
        rep_header, rep_row = report[0], report[1]
        assert json.loads(rep_row[rep_header.index("Conversation tags")]) == {"env": "prod", "legacy": "x"}
        assert json.loads(rep_row[rep_header.index("Tags not sent")]) == ["legacy"]

    async def test_transcript_carries_the_recorded_tag_context(self, db_session: AsyncSession) -> None:
        # `Tag context` is the map the reply's prompt actually carried, distinct from the stored
        # `Message tags` (the fold's input) and from the policy-derived `Tags not sent`.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-recorded@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        message = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation.id)
            )
        ).scalar_one()
        message.extra = {
            **message.extra,
            TAG_CONTEXT_EXTRA_KEY: {"env": "prod"},
            TAG_CONTEXT_PARTIAL_EXTRA_KEY: True,
        }
        db_session.add(message)
        await db_session.flush()

        transcript = list(
            csv.reader(io.StringIO(await _render_eval(db_session, caller, perms, evaluation, "transcript")))
        )
        header, row = transcript[0], transcript[1]
        assert json.loads(row[header.index("Tag context")]) == {"env": "prod"}
        # Without this the cell reads as the whole reply's context, while `Content` holds prefix +
        # continuation and the prefix's own record is on a superseded row the export excludes.
        assert row[header.index("Tag context partial")] == "true"

    async def test_unsent_is_historical_only_for_the_row_that_recorded_it(self, db_session: AsyncSession) -> None:
        # A tag folded and later dropped from the evaluation's allowed keys must not be reported unsent
        # for the reply whose own record proves it was sent — that is the historically true answer, and
        # it must diverge from a message with no record, which stays judged against the *current* policy
        # (this is the assertion that fails under the old, always-current-policy behaviour).
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-historical@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.tags_restricted = True  # "env" is not in the (empty) allowed set, so it now reads unsent
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"env": "prod"}
        recorded = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation.id)
            )
        ).scalar_one()
        recorded.extra = {**recorded.extra, TAG_CONTEXT_EXTRA_KEY: {"env": "prod"}}
        later_turn = Turn(conversation_id=conversation.id, turn_index=1)
        db_session.add(later_turn)
        await db_session.flush()
        unrecorded = Message(
            turn_id=later_turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="later"
        )
        db_session.add_all([evaluation, conversation, recorded, unrecorded])
        await db_session.flush()

        rows = await _render_eval_json(db_session, caller, perms, evaluation, "transcript")
        by_id = {row["Message ID"]: row for row in rows}
        assert by_id[str(recorded.id)]["Tags not sent"] == []  # historical: the record proves it was sent
        assert by_id[str(unrecorded.id)]["Tags not sent"] == ["env"]  # no record: judged against today's policy

    async def test_engagement_report_unsent_is_historical_for_a_turn_with_a_record(
        self, db_session: AsyncSession
    ) -> None:
        # The engagement report reduces per conversation, but `_unsent_across_turns` must judge each
        # turn on its own record before reducing: `env` is folded and later dropped from the allowed
        # set, but the turn's own reply recorded it, so it must not be reported unsent — while `legacy`,
        # authored on the same turn but never recorded, still is. Fails under a reduction that always
        # calls the live policy, ignoring any turn's record.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-report-historical@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.tags_restricted = True  # nothing is in the (empty) allowed set
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"env": "prod"}
        turn = (await db_session.execute(select(Turn).where(col(Turn.conversation_id) == conversation.id))).scalar_one()
        recorded = (await db_session.execute(select(Message).where(col(Message.turn_id) == turn.id))).scalar_one()
        recorded.extra = {**recorded.extra, TAG_CONTEXT_EXTRA_KEY: {"env": "prod"}}
        db_session.add(
            Message(
                turn_id=turn.id,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETE,
                content="ask",
                tags={"legacy": "x"},
            )
        )
        db_session.add_all([evaluation, conversation, recorded])
        await db_session.flush()

        rows = await _render_eval_json(db_session, caller, perms, evaluation, "engagement_report")

        assert rows[0]["Tags not sent"] == ["legacy"]

    async def test_unsent_column_is_empty_when_the_policy_sends_everything(self, db_session: AsyncSession) -> None:
        # The inverse: an unrestricted evaluation folds every tag, so nothing may be reported dropped.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-all-sent@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"env": "prod"}
        db_session.add(conversation)
        await db_session.flush()

        rows = list(csv.reader(io.StringIO(await _render_eval(db_session, caller, perms, evaluation, "conversations"))))
        header, row = rows[0], rows[1]
        assert json.loads(row[header.index("Tags not sent")]) == []

    async def test_json_export_carries_tags_as_real_objects(self, db_session: AsyncSession) -> None:
        # CSV cells are JSON-encoded strings; the JSON export must emit the same maps as real nested
        # objects (and the dropped keys as a real array), or a consumer has to double-decode one format.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-json@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.tags_restricted = True
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"env": "prod", "legacy": "x"}
        message = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation.id)
            )
        ).scalar_one()
        message.tags = {"turn": "1"}
        db_session.add_all(
            [evaluation, conversation, message, EvaluationTagKey(evaluation_id=evaluation.id, key="env")]
        )
        await db_session.flush()

        transcript = await _render_eval_json(db_session, caller, perms, evaluation, "transcript")
        assert transcript[0]["Conversation tags"] == {"env": "prod", "legacy": "x"}
        assert transcript[0]["Message tags"] == {"turn": "1"}
        # The transcript judges the turn's *merged* map, so a message tag outside the allow-list is
        # dropped as well — `turn` never reached the model either.
        assert transcript[0]["Tags not sent"] == ["legacy", "turn"]

        # Every template that carries the column emits a real array, not a joined string: a consumer
        # should not have to special-case one export's shape against another's.

        conversations = await _render_eval_json(db_session, caller, perms, evaluation, "conversations")
        assert conversations[0]["Tags"] == {"env": "prod", "legacy": "x"}
        assert conversations[0]["Tags not sent"] == ["legacy"]

        report = await _render_eval_json(db_session, caller, perms, evaluation, "engagement_report")
        assert report[0]["Conversation tags"] == {"env": "prod", "legacy": "x"}
        assert report[0]["Messages"][0]["tags"] == {"turn": "1"}
        # The deliverable embeds the message layer, so its column has to judge both layers.
        assert report[0]["Tags not sent"] == ["legacy", "turn"]

    async def test_the_report_judges_message_tags_on_a_production_shaped_turn(self, db_session: AsyncSession) -> None:
        # A real turn is a user message carrying the tags plus an assistant placeholder carrying none, so
        # a reduction over *messages* would fold that empty map in as its own turn and silently make the
        # message layer unreportable. Grouping by turn is what keeps this answerable.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-turnshape@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.tags_enabled = False  # the fold sends nothing, so every authored key is unsent
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        turn = (await db_session.execute(select(Turn).where(col(Turn.conversation_id) == conversation.id))).scalar_one()
        db_session.add(
            Message(
                turn_id=turn.id,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETE,
                content="ask",
                tags={"persona": "pirate"},
            )
        )
        db_session.add(evaluation)
        await db_session.flush()

        # A second turn tagged with a *different* key: with tagging off neither reached a prompt, and a
        # key missing from one turn must not read as "that turn sent it" — the trap an intersection over
        # the per-turn sets falls into, which empties the column exactly when the turns differ.
        later = Turn(conversation_id=conversation.id, turn_index=1)
        db_session.add(later)
        await db_session.flush()
        db_session.add_all(
            [
                Message(
                    turn_id=later.id,
                    role=MessageRole.USER,
                    status=MessageStatus.COMPLETE,
                    content="again",
                    tags={"tone": "curt"},
                ),
                Message(turn_id=later.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="ok"),
            ]
        )
        await db_session.flush()

        rows = await _render_eval_json(db_session, caller, perms, evaluation, "engagement_report")

        assert rows[0]["Tags not sent"] == ["persona", "tone"]

    async def test_the_report_reports_a_key_no_turn_sent_and_not_one_some_turn_did(
        self, db_session: AsyncSession
    ) -> None:
        # One row covers a whole conversation, so the per-turn answer has to be reduced. Merging the
        # message maps would let the last turn decide: a later turn blanking `env` would claim `env`
        # never reached the model though an earlier turn sent it.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-rollup@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"env": "prod", "never": ""}
        sent = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation.id)
            )
        ).scalar_one()
        sent.tags = {"env": "staging"}
        # A second turn that blanks the same key — the one that used to decide the whole row.
        later_turn = Turn(conversation_id=conversation.id, turn_index=1)
        db_session.add(later_turn)
        await db_session.flush()
        db_session.add(
            Message(
                turn_id=later_turn.id,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETE,
                content="second",
                tags={"env": "", "never": ""},
            )
        )
        db_session.add_all([conversation, sent])
        await db_session.flush()

        rows = await _render_eval_json(db_session, caller, perms, evaluation, "engagement_report")

        # Both halves in one row: `env` was sent by the first turn, so a later turn blanking it must not
        # make the report claim it never arrived; `never` carries no text on either turn, so no turn sent
        # it and it is named. An intersection over the per-turn sets gets the second half wrong.
        assert rows[0]["Tags not sent"] == ["never"]

    async def test_a_message_tag_overrides_the_conversation_tag_of_the_same_key(self, db_session: AsyncSession) -> None:
        # The fold merges the two layers with the message's winning per key, so a blank message value
        # over a filled conversation value means nothing is sent for that key — the reverse order would
        # report the opposite. Only the merged map can tell them apart.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tags-merge@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        conversation = (
            await db_session.execute(select(Conversation).where(col(Conversation.evaluation_id) == evaluation.id))
        ).scalar_one()
        conversation.tags = {"note": "from the conversation"}
        message = (
            await db_session.execute(
                select(Message)
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation.id)
            )
        ).scalar_one()
        message.tags = {"note": ""}
        db_session.add_all([conversation, message])
        await db_session.flush()

        rows = await _render_eval_json(db_session, caller, perms, evaluation, "transcript")
        assert rows[0]["Tags not sent"] == ["note"]

    async def test_conversations_filter_by_user_narrows(self, db_session: AsyncSession) -> None:
        # The conversations export maps user_id onto Conversation.user_id (the owner).
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="conv-filt@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)  # caller's conversation
        other = await _caller(db_session, email="conv-other@example.com", permissions=perms)
        await _add_flagged_conversation(db_session, evaluation=evaluation, owner=other, reason="x")

        unfiltered = (await _render_eval(db_session, caller, perms, evaluation, "conversations")).splitlines()
        assert len(unfiltered) == 3  # header + both conversations

        filtered = (
            await _render_eval(
                db_session, caller, perms, evaluation, "conversations", filters=ExportFilters(user_id=caller.id)
            )
        ).splitlines()
        assert len(filtered) == 2  # header + only the caller's conversation

    async def test_transcript_filter_by_user_narrows(self, db_session: AsyncSession) -> None:
        # Proves the shared iter_scoped_conversations path threads the conversation-level filter.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="tr-filt@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        other = await _caller(db_session, email="tr-other@example.com", permissions=perms)
        await _add_flagged_conversation(db_session, evaluation=evaluation, owner=other, reason="x")

        unfiltered = (await _render_eval(db_session, caller, perms, evaluation, "transcript")).splitlines()
        assert len(unfiltered) == 3  # header + one message per conversation

        filtered = (
            await _render_eval(
                db_session, caller, perms, evaluation, "transcript", filters=ExportFilters(user_id=caller.id)
            )
        ).splitlines()
        assert len(filtered) == 2  # header + only the caller's conversation's message

    async def test_transcript_inherits_group_license(self, db_session: AsyncSession) -> None:
        # An evaluation with no override exports its group's license — the group
        # layer flows through `resolve_effective_licenses` into the Data license column.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="trexp-group@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        group = await db_session.get(EvaluationGroup, evaluation.evaluation_group_id)
        assert group is not None
        group.data_license_id = curated_license_id("CC0-1.0")
        await db_session.flush()

        lines = (await _render_eval(db_session, caller, perms, evaluation, "transcript")).splitlines()

        assert "CC0-1.0" in lines[1]

    async def test_transcript_and_report_name_the_no_license_sentinel(self, db_session: AsyncSession) -> None:
        # The sentinel is the first curated licence to reach the licence cell with `spdx_id IS NULL`,
        # so it is the only value that exercises the column's `or lic.name` arm — and clause 4 of its
        # own legal text promises exports carry "No license". Without this, dropping the fallback
        # leaves every closed-data export with a blank licence column and a green suite.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="trexp-none@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.data_license_id = curated_license_id(NO_LICENSE_SPDX_ID)
        await db_session.flush()

        transcript = (await _render_eval(db_session, caller, perms, evaluation, "transcript")).splitlines()
        report = (await _render_eval(db_session, caller, perms, evaluation, "engagement_report")).splitlines()

        assert "No license" in transcript[1]
        assert "No license" in report[1]

    async def test_engagement_report(self, db_session: AsyncSession) -> None:
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="report@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)
        evaluation.data_license_id = curated_license_id(
            "CC0-1.0"
        )  # explicit override → surfaces in the Data license column
        await db_session.flush()

        lines = (await _render_eval(db_session, caller, perms, evaluation, "engagement_report")).splitlines()

        assert lines[0].startswith(
            "Red-teamer email,Evaluation group ID,Evaluation group title,Evaluation ID,Evaluation title,"
        )
        assert "Data license" in lines[0]  # license metadata embedded
        assert len(lines) == 2  # header + the one conversation seeded
        row = lines[1]
        assert "report@example.com" in row  # denormalized owner email
        assert "probe" in row  # the message content, embedded in the JSON blob
        assert "message_id" in row  # each embedded message keeps its id (reconstructable)
        assert "CC0-1.0" in row  # the effective data license

    async def test_engagement_report_json_nests_messages_as_array(self, db_session: AsyncSession) -> None:
        # The "real nested arrays" decision: in JSON the Messages column is a structured array of
        # message objects, not the escaped JSON string the CSV cell carries.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="report-json@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)

        rows = await _render_eval_json(db_session, caller, perms, evaluation, "engagement_report")

        assert len(rows) == 1
        messages = rows[0]["Messages"]
        assert isinstance(messages, list)  # a real array, not a stringified blob
        assert messages
        assert isinstance(messages[0], dict)
        assert messages[0]["content"] == "probe"  # message content as structured data
        assert "message_id" in messages[0]

    async def test_flags_json_nests_messages_as_array(self, db_session: AsyncSession) -> None:
        # Parity with engagement_report: in JSON the flagged-messages column is a structured array of
        # message objects (carrying content), not the escaped JSON string the CSV cell carries.
        perms = [Permission.FLAGS_READ.value]
        caller = await _caller(db_session, email="flags-json@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)

        rows = await _render_eval_json(db_session, caller, perms, evaluation, "flags")

        assert len(rows) == 1
        messages = rows[0]["Flagged messages"]
        assert isinstance(messages, list)  # a real array, not a stringified blob
        assert messages
        assert isinstance(messages[0], dict)
        assert "content" in messages[0]
        assert "message_id" in messages[0]

    async def test_engagement_report_is_group_wide_for_authorized_exporter(self, db_session: AsyncSession) -> None:
        # An authorised export is a whole-group deliverable: an in-group owner (no global manage)
        # sees EVERY red-teamer's conversation — their own AND a colleague's — so the report isn't
        # a falsified owner-only view. Non-owners can't export at all (authorised at the endpoint),
        # so surfacing a colleague's email/content here is by design, not a leak.
        perms = [Permission.CONVERSATIONS_READ.value]
        alice = await _caller(db_session, email="alice@example.com", permissions=perms)
        bob = await _caller(db_session, email="bob@example.com", permissions=perms)
        group = await persist_evaluation_group(
            db_session, access_level=EvaluationGroupAccessLevel.PUBLIC, created_by_id=alice.id
        )
        evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=alice.id)
        db_session.add(evaluation)
        await db_session.flush()
        alias = f"m-{uuid4().hex[:8]}"
        model = AiModel(
            name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id"
        )
        db_session.add(model)
        await db_session.flush()
        assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
        db_session.add(assignment)
        await db_session.flush()
        await _add_conversation_with_message(
            db_session, evaluation_id=evaluation.id, assignment_id=assignment.id, owner=alice, content="alice-secret"
        )
        await _add_conversation_with_message(
            db_session, evaluation_id=evaluation.id, assignment_id=assignment.id, owner=bob, content="bob-secret"
        )

        body = await _render_eval(db_session, alice, perms, evaluation, "engagement_report")

        assert "alice@example.com" in body
        assert "alice-secret" in body
        assert "bob@example.com" in body  # group-wide: the whole engagement, not just the owner's rows
        assert "bob-secret" in body

    async def test_group_transcript(self, db_session: AsyncSession) -> None:
        # The group export unions the per-evaluation export across the group's visible
        # evaluations — here one evaluation with one message.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="grpexp@example.com", permissions=perms)
        evaluation = await _seed_evaluation(db_session, caller)

        lines = (
            await _render_group(db_session, caller, perms, evaluation.evaluation_group_id, "transcript")
        ).splitlines()

        # The group stream is BOM-prefixed (Excel encoding); strip it before matching the header.
        assert (
            lines[0]
            .lstrip("﻿")
            .startswith("Evaluation ID,Evaluation title,Data license,Conversation ID,Message ID,Turn ID,")
        )
        assert len(lines) == 2  # header + the one message in the group's one evaluation
        assert "probe" in lines[1]

    async def test_group_transcript_unions_multiple_evaluations(self, db_session: AsyncSession) -> None:
        # Regression guard: a group with several evaluations emits a single header then every
        # evaluation's rows — fetched/streamed per evaluation, not buffered as one giant list.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="multigrp@example.com", permissions=perms)
        first = await _seed_evaluation(db_session, caller)  # group + eval "E" with message "probe"
        group_id = first.evaluation_group_id
        await _add_evaluation_with_message(
            db_session, group_id=group_id, owner=caller, title="E2", content="second-probe"
        )

        body = await _render_group(db_session, caller, perms, group_id, "transcript")
        assert body.startswith("﻿")  # UTF-8 BOM once, at the very start
        lines = body.splitlines()

        assert lines.count(lines[0]) == 1  # header appears exactly once
        assert (
            lines[0]
            .lstrip("﻿")
            .startswith("Evaluation ID,Evaluation title,Data license,Conversation ID,Message ID,Turn ID,")
        )
        assert len(lines) == 3  # one header + one message row per evaluation
        assert "probe" in body
        assert "second-probe" in body

    async def test_group_json_unions_evaluations_as_one_array(self, db_session: AsyncSession) -> None:
        # The JSON group export must be ONE valid array across all evaluations — the `first`/comma
        # flag in stream_export spans evaluation boundaries (json.loads fails on `][` / double comma).
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="grp-json@example.com", permissions=perms)
        first = await _seed_evaluation(db_session, caller)
        group_id = first.evaluation_group_id
        await _add_evaluation_with_message(
            db_session, group_id=group_id, owner=caller, title="E2", content="second-probe"
        )

        rows = await _render_group_json(db_session, caller, perms, group_id, "transcript")

        assert isinstance(rows, list)
        assert len(rows) == 2  # one message row per evaluation, unioned into a single array
        contents = [row["Content"] for row in rows]
        assert "probe" in contents
        assert "second-probe" in contents

    async def test_group_json_empty_scope_renders_empty_array(self, db_session: AsyncSession) -> None:
        # stream_export's JSON branch frames `[` then `]` around the fetch; a group with no
        # evaluations (zero rows) must render as a valid empty array `[]`, not choke on the empty
        # stream. This locks the empty case of the branch (separate impl from json_generator.iter_json).
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="empty-json@example.com", permissions=perms)
        group = await persist_evaluation_group(
            db_session, access_level=EvaluationGroupAccessLevel.PUBLIC, created_by_id=caller.id
        )

        rows = await _render_group_json(db_session, caller, perms, group.id, "engagement_report")

        assert rows == []

    async def test_group_export_batches_the_effective_license_lookup(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # N+1 guard: a license-bearing export (transcript) over a group of N evaluations resolves
        # the effective license once (batched, in `_iter_export_rows`), not once per evaluation —
        # the per-evaluation fetch reads it off the scope, never the single-id resolver.
        perms = [Permission.CONVERSATIONS_READ.value]
        caller = await _caller(db_session, email="lic-batch@example.com", permissions=perms)
        first = await _seed_evaluation(db_session, caller)
        group_id = first.evaluation_group_id
        await _add_evaluation_with_message(
            db_session, group_id=group_id, owner=caller, title="E2", content="second-probe"
        )

        batched = 0
        per_eval = 0

        async def _count_batched(session: AsyncSession, ids: Iterable[UUID]) -> dict[UUID, DataLicense]:
            nonlocal batched
            batched += 1
            return await resolve_effective_licenses(session, ids)

        async def _count_single(session: AsyncSession, evaluation_id: UUID) -> DataLicense:
            nonlocal per_eval
            per_eval += 1
            return await resolve_effective_license(session, evaluation_id)

        # Patch the names as the streamed path resolves them: the batched one in `generation`,
        # the single-id fallback in the `transcript` template.
        monkeypatch.setattr("app.core.exports.generation.resolve_effective_licenses", _count_batched)
        monkeypatch.setattr("app.core.exports.templates.transcript.resolve_effective_license", _count_single)

        body = await _render_group(db_session, caller, perms, group_id, "transcript")

        assert "probe" in body  # first evaluation streamed
        assert "second-probe" in body  # second evaluation streamed
        assert batched == 1  # one batched resolve for the whole group (2 evaluations)
        assert per_eval == 0  # no per-evaluation single-id fallback in the streamed path


class TestAsyncExportJobs:
    """The background export-job endpoints (POST /exports/jobs, status, download)."""

    async def test_create_queues_and_returns_pending(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caller = await _caller(db_session, email="jobber@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        enqueued: list[str] = []
        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=enqueued.append))

        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id)},
            headers=_auth(caller),
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        body = response.json()
        assert body["status"] == "pending"
        assert body["template"] == "flags"
        assert body["format"] == "csv"  # default format surfaced on the response projection
        assert body["evaluation_id"] == str(evaluation.id)
        assert enqueued == [body["id"]]  # enqueued only after the row is committed, with its id
        job = (await db_session.execute(select(ExportJob).where(col(ExportJob.id) == UUID(body["id"])))).scalar_one()
        assert job.requested_by_id == caller.id

    async def test_create_broker_enqueue_failure_rolls_back_job(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the broker enqueue fails after the row was committed, the endpoint must drop the orphan
        # (no phantom `pending` job that never runs) and return a retryable 503 — not a 500 with a
        # perpetually-"Generating…" job the caller never successfully requested.
        caller = await _caller(db_session, email="broker-down@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)

        def _boom(_job_id: str) -> None:
            raise RuntimeError("broker unavailable")

        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=_boom))

        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id)},
            headers=_auth(caller),
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        rows = (
            (await db_session.execute(select(ExportJob).where(col(ExportJob.requested_by_id) == caller.id)))
            .scalars()
            .all()
        )
        assert rows == []  # orphan deleted, so a retry with a fresh key starts clean

    async def test_create_is_idempotent_on_key(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A repeated create with the same idempotency_key (an impatient double-click) replays
        # the queued job: same id, one enqueue, one row — never a duplicate generation.
        caller = await _caller(db_session, email="idem@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        enqueued: list[str] = []
        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=enqueued.append))
        body = {"template": "flags", "evaluation_id": str(evaluation.id), "idempotency_key": str(uuid4())}

        first = await auth_db_client.post("/api/v1/exports/jobs", json=body, headers=_auth(caller))
        second = await auth_db_client.post("/api/v1/exports/jobs", json=body, headers=_auth(caller))

        assert first.status_code == status.HTTP_202_ACCEPTED
        assert second.status_code == status.HTTP_202_ACCEPTED
        assert first.json()["id"] == second.json()["id"]  # replayed, not a new job
        assert enqueued == [first.json()["id"]]  # enqueued exactly once
        rows = (
            (await db_session.execute(select(ExportJob).where(col(ExportJob.requested_by_id) == caller.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1

    async def test_create_idempotency_key_mismatch_is_409(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reusing a key for a *different* request (here: a different template) is a client error —
        # not a silent replay of the first job's resource (Stripe-style idempotency fingerprinting).
        caller = await _caller(db_session, email="idem-mismatch@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=lambda _job_id: None))
        key = str(uuid4())

        first = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id), "idempotency_key": key},
            headers=_auth(caller),
        )
        second = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "conversations", "evaluation_id": str(evaluation.id), "idempotency_key": key},
            headers=_auth(caller),
        )

        assert first.status_code == status.HTTP_202_ACCEPTED
        assert second.status_code == status.HTTP_409_CONFLICT
        assert second.headers["content-type"] == "application/problem+json"

    async def test_create_idempotency_key_format_mismatch_is_409(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same key + same template/scope but a different *format* is still a different request —
        # format is part of the idempotency fingerprint, so it's a 409, not a wrong-format replay.
        caller = await _caller(db_session, email="idem-fmt@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=lambda _job_id: None))
        key = str(uuid4())

        first = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id), "format": "csv", "idempotency_key": key},
            headers=_auth(caller),
        )
        second = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id), "format": "json", "idempotency_key": key},
            headers=_auth(caller),
        )

        assert first.status_code == status.HTTP_202_ACCEPTED
        assert second.status_code == status.HTTP_409_CONFLICT

    async def test_create_idempotency_key_filters_mismatch_is_409(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same key + same template/format/scope but different *filters* is still a different request —
        # filters are part of the idempotency fingerprint, so it's a 409, not a differently-filtered replay.
        caller = await _caller(db_session, email="idem-filters@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=lambda _job_id: None))
        key = str(uuid4())

        first = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={
                "template": "flags",
                "evaluation_id": str(evaluation.id),
                "filters": {"status": "pending"},
                "idempotency_key": key,
            },
            headers=_auth(caller),
        )
        second = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={
                "template": "flags",
                "evaluation_id": str(evaluation.id),
                "filters": {"status": "approved"},
                "idempotency_key": key,
            },
            headers=_auth(caller),
        )

        assert first.status_code == status.HTTP_202_ACCEPTED
        assert second.status_code == status.HTTP_409_CONFLICT

    async def test_create_idempotency_key_same_filters_replays(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same key + same NON-EMPTY filters is a genuine replay → the already-queued job (202, same
        # id), not a 409. Guards the store/compare symmetry (`model_dump(exclude_none=True) or None`)
        # that the mismatch test never exercises as a match.
        caller = await _caller(db_session, email="idem-same@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=lambda _job_id: None))
        body = {
            "template": "flags",
            "evaluation_id": str(evaluation.id),
            "filters": {"status": "pending", "user_id": str(caller.id)},
            "idempotency_key": str(uuid4()),
        }

        first = await auth_db_client.post("/api/v1/exports/jobs", json=body, headers=_auth(caller))
        second = await auth_db_client.post("/api/v1/exports/jobs", json=body, headers=_auth(caller))

        assert first.status_code == status.HTTP_202_ACCEPTED
        assert second.status_code == status.HTTP_202_ACCEPTED
        assert second.json()["id"] == first.json()["id"]

    async def test_create_rejects_unknown_status_filter_422(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # An unknown status filter is a request error (422), not a job that silently exports everything.
        caller = await _caller(db_session, email="badstatus@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)

        resp = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id), "filters": {"status": "bogus"}},
            headers=_auth(caller),
        )

        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_create_rejects_filter_unsupported_by_template_400(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # A filter dimension the chosen template doesn't honor is a 400 at request time — not
        # accepted-then-silently-dropped. `task_id` is flags-only, so the reviews export rejects it.
        caller = await _caller(db_session, email="badfilter@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)

        resp = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "reviews", "evaluation_id": str(evaluation.id), "filters": {"task_id": str(uuid4())}},
            headers=_auth(caller),
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    async def test_create_idempotency_race_lost_is_409(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the concurrent-create race: a colliding live row exists, but both the up-front and
        # winner lookups miss (as if the row landed between them), so the flush hits the
        # partial-unique index and the empty winner re-query surfaces as a 409.
        caller = await _caller(db_session, email="idem-race@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        key = uuid4()
        db_session.add(
            ExportJob(
                template="flags",
                requested_by_id=caller.id,
                evaluation_id=evaluation.id,
                idempotency_key=key,
            )
        )
        await db_session.commit()

        async def _miss(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr("app.core.exports.jobs._find_by_idempotency_key", _miss)

        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id), "idempotency_key": str(key)},
            headers=_auth(caller),
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.headers["content-type"] == "application/problem+json"

    async def test_create_rejects_both_scopes(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="both@example.com", permissions=[Permission.FLAGS_READ.value])
        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(uuid4()), "evaluation_group_id": str(uuid4())},
            headers=_auth(caller),
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_create_rejects_no_scope(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="noscope@example.com", permissions=[Permission.FLAGS_READ.value])
        response = await auth_db_client.post("/api/v1/exports/jobs", json={"template": "flags"}, headers=_auth(caller))
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_create_forbidden_for_non_owner(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # Exports are an owner/admin action: a caller who can SEE the (public) group but is
        # not its in-group owner and lacks the manage break-glass is refused (403, not 404).
        owner = await _caller(db_session, email="grp-owner@example.com", permissions=[])
        evaluation = await _seed_evaluation(db_session, owner)  # public group; owner holds in-group owner
        other = await _caller(db_session, email="not-owner@example.com", permissions=[Permission.FLAGS_READ.value])

        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id)},
            headers=_auth(other),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_create_forbidden_for_in_group_editor(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # The gate keys on `evaluation_groups:export`, not the delegable `evaluation_groups:update`:
        # a custom in-group role handed out to let someone edit a group must not also be able to
        # export every member's transcripts, flags and reviews.
        owner = await _caller(db_session, email="exp-grp-owner@example.com", permissions=[])
        evaluation = await _seed_evaluation(db_session, owner)
        editor = await _caller(db_session, email="exp-editor@example.com", permissions=[Permission.FLAGS_READ.value])
        editor_role = Role(
            name="exp-group-editor", permissions=[Permission.EVALUATION_GROUPS_UPDATE.value], is_system=False
        )
        db_session.add(editor_role)
        await db_session.flush()
        await grant_roles(
            db_session, ObjectType.EVALUATION_GROUP, evaluation.evaluation_group_id, editor.id, [editor_role]
        )
        await db_session.flush()

        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id)},
            headers=_auth(editor),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_create_as_admin_break_glass(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `evaluation_groups:manage` (admin) is the break-glass — an admin exports any group.
        owner = await _caller(db_session, email="og@example.com", permissions=[])
        evaluation = await _seed_evaluation(db_session, owner)
        admin = await _caller(
            db_session, email="exports-admin@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
        )
        monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=lambda _job_id: None))

        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(evaluation.id)},
            headers=_auth(admin),
        )

        assert response.status_code == status.HTTP_202_ACCEPTED

    async def test_create_target_not_visible_is_404(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await _caller(db_session, email="ghost@example.com", permissions=[Permission.FLAGS_READ.value])
        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "flags", "evaluation_id": str(uuid4())},
            headers=_auth(caller),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_create_unknown_template_is_404(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="badtmpl@example.com", permissions=[])
        response = await auth_db_client.post(
            "/api/v1/exports/jobs",
            json={"template": "nope", "evaluation_id": str(uuid4())},
            headers=_auth(caller),
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND  # unknown template checked before authority

    async def test_status_returns_state(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="stat@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags", requested_by_id=caller.id, evaluation_id=evaluation.id, status=ExportJobStatus.READY
        )
        db_session.add(job)
        await db_session.flush()

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "ready"

    async def test_status_hidden_from_other_requester(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        owner = await _caller(db_session, email="owner-job@example.com", permissions=[Permission.FLAGS_READ.value])
        other = await _caller(db_session, email="other-job@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, owner)
        job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id)
        db_session.add(job)
        await db_session.flush()

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}", headers=_auth(other))

        assert response.status_code == status.HTTP_404_NOT_FOUND  # owner-scoped, no existence leak

    async def test_download_streams_ready_csv(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caller = await _caller(db_session, email="dl@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.csv",
        )
        db_session.add(job)
        await db_session.flush()
        monkeypatch.setattr(
            "app.api.v1.exports.get_export_storage", lambda: _FakeStorage(present=True, body=b"Flag ID\nboom\n")
        )

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}/download", headers=_auth(caller))

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("text/csv")
        # Human, dated attachment name from the title slug — not the raw UUID (guards the wiring of
        # `_job_filename` → report_filename, which the extension-only asserts elsewhere wouldn't).
        disposition = response.headers["content-disposition"]
        assert "attachment" in disposition
        assert "e-flags-" in disposition  # <title-slug>-<template>-
        assert str(job.id) not in disposition
        assert "boom" in response.text

    async def test_download_streams_ready_json(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A format=json job serves application/json with a .json attachment — media type + filename
        # extension derive from job.format, not a hardcoded text/csv/.csv.
        caller = await _caller(db_session, email="dl-json@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags",
            format="json",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.json",
        )
        db_session.add(job)
        await db_session.flush()
        monkeypatch.setattr(
            "app.api.v1.exports.get_export_storage", lambda: _FakeStorage(present=True, body=b'[{"Flag ID":"x"}]')
        )

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}/download", headers=_auth(caller))

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("application/json")
        assert ".json" in response.headers["content-disposition"]
        assert '"Flag ID"' in response.text

    async def test_download_corrupt_format_falls_back_to_csv_extension(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A corrupt stored `format` renders as CSV (parse_stored_format fallback); the attachment
        # extension must follow — not echo the junk — so filename, media type and bytes agree.
        caller = await _caller(db_session, email="dl-junk@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags",
            format="junk",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.csv",
        )
        db_session.add(job)
        await db_session.flush()
        monkeypatch.setattr(
            "app.api.v1.exports.get_export_storage", lambda: _FakeStorage(present=True, body=b"Flag ID\nboom\n")
        )

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}/download", headers=_auth(caller))

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("text/csv")
        disposition = response.headers["content-disposition"]
        assert ".csv" in disposition
        assert ".junk" not in disposition

    async def test_download_expired_is_404(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TTL is enforced on the read path, not just by the periodic reaper: a ready-but-expired
        # job 404s immediately even while its file still physically exists.
        caller = await _caller(db_session, email="expired-dl@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="stale.csv",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db_session.add(job)
        await db_session.flush()
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: _FakeStorage(present=True))

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}/download", headers=_auth(caller))

        assert response.status_code == status.HTTP_404_NOT_FOUND  # expired → not served even though the file exists

    async def test_download_forbidden_after_authority_revoked(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Authority is re-checked live on download (mirrors the worker): a requester who was the
        # group's owner at generation but has since lost the role can no longer download the file,
        # even inside the TTL window. The group stays public (still visible), so it's 403 not 404.
        caller = await _caller(db_session, email="revoked-dl@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)  # grants caller the in-group owner role
        job = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.csv",
        )
        db_session.add(job)
        await db_session.flush()
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: _FakeStorage(present=True))
        # Revoke the owner role — the create-time authority no longer holds. Soft-delete the
        # assignment directly (remove_member guards the last owner; here we're simulating a revoke).
        assignments = (
            (
                await db_session.execute(
                    ObjectRoleAssignment.live_select().where(
                        col(ObjectRoleAssignment.object_type) == ObjectType.EVALUATION_GROUP,
                        col(ObjectRoleAssignment.object_id) == evaluation.evaluation_group_id,
                        col(ObjectRoleAssignment.user_id) == caller.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for assignment in assignments:
            assignment.soft_delete(None)
        await db_session.flush()

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}/download", headers=_auth(caller))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_download_not_ready_is_409(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="pending-dl@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(template="flags", requested_by_id=caller.id, evaluation_id=evaluation.id)
        db_session.add(job)
        await db_session.flush()

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}/download", headers=_auth(caller))

        assert response.status_code == status.HTTP_409_CONFLICT

    async def test_download_missing_file_is_404(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caller = await _caller(db_session, email="gone-dl@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="expired.csv",
        )
        db_session.add(job)
        await db_session.flush()
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: _FakeStorage(present=False))

        response = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}/download", headers=_auth(caller))

        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestDeleteExportJob:
    async def test_delete_removes_file_and_soft_deletes_row(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caller = await _caller(db_session, email="del@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.csv",
        )
        db_session.add(job)
        await db_session.flush()
        storage = _FakeStorage(present=True)
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: storage)

        response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert "ready.csv" in storage.deleted  # file physically removed
        # Soft-deleted: no longer a live job on the owner-scoped read.
        follow = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))
        assert follow.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_failed_job_without_file_still_soft_deletes(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failed job is terminal but carries no file_ref — no storage delete is attempted, still 204.
        caller = await _caller(db_session, email="del-failed@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags", requested_by_id=caller.id, evaluation_id=evaluation.id, status=ExportJobStatus.FAILED
        )
        db_session.add(job)
        await db_session.flush()
        storage = _FakeStorage(present=True)
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: storage)

        response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert storage.deleted == []  # nothing to remove
        # The row is still soft-deleted despite the no-file branch skipping storage.delete.
        follow = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))
        assert follow.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_running_job_is_409(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # A still-generating job is not deletable — deleting it would race the worker finalize and
        # orphan the file the reaper (live-rows only) could never reclaim.
        caller = await _caller(db_session, email="del-running@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags", requested_by_id=caller.id, evaluation_id=evaluation.id, status=ExportJobStatus.RUNNING
        )
        db_session.add(job)
        await db_session.flush()

        response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.headers["content-type"] == PROBLEM_CT
        body = response.json()
        assert body["status"] == status.HTTP_409_CONFLICT
        assert "running" in body["detail"]
        # Still live and readable — the delete was rejected, not partially applied.
        follow = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))
        assert follow.status_code == status.HTTP_200_OK

    async def test_delete_soft_deletes_row_even_if_file_removal_fails(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Best-effort file delete: a storage failure is swallowed so the row is still soft-deleted —
        # a lingering file can never keep the job "live" (and the reaper's sweep can't be stalled).
        caller = await _caller(
            db_session, email="del-badstorage@example.com", permissions=[Permission.FLAGS_READ.value]
        )
        evaluation = await _seed_evaluation(db_session, caller)
        job = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.csv",
        )
        db_session.add(job)
        await db_session.flush()
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: _FakeStorage(raise_on_delete=True))

        response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))

        assert response.status_code == status.HTTP_204_NO_CONTENT  # storage error swallowed
        follow = await auth_db_client.get(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))
        assert follow.status_code == status.HTTP_404_NOT_FOUND  # row soft-deleted regardless

    async def test_delete_hidden_from_other_requester(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        owner = await _caller(db_session, email="del-owner@example.com", permissions=[Permission.FLAGS_READ.value])
        other = await _caller(db_session, email="del-other@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, owner)
        job = ExportJob(template="flags", requested_by_id=owner.id, evaluation_id=evaluation.id)
        db_session.add(job)
        await db_session.flush()

        response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job.id}", headers=_auth(other))

        assert response.status_code == status.HTTP_404_NOT_FOUND  # owner-scoped, no existence leak

    async def test_delete_by_manage_admin_removes_another_users_export(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The evaluation_groups:manage break-glass lifts owner-scope so an admin can clean up an
        # export a departed red-teamer left behind (housekeeping — delete only, not download).
        owner = await _caller(db_session, email="del-departed@example.com", permissions=[Permission.FLAGS_READ.value])
        admin = await _caller(
            db_session, email="del-admin@example.com", permissions=[Permission.EVALUATION_GROUPS_MANAGE.value]
        )
        evaluation = await _seed_evaluation(db_session, owner)
        job = ExportJob(
            template="flags",
            requested_by_id=owner.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.csv",
        )
        db_session.add(job)
        await db_session.flush()
        storage = _FakeStorage(present=True)
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: storage)

        response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job.id}", headers=_auth(admin))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert "ready.csv" in storage.deleted  # break-glass reached another requester's export

    async def test_delete_allowed_for_own_job_after_authority_revoked(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unlike download, delete is owner-scoped only and does NOT re-check live group authority:
        # a requester can always clear their OWN export artifact, even after losing the owner role
        # (or when the target group is gone — the common cause of a failed export).
        caller = await _caller(db_session, email="del-revoked@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)  # grants caller the in-group owner role
        job = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_id=evaluation.id,
            status=ExportJobStatus.READY,
            file_ref="ready.csv",
        )
        db_session.add(job)
        await db_session.flush()
        storage = _FakeStorage(present=True)
        monkeypatch.setattr("app.api.v1.exports.get_export_storage", lambda: storage)
        assignments = (
            (
                await db_session.execute(
                    ObjectRoleAssignment.live_select().where(
                        col(ObjectRoleAssignment.object_type) == ObjectType.EVALUATION_GROUP,
                        col(ObjectRoleAssignment.object_id) == evaluation.evaluation_group_id,
                        col(ObjectRoleAssignment.user_id) == caller.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for assignment in assignments:
            assignment.soft_delete(None)
        await db_session.flush()

        response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job.id}", headers=_auth(caller))

        assert response.status_code == status.HTTP_204_NO_CONTENT  # own artifact, still deletable
        assert "ready.csv" in storage.deleted


class TestListExportJobs:
    """GET /exports/jobs — the caller's own jobs for one scope (backs the detail download lists)."""

    async def test_lists_group_jobs_newest_first(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="listjobs@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        group_id = evaluation.evaluation_group_id
        older = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_group_id=group_id,
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        newer = ExportJob(
            template="conversations",
            requested_by_id=caller.id,
            evaluation_group_id=group_id,
            created_at=datetime.now(UTC),
        )
        db_session.add_all([older, newer])
        await db_session.flush()

        response = await auth_db_client.get(
            "/api/v1/exports/jobs", params={"evaluation_group_id": str(group_id)}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_200_OK
        ids = [item["id"] for item in response.json()["items"]]
        assert ids == [str(newer.id), str(older.id)]  # newest first

    async def test_scoped_to_requester(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # Owner-scoped like the single-job GET: a different caller sees none of the owner's jobs.
        owner = await _caller(db_session, email="jobs-owner@example.com", permissions=[Permission.FLAGS_READ.value])
        other = await _caller(db_session, email="jobs-other@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, owner)
        group_id = evaluation.evaluation_group_id
        db_session.add(ExportJob(template="flags", requested_by_id=owner.id, evaluation_group_id=group_id))
        await db_session.flush()

        response = await auth_db_client.get(
            "/api/v1/exports/jobs", params={"evaluation_group_id": str(group_id)}, headers=_auth(other)
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["items"] == []

    async def test_filters_by_evaluation_scope(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # A group-scoped job and an evaluation-scoped job coexist; ?evaluation_id returns only the latter.
        caller = await _caller(
            db_session, email="jobs-evalscope@example.com", permissions=[Permission.FLAGS_READ.value]
        )
        evaluation = await _seed_evaluation(db_session, caller)
        eval_job = ExportJob(template="flags", requested_by_id=caller.id, evaluation_id=evaluation.id)
        group_job = ExportJob(
            template="flags", requested_by_id=caller.id, evaluation_group_id=evaluation.evaluation_group_id
        )
        db_session.add_all([eval_job, group_job])
        await db_session.flush()

        response = await auth_db_client.get(
            "/api/v1/exports/jobs", params={"evaluation_id": str(evaluation.id)}, headers=_auth(caller)
        )

        ids = [item["id"] for item in response.json()["items"]]
        assert ids == [str(eval_job.id)]  # the group-scoped job is a different scope, excluded

    async def test_rejects_both_scopes(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="jobs-both@example.com", permissions=[Permission.FLAGS_READ.value])

        response = await auth_db_client.get(
            "/api/v1/exports/jobs",
            params={"evaluation_id": str(uuid4()), "evaluation_group_id": str(uuid4())},
            headers=_auth(caller),
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.headers["content-type"] == PROBLEM_CT

    async def test_rejects_no_scope(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="jobs-noscope@example.com", permissions=[Permission.FLAGS_READ.value])

        response = await auth_db_client.get("/api/v1/exports/jobs", headers=_auth(caller))

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    async def test_excludes_ttl_expired_jobs(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # A ready-but-expired job (TTL lapsed, reaper hasn't swept) would 404 on download,
        # so the list must not surface it — only the live job is returned.
        caller = await _caller(db_session, email="jobs-ttl@example.com", permissions=[Permission.FLAGS_READ.value])
        evaluation = await _seed_evaluation(db_session, caller)
        group_id = evaluation.evaluation_group_id
        live = ExportJob(
            template="flags", requested_by_id=caller.id, evaluation_group_id=group_id, status=ExportJobStatus.READY
        )
        expired = ExportJob(
            template="flags",
            requested_by_id=caller.id,
            evaluation_group_id=group_id,
            status=ExportJobStatus.READY,
            file_ref="stale.csv",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db_session.add_all([live, expired])
        await db_session.flush()

        response = await auth_db_client.get(
            "/api/v1/exports/jobs", params={"evaluation_group_id": str(group_id)}, headers=_auth(caller)
        )

        ids = [item["id"] for item in response.json()["items"]]
        assert ids == [str(live.id)]  # the expired job is excluded
