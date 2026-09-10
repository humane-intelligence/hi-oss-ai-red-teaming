"""Tests for the evaluation-group service helpers."""

import re
from datetime import UTC
from datetime import date
from datetime import datetime

import pytest
import time_machine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.filters import EvaluationGroupFilters
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.evaluations.services.evaluation_groups import collect_publication_blockers
from app.core.evaluations.services.evaluation_groups import collect_publish_blockers
from app.core.evaluations.services.evaluation_groups import collect_submit_blockers
from app.core.evaluations.services.evaluation_groups import default_license_for_access
from app.core.evaluations.services.evaluation_groups import get_evaluation_group
from app.core.evaluations.services.evaluation_groups import list_evaluation_groups
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from tests.conftest import persist_evaluation_group

UUID_IN_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("access_level", "expected"),
    [
        (EvaluationGroupAccessLevel.INVITATION_ONLY, curated_license_id(NO_LICENSE_SPDX_ID)),
        (EvaluationGroupAccessLevel.PUBLIC, None),
        (EvaluationGroupAccessLevel.ORGANIZATION, None),
    ],
)
def test_default_license_for_access(access_level: EvaluationGroupAccessLevel, expected: object) -> None:
    assert default_license_for_access(access_level) == expected


@pytest.mark.integration
async def test_group_detail_does_not_load_the_license_text(db_session: AsyncSession) -> None:
    # The group detail resolves its licence through the relationship loader, not the batch resolver —
    # the second hot path, and the one the console hits most. Asserted on the ORM, not on the
    # response: the response schema omits `content` either way, so its shape proves nothing.
    owner = User(email="grp-lic-defer@example.com", hashed_password="x", is_active=True)
    db_session.add(owner)
    await db_session.flush()
    group = EvaluationGroup(
        title="G",
        description="d",
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        start_date=datetime.now(UTC).date(),
        created_by_id=owner.id,
        # Not a draft: a `public` draft is deliberately invisible by access alone.
        status=PublicationStatus.APPROVED,
        data_license_id=curated_license_id(NO_LICENSE_SPDX_ID),
    )
    db_session.add(group)
    await db_session.flush()
    evaluation = Evaluation(
        title="E",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=owner.id,
        data_license_id=curated_license_id(NO_LICENSE_SPDX_ID),
    )
    db_session.add(evaluation)
    await db_session.flush()

    loaded = await get_evaluation_group(
        db_session,
        group.id,
        caller_id=owner.id,
        with_evaluations=True,
    )

    lic = loaded.data_license
    assert lic is not None
    assert lic.has_text is True
    state = sa_inspect(lic)
    assert state is not None
    assert "content" in state.unloaded
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = lic.content

    # The child embed is a second loader with its own `defer`, and the group above carries no
    # evaluations — so without one here that hunk is unexercised (the embed's JSON is identical
    # either way, since `DataLicenseSummary` has no `content` field).
    child_lic = loaded.evaluations[0].data_license
    assert child_lic is not None
    assert child_lic.has_text is True
    child_state = sa_inspect(child_lic)
    assert child_state is not None
    assert "content" in child_state.unloaded
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = child_lic.content


@pytest.mark.integration
async def test_group_list_does_not_load_the_license_text(db_session: AsyncSession) -> None:
    # The list is the page-sized path: the relationship is `lazy="selectin"`, so the licence rows come
    # back whether asked for or not, and one legal text per distinct licence would ride along invisibly
    # — the response projects `DataLicenseSummary`, which has no text field, so its shape proves
    # nothing either way.
    owner = User(email="grp-list-lic-defer@example.com", hashed_password="x", is_active=True)
    db_session.add(owner)
    await db_session.flush()
    group = EvaluationGroup(
        title="Listed",
        description="d",
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        start_date=datetime.now(UTC).date(),
        created_by_id=owner.id,
        status=PublicationStatus.APPROVED,
        data_license_id=curated_license_id(NO_LICENSE_SPDX_ID),
    )
    db_session.add(group)
    await db_session.flush()

    groups, _ = await list_evaluation_groups(
        db_session,
        caller_id=owner.id,
        show_all=True,
        filters=EvaluationGroupFilters(),
        order_by="-created_at",
        limit=20,
        offset=0,
    )

    listed = next(g for g in groups if g.id == group.id)
    lic = listed.data_license
    assert lic is not None
    assert lic.has_text is True
    state = sa_inspect(lic)
    assert state is not None
    assert "content" in state.unloaded
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = lic.content


