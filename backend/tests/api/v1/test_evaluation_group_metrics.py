"""API tests for `GET /api/v1/evaluation-groups/{group_id}/metrics`.

Who may read the metrics is the group's own configuration:
`metrics_access_during` applies while the group runs, `metrics_access_after`
once it is `inactive` — from `owner_only` (in-group `owner` or a
break-glass `evaluation_groups:manage` admin only) through `members_personal_metrics`
(the default: a `view_personal_metrics` member sees a `personal`-scope read of their own data)
and `all_members` (any member sees the full aggregate) up to
`inherit_group_access` (whoever can see the group at all, per its own
`access_level`). Visibility still gates first: a non-member of a private group
is 404, an anonymous caller 401.
"""

from datetime import UTC
from datetime import date
from datetime import datetime
from uuid import uuid4

import pytest
import time_machine
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from tests.api.v1.conftest import as_user
from tests.api.v1.conftest import make_role
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _user(db: AsyncSession, permissions: list[str] | None = None) -> User:
    # Default to the global `evaluation_groups:read` floor every real role carries
    # (the metrics gate requires it, like the group-detail read). Pass an explicit
    # list to override — e.g. `[]` to model a caller lacking that floor.
    role = await make_role(db, [Permission.EVALUATION_GROUPS_READ.value] if permissions is None else permissions)
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role])


async def _member(db: AsyncSession, group: EvaluationGroup, system_roles: dict[str, Role], *roles: SystemRole) -> User:
    """A fresh user granted the given in-group role(s) on `group`."""
    member = await _user(db)
    await grant_roles(
        db, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles[role.value] for role in roles]
    )
    return member


def _url(group: EvaluationGroup) -> str:
    return f"/api/v1/evaluation-groups/{group.id}/metrics"


async def test_owner_gets_full_metrics(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.PUBLIC, start_date=date(2026, 3, 1)
    )

    # Frozen so the timeline's span is exact: the group starts 2026-03-01 and has no submissions,
    # so the axis is its start date through "today" — three zero-filled days, serialised as dates.
    with as_user(owner), time_machine.travel(datetime(2026, 3, 3, 12, tzinfo=UTC), tick=False):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["group_id"] == str(group.id)
    assert body["scope"] == "full"
    assert {
        "scope",
        "members",
        "submissions",
        "reviews",
        "activity",
        "tokens",
        "evaluations",
        "submissions_by_day",
    } <= body.keys()
    assert body["submissions_by_day"] == [
        {"day": "2026-03-01", "submissions": 0, "exploited_submissions": 0},
        {"day": "2026-03-02", "submissions": 0, "exploited_submissions": 0},
        {"day": "2026-03-03", "submissions": 0, "exploited_submissions": 0},
    ]


async def test_caller_without_global_read_is_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The metrics gate enforces the same global `evaluation_groups:read` floor the
    # group detail read does, so a caller lacking it is 403 even as the in-group
    # owner (who would otherwise pass the metrics-access policy). No real role omits
    # read, but this pins the floor so metrics can't be reached past a closed detail.
    owner = await _user(db_session, [])
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.PUBLIC
    )

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


