"""API tests for `POST /api/v1/evaluations/{id}/approve` and `/reject`.

Auth is faked by overriding `current_user` (what `require_permission` resolves
transitively) with `session_user_from` — no JWT minting needed.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dependencies import current_user
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.models import Evaluation
from app.main import app
from tests.conftest import persist_evaluation_group
from tests.conftest import session_user_from

pytestmark = pytest.mark.integration


@contextmanager
def as_user(user: User) -> Iterator[None]:
    app.dependency_overrides[current_user] = lambda: session_user_from(user)
    try:
        yield
    finally:
        app.dependency_overrides.pop(current_user, None)


async def _role(db: AsyncSession, permissions: list[str]) -> Role:
    role = Role(name=f"role-{uuid4().hex[:8]}", description="test", permissions=permissions)
    db.add(role)
    await db.flush()
    await db.refresh(role)
    return role


async def _user(db: AsyncSession, role: Role) -> User:
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role])


async def _evaluation(
    db: AsyncSession,
    *,
    status_: EvaluationStatus = EvaluationStatus.UNDER_REVIEW,
    rejection_reason: str | None = None,
) -> Evaluation:
    group = await persist_evaluation_group(db)
    evaluation = Evaluation(
        title="eval",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        status=status_,
        rejection_reason=rejection_reason,
    )
    db.add(evaluation)
    await db.flush()
    await db.refresh(evaluation)
    return evaluation


async def _approver(db: AsyncSession) -> User:
    return await _user(db, await _role(db, ["evaluations:approve"]))


async def test_approve_moves_under_review_to_approved(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    approver = await _approver(db_session)
    evaluation = await _evaluation(db_session, rejection_reason="stale note")

    with as_user(approver):
        response = await async_client_with_db.post(f"/api/v1/evaluations/{evaluation.id}/approve")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "approved"
    assert body["rejection_reason"] is None


async def test_reject_moves_under_review_to_rejected(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    approver = await _approver(db_session)
    evaluation = await _evaluation(db_session)

    with as_user(approver):
        response = await async_client_with_db.post(
            f"/api/v1/evaluations/{evaluation.id}/reject",
            json={"rejection_reason": "Off-topic for this engagement."},
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "rejected"
    assert body["rejection_reason"] == "Off-topic for this engagement."


async def test_approve_without_permission_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await _user(db_session, await _role(db_session, ["evaluations:read"]))
    evaluation = await _evaluation(db_session)

    with as_user(reader):
        response = await async_client_with_db.post(f"/api/v1/evaluations/{evaluation.id}/approve")

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_approve_unknown_evaluation_returns_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    approver = await _approver(db_session)

    with as_user(approver):
        response = await async_client_with_db.post(f"/api/v1/evaluations/{uuid4()}/approve")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_approve_wrong_state_returns_409(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    approver = await _approver(db_session)
    evaluation = await _evaluation(db_session, status_=EvaluationStatus.DRAFT)

    with as_user(approver):
        response = await async_client_with_db.post(f"/api/v1/evaluations/{evaluation.id}/approve")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("payload", [{}, {"rejection_reason": ""}, {"rejection_reason": "   "}])
async def test_reject_without_reason_returns_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession, payload: dict[str, str]
) -> None:
    approver = await _approver(db_session)
    evaluation = await _evaluation(db_session)

    with as_user(approver):
        response = await async_client_with_db.post(f"/api/v1/evaluations/{evaluation.id}/reject", json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
