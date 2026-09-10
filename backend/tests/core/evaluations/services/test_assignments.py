"""Integration tests for `app.core.evaluations.services.assignments` — service layer over a real DB."""

import uuid
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.config import get_settings
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.filters import EvaluationAiModelFilters
from app.core.evaluations.models import Evaluation
from app.core.evaluations.schemas import EvaluationAiModelUpdateChanges
from app.core.evaluations.schemas import EvaluationAiModelView
from app.core.evaluations.services.assignments import assign_model
from app.core.evaluations.services.assignments import get_assignment
from app.core.evaluations.services.assignments import list_evaluation_models
from app.core.evaluations.services.assignments import soft_delete_assignment
from app.core.evaluations.services.assignments import unassign_models_for_model
from app.core.evaluations.services.assignments import update_assignment
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.restore import restore_cutoff
from tests.conftest import persist_evaluation_group

# These suites exercise the live set; the window is inert but the services now
# require it explicitly, as the routes derive it.
_CUTOFF = restore_cutoff(get_settings())


async def _persist_evaluation(db_session: AsyncSession, *, title: str = "eval") -> Evaluation:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title=title, description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    return evaluation


async def _persist_model(
    db_session: AsyncSession, *, model_alias: str = "assigned", warmup_enabled: bool = False
) -> AiModel:
    model = AiModel(
        name=model_alias,
        model_alias=model_alias,
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
        warmup_enabled=warmup_enabled,
    )
    db_session.add(model)
    await db_session.flush()
    return model


@pytest.mark.integration
async def test_assign_model_persists_row(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)

    assignment = await assign_model(
        db_session,
        evaluation_id=evaluation.id,
        model_id=model.id,
        model_display_mask="Model A",
        parameters={"temperature": 0.2},
    )

    assert assignment.evaluation_id == evaluation.id
    assert assignment.model_id == model.id
    assert assignment.model_display_mask == "Model A"
    assert assignment.parameters == {"temperature": 0.2}


@pytest.mark.integration
async def test_assign_model_defaults_empty_parameters(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)

    assignment = await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)

    assert assignment.parameters == {}
    assert assignment.model_display_mask is None


@pytest.mark.integration
async def test_assign_model_unknown_evaluation_raises_not_found(db_session: AsyncSession) -> None:
    model = await _persist_model(db_session)

    with pytest.raises(NotFoundError):
        await assign_model(db_session, evaluation_id=uuid.uuid4(), model_id=model.id)


@pytest.mark.integration
async def test_assign_model_unknown_model_raises_not_found(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    with pytest.raises(NotFoundError):
        await assign_model(db_session, evaluation_id=evaluation.id, model_id=uuid.uuid4())


@pytest.mark.integration
async def test_assign_soft_deleted_model_raises_not_found(db_session: AsyncSession) -> None:
    """A tombstoned model is invisible to `get_model`, so assigning it is a 404 — not an FK error."""
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)
    model.soft_delete(None)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)


@pytest.mark.integration
async def test_assign_model_duplicate_raises_conflict(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)

    with pytest.raises(ConflictError):
        await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)


@pytest.mark.integration
async def test_assign_model_allows_reassign_after_soft_delete(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)
    first = await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)
    await soft_delete_assignment(db_session, first, by_id=uuid4())

    again = await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)

    assert again.id != first.id


@pytest.mark.integration
async def test_get_assignment_returns_row(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)
    assignment = await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)

    found = await get_assignment(db_session, evaluation.id, assignment.id, for_update=True)

    assert found.id == assignment.id
    assert found.model_id == model.id


@pytest.mark.integration
async def test_get_assignment_missing_raises_not_found(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    with pytest.raises(NotFoundError):
        await get_assignment(db_session, evaluation.id, uuid.uuid4())


@pytest.mark.integration
async def test_get_assignment_other_evaluation_raises_not_found(db_session: AsyncSession) -> None:
    """An assignment id is scoped to its evaluation — reading it under another 404s."""
    evaluation = await _persist_evaluation(db_session, title="a")
    other = await _persist_evaluation(db_session, title="b")
    model = await _persist_model(db_session)
    assignment = await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)

    with pytest.raises(NotFoundError):
        await get_assignment(db_session, other.id, assignment.id)


