"""Integration tests for the `/v1/evaluations/{id}/models` assignment router."""

from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroupAiModel
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group


@pytest_asyncio.fixture
async def updater_role(db_session: AsyncSession) -> Role:
    """Read + update, plus `evaluation_groups:manage`.

    The manage elevation lifts the in-group-role write gate, so the assignment
    *mechanics* tests below stay authorized regardless of the caller's role in
    the parent group. The gate itself is exercised by the dedicated tests at the
    bottom of this module, which use `owner_updater_role` (no manage).
    """
    role = Role(
        name="evaluator",
        description="Evaluation read + update + manage",
        permissions=[
            Permission.EVALUATIONS_READ.value,
            Permission.EVALUATIONS_UPDATE.value,
            Permission.EVALUATION_GROUPS_MANAGE.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def owner_updater_role(db_session: AsyncSession) -> Role:
    """Read + update only — subject to the parent group's in-group-role write gate."""
    role = Role(
        name="evaluator-no-manage",
        description="Evaluation read + update",
        permissions=[Permission.EVALUATIONS_READ.value, Permission.EVALUATIONS_UPDATE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    role = Role(name="eval-reader", description="Read-only", permissions=[Permission.EVALUATIONS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def evaluation(db_session: AsyncSession, ai_model: AiModel) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    # Allow the shared `ai_model` in the group so assignment tests pass the subset gate.
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=ai_model.id))
    await db_session.flush()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def ai_model(db_session: AsyncSession) -> AiModel:
    row = AiModel(
        name="Claude",
        model_alias="claude-3-5-sonnet",
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def unmasked_evaluation(db_session: AsyncSession, ai_model: AiModel) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    row = Evaluation(
        title="Open",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        mask_models_enabled=False,
    )
    db_session.add(row)
    await db_session.flush()
    # Allow the shared `ai_model` in the group so assignment tests pass the subset gate.
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=ai_model.id))
    await db_session.flush()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def noperm_role(db_session: AsyncSession) -> Role:
    role = Role(name="noperm", description="No evaluation permissions", permissions=[])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _persist_model(
    db_session: AsyncSession, *, name: str, provider: ProviderVendor = ProviderVendor.ANTHROPIC
) -> AiModel:
    row = AiModel(name=name, model_alias=name, provider=provider, provider_model_id=f"{name}-id")
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


@pytest.mark.integration
async def test_assign_model_returns_201_and_location(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="assign@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models",
        json={"model_id": str(ai_model.id), "model_display_mask": "Model A", "parameters": {"temperature": 0.2}},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["evaluation_id"] == str(evaluation.id)
    assert body["model_id"] == str(ai_model.id)
    assert body["model_display_mask"] == "Model A"
    assert body["parameters"] == {"temperature": 0.2}
    assert response.headers["Location"] == f"/api/v1/evaluations/{evaluation.id}/models/{body['id']}"


@pytest.mark.integration
async def test_assign_model_not_in_group_subset_returns_400(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
) -> None:
    # The `evaluation` fixture's group allows only its `ai_model`; a different model is rejected.
    caller = await _caller(db_session, updater_role, email="outsider@example.com")
    outsider = await _persist_model(db_session, name="Outsider")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models",
        json={"model_id": str(outsider.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.integration
async def test_assign_model_empty_subset_returns_400(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    ai_model: AiModel,
) -> None:
    # Fail-closed: a group with no allowed-model subset accepts no assignments.
    caller = await _caller(db_session, updater_role, email="emptysubset@example.com")
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models",
        json={"model_id": str(ai_model.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.integration
async def test_get_assignment_returns_200(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="get@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assignment_id = (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/models", json={"model_id": str(ai_model.id)}, headers=headers
        )
    ).json()["id"]

    response = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}", headers=headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == assignment_id
    assert response.json()["model_id"] == str(ai_model.id)


@pytest.mark.integration
async def test_get_assignment_forbidden_when_not_group_owner(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
    ai_model: AiModel,
) -> None:
    # An update-holder who neither owns the (public) evaluation nor can manage is
    # forbidden the unmasked by-id view — model identities reach them only through
    # the masked list.
    other = await _caller(db_session, owner_updater_role, email="gaf-owner@example.com")
    evaluation = await _evaluation_in_group(
        db_session, owner_id=other.id, access_level=EvaluationGroupAccessLevel.PUBLIC
    )
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=ai_model.id)
    db_session.add(assignment)
    await db_session.flush()
    caller = await _caller(db_session, owner_updater_role, email="gaf-notowner@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_update_assignment_changes_mask_and_parameters(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="patch@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assignment_id = (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/models",
            json={"model_id": str(ai_model.id), "model_display_mask": "old"},
            headers=headers,
        )
    ).json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}",
        json={"model_display_mask": "new", "parameters": {"top_p": 0.9}},
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["model_display_mask"] == "new"
    assert body["parameters"] == {"top_p": 0.9}


@pytest.mark.integration
async def test_update_assignment_strips_null_valued_knobs(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    """A knob set to `null` is dropped, not persisted — matching the assign path's `exclude_none`."""
    caller = await _caller(db_session, updater_role, email="nullknob@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assignment_id = (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/models", json={"model_id": str(ai_model.id)}, headers=headers
        )
    ).json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}",
        json={"parameters": {"temperature": 0.5, "top_p": None}},
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["parameters"] == {"temperature": 0.5}


@pytest.mark.integration
async def test_update_assignment_clears_mask_with_explicit_null(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="clearmask@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assignment_id = (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/models",
            json={"model_id": str(ai_model.id), "model_display_mask": "set"},
            headers=headers,
        )
    ).json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}",
        json={"model_display_mask": None},
        headers=headers,
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["model_display_mask"] is None


@pytest.mark.integration
async def test_update_assignment_rejects_explicit_null_parameters(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="nullparams@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assignment_id = (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/models", json={"model_id": str(ai_model.id)}, headers=headers
        )
    ).json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}",
        json={"parameters": None},
        headers=headers,
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("parameters" in error["loc"] for error in response.json()["errors"])


