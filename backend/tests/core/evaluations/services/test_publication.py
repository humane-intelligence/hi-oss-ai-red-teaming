"""Integration tests for evaluation-group publication transitions."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.evaluations.services.publication import approve_evaluation_group
from app.core.evaluations.services.publication import finish_evaluation_group
from app.core.evaluations.services.publication import publish_evaluation_group
from app.core.evaluations.services.publication import reject_evaluation_group
from app.core.evaluations.services.publication import request_changes_for_evaluation_group
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.filters import NotificationFilters
from app.core.notifications.services.notifications import list_notifications
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration

_ALL_STATES = list(PublicationStatus)


async def _persist_user(db_session: AsyncSession) -> User:
    user = User(email=f"{uuid4().hex[:8]}@example.com")
    db_session.add(user)
    await db_session.flush()
    return user


async def _persist_evaluation(db_session: AsyncSession, group: EvaluationGroup, *, title: str = "eval") -> Evaluation:
    evaluation = Evaluation(
        title=title, description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    return evaluation


async def _persist_scenario(db_session: AsyncSession, evaluation: Evaluation) -> Scenario:
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    return scenario


async def _persist_publishable_group(
    db_session: AsyncSession,
    *,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
) -> EvaluationGroup:
    """An `approved` group holding one playable evaluation — the publish gate's happy path."""
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED, access_level=access_level)
    await _persist_scenario(db_session, await _persist_evaluation(db_session, group))
    return group


async def test_publish_moves_approved_to_published(db_session: AsyncSession) -> None:
    group = await _persist_publishable_group(db_session)

    result, previous_status = await publish_evaluation_group(
        db_session, group.id, caller_id=group.created_by_id, can_manage=False
    )

    assert result.status is PublicationStatus.PUBLISHED
    assert previous_status is PublicationStatus.APPROVED


async def test_publish_blocked_when_group_has_no_evaluations(db_session: AsyncSession) -> None:
    # Enforced at publish, not submit: evaluations may only be added to an already
    # `approved` group, so a draft could never satisfy this rule.
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)

    with pytest.raises(BadRequestError, match="at least one evaluation"):
        await publish_evaluation_group(db_session, group.id, caller_id=group.created_by_id, can_manage=False)


async def test_publish_blocked_when_evaluation_lacks_scenario(db_session: AsyncSession) -> None:
    # The submit-time gate re-checked at publish: scenarios are soft-deletable, so
    # an evaluation may have lost its last one while the group sat in review.
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)
    covered = await _persist_evaluation(db_session, group, title="covered")
    await _persist_scenario(db_session, covered)
    await _persist_evaluation(db_session, group, title="bare")
    tombstoned_only = await _persist_evaluation(db_session, group, title="tombstoned-only")
    dead_scenario = await _persist_scenario(db_session, tombstoned_only)
    dead_scenario.soft_delete(None)
    await db_session.flush()

    with pytest.raises(BadRequestError) as excinfo:
        await publish_evaluation_group(db_session, group.id, caller_id=group.created_by_id, can_manage=False)

    assert "bare" in str(excinfo.value)
    assert "tombstoned-only" in str(excinfo.value)
    assert "covered" not in str(excinfo.value)


async def test_publish_ignores_tombstoned_evaluation_without_scenario(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)
    covered = await _persist_evaluation(db_session, group, title="covered")
    await _persist_scenario(db_session, covered)
    dead = await _persist_evaluation(db_session, group, title="dead")
    dead.soft_delete(None)
    await db_session.flush()

    result, _ = await publish_evaluation_group(db_session, group.id, caller_id=group.created_by_id, can_manage=False)

    assert result.status is PublicationStatus.PUBLISHED


async def test_finish_moves_published_to_inactive(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PUBLISHED)

    result, previous_status = await finish_evaluation_group(
        db_session, group.id, caller_id=group.created_by_id, can_manage=False
    )

    assert result.status is PublicationStatus.INACTIVE
    assert previous_status is PublicationStatus.PUBLISHED