@pytest.mark.integration
async def test_update_assignment_applies_only_set_fields(db_session: AsyncSession) -> None:
    """An omitted field is left untouched; `model_fields_set` drives what's written."""
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)
    assignment = await assign_model(
        db_session, evaluation_id=evaluation.id, model_id=model.id, model_display_mask="orig", parameters={"top_p": 0.5}
    )

    changes = EvaluationAiModelUpdateChanges(model_display_mask="renamed")
    updated = await update_assignment(db_session, assignment, changes)

    assert updated.model_display_mask == "renamed"
    assert updated.parameters == {"top_p": 0.5}  # untouched — not in the change set


@pytest.mark.integration
async def test_soft_delete_assignment_stamps_deleted_at(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)
    assignment = await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)

    await soft_delete_assignment(db_session, assignment, by_id=uuid4())

    assert assignment.is_deleted is True


@pytest.mark.integration
async def test_unassign_models_for_model_cascades_to_live_assignments(db_session: AsyncSession) -> None:
    """Soft-deleting a model unassigns it everywhere it is live."""
    model = await _persist_model(db_session)
    first = await _persist_evaluation(db_session, title="a")
    second = await _persist_evaluation(db_session, title="b")
    a1 = await assign_model(db_session, evaluation_id=first.id, model_id=model.id)
    a2 = await assign_model(db_session, evaluation_id=second.id, model_id=model.id)

    count = await unassign_models_for_model(db_session, model.id, by_id=uuid4())

    await db_session.refresh(a1)
    await db_session.refresh(a2)
    assert count == 2
    assert a1.is_deleted is True
    assert a2.is_deleted is True


@pytest.mark.integration
async def test_unassign_models_for_model_leaves_other_models_untouched(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    target = await _persist_model(db_session, model_alias="target")
    other = await _persist_model(db_session, model_alias="other")
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=target.id)
    kept = await assign_model(db_session, evaluation_id=evaluation.id, model_id=other.id)

    count = await unassign_models_for_model(db_session, target.id, by_id=uuid4())

    await db_session.refresh(kept)
    assert count == 1
    assert kept.is_deleted is False


@pytest.mark.integration
async def test_unassign_models_for_model_is_idempotent(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    model = await _persist_model(db_session)
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id)
    await unassign_models_for_model(db_session, model.id, by_id=uuid4())

    count = await unassign_models_for_model(db_session, model.id, by_id=uuid4())

    assert count == 0


async def _persist_evaluation_in(
    db_session: AsyncSession, group_id: uuid.UUID, *, created_by_id: uuid.UUID, mask: bool = True, title: str = "eval"
) -> Evaluation:
    evaluation = Evaluation(
        title=title,
        description="d",
        evaluation_group_id=group_id,
        created_by_id=created_by_id,
        mask_models_enabled=mask,
    )
    db_session.add(evaluation)
    await db_session.flush()
    await db_session.refresh(evaluation)
    return evaluation


def _filters(search: str | None = None) -> EvaluationAiModelFilters:
    return EvaluationAiModelFilters(search=search)