@pytest.mark.integration
async def test_unassign_model_returns_204_then_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="delete@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assignment_id = (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/models", json={"model_id": str(ai_model.id)}, headers=headers
        )
    ).json()["id"]

    delete_response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}", headers=headers
    )
    get_response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}", headers=headers
    )

    assert delete_response.status_code == status.HTTP_204_NO_CONTENT
    assert get_response.status_code == status.HTTP_404_NOT_FOUND


async def _evaluation_in_group(
    db_session: AsyncSession,
    *,
    owner_id: UUID,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
) -> Evaluation:
    """Build an evaluation whose parent group is owned by ``owner_id``."""
    group = await persist_evaluation_group(db_session, access_level=access_level, created_by_id=owner_id)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


@pytest.mark.integration
async def test_assign_model_owner_of_parent_group_succeeds(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
    ai_model: AiModel,
) -> None:
    # No manage elevation: the caller is authorized purely by owning the parent group.
    caller = await _caller(db_session, owner_updater_role, email="owner-assign@example.com")
    evaluation = await _evaluation_in_group(db_session, owner_id=caller.id)
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=evaluation.evaluation_group_id, model_id=ai_model.id))
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models",
        json={"model_id": str(ai_model.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED


@pytest.mark.integration
async def test_assign_model_forbidden_when_not_group_owner(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    # `evaluation` fixture's group is owned by a throwaway user; a non-owner without
    # manage may not mutate its assignments even with `evaluations:update`.
    caller = await _caller(db_session, owner_updater_role, email="notowner-assign@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models",
        json={"model_id": str(ai_model.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_assign_model_private_group_not_owned_returns_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
    ai_model: AiModel,
) -> None:
    other = await _caller(db_session, owner_updater_role, email="otherowner@example.com")
    evaluation = await _evaluation_in_group(
        db_session, owner_id=other.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    caller = await _caller(db_session, owner_updater_role, email="notowner-assign-private@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/models",
        json={"model_id": str(ai_model.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_get_assignment_hidden_when_evaluation_not_visible(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
    ai_model: AiModel,
) -> None:
    # The assignment inherits the parent evaluation's visibility: a reader who
    # can't see the private-group evaluation can't read its assignment by id (and
    # so can't turn a masked view's assignment_id back into a real model_id).
    other = await _caller(db_session, owner_updater_role, email="ga-owner@example.com")
    evaluation = await _evaluation_in_group(
        db_session, owner_id=other.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=ai_model.id)
    db_session.add(assignment)
    await db_session.flush()
    caller = await _caller(db_session, owner_updater_role, email="ga-reader@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_get_assignment_manager_sees_others_private(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
    updater_role: Role,
    ai_model: AiModel,
) -> None:
    other = await _caller(db_session, owner_updater_role, email="gam-owner@example.com")
    evaluation = await _evaluation_in_group(
        db_session, owner_id=other.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=ai_model.id)
    db_session.add(assignment)
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="gam-manager@example.com")  # updater_role carries manage

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(assignment.id)


@pytest.mark.integration
async def test_unassign_model_forbidden_when_not_group_owner(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    # A manager (updater_role has manage) seeds the assignment; a non-owner without
    # manage is then refused the unassign.
    manager = await _caller(db_session, updater_role, email="seed-manager@example.com")
    assignment_id = (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/models",
            json={"model_id": str(ai_model.id)},
            headers={"Authorization": f"Bearer {_token(manager)}"},
        )
    ).json()["id"]
    caller = await _caller(db_session, owner_updater_role, email="notowner-unassign@example.com")

    response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment_id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def _assign(
    client: AsyncClient, evaluation: Evaluation, model: AiModel, headers: dict[str, str], *, mask: str | None = None
) -> str:
    body: dict[str, object] = {"model_id": str(model.id)}
    if mask is not None:
        body["model_display_mask"] = mask
    response = await client.post(f"/api/v1/evaluations/{evaluation.id}/models", json=body, headers=headers)
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()["id"]


@pytest.mark.integration
async def test_list_models_returns_200_with_masked_items(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="listmasked@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assignment_id = await _assign(auth_db_client, evaluation, ai_model, headers, mask="Alpha")

    response = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/models", headers=headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    [item] = body["items"]
    assert item["assignment_id"] == assignment_id
    assert item["name"] == "Alpha"  # display mask, not the real name
    assert item["provider"] is None
    assert item["provider_model_id"] is None


@pytest.mark.integration
async def test_list_models_unmasked_exposes_identity(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    unmasked_evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="listunmasked@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await _assign(auth_db_client, unmasked_evaluation, ai_model, headers, mask="Alpha")

    response = await auth_db_client.get(f"/api/v1/evaluations/{unmasked_evaluation.id}/models", headers=headers)

    assert response.status_code == status.HTTP_200_OK
    [item] = response.json()["items"]
    assert item["name"] == "Claude"  # real name, mask ignored
    assert item["provider"] == ProviderVendor.ANTHROPIC.value
    assert item["provider_model_id"] == "claude-3-5-sonnet-20240620"


@pytest.mark.integration
async def test_list_models_never_carries_the_registry_note(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    unmasked_evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    """The note is registry metadata and this is the surface a red-teamer reads.

    Asserted on the body, not on `model_fields`, so it also fails if the note arrives
    through a future embed rather than through a field added to this projection. The
    unmasked evaluation is the harder case: identity is surfaced here, so nothing but
    an explicit omission keeps the note out.
    """
    ai_model.description = "Client Acme only \u2014 do not assign elsewhere."
    db_session.add(ai_model)
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="listnote@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await _assign(auth_db_client, unmasked_evaluation, ai_model, headers, mask="Alpha")

    response = await auth_db_client.get(f"/api/v1/evaluations/{unmasked_evaluation.id}/models", headers=headers)

    assert response.status_code == status.HTTP_200_OK
    [item] = response.json()["items"]
    assert item["name"] == "Claude"  # identity is surfaced here, so the omission is deliberate
    assert "description" not in item
    assert "Acme" not in response.text


@pytest.mark.integration
async def test_list_models_search_param_filters(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    # Wiring of ?search= into the service filter only — masked-vs-unmasked search
    # semantics live in the service tests.
    caller = await _caller(db_session, updater_role, email="searchwiring@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await _assign(auth_db_client, evaluation, ai_model, headers, mask="Alpha")

    hit = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/models?search=alph", headers=headers)
    miss = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/models?search=nope", headers=headers)

    assert hit.json()["total"] == 1
    assert miss.json()["total"] == 0


@pytest.mark.integration
async def test_list_models_paginates(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="paginate@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    second = await _persist_model(db_session, name="Gemini")
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=evaluation.evaluation_group_id, model_id=second.id))
    await db_session.flush()
    await _assign(auth_db_client, evaluation, ai_model, headers, mask="Bravo")
    await _assign(auth_db_client, evaluation, second, headers, mask="Alpha")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/models?order_by=name&limit=1&offset=0", headers=headers
    )

    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert [i["name"] for i in body["items"]] == ["Alpha"]


@pytest.mark.integration
async def test_list_models_excludes_soft_deleted(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="softdel@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    second = await _persist_model(db_session, name="Gemini")
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=evaluation.evaluation_group_id, model_id=second.id))
    await db_session.flush()
    keep = await _assign(auth_db_client, evaluation, ai_model, headers, mask="Bravo")
    drop = await _assign(auth_db_client, evaluation, second, headers, mask="Alpha")
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation.id}/models/{drop}", headers=headers)

    response = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/models", headers=headers)

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["assignment_id"] == keep


@pytest.mark.integration
async def test_list_models_unknown_evaluation_returns_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    caller = await _caller(db_session, reader_role, email="listnoeval@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{uuid4()}/models",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_models_not_visible_returns_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
    ai_model: AiModel,
) -> None:
    # Invitation-only group owned by someone else → not visible to the caller.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    hidden = Evaluation(
        title="Hidden", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(hidden)
    await db_session.flush()
    await db_session.refresh(hidden)
    caller = await _caller(db_session, reader_role, email="listhidden@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{hidden.id}/models",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def _restore_assignment(
    client: AsyncClient, evaluation_id: UUID, assignment_id: str, headers: dict[str, str]
) -> Response:
    return await client.post(f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}/restore", headers=headers)


@pytest.mark.integration
async def test_restore_assignment_makes_the_model_dispatchable_again(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    caller = await _caller(db_session, updater_role, email="restore-assign@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    evaluation_id = evaluation.id
    assignment_id = await _assign(auth_db_client, evaluation, ai_model, headers)
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}", headers=headers)

    response = await _restore_assignment(auth_db_client, evaluation_id, assignment_id, headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    follow_up = await auth_db_client.get(f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}", headers=headers)
    assert follow_up.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_restore_assignment_409s_when_the_model_was_reassigned(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    # `(model_id, evaluation_id)` is unique among live rows, so the fresh assignment
    # holds the slot and the tombstone cannot come back beside it.
    caller = await _caller(db_session, updater_role, email="reassigned@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    evaluation_id = evaluation.id
    assignment_id = await _assign(auth_db_client, evaluation, ai_model, headers)
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}", headers=headers)
    await _assign(auth_db_client, evaluation, ai_model, headers)

    response = await _restore_assignment(auth_db_client, evaluation_id, assignment_id, headers)

    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.integration
async def test_restore_assignment_404s_when_its_model_was_deleted(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    # The model-delete cascade unassigned it. Re-instating the assignment would point
    # the evaluation at a model that can never dispatch, so the tombstone reads as gone.
    caller = await _caller(db_session, updater_role, email="dead-model@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    evaluation_id = evaluation.id
    assignment_id = await _assign(auth_db_client, evaluation, ai_model, headers)
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}", headers=headers)
    ai_model.soft_delete(caller.id)
    await db_session.flush()

    response = await _restore_assignment(auth_db_client, evaluation_id, assignment_id, headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_deleted_assignment_listing_hides_one_whose_model_died(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    # The listing is the restore surface, so it must exclude what restore refuses.
    caller = await _caller(db_session, updater_role, email="deleted-assign-list@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    evaluation_id = evaluation.id
    assignment_id = await _assign(auth_db_client, evaluation, ai_model, headers)
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}", headers=headers)

    listed = await auth_db_client.get(f"/api/v1/evaluations/{evaluation_id}/models?deleted=true", headers=headers)
    assert [item["assignment_id"] for item in listed.json()["items"]] == [assignment_id]

    ai_model.soft_delete(caller.id)
    await db_session.flush()

    after = await auth_db_client.get(f"/api/v1/evaluations/{evaluation_id}/models?deleted=true", headers=headers)
    assert after.json()["items"] == []


@pytest.mark.integration
async def test_deleted_assignment_listing_needs_write_access(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    owner_updater_role: Role,
) -> None:
    # Reading the live list needs `evaluations:read`; the tombstone list is the restore
    # surface, so it is authorized as a write on the parent group — which a caller
    # holding `evaluations:update` but no in-group role does not have.
    owner = await _caller(db_session, owner_updater_role, email="group-owner-deleted@example.com")
    outsider = await _caller(db_session, owner_updater_role, email="outsider-deleted@example.com")
    evaluation = await _evaluation_in_group(db_session, owner_id=owner.id)

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/models?deleted=true",
        headers={"Authorization": f"Bearer {_token(outsider)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_restore_assignment_404s_through_another_evaluation(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    evaluation: Evaluation,
    ai_model: AiModel,
) -> None:
    # `evaluation_id` is part of the restore lookup, so write access to one evaluation
    # cannot re-instate another's assignment by swapping the path segment.
    caller = await _caller(db_session, updater_role, email="cross-parent-assignment@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    victim_id = evaluation.id
    assignment_id = await _assign(auth_db_client, evaluation, ai_model, headers)
    await auth_db_client.delete(f"/api/v1/evaluations/{victim_id}/models/{assignment_id}", headers=headers)
    other = await _evaluation_in_group(db_session, owner_id=caller.id)

    response = await _restore_assignment(auth_db_client, other.id, assignment_id, headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_deleted_assignment_listing_needs_the_global_update_permission(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
    evaluation: Evaluation,
) -> None:
    # An in-group owner holding only `evaluations:read` globally passes the object-scope
    # gate but is refused every Restore, so the list must refuse them too rather than
    # showing rows whose restore 403s.
    caller = await _caller(db_session, reader_role, email="read-only-deleted-assignments@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/models?deleted=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
