"""Integration tests for the `/v1/evaluations/{id}/label-categories` router."""

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationLabelCategory
from app.core.evaluations.schemas import MAX_LABEL_CATEGORIES
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def updater_role(db_session: AsyncSession) -> Role:
    """Read + update + `evaluation_groups:manage` (break-glass lifts the in-group write gate)."""
    role = Role(
        name="label-updater",
        description="eval read/update + manage",
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
async def plain_updater_role(db_session: AsyncSession) -> Role:
    role = Role(
        name="label-plain-updater",
        description="eval read/update, no manage",
        permissions=[Permission.EVALUATIONS_READ.value, Permission.EVALUATIONS_UPDATE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    role = Role(name="label-reader", description="read-only", permissions=[Permission.EVALUATIONS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def evaluation(db_session: AsyncSession) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _auth(caller: User) -> dict:
    return {"Authorization": f"Bearer {_token(caller)}"}


def _url(evaluation: Evaluation, suffix: str = "") -> str:
    return f"/api/v1/evaluations/{evaluation.id}/label-categories{suffix}"


async def test_create_lists_by_name_and_sets_location(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="lc-create@example.com")
    second = await auth_db_client.post(_url(evaluation), json={"name": "note"}, headers=_auth(caller))
    first = await auth_db_client.post(
        _url(evaluation), json={"name": "Language", "values": ["english", "polish"]}, headers=_auth(caller)
    )
    listed = await auth_db_client.get(_url(evaluation), headers=_auth(caller))

    assert first.status_code == status.HTTP_201_CREATED
    assert first.headers["Location"] == f"/api/v1/evaluations/{evaluation.id}/label-categories/{first.json()['id']}"
    assert second.json()["values"] == []
    assert [(c["name"], c["values"]) for c in listed.json()] == [("Language", ["english", "polish"]), ("note", [])]


async def test_names_and_values_are_cleaned_to_one_line(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="lc-clean@example.com")
    response = await auth_db_client.post(
        _url(evaluation), json={"name": " spoken\nlanguage ", "values": ["en\tGB"]}, headers=_auth(caller)
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert (response.json()["name"], response.json()["values"]) == ("spoken language", ["en GB"])


@pytest.mark.parametrize(
    "body",
    [
        {"name": "  "},
        {"name": "x" * 65},
        {"name": "language", "values": ["english", "English"]},
        {"name": "language", "values": [""]},
        {"name": "language", "values": [f"v{i}" for i in range(51)]},
    ],
)
async def test_invalid_payload_is_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation, body: dict
) -> None:
    caller = await _caller(db_session, updater_role, email="lc-invalid@example.com")
    response = await auth_db_client.post(_url(evaluation), json=body, headers=_auth(caller))
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_a_name_is_taken_ignoring_case(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    db_session.add(EvaluationLabelCategory(evaluation_id=evaluation.id, name="Language"))
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="lc-dup@example.com")
    response = await auth_db_client.post(_url(evaluation), json={"name": "language"}, headers=_auth(caller))
    assert response.status_code == status.HTTP_409_CONFLICT


async def test_the_category_cap_is_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    db_session.add_all(
        EvaluationLabelCategory(evaluation_id=evaluation.id, name=f"c{i}") for i in range(MAX_LABEL_CATEGORIES)
    )
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="lc-cap@example.com")
    response = await auth_db_client.post(_url(evaluation), json={"name": "one more"}, headers=_auth(caller))
    assert response.status_code == status.HTTP_409_CONFLICT


async def test_patch_renames_and_replaces_values_and_audits(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    row = EvaluationLabelCategory(evaluation_id=evaluation.id, name="lang", values=["english"])
    db_session.add(row)
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="lc-patch@example.com")

    response = await auth_db_client.patch(
        _url(evaluation, f"/{row.id}"),
        json={"name": "language", "values": ["english", "polish"]},
        headers=_auth(caller),
    )

    assert response.status_code == status.HTTP_200_OK
    assert (response.json()["name"], response.json()["values"]) == ("language", ["english", "polish"])
    audit = (
        await db_session.execute(
            select(AuditLog).where(col(AuditLog.action) == AuditAction.EVALUATION_LABEL_CATEGORY_UPDATE.value)
        )
    ).scalar_one()
    assert audit.before == {"name": "lang", "values": ["english"]}
    assert audit.after == {"name": "language", "values": ["english", "polish"]}


async def test_patch_to_a_name_taken_by_another_category_is_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    row = EvaluationLabelCategory(evaluation_id=evaluation.id, name="lang")
    db_session.add_all([row, EvaluationLabelCategory(evaluation_id=evaluation.id, name="topic")])
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="lc-patch-dup@example.com")
    response = await auth_db_client.patch(_url(evaluation, f"/{row.id}"), json={"name": "TOPIC"}, headers=_auth(caller))
    assert response.status_code == status.HTTP_409_CONFLICT


async def test_patch_rejects_explicit_null(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    row = EvaluationLabelCategory(evaluation_id=evaluation.id, name="lang")
    db_session.add(row)
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="lc-null@example.com")
    response = await auth_db_client.patch(_url(evaluation, f"/{row.id}"), json={"values": None}, headers=_auth(caller))
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_delete_hides_the_category_and_frees_its_name(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    row = EvaluationLabelCategory(evaluation_id=evaluation.id, name="lang")
    db_session.add(row)
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="lc-delete@example.com")

    deleted = await auth_db_client.delete(_url(evaluation, f"/{row.id}"), headers=_auth(caller))
    listed = await auth_db_client.get(_url(evaluation), headers=_auth(caller))
    recreated = await auth_db_client.post(_url(evaluation), json={"name": "lang"}, headers=_auth(caller))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert listed.json() == []
    assert recreated.status_code == status.HTTP_201_CREATED


async def test_a_category_of_another_evaluation_is_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    other = Evaluation(
        title="Other",
        description="d",
        evaluation_group_id=evaluation.evaluation_group_id,
        created_by_id=evaluation.created_by_id,
    )
    db_session.add(other)
    await db_session.flush()
    row = EvaluationLabelCategory(evaluation_id=other.id, name="lang")
    db_session.add(row)
    await db_session.flush()
    caller = await _caller(db_session, updater_role, email="lc-scope@example.com")
    response = await auth_db_client.patch(_url(evaluation, f"/{row.id}"), json={"name": "x"}, headers=_auth(caller))
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.parametrize("verb", ["post", "patch", "delete"])
async def test_writes_forbidden_without_manage_or_ownership(
    auth_db_client: AsyncClient, db_session: AsyncSession, plain_updater_role: Role, evaluation: Evaluation, verb: str
) -> None:
    row = EvaluationLabelCategory(evaluation_id=evaluation.id, name="lang")
    db_session.add(row)
    await db_session.flush()
    caller = await _caller(db_session, plain_updater_role, email=f"lc-noowner-{verb}@example.com")
    headers = _auth(caller)
    if verb == "post":
        response = await auth_db_client.post(_url(evaluation), json={"name": "other"}, headers=headers)
    elif verb == "patch":
        response = await auth_db_client.patch(_url(evaluation, f"/{row.id}"), json={"name": "x"}, headers=headers)
    else:
        response = await auth_db_client.delete(_url(evaluation, f"/{row.id}"), headers=headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_create_and_delete_are_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="lc-audit@example.com")
    created = await auth_db_client.post(
        _url(evaluation), json={"name": "language", "values": ["en"]}, headers=_auth(caller)
    )
    await auth_db_client.delete(_url(evaluation, f"/{created.json()['id']}"), headers=_auth(caller))

    rows = {
        row.action: row
        for row in (
            await db_session.execute(select(AuditLog).where(col(AuditLog.object_type) == "evaluation_label_category"))
        ).scalars()
    }
    snapshot = {"name": "language", "values": ["en"]}
    assert rows[AuditAction.EVALUATION_LABEL_CATEGORY_CREATE.value].after == snapshot
    assert rows[AuditAction.EVALUATION_LABEL_CATEGORY_DELETE.value].before == snapshot
    assert rows[AuditAction.EVALUATION_LABEL_CATEGORY_DELETE.value].context == {"evaluation_id": str(evaluation.id)}


async def test_reader_can_list_but_not_create(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, reader_role, email="lc-reader@example.com")
    listed = await auth_db_client.get(_url(evaluation), headers=_auth(caller))
    created = await auth_db_client.post(_url(evaluation), json={"name": "lang"}, headers=_auth(caller))
    assert listed.status_code == status.HTTP_200_OK
    assert created.status_code == status.HTTP_403_FORBIDDEN
