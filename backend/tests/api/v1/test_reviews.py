"""Integration tests for the review router — flat `/api/v1/reviews` + `/review-queue`."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy import delete
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
from app.core.auth.models import UserRole
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.users import create_user as create_user_service
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import TAG_CONTEXT_EXTRA_KEY
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.email.models import OutboundEmail
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.organizations.models import Organization
from app.core.reviews.models import Review
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


_REVIEW_PERMS = [
    Permission.REVIEWS_READ.value,
    Permission.REVIEWS_CREATE.value,
    Permission.REVIEWS_UPDATE.value,
    Permission.REVIEWS_DELETE.value,
]


@pytest_asyncio.fixture
async def reviewer_role(system_roles: dict[str, Role]) -> Role:
    """The canonical `annotator` role: full review CRUD and the assignable pool (no break-glass)."""
    return system_roles[SystemRole.ANNOTATOR.value]


@pytest_asyncio.fixture
async def manager_role(db_session: AsyncSession) -> Role:
    """Review CRUD plus the `evaluation_groups:manage` break-glass that lifts the scope."""
    role = Role(
        name="review-manager",
        description="reviews CRUD + manage",
        permissions=[*_REVIEW_PERMS, Permission.EVALUATION_GROUPS_MANAGE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def author_role(db_session: AsyncSession) -> Role:
    """A red-teamer: authors flags and reads reviews of their own flags (no review write)."""
    role = Role(
        name="flag-author",
        description="flags CRUD + reviews read",
        permissions=[
            Permission.FLAGS_READ.value,
            Permission.FLAGS_CREATE.value,
            Permission.FLAGS_UPDATE.value,
            Permission.FLAGS_DELETE.value,
            Permission.REVIEWS_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _evaluation_for(
    db_session: AsyncSession,
    *,
    owner_id: UUID | None = None,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
    organization_id: UUID | None = None,
) -> Evaluation:
    group = await persist_evaluation_group(db_session, access_level=access_level, created_by_id=owner_id)
    if organization_id is not None:
        group.organization_id = organization_id
        db_session.add(group)
        await db_session.flush()
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


async def _scenario(db_session: AsyncSession, evaluation_id: UUID, *, required_reviews: int = 1) -> Scenario:
    row = Scenario(
        name="S", description="d", evaluation_id=evaluation_id, position=0, required_reviews=required_reviews
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _flag(
    db_session: AsyncSession, *, created_by_id: UUID, evaluation: Evaluation, scenario_id: UUID | None = None
) -> MessageFlag:
    assignment = await _assignment(db_session, evaluation.id)
    if scenario_id is not None:
        conversation_scenario_id = scenario_id
    else:
        conversation_scenario_id = (await _scenario(db_session, evaluation.id)).id
    conversation_group = ConversationGroup(
        user_id=created_by_id, evaluation_id=evaluation.id, name="group", scenario_id=conversation_scenario_id
    )
    db_session.add(conversation_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=created_by_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        scenario_id=conversation_scenario_id,
        conversation_group_id=conversation_group.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    flag = MessageFlag(
        reason="exploit-worthy",
        created_by_id=created_by_id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=evaluation.evaluation_group_id,
        scenario_id=scenario_id,
    )
    db_session.add(flag)
    await db_session.flush()
    await db_session.refresh(flag)
    return flag


async def _flag_a_message(db_session: AsyncSession, flag: MessageFlag, content: str) -> None:
    turn = Turn(conversation_id=flag.conversation_id, turn_index=0)
    db_session.add(turn)
    await db_session.flush()
    message = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content=content)
    db_session.add(message)
    await db_session.flush()
    db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=message.id))
    await db_session.flush()


async def _seed_messages(db_session: AsyncSession, conversation_id: UUID, count: int) -> None:
    for index in range(count):
        turn = Turn(conversation_id=conversation_id, turn_index=index)
        db_session.add(turn)
        await db_session.flush()
        db_session.add(
            Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content=f"msg {index}")
        )
    await db_session.flush()


async def _grant_in_group(db_session: AsyncSession, group_id: UUID, user_id: UUID, role: Role) -> None:
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group_id, user_id, [role])
    await db_session.flush()


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def _assign(client: AsyncClient, token: str, flag_id: UUID, reviewer_id: UUID) -> Response:
    return await client.post(
        "/api/v1/reviews",
        json={"message_flag_id": str(flag_id), "reviewer_id": str(reviewer_id)},
        headers=_auth(token),
    )


async def test_assign_returns_201_pending_and_location(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await _assign(auth_db_client, _token(caller), flag.id, reviewer.id)

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["message_flag_id"] == str(flag.id)
    assert body["reviewer_id"] == str(reviewer.id)
    assert body["assigned_by_id"] == str(caller.id)
    assert body["evaluation_id"] == str(evaluation.id)
    assert body["status"] == "pending"
    assert body["successful_exploit"] is None
    assert response.headers["Location"] == f"/api/v1/reviews/{body['id']}"


async def test_assign_duplicate_reviewer_returns_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="dup@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="dup-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=3)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)

    first = await _assign(auth_db_client, token, flag.id, reviewer.id)
    second = await _assign(auth_db_client, token, flag.id, reviewer.id)

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_409_CONFLICT
    _assert_problem(second, status.HTTP_409_CONFLICT)


async def test_queue_projects_resolved_reviewer_email(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="qemail-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="qemail-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    await _assign(auth_db_client, _token(caller), flag.id, reviewer.id)

    response = await auth_db_client.get("/api/v1/review-queue", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_200_OK
    item = next(i for i in response.json()["items"] if i["submission"]["id"] == str(flag.id))
    assert item["reviews"][0]["reviewer_email"] == reviewer.email


async def test_list_projects_resolved_reviewer_email(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="lemail-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="lemail-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    await _assign(auth_db_client, _token(caller), flag.id, reviewer.id)

    response = await auth_db_client.get(f"/api/v1/reviews?message_flag_id={flag.id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"][0]["reviewer_email"] == reviewer.email


async def test_list_projects_soft_deleted_reviewer_email(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    # resolve_reviewer_emails uses a plain (not live) select so a soft-deleted reviewer's email still
    # shows for historical attribution — deliberately asymmetric with the notify path (see services).
    caller = await _caller(db_session, reviewer_role, email="sdemail-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sdemail-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, reviewer.id)

    reviewer.deleted_at = datetime.now(UTC)
    await db_session.commit()

    response = await auth_db_client.get(f"/api/v1/reviews?message_flag_id={flag.id}", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"][0]["reviewer_email"] == reviewer.email


async def test_bulk_assign_reviews_cartesian_with_per_row_outcomes(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="bulk-author@example.com")
    r1 = await _caller(db_session, reviewer_role, email="bulk-r1@example.com")
    r2 = await _caller(db_session, reviewer_role, email="bulk-r2@example.com")
    evaluation = await _evaluation_for(db_session)
    flag_a = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    flag_b = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    # Pre-assign r1 to flag_a so that pair is an already-assigned 409 inside the batch.
    pre = await _assign(auth_db_client, _token(caller), flag_a.id, r1.id)
    assert pre.status_code == status.HTTP_201_CREATED

    rows = [
        {"row_key": f"{i}", "data": {"message_flag_id": str(fid), "reviewer_id": str(rid)}}
        for i, (fid, rid) in enumerate([(flag_a.id, r1.id), (flag_a.id, r2.id), (flag_b.id, r1.id), (flag_b.id, r2.id)])
    ]
    response = await auth_db_client.post(
        "/api/v1/reviews/bulk", json={"rows": rows, "dry_run": False}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 4
    assert body["succeeded"] == 3
    assert body["failed"] == 1
    failed = [r for r in body["results"] if r["status"] == "failed"]
    assert failed[0]["row_key"] == "0"  # (flag_a, r1) already assigned
    assert failed[0]["error"]["status"] == status.HTTP_409_CONFLICT


async def test_bulk_assign_reviews_dry_run_writes_nothing(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="bulkdry-author@example.com")
    r1 = await _caller(db_session, reviewer_role, email="bulkdry-r1@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    rows = [{"row_key": "0", "data": {"message_flag_id": str(flag.id), "reviewer_id": str(r1.id)}}]

    response = await auth_db_client.post(
        "/api/v1/reviews/bulk", json={"rows": rows, "dry_run": True}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["dry_run"] is True
    assert response.json()["succeeded"] == 1
    listing = await auth_db_client.get(f"/api/v1/reviews?message_flag_id={flag.id}", headers=_auth(_token(caller)))
    assert listing.json()["total"] == 0  # rolled back, nothing persisted


async def test_bulk_assign_rejects_duplicate_row_key(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="bulkdup-author@example.com")
    r1 = await _caller(db_session, reviewer_role, email="bulkdup-r1@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    rows = [{"row_key": "x", "data": {"message_flag_id": str(flag.id), "reviewer_id": str(r1.id)}}] * 2

    response = await auth_db_client.post("/api/v1/reviews/bulk", json={"rows": rows}, headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_assign_beyond_required_reviews_succeeds(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="cap@example.com")
    r1 = await _caller(db_session, reviewer_role, email="cap-r1@example.com")
    r2 = await _caller(db_session, reviewer_role, email="cap-r2@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)

    first = await _assign(auth_db_client, token, flag.id, r1.id)
    second = await _assign(auth_db_client, token, flag.id, r2.id)

    assert first.status_code == status.HTTP_201_CREATED
    # required_reviews is the verdict target the queue counts against, not a cap on assignments
    assert second.status_code == status.HTTP_201_CREATED


async def test_assign_without_scenario_allows_multiple(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="noscn@example.com")
    r1 = await _caller(db_session, reviewer_role, email="noscn-r1@example.com")
    r2 = await _caller(db_session, reviewer_role, email="noscn-r2@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)  # no scenario
    token = _token(caller)

    first = await _assign(auth_db_client, token, flag.id, r1.id)
    second = await _assign(auth_db_client, token, flag.id, r2.id)

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_201_CREATED


async def test_assign_unknown_flag_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="ghostflag@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="ghostflag-rv@example.com")

    response = await _assign(auth_db_client, _token(caller), uuid4(), reviewer.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_assign_unknown_reviewer_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="ghostrv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await _assign(auth_db_client, _token(caller), flag.id, uuid4())

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_assign_flag_author_as_reviewer_returns_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    author = await _caller(db_session, reviewer_role, email="selfrev-author@example.com")
    assigner = await _caller(db_session, reviewer_role, email="selfrev-assigner@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)

    response = await _assign(auth_db_client, _token(assigner), flag.id, author.id)

    assert response.status_code == status.HTTP_409_CONFLICT
    _assert_problem(response, status.HTTP_409_CONFLICT)


async def test_assign_to_decided_flag_returns_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="decided@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="decided-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    flag.status = FlagStatus.APPROVED
    db_session.add(flag)
    await db_session.flush()

    response = await _assign(auth_db_client, _token(caller), flag.id, reviewer.id)

    assert response.status_code == status.HTTP_409_CONFLICT
    _assert_problem(response, status.HTTP_409_CONFLICT)


async def test_reviewer_records_verdict_flips_status(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    assigner = await _caller(db_session, reviewer_role, email="v-assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="v-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=assigner.id, evaluation=evaluation)
    created = await _assign(auth_db_client, _token(assigner), flag.id, reviewer.id)
    review_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/reviews/{review_id}",
        json={
            "status": "approved",
            "successful_exploit": True,
            "unique_exploit": False,
            "valid_submission": True,
            "number_prompts": 4,
            "notes": "reproduced",
        },
        headers=_auth(_token(reviewer)),
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "approved"
    assert body["successful_exploit"] is True
    assert body["number_prompts"] == 4
    assert body["notes"] == "reproduced"


async def test_verdict_by_non_assigned_reviewer_returns_403(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    assigner = await _caller(db_session, reviewer_role, email="other-assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="the-reviewer@example.com")
    intruder = await _caller(db_session, reviewer_role, email="intruder-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=assigner.id, evaluation=evaluation)
    created = await _assign(auth_db_client, _token(assigner), flag.id, reviewer.id)
    review_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/reviews/{review_id}", json={"status": "approved"}, headers=_auth(_token(intruder))
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_manager_can_record_any_verdict(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, manager_role: Role
) -> None:
    reviewer = await _caller(db_session, reviewer_role, email="m-reviewer@example.com")
    manager = await _caller(db_session, manager_role, email="m-manager@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=manager.id, evaluation=evaluation)
    created = await _assign(auth_db_client, _token(manager), flag.id, reviewer.id)
    review_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/reviews/{review_id}", json={"status": "rejected"}, headers=_auth(_token(manager))
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "rejected"


async def test_verdict_status_pending_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    reviewer = await _caller(db_session, reviewer_role, email="pend@example.com")
    author = await _caller(db_session, reviewer_role, email="pend-author@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    created = await _assign(auth_db_client, _token(reviewer), flag.id, reviewer.id)
    review_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/reviews/{review_id}", json={"status": "pending"}, headers=_auth(_token(reviewer))
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_verdict_negative_prompt_count_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    reviewer = await _caller(db_session, reviewer_role, email="neg@example.com")
    author = await _caller(db_session, reviewer_role, email="neg-author@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    created = await _assign(auth_db_client, _token(reviewer), flag.id, reviewer.id)
    review_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/reviews/{review_id}", json={"number_prompts": -1}, headers=_auth(_token(reviewer))
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_list_reviews_filtered_by_flag(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="lf@example.com")
    r1 = await _caller(db_session, reviewer_role, email="lf-r1@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=2)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    other_flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, r1.id)
    await _assign(auth_db_client, token, other_flag.id, r1.id)

    response = await auth_db_client.get(f"/api/v1/reviews?message_flag_id={flag.id}", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["message_flag_id"] == str(flag.id)


async def test_list_reviews_filtered_by_evaluation(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="le@example.com")
    r1 = await _caller(db_session, reviewer_role, email="le-r1@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, r1.id)

    response = await auth_db_client.get(f"/api/v1/reviews?evaluation_id={evaluation.id}", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1


async def test_red_teamer_sees_only_own_flag_reviews(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    assigner = await _caller(db_session, reviewer_role, email="rt-assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rt-reviewer@example.com")
    nosy = await _caller(db_session, author_role, email="rt-nosy@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=assigner.id, evaluation=evaluation)
    created = await _assign(auth_db_client, _token(assigner), flag.id, reviewer.id)
    review_id = created.json()["id"]

    listing = await auth_db_client.get("/api/v1/reviews", headers=_auth(_token(nosy)))
    detail = await auth_db_client.get(f"/api/v1/reviews/{review_id}", headers=_auth(_token(nosy)))

    assert listing.status_code == status.HTTP_200_OK
    assert listing.json()["total"] == 0  # nosy authored no flags → sees no reviews
    assert detail.status_code == status.HTTP_404_NOT_FOUND


async def test_author_reads_reviews_of_own_flag(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="own-author@example.com")
    assigner = await _caller(db_session, reviewer_role, email="own-assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="own-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    await _assign(auth_db_client, _token(assigner), flag.id, reviewer.id)

    response = await auth_db_client.get(f"/api/v1/reviews?message_flag_id={flag.id}", headers=_auth(_token(author)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1


async def test_queue_lists_awaiting_then_excludes_completed(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="q-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="q-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    created = await _assign(auth_db_client, token, flag.id, reviewer.id)
    review_id = created.json()["id"]

    awaiting = await auth_db_client.get(f"/api/v1/review-queue?scenario_id={scenario.id}", headers=_auth(token))
    assert awaiting.status_code == status.HTTP_200_OK
    awaiting_body = awaiting.json()
    assert awaiting_body["total"] == 1
    item = awaiting_body["items"][0]
    assert item["submission"]["id"] == str(flag.id)
    assert item["required_reviews"] == 1
    assert item["completed_reviews"] == 0
    assert len(item["reviews"]) == 1

    await auth_db_client.patch(
        f"/api/v1/reviews/{review_id}", json={"status": "approved"}, headers=_auth(_token(reviewer))
    )
    after = await auth_db_client.get(f"/api/v1/review-queue?scenario_id={scenario.id}", headers=_auth(token))

    assert after.status_code == status.HTTP_200_OK
    assert after.json()["total"] == 0  # completed == required → no longer awaiting


async def test_queue_unassigned_lists_only_flags_with_no_reviewer(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="unq-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="unq-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    assigned = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    untouched = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    await _assign(auth_db_client, token, assigned.id, reviewer.id)

    response = await auth_db_client.get(
        f"/api/v1/review-queue?scenario_id={scenario.id}&unassigned=true", headers=_auth(token)
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [item["submission"]["id"] for item in body["items"]] == [str(untouched.id)]
    # The count must describe the filtered set, or the pager lies about how much is left.
    assert body["total"] == 1


async def test_queue_unassigned_counts_a_soft_deleted_review_as_unassigned(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="unqd-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="unqd-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    # A second flag keeps a live reviewer, so the assertion below separates "filtered" from
    # "there was only ever one flag".
    still_assigned = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    created = await _assign(auth_db_client, token, flag.id, reviewer.id)
    await _assign(auth_db_client, token, still_assigned.id, reviewer.id)
    await auth_db_client.delete(f"/api/v1/reviews/{created.json()['id']}", headers=_auth(token))

    response = await auth_db_client.get(
        f"/api/v1/review-queue?scenario_id={scenario.id}&unassigned=true", headers=_auth(token)
    )

    assert [item["submission"]["id"] for item in response.json()["items"]] == [str(flag.id)]


async def test_queue_unassigned_hides_a_flag_whose_only_review_was_decided(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="unqv-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="unqv-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    # Two required reviews with one decided is the only state that separates the two readings of
    # "unassigned": the flag is short of its quota, so it stays queued, yet it holds a review.
    scenario = await _scenario(db_session, evaluation.id, required_reviews=2)
    decided = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    untouched = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    await _assign_and_decide(auth_db_client, assigner=caller, reviewer=reviewer, flag_id=decided.id)

    unfiltered = await auth_db_client.get(f"/api/v1/review-queue?scenario_id={scenario.id}", headers=_auth(token))
    filtered = await auth_db_client.get(
        f"/api/v1/review-queue?scenario_id={scenario.id}&unassigned=true", headers=_auth(token)
    )

    # Still short of its quota, so still queued…
    assert str(decided.id) in [item["submission"]["id"] for item in unfiltered.json()["items"]]
    # …but the filter means "no review row at all", not "nobody is working on it right now".
    assert [item["submission"]["id"] for item in filtered.json()["items"]] == [str(untouched.id)]
    assert filtered.json()["total"] == 1


async def test_queue_without_the_filter_still_lists_assigned_flags(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="unqo-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="unqo-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, reviewer.id)

    response = await auth_db_client.get(f"/api/v1/review-queue?scenario_id={scenario.id}", headers=_auth(token))

    assert [item["submission"]["id"] for item in response.json()["items"]] == [str(flag.id)]


async def test_unassign_soft_deletes(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="u-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="u-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    created = await _assign(auth_db_client, token, flag.id, reviewer.id)
    review_id = created.json()["id"]

    deleted = await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    follow_up = await auth_db_client.get(f"/api/v1/reviews/{review_id}", headers=_auth(token))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


async def test_unassign_then_reassign_same_reviewer(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="re-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="re-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    first = await _assign(auth_db_client, token, flag.id, reviewer.id)
    await auth_db_client.delete(f"/api/v1/reviews/{first.json()['id']}", headers=_auth(token))

    # The partial-unique index ignores the tombstoned row, so re-assigning works.
    second = await _assign(auth_db_client, token, flag.id, reviewer.id)

    assert second.status_code == status.HTTP_201_CREATED


async def _assign_and_decide(client: AsyncClient, *, assigner: User, reviewer: User, flag_id: UUID) -> str:
    created = await _assign(client, _token(assigner), flag_id, reviewer.id)
    review_id = created.json()["id"]
    verdict = await client.patch(
        f"/api/v1/reviews/{review_id}", json={"status": "approved"}, headers=_auth(_token(reviewer))
    )
    assert verdict.status_code == status.HTTP_200_OK
    return review_id


async def test_unassign_decided_review_by_peer_returns_403(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="udp-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="udp-reviewer@example.com")
    peer = await _caller(db_session, reviewer_role, email="udp-peer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    review_id = await _assign_and_decide(auth_db_client, assigner=peer, reviewer=reviewer, flag_id=flag.id)

    deleted = await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(peer)))

    assert deleted.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(deleted, status.HTTP_403_FORBIDDEN)


async def test_unassign_own_decided_review_succeeds(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="udo-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="udo-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    review_id = await _assign_and_decide(auth_db_client, assigner=reviewer, reviewer=reviewer, flag_id=flag.id)

    deleted = await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(reviewer)))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT


async def test_unassign_decided_review_by_manager_succeeds(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, manager_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="udm-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="udm-reviewer@example.com")
    manager = await _caller(db_session, manager_role, email="udm-manager@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    review_id = await _assign_and_decide(auth_db_client, assigner=manager, reviewer=reviewer, flag_id=flag.id)

    deleted = await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(manager)))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT


async def _backdate_review_tombstone(db_session: AsyncSession, review_id: str, *, days: int) -> None:
    """Age a tombstone past the restore window.

    Plain `select` (it must see deleted rows) with `populate_existing` to refresh just this
    row — a session-wide `expire_all()` would leave every `User` the test still holds
    expired, and the next attribute read would try a sync lazy-load off the async session.
    """
    statement = select(Review).where(col(Review.id) == UUID(review_id)).execution_options(populate_existing=True)
    row = (await db_session.execute(statement)).scalar_one()
    row.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db_session.add(row)
    await db_session.flush()


async def test_restore_puts_the_reviewer_back(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="rs-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rs-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    follow_up = await auth_db_client.get(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    assert follow_up.status_code == status.HTTP_200_OK


async def test_restore_keeps_the_recorded_verdict(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    # Unassigning a decided review and restoring it returns the verdict, not a blank assignment.
    author = await _caller(db_session, author_role, email="rv-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rv-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    review_id = await _assign_and_decide(auth_db_client, assigner=reviewer, reviewer=reviewer, flag_id=flag.id)
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(reviewer)))

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "approved"


async def test_restore_decided_review_by_peer_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, manager_role: Role, author_role: Role
) -> None:
    # Mirrors the unassign gate: a peer may not resurrect someone else's recorded verdict.
    # The manager unassigns (a peer cannot), then the peer tries to bring it back.
    author = await _caller(db_session, author_role, email="rdp-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rdp-reviewer@example.com")
    peer = await _caller(db_session, reviewer_role, email="rdp-peer@example.com")
    manager = await _caller(db_session, manager_role, email="rdp-manager@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    review_id = await _assign_and_decide(auth_db_client, assigner=manager, reviewer=reviewer, flag_id=flag.id)
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(manager)))

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(_token(peer)))

    # The manager's tombstone isn't the peer's to see, so the deleter scope answers first.
    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_restore_decided_review_by_manager_succeeds(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, manager_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="rdm-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rdm-reviewer@example.com")
    manager = await _caller(db_session, manager_role, email="rdm-manager@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    review_id = await _assign_and_decide(auth_db_client, assigner=manager, reviewer=reviewer, flag_id=flag.id)
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(manager)))

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(_token(manager)))

    assert response.status_code == status.HTTP_200_OK


async def test_restore_pending_review_with_deleted_reviewer_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, manager_role: Role
) -> None:
    # A pending review is a working assignment — reviving one whose reviewer is gone would
    # park the submission behind a review nobody can act on.
    caller = await _caller(db_session, manager_role, email="ddr-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="ddr-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    reviewer.soft_delete(by_id=caller.id)
    db_session.add(reviewer)
    await db_session.flush()

    # Listing first: the failed restore's rollback would also undo the flushed tombstone.
    listed = await auth_db_client.get("/api/v1/reviews?deleted=true", headers=_auth(token))
    assert review_id not in [row["id"] for row in listed.json()["items"]]
    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_restore_pending_review_with_unassignable_reviewer_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    # Mirrors `assign_reviewer`: a reviewer who has since left the assignable pool cannot be
    # handed the assignment back either.
    caller = await _caller(db_session, reviewer_role, email="uar-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="uar-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    await db_session.execute(delete(UserRole).where(col(UserRole.user_id) == reviewer.id))
    await db_session.flush()

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_restore_pending_review_on_decided_flag_returns_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    # Same rule as assigning: a decided submission takes no new pending reviewers.
    caller = await _caller(db_session, reviewer_role, email="rdf-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rdf-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    statement = select(MessageFlag).where(col(MessageFlag.id) == flag.id).execution_options(populate_existing=True)
    row = (await db_session.execute(statement)).scalar_one()
    row.status = FlagStatus.APPROVED
    db_session.add(row)
    await db_session.flush()

    # Listing first: the failed restore's rollback would also undo the flushed flag flip.
    listed = await auth_db_client.get("/api/v1/reviews?deleted=true", headers=_auth(token))
    assert review_id not in [row["id"] for row in listed.json()["items"]]
    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    _assert_problem(response, status.HTTP_409_CONFLICT)


async def test_restore_decided_review_with_deleted_reviewer_succeeds(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, manager_role: Role, author_role: Role
) -> None:
    # A decided review is a record, not a working assignment: the verdict comes back for
    # consensus even when its reviewer's account is gone (no reassignment mail is sent).
    author = await _caller(db_session, author_role, email="drr-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="drr-reviewer@example.com")
    manager = await _caller(db_session, manager_role, email="drr-manager@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    review_id = await _assign_and_decide(auth_db_client, assigner=manager, reviewer=reviewer, flag_id=flag.id)
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(manager)))
    reviewer.soft_delete(by_id=manager.id)
    db_session.add(reviewer)
    await db_session.flush()

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(_token(manager)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "approved"


async def test_restore_409s_when_the_reviewer_was_reassigned(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    # The partial-unique index covers live rows only, so the unassign freed the slot; a fresh
    # assignment now holds it and the tombstone can't come back alongside it.
    caller = await _caller(db_session, reviewer_role, email="rc-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rc-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    assert (await _assign(auth_db_client, token, flag.id, reviewer.id)).status_code == status.HTTP_201_CREATED

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_restore_404s_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="rw-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rw-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    await _backdate_review_tombstone(db_session, review_id, days=30)

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_restore_404s_when_the_parent_flag_is_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    # Parent liveness is the visibility spine: restoring the flag is what brings its
    # reviews back into reach, and a 404 doesn't confirm the tombstone exists.
    caller = await _caller(db_session, reviewer_role, email="rp-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="rp-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    flag.soft_delete(caller.id)
    await db_session.flush()

    response = await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_restore_dispatches_the_assignment_email(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reviewer_role: Role,
    celery_enqueue_stub: MagicMock,
) -> None:
    caller = await _caller(db_session, reviewer_role, email="re-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="re-mail-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))

    reviewer_email = reviewer.email

    await auth_db_client.post(f"/api/v1/reviews/{review_id}/restore", headers=_auth(token))

    all_emails = (await db_session.execute(OutboundEmail.live_select())).scalars().all()
    assigned = [email for email in all_emails if email.template_name == "review_assigned"]
    # The reviewer is told twice: the original assignment and the restore that re-instates it.
    assert [email.recipient for email in assigned] == [reviewer_email, reviewer_email]


async def test_deleted_listing_scopes_to_the_callers_own_unassignments(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, manager_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="dl-caller@example.com")
    other = await _caller(db_session, reviewer_role, email="dl-other@example.com")
    manager = await _caller(db_session, manager_role, email="dl-manager@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="dl-reviewer@example.com")
    second_reviewer = await _caller(db_session, reviewer_role, email="dl-reviewer-2@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    mine = (await _assign(auth_db_client, _token(caller), flag.id, reviewer.id)).json()["id"]
    theirs = (await _assign(auth_db_client, _token(caller), flag.id, second_reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{mine}", headers=_auth(_token(caller)))
    await auth_db_client.delete(f"/api/v1/reviews/{theirs}", headers=_auth(_token(other)))

    own = await auth_db_client.get("/api/v1/reviews?deleted=true", headers=_auth(_token(caller)))
    break_glass = await auth_db_client.get("/api/v1/reviews?deleted=true", headers=_auth(_token(manager)))

    assert [item["id"] for item in own.json()["items"]] == [mine]
    assert {item["id"] for item in break_glass.json()["items"]} == {mine, theirs}


async def test_deleted_listing_excludes_tombstones_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="dw-caller@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="dw-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    review_id = (await _assign(auth_db_client, token, flag.id, reviewer.id)).json()["id"]
    await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(token))
    await _backdate_review_tombstone(db_session, review_id, days=30)

    listed = await auth_db_client.get("/api/v1/reviews?deleted=true", headers=_auth(token))

    assert [item["id"] for item in listed.json()["items"]] == []


async def test_assign_non_annotator_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="na-caller@example.com")
    outsider = await _caller(db_session, author_role, email="na-outsider@example.com")  # not an annotator
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await _assign(auth_db_client, _token(caller), flag.id, outsider.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_assign_invitation_only_requires_in_group_annotator(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reviewer_role: Role,
    manager_role: Role,
    author_role: Role,
    system_roles: dict[str, Role],
) -> None:
    manager = await _caller(db_session, manager_role, email="io-manager@example.com")
    outsider = await _caller(
        db_session, reviewer_role, email="io-outsider@example.com"
    )  # global annotator, not a member
    member = await _caller(db_session, author_role, email="io-member@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await _grant_in_group(
        db_session, evaluation.evaluation_group_id, member.id, system_roles[SystemRole.ANNOTATOR.value]
    )
    scenario = await _scenario(db_session, evaluation.id, required_reviews=2)  # cap > 1 so it can't confound
    flag = await _flag(db_session, created_by_id=manager.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(manager)  # break-glass lifts visibility on this invitation-only group

    # Successful assign first: a failed (rolled-back) write would otherwise discard the shared-session setup.
    accepted = await _assign(auth_db_client, token, flag.id, member.id)
    rejected = await _assign(auth_db_client, token, flag.id, outsider.id)

    assert accepted.status_code == status.HTTP_201_CREATED  # in-group annotator member
    assert rejected.status_code == status.HTTP_404_NOT_FOUND  # global annotator, but not an in-group member


async def test_assign_organization_admits_org_annotators_and_in_group_members(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reviewer_role: Role,
    manager_role: Role,
    author_role: Role,
    system_roles: dict[str, Role],
) -> None:
    org = Organization(name="Acme Corp")
    db_session.add(org)
    await db_session.flush()
    manager = await _caller(db_session, manager_role, email="org-manager@example.com")
    # In the org and holds the global annotator role, but not an in-group member.
    org_annotator = await _caller(db_session, reviewer_role, email="org-annotator@example.com")
    org_annotator.organization_id = org.id
    # In-group annotator member invited from outside the org (no org of their own).
    invited_external = await _caller(db_session, author_role, email="org-external@example.com")
    # Global annotator, but neither in the org nor an in-group member.
    outsider = await _caller(db_session, reviewer_role, email="org-outsider@example.com")
    await db_session.flush()
    evaluation = await _evaluation_for(
        db_session, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )
    await _grant_in_group(
        db_session, evaluation.evaluation_group_id, invited_external.id, system_roles[SystemRole.ANNOTATOR.value]
    )
    flag = await _flag(db_session, created_by_id=manager.id, evaluation=evaluation)
    token = _token(manager)  # break-glass lifts visibility on the org group

    # Successful assigns first: a rolled-back failure would discard the shared-session setup.
    org_member = await _assign(auth_db_client, token, flag.id, org_annotator.id)
    in_group = await _assign(auth_db_client, token, flag.id, invited_external.id)
    rejected = await _assign(auth_db_client, token, flag.id, outsider.id)

    assert org_member.status_code == status.HTTP_201_CREATED  # org user holding the global annotator role
    assert in_group.status_code == status.HTTP_201_CREATED  # in-group annotator member (membership overrides scope)
    assert rejected.status_code == status.HTTP_404_NOT_FOUND  # annotator, but neither in the org nor a member


async def test_invitation_only_reviews_hidden_from_non_member(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reviewer_role: Role,
    manager_role: Role,
    author_role: Role,
    system_roles: dict[str, Role],
) -> None:
    manager = await _caller(db_session, manager_role, email="vis-manager@example.com")
    member = await _caller(db_session, author_role, email="vis-member@example.com")
    outsider = await _caller(db_session, reviewer_role, email="vis-outsider@example.com")  # annotator, not a member
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await _grant_in_group(
        db_session, evaluation.evaluation_group_id, member.id, system_roles[SystemRole.ANNOTATOR.value]
    )
    scenario = await _scenario(db_session, evaluation.id, required_reviews=1)
    flag = await _flag(db_session, created_by_id=manager.id, evaluation=evaluation, scenario_id=scenario.id)
    created = await _assign(auth_db_client, _token(manager), flag.id, member.id)
    assert created.status_code == status.HTTP_201_CREATED
    review_id = created.json()["id"]

    out_list = await auth_db_client.get("/api/v1/reviews", headers=_auth(_token(outsider)))
    out_detail = await auth_db_client.get(f"/api/v1/reviews/{review_id}", headers=_auth(_token(outsider)))
    out_queue = await auth_db_client.get("/api/v1/review-queue", headers=_auth(_token(outsider)))
    mgr_detail = await auth_db_client.get(f"/api/v1/reviews/{review_id}", headers=_auth(_token(manager)))

    assert out_list.json()["total"] == 0  # invitation-only group invisible to a non-member
    assert out_detail.status_code == status.HTTP_404_NOT_FOUND
    assert out_queue.json()["total"] == 0
    assert mgr_detail.status_code == status.HTTP_200_OK  # break-glass manager sees it


async def test_queue_scopes_red_teamer_to_own_flags(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    red_teamer = await _caller(db_session, author_role, email="q-rt@example.com")
    other = await _caller(db_session, reviewer_role, email="q-other@example.com")
    evaluation = await _evaluation_for(db_session)
    own_flag = await _flag(db_session, created_by_id=red_teamer.id, evaluation=evaluation)
    foreign_flag = await _flag(db_session, created_by_id=other.id, evaluation=evaluation)

    response = await auth_db_client.get("/api/v1/review-queue", headers=_auth(_token(red_teamer)))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    flag_ids = {item["submission"]["id"] for item in body["items"]}
    assert body["total"] == 1
    assert str(own_flag.id) in flag_ids
    assert str(foreign_flag.id) not in flag_ids


async def test_list_endpoints_reject_limit_over_max(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="pg-over@example.com")
    token = _token(caller)

    reviews_over = await auth_db_client.get("/api/v1/reviews?limit=101", headers=_auth(token))
    queue_over = await auth_db_client.get("/api/v1/review-queue?limit=101", headers=_auth(token))

    assert reviews_over.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(reviews_over, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert queue_over.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(queue_over, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_list_reviews_paginates_with_limit_offset(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="pgp-caller@example.com")
    r1 = await _caller(db_session, reviewer_role, email="pgp-r1@example.com")
    r2 = await _caller(db_session, reviewer_role, email="pgp-r2@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id, required_reviews=2)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, r1.id)
    await _assign(auth_db_client, token, flag.id, r2.id)

    base = f"/api/v1/reviews?message_flag_id={flag.id}"
    first = await auth_db_client.get(f"{base}&limit=1&offset=0", headers=_auth(token))
    second = await auth_db_client.get(f"{base}&limit=1&offset=1", headers=_auth(token))

    assert first.json()["total"] == 2
    assert second.json()["total"] == 2
    assert len(first.json()["items"]) == 1
    assert len(second.json()["items"]) == 1
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]


async def test_assign_dispatches_review_assigned_email(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, celery_enqueue_stub: MagicMock
) -> None:
    caller = await _caller(db_session, reviewer_role, email="mail-assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="mail-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await _assign(auth_db_client, _token(caller), flag.id, reviewer.id)

    assert response.status_code == status.HTTP_201_CREATED
    emails = (await db_session.execute(OutboundEmail.live_select())).scalars().all()
    assert len(emails) == 1
    assert emails[0].template_name == "review_assigned"
    assert emails[0].recipient == reviewer.email
    assert emails[0].context["assignee_name"] == reviewer.email  # no first/last name set
    assert emails[0].context["evaluation_title"] == "Eval"
    celery_enqueue_stub.assert_called_once()


async def test_unassign_dispatches_review_unassigned_email(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, celery_enqueue_stub: MagicMock
) -> None:
    caller = await _caller(db_session, reviewer_role, email="umail-assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="umail-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    created = await _assign(auth_db_client, token, flag.id, reviewer.id)

    deleted = await auth_db_client.delete(f"/api/v1/reviews/{created.json()['id']}", headers=_auth(token))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    all_emails = (await db_session.execute(OutboundEmail.live_select())).scalars().all()
    unassigned = [email for email in all_emails if email.template_name == "review_unassigned"]
    assert len(unassigned) == 1
    assert unassigned[0].recipient == reviewer.email
    assert celery_enqueue_stub.call_count == 2  # assign + unassign each enqueue one mail


async def test_bulk_assign_sends_one_assignment_mail_per_review(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, celery_enqueue_stub: MagicMock
) -> None:
    caller = await _caller(db_session, reviewer_role, email="digest-author@example.com")
    r1 = await _caller(db_session, reviewer_role, email="digest-r1@example.com")
    r2 = await _caller(db_session, reviewer_role, email="digest-r2@example.com")
    evaluation = await _evaluation_for(db_session)
    flag_a = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    flag_b = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    # r1 → both flags (2 assignments), r2 → one flag (1 assignment) = 3 assignments, 2 reviewers.
    rows = [
        {"row_key": f"{i}", "data": {"message_flag_id": str(fid), "reviewer_id": str(rid)}}
        for i, (fid, rid) in enumerate([(flag_a.id, r1.id), (flag_b.id, r1.id), (flag_a.id, r2.id)])
    ]

    response = await auth_db_client.post("/api/v1/reviews/bulk", json={"rows": rows}, headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["succeeded"] == 3
    emails = (await db_session.execute(OutboundEmail.live_select())).scalars().all()
    # Bulk reuses the single-assign mail: one per assignment (r1 gets two, r2 one), no bespoke digest.
    assigned = [email for email in emails if email.template_name == "review_assigned"]
    assert sorted(email.recipient for email in assigned) == sorted([r1.email, r1.email, r2.email])


async def test_bulk_assign_notify_failure_does_not_fail_batch(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # apply_bulk commits the reviews before the endpoint's best-effort post-commit notify. A failure in
    # that notify block is swallowed + logged — the already-durable assignments must not 500 the batch.
    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("notify down")

    monkeypatch.setattr("app.api.v1.reviews.notify_review_assigned", _boom)
    caller = await _caller(db_session, reviewer_role, email="bnotify-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="bnotify-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    rows = [{"row_key": "0", "data": {"message_flag_id": str(flag.id), "reviewer_id": str(reviewer.id)}}]

    response = await auth_db_client.post("/api/v1/reviews/bulk", json={"rows": rows}, headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["succeeded"] == 1
    # The assignment is durable despite the swallowed notify failure.
    listed = await auth_db_client.get(f"/api/v1/reviews?message_flag_id={flag.id}", headers=_auth(token))
    assert listed.json()["total"] == 1


async def test_bulk_assign_requires_reviews_create(
    auth_db_client: AsyncClient, db_session: AsyncSession, author_role: Role, reviewer_role: Role
) -> None:
    # author_role holds reviews:read only — bulk assign needs reviews:create → 403.
    caller = await _caller(db_session, author_role, email="bulk-noperm@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="bulk-noperm-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    rows = [{"row_key": "0", "data": {"message_flag_id": str(flag.id), "reviewer_id": str(reviewer.id)}}]

    response = await auth_db_client.post("/api/v1/reviews/bulk", json={"rows": rows}, headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_assign_email_failure_does_not_roll_back(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("smtp down")

    monkeypatch.setattr("app.core.email.send_email", _boom)
    caller = await _caller(db_session, reviewer_role, email="boom-assigner@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="boom-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await _assign(auth_db_client, _token(caller), flag.id, reviewer.id)

    assert response.status_code == status.HTTP_201_CREATED  # best-effort mail failure must not roll back the assign
    follow_up = await auth_db_client.get(f"/api/v1/reviews/{response.json()['id']}", headers=_auth(_token(caller)))
    assert follow_up.status_code == status.HTTP_200_OK


async def test_endpoints_require_auth(auth_db_client: AsyncClient) -> None:
    assert (await auth_db_client.get("/api/v1/reviews")).status_code == status.HTTP_401_UNAUTHORIZED
    assert (await auth_db_client.get("/api/v1/review-queue")).status_code == status.HTTP_401_UNAUTHORIZED
    assert (await auth_db_client.get(f"/api/v1/reviews/{uuid4()}")).status_code == status.HTTP_401_UNAUTHORIZED
    assert (await auth_db_client.get(f"/api/v1/submissions/{uuid4()}")).status_code == status.HTTP_401_UNAUTHORIZED
    assert (
        await auth_db_client.get(f"/api/v1/submissions/{uuid4()}/messages")
    ).status_code == status.HTTP_401_UNAUTHORIZED


async def test_submission_messages_returns_conversation_transcript(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sm-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sm-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    await _seed_messages(db_session, flag.conversation_id, 2)

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}/messages", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert [message["content"] for message in body["items"]] == ["msg 0", "msg 1"]


async def test_submission_messages_carry_transcript_fields_without_flag_count(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sm-shape-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sm-shape-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    await _seed_messages(db_session, flag.conversation_id, 1)

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}/messages", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    item = response.json()["items"][0]
    assert {"turn_id", "replaces_message_id", "extra"} <= item.keys()
    assert "flag_count" not in item


async def test_submission_messages_carry_per_message_tags(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    # The tags went into the prompt that produced the reply, so a reviewer judging the exploit has
    # to see them; this is the HTTP-level guard that the reviewer transcript projects them.
    author = await _caller(db_session, author_role, email="sm-tags-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sm-tags-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    turn = Turn(conversation_id=flag.conversation_id, turn_index=0)
    db_session.add(turn)
    await db_session.flush()
    db_session.add(
        Message(
            turn_id=turn.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETE,
            content="msg 0",
            tags={"env": "prod"},
        )
    )
    await db_session.flush()

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}/messages", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"][0]["tags"] == {"env": "prod"}


async def test_submission_messages_carry_tag_context(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    # Guards `TranscriptMessage._base_kwargs` dropping `tag_context` silently on the reviewer transcript.
    author = await _caller(db_session, author_role, email="sm-tagctx-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sm-tagctx-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    turn = Turn(conversation_id=flag.conversation_id, turn_index=0)
    db_session.add(turn)
    await db_session.flush()
    db_session.add(
        Message(
            turn_id=turn.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETE,
            content="msg 0",
            extra={TAG_CONTEXT_EXTRA_KEY: {"env": "prod"}},
        )
    )
    await db_session.flush()

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}/messages", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"][0]["tag_context"] == {"env": "prod"}


async def test_submission_messages_red_teamer_sees_own_not_foreign(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    red_teamer = await _caller(db_session, author_role, email="sm-rt@example.com")
    other = await _caller(db_session, reviewer_role, email="sm-other@example.com")
    evaluation = await _evaluation_for(db_session)
    own = await _flag(db_session, created_by_id=red_teamer.id, evaluation=evaluation)
    foreign = await _flag(db_session, created_by_id=other.id, evaluation=evaluation)
    token = _token(red_teamer)

    own_response = await auth_db_client.get(f"/api/v1/submissions/{own.id}/messages", headers=_auth(token))
    foreign_response = await auth_db_client.get(f"/api/v1/submissions/{foreign.id}/messages", headers=_auth(token))

    assert own_response.status_code == status.HTTP_200_OK
    assert foreign_response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(foreign_response, status.HTTP_404_NOT_FOUND)


async def test_submission_messages_unknown_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="sm-ghost@example.com")

    response = await auth_db_client.get(f"/api/v1/submissions/{uuid4()}/messages", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_submission_detail_visible_to_reviewer_with_reviews(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sd-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sd-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    await _assign(auth_db_client, _token(reviewer), flag.id, reviewer.id)

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["submission"]["id"] == str(flag.id)
    assert body["submission"]["conversation_id"] == str(flag.conversation_id)
    assert body["reviews"]["total"] == 1
    assert body["reviews"]["items"][0]["reviewer_id"] == str(reviewer.id)


async def test_submission_detail_returns_all_reviews(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sd-multi-author@example.com")
    r1 = await _caller(db_session, reviewer_role, email="sd-multi-r1@example.com")
    r2 = await _caller(db_session, reviewer_role, email="sd-multi-r2@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    await _assign(auth_db_client, _token(r1), flag.id, r1.id)
    await _assign(auth_db_client, _token(r1), flag.id, r2.id)  # no required_reviews cap on assignments

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(r1)))

    assert response.status_code == status.HTTP_200_OK
    reviewer_ids = {review["reviewer_id"] for review in response.json()["reviews"]["items"]}
    assert reviewer_ids == {str(r1.id), str(r2.id)}


async def test_submission_detail_empty_reviews_when_unassigned(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sd-empty-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sd-empty-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["reviews"]["total"] == 0
    assert response.json()["reviews"]["items"] == []


async def test_submission_detail_red_teamer_sees_own_not_foreign(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    red_teamer = await _caller(db_session, author_role, email="sd-rt@example.com")
    other = await _caller(db_session, reviewer_role, email="sd-other@example.com")
    evaluation = await _evaluation_for(db_session)
    own = await _flag(db_session, created_by_id=red_teamer.id, evaluation=evaluation)
    foreign = await _flag(db_session, created_by_id=other.id, evaluation=evaluation)
    token = _token(red_teamer)

    own_response = await auth_db_client.get(f"/api/v1/submissions/{own.id}", headers=_auth(token))
    foreign_response = await auth_db_client.get(f"/api/v1/submissions/{foreign.id}", headers=_auth(token))

    assert own_response.status_code == status.HTTP_200_OK
    assert own_response.json()["submission"]["id"] == str(own.id)
    assert foreign_response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(foreign_response, status.HTTP_404_NOT_FOUND)


async def test_submission_detail_unknown_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="sd-ghost@example.com")

    response = await auth_db_client.get(f"/api/v1/submissions/{uuid4()}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_submission_detail_includes_flagged_messages(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sd-msg-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sd-msg-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    await _flag_a_message(db_session, flag, "exploit reply")

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    assert [m["content"] for m in response.json()["submission"]["messages"]] == ["exploit reply"]


async def test_submission_detail_marks_superseded_flagged_messages(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sd-sup-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sd-sup-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)

    turn = Turn(conversation_id=flag.conversation_id, turn_index=0)
    db_session.add(turn)
    await db_session.flush()
    superseded = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="original")
    db_session.add(superseded)
    await db_session.flush()
    db_session.add(  # the regenerate that supersedes it (not itself flagged)
        Message(
            turn_id=turn.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETE,
            content="regenerated",
            replaces_message_id=superseded.id,
        )
    )
    live = Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content="live prompt")
    db_session.add(live)
    await db_session.flush()
    db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=superseded.id))
    db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=live.id))
    await db_session.flush()

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json().get("superseded_message_ids") == [str(superseded.id)]


async def test_submission_detail_invitation_only_hidden_from_non_member(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reviewer_role: Role,
    manager_role: Role,
    author_role: Role,
    system_roles: dict[str, Role],
) -> None:
    manager = await _caller(db_session, manager_role, email="sd-io-manager@example.com")
    member = await _caller(db_session, author_role, email="sd-io-member@example.com")
    outsider = await _caller(db_session, reviewer_role, email="sd-io-outsider@example.com")  # annotator, not a member
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await _grant_in_group(
        db_session, evaluation.evaluation_group_id, member.id, system_roles[SystemRole.ANNOTATOR.value]
    )
    flag = await _flag(db_session, created_by_id=member.id, evaluation=evaluation)

    outsider_response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(outsider)))
    manager_response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(manager)))

    assert outsider_response.status_code == status.HTTP_404_NOT_FOUND  # invitation-only group invisible to a non-member
    _assert_problem(outsider_response, status.HTTP_404_NOT_FOUND)
    assert manager_response.status_code == status.HTTP_200_OK  # break-glass manager sees it


async def test_submission_detail_hidden_when_conversation_soft_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sd-conv-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sd-conv-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    conversation = await db_session.get(Conversation, flag.conversation_id)
    assert conversation is not None
    conversation.deleted_at = datetime.now(UTC)
    await db_session.flush()

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_submission_messages_hidden_when_conversation_soft_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role, author_role: Role
) -> None:
    author = await _caller(db_session, author_role, email="sm-conv-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="sm-conv-reviewer@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=author.id, evaluation=evaluation)
    conversation = await db_session.get(Conversation, flag.conversation_id)
    assert conversation is not None
    conversation.deleted_at = datetime.now(UTC)
    await db_session.flush()

    response = await auth_db_client.get(f"/api/v1/submissions/{flag.id}/messages", headers=_auth(_token(reviewer)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def _assignable(client: AsyncClient, token: str, flag_id: UUID) -> Response:
    return await client.get(f"/api/v1/submissions/{flag_id}/assignable-reviewers", headers=_auth(token))


async def test_assignable_reviewers_lists_only_gate_accepted_users(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="ar-author@example.com")
    b = await _caller(db_session, reviewer_role, email="ar-b@example.com")
    c = await _caller(db_session, reviewer_role, email="ar-c@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await _assignable(auth_db_client, _token(caller), flag.id)

    assert response.status_code == status.HTTP_200_OK
    ids = {item["id"] for item in response.json()["items"]}
    assert str(caller.id) not in ids  # flag author excluded (no self-review)
    assert {str(b.id), str(c.id)} <= ids
    # Contract the picker relies on: every listed candidate is actually assignable.
    for uid in ids:
        assigned = await _assign(auth_db_client, _token(caller), flag.id, UUID(uid))
        assert assigned.status_code == status.HTTP_201_CREATED


async def test_assignable_reviewers_search_narrows_by_email(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="ars-author@example.com")
    await _caller(db_session, reviewer_role, email="ars-alice@example.com")
    await _caller(db_session, reviewer_role, email="ars-bob@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await auth_db_client.get(
        f"/api/v1/submissions/{flag.id}/assignable-reviewers?search=ALICE", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    assert {item["email"] for item in response.json()["items"]} == {"ars-alice@example.com"}


async def test_assignable_reviewers_search_treats_metacharacters_literally(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    # The reviewer picker escapes `search` at its own inline site; a lone '%' must stay literal
    # (would otherwise list every candidate).
    caller = await _caller(db_session, reviewer_role, email="arm-author@example.com")
    await _caller(db_session, reviewer_role, email="arm-other@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    response = await auth_db_client.get(
        f"/api/v1/submissions/{flag.id}/assignable-reviewers",
        params={"search": "%"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"] == []


async def test_assignable_reviewers_excludes_already_assigned(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="ar2-author@example.com")
    b = await _caller(db_session, reviewer_role, email="ar2-b@example.com")
    c = await _caller(db_session, reviewer_role, email="ar2-c@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)

    assigned = await _assign(auth_db_client, token, flag.id, b.id)
    assert assigned.status_code == status.HTTP_201_CREATED

    response = await _assignable(auth_db_client, token, flag.id)

    ids = {item["id"] for item in response.json()["items"]}
    assert str(b.id) not in ids
    assert str(c.id) in ids


async def test_assignable_reviewers_invisible_flag_is_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    owner = await _caller(db_session, reviewer_role, email="ar3-owner@example.com")
    outsider = await _caller(db_session, reviewer_role, email="ar3-outsider@example.com")
    evaluation = await _evaluation_for(
        db_session, owner_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    flag = await _flag(db_session, created_by_id=owner.id, evaluation=evaluation)

    response = await _assignable(auth_db_client, _token(outsider), flag.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_assignable_reviewers_empty_for_decided_flag(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    # A decided flag takes no new reviewers (assign 409s), so the picker must offer none.
    caller = await _caller(db_session, reviewer_role, email="ar-decided@example.com")
    await _caller(db_session, reviewer_role, email="ar-decided-b@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    flag.status = FlagStatus.APPROVED
    db_session.add(flag)
    await db_session.flush()

    response = await _assignable(auth_db_client, _token(caller), flag.id)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"] == []


async def _workload(client: AsyncClient, token: str, flag_id: UUID, user_id: UUID) -> int:
    response = await _assignable(client, token, flag_id)
    assert response.status_code == status.HTTP_200_OK
    by_id = {item["id"]: item for item in response.json()["items"]}
    return by_id[str(user_id)]["active_review_count"]


async def test_assignable_reviewers_report_open_work_per_candidate(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="wl-author@example.com")
    busy = await _caller(db_session, reviewer_role, email="wl-busy@example.com")
    idle = await _caller(db_session, reviewer_role, email="wl-idle@example.com")
    evaluation = await _evaluation_for(db_session)
    elsewhere = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    await _assign(auth_db_client, token, elsewhere.id, busy.id)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)

    assert await _workload(auth_db_client, token, target.id, busy.id) == 1
    # Missing from the aggregate must still answer zero, not omit the user.
    assert await _workload(auth_db_client, token, target.id, idle.id) == 0


async def test_assignable_reviewers_workload_ignores_decided_reviews(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="wld-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="wld-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    done = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    created = await _assign(auth_db_client, token, done.id, reviewer.id)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 1

    await auth_db_client.patch(
        f"/api/v1/reviews/{created.json()['id']}",
        json={"status": "approved", "successful_exploit": False},
        headers=_auth(_token(reviewer)),
    )

    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 0


async def test_assignable_reviewers_workload_drops_an_unassigned_reviewer(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="wlu-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="wlu-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    created = await _assign(auth_db_client, token, flag.id, reviewer.id)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 1

    # Unassign tombstones the row rather than deleting it; the workload must not keep counting it.
    await auth_db_client.delete(f"/api/v1/reviews/{created.json()['id']}", headers=_auth(token))

    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 0


async def test_assignable_reviewers_workload_ignores_closed_context(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="wlc-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="wlc-rv@example.com")
    # Two contexts so each can be closed on its own axis: a finished evaluation, a retired group.
    finished_eval = await _evaluation_for(db_session)
    closed_group_eval = await _evaluation_for(db_session)
    in_finished = await _flag(db_session, created_by_id=caller.id, evaluation=finished_eval)
    in_closed_group = await _flag(db_session, created_by_id=caller.id, evaluation=closed_group_eval)
    token = _token(caller)
    await _assign(auth_db_client, token, in_finished.id, reviewer.id)
    await _assign(auth_db_client, token, in_closed_group.id, reviewer.id)
    live_eval = await _evaluation_for(db_session)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=live_eval)
    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 2

    finished_eval.status = EvaluationStatus.COMPLETED
    db_session.add(finished_eval)
    group = await db_session.get(EvaluationGroup, closed_group_eval.evaluation_group_id)
    assert group is not None
    group.status = PublicationStatus.INACTIVE
    db_session.add(group)
    await db_session.flush()

    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 0


@pytest.mark.parametrize(
    ("tombstone", "expected"),
    [
        pytest.param("conversation", 0, id="conversation_tombstoned"),
        pytest.param("flag", 0, id="flag_tombstoned"),
        pytest.param("evaluation", 0, id="evaluation_tombstoned"),
        pytest.param("group", 0, id="group_tombstoned"),
        pytest.param(None, 1, id="everything_live"),
    ],
)
async def test_assignable_reviewers_workload_follows_the_whole_parent_chain(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reviewer_role: Role,
    tombstone: str | None,
    expected: int,
) -> None:
    # Every review surface reads through flag -> conversation -> evaluation -> group and hides a row
    # whose chain is broken anywhere; the workload has to agree, or it reports load nobody can act on.
    caller = await _caller(db_session, reviewer_role, email=f"wlc-{tombstone}-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email=f"wlc-{tombstone}-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, reviewer.id)
    live_eval = await _evaluation_for(db_session)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=live_eval)
    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 1

    if tombstone is not None:
        row = {
            "conversation": await db_session.get(Conversation, flag.conversation_id),
            "flag": flag,
            "evaluation": evaluation,
            "group": await db_session.get(EvaluationGroup, evaluation.evaluation_group_id),
        }[tombstone]
        assert row is not None
        row.soft_delete(caller.id)
        db_session.add(row)
        await db_session.flush()

    assert await _workload(auth_db_client, token, target.id, reviewer.id) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(EvaluationStatus.REJECTED, 0, id="rejected"),
        pytest.param(EvaluationStatus.COMPLETED, 0, id="completed"),
        pytest.param(EvaluationStatus.PUBLISHED, 1, id="published"),
    ],
)
async def test_assignable_reviewers_workload_drops_terminal_evaluation_states(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reviewer_role: Role,
    status: EvaluationStatus,
    expected: int,
) -> None:
    caller = await _caller(db_session, reviewer_role, email=f"wte-{status.value}-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email=f"wte-{status.value}-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, reviewer.id)
    live_eval = await _evaluation_for(db_session)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=live_eval)

    evaluation.status = status
    db_session.add(evaluation)
    await db_session.flush()

    assert await _workload(auth_db_client, token, target.id, reviewer.id) == expected


async def test_assignable_reviewers_workload_drops_a_not_approved_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="wna-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="wna-rv@example.com")
    evaluation = await _evaluation_for(db_session)
    flag = await _flag(db_session, created_by_id=caller.id, evaluation=evaluation)
    token = _token(caller)
    await _assign(auth_db_client, token, flag.id, reviewer.id)
    live_eval = await _evaluation_for(db_session)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=live_eval)
    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 1

    group = await db_session.get(EvaluationGroup, evaluation.evaluation_group_id)
    assert group is not None
    group.status = PublicationStatus.NOT_APPROVED
    db_session.add(group)
    await db_session.flush()

    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 0


async def test_assignable_reviewers_workload_counts_work_the_caller_cannot_see(
    auth_db_client: AsyncClient, db_session: AsyncSession, reviewer_role: Role
) -> None:
    caller = await _caller(db_session, reviewer_role, email="wlx-author@example.com")
    reviewer = await _caller(db_session, reviewer_role, email="wlx-rv@example.com")
    owner = await _caller(db_session, reviewer_role, email="wlx-owner@example.com")
    # Open work parked in an invitation-only group the caller is not a member of. The row is written
    # directly because such a group's assignable pool is its own members — even the owner gets a 404
    # for a non-member reviewer — and this count is documented to look past exactly that work.
    hidden = await _evaluation_for(
        db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY, owner_id=owner.id
    )
    hidden_flag = await _flag(db_session, created_by_id=owner.id, evaluation=hidden)
    db_session.add(
        Review(
            message_flag_id=hidden_flag.id,
            reviewer_id=reviewer.id,
            assigned_by_id=owner.id,
            evaluation_id=hidden.id,
        )
    )
    await db_session.flush()
    visible = await _evaluation_for(db_session)
    target = await _flag(db_session, created_by_id=caller.id, evaluation=visible)
    token = _token(caller)

    hidden_queue = await auth_db_client.get(f"/api/v1/review-queue?evaluation_id={hidden.id}", headers=_auth(token))

    # The caller cannot see that work at all…
    assert [item["submission"]["id"] for item in hidden_queue.json()["items"]] == []
    # …and the count still answers how loaded the person is, which is what the field promises.
    assert await _workload(auth_db_client, token, target.id, reviewer.id) == 1


# 401 (unauthenticated) and 403 (no `reviews:create`) are covered generically by the
# auto-discovering guard sweep in test_endpoint_guards.py — not re-asserted per-route here.