@pytest.mark.integration
async def test_submit_blockers_collect_every_gap_at_once(db_session: AsyncSession) -> None:
    # The gate collects instead of failing fast on the first gap: the owner has to see
    # the whole list before clicking submit, not one gap per attempt.
    group = await persist_evaluation_group(db_session, status=PublicationStatus.DRAFT, start_date=date(2020, 1, 1))
    group.description = None
    await db_session.flush()

    blockers = await collect_submit_blockers(db_session, group)

    assert "Fill in the required fields: description." in blockers
    assert "The start date must not be before today." in blockers
    assert "Assign at least one allowed model." in blockers


@pytest.mark.integration
async def test_submit_blockers_carry_the_organization_rule(db_session: AsyncSession) -> None:
    # The collector borrows the write path's org guard by catching its `BadRequestError`,
    # so this is the one blocker whose wiring a type change could silently drop.
    group = await persist_evaluation_group(
        db_session,
        status=PublicationStatus.DRAFT,
        access_level=EvaluationGroupAccessLevel.ORGANIZATION,
        start_date=datetime.now(UTC).date(),
    )

    blockers = await collect_submit_blockers(db_session, group)

    assert "organization_id is required when access_level is 'organization'." in blockers


@pytest.mark.integration
async def test_publish_blockers_flag_a_group_with_no_evaluations(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)

    assert await collect_publish_blockers(db_session, group) == ["Add at least one evaluation to the group."]


@pytest.mark.integration
async def test_publish_blockers_name_evaluations_by_title_without_ids(db_session: AsyncSession) -> None:
    # The blocker is rendered verbatim in the console's readiness panel, the button tooltip and the
    # refusal toast, and `frontend/CLAUDE.md` forbids a raw UUID in the UI.
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)
    for title in ("Jailbreak probes", "Alignment probes"):
        db_session.add(
            Evaluation(title=title, description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
        )
    await db_session.flush()

    blockers = await collect_publish_blockers(db_session, group)

    assert blockers == ["Every evaluation needs at least one scenario; add one to: Alignment probes, Jailbreak probes."]
    assert not UUID_IN_TEXT.search(blockers[0])


@pytest.mark.integration
async def test_publish_blockers_empty_once_every_evaluation_is_playable(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, status=PublicationStatus.APPROVED)
    evaluation = Evaluation(title="e", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    db_session.add(Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0))
    await db_session.flush()

    assert await collect_publish_blockers(db_session, group) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("group_status", "expected"),
    [
        (PublicationStatus.DRAFT, ["Assign at least one allowed model."]),
        (PublicationStatus.CHANGES_REQUESTED, ["Assign at least one allowed model."]),
        (PublicationStatus.APPROVED, ["Add at least one evaluation to the group."]),
        (PublicationStatus.PENDING_APPROVAL, []),
        (PublicationStatus.PUBLISHED, []),
        (PublicationStatus.NOT_APPROVED, []),
        (PublicationStatus.INACTIVE, []),
    ],
)
async def test_publication_blockers_describe_the_next_lifecycle_step(
    db_session: AsyncSession, group_status: PublicationStatus, expected: list[str]
) -> None:
    # The same bare group reads differently per status: the submit gate applies while it
    # is still the owner's to complete, the publish gate once approved, and nothing where
    # no owner-fixable transition is next.
    # Frozen clock so `start_date` stays *exactly* today — the boundary the gate's `<` has
    # to accept — without a real midnight flip adding the start-date blocker mid-test.
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    with time_machine.travel(now, tick=False):
        group = await persist_evaluation_group(db_session, status=group_status, start_date=now.date())

        assert await collect_publication_blockers(db_session, group) == expected
