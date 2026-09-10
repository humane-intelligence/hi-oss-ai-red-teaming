"""API tests for `POST /api/v1/evaluation-groups/{id}/submit|approve|request-changes|reject|publish|finish`.

Auth is faked by overriding `current_user` (what `require_permission` resolves
transitively) with `session_user_from` — no JWT minting needed.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.dependencies import current_user
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationGroupAiModel
from app.core.evaluations.models import Scenario
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


async def _user(db: AsyncSession, permissions: list[str]) -> User:
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[await _role(db, permissions)])


def _future(days: int) -> date:
    """A date `days` from today — submit re-imposes `start_date >= today`, so a
    submittable group needs a non-past start date."""
    return (datetime.now(UTC) + timedelta(days=days)).date()


async def _attach_model(db: AsyncSession, group: EvaluationGroup) -> AiModel:
    """Give `group` one allowed model so it satisfies the submit gate (≥1 model)."""
    slug = f"m-{uuid4().hex[:8]}"
    model = AiModel(
        name=slug,
        model_alias=slug,
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
    )
    db.add(model)
    await db.flush()
    db.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db.flush()
    return model


async def _attach_playable_evaluation(db: AsyncSession, group: EvaluationGroup) -> Evaluation:
    """Give `group` one evaluation with one scenario so it satisfies the publish gate."""
    evaluation = Evaluation(title="e", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db.add(evaluation)
    await db.flush()
    db.add(Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0))
    await db.flush()
    return evaluation


async def _owner(db: AsyncSession) -> User:
    return await _user(db, ["evaluation_groups:update"])


async def _manager(db: AsyncSession) -> User:
    return await _user(db, ["evaluation_groups:update", "evaluation_groups:manage"])


async def test_publish_moves_approved_to_published(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id, status=PublicationStatus.APPROVED)
    await _attach_playable_evaluation(db_session, group)

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/publish")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "published"


async def test_finish_moves_published_to_inactive(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id, status=PublicationStatus.PUBLISHED)

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/finish")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "inactive"


async def test_reject_moves_pending_approval_to_not_approved(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)

    with as_user(manager):
        response = await async_client_with_db.post(
            f"/api/v1/evaluation-groups/{group.id}/reject",
            json={"rejection_reason": "Engagement scope too broad."},
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "not_approved"
    assert body["rejection_reason"] == "Engagement scope too broad."


async def test_manage_publishes_someone_elses_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(
        db_session,
        status=PublicationStatus.APPROVED,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
    )
    await _attach_playable_evaluation(db_session, group)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/publish")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "published"


async def test_manage_finishes_someone_elses_group(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(
        db_session,
        status=PublicationStatus.PUBLISHED,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
    )

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/finish")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "inactive"


async def test_publish_without_permission_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await _user(db_session, ["evaluation_groups:read"])
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)

    with as_user(reader):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/publish")

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_publish_by_non_owner_of_public_group_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _owner(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/publish")

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_publish_invisible_private_group_returns_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _owner(db_session)
    group = await persist_evaluation_group(
        db_session,
        status=PublicationStatus.APPROVED,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
    )

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/publish")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_reject_without_manage_returns_403(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, status=PublicationStatus.PENDING_APPROVAL
    )

    with as_user(owner):
        response = await async_client_with_db.post(
            f"/api/v1/evaluation-groups/{group.id}/reject",
            json={"rejection_reason": "x"},
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_publish_unknown_group_returns_404(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    manager = await _manager(db_session)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{uuid4()}/publish")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_publish_wrong_state_returns_409(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id, status=PublicationStatus.DRAFT)

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/publish")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_finish_wrong_state_returns_409(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id, status=PublicationStatus.APPROVED)

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/finish")

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_reject_wrong_state_returns_409(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.DRAFT)

    with as_user(manager):
        response = await async_client_with_db.post(
            f"/api/v1/evaluation-groups/{group.id}/reject",
            json={"rejection_reason": "x"},
        )

    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.parametrize("payload", [{}, {"rejection_reason": ""}, {"rejection_reason": "   "}])
async def test_reject_without_reason_returns_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession, payload: dict[str, str]
) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/reject", json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_submit_moves_draft_to_pending_approval(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, status=PublicationStatus.DRAFT, start_date=_future(7)
    )
    await _attach_model(db_session, group)

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/submit")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "pending_approval"


async def test_submit_resubmits_changes_requested_to_pending_approval(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, status=PublicationStatus.CHANGES_REQUESTED, start_date=_future(7)
    )
    await _attach_model(db_session, group)

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/submit")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "pending_approval"


async def test_manage_submits_someone_elses_group(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(
        db_session,
        status=PublicationStatus.DRAFT,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        start_date=_future(7),
    )
    await _attach_model(db_session, group)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/submit")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "pending_approval"


@pytest.mark.usefixtures("system_roles")
async def test_submit_incomplete_draft_returns_400(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # A draft saved with only a title can't enter review: submit re-imposes the
    # completeness (required fields) the draft path skipped.
    caller = await _user(db_session, ["evaluation_groups:create", "evaluation_groups:update"])

    with as_user(caller):
        draft = await async_client_with_db.post("/api/v1/evaluation-groups/draft", json={"title": "Thin draft"})
        assert draft.status_code == status.HTTP_201_CREATED
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{draft.json()['id']}/submit")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.usefixtures("system_roles")
async def test_submit_draft_with_past_start_date_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Completeness includes the create rule `start_date >= today` (full parity), even
    # though a plain edit allows backdating — a group can't enter review opening in the past.
    caller = await _user(db_session, ["evaluation_groups:create", "evaluation_groups:update"])

    with as_user(caller):
        draft = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={
                "title": "Backdated",
                "description": "Complete but starts in the past.",
                "start_date": _future(-1).isoformat(),
            },
        )
        assert draft.status_code == status.HTTP_201_CREATED
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{draft.json()['id']}/submit")

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.usefixtures("system_roles")
async def test_submit_completed_draft_succeeds(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # The whole flow: save a thin draft, fill it to completeness, submit — the gate passes.
    caller = await _user(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    slug = f"m-{uuid4().hex[:8]}"
    model = AiModel(
        name=slug, model_alias=slug, provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5-sonnet"
    )
    db_session.add(model)
    await db_session.flush()

    with as_user(caller):
        draft = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={
                "title": "Complete",
                "description": "All set.",
                "start_date": _future(7).isoformat(),
                "allowed_model_ids": [str(model.id)],
            },
        )
        assert draft.status_code == status.HTTP_201_CREATED
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{draft.json()['id']}/submit")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "pending_approval"


@pytest.mark.usefixtures("system_roles")
async def test_submit_with_evaluation_missing_scenario_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Completeness includes the scenario gate: every live evaluation of the group
    # must carry at least one live scenario before the group can enter review.
    caller = await _user(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    slug = f"m-{uuid4().hex[:8]}"
    model = AiModel(
        name=slug, model_alias=slug, provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5-sonnet"
    )
    db_session.add(model)
    await db_session.flush()

    with as_user(caller):
        draft = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={
                "title": "Complete but bare",
                "description": "All set, except an evaluation with no scenario.",
                "start_date": _future(7).isoformat(),
                "allowed_model_ids": [str(model.id)],
            },
        )
        assert draft.status_code == status.HTTP_201_CREATED
        db_session.add(
            Evaluation(title="bare", description="d", evaluation_group_id=draft.json()["id"], created_by_id=caller.id)
        )
        await db_session.flush()
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{draft.json()['id']}/submit")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "bare" in response.json()["detail"]


async def test_publish_with_evaluation_missing_scenario_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The submit-time scenario gate is re-checked at publish (a scenario can be
    # deleted while the group sits in review).
    owner = await _owner(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id, status=PublicationStatus.APPROVED)
    evaluation = Evaluation(title="bare", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db_session.add(evaluation)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    scenario.soft_delete(None)
    db_session.add(scenario)
    await db_session.flush()

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/publish")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "bare" in response.json()["detail"]


async def test_submit_by_non_owner_of_public_group_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _owner(db_session)
    # A public *draft* is owner/manager-only, so it would 404 a non-owner;
    # use a `changes_requested` group — still submittable, but visible via the
    # public arm — to exercise the visible-but-insufficient → 403 path.
    group = await persist_evaluation_group(db_session, status=PublicationStatus.CHANGES_REQUESTED)

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/submit")

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_submit_invisible_private_group_returns_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _owner(db_session)
    group = await persist_evaluation_group(
        db_session,
        status=PublicationStatus.DRAFT,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
    )

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/submit")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_submit_wrong_state_returns_409(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id, status=PublicationStatus.APPROVED)

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/submit")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_submit_unknown_group_returns_404(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    manager = await _manager(db_session)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{uuid4()}/submit")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_approve_moves_pending_approval_to_approved(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/approve")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "approved"


async def test_owner_approves_own_group(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # An owner may approve their OWN group (owner-or-manage) — no separate
    # moderator is required for a self-submitted group.
    owner = await _owner(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, status=PublicationStatus.PENDING_APPROVAL
    )

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/approve")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "approved"


async def test_approve_non_owner_with_update_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `update` alone isn't enough: a caller who is neither the group's owner nor a
    # manager cannot approve it (owner-or-manage, not "anyone with update").
    owner = await _owner(db_session)
    other = await _owner(db_session)  # holds global update, but not THIS group's owner role
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, status=PublicationStatus.PENDING_APPROVAL
    )

    with as_user(other):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/approve")

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_approve_wrong_state_returns_409(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.DRAFT)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/approve")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_approve_unknown_group_returns_404(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    manager = await _manager(db_session)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{uuid4()}/approve")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_owner_with_manage_approves_own_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A caller who both owns the group and holds `manage` CAN approve it — both arms
    # of owner-or-manage are satisfied.
    manager = await _manager(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=manager.id, status=PublicationStatus.PENDING_APPROVAL
    )

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/approve")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "approved"


async def test_request_changes_moves_pending_approval_to_changes_requested(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/request-changes")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "changes_requested"


async def test_request_changes_then_owner_resubmits_returns_to_pending(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The rework loop: a moderator bounces the group back, the owner resubmits.
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, status=PublicationStatus.PENDING_APPROVAL, start_date=_future(7)
    )
    await _attach_model(db_session, group)

    with as_user(manager):
        bounced = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/request-changes")
    assert bounced.status_code == status.HTTP_200_OK
    assert bounced.json()["status"] == "changes_requested"

    with as_user(owner):
        resubmitted = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/submit")
    assert resubmitted.status_code == status.HTTP_200_OK
    assert resubmitted.json()["status"] == "pending_approval"


async def test_request_changes_without_manage_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _owner(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, status=PublicationStatus.PENDING_APPROVAL
    )

    with as_user(owner):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/request-changes")

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_request_changes_wrong_state_returns_409(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.DRAFT)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/request-changes")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_request_changes_unknown_group_returns_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await _manager(db_session)

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{uuid4()}/request-changes")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_approve_clears_stale_rejection_reason(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A positive verdict must not carry a leftover rejection reason on the wire
    # (mirrors approve_evaluation in services/approval.py).
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    group.rejection_reason = "left over from an earlier cycle"
    db_session.add(group)
    await db_session.flush()

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/approve")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "approved"
    assert body["rejection_reason"] is None


async def test_request_changes_clears_stale_rejection_reason(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Bouncing back for edits also drops any stale reason — the verdict is non-terminal.
    manager = await _manager(db_session)
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    group.rejection_reason = "left over from an earlier cycle"
    db_session.add(group)
    await db_session.flush()

    with as_user(manager):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{group.id}/request-changes")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "changes_requested"
    assert body["rejection_reason"] is None
