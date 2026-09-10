"""API tests for `GET /api/v1/evaluations/{evaluation_id}/metrics`.

Mirrors the group metrics gate one level down: the parent group's configured
`metrics_access_during` / `metrics_access_after` levels decide the audience; the
in-group `owner` and a break-glass `evaluation_groups:manage` admin always get the
full aggregate. At the default `members_personal_metrics` a `view_personal_metrics` member gets
a `personal`-scope read; at `owner_only` a lesser member is 403. A
non-member of a private group is 404; a missing evaluation is 404; an anonymous
caller is 401. Model names in the by-model distribution are masked for any
non-owner/admin caller when the evaluation masks models. The full per-level matrix
lives with the group dashboard tests (`test_evaluation_group_metrics`) — the gate
is shared, so this file keeps a representative slice.
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


async def _evaluation(db: AsyncSession, group: EvaluationGroup, *, mask_models: bool = False) -> Evaluation:
    evaluation = Evaluation(
        title="E",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        mask_models_enabled=mask_models,
    )
    db.add(evaluation)
    await db.flush()
    return evaluation


async def _assign_model(db: AsyncSession, evaluation: Evaluation, *, display_mask: str | None = None) -> None:
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db.add(model)
    await db.flush()
    db.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id, model_display_mask=display_mask))
    await db.flush()


async def _member(db: AsyncSession, group: EvaluationGroup, system_roles: dict[str, Role], *roles: SystemRole) -> User:
    member = await _user(db)
    await grant_roles(
        db, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles[role.value] for role in roles]
    )
    return member


def _url(evaluation: Evaluation) -> str:
    return f"/api/v1/evaluations/{evaluation.id}/metrics"


async def test_owner_gets_full_metrics(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.PUBLIC, start_date=date(2026, 3, 1)
    )
    evaluation = await _evaluation(db_session, group)

    # Frozen so the timeline's span is exact. It also pins the route's own wiring: the axis exists
    # only because the handler threads the parent group the gate resolved, which no service test can
    # see. The group starts 2026-03-01 and the evaluation has no submissions, so the axis is three
    # zero-filled days up to "today".
    with as_user(owner), time_machine.travel(datetime(2026, 3, 3, 12, tzinfo=UTC), tick=False):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["evaluation_id"] == str(evaluation.id)
    assert body["scope"] == "full"
    assert {
        "scope",
        "submissions",
        "reviews",
        "activity",
        "tokens",
        "scenarios",
        "exploits_by_prompt_count",
        "exploits_by_model",
        "tokens_by_model",
        "submissions_by_day",
    } <= body.keys()
    assert body["submissions_by_day"] == [
        {"day": "2026-03-01", "submissions": 0, "exploited_submissions": 0},
        {"day": "2026-03-02", "submissions": 0, "exploited_submissions": 0},
        {"day": "2026-03-03", "submissions": 0, "exploited_submissions": 0},
    ]


async def test_break_glass_admin_gets_metrics(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    evaluation = await _evaluation(db_session, group)
    admin = await _user(
        db_session, [Permission.EVALUATION_GROUPS_MANAGE.value, Permission.EVALUATION_GROUPS_READ.value]
    )

    with as_user(admin):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_200_OK


async def test_member_without_view_metrics_is_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.PUBLIC
    )
    evaluation = await _evaluation(db_session, group)
    member = await _member(db_session, group, system_roles, SystemRole.RED_TEAMER)

    with as_user(member):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


async def test_non_member_of_private_group_is_not_found(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    evaluation = await _evaluation(db_session, group)
    outsider = await _user(db_session, [Permission.EVALUATION_GROUPS_READ.value])

    with as_user(outsider):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_member_gets_full_at_all_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
    )
    evaluation = await _evaluation(db_session, group)
    member = await _member(db_session, group, system_roles, SystemRole.RED_TEAMER)

    with as_user(member):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["scope"] == "full"


async def test_red_teamer_gets_personal_scope_at_members_personal_metrics(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    )
    evaluation = await _evaluation(db_session, group)
    member = await _member(db_session, group, system_roles, SystemRole.RED_TEAMER)

    with as_user(member):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["scope"] == "personal"


async def test_annotator_is_forbidden_at_members_personal_metrics(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    )
    evaluation = await _evaluation(db_session, group)
    annotator = await _member(db_session, group, system_roles, SystemRole.ANNOTATOR)

    with as_user(annotator):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_model_names_masked_for_non_owner_when_evaluation_masks_models(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # On a masked evaluation, the by-model distribution hides real aliases from a
    # non-owner/admin caller (showing the display mask), while the owner — a
    # full-access `view_metrics` holder — still sees real names. The distribution is
    # zero-inclusive, so the assignment appears even with no exploits recorded.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
    )
    evaluation = await _evaluation(db_session, group, mask_models=True)
    await _assign_model(db_session, evaluation, display_mask="Model A")
    member = await _member(db_session, group, system_roles, SystemRole.RED_TEAMER)

    with as_user(owner):
        owner_body = (await async_client_with_db.get(_url(evaluation))).json()
    with as_user(member):
        member_body = (await async_client_with_db.get(_url(evaluation))).json()

    owner_names = {row["model_alias"] for row in owner_body["exploits_by_model"]}
    member_names = {row["model_alias"] for row in member_body["exploits_by_model"]}
    # The owner sees the real alias (never the mask); the member sees only the mask.
    assert "Model A" not in owner_names
    assert member_names == {"Model A"}


async def test_after_level_takes_over_when_group_finishes(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session,
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        status=PublicationStatus.PUBLISHED,
        metrics_access_during=MetricsAccessLevel.INHERIT_GROUP_ACCESS,
        metrics_access_after=MetricsAccessLevel.OWNER_ONLY,
    )
    evaluation = await _evaluation(db_session, group)
    outsider = await _user(db_session, [Permission.EVALUATION_GROUPS_READ.value])

    with as_user(outsider):
        assert (await async_client_with_db.get(_url(evaluation))).status_code == status.HTTP_200_OK

    group.status = PublicationStatus.INACTIVE
    db_session.add(group)
    await db_session.flush()

    with as_user(outsider):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_missing_evaluation_is_not_found(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    admin = await _user(
        db_session, [Permission.EVALUATION_GROUPS_MANAGE.value, Permission.EVALUATION_GROUPS_READ.value]
    )

    with as_user(admin):
        response = await async_client_with_db.get(f"/api/v1/evaluations/{uuid4()}/metrics")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_unauthenticated_is_unauthorized(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    group = await persist_evaluation_group(db_session, created_by_id=owner.id)
    evaluation = await _evaluation(db_session, group)

    response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_token_metrics_are_projected_over_http(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Contract only — the aggregation branches (partial usage, masking, soft-deleted parents,
    # personal scope) are the service suite's job. What the service tests cannot see is the JSON
    # projection: that `tokens` and the nested per-model `tokens` survive serialisation with the
    # full field set, including the two float averages.
    owner = await _user(db_session)
    group = await persist_evaluation_group(
        db_session, created_by_id=owner.id, access_level=EvaluationGroupAccessLevel.PUBLIC
    )
    evaluation = await _evaluation(db_session, group)
    model = AiModel(name="m", model_alias="m", provider=ProviderVendor.ANTHROPIC, provider_model_id="m-id")
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id))
    await db_session.flush()

    with as_user(owner):
        response = await async_client_with_db.get(_url(evaluation))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    expected_fields = {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "messages_with_usage",
        "conversations_with_usage",
        "avg_tokens_per_message",
        "avg_tokens_per_conversation",
    }
    assert body["tokens"].keys() == expected_fields
    # The assigned model appears even with no spend, carrying the same nested shape.
    assert len(body["tokens_by_model"]) == 1
    row = body["tokens_by_model"][0]
    assert row.keys() == {"evaluation_ai_model_id", "model_alias", "tokens"}
    assert row["tokens"].keys() == expected_fields
    assert isinstance(row["tokens"]["avg_tokens_per_message"], float)