async def test_reject_moves_pending_approval_to_not_approved_with_reason(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    caller = await _persist_user(db_session)

    result, previous_status = await reject_evaluation_group(
        db_session, group.id, caller_id=caller.id, rejection_reason="off-scope"
    )

    assert result.status is PublicationStatus.NOT_APPROVED
    assert result.rejection_reason == "off-scope"
    assert previous_status is PublicationStatus.PENDING_APPROVAL


@pytest.mark.parametrize("status", [s for s in _ALL_STATES if s is not PublicationStatus.APPROVED])
async def test_publish_rejects_wrong_source_state(db_session: AsyncSession, status: PublicationStatus) -> None:
    group = await persist_evaluation_group(db_session, status=status)

    with pytest.raises(ConflictError):
        await publish_evaluation_group(db_session, group.id, caller_id=group.created_by_id, can_manage=False)


@pytest.mark.parametrize("status", [s for s in _ALL_STATES if s is not PublicationStatus.PUBLISHED])
async def test_finish_rejects_wrong_source_state(db_session: AsyncSession, status: PublicationStatus) -> None:
    group = await persist_evaluation_group(db_session, status=status)

    with pytest.raises(ConflictError):
        await finish_evaluation_group(db_session, group.id, caller_id=group.created_by_id, can_manage=False)


@pytest.mark.parametrize("status", [s for s in _ALL_STATES if s is not PublicationStatus.PENDING_APPROVAL])
async def test_reject_rejects_wrong_source_state(db_session: AsyncSession, status: PublicationStatus) -> None:
    group = await persist_evaluation_group(db_session, status=status)

    with pytest.raises(ConflictError):
        await reject_evaluation_group(db_session, group.id, caller_id=uuid4(), rejection_reason="x")


async def test_publish_forbidden_for_non_owner_of_public_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)
    caller = await _persist_user(db_session)

    with pytest.raises(ForbiddenError):
        await publish_evaluation_group(db_session, group.id, caller_id=caller.id, can_manage=False)


async def test_finish_forbidden_for_non_owner_of_public_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PUBLISHED)
    caller = await _persist_user(db_session)

    with pytest.raises(ForbiddenError):
        await finish_evaluation_group(db_session, group.id, caller_id=caller.id, can_manage=False)


async def test_publish_hides_invisible_private_group_as_missing(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(
        db_session,
        status=PublicationStatus.APPROVED,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
    )
    caller = await _persist_user(db_session)

    with pytest.raises(NotFoundError):
        await publish_evaluation_group(db_session, group.id, caller_id=caller.id, can_manage=False)


async def test_manage_publishes_someone_elses_private_group(db_session: AsyncSession) -> None:
    group = await _persist_publishable_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    caller = await _persist_user(db_session)

    result, _previous_status = await publish_evaluation_group(
        db_session, group.id, caller_id=caller.id, can_manage=True
    )

    assert result.status is PublicationStatus.PUBLISHED


async def test_publish_unknown_group_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await publish_evaluation_group(db_session, uuid4(), caller_id=uuid4(), can_manage=False)


async def test_finish_unknown_group_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await finish_evaluation_group(db_session, uuid4(), caller_id=uuid4(), can_manage=False)


async def test_reject_unknown_group_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await reject_evaluation_group(db_session, uuid4(), caller_id=uuid4(), rejection_reason="x")


async def _owner_notifications(db_session: AsyncSession, group: EvaluationGroup) -> list:
    items, _ = await list_notifications(
        db_session,
        user_id=group.created_by_id,
        filters=NotificationFilters(),
        order_by="-created_at",
        limit=20,
        offset=0,
    )
    return items


async def test_approve_by_manager_notifies_owner(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    manager = await _persist_user(db_session)

    await approve_evaluation_group(db_session, group.id, caller_id=manager.id, can_manage=True)

    (note,) = await _owner_notifications(db_session, group)
    assert note.name == "Evaluation group approved"
    assert note.object_type == NotificationObjectType.EVALUATION_GROUP
    assert note.object_id == group.id
    assert note.read_at is None


async def test_approve_by_owner_notifies_nobody(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)

    await approve_evaluation_group(db_session, group.id, caller_id=group.created_by_id, can_manage=False)

    assert await _owner_notifications(db_session, group) == []


async def test_request_changes_notifies_owner(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    manager = await _persist_user(db_session)

    await request_changes_for_evaluation_group(db_session, group.id, caller_id=manager.id)

    (note,) = await _owner_notifications(db_session, group)
    assert note.name == "Evaluation group changes requested"
    assert note.object_type == NotificationObjectType.EVALUATION_GROUP
    assert note.object_id == group.id


async def test_reject_notifies_owner_with_reason(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    manager = await _persist_user(db_session)

    await reject_evaluation_group(db_session, group.id, caller_id=manager.id, rejection_reason="off-scope")

    (note,) = await _owner_notifications(db_session, group)
    assert note.name == "Evaluation group rejected"
    assert note.object_type == NotificationObjectType.EVALUATION_GROUP
    assert "off-scope" in note.description


async def test_verdict_by_owner_notifies_nobody(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)

    await reject_evaluation_group(db_session, group.id, caller_id=group.created_by_id, rejection_reason="mine")

    assert await _owner_notifications(db_session, group) == []
