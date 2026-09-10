"""Integration tests for the task-completion router.

Routes hang off the conversation (`PUT/DELETE/GET
/conversations/{id}/completed-tasks[/{task_id}]`) and the group roll-up
(`GET /conversation-groups/{id}/completed-tasks`). Completions are owner-scoped,
mutations gate on `conversations:update`, reads on `conversations:read`.
"""

from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.annotations.models import TaskCompletion
from app.core.annotations.services import task_completions as tc_service
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.users import create_user as create_user_service
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from tests.api.v1.conftest import make_role
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


_CONV_PERMS = [Permission.CONVERSATIONS_READ.value, Permission.CONVERSATIONS_UPDATE.value]


@pytest_asyncio.fixture
async def redteamer_role(db_session: AsyncSession) -> Role:
    """Read + update on conversations — the owner-scoped check-off surface (no break-glass)."""
    role = Role(name="rt", description="conversations rw", permissions=list(_CONV_PERMS))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    """Read-only conversations permission — can list/roll up but not toggle."""
    role = Role(name="rt-reader", description="read-only", permissions=[Permission.CONVERSATIONS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def manager_role(db_session: AsyncSession) -> Role:
    """Conversations rw plus the `evaluation_groups:manage` break-glass that lifts the owner scope on reads."""
    role = Role(
        name="rt-manager",
        description="conversations rw + manage",
        permissions=[*_CONV_PERMS, Permission.EVALUATION_GROUPS_MANAGE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def no_perms_role(db_session: AsyncSession) -> Role:
    """No conversation permissions — proves the read endpoints gate on `conversations:read` (403)."""
    role = Role(name="rt-none", description="no conversation perms", permissions=[])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _evaluation_for(
    db_session: AsyncSession,
    *,
    owner_id: UUID | None = None,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
) -> Evaluation:
    group = await persist_evaluation_group(db_session, access_level=access_level, created_by_id=owner_id)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _assignment(db_session: AsyncSession, evaluation_id: UUID) -> EvaluationAiModel:
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation_id, model_id=model.id)
    db_session.add(assignment)
    await db_session.flush()
    await db_session.refresh(assignment)
    return assignment


async def _scenario(db_session: AsyncSession, evaluation_id: UUID) -> Scenario:
    row = Scenario(name="S", description="d", evaluation_id=evaluation_id, position=0)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _task(db_session: AsyncSession, scenario_id: UUID) -> Task:
    row = Task(name="T", description="d", scenario_id=scenario_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _group_with_conversations(
    db_session: AsyncSession,
    *,
    user_id: UUID,
    evaluation: Evaluation,
    scenario_id: UUID,
    count: int,
) -> tuple[ConversationGroup, list[Conversation]]:
    group = ConversationGroup(user_id=user_id, evaluation_id=evaluation.id, name="g", scenario_id=scenario_id)
    db_session.add(group)
    await db_session.flush()
    conversations: list[Conversation] = []
    for _ in range(count):
        assignment = await _assignment(db_session, evaluation.id)
        conversation = Conversation(
            user_id=user_id,
            evaluation_id=evaluation.id,
            evaluation_ai_model_id=assignment.id,
            scenario_id=scenario_id,
            conversation_group_id=group.id,
        )
        db_session.add(conversation)
        await db_session.flush()
        await db_session.refresh(conversation)
        conversations.append(conversation)
    return group, conversations


async def _conversation_with_task(
    db_session: AsyncSession, *, user_id: UUID, evaluation: Evaluation
) -> tuple[Conversation, Task]:
    scenario = await _scenario(db_session, evaluation.id)
    task = await _task(db_session, scenario.id)
    _, (conversation,) = await _group_with_conversations(
        db_session, user_id=user_id, evaluation=evaluation, scenario_id=scenario.id, count=1
    )
    return conversation, task


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


def _completed_url(conversation_id: UUID, task_id: UUID) -> str:
    return f"/api/v1/conversations/{conversation_id}/completed-tasks/{task_id}"


def _list_url(conversation_id: UUID) -> str:
    return f"/api/v1/conversations/{conversation_id}/completed-tasks"


def _rollup_url(group_id: UUID) -> str:
    return f"/api/v1/conversation-groups/{group_id}/completed-tasks"


async def _live_completion_count(db_session: AsyncSession, conversation_id: UUID, task_id: UUID) -> int:
    """Raw count of live completions for a pair, bypassing the ORM live-row filter."""
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(TaskCompletion).where(
                    col(TaskCompletion.conversation_id) == conversation_id,
                    col(TaskCompletion.task_id) == task_id,
                    col(TaskCompletion.deleted_at).is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


async def test_complete_marks_task_and_denormalises_ancestry(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="mark@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)

    response = await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["conversation_id"] == str(conversation.id)
    assert body["task_id"] == str(task.id)
    assert body["conversation_group_id"] == str(conversation.conversation_group_id)
    assert body["evaluation_id"] == str(evaluation.id)
    assert body["evaluation_group_id"] == str(evaluation.evaluation_group_id)
    assert body["scenario_id"] == str(conversation.scenario_id)
    assert body["created_by_id"] == str(caller.id)
    assert body["completed_at"]


async def test_complete_is_idempotent(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="idem@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)
    token = _token(caller)

    first = await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(token))
    second = await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(token))

    assert first.status_code == status.HTTP_200_OK
    assert second.status_code == status.HTTP_200_OK
    assert first.json()["id"] == second.json()["id"]  # same row, not a duplicate
    assert await _live_completion_count(db_session, conversation.id, task.id) == 1


async def test_list_reflects_completion(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="list@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)
    token = _token(caller)

    before = await auth_db_client.get(_list_url(conversation.id), headers=_auth(token))
    await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(token))
    after = await auth_db_client.get(_list_url(conversation.id), headers=_auth(token))

    assert before.json() == []
    assert [item["task_id"] for item in after.json()] == [str(task.id)]


async def test_uncomplete_soft_deletes_and_recheck_makes_new_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="toggle@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)
    token = _token(caller)

    first = await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(token))
    deleted = await auth_db_client.delete(_completed_url(conversation.id, task.id), headers=_auth(token))
    after_delete = await auth_db_client.get(_list_url(conversation.id), headers=_auth(token))
    recheck = await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(token))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert after_delete.json() == []  # soft-deleted, hidden from the live list
    assert recheck.status_code == status.HTTP_200_OK
    assert recheck.json()["id"] != first.json()["id"]  # a fresh row (freed pair), new completion event
    assert await _live_completion_count(db_session, conversation.id, task.id) == 1


async def test_uncomplete_when_not_completed_is_noop(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="noop@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)

    response = await auth_db_client.delete(_completed_url(conversation.id, task.id), headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT


async def test_complete_task_from_other_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="xscenario@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, _own_task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)
    other_scenario = await _scenario(db_session, evaluation.id)
    foreign_task = await _task(db_session, other_scenario.id)

    response = await auth_db_client.put(_completed_url(conversation.id, foreign_task.id), headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_complete_on_other_users_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    owner = await _caller(db_session, redteamer_role, email="cowner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=owner.id, evaluation=evaluation)
    intruder = await _caller(db_session, redteamer_role, email="cnosy@example.com")

    response = await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(_token(intruder)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_complete_unknown_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="ghost@example.com")

    response = await auth_db_client.put(_completed_url(uuid4(), uuid4()), headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_soft_deleting_conversation_hides_completion(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    # Parent-liveness via the shared join: soft-deleting the conversation hides its
    # completions with no dedicated cascade hook on the completion itself.
    caller = await _caller(db_session, redteamer_role, email="parent@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)
    await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(token))

    conversation.soft_delete(None)
    db_session.add(conversation)
    await db_session.flush()

    listed = await auth_db_client.get(_list_url(conversation.id), headers=_auth(token))

    assert listed.json() == []  # hidden by the live-parent join


async def test_group_rollup_counts_conversations_with_completion(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="rollup@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    task = await _task(db_session, scenario.id)
    group, (conv_a, conv_b) = await _group_with_conversations(
        db_session, user_id=caller.id, evaluation=evaluation, scenario_id=scenario.id, count=2
    )

    await auth_db_client.put(_completed_url(conv_a.id, task.id), headers=_auth(token))
    one_done = await auth_db_client.get(_rollup_url(group.id), headers=_auth(token))
    await auth_db_client.put(_completed_url(conv_b.id, task.id), headers=_auth(token))
    both_done = await auth_db_client.get(_rollup_url(group.id), headers=_auth(token))

    assert one_done.json()["total_conversations"] == 2
    assert one_done.json()["completed_counts"] == [{"task_id": str(task.id), "completed_count": 1}]
    assert both_done.json()["total_conversations"] == 2
    assert both_done.json()["completed_counts"] == [{"task_id": str(task.id), "completed_count": 2}]


async def test_group_rollup_omits_untouched_tasks(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    caller = await _caller(db_session, redteamer_role, email="empty-rollup@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    await _task(db_session, scenario.id)
    group, _convs = await _group_with_conversations(
        db_session, user_id=caller.id, evaluation=evaluation, scenario_id=scenario.id, count=1
    )

    response = await auth_db_client.get(_rollup_url(group.id), headers=_auth(_token(caller)))

    assert response.json() == {"total_conversations": 1, "completed_counts": []}


async def test_group_rollup_follows_conversation_after_move(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    # A completion denormalises conversation_group_id at create; a conversation can
    # be moved to another group of the same evaluation. The roll-up must scope by the
    # conversation's CURRENT group (live), not that stale snapshot — else the moved
    # conversation counts under its old group and vanishes from the new one.
    caller = await _caller(db_session, redteamer_role, email="move@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    task = await _task(db_session, scenario.id)
    g1, (conversation,) = await _group_with_conversations(
        db_session, user_id=caller.id, evaluation=evaluation, scenario_id=scenario.id, count=1
    )
    g2, _ = await _group_with_conversations(
        db_session, user_id=caller.id, evaluation=evaluation, scenario_id=scenario.id, count=0
    )
    await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(token))

    conversation.conversation_group_id = g2.id  # simulate the move endpoint
    db_session.add(conversation)
    await db_session.flush()

    r_g1 = await auth_db_client.get(_rollup_url(g1.id), headers=_auth(token))
    r_g2 = await auth_db_client.get(_rollup_url(g2.id), headers=_auth(token))

    # Old group: no longer holds the conversation, so no completion counts there.
    assert r_g1.json() == {"total_conversations": 0, "completed_counts": []}
    # New group: holds the conversation and its completion follows it.
    assert r_g2.json() == {
        "total_conversations": 1,
        "completed_counts": [{"task_id": str(task.id), "completed_count": 1}],
    }


async def test_manager_cannot_toggle_other_users_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role, manager_role: Role
) -> None:
    # Authoring is owner-only: the evaluation_groups:manage break-glass lifts the
    # owner scope on reads (test_manager_reads_other_users_completions) but NOT on
    # mutations — a manager can view but never check off in another user's conversation.
    owner = await _caller(db_session, redteamer_role, email="mt-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=owner.id, evaluation=evaluation)
    manager = await _caller(db_session, manager_role, email="mt-admin@example.com")
    manager_auth = _auth(_token(manager))

    put = await auth_db_client.put(_completed_url(conversation.id, task.id), headers=manager_auth)
    delete = await auth_db_client.delete(_completed_url(conversation.id, task.id), headers=manager_auth)

    assert put.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(put, status.HTTP_404_NOT_FOUND)
    assert delete.status_code == status.HTTP_404_NOT_FOUND


async def test_manager_reads_other_users_completions(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, redteamer_role, email="m-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=owner.id, evaluation=evaluation)
    await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(_token(owner)))
    manager = await _caller(db_session, manager_role, email="m-admin@example.com")

    response = await auth_db_client.get(_list_url(conversation.id), headers=_auth(_token(manager)))

    assert response.status_code == status.HTTP_200_OK
    assert [item["task_id"] for item in response.json()] == [str(task.id)]


async def test_plain_caller_does_not_read_other_users_completions(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role
) -> None:
    # The other direction of the break-glass above: without `evaluation_groups:manage`
    # the author predicate applies, so a second red-teamer in the same visible group
    # sees an empty list rather than the owner's check-offs.
    owner = await _caller(db_session, redteamer_role, email="p-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=owner.id, evaluation=evaluation)
    await auth_db_client.put(_completed_url(conversation.id, task.id), headers=_auth(_token(owner)))
    intruder = await _caller(db_session, redteamer_role, email="p-nosy@example.com")

    response = await auth_db_client.get(_list_url(conversation.id), headers=_auth(_token(intruder)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []


async def test_manager_reads_other_users_group_rollup(
    auth_db_client: AsyncClient, db_session: AsyncSession, redteamer_role: Role, manager_role: Role
) -> None:
    # Break-glass parity with the list endpoint: `evaluation_groups:manage` lifts the owner
    # scope on the roll-up too, so a manager counts another owner's conversations into K/N.
    owner = await _caller(db_session, redteamer_role, email="mr-owner@example.com")
    token = _token(owner)
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    task = await _task(db_session, scenario.id)
    group, (conv_a, _conv_b) = await _group_with_conversations(
        db_session, user_id=owner.id, evaluation=evaluation, scenario_id=scenario.id, count=2
    )
    await auth_db_client.put(_completed_url(conv_a.id, task.id), headers=_auth(token))
    manager = await _caller(db_session, manager_role, email="mr-admin@example.com")

    response = await auth_db_client.get(_rollup_url(group.id), headers=_auth(_token(manager)))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total_conversations"] == 2
    assert body["completed_counts"] == [{"task_id": str(task.id), "completed_count": 1}]


async def test_complete_resolves_insert_race_to_winning_row(
    db_session: AsyncSession, redteamer_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concurrent-insert branch: a racing insert already holds the unique
    ``(conversation_id, task_id)`` row, so our savepoint insert trips ``IntegrityError``
    and we re-read and return the winner — idempotent success, still one live row.
    """
    caller = await _caller(db_session, redteamer_role, email="race@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation, task = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)

    # The winner of the race: a live completion already committed, so the partial-unique
    # index is populated and a second insert for the pair must fail.
    winner = TaskCompletion(
        created_by_id=caller.id,
        conversation_id=conversation.id,
        task_id=task.id,
        conversation_group_id=conversation.conversation_group_id,
        evaluation_id=conversation.evaluation_id,
        evaluation_group_id=evaluation.evaluation_group_id,
        scenario_id=conversation.scenario_id,
    )
    db_session.add(winner)
    await db_session.flush()
    await db_session.refresh(winner)

    # Force the race window: the pre-insert owner-check reports "none" (as it would for
    # the loser), so complete_task proceeds to insert and trips the index; the post-error
    # re-read runs for real and finds the winner.
    real = tc_service._get_owned_live_completion
    calls = {"n": 0}

    async def fake_get(
        session_: AsyncSession, conversation_id: UUID, task_id: UUID, caller_id: UUID
    ) -> TaskCompletion | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real(session_, conversation_id, task_id, caller_id)

    monkeypatch.setattr(tc_service, "_get_owned_live_completion", fake_get)

    result = await tc_service.complete_task(
        db_session, conversation_id=conversation.id, task_id=task.id, caller_id=caller.id
    )

    assert result.id == winner.id
    assert calls["n"] == 2  # pre-insert check + post-IntegrityError re-read
    assert await _live_completion_count(db_session, conversation.id, task.id) == 1


async def test_in_group_authority_lists_and_rolls_up_without_the_global_permission(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
) -> None:
    """Both read routes accept the permission from a role held on the parent group.

    The checklist and its group roll-up fire on every conversation page, so a JWT-only gate
    answered a group-scoped red teamer with a 403 toast on a page they may otherwise use.
    """
    caller = await _caller(db_session, await make_role(db_session, []), email="tc-in-group@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await grant_roles(
        db_session,
        ObjectType.EVALUATION_GROUP,
        evaluation.evaluation_group_id,
        caller.id,
        [system_roles[SystemRole.RED_TEAMER.value]],
    )
    await db_session.flush()
    conversation, _ = await _conversation_with_task(db_session, user_id=caller.id, evaluation=evaluation)

    listed = await auth_db_client.get(_list_url(conversation.id), headers=_auth(_token(caller)))
    rolled_up = await auth_db_client.get(_rollup_url(conversation.conversation_group_id), headers=_auth(_token(caller)))

    assert listed.status_code == status.HTTP_200_OK
    assert rolled_up.status_code == status.HTTP_200_OK