async def test_break_glass_admin_gets_metrics(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    admin = await _user(
        db_session, [Permission.EVALUATION_GROUPS_MANAGE.value, Permission.EVALUATION_GROUPS_READ.value]
    )

    with as_user(admin):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["scope"] == "full"


@pytest.mark.parametrize("member_role", [SystemRole.RED_TEAMER, SystemRole.ANNOTATOR, SystemRole.VIEWER])
async def test_member_is_forbidden_at_owner_only(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    member_role: SystemRole,
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.PUBLIC
    )
    member = await _member(db_session, group, system_roles, member_role)

    with as_user(member):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("member_role", [SystemRole.RED_TEAMER, SystemRole.ANNOTATOR, SystemRole.VIEWER])
async def test_any_member_gets_full_at_all_members(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    member_role: SystemRole,
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
    )
    member = await _member(db_session, group, system_roles, member_role)

    with as_user(member):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    # `all_members` grants the full event-wide aggregate, not a personal slice.
    assert response.json()["scope"] == "full"


async def test_non_member_is_forbidden_at_all_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # On a *public* group a non-member passes visibility (so no 404 hide) but is
    # not a member — `all_members` still turns them away with 403.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
    )
    outsider = await _user(db_session, [Permission.EVALUATION_GROUPS_READ.value])

    with as_user(outsider):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize(
    ("member_roles", "expected_status"),
    [
        ((SystemRole.RED_TEAMER,), status.HTTP_200_OK),
        # The viewer role does NOT carry `view_personal_metrics` (it authors nothing),
        # so it is turned away here — its metrics reach rides the wider levels.
        ((SystemRole.VIEWER,), status.HTTP_403_FORBIDDEN),
        ((SystemRole.ANNOTATOR,), status.HTTP_403_FORBIDDEN),
        # A multi-role member passes via the permission union — the red_teamer
        # role grants `view_personal_metrics` even though annotator does not.
        ((SystemRole.ANNOTATOR, SystemRole.RED_TEAMER), status.HTTP_200_OK),
    ],
)
async def test_members_personal_metrics_admits_only_view_personal_metrics_holders(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    member_roles: tuple[SystemRole, ...],
    expected_status: int,
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    )
    member = await _member(db_session, group, system_roles, *member_roles)

    with as_user(member):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == expected_status
    if expected_status == status.HTTP_200_OK:
        # An admitted member gets a *personal*-scope read, not the full aggregate.
        assert response.json()["scope"] == "personal"


async def test_owner_gets_full_scope_on_a_members_personal_metrics_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The owner always sees the full event-wide aggregate, even at a level that scopes
    # everyone else to their own data.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    )

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["scope"] == "full"


async def test_inherit_group_access_admits_any_caller_who_sees_the_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.INHERIT_GROUP_ACCESS,
    )
    outsider = await _user(db_session, [Permission.EVALUATION_GROUPS_READ.value])

    with as_user(outsider):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["scope"] == "full"


async def test_inherit_group_access_still_hides_an_invisible_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # `inherit_group_access` never bypasses group visibility: a non-member of
    # an invitation-only group still reads 404, whatever the metrics level.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.INHERIT_GROUP_ACCESS,
    )
    outsider = await _user(db_session, [Permission.EVALUATION_GROUPS_READ.value])

    with as_user(outsider):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_after_level_takes_over_when_group_finishes(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # during=inherit_group_access / after=owner_only: the same outsider passes
    # while the group runs and is turned away once it is finished.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.INHERIT_GROUP_ACCESS,
        metrics_access_after=MetricsAccessLevel.OWNER_ONLY,
    )
    outsider = await _user(db_session, [Permission.EVALUATION_GROUPS_READ.value])

    with as_user(outsider):
        assert (await async_client_with_db.get(_url(group))).status_code == status.HTTP_200_OK

    group.status = PublicationStatus.INACTIVE
    db_session.add(group)
    await db_session.flush()

    with as_user(outsider):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_after_level_can_widen_access_on_a_finished_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The inverse switch: during=owner_only / after=all_members — a member
    # is turned away while the group runs and admitted once it is finished.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.OWNER_ONLY,
        metrics_access_after=MetricsAccessLevel.ALL_MEMBERS,
    )
    member = await _member(db_session, group, system_roles, SystemRole.RED_TEAMER)

    with as_user(member):
        assert (await async_client_with_db.get(_url(group))).status_code == status.HTTP_403_FORBIDDEN

    group.status = PublicationStatus.INACTIVE
    db_session.add(group)
    await db_session.flush()

    with as_user(member):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK


