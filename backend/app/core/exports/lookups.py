"""Batched id→label lookups for export templates.

Export rows carry raw UUIDs (owner, scenario, task, reviewer); these resolve a whole
column of them to human-readable labels in one query each, so a template stays a single
fetch + a couple of `IN` lookups rather than an N+1. Names resolve even for soft-deleted
rows (historical export context); `None` ids are dropped and missing ids simply absent
from the returned map (the column then renders an empty cell).
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import User
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.exports.base import ExportScope


async def resolve_evaluation_titles(session: AsyncSession, evaluation_ids: Iterable[UUID | None]) -> dict[UUID, str]:
    """Map evaluation ids to titles (one `IN` query); drops `None`s, ignores unknown ids."""
    ids = {evaluation_id for evaluation_id in evaluation_ids if evaluation_id is not None}
    if not ids:
        return {}
    result = await session.execute(select(col(Evaluation.id), col(Evaluation.title)).where(col(Evaluation.id).in_(ids)))
    return {evaluation_id: title for evaluation_id, title in result.all()}  # noqa: C416


async def resolve_scope_evaluation_title(session: AsyncSession, scope: ExportScope) -> str | None:
    """The scope's single evaluation title — resolved once, not per page.

    Every row a per-evaluation fetcher yields shares `scope.evaluation_id`, so the title is
    constant across the whole export. Read it from the already-loaded `scope.evaluation`
    (the worker's `stream_export` populates it per evaluation) with a one-query fallback,
    instead of re-issuing a single-value `IN` lookup on every page.
    """
    if scope.evaluation is not None:
        return scope.evaluation.title
    return (await resolve_evaluation_titles(session, [scope.evaluation_id])).get(scope.evaluation_id)


async def resolve_user_emails(session: AsyncSession, user_ids: Iterable[UUID | None]) -> dict[UUID, str]:
    """Map user ids to emails (one `IN` query); drops `None`s, ignores unknown ids."""
    ids = {user_id for user_id in user_ids if user_id is not None}
    if not ids:
        return {}
    result = await session.execute(select(col(User.id), col(User.email)).where(col(User.id).in_(ids)))
    return {user_id: email for user_id, email in result.all()}  # noqa: C416


async def resolve_scenario_names(session: AsyncSession, scenario_ids: Iterable[UUID | None]) -> dict[UUID, str]:
    """Map scenario ids to names (one `IN` query); drops `None`s, ignores unknown ids."""
    ids = {scenario_id for scenario_id in scenario_ids if scenario_id is not None}
    if not ids:
        return {}
    result = await session.execute(select(col(Scenario.id), col(Scenario.name)).where(col(Scenario.id).in_(ids)))
    return {scenario_id: name for scenario_id, name in result.all()}  # noqa: C416


async def resolve_task_names(session: AsyncSession, task_ids: Iterable[UUID | None]) -> dict[UUID, str]:
    """Map task ids to names (one `IN` query); drops `None`s, ignores unknown ids."""
    ids = {task_id for task_id in task_ids if task_id is not None}
    if not ids:
        return {}
    result = await session.execute(select(col(Task.id), col(Task.name)).where(col(Task.id).in_(ids)))
    return {task_id: name for task_id, name in result.all()}  # noqa: C416
