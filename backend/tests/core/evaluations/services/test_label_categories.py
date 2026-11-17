"""Tests for `app.core.evaluations.services.label_categories` — what the API layer cannot reach.

A real race needs two transactions, which the single-connection test session cannot open; these
pin the two guards a race depends on instead: the evaluation row lock taken before the cap count,
and the IntegrityError mapping that only the name index may turn into a 409.
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationLabelCategory
from app.core.evaluations.services import label_categories
from app.core.evaluations.services.label_categories import create_label_category
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _evaluation(db_session: AsyncSession) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    return evaluation


@pytest.fixture
def statements(db_session: AsyncSession) -> Iterator[list[str]]:
    seen: list[str] = []
    engine = db_session.bind.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


async def test_create_locks_the_evaluation_before_counting(db_session: AsyncSession, statements: list[str]) -> None:
    evaluation = await _evaluation(db_session)
    statements.clear()

    await create_label_category(db_session, evaluation_id=evaluation.id, name="Language", values=[])

    lock = next(i for i, sql in enumerate(statements) if "FROM evaluations" in sql and "FOR NO KEY UPDATE" in sql)
    count = next(i for i, sql in enumerate(statements) if "FROM evaluation_label_categories" in sql)
    assert lock < count


async def test_create_refuses_an_evaluation_deleted_after_the_check(db_session: AsyncSession) -> None:
    evaluation = await _evaluation(db_session)
    evaluation.soft_delete(evaluation.created_by_id)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await create_label_category(db_session, evaluation_id=evaluation.id, name="Language", values=[])


async def test_a_name_index_violation_is_a_conflict(db_session: AsyncSession) -> None:
    evaluation = await _evaluation(db_session)
    db_session.add(EvaluationLabelCategory(evaluation_id=evaluation.id, name="Language"))
    await db_session.flush()

    async with db_session.begin_nested():
        # Bypasses the pre-read, as a racing create that passed it would.
        db_session.add(EvaluationLabelCategory(evaluation_id=evaluation.id, name="language"))
        with pytest.raises(ConflictError, match="already exists"):
            await label_categories._flush_or_conflict(db_session, "language")


async def test_another_constraint_is_not_reported_as_a_name_clash(db_session: AsyncSession) -> None:
    async with db_session.begin_nested():
        db_session.add(EvaluationLabelCategory(evaluation_id=uuid4(), name="Language"))
        with pytest.raises(IntegrityError):
            await label_categories._flush_or_conflict(db_session, "Language")
