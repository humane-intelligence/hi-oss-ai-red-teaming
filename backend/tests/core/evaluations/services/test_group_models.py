"""Integration tests for `app.core.evaluations.services.group_models` — service layer over a real DB."""

import uuid
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationGroupAiModel
from app.core.evaluations.services.assignments import assign_model
from app.core.evaluations.services.evaluation_groups import get_evaluation_group
from app.core.evaluations.services.group_models import assert_model_assignable_to_group
from app.core.evaluations.services.group_models import sync_group_models
from app.core.evaluations.services.group_models import unassign_group_models_for_model
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from tests.conftest import persist_evaluation_group


async def _persist_model(db_session: AsyncSession, *, model_alias: str = "m") -> AiModel:
    model = AiModel(
        name=model_alias,
        model_alias=model_alias,
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
    )
    db_session.add(model)
    await db_session.flush()
    return model


async def _persist_evaluation(db_session: AsyncSession, group: EvaluationGroup, *, title: str = "eval") -> Evaluation:
    evaluation = Evaluation(
        title=title, description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    return evaluation


async def _live_subset_ids(db_session: AsyncSession, group_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (
        (
            await db_session.execute(
                EvaluationGroupAiModel.live_select().where(col(EvaluationGroupAiModel.evaluation_group_id) == group_id)
            )
        )
        .scalars()
        .all()
    )
    return {row.model_id for row in rows}


@pytest.mark.integration
async def test_sync_populates_empty_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    alpha = await _persist_model(db_session, model_alias="alpha")
    beta = await _persist_model(db_session, model_alias="beta")

    previous, current = await sync_group_models(
        db_session, group_id=group.id, model_ids=[alpha.id, beta.id], by_id=uuid4()
    )

    assert previous == []
    assert current == sorted([alpha.id, beta.id])
    assert await _live_subset_ids(db_session, group.id) == {alpha.id, beta.id}


@pytest.mark.integration
async def test_sync_dedupes_input(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)

    _, current = await sync_group_models(db_session, group_id=group.id, model_ids=[model.id, model.id], by_id=uuid4())

    assert current == [model.id]
    assert await _live_subset_ids(db_session, group.id) == {model.id}


@pytest.mark.integration
async def test_sync_unknown_model_raises_bad_request(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    unknown = uuid.uuid4()

    with pytest.raises(BadRequestError, match=str(unknown)):
        await sync_group_models(db_session, group_id=group.id, model_ids=[unknown], by_id=uuid4())


@pytest.mark.integration
async def test_sync_soft_deleted_model_raises_bad_request(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)
    model.soft_delete(None)
    await db_session.flush()

    with pytest.raises(BadRequestError):
        await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())


@pytest.mark.integration
async def test_sync_replaces_set(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    kept = await _persist_model(db_session, model_alias="kept")
    removed = await _persist_model(db_session, model_alias="removed")
    added = await _persist_model(db_session, model_alias="added")
    await sync_group_models(db_session, group_id=group.id, model_ids=[kept.id, removed.id], by_id=uuid4())

    previous, current = await sync_group_models(
        db_session, group_id=group.id, model_ids=[kept.id, added.id], by_id=uuid4()
    )

    assert previous == sorted([kept.id, removed.id])
    assert current == sorted([kept.id, added.id])
    assert await _live_subset_ids(db_session, group.id) == {kept.id, added.id}


@pytest.mark.integration
async def test_sync_unchanged_set_keeps_rows(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)
    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())
    row_ids_before = {
        row.id
        for row in (
            await db_session.execute(
                EvaluationGroupAiModel.live_select().where(col(EvaluationGroupAiModel.evaluation_group_id) == group.id)
            )
        ).scalars()
    }

    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())

    row_ids_after = {
        row.id
        for row in (
            await db_session.execute(
                EvaluationGroupAiModel.live_select().where(col(EvaluationGroupAiModel.evaluation_group_id) == group.id)
            )
        ).scalars()
    }
    assert row_ids_after == row_ids_before


@pytest.mark.integration
async def test_sync_remove_in_use_raises_conflict(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)
    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())
    evaluation = await _persist_evaluation(db_session, group)
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)

    with pytest.raises(ConflictError):
        await sync_group_models(db_session, group_id=group.id, model_ids=[], by_id=uuid4())


@pytest.mark.integration
async def test_sync_removed_model_can_be_readded(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)
    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())
    await sync_group_models(db_session, group_id=group.id, model_ids=[], by_id=uuid4())

    # Partial unique index excludes the tombstoned row, so re-adding works.
    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())

    assert await _live_subset_ids(db_session, group.id) == {model.id}


@pytest.mark.integration
async def test_assert_assignable_member_passes(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)
    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())

    await assert_model_assignable_to_group(db_session, group_id=group.id, model_id=model.id)


@pytest.mark.integration
async def test_assert_assignable_outsider_raises(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    member = await _persist_model(db_session, model_alias="member")
    outsider = await _persist_model(db_session, model_alias="outsider")
    await sync_group_models(db_session, group_id=group.id, model_ids=[member.id], by_id=uuid4())

    with pytest.raises(BadRequestError):
        await assert_model_assignable_to_group(db_session, group_id=group.id, model_id=outsider.id)


@pytest.mark.integration
async def test_assert_assignable_empty_subset_fails_closed(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)

    with pytest.raises(BadRequestError):
        await assert_model_assignable_to_group(db_session, group_id=group.id, model_id=model.id)


@pytest.mark.integration
async def test_get_group_eager_loads_allowed_models(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session, model_alias="loaded")
    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())
    group_id, owner_id = group.id, group.created_by_id
    # Expire so the relationship read below can only succeed via the eager load.
    db_session.expire_all()

    loaded = await get_evaluation_group(db_session, group_id, caller_id=owner_id, with_allowed_models=True)

    # Reading the relationship (and each row's model) must not lazy-load.
    assert [row.ai_model.name for row in loaded.allowed_models] == ["loaded"]


@pytest.mark.integration
async def test_unassign_group_models_for_model_cascades(db_session: AsyncSession) -> None:
    """Soft-deleting a model unassigns it from every group's subset."""
    group_a = await persist_evaluation_group(db_session, title="a")
    group_b = await persist_evaluation_group(db_session, title="b")
    model = await _persist_model(db_session, model_alias="shared")
    await sync_group_models(db_session, group_id=group_a.id, model_ids=[model.id], by_id=uuid4())
    await sync_group_models(db_session, group_id=group_b.id, model_ids=[model.id], by_id=uuid4())

    count = await unassign_group_models_for_model(db_session, model.id, by_id=uuid4())

    assert count == 2
    assert await _live_subset_ids(db_session, group_a.id) == set()
    assert await _live_subset_ids(db_session, group_b.id) == set()


@pytest.mark.integration
async def test_unassign_group_models_for_model_is_idempotent(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session)
    await sync_group_models(db_session, group_id=group.id, model_ids=[model.id], by_id=uuid4())
    await unassign_group_models_for_model(db_session, model.id, by_id=uuid4())

    count = await unassign_group_models_for_model(db_session, model.id, by_id=uuid4())

    assert count == 0
