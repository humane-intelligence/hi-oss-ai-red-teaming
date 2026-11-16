"""CRUD for an evaluation's conversation-label categories.

Labels are metadata for organising and reporting conversations. Nothing here reaches a model
prompt; only conversation tags do.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.evaluations.models import EvaluationLabelCategory
from app.core.evaluations.schemas import MAX_LABEL_CATEGORIES
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError


async def list_label_categories(session: AsyncSession, evaluation_id: UUID) -> list[EvaluationLabelCategory]:
    """The evaluation's live categories, by name ignoring case."""
    result = await session.execute(
        EvaluationLabelCategory.live_select()
        .where(col(EvaluationLabelCategory.evaluation_id) == evaluation_id)
        .order_by(func.lower(col(EvaluationLabelCategory.name)), col(EvaluationLabelCategory.id))
    )
    return list(result.scalars().all())


async def get_label_category(session: AsyncSession, evaluation_id: UUID, category_id: UUID) -> EvaluationLabelCategory:
    """One live category of the evaluation (404 otherwise)."""
    row = (
        await session.execute(
            EvaluationLabelCategory.live_select().where(
                col(EvaluationLabelCategory.id) == category_id,
                col(EvaluationLabelCategory.evaluation_id) == evaluation_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Label category not found.")
    return row


def _assert_name_free(live: list[EvaluationLabelCategory], name: str, *, except_id: UUID | None = None) -> None:
    if any(row.name.casefold() == name.casefold() and row.id != except_id for row in live):
        raise ConflictError(f"Label category {name!r} already exists on this evaluation.")


async def _flush_or_conflict(session: AsyncSession, name: str) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        # The pre-read loses a concurrent race; the partial-unique index on lower(name) decides.
        raise ConflictError(f"Label category {name!r} already exists on this evaluation.") from exc


async def create_label_category(
    session: AsyncSession, *, evaluation_id: UUID, name: str, values: list[str]
) -> EvaluationLabelCategory:
    """Define a category.

    Raises:
        ConflictError: If the name is taken (ignoring case) or the evaluation is at the
            `MAX_LABEL_CATEGORIES` cap. The cap has no index behind it, so racing creates can
            overshoot it by the number of racers.
    """
    live = await list_label_categories(session, evaluation_id)
    _assert_name_free(live, name)
    if len(live) >= MAX_LABEL_CATEGORIES:
        raise ConflictError(f"Evaluation already has the maximum of {MAX_LABEL_CATEGORIES} label categories.")
    row = EvaluationLabelCategory(evaluation_id=evaluation_id, name=name, values=values)
    session.add(row)
    await _flush_or_conflict(session, name)
    await session.refresh(row)
    return row


async def update_label_category(
    session: AsyncSession, row: EvaluationLabelCategory, changes: dict[str, Any]
) -> EvaluationLabelCategory:
    """Apply `changes` (`name` and/or `values`) to `row` (409 on a name taken by another category)."""
    if "name" in changes:
        _assert_name_free(await list_label_categories(session, row.evaluation_id), changes["name"], except_id=row.id)
        row.name = changes["name"]
    if "values" in changes:
        row.values = list(changes["values"])
    session.add(row)
    await _flush_or_conflict(session, row.name)
    await session.refresh(row)
    return row


async def delete_label_category(session: AsyncSession, row: EvaluationLabelCategory, *, by_id: UUID) -> None:
    """Soft-delete `row`; labels set under it stop being read but are not removed."""
    row.soft_delete(by_id)
    session.add(row)
    await session.flush()