@pytest.mark.integration
async def test_list_evaluation_models_masked_search_uses_display_mask(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation_in(db_session, group.id, created_by_id=group.created_by_id, mask=True)
    model = await _persist_model(db_session, model_alias="claude")
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id, model_display_mask="Alpha")

    hit, hit_total, masked = await list_evaluation_models(
        db_session,
        evaluation_id=evaluation.id,
        caller_id=group.created_by_id,
        filters=_filters("alpha"),
        order_by="name",
        limit=10,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )
    _, miss_total, _ = await list_evaluation_models(
        db_session,
        evaluation_id=evaluation.id,
        caller_id=group.created_by_id,
        filters=_filters("claude"),
        order_by="name",
        limit=10,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert masked is True
    assert hit_total == 1
    assert hit[0].model_display_mask == "Alpha"
    assert miss_total == 0  # the real model name is not searchable when masked


@pytest.mark.integration
async def test_list_evaluation_models_masked_loads_warmup_flag(db_session: AsyncSession) -> None:
    # Regression: the masked projection reads `warmup_enabled` off `ai_model`
    # (EvaluationAiModelView.from_assignment), so the masked path must eager-load it.
    # `expunge_all()` empties the identity map — as on a fresh per-request session —
    # so the relationship is genuinely unloaded; without the eager-load, reading it
    # lazy-loads on the async session and raises MissingGreenlet (a 500 in prod).
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation_in(db_session, group.id, created_by_id=group.created_by_id, mask=True)
    model = await _persist_model(db_session, model_alias="warm", warmup_enabled=True)
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id, model_display_mask="Alpha")
    await db_session.flush()
    db_session.expunge_all()

    items, total, masked = await list_evaluation_models(
        db_session,
        evaluation_id=evaluation.id,
        caller_id=group.created_by_id,
        filters=_filters(),
        order_by="name",
        limit=10,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert masked is True
    assert total == 1
    view = EvaluationAiModelView.from_assignment(items[0], masked=True)
    assert view.warmup_enabled is True


@pytest.mark.integration
async def test_list_evaluation_models_unmasked_search_uses_real_name(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation_in(db_session, group.id, created_by_id=group.created_by_id, mask=False)
    model = await _persist_model(db_session, model_alias="claude")
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=model.id, model_display_mask="Alpha")

    items, total, masked = await list_evaluation_models(
        db_session,
        evaluation_id=evaluation.id,
        caller_id=group.created_by_id,
        filters=_filters("claude"),
        order_by="name",
        limit=10,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert masked is False
    assert total == 1
    assert items[0].ai_model.name == "claude"


@pytest.mark.integration
async def test_list_evaluation_models_orders_by_masking_aware_name(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation_in(db_session, group.id, created_by_id=group.created_by_id, mask=True)
    first = await _persist_model(db_session, model_alias="m1")
    second = await _persist_model(db_session, model_alias="m2")
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=first.id, model_display_mask="Bravo")
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=second.id, model_display_mask="Alpha")

    asc, _, _ = await list_evaluation_models(
        db_session,
        evaluation_id=evaluation.id,
        caller_id=group.created_by_id,
        filters=_filters(),
        order_by="name",
        limit=10,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )
    desc, _, _ = await list_evaluation_models(
        db_session,
        evaluation_id=evaluation.id,
        caller_id=group.created_by_id,
        filters=_filters(),
        order_by="-name",
        limit=10,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert [a.model_display_mask for a in asc] == ["Alpha", "Bravo"]
    assert [a.model_display_mask for a in desc] == ["Bravo", "Alpha"]


@pytest.mark.integration
async def test_list_evaluation_models_unmasked_orders_by_real_name(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation_in(db_session, group.id, created_by_id=group.created_by_id, mask=False)
    bravo = await _persist_model(db_session, model_alias="bravo")
    alpha = await _persist_model(db_session, model_alias="alpha")
    # Display masks are deliberately reverse-sorted to prove the unmasked path
    # orders by the real `AiModel.name`, not the mask.
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=bravo.id, model_display_mask="Zulu")
    await assign_model(db_session, evaluation_id=evaluation.id, model_id=alpha.id, model_display_mask="Yankee")

    asc, _, masked = await list_evaluation_models(
        db_session,
        evaluation_id=evaluation.id,
        caller_id=group.created_by_id,
        filters=_filters(),
        order_by="name",
        limit=10,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert masked is False
    assert [a.ai_model.name for a in asc] == ["alpha", "bravo"]


@pytest.mark.integration
async def test_list_evaluation_models_not_visible_raises_not_found(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await _persist_evaluation_in(db_session, group.id, created_by_id=group.created_by_id)

    with pytest.raises(NotFoundError):
        await list_evaluation_models(
            db_session,
            evaluation_id=evaluation.id,
            caller_id=uuid.uuid4(),
            filters=_filters(),
            order_by="name",
            limit=10,
            offset=0,
            deleted_cutoff=_CUTOFF,
        )
