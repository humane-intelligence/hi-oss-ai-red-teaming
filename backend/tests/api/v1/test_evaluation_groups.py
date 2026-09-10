"""API tests for the `/api/v1/evaluation-groups` endpoints (list / create / get / update).

Auth is faked by overriding `current_user` (what `require_permission` resolves
transitively) with `session_user_from` — no JWT minting needed.
"""

from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import SystemRole
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationGroupAiModel
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.organizations.models import Organization
from tests.api.v1.conftest import as_user
from tests.api.v1.conftest import make_role
from tests.conftest import ensure_owner_role

pytestmark = pytest.mark.integration

_START = date(2026, 3, 1)


def _future(days: int) -> date:
    """A UTC calendar date `days` from today (create requires a start not before today)."""
    return (datetime.now(UTC) + timedelta(days=days)).date()


_FIXTURE_MODEL_ID = UUID("0a0a0a0a-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
async def _seed_allowed_model(db_session: AsyncSession) -> None:
    """A live model the create/edit payloads reference for `allowed_model_ids` (now required on create)."""
    db_session.add(
        AiModel(
            id=_FIXTURE_MODEL_ID,
            name="fixture-model",
            model_alias="fixture-model",
            provider=ProviderVendor.ANTHROPIC,
            provider_model_id="claude-3-5-sonnet-20240620",
        )
    )
    await db_session.flush()


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "New engagement",
        "description": "A fresh red-teaming engagement.",
        "access_level": "public",
        "start_date": _future(7).isoformat(),
        "end_date": _future(14).isoformat(),
        "allowed_model_ids": [str(_FIXTURE_MODEL_ID)],
    }
    payload.update(overrides)
    return payload


async def _user(db: AsyncSession, role: Role, email: str | None = None) -> User:
    return await create_user(db, email=email or f"{uuid4().hex[:8]}@example.com", roles=[role])


async def _org(db: AsyncSession, *, name: str = "Acme Corp", deleted: bool = False) -> Organization:
    org = Organization(name=name)
    db.add(org)
    await db.flush()
    if deleted:
        org.soft_delete(None)
        await db.flush()
    await db.refresh(org)
    return org


async def _user_in_org(db: AsyncSession, role: Role, org: Organization, email: str | None = None) -> User:
    user = await _user(db, role, email)
    # Set the FK directly and flush — no `refresh`, which would expire the `roles`
    # collection `create_user` loaded and make `session_user_from` (sync, in
    # `as_user`) lazy-load it on an async session.
    user.organization_id = org.id
    await db.flush()
    return user


async def _group(  # noqa: PLR0913 — keyword-only args mirror the group's column set, like the service helpers
    db: AsyncSession,
    *,
    owner: User,
    access_level: EvaluationGroupAccessLevel,
    organization_id: UUID | None = None,
    status_: PublicationStatus = PublicationStatus.PUBLISHED,
    title: str = "Engagement",
    description: str = "A red-teaming engagement.",
    metrics_access_during: MetricsAccessLevel = MetricsAccessLevel.OWNER_ONLY,
    metrics_access_after: MetricsAccessLevel = MetricsAccessLevel.OWNER_ONLY,
    data_license_id: UUID | None = None,
) -> EvaluationGroup:
    group = EvaluationGroup(
        title=title,
        description=description,
        created_by_id=owner.id,
        access_level=access_level,
        organization_id=organization_id,
        status=status_,
        start_date=_START,
        metrics_access_during=metrics_access_during,
        metrics_access_after=metrics_access_after,
        data_license_id=data_license_id,
    )
    db.add(group)
    await db.flush()
    await db.refresh(group)
    # Object authority is the owner role only (no created_by fallback), so mirror
    # the create path and grant it — otherwise the creator can't see/edit it.
    await grant_roles(db, ObjectType.EVALUATION_GROUP, group.id, owner.id, [await ensure_owner_role(db)])
    return group


async def test_list_returns_visible_groups_and_page_shape(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)

    public = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.PUBLIC)
    own_private = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {str(public.id), str(own_private.id)}


@pytest.mark.parametrize(
    ("query", "expected_title"),
    [
        ("?status=draft", "Draft one"),
        ("?access_level=invitation_only", "Invite only"),
        ("?search=NEEDLE", "Has needle"),
    ],
)
async def test_filters_and_search_narrow_results(
    async_client_with_db: AsyncClient, db_session: AsyncSession, query: str, expected_title: str
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)

    await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status_=PublicationStatus.DRAFT,
        title="Draft one",
    )
    await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        title="Invite only",
    )
    await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        title="Has needle",
        description="contains a needle in the haystack",
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups{query}")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == expected_title


@pytest.mark.parametrize(
    ("query_value", "expected_titles"),
    [
        ("true", {"Approved", "Published"}),
        ("false", {"Pending", "Rejected", "Finished"}),
    ],
)
async def test_accepts_evaluations_filter_follows_lifecycle_allowlist(
    async_client_with_db: AsyncClient, db_session: AsyncSession, query_value: str, expected_titles: set[str]
) -> None:
    # Server-side counterpart of the evaluation-create gate: the picker asks for
    # groups it may add evaluations to, without re-encoding which statuses qualify.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    for title, status_ in [
        ("Approved", PublicationStatus.APPROVED),
        ("Published", PublicationStatus.PUBLISHED),
        ("Pending", PublicationStatus.PENDING_APPROVAL),
        ("Rejected", PublicationStatus.NOT_APPROVED),
        ("Finished", PublicationStatus.INACTIVE),
    ]:
        await _group(
            db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, status_=status_, title=title
        )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups?accepts_evaluations={query_value}")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert {item["title"] for item in body["items"]} == expected_titles


async def test_list_respects_limit_offset_and_rejects_oversized_limit(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    for index in range(3):
        await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title=f"G{index}")

    with as_user(caller):
        page = await async_client_with_db.get("/api/v1/evaluation-groups?limit=1&offset=1")
        too_big = await async_client_with_db.get("/api/v1/evaluation-groups?limit=101")

    assert page.status_code == status.HTTP_200_OK
    page_body = page.json()
    assert page_body["total"] == 3
    assert len(page_body["items"]) == 1
    assert page_body["limit"] == 1
    assert page_body["offset"] == 1

    assert too_big.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_order_by_sorts_on_whitelisted_column(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    for title in ("Bravo", "Alpha", "Charlie"):
        await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title=title)

    with as_user(caller):
        ascending = await async_client_with_db.get("/api/v1/evaluation-groups?order_by=title")
        descending = await async_client_with_db.get("/api/v1/evaluation-groups?order_by=-title")

    assert [item["title"] for item in ascending.json()["items"]] == ["Alpha", "Bravo", "Charlie"]
    assert [item["title"] for item in descending.json()["items"]] == ["Charlie", "Bravo", "Alpha"]


async def test_order_by_rejects_value_outside_whitelist(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups?order_by=status")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_soft_deleted_groups_are_excluded(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    live = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="Live")
    gone = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="Gone")
    gone.soft_delete(None)
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups")

    body = response.json()
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(live.id)}


async def test_search_escapes_like_wildcards(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="50% off")
    await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="5000 tokens")

    # Unescaped, "50%" as a LIKE pattern would also match "5000 tokens"; escaped
    # it matches the literal "50%" only.
    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups", params={"search": "50%"})

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "50% off"


async def test_search_does_not_widen_past_visibility(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    mine = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="my needle")
    await _group(
        db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY, title="hidden needle"
    )

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups", params={"search": "needle"})

    body = response.json()
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(mine.id)}


