"""Integration tests for the `/v1/evaluations/{id}/models/{assignment_id}/warmup` route.

`dispatch_probe` is monkeypatched so no real provider call fires — these exercise
the gate, the conversation-style visibility scope, and the masking-safe response shape.
"""

from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.enums import WarmupStatus
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.users import create_user as create_user_service
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group


@pytest_asyncio.fixture
async def warmer_role(db_session: AsyncSession) -> Role:
    """Holds `conversations:update` — the same gate the message-send path uses."""
    role = Role(
        name="warmer",
        description="Conversations update",
        permissions=[Permission.CONVERSATIONS_UPDATE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def noperm_role(db_session: AsyncSession) -> Role:
    role = Role(name="noperm-warm", description="No permissions", permissions=[])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def ai_model(db_session: AsyncSession) -> AiModel:
    row = AiModel(
        name="Local SLM",
        model_alias="local-slm",
        provider=ProviderVendor.GENERIC,
        provider_model_id="qwen2.5-0.5b-instruct",
        inference_endpoint="http://slm:8080/v1",
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _evaluation(
    db_session: AsyncSession,
    *,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
    owner_id: UUID | None = None,
) -> Evaluation:
    group = await persist_evaluation_group(db_session, access_level=access_level, created_by_id=owner_id)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _assignment(db_session: AsyncSession, evaluation: Evaluation, model: AiModel) -> EvaluationAiModel:
    row = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _probe(result: WarmupStatus, calls: list[str] | None = None):
    async def _fake(
        _session: object,
        _settings: object,
        *,
        model_alias: str,
        provider: object = None,
        session_provider: object = None,
    ) -> WarmupStatus:
        if calls is not None:
            calls.append(model_alias)
        return result

    return _fake


@pytest.mark.integration
async def test_warmup_returns_ready_and_probes_the_assigned_model(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    warmer_role: Role,
    ai_model: AiModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation = await _evaluation(db_session)
    assignment = await _assignment(db_session, evaluation, ai_model)
    caller = await _caller(db_session, warmer_role, email="warm-ready@example.com")
    calls: list[str] = []
    monkeypatch.setattr("app.api.v1.model_warmup.dispatch_probe", _probe(WarmupStatus.READY, calls))

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}/warmup",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ready"}
    assert calls == ["local-slm"]  # resolved the assignment → the model's dispatch alias


@pytest.mark.integration
@pytest.mark.parametrize("result", [WarmupStatus.STARTING, WarmupStatus.ERROR])
async def test_warmup_passes_through_probe_status(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    warmer_role: Role,
    ai_model: AiModel,
    monkeypatch: pytest.MonkeyPatch,
    result: WarmupStatus,
) -> None:
    evaluation = await _evaluation(db_session)
    assignment = await _assignment(db_session, evaluation, ai_model)
    caller = await _caller(db_session, warmer_role, email=f"warm-{result.value}@example.com")
    monkeypatch.setattr("app.api.v1.model_warmup.dispatch_probe", _probe(result))

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}/warmup",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    # Response carries only the opaque status — no provider/model identity leaks (masking-safe).
    assert response.json() == {"status": result.value}


@pytest.mark.integration
async def test_warmup_missing_assignment_returns_404_without_probing(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    warmer_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation = await _evaluation(db_session)
    caller = await _caller(db_session, warmer_role, email="warm-missing@example.com")
    calls: list[str] = []
    monkeypatch.setattr("app.api.v1.model_warmup.dispatch_probe", _probe(WarmupStatus.READY, calls))

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models/{uuid4()}/warmup",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert calls == []  # resolution fails before any provider call


@pytest.mark.integration
async def test_warmup_hidden_evaluation_returns_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    warmer_role: Role,
    ai_model: AiModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = await _caller(db_session, warmer_role, email="warm-owner@example.com")
    evaluation = await _evaluation(
        db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY, owner_id=other.id
    )
    assignment = await _assignment(db_session, evaluation, ai_model)
    caller = await _caller(db_session, warmer_role, email="warm-outsider@example.com")
    calls: list[str] = []
    monkeypatch.setattr("app.api.v1.model_warmup.dispatch_probe", _probe(WarmupStatus.READY, calls))

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}/warmup",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert calls == []  # a hidden evaluation must 404 before waking its model


@pytest.mark.integration
async def test_warmup_requires_conversations_update(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    noperm_role: Role,
    ai_model: AiModel,
) -> None:
    evaluation = await _evaluation(db_session)
    assignment = await _assignment(db_session, evaluation, ai_model)
    caller = await _caller(db_session, noperm_role, email="warm-noperm@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}/warmup",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_warmup_accepts_in_group_authority_without_the_global_permission(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    noperm_role: Role,
    system_roles: dict[str, Role],
    ai_model: AiModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group-scoped red teamer must clear warmup, or the composer never unblocks for them."""
    evaluation = await _evaluation(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    assignment = await _assignment(db_session, evaluation, ai_model)
    caller = await _caller(db_session, noperm_role, email="warm-in-group@example.com")
    await grant_roles(
        db_session,
        ObjectType.EVALUATION_GROUP,
        evaluation.evaluation_group_id,
        caller.id,
        [system_roles[SystemRole.RED_TEAMER.value]],
    )
    await db_session.flush()
    monkeypatch.setattr("app.api.v1.model_warmup.dispatch_probe", _probe(WarmupStatus.READY))

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}/warmup",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