async def test_owner_and_admin_pass_on_a_finished_owner_only_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.INACTIVE,
    )
    admin = await _user(
        db_session, [Permission.EVALUATION_GROUPS_MANAGE.value, Permission.EVALUATION_GROUPS_READ.value]
    )

    with as_user(owner):
        assert (await async_client_with_db.get(_url(group))).status_code == status.HTTP_200_OK
    with as_user(admin):
        assert (await async_client_with_db.get(_url(group))).status_code == status.HTTP_200_OK


async def test_non_member_of_private_group_is_not_found(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    outsider = await _user(db_session, [Permission.EVALUATION_GROUPS_READ.value])

    with as_user(outsider):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_unauthenticated_is_unauthorized(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id)

    response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def _evaluation_with_model(db: AsyncSession, group: EvaluationGroup, *, mask_models: bool) -> None:
    """One evaluation of ``group`` with a model assigned — enough for the zero-inclusive roll-up."""
    evaluation = Evaluation(
        title="E",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        mask_models_enabled=mask_models,
    )
    db.add(evaluation)
    await db.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db.add(model)
    await db.flush()
    db.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id))
    await db.flush()


async def test_per_model_spend_is_withheld_from_a_member_when_an_evaluation_masks(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # End-to-end guard on the gate the service can't check for itself: `full_model_access` comes
    # from the *dependency*, so the service tests (which pass it directly) would stay green even if
    # the route resolved it wrongly. A group-level per-model row correlates one model's cost across
    # evaluations, which per-evaluation masking withholds — so an `all_members` reader who is not a
    # `view_metrics` holder gets the totals but no attribution, while the in-group owner gets both.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
    )
    await _evaluation_with_model(db_session, group, mask_models=True)
    reader = await _member(db_session, group, system_roles, SystemRole.RED_TEAMER)
    in_group_owner = await _member(db_session, group, system_roles, SystemRole.OWNER)

    with as_user(reader):
        withheld = await async_client_with_db.get(_url(group))
    with as_user(in_group_owner):
        allowed = await async_client_with_db.get(_url(group))

    assert withheld.status_code == status.HTTP_200_OK
    assert withheld.json()["tokens_by_model"] == []
    # Pinned directly: the empty list alone cannot tell "withheld" from "nothing assigned".
    assert withheld.json()["tokens_by_model_withheld"] is True
    # Only the attribution is withheld — the group total is still served.
    assert withheld.json()["tokens"]["total_tokens"] == 0

    assert allowed.status_code == status.HTTP_200_OK
    assert allowed.json()["tokens_by_model_withheld"] is False
    rows = allowed.json()["tokens_by_model"]
    assert len(rows) == 1
    assert rows[0]["model_alias"]


async def test_per_model_spend_reaches_a_member_when_nothing_is_masked(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The complement: withholding must key on masking, not on merely lacking `view_metrics`.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
    )
    await _evaluation_with_model(db_session, group, mask_models=False)
    reader = await _member(db_session, group, system_roles, SystemRole.RED_TEAMER)

    with as_user(reader):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()["tokens_by_model"]) == 1
    assert response.json()["tokens_by_model_withheld"] is False


async def test_break_glass_admin_gets_per_model_spend_on_a_masking_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The third caller type for this gate, and the one no test covered: a break-glass admin holds no
    # in-group role, so `full_model_access` reaches them only through `ObjectAccessContext.has`'s
    # super-permission short-circuit. Dropping that short-circuit would blind every admin to the
    # attribution on any masking group, with the owner-side and member-side tests still green.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    await _evaluation_with_model(db_session, group, mask_models=True)
    admin = await _user(
        db_session, [Permission.EVALUATION_GROUPS_MANAGE.value, Permission.EVALUATION_GROUPS_READ.value]
    )

    with as_user(admin):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["tokens_by_model_withheld"] is False
    rows = response.json()["tokens_by_model"]
    assert len(rows) == 1
    assert rows[0]["model_alias"]