async def test_member_sees_invitation_only_group_but_non_member_does_not(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    owner = await _user(db_session, reader)
    member = await _user(db_session, reader)
    non_member = await _user(db_session, reader)
    private = await _group(db_session, owner=owner, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, private.id, member.id, [system_roles["red_teamer"]])

    with as_user(member):
        member_view = await async_client_with_db.get("/api/v1/evaluation-groups")
    with as_user(non_member):
        non_member_view = await async_client_with_db.get("/api/v1/evaluation-groups")

    assert {item["id"] for item in member_view.json()["items"]} == {str(private.id)}
    assert non_member_view.json()["total"] == 0


async def test_list_manager_with_all_flag_sees_others_invitation_only(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `all_groups=true` (gated on `evaluation_groups:manage`) lifts the visibility
    # scope, so a manager sees a private group owned by someone else a plain reader can't.
    manager = await make_role(db_session, ["evaluation_groups:read", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    hidden = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups?all_groups=true")

    assert response.status_code == status.HTTP_200_OK
    assert str(hidden.id) in {item["id"] for item in response.json()["items"]}


async def test_list_manager_without_all_flag_is_scoped(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Without `all_groups=true` a manager sees the same scoped set as anyone else —
    # the override is opt-in, not always-on.
    manager = await make_role(db_session, ["evaluation_groups:read", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    hidden = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups")

    assert response.status_code == status.HTTP_200_OK
    assert str(hidden.id) not in {item["id"] for item in response.json()["items"]}


async def test_list_all_flag_forbidden_without_manage(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A read-only caller passing `all_groups=true` is rejected, not silently scoped.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups?all_groups=true")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_list_defaults_to_newest_first(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    older = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="Older")
    newer = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="Newer")
    # now() is fixed per transaction, so set created_at explicitly to order them.
    older.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    newer.created_at = datetime(2026, 2, 1, tzinfo=UTC)
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups")

    assert [item["title"] for item in response.json()["items"]] == ["Newer", "Older"]


# --- create -----------------------------------------------------------------


@pytest.mark.usefixtures("system_roles")
async def test_create_returns_201_location_and_pending_approval(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A full create is a completed group submitted for review → `pending_approval`
    # (partial progress goes through POST /draft, which stays `draft`).
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post("/api/v1/evaluation-groups", json=_create_payload())

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["status"] == PublicationStatus.PENDING_APPROVAL.value
    assert body["created_by_id"] == str(caller.id)
    assert body["title"] == "New engagement"
    assert response.headers["location"] == f"/api/v1/evaluation-groups/{body['id']}"


@pytest.mark.usefixtures("system_roles")
async def test_create_rejects_empty_allowed_models_with_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _user(db_session, await make_role(db_session, ["evaluation_groups:create"]))

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups", json=_create_payload(allowed_model_ids=[])
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.usefixtures("system_roles")
async def test_create_rejects_unknown_allowed_model_with_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _user(db_session, await make_role(db_session, ["evaluation_groups:create"]))

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups", json=_create_payload(allowed_model_ids=[str(uuid4())])
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.usefixtures("system_roles")
async def test_update_replaces_allowed_models(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    role = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    caller = await _user(db_session, role)
    other = AiModel(
        name="other-model", model_alias="other-model", provider=ProviderVendor.ANTHROPIC, provider_model_id="x"
    )
    db_session.add(other)
    await db_session.flush()

    with as_user(caller):
        created = await async_client_with_db.post("/api/v1/evaluation-groups", json=_create_payload())
        group_id = created.json()["id"]
        patched = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group_id}", json={"allowed_model_ids": [str(other.id)]}
        )

    assert patched.status_code == status.HTTP_200_OK
    subset = (
        (
            await db_session.execute(
                EvaluationGroupAiModel.live_select().where(
                    col(EvaluationGroupAiModel.evaluation_group_id) == UUID(group_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.model_id for row in subset} == {other.id}


@pytest.mark.usefixtures("system_roles")
async def test_update_rejects_removing_in_use_model_with_409(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Dropping a model from `allowed_model_ids` while it's assigned to a live evaluation
    # in the group conflicts (409) — the documented response for the PATCH route.
    caller = await _user(db_session, await make_role(db_session, ["evaluation_groups:update"]))
    group = await _group(
        db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, status_=PublicationStatus.APPROVED
    )
    other = AiModel(
        name="other-model", model_alias="other-model", provider=ProviderVendor.ANTHROPIC, provider_model_id="x"
    )
    db_session.add_all([other, EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=_FIXTURE_MODEL_ID)])
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    db_session.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=_FIXTURE_MODEL_ID))
    await db_session.flush()

    with as_user(caller):
        patched = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"allowed_model_ids": [str(other.id)]}
        )

    assert patched.status_code == status.HTTP_409_CONFLICT


@pytest.mark.usefixtures("system_roles")
async def test_create_roundtrips_metrics_access_levels(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        explicit = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(
                metrics_access_during=MetricsAccessLevel.ALL_MEMBERS.value,
                metrics_access_after=MetricsAccessLevel.INHERIT_GROUP_ACCESS.value,
            ),
        )
        omitted = await async_client_with_db.post("/api/v1/evaluation-groups", json=_create_payload())

    assert explicit.status_code == status.HTTP_201_CREATED
    body = explicit.json()
    assert body["metrics_access_during"] == MetricsAccessLevel.ALL_MEMBERS.value
    assert body["metrics_access_after"] == MetricsAccessLevel.INHERIT_GROUP_ACCESS.value
    assert omitted.status_code == status.HTTP_201_CREATED
    assert omitted.json()["metrics_access_during"] == MetricsAccessLevel.MEMBERS_PERSONAL_METRICS.value
    assert omitted.json()["metrics_access_after"] == MetricsAccessLevel.MEMBERS_PERSONAL_METRICS.value


@pytest.mark.usefixtures("system_roles")
async def test_create_forces_pending_approval_even_if_client_sends_status(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    # `status` is not part of the create schema, so a client-supplied value is ignored.
    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups", json=_create_payload(status="published")
        )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["status"] == PublicationStatus.PENDING_APPROVAL.value


async def test_create_rejects_missing_field_with_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A required field absent is pure schema validation → 422. The date rules are
    # 400 (service), see below.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post("/api/v1/evaluation-groups", json=_create_payload(description=None))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize(
    "payload",
    [
        _create_payload(start_date=_future(-1).isoformat()),
        _create_payload(start_date=_future(20).isoformat(), end_date=_future(10).isoformat()),
        _create_payload(start_date=_future(10).isoformat(), end_date=_future(10).isoformat()),
    ],
    ids=["start-before-today", "end-before-start", "end-equals-start"],
)
async def test_create_rejects_bad_dates_with_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession, payload: dict[str, object]
) -> None:
    # Date rules live in the service (→ 400, same code/envelope as the update
    # merged-state check): a start before today, end before start, and the
    # `end == start` boundary (`<=`, not `<`).
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post("/api/v1/evaluation-groups", json=payload)

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.usefixtures("system_roles")
async def test_create_allows_start_today(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # Day-granular past check (matching the legacy wizard): a start of *today* is
    # accepted — only a prior calendar day is rejected.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    payload = _create_payload(start_date=_future(0).isoformat())

    with as_user(caller):
        response = await async_client_with_db.post("/api/v1/evaluation-groups", json=payload)

    assert response.status_code == status.HTTP_201_CREATED


# --- organization belonging + access scoping ---


@pytest.mark.usefixtures("system_roles")
async def test_create_organization_access_sets_org(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    org = await _org(db_session)
    caller = await _user_in_org(db_session, creator, org)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(access_level="organization", organization_id=str(org.id)),
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["access_level"] == "organization"
    assert body["organization_id"] == str(org.id)


@pytest.mark.usefixtures("system_roles")
async def test_create_with_foreign_org_returns_403(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # A non-admin caller may only assign a group to their *own* org — targeting an
    # org they don't belong to is 403 (cross-tenant planting), even though the org
    # is live (so it passes the liveness 400 check first).
    creator = await make_role(db_session, ["evaluation_groups:create"])
    own_org = await _org(db_session, name="Home Org")
    foreign_org = await _org(db_session, name="Foreign Inc")
    caller = await _user_in_org(db_session, creator, own_org)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(access_level="organization", organization_id=str(foreign_org.id)),
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.usefixtures("system_roles")
async def test_create_with_foreign_org_allowed_for_manager(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The `evaluation_groups:manage` break-glass lifts the own-org constraint — an
    # admin may park a group in any live org.
    manager = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:manage"])
    foreign_org = await _org(db_session, name="Foreign Inc")
    caller = await _user(db_session, manager)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(access_level="organization", organization_id=str(foreign_org.id)),
        )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["organization_id"] == str(foreign_org.id)


async def test_create_organization_access_without_org_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups", json=_create_payload(access_level="organization")
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_create_with_unknown_org_returns_400(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(access_level="organization", organization_id=str(uuid4())),
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_create_with_soft_deleted_org_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    dead = await _org(db_session, name="Tombstoned", deleted=True)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(access_level="organization", organization_id=str(dead.id)),
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_update_sets_organization(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    updater = await make_role(db_session, ["evaluation_groups:update"])
    org = await _org(db_session)
    caller = await _user_in_org(db_session, updater, org)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={"access_level": "organization", "organization_id": str(org.id)},
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["access_level"] == "organization"
    assert body["organization_id"] == str(org.id)


async def test_update_assign_foreign_org_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An owner may not move their group into an org they don't belong to.
    updater = await make_role(db_session, ["evaluation_groups:update"])
    own_org = await _org(db_session, name="Home Org")
    foreign_org = await _org(db_session, name="Foreign Inc")
    caller = await _user_in_org(db_session, updater, own_org)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={"access_level": "organization", "organization_id": str(foreign_org.id)},
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_update_assign_foreign_org_allowed_for_manager(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `evaluation_groups:manage` lifts the own-org constraint on the *update*
    # assignment path too — a distinct `_assert_caller_may_assign_org` call-site
    # (gated additionally on the org actually changing) from the create path's.
    manager = await make_role(db_session, ["evaluation_groups:update", "evaluation_groups:manage"])
    foreign_org = await _org(db_session, name="Foreign Inc")
    caller = await _user(db_session, manager)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={"access_level": "organization", "organization_id": str(foreign_org.id)},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["organization_id"] == str(foreign_org.id)


async def test_update_same_org_owner_edits_organization_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Positive counterpart to the foreign-org 403: a same-org owner may freely edit
    # their own `organization` group (here re-sending the unchanged org id with a
    # title change), since the own-org check fires only on an org *change*.
    updater = await make_role(db_session, ["evaluation_groups:update"])
    org = await _org(db_session)
    caller = await _user_in_org(db_session, updater, org)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.ORGANIZATION,
        organization_id=org.id,
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={"title": "Renamed", "organization_id": str(org.id)},
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["title"] == "Renamed"
    assert body["organization_id"] == str(org.id)


async def test_update_unrelated_field_on_foreign_org_group_is_allowed(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The own-org constraint applies only to a fresh org *assignment*. Editing
    # another field of a group already parked in a foreign org (e.g. by an admin)
    # must not 403 — the org is left untouched.
    updater = await make_role(db_session, ["evaluation_groups:update"])
    own_org = await _org(db_session, name="Home Org")
    foreign_org = await _org(db_session, name="Foreign Inc")
    caller = await _user_in_org(db_session, updater, own_org)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        organization_id=foreign_org.id,
    )

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Renamed"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "Renamed"


async def test_update_switch_to_organization_access_without_org_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    updater = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, updater)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"access_level": "organization"}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_update_clear_org_while_organization_access_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    updater = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, updater)
    org = await _org(db_session)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.ORGANIZATION,
        organization_id=org.id,
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"organization_id": None}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


# --- get by id --------------------------------------------------------------


async def test_get_by_id_returns_public_and_own(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    own = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{own.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(own.id)


async def test_get_by_id_hides_others_invitation_only(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    hidden = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{hidden.id}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_get_by_id_manager_sees_others_invitation_only(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `evaluation_groups:manage` lifts the visibility scope: a manager resolves
    # any group, including a private one owned by someone else.
    manager = await make_role(db_session, ["evaluation_groups:read", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    hidden = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{hidden.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(hidden.id)


async def test_public_draft_hidden_from_non_member_but_published_is_visible(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A `public` *draft* is owner/manager-only: `public` access alone does
    # not expose an incomplete WIP. A `public` *published* group of the same owner
    # stays visible, proving it is the draft status — not the access level — hiding it.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    draft = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status_=PublicationStatus.DRAFT,
        title="Public draft",
    )
    published = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        title="Public published",
    )

    with as_user(caller):
        list_view = await async_client_with_db.get("/api/v1/evaluation-groups")
        get_draft = await async_client_with_db.get(f"/api/v1/evaluation-groups/{draft.id}")

    assert {item["id"] for item in list_view.json()["items"]} == {str(published.id)}
    assert get_draft.status_code == status.HTTP_404_NOT_FOUND


async def test_owner_sees_own_public_draft(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # The owner holds the in-group `owner` role, so the membership arm keeps their
    # own public draft visible even though the `public` arm is gated on non-draft.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    own = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status_=PublicationStatus.DRAFT,
        title="My public draft",
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{own.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(own.id)


async def test_manager_sees_others_public_draft(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # Break-glass `evaluation_groups:manage` lifts the scope, so a manager resolves
    # another owner's public draft the membership/public arms would otherwise hide.
    manager = await make_role(db_session, ["evaluation_groups:read", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    draft = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status_=PublicationStatus.DRAFT,
        title="Other's public draft",
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{draft.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(draft.id)


async def test_get_by_id_unknown_returns_404(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{uuid4()}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_get_by_id_surfaces_in_group_owner_permissions(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The fix pinned here: an in-group owner whose *global* role grants only read
    # still surfaces their object-scope authority (edit / lifecycle / member
    # management / child-evaluation writes) via `user_permissions`, so the UI can
    # gate those actions on it even though the global permission set lacks them.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    # An in-group owner's effective set is exactly their `owner` role's permission
    # union (derived from the object-role registry); break-glass `manage` is absent.
    user_permissions = response.json()["user_permissions"]
    assert user_permissions == sorted(ROLE_PERMISSIONS[SystemRole.OWNER])
    assert "evaluation_groups:manage" not in user_permissions


async def test_get_by_id_non_member_has_no_group_permissions(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A non-member viewing a public group holds no in-group role, so
    # `user_permissions` is empty — global read alone confers no object authority.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    public = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{public.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["user_permissions"] == []


async def test_get_by_id_projects_the_next_steps_publication_blockers(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The detail read carries the submit gate's own sentences, so the console shows what
    # is missing before the owner clicks — no readiness endpoint, no gate mirrored on the FE.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status_=PublicationStatus.DRAFT,
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert "Assign at least one allowed model." in response.json()["publication_blockers"]


async def test_get_by_id_publication_blockers_empty_for_a_published_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A live group has no next transition left to block, whatever its content.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["publication_blockers"] == []


async def test_get_by_id_break_glass_manager_has_full_group_permissions(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The `evaluation_groups:manage` break-glass confers full object authority on
    # any group regardless of membership, so `user_permissions` lists the whole
    # group scope (incl. `manage`) even for a non-member.
    manager = await make_role(db_session, ["evaluation_groups:read", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    hidden = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{hidden.id}")

    assert response.status_code == status.HTTP_200_OK
    # Break-glass confers the object type's full vocabulary (every assignable role's
    # grants plus the break-glass key), even for a non-member.
    perms = set(response.json()["user_permissions"])
    assert "evaluation_groups:manage" in perms
    assert {"evaluation_groups:update", "evaluation_groups:manage_members", "evaluations:create"} <= perms
    assert set(ROLE_PERMISSIONS[SystemRole.OWNER]) <= perms


async def test_get_by_id_injects_view_metrics_for_config_granted_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The FE gates its metrics fetch on `evaluation_groups:view_metrics` in
    # `user_permissions`, so a member the *config* admits (no role grants the
    # key) must still see it surfaced — it mirrors what the metrics gate allows.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
    )
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, group.id, caller.id, [system_roles[SystemRole.RED_TEAMER.value]]
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert "evaluation_groups:view_metrics" in response.json()["user_permissions"]


async def test_get_by_id_omits_view_metrics_for_excluded_annotator(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # An annotator at `members_personal_metrics` lacks `view_personal_metrics`, so the
    # policy denies them and the FE fetch-gate key is not injected — even though the
    # annotator *is* a member (the fetch-gate is `view_metrics`, injected iff granted).
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        metrics_access_during=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    )
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, group.id, caller.id, [system_roles[SystemRole.ANNOTATOR.value]]
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert "evaluation_groups:view_metrics" not in response.json()["user_permissions"]


async def test_get_by_id_injects_view_metrics_for_personal_scope_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A red_teamer at `members_personal_metrics` is granted a personal-scope read, so
    # the single FE fetch-gate key (`view_metrics`) is injected — the breadth (personal)
    # rides the metrics response's own `scope` field, not this key.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        metrics_access_during=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    )
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, group.id, caller.id, [system_roles[SystemRole.RED_TEAMER.value]]
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert "evaluation_groups:view_metrics" in response.json()["user_permissions"]


async def test_get_by_id_injects_view_metrics_for_inherit_level_non_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A non-member on a public group holds no in-group role (their permission set
    # is otherwise empty), but an `inherit_group_access` metrics level admits
    # them — the key is injected alone.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        metrics_access_during=MetricsAccessLevel.INHERIT_GROUP_ACCESS,
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["user_permissions"] == ["evaluation_groups:view_metrics"]


async def test_get_by_id_view_metrics_injection_respects_after_level(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # On a finished group the `after` level governs: during=owner_only would deny,
    # but after=inherit_group_access admits the non-member once status is `inactive`.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    other = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=other,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status_=PublicationStatus.INACTIVE,
        metrics_access_during=MetricsAccessLevel.OWNER_ONLY,
        metrics_access_after=MetricsAccessLevel.INHERIT_GROUP_ACCESS,
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert "evaluation_groups:view_metrics" in response.json()["user_permissions"]


async def _evaluation(
    db: AsyncSession, *, group: EvaluationGroup, title: str = "Eval", created_at: datetime | None = None
) -> Evaluation:
    evaluation = Evaluation(
        title=title, description="body", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    if created_at is not None:  # server_default is now() (= txn time), so set explicitly for order tests
        evaluation.created_at = created_at
    db.add(evaluation)
    await db.flush()
    await db.refresh(evaluation)
    return evaluation


async def test_get_by_id_embeds_child_evaluations(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    a = await _evaluation(db_session, group=group, title="A")
    b = await _evaluation(db_session, group=group, title="B")

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    evaluations = response.json()["evaluations"]
    assert {e["id"] for e in evaluations} == {str(a.id), str(b.id)}
    # Full evaluation shape — same projection as GET /v1/evaluations/{id} (incl. models).
    assert set(evaluations[0]) == {
        "id",
        "evaluation_group_id",
        "created_by_id",
        "title",
        "description",
        "cover_image",
        "status",
        "mask_models_enabled",
        "tags_enabled",
        "tags_restricted",
        "data_license_id",
        "effective_license",
        "rejection_reason",
        "models",
        "created_at",
        "updated_at",
        "deleted_at",
        "deleted_by_id",
    }


async def test_get_by_id_surfaces_group_rejection_reason(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Guards the detail projection's `_base_kwargs`: a base group field (here
    # rejection_reason) must survive the detail read, not just list/create/update.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    group.rejection_reason = "Scope too broad."
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["rejection_reason"] == "Scope too broad."


async def test_get_by_id_embeds_in_creation_order(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    older = await _evaluation(db_session, group=group, title="older", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = await _evaluation(db_session, group=group, title="newer", created_at=datetime(2026, 2, 1, tzinfo=UTC))

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert [e["id"] for e in response.json()["evaluations"]] == [str(older.id), str(newer.id)]


async def test_get_by_id_embed_order_breaks_created_at_ties_by_id(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Siblings created in one transaction share now() (= txn time), so created_at
    # alone is ambiguous; the relationship order_by falls back to id for stability.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    same = datetime(2026, 1, 1, tzinfo=UTC)
    one = await _evaluation(db_session, group=group, title="one", created_at=same)
    two = await _evaluation(db_session, group=group, title="two", created_at=same)

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    expected = [str(e.id) for e in sorted((one, two), key=lambda e: e.id)]
    assert [e["id"] for e in response.json()["evaluations"]] == expected


async def test_get_by_id_excludes_soft_deleted_child(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    live = await _evaluation(db_session, group=group, title="live")
    doomed = await _evaluation(db_session, group=group, title="doomed")
    doomed.soft_delete(None)
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert [e["id"] for e in response.json()["evaluations"]] == [str(live.id)]


async def test_get_by_id_embeds_assigned_models(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # Exercises the nested eager-load (evaluations → models → ai_model): an
    # un-masked child surfaces its assignment's real model identity in the embed.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = await _evaluation(db_session, group=group, title="A")
    evaluation.mask_models_enabled = False
    model = AiModel(
        name="claude", model_alias="claude", provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5"
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id))
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    embedded = response.json()["evaluations"][0]
    assert [m["name"] for m in embedded["models"]] == ["claude"]


async def test_get_by_id_embeds_allowed_models_for_models_read(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _user(db_session, await make_role(db_session, ["evaluation_groups:read", "models:read"]))
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    model = AiModel(
        name="allowed", model_alias="allowed", provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5"
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    allowed = response.json()["allowed_models"]
    assert [m["name"] for m in allowed] == ["allowed"]
    assert allowed[0]["model_id"] == str(model.id)


async def test_get_by_id_embeds_allowed_model_modalities_in_prompt_reply_order(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Asymmetric on purpose: with equal sets, swapping the two in the projection reads
    # identically. This is the only surface carrying them outside the models router.
    caller = await _user(db_session, await make_role(db_session, ["evaluation_groups:read", "models:read"]))
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    model = AiModel(
        name="vision",
        model_alias="vision",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
        output_modalities=[Modality.TEXT],
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    allowed = response.json()["allowed_models"][0]
    assert (allowed["input_modalities"], allowed["output_modalities"]) == (["text", "image"], ["text"])


async def test_get_by_id_carries_the_model_description_in_the_allowed_subset(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # This subset is where a curator decides whether the group may use a model, and the note is
    # what answers that. It is `models:read`-gated and unmasked, like the rest of the projection.
    caller = await _user(db_session, await make_role(db_session, ["evaluation_groups:read", "models:read"]))
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    model = AiModel(
        name="acme-only",
        model_alias="acme-only",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        description="Client Acme only — do not assign elsewhere.",
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["allowed_models"][0]["description"] == ("Client Acme only — do not assign elsewhere.")


async def test_get_by_id_masks_allowed_models_without_models_read(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Without `models:read` — global or via an in-group role — the subset is hidden
    # (null), not merely empty. A non-owner viewer of a public group has neither.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    owner = await _user(db_session, reader, email="subset-owner@example.com")
    caller = await _user(db_session, reader, email="subset-viewer@example.com")
    group = await _group(db_session, owner=owner, access_level=EvaluationGroupAccessLevel.PUBLIC)
    model = AiModel(
        name="hidden", model_alias="hidden", provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5"
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["allowed_models"] is None


async def test_get_by_id_embeds_allowed_models_for_in_group_owner_without_global_read(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An in-group owner holds `models:read` via the owner role, so the subset is
    # embedded even without the global permission.
    caller = await _user(db_session, await make_role(db_session, ["evaluation_groups:read"]))
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    model = AiModel(
        name="owned", model_alias="owned", provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5"
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert [m["name"] for m in response.json()["allowed_models"]] == ["owned"]


async def test_get_by_id_embed_masks_assigned_models(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Security-relevant branch: a masked child (default) must surface the display
    # mask through the embed, never the real model identity.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = await _evaluation(db_session, group=group, title="A")
    evaluation.mask_models_enabled = True  # explicit — don't lean on the model default
    model = AiModel(
        name="claude", model_alias="claude", provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5"
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id, model_display_mask="Masked A"))
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    [embedded_model] = response.json()["evaluations"][0]["models"]
    assert embedded_model["name"] == "Masked A"
    assert embedded_model["provider"] is None
    assert embedded_model["provider_model_id"] is None


async def test_list_does_not_embed_child_evaluations(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Child evaluations are a detail-view concern; the base response has no
    # `evaluations` field at all, so list items must not carry the key.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    await _evaluation(db_session, group=group, title="A")

    with as_user(caller):
        response = await async_client_with_db.get("/api/v1/evaluation-groups")

    item = next(g for g in response.json()["items"] if g["id"] == str(group.id))
    assert "evaluations" not in item


# --- update -----------------------------------------------------------------


async def test_update_partial_change_lands(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="Before")

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "After"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["title"] == "After"
    assert body["description"] == "A red-teaming engagement."  # untouched


async def test_update_can_clear_end_date(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    group.end_date = _START + timedelta(days=30)
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"end_date": None})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["end_date"] is None


async def test_update_non_owner_forbidden(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    other = await _user(db_session, editor)
    group = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Hijack"})

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_update_others_private_group_reads_as_missing(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A private group owned by someone else must 404, not 403 — PATCH mirrors the
    # get endpoint so it never leaks the existence of an invisible group.
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    other = await _user(db_session, editor)
    group = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Hijack"})

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_update_manager_can_edit_others_public_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `evaluation_groups:manage` lifts the in-group-role requirement: a manager
    # edits a group it holds no role on.
    manager = await make_role(db_session, ["evaluation_groups:update", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    group = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"title": "Moderated"}
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "Moderated"


async def test_update_manager_can_edit_others_private_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Manage lifts both the in-group-role requirement and the visibility scope,
    # so even a private group the caller holds no role on is editable (not a 404).
    manager = await make_role(db_session, ["evaluation_groups:update", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    group = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"title": "Moderated"}
        )

    assert response.status_code == status.HTTP_200_OK


async def test_update_in_group_owner_non_creator_can_edit(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # An added in-group `owner` (not the creator) may edit the group — write
    # authority comes from the object role, not `created_by_id`.
    editor = await make_role(db_session, ["evaluation_groups:update"])
    creator = await _user(db_session, await make_role(db_session, []))
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=creator, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, caller.id, [system_roles["owner"]])

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Renamed"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "Renamed"


async def test_update_in_group_member_with_lesser_role_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The override: a red_teamer member is denied even though they carry
    # `evaluation_groups:update` globally (enough to pass the route gate).
    editor = await make_role(db_session, ["evaluation_groups:update"])
    creator = await _user(db_session, await make_role(db_session, []))
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=creator, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, caller.id, [system_roles["red_teamer"]])

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Renamed"})

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_update_in_group_member_with_deactivated_role_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A deactivated in-group role grants no write authority (even owner); the
    # member stays visible (assignment is live), so it's a 403, not a 404.
    editor = await make_role(db_session, ["evaluation_groups:update"])
    creator = await _user(db_session, await make_role(db_session, []))
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=creator, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, caller.id, [system_roles["owner"]])
    system_roles["owner"].is_active = False
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Renamed"})

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_update_unknown_id_returns_404(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)

    with as_user(caller):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{uuid4()}", json={"title": "X"})

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_update_rejects_explicit_null_on_required_column(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # title/access_level/metrics_access_* back NOT NULL columns — explicit null is a 422.
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with as_user(caller):
        null_title = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": None})
        null_access = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"access_level": None}
        )
        null_during = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"metrics_access_during": None}
        )
        null_after = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"metrics_access_after": None}
        )

    assert null_title.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert null_access.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert null_during.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert null_after.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_update_roundtrips_metrics_access_levels(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={
                "metrics_access_during": MetricsAccessLevel.MEMBERS_PERSONAL_METRICS.value,
                "metrics_access_after": MetricsAccessLevel.ALL_MEMBERS.value,
            },
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["metrics_access_during"] == MetricsAccessLevel.MEMBERS_PERSONAL_METRICS.value
    assert body["metrics_access_after"] == MetricsAccessLevel.ALL_MEMBERS.value


async def test_update_can_clear_nullable_draft_columns(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # start_date/description are nullable — a draft may blank them back out
    # via PATCH, mirroring that /draft can create them null in the first place.
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(
        db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, status_=PublicationStatus.DRAFT
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"start_date": None, "description": None}
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["start_date"] is None
    assert body["description"] is None


async def test_update_bad_merged_date_order_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(
        db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC
    )  # start=_START, no end

    # An end_date before the stored start_date must fail on the merged state.
    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={"end_date": (_START - timedelta(days=1)).isoformat()},
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


# --- data license: group-level override -----------------------------


@pytest.mark.usefixtures("system_roles")
async def test_create_with_data_license_stores_and_resolves(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        with_license = await async_client_with_db.post(
            "/api/v1/evaluation-groups", json=_create_payload(data_license_id=str(curated_license_id("CC0-1.0")))
        )
        without_license = await async_client_with_db.post("/api/v1/evaluation-groups", json=_create_payload())

    assert with_license.status_code == status.HTTP_201_CREATED
    assert with_license.json()["data_license_id"] == str(curated_license_id("CC0-1.0"))
    assert with_license.json()["effective_license"]["spdx_id"] == "CC0-1.0"
    # Omitted → the group inherits the platform default.
    assert without_license.json()["data_license_id"] is None
    assert without_license.json()["effective_license"]["spdx_id"] == "CC-BY-4.0"


@pytest.mark.usefixtures("system_roles")
async def test_create_rejects_unknown_data_license(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        malformed = await async_client_with_db.post(
            "/api/v1/evaluation-groups", json=_create_payload(data_license_id="NOPE")
        )
        unknown = await async_client_with_db.post(
            "/api/v1/evaluation-groups", json=_create_payload(data_license_id=str(uuid4()))
        )

    assert malformed.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT  # not a UUID
    assert unknown.status_code == status.HTTP_400_BAD_REQUEST  # well-formed, but no such live licence


async def test_update_sets_clears_and_keeps_data_license(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        data_license_id=curated_license_id("CC-BY-SA-4.0"),
    )
    url = f"/api/v1/evaluation-groups/{group.id}"

    with as_user(caller):
        unrelated_edit = await async_client_with_db.patch(url, json={"title": "Renamed"})
        set_override = await async_client_with_db.patch(
            url, json={"data_license_id": str(curated_license_id("CC0-1.0"))}
        )
        reset_inherit = await async_client_with_db.patch(url, json={"data_license_id": None})

    # Omitted field stays; a value sets; explicit null resets to inherit the platform default.
    assert unrelated_edit.json()["data_license_id"] == str(curated_license_id("CC-BY-SA-4.0"))
    assert set_override.json()["data_license_id"] == str(curated_license_id("CC0-1.0"))
    assert set_override.json()["effective_license"]["spdx_id"] == "CC0-1.0"
    assert reset_inherit.json()["data_license_id"] is None
    assert reset_inherit.json()["effective_license"]["spdx_id"] == "CC-BY-4.0"


async def test_update_rejects_unknown_data_license(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    editor = await make_role(db_session, ["evaluation_groups:update"])
    caller = await _user(db_session, editor)
    group = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    url = f"/api/v1/evaluation-groups/{group.id}"

    with as_user(caller):
        malformed = await async_client_with_db.patch(url, json={"data_license_id": "NOPE"})
        unknown = await async_client_with_db.patch(url, json={"data_license_id": str(uuid4())})

    assert malformed.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT  # not a UUID
    assert unknown.status_code == status.HTTP_400_BAD_REQUEST  # well-formed, but no such live licence


async def test_get_by_id_embed_child_license_cascades(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The three-level rule in one read: a child with no override inherits the group's
    # license, a child override beats it, and the group itself resolves its own layer.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        data_license_id=curated_license_id("CC0-1.0"),
    )
    inheriting = await _evaluation(db_session, group=group, title="inherits group")
    overriding = await _evaluation(db_session, group=group, title="own override")
    overriding.data_license_id = curated_license_id("CC-BY-SA-4.0")
    await db_session.flush()

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["effective_license"]["spdx_id"] == "CC0-1.0"
    licenses = {e["id"]: e["effective_license"]["spdx_id"] for e in body["evaluations"]}
    assert licenses == {str(inheriting.id): "CC0-1.0", str(overriding.id): "CC-BY-SA-4.0"}


@pytest.mark.usefixtures("system_roles")
async def test_create_private_without_data_license_defaults_to_no_license(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(access_level=EvaluationGroupAccessLevel.INVITATION_ONLY.value),
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    # Omitting the field on a private create derives "no license", which blocks inheritance.
    assert body["data_license_id"] == str(curated_license_id(NO_LICENSE_SPDX_ID))
    assert body["effective_license"]["name"] == "No license"
    assert body["effective_license"]["spdx_id"] is None


@pytest.mark.usefixtures("system_roles")
async def test_create_private_with_explicit_null_data_license_inherits_platform_default(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups",
            json=_create_payload(access_level=EvaluationGroupAccessLevel.INVITATION_ONLY.value, data_license_id=None),
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    # An explicit null is a deliberate "inherit", distinct from omitting the field.
    assert body["data_license_id"] is None
    assert body["effective_license"]["spdx_id"] == "CC-BY-4.0"


async def test_get_by_id_embed_child_inherits_no_license(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # "No license" propagates down the cascade like any other group-level licence.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    caller = await _user(db_session, reader)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        data_license_id=curated_license_id(NO_LICENSE_SPDX_ID),
    )
    inheriting = await _evaluation(db_session, group=group, title="inherits group")

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["effective_license"]["name"] == "No license"
    # The embedded licence carries `has_content`, which is what the console gates its
    # "view license text" affordance on.
    assert body["effective_license"]["has_content"] is True
    child = next(e for e in body["evaluations"] if e["id"] == str(inheriting.id))
    assert child["effective_license"]["name"] == "No license"


# --- Partial draft: POST /evaluation-groups/draft ---


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_with_title_only_returns_201_with_nulls(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post("/api/v1/evaluation-groups/draft", json={"title": "Draft only"})

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["status"] == PublicationStatus.DRAFT.value
    assert body["title"] == "Draft only"
    assert body["description"] is None
    # access_level is NOT nullable — a title-only draft defaults to `invitation_only`
    # (a safe, never-public default) rather than null.
    assert body["access_level"] == EvaluationGroupAccessLevel.INVITATION_ONLY.value
    # Same for the metrics levels: the `members_personal_metrics` default rather than null.
    assert body["metrics_access_during"] == MetricsAccessLevel.MEMBERS_PERSONAL_METRICS.value
    assert body["metrics_access_after"] == MetricsAccessLevel.MEMBERS_PERSONAL_METRICS.value
    assert body["start_date"] is None
    # A title-only draft defaults to `invitation_only`, which derives "no license".
    assert body["data_license_id"] == str(curated_license_id(NO_LICENSE_SPDX_ID))
    assert body["effective_license"]["name"] == "No license"
    assert body["created_by_id"] == str(caller.id)
    assert response.headers["location"] == f"/api/v1/evaluation-groups/{body['id']}"


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_roundtrips_metrics_access_levels(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={
                "title": "Draft with metrics config",
                "metrics_access_during": MetricsAccessLevel.ALL_MEMBERS.value,
                "metrics_access_after": MetricsAccessLevel.INHERIT_GROUP_ACCESS.value,
            },
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["metrics_access_during"] == MetricsAccessLevel.ALL_MEMBERS.value
    assert body["metrics_access_after"] == MetricsAccessLevel.INHERIT_GROUP_ACCESS.value


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_accepts_data_license(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={"title": "Draft", "data_license_id": str(curated_license_id("CC0-1.0"))},
        )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["data_license_id"] == str(curated_license_id("CC0-1.0"))
    assert response.json()["effective_license"]["spdx_id"] == "CC0-1.0"


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_with_explicit_null_data_license_inherits_platform_default(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft", json={"title": "Draft", "data_license_id": None}
        )

    assert response.status_code == status.HTTP_201_CREATED
    # A draft defaults to `invitation_only`, so only the explicit null keeps it inheriting.
    assert response.json()["data_license_id"] is None
    assert response.json()["effective_license"]["spdx_id"] == "CC-BY-4.0"


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_missing_or_empty_title_returns_422(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        empty = await async_client_with_db.post("/api/v1/evaluation-groups/draft", json={})
        blank = await async_client_with_db.post("/api/v1/evaluation-groups/draft", json={"title": ""})

    assert empty.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert blank.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_out_of_order_dates_returns_400(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={"title": "D", "start_date": _future(20).isoformat(), "end_date": _future(10).isoformat()},
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_skips_org_invariant_and_past_start(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A draft intentionally skips the full create's completeness checks: the
    # `organization` invariant and the start-not-before-today rule don't apply.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={"title": "D", "access_level": "organization", "start_date": _future(-5).isoformat()},
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["access_level"] == "organization"
    assert body["organization_id"] is None


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_then_patch_completes_it(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    caller = await _user(db_session, creator)

    with as_user(caller):
        created = await async_client_with_db.post("/api/v1/evaluation-groups/draft", json={"title": "D"})
        group_id = created.json()["id"]
        patched = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group_id}",
            json={
                "description": "now filled in",
                "access_level": "public",
                "start_date": _future(5).isoformat(),
            },
        )

    assert patched.status_code == status.HTTP_200_OK
    body = patched.json()
    assert body["description"] == "now filled in"
    assert body["access_level"] == "public"
    assert body["start_date"] == _future(5).isoformat()


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_with_own_org_returns_201(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Tenant isolation isn't a completeness check, so it applies to drafts too — but
    # the caller's *own* org is always allowed.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    org = await _org(db_session)
    caller = await _user_in_org(db_session, creator, org)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={"title": "Draft for my org", "access_level": "organization", "organization_id": str(org.id)},
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["status"] == PublicationStatus.DRAFT.value
    assert body["organization_id"] == str(org.id)


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_with_foreign_org_returns_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The /draft path must run the same cross-tenant guard as create/update — a
    # non-manage owner can't plant a draft into another tenant's org (which the
    # `organization` visibility arm, not draft-gated, would otherwise expose to that
    # org's members).
    creator = await make_role(db_session, ["evaluation_groups:create"])
    own_org = await _org(db_session, name="Home Org")
    foreign_org = await _org(db_session, name="Foreign Inc")
    caller = await _user_in_org(db_session, creator, own_org)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={"title": "Planted", "access_level": "organization", "organization_id": str(foreign_org.id)},
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.usefixtures("system_roles")
async def test_create_draft_with_foreign_org_allowed_for_manager(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The `evaluation_groups:manage` break-glass lifts the own-org constraint on the
    # draft path too, mirroring create/update.
    manager = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:manage"])
    foreign_org = await _org(db_session, name="Foreign Inc")
    caller = await _user(db_session, manager)

    with as_user(caller):
        response = await async_client_with_db.post(
            "/api/v1/evaluation-groups/draft",
            json={"title": "Managed draft", "access_level": "organization", "organization_id": str(foreign_org.id)},
        )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["organization_id"] == str(foreign_org.id)


# --- Duplicate-from-template deep copy: POST /{id}/duplicate ---


@pytest.mark.usefixtures("system_roles")
async def test_duplicate_deep_copies_children_into_new_draft(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status_=PublicationStatus.PUBLISHED,
        title="Template",
    )
    evaluation = await _evaluation(db_session, group=source, title="Eval A")
    model = AiModel(
        name="claude", model_alias="claude", provider=ProviderVendor.ANTHROPIC, provider_model_id="claude-3-5"
    )
    db_session.add(model)
    await db_session.flush()
    db_session.add(
        EvaluationAiModel(
            evaluation_id=evaluation.id,
            model_id=model.id,
            model_display_mask="Masked",
            parameters={"temperature": 0.3},
        )
    )
    scenario = Scenario(name="S1", description="d", evaluation_id=evaluation.id, position=2, required_reviews=3)
    db_session.add(scenario)
    await db_session.flush()
    db_session.add(Task(name="T1", description="d", scenario_id=scenario.id))
    await db_session.flush()
    source_id, eval_id, model_id = source.id, evaluation.id, model.id

    with as_user(caller):
        response = await async_client_with_db.post(
            f"/api/v1/evaluation-groups/{source_id}/duplicate?include_children=true"
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    new_id = UUID(body["id"])
    assert new_id != source_id
    assert body["status"] == PublicationStatus.DRAFT.value
    assert body["title"] == "Template"
    assert body["created_by_id"] == str(caller.id)
    assert response.headers["location"] == f"/api/v1/evaluation-groups/{new_id}"

    new_evals = (
        (await db_session.execute(select(Evaluation).where(col(Evaluation.evaluation_group_id) == new_id)))
        .scalars()
        .all()
    )
    assert len(new_evals) == 1
    new_eval = new_evals[0]
    assert new_eval.id != eval_id
    assert new_eval.title == "Eval A"
    assert new_eval.created_by_id == caller.id

    new_scenarios = (
        (await db_session.execute(select(Scenario).where(col(Scenario.evaluation_id) == new_eval.id))).scalars().all()
    )
    assert len(new_scenarios) == 1
    assert new_scenarios[0].position == 2  # preserved verbatim
    assert new_scenarios[0].required_reviews == 3

    new_tasks = (
        (await db_session.execute(select(Task).where(col(Task.scenario_id) == new_scenarios[0].id))).scalars().all()
    )
    assert [t.name for t in new_tasks] == ["T1"]

    new_assignments = (
        (await db_session.execute(select(EvaluationAiModel).where(col(EvaluationAiModel.evaluation_id) == new_eval.id)))
        .scalars()
        .all()
    )
    assert len(new_assignments) == 1
    assert new_assignments[0].model_id == model_id
    assert new_assignments[0].model_display_mask == "Masked"
    assert new_assignments[0].parameters == {"temperature": 0.3}


@pytest.mark.usefixtures("system_roles")
async def test_duplicate_copies_each_evaluations_tag_schema(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Governance travels with a cloned evaluation: a restriction that lapsed on the copy would let
    # in tag keys the source forbids, so the two flags and the allowed keys move together.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = await _evaluation(db_session, group=source, title="Tagged")
    evaluation.tags_enabled = False
    evaluation.tags_restricted = True
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.flush()
    source_id = source.id

    with as_user(caller):
        response = await async_client_with_db.post(
            f"/api/v1/evaluation-groups/{source_id}/duplicate?include_children=true"
        )

    assert response.status_code == status.HTTP_201_CREATED
    new_eval = (
        (
            await db_session.execute(
                select(Evaluation).where(col(Evaluation.evaluation_group_id) == UUID(response.json()["id"]))
            )
        )
        .scalars()
        .one()
    )
    assert new_eval.tags_enabled is False
    assert new_eval.tags_restricted is True
    copied_keys = (
        (await db_session.execute(select(EvaluationTagKey).where(col(EvaluationTagKey.evaluation_id) == new_eval.id)))
        .scalars()
        .all()
    )
    assert [row.key for row in copied_keys] == ["env"]


@pytest.mark.usefixtures("system_roles")
async def test_duplicate_copies_data_license(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # The license is a group field like title/dates — a template's license travels to the copy.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        data_license_id=curated_license_id("CC0-1.0"),
    )

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source.id}/duplicate")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["data_license_id"] == str(curated_license_id("CC0-1.0"))
    assert response.json()["effective_license"]["spdx_id"] == "CC0-1.0"


@pytest.mark.usefixtures("system_roles")
async def test_duplicate_of_private_group_keeps_source_license(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A private source keeps its explicit licence; the copy does not re-derive "no license".
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        data_license_id=curated_license_id("CC0-1.0"),
    )

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source.id}/duplicate")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["data_license_id"] == str(curated_license_id("CC0-1.0"))


@pytest.mark.usefixtures("system_roles")
async def test_embedded_license_reports_no_text_when_the_catalog_ships_none(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `has_content` on an embedded `effective_license` is derived from the loaded row — this is the
    # only path that still derives it, and the `False` arm is what the console needs to link out to
    # the canonical text instead of opening a dialog onto an empty body.
    role = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:read"])
    caller = await _user(db_session, role)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        data_license_id=curated_license_id("CC0-1.0"),
    )

    with as_user(caller):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["effective_license"]["has_content"] is False


@pytest.mark.usefixtures("system_roles")
async def test_duplicate_of_private_group_inheriting_keeps_inheriting(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The sibling arm: a private source that inherits carries NO licence id, so a copy that re-derived
    # from the access level would hand it "no license" and silently unlicense an inheriting group.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        data_license_id=None,
    )

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source.id}/duplicate")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["data_license_id"] is None


@pytest.mark.usefixtures("system_roles")
async def test_changing_access_level_leaves_the_license_alone(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A group whose delivered exports already name a licence must not have it rewritten by a later
    # visibility change, so a *stored* licence survives the re-derivation the test below covers.
    role = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    caller = await _user(db_session, role)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        data_license_id=curated_license_id("CC0-1.0"),
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"access_level": "invitation_only"}
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["data_license_id"] == str(curated_license_id("CC0-1.0"))


@pytest.mark.usefixtures("system_roles")
async def test_going_private_derives_the_sentinel_for_a_group_that_inherits(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An inheriting group never chose a licence — its effective one is whatever the platform default
    # is at read time — so there is nothing to preserve, and leaving it inheriting would hand a
    # private engagement the shareable default with no server-side signal.
    role = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    caller = await _user(db_session, role)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        data_license_id=None,
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"access_level": "invitation_only"}
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["data_license_id"] == str(curated_license_id(NO_LICENSE_SPDX_ID))


@pytest.mark.usefixtures("system_roles")
async def test_going_private_keeps_an_explicit_null_in_the_same_patch(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `null` is a decision, not an absence: the console sends the licence field on every edit, so
    # deriving over it would override the value the operator is looking at.
    role = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    caller = await _user(db_session, role)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        data_license_id=None,
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={"access_level": "invitation_only", "data_license_id": None},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["data_license_id"] is None


@pytest.mark.usefixtures("system_roles")
async def test_resending_the_same_access_level_does_not_derive_a_license(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Clients PATCH the whole form, so the field arrives unchanged on every edit; keying on the
    # merged state instead of an actual change would rewrite the licence of any private group whose
    # title someone edits.
    role = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    caller = await _user(db_session, role)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        data_license_id=None,
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}",
            json={"title": "Renamed", "access_level": "invitation_only"},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["data_license_id"] is None


@pytest.mark.usefixtures("system_roles")
async def test_going_public_keeps_the_no_license_sentinel(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The mirror direction is deliberately not symmetric: the sentinel is a stored id, and nothing
    # distinguishes one a create derived from one an operator picked — so opening the group up leaves
    # its data unlicensed until someone says otherwise.
    role = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:update"])
    caller = await _user(db_session, role)
    group = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        data_license_id=curated_license_id(NO_LICENSE_SPDX_ID),
    )

    with as_user(caller):
        response = await async_client_with_db.patch(
            f"/api/v1/evaluation-groups/{group.id}", json={"access_level": "public"}
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["data_license_id"] == str(curated_license_id(NO_LICENSE_SPDX_ID))


@pytest.mark.usefixtures("system_roles")
async def test_duplicate_skips_soft_deleted_model_assignment(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="T")
    evaluation = await _evaluation(db_session, group=source, title="Eval A")
    model = AiModel(name="dead", model_alias="dead", provider=ProviderVendor.ANTHROPIC, provider_model_id="x")
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id))
    await db_session.flush()
    model.soft_delete(None)
    await db_session.flush()
    source_id = source.id

    with as_user(caller):
        response = await async_client_with_db.post(
            f"/api/v1/evaluation-groups/{source_id}/duplicate?include_children=true"
        )

    assert response.status_code == status.HTTP_201_CREATED
    new_id = UUID(response.json()["id"])
    new_eval = (
        (await db_session.execute(select(Evaluation).where(col(Evaluation.evaluation_group_id) == new_id)))
        .scalars()
        .one()
    )
    new_assignments = (
        (await db_session.execute(select(EvaluationAiModel).where(col(EvaluationAiModel.evaluation_id) == new_eval.id)))
        .scalars()
        .all()
    )
    assert new_assignments == []  # dead-model assignment not copied


@pytest.mark.usefixtures("system_roles")
async def test_duplicate_unseen_source_returns_404(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    other = await _user(db_session, creator)
    private = await _group(db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    private_id = private.id

    with as_user(caller):
        seen = await async_client_with_db.post(f"/api/v1/evaluation-groups/{private_id}/duplicate")
        unknown = await async_client_with_db.post(f"/api/v1/evaluation-groups/{uuid4()}/duplicate")

    assert seen.status_code == status.HTTP_404_NOT_FOUND
    assert unknown.status_code == status.HTTP_404_NOT_FOUND


# --- organization access visibility (read scoping) ---------------------------


async def test_list_organization_group_scoped_to_callers_live_org(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An `organization` group is visible only to a caller whose *live* org matches
    # it — a member of another org and an orgless caller both miss it.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    org = await _org(db_session)
    other_org = await _org(db_session, name="Other Inc")
    owner = await _user_in_org(db_session, reader, org)
    insider = await _user_in_org(db_session, reader, org)
    outsider = await _user_in_org(db_session, reader, other_org)
    orgless = await _user(db_session, reader)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )

    with as_user(insider):
        insider_view = await async_client_with_db.get("/api/v1/evaluation-groups")
    with as_user(outsider):
        outsider_view = await async_client_with_db.get("/api/v1/evaluation-groups")
    with as_user(orgless):
        orgless_view = await async_client_with_db.get("/api/v1/evaluation-groups")

    assert {item["id"] for item in insider_view.json()["items"]} == {str(group.id)}
    assert outsider_view.json()["total"] == 0
    assert orgless_view.json()["total"] == 0


async def test_list_public_group_with_org_stays_platform_wide(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # `public` is a separate axis from belonging: a public group that happens to
    # carry an org is still visible to a caller in a different org.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    org = await _org(db_session)
    other_org = await _org(db_session, name="Other Inc")
    owner = await _user_in_org(db_session, reader, org)
    outsider = await _user_in_org(db_session, reader, other_org)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.PUBLIC, organization_id=org.id
    )

    with as_user(outsider):
        response = await async_client_with_db.get("/api/v1/evaluation-groups")

    assert str(group.id) in {item["id"] for item in response.json()["items"]}


async def test_list_organization_group_fails_closed_when_org_soft_deleted(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Fail-closed: once the owning org is tombstoned the caller's live org reads as
    # None and the group's org is dead, so the `organization` arm stops matching.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    org = await _org(db_session)
    member = await _user_in_org(db_session, reader, org)
    group = await _group(
        db_session, owner=member, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )
    # The owner keeps access via the in-group owner role, so use a plain member to
    # isolate the org arm; grant no in-group role here.
    plain = await _user_in_org(db_session, reader, org)

    with as_user(plain):
        before = await async_client_with_db.get("/api/v1/evaluation-groups")
    org.soft_delete(None)
    await db_session.flush()
    with as_user(plain):
        after = await async_client_with_db.get("/api/v1/evaluation-groups")

    assert {item["id"] for item in before.json()["items"]} == {str(group.id)}
    assert after.json()["total"] == 0


async def test_get_organization_group_visible_to_member_hidden_from_outsider(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await make_role(db_session, ["evaluation_groups:read"])
    org = await _org(db_session)
    other_org = await _org(db_session, name="Other Inc")
    owner = await _user_in_org(db_session, reader, org)
    insider = await _user_in_org(db_session, reader, org)
    outsider = await _user_in_org(db_session, reader, other_org)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )

    with as_user(insider):
        seen = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")
    with as_user(outsider):
        hidden = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert seen.status_code == status.HTTP_200_OK
    assert seen.json()["id"] == str(group.id)
    assert hidden.status_code == status.HTTP_404_NOT_FOUND


async def test_members_endpoint_applies_org_visibility(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The members endpoint resolves visibility through the in-Python gate
    # (`group_is_visible`), so its org arm must agree with the SQL predicate: an
    # org peer sees the roster, an outsider gets 404.
    reader = await make_role(db_session, ["evaluation_groups:read"])
    org = await _org(db_session)
    other_org = await _org(db_session, name="Other Inc")
    owner = await _user_in_org(db_session, reader, org)
    insider = await _user_in_org(db_session, reader, org)
    outsider = await _user_in_org(db_session, reader, other_org)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )

    with as_user(insider):
        insider_view = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}/members")
    with as_user(outsider):
        outsider_view = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}/members")

    assert insider_view.status_code == status.HTTP_200_OK
    assert outsider_view.status_code == status.HTTP_404_NOT_FOUND


async def test_update_org_group_by_org_peer_without_role_is_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An org peer can *see* the group (org arm) but holds no in-group role, so a
    # write is 403 (visible-but-insufficient) — not 404. Exercises the re-query
    # in `assert_group_write_access` picking up the org arm.
    updater = await make_role(db_session, ["evaluation_groups:update"])
    org = await _org(db_session)
    owner = await _user_in_org(db_session, updater, org)
    peer = await _user_in_org(db_session, updater, org)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )

    with as_user(peer):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Hijack"})

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_duplicate_without_children_copies_group_only(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Default (include_children omitted) clones the group alone — no evaluations.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(
        db_session,
        owner=caller,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        title="Template",
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
        metrics_access_after=MetricsAccessLevel.INHERIT_GROUP_ACCESS,
    )
    await _evaluation(db_session, group=source, title="Eval A")
    source_id = source.id

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source_id}/duplicate")

    assert response.status_code == status.HTTP_201_CREATED
    # The metrics-access config is part of the group's own fields — copied like
    # `access_level`, not reset to the defaults.
    body = response.json()
    assert body["metrics_access_during"] == MetricsAccessLevel.ALL_MEMBERS.value
    assert body["metrics_access_after"] == MetricsAccessLevel.INHERIT_GROUP_ACCESS.value
    new_id = UUID(body["id"])
    new_evals = (
        (await db_session.execute(select(Evaluation).where(col(Evaluation.evaluation_group_id) == new_id)))
        .scalars()
        .all()
    )
    assert new_evals == []  # children not copied without include_children


async def test_duplicate_copies_allowed_models(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    source = await _group(db_session, owner=caller, access_level=EvaluationGroupAccessLevel.PUBLIC, title="Template")
    model = AiModel(name="dup-model", model_alias="dup-model", provider=ProviderVendor.ANTHROPIC, provider_model_id="x")
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=source.id, model_id=model.id))
    await db_session.flush()
    source_id = source.id

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source_id}/duplicate")

    assert response.status_code == status.HTTP_201_CREATED
    new_id = UUID(response.json()["id"])
    subset = (
        (
            await db_session.execute(
                EvaluationGroupAiModel.live_select().where(col(EvaluationGroupAiModel.evaluation_group_id) == new_id)
            )
        )
        .scalars()
        .all()
    )
    assert {row.model_id for row in subset} == {model.id}


async def test_duplicate_visible_but_not_owner_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A caller who can *see* a public group but is not its owner cannot clone it —
    # cloning is owner-or-manage, not merely visible.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    caller = await _user(db_session, creator)
    other = await _user(db_session, creator)
    source = await _group(
        db_session, owner=other, access_level=EvaluationGroupAccessLevel.PUBLIC, status_=PublicationStatus.PUBLISHED
    )

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source.id}/duplicate")

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_duplicate_manager_can_duplicate_others_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # Break-glass `evaluation_groups:manage` lets a non-owner clone another's group.
    manager = await make_role(db_session, ["evaluation_groups:create", "evaluation_groups:manage"])
    caller = await _user(db_session, manager)
    other = await _user(db_session, manager)
    source = await _group(
        db_session, owner=other, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY, title="Theirs"
    )

    with as_user(caller):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source.id}/duplicate")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["created_by_id"] == str(caller.id)


async def test_duplicate_by_member_with_update_role(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The duplicate gate is write access (an in-group role granting
    # `evaluation_groups:update`), not creator-only: a non-creator granted the
    # in-group `owner` role on the source can clone it.
    creator = await make_role(db_session, ["evaluation_groups:create"])
    owner = await _user(db_session, creator)
    member = await _user(db_session, creator)
    source = await _group(db_session, owner=owner, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, source.id, member.id, [await ensure_owner_role(db_session)]
    )

    with as_user(member):
        response = await async_client_with_db.post(f"/api/v1/evaluation-groups/{source.id}/duplicate")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["created_by_id"] == str(member.id)


async def test_update_org_group_by_outsider_reads_as_missing(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An outsider can't see the org group at all, so a write reads as 404 — the
    # gate never leaks an invisible group's existence.
    updater = await make_role(db_session, ["evaluation_groups:update"])
    org = await _org(db_session)
    other_org = await _org(db_session, name="Other Inc")
    owner = await _user_in_org(db_session, updater, org)
    outsider = await _user_in_org(db_session, updater, other_org)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )

    with as_user(outsider):
        response = await async_client_with_db.patch(f"/api/v1/evaluation-groups/{group.id}", json={"title": "Hijack"})

    assert response.status_code == status.HTTP_404_NOT_FOUND
