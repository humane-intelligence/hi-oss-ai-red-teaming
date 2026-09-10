"""Integration tests for the `/v1/evaluations` CRUD router."""

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
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.licenses.catalog import curated_license_id
from app.core.platform_settings.service import update_platform_settings
from tests.api.v1.conftest import make_token as _token
from tests.conftest import ensure_owner_role
from tests.conftest import persist_evaluation_group


@pytest_asyncio.fixture
async def manager_role(db_session: AsyncSession) -> Role:
    role = Role(
        name="eval-manager",
        description="Full evaluation CRUD",
        permissions=[
            Permission.EVALUATIONS_READ.value,
            Permission.EVALUATIONS_CREATE.value,
            Permission.EVALUATIONS_UPDATE.value,
            Permission.EVALUATIONS_DELETE.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def manager_with_manage_role(db_session: AsyncSession) -> Role:
    """Full evaluation CRUD plus the `evaluation_groups:manage` elevation.

    A holder may read and mutate evaluations in any group regardless of their
    role in the parent — the admin-only override that lifts the in-group-role gate.
    """
    role = Role(
        name="eval-manager-elevated",
        description="Evaluation CRUD + manage elevation",
        permissions=[
            Permission.EVALUATIONS_READ.value,
            Permission.EVALUATIONS_CREATE.value,
            Permission.EVALUATIONS_UPDATE.value,
            Permission.EVALUATIONS_DELETE.value,
            Permission.EVALUATION_GROUPS_MANAGE.value,
        ],
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


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


@pytest.mark.integration
async def test_create_evaluation_returns_201_draft_and_location(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="create@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "Gauntlet", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["status"] == "new"
    assert body["models"] == []
    assert body["created_by_id"] == str(caller.id)
    assert response.headers["Location"] == f"/api/v1/evaluations/{body['id']}"


@pytest.mark.integration
async def test_create_evaluation_ignores_status_in_payload(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="status@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id), "status": "published"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["status"] == "new"


@pytest.mark.integration
async def test_create_evaluation_title_only_succeeds(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="title-only@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "Just a title", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["title"] == "Just a title"
    assert body["description"] is None


@pytest.mark.integration
async def test_update_evaluation_clears_description_with_explicit_null(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="clear-desc@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}",
        json={"description": None},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "group_status",
    [
        PublicationStatus.DRAFT,
        PublicationStatus.PENDING_APPROVAL,
        PublicationStatus.CHANGES_REQUESTED,
        PublicationStatus.NOT_APPROVED,
        PublicationStatus.INACTIVE,
    ],
)
async def test_create_evaluation_non_accepting_group_returns_409(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
    group_status: PublicationStatus,
) -> None:
    caller = await _caller(db_session, manager_role, email=f"gate-{group_status.value}@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id, status=group_status)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Conflict"


@pytest.mark.integration
async def test_create_evaluation_published_group_succeeds(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="published-ok@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id, status=PublicationStatus.PUBLISHED)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED


@pytest.mark.integration
async def test_create_evaluation_manager_cannot_bypass_status_gate(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_with_manage_role: Role,
) -> None:
    # The lifecycle gate is state, not authority — `evaluation_groups:manage`
    # lifts the write gate but must not open a pre-approval group to adds.
    group = await persist_evaluation_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    caller = await _caller(db_session, manager_with_manage_role, email="manager-gate@example.com")

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.integration
async def test_create_evaluation_forbidden_for_reader(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session)
    caller = await _caller(db_session, reader_role, email="reader-create@example.com")

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_create_evaluation_write_gate_precedes_status_gate(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    # A caller lacking write access on a non-accepting group must get the write
    # verdict (403), never the lifecycle 409 — so the status gate can't leak a group
    # the caller couldn't write anyway. `not_approved` is non-accepting yet (non-draft
    # + public) visible, so the 403 comes from the write gate, not a visibility 404.
    group = await persist_evaluation_group(db_session, status=PublicationStatus.NOT_APPROVED)
    caller = await _caller(db_session, reader_role, email="gate-order@example.com")

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_get_evaluation_surfaces_effective_parameters(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    # effective_parameters = model baseline merged with the per-assignment overrides
    # (assignment wins on conflict) — what a conversation under this assignment inherits.
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    model = AiModel(
        name="Claude",
        model_alias="claude",
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-x",
        parameters={"temperature": 0.5, "top_p": 0.1},
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(
        EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id, parameters={"top_p": 0.9, "max_tokens": 256})
    )
    await db_session.flush()
    caller = await _caller(db_session, reader_role, email="effparams@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_200_OK
    view = response.json()["models"][0]
    assert view["effective_parameters"] == {"temperature": 0.5, "top_p": 0.9, "max_tokens": 256}


@pytest.mark.integration
async def test_get_evaluation_private_group_hidden_returns_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, reader_role, email="hidden@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_evaluations_filters_by_group(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    group_a = await persist_evaluation_group(db_session, title="a")
    group_b = await persist_evaluation_group(db_session, title="b")
    db_session.add(
        Evaluation(title="in-a", description="d", evaluation_group_id=group_a.id, created_by_id=group_a.created_by_id)
    )
    db_session.add(
        Evaluation(title="in-b", description="d", evaluation_group_id=group_b.id, created_by_id=group_b.created_by_id)
    )
    await db_session.flush()
    caller = await _caller(db_session, reader_role, email="list@example.com")

    response = await auth_db_client.get(
        "/api/v1/evaluations",
        params={"evaluation_group_id": str(group_a.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "in-a"


@pytest.mark.integration
async def test_update_evaluation_changes_title(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="update@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(
        title="orig", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}",
        json={"title": "renamed"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "renamed"


@pytest.mark.integration
async def test_delete_evaluation_returns_204_then_get_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="delete@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    headers = {"Authorization": f"Bearer {_token(caller)}"}

    delete = await auth_db_client.delete(f"/api/v1/evaluations/{evaluation.id}", headers=headers)
    get = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}", headers=headers)

    assert delete.status_code == status.HTTP_204_NO_CONTENT
    assert get.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_delete_evaluation_forbidden_for_reader(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, reader_role, email="reader-delete@example.com")

    response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


# --- in-group-role write gate (inherited from the parent group) -------------


@pytest.mark.integration
async def test_create_evaluation_forbidden_when_not_group_owner(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # Public group owned by someone else: visible, but only its owner (or a
    # manager) may add evaluations to it.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    caller = await _caller(db_session, manager_role, email="notowner-create@example.com")

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_update_evaluation_forbidden_when_not_group_owner(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, manager_role, email="notowner-update@example.com")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}",
        json={"title": "hijack"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_update_evaluation_manager_can_edit_others_group(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_with_manage_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, manager_with_manage_role, email="manager-update@example.com")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}",
        json={"title": "moderated"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "moderated"


@pytest.mark.integration
async def test_delete_evaluation_forbidden_when_not_group_owner(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, manager_role, email="notowner-delete@example.com")

    response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_delete_evaluation_manager_can_delete_others_group(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_with_manage_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, manager_with_manage_role, email="manager-delete@example.com")

    response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.integration
async def test_get_evaluation_manager_sees_others_private_group(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_with_manage_role: Role,
) -> None:
    # `evaluation_groups:manage` lifts the read-visibility scope one level down:
    # a manager resolves an evaluation under a private group it doesn't own.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, manager_with_manage_role, email="manager-get@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(evaluation.id)


@pytest.mark.integration
async def test_list_evaluations_manager_sees_others_private_group(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_with_manage_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = Evaluation(
        title="hidden", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    caller = await _caller(db_session, manager_with_manage_role, email="manager-list@example.com")

    response = await auth_db_client.get("/api/v1/evaluations", headers={"Authorization": f"Bearer {_token(caller)}"})

    assert response.status_code == status.HTTP_200_OK
    assert str(evaluation.id) in {item["id"] for item in response.json()["items"]}


@pytest.mark.integration
async def test_create_evaluation_inherits_platform_default_license(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="lic-inherit@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "t", "description": "d", "evaluation_group_id": str(group.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["data_license_id"] is None
    assert body["effective_license"]["spdx_id"] == "CC-BY-4.0"


@pytest.mark.integration
async def test_create_evaluation_with_license_override(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="lic-override@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={
            "title": "t",
            "description": "d",
            "evaluation_group_id": str(group.id),
            "data_license_id": str(curated_license_id("CC0-1.0")),
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["data_license_id"] == str(curated_license_id("CC0-1.0"))
    assert body["effective_license"]["spdx_id"] == "CC0-1.0"


@pytest.mark.integration
async def test_create_evaluation_rejects_unknown_license(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="lic-bad@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    base = {"title": "t", "description": "d", "evaluation_group_id": str(group.id)}

    malformed = await auth_db_client.post(
        "/api/v1/evaluations", json={**base, "data_license_id": "NOPE"}, headers=headers
    )
    unknown = await auth_db_client.post(
        "/api/v1/evaluations", json={**base, "data_license_id": str(uuid4())}, headers=headers
    )

    assert malformed.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT  # not a UUID
    assert unknown.status_code == status.HTTP_400_BAD_REQUEST  # well-formed, but no such live licence


@pytest.mark.integration
async def test_update_evaluation_license_set_then_reset_to_inherit(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="lic-update@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    headers = {"Authorization": f"Bearer {_token(caller)}"}

    set_override = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}",
        json={"data_license_id": str(curated_license_id("CC0-1.0"))},
        headers=headers,
    )
    reset_inherit = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}", json={"data_license_id": None}, headers=headers
    )
    unknown = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}", json={"data_license_id": str(uuid4())}, headers=headers
    )

    assert set_override.json()["data_license_id"] == str(curated_license_id("CC0-1.0"))
    assert set_override.json()["effective_license"]["spdx_id"] == "CC0-1.0"
    assert reset_inherit.json()["data_license_id"] is None
    assert reset_inherit.json()["effective_license"]["spdx_id"] == "CC-BY-4.0"
    assert unknown.status_code == status.HTTP_400_BAD_REQUEST  # well-formed, but no such live licence


@pytest.mark.integration
async def test_effective_license_inherits_group_override(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # The group layer sits between the evaluation override and the platform
    # default — an un-overridden evaluation resolves to its group's license, and an
    # evaluation override still beats it.
    caller = await _caller(db_session, manager_role, email="lic-group@example.com")
    group = await persist_evaluation_group(
        db_session, created_by_id=caller.id, data_license_id=curated_license_id("CC0-1.0")
    )
    inheriting = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    overriding = Evaluation(
        title="t2",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=caller.id,
        data_license_id=curated_license_id("CC-BY-SA-4.0"),
    )
    db_session.add_all([inheriting, overriding])
    await db_session.flush()
    headers = {"Authorization": f"Bearer {_token(caller)}"}

    inherited = await auth_db_client.get(f"/api/v1/evaluations/{inheriting.id}", headers=headers)
    overridden = await auth_db_client.get(f"/api/v1/evaluations/{overriding.id}", headers=headers)

    assert inherited.json()["data_license_id"] is None
    assert inherited.json()["effective_license"]["spdx_id"] == "CC0-1.0"
    assert overridden.json()["data_license_id"] == str(curated_license_id("CC-BY-SA-4.0"))
    assert overridden.json()["effective_license"]["spdx_id"] == "CC-BY-SA-4.0"


@pytest.mark.integration
async def test_effective_license_follows_platform_default_change(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="lic-platform@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    await update_platform_settings(db_session, default_license_id=curated_license_id("CC0-1.0"))

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["data_license_id"] is None
    assert response.json()["effective_license"]["spdx_id"] == "CC0-1.0"


# --- Duplicate: POST /{id}/duplicate ----------------------------------------


@pytest.mark.integration
async def test_duplicate_deep_copies_children_into_new_evaluation(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="dup-deep@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    source = Evaluation(
        title="Template",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=caller.id,
        cover_image="c.png",
        status=EvaluationStatus.REJECTED,
        rejection_reason="Out of scope.",
    )
    db_session.add(source)
    await db_session.flush()
    model = AiModel(
        name="claude", model_alias="claude", provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5"
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(
        EvaluationAiModel(
            evaluation_id=source.id, model_id=model.id, model_display_mask="Masked", parameters={"temperature": 0.3}
        )
    )
    scenario = Scenario(name="S1", description="d", evaluation_id=source.id, position=2, required_reviews=3)
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(Task(name="T1", description="d", scenario_id=scenario.id))
    await db_session.flush()
    source_id, model_id = source.id, model.id

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source_id}/duplicate?include_children=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    new_id = body["id"]
    assert new_id != str(source_id)
    assert body["status"] == "new"  # reset from the source's `rejected`
    assert body["rejection_reason"] is None
    assert body["title"] == "Template"
    assert body["cover_image"] == "c.png"
    assert body["evaluation_group_id"] == str(group.id)
    assert body["created_by_id"] == str(caller.id)
    assert response.headers["location"] == f"/api/v1/evaluations/{new_id}"

    new_assignments = (
        (await db_session.execute(select(EvaluationAiModel).where(col(EvaluationAiModel.evaluation_id) == new_id)))
        .scalars()
        .all()
    )
    assert len(new_assignments) == 1
    assert new_assignments[0].model_id == model_id
    assert new_assignments[0].model_display_mask == "Masked"
    assert new_assignments[0].parameters == {"temperature": 0.3}

    new_scenarios = (
        (await db_session.execute(select(Scenario).where(col(Scenario.evaluation_id) == new_id))).scalars().all()
    )
    assert len(new_scenarios) == 1
    assert new_scenarios[0].position == 2  # preserved verbatim
    assert new_scenarios[0].required_reviews == 3

    new_tasks = (
        (await db_session.execute(select(Task).where(col(Task.scenario_id) == new_scenarios[0].id))).scalars().all()
    )
    assert [t.name for t in new_tasks] == ["T1"]


@pytest.mark.integration
async def test_duplicate_deep_copies_tag_schema(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # Regression (review #1): a restricted source must copy its allowed-key set too, or the copy
    # would be `restricted=True` with an empty set → silently forbid every conversation tag.
    caller = await _caller(db_session, manager_role, email="dup-tagschema@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    source = Evaluation(
        title="T",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=caller.id,
        tags_enabled=False,
        tags_restricted=True,
    )
    db_session.add(source)
    await db_session.flush()
    db_session.add(EvaluationTagKey(evaluation_id=source.id, key="env"))
    await db_session.flush()
    source_id = source.id

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source_id}/duplicate?include_children=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    new_id = response.json()["id"]
    copy = (await db_session.execute(select(Evaluation).where(col(Evaluation.id) == new_id))).scalar_one()
    assert copy.tags_restricted is True
    # Both flags travel: the copy of an evaluation that opted out of tagging must not silently opt in.
    assert copy.tags_enabled is False
    new_keys = (
        (await db_session.execute(EvaluationTagKey.live_select().where(col(EvaluationTagKey.evaluation_id) == new_id)))
        .scalars()
        .all()
    )
    assert [k.key for k in new_keys] == ["env"]


@pytest.mark.integration
async def test_duplicate_without_children_still_copies_the_tag_schema(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # The tag schema is governance, not child data: a shell copy carries the restriction *and* its
    # keys, so duplicating can't silently downgrade a restricted evaluation to free-form (copying
    # the flag alone would instead forbid every tag — hence both or neither).
    caller = await _caller(db_session, manager_role, email="dup-tagshell@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    source = Evaluation(
        title="T",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=caller.id,
        tags_enabled=False,
        tags_restricted=True,
    )
    db_session.add(source)
    await db_session.flush()
    db_session.add(EvaluationTagKey(evaluation_id=source.id, key="env"))
    await db_session.flush()
    source_id = source.id

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source_id}/duplicate", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_201_CREATED
    new_id = response.json()["id"]
    copy = (await db_session.execute(select(Evaluation).where(col(Evaluation.id) == new_id))).scalar_one()
    assert copy.tags_restricted is True
    # Both flags travel: the copy of an evaluation that opted out of tagging must not silently opt in.
    assert copy.tags_enabled is False
    new_keys = (
        (await db_session.execute(EvaluationTagKey.live_select().where(col(EvaluationTagKey.evaluation_id) == new_id)))
        .scalars()
        .all()
    )
    assert [k.key for k in new_keys] == ["env"]  # flag without keys would forbid everything


@pytest.mark.integration
async def test_duplicate_without_children_copies_evaluation_only(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="dup-shallow@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    source = Evaluation(title="T", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(source)
    await db_session.flush()
    db_session.add(Scenario(name="S1", description="d", evaluation_id=source.id, position=1))
    await db_session.flush()
    source_id = source.id

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source_id}/duplicate", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_201_CREATED
    new_id = response.json()["id"]
    new_scenarios = (
        (await db_session.execute(select(Scenario).where(col(Scenario.evaluation_id) == new_id))).scalars().all()
    )
    assert new_scenarios == []  # children not copied by default


@pytest.mark.integration
async def test_duplicate_in_non_accepting_group_returns_409(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # A `draft` group can legitimately hold evaluations (group duplication deep-copies
    # them) — but duplicating one of those must not grow the group before approval.
    caller = await _caller(db_session, manager_role, email="dup-draft-group@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id, status=PublicationStatus.DRAFT)
    source = Evaluation(title="T", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(source)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source.id}/duplicate", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Conflict"


@pytest.mark.integration
async def test_duplicate_skips_soft_deleted_model_assignment(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    caller = await _caller(db_session, manager_role, email="dup-dead@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    source = Evaluation(title="T", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(source)
    await db_session.flush()
    model = AiModel(name="dead", model_alias="dead", provider=ProviderVendor.ANTHROPIC, provider_model_id="x")
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationAiModel(evaluation_id=source.id, model_id=model.id))
    await db_session.flush()
    model.soft_delete(None)
    await db_session.flush()
    source_id = source.id

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source_id}/duplicate?include_children=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    new_id = response.json()["id"]
    new_assignments = (
        (await db_session.execute(select(EvaluationAiModel).where(col(EvaluationAiModel.evaluation_id) == new_id)))
        .scalars()
        .all()
    )
    assert new_assignments == []  # dead-model assignment not copied


@pytest.mark.integration
async def test_duplicate_unseen_source_returns_404(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # A private group the caller can't see, and an unknown id, both read as missing —
    # never leaking existence — for a caller without the manage break-glass.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    source = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(source)
    await db_session.flush()
    caller = await _caller(db_session, manager_role, email="dup-unseen@example.com")
    headers = {"Authorization": f"Bearer {_token(caller)}"}

    seen = await auth_db_client.post(f"/api/v1/evaluations/{source.id}/duplicate", headers=headers)
    unknown = await auth_db_client.post(f"/api/v1/evaluations/{uuid4()}/duplicate", headers=headers)

    assert seen.status_code == status.HTTP_404_NOT_FOUND
    assert unknown.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_duplicate_forbidden_for_reader(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    caller = await _caller(db_session, reader_role, email="dup-reader@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    source = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(source)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source.id}/duplicate", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_duplicate_visible_but_not_group_owner_forbidden(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # Public group owned by someone else: the caller can see the source (and holds
    # `evaluations:create` globally) but has no in-group role, so cloning is 403.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    source = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(source)
    await db_session.flush()
    caller = await _caller(db_session, manager_role, email="dup-notowner@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source.id}/duplicate", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_duplicate_by_member_with_in_group_role(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # The gate is write access on the parent group, not creator-only: a non-creator
    # holding the in-group `owner` role on the source's group can clone.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    source = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(source)
    await db_session.flush()
    caller = await _caller(db_session, manager_role, email="dup-member@example.com")
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, group.id, caller.id, [await ensure_owner_role(db_session)]
    )

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source.id}/duplicate", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["created_by_id"] == str(caller.id)


@pytest.mark.integration
async def test_duplicate_manager_can_duplicate_others_private_group(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_with_manage_role: Role,
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    source = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(source)
    await db_session.flush()
    caller = await _caller(db_session, manager_with_manage_role, email="dup-manager@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{source.id}/duplicate", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["created_by_id"] == str(caller.id)


async def _restore_evaluation(client: AsyncClient, evaluation_id: UUID, headers: dict[str, str]) -> Response:
    return await client.post(f"/api/v1/evaluations/{evaluation_id}/restore", headers=headers)


@pytest.mark.integration
async def test_restore_evaluation_brings_back_its_whole_subtree(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # The delete tombstones only the evaluation row — its children hide behind the
    # parent-visibility join. So the restore needs no cascade of its own, and the
    # scenario/assignment come back readable with it. This is the whole reason
    # `restore_evaluation` is three lines.
    caller = await _caller(db_session, manager_role, email="restore-eval@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    evaluation_id = evaluation.id
    model = AiModel(
        name="Subtree", model_alias="subtree", provider=ProviderVendor.ANTHROPIC, provider_model_id="subtree-x"
    )
    db_session.add(model)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation_id, position=0)
    assignment = EvaluationAiModel(evaluation_id=evaluation_id, model_id=model.id)
    db_session.add_all([scenario, assignment])
    await db_session.flush()
    scenario_id = scenario.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}", headers=headers)

    response = await _restore_evaluation(auth_db_client, evaluation_id, headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    assert len(response.json()["models"]) == 1
    scenarios = await auth_db_client.get(f"/api/v1/evaluations/{evaluation_id}/scenarios", headers=headers)
    assert [item["id"] for item in scenarios.json()["items"]] == [str(scenario_id)]


@pytest.mark.integration
async def test_restore_evaluation_404s_when_its_group_is_deleted(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_with_manage_role: Role,
) -> None:
    # A restored evaluation under a dead group would be invisible to everyone
    # (`_scope_to_groups` enforces group liveness for managers too), so it stays deleted.
    caller = await _caller(db_session, manager_with_manage_role, email="dead-group@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    evaluation_id = evaluation.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}", headers=headers)
    group.soft_delete(caller.id)
    await db_session.flush()

    response = await _restore_evaluation(auth_db_client, evaluation_id, headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_restore_evaluation_forbidden_without_group_write_access(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
) -> None:
    # `evaluations:delete` alone is not enough — the restore runs the same in-group
    # write gate the delete did.
    owner = await _caller(db_session, manager_role, email="eval-owner@example.com")
    outsider = await _caller(db_session, manager_role, email="eval-outsider@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=owner.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db_session.add(evaluation)
    await db_session.flush()
    evaluation_id = evaluation.id
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}", headers={"Authorization": f"Bearer {_token(owner)}"}
    )

    response = await _restore_evaluation(auth_db_client, evaluation_id, {"Authorization": f"Bearer {_token(outsider)}"})

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_restore_evaluation_404s_for_another_users_delete(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
    manager_with_manage_role: Role,
) -> None:
    # Group write access is not enough. The restore carries the same deleter scope the
    # deleted listing does, so an admin's moderation action can't be undone by the group's
    # owner — otherwise an id would be all it took to reach a row the listing hides.
    owner = await _caller(db_session, manager_role, email="own-deletes-only@example.com")
    admin = await _caller(db_session, manager_with_manage_role, email="eval-moderator@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=owner.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db_session.add(evaluation)
    await db_session.flush()
    evaluation_id = evaluation.id
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}", headers={"Authorization": f"Bearer {_token(admin)}"}
    )

    response = await _restore_evaluation(auth_db_client, evaluation_id, {"Authorization": f"Bearer {_token(owner)}"})

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_restore_evaluation_break_glass_reaches_another_users_delete(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
    manager_with_manage_role: Role,
) -> None:
    owner = await _caller(db_session, manager_role, email="eval-deleted-by-owner@example.com")
    admin = await _caller(db_session, manager_with_manage_role, email="eval-break-glass@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=owner.id)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db_session.add(evaluation)
    await db_session.flush()
    evaluation_id = evaluation.id
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}", headers={"Authorization": f"Bearer {_token(owner)}"}
    )

    response = await _restore_evaluation(auth_db_client, evaluation_id, {"Authorization": f"Bearer {_token(admin)}"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None


@pytest.mark.integration
async def test_deleted_evaluation_listing_scopes_to_the_callers_own_deletes(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    manager_role: Role,
    manager_with_manage_role: Role,
) -> None:
    # The flat list spans groups, so it can't run the per-group write gate — it scopes
    # by deleter instead, and the break-glass lifts that.
    owner = await _caller(db_session, manager_role, email="mine-deleted@example.com")
    other = await _caller(db_session, manager_role, email="theirs-deleted@example.com")
    admin = await _caller(db_session, manager_with_manage_role, email="admin-deleted@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=owner.id)
    mine = Evaluation(title="mine", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    theirs = Evaluation(title="theirs", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db_session.add_all([mine, theirs])
    await db_session.flush()
    mine_id, theirs_id = mine.id, theirs.id
    await auth_db_client.delete(f"/api/v1/evaluations/{mine_id}", headers={"Authorization": f"Bearer {_token(owner)}"})
    # The admin's break-glass lets them delete inside a group they don't own.
    await auth_db_client.delete(
        f"/api/v1/evaluations/{theirs_id}", headers={"Authorization": f"Bearer {_token(admin)}"}
    )

    own = await auth_db_client.get(
        "/api/v1/evaluations?deleted=true", headers={"Authorization": f"Bearer {_token(owner)}"}
    )
    elevated = await auth_db_client.get(
        "/api/v1/evaluations?deleted=true", headers={"Authorization": f"Bearer {_token(admin)}"}
    )

    assert [item["id"] for item in own.json()["items"]] == [str(mine_id)]
    listed_by_admin = {item["id"] for item in elevated.json()["items"]}
    assert {str(mine_id), str(theirs_id)} <= listed_by_admin
    # The third user deleted nothing, so their view is empty even though they hold the
    # same permission — the scope is the deleter, not the group.
    theirs_view = await auth_db_client.get(
        "/api/v1/evaluations?deleted=true", headers={"Authorization": f"Bearer {_token(other)}"}
    )
    assert theirs_view.json()["items"] == []


@pytest.mark.integration
async def test_deleted_evaluation_listing_requires_evaluations_delete(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    reader_role: Role,
) -> None:
    caller = await _caller(db_session, reader_role, email="reader-deleted-eval@example.com")

    response = await auth_db_client.get(
        "/api/v1/evaluations?deleted=true", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
