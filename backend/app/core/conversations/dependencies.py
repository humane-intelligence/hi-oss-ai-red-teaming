"""Conversation-specific FastAPI dependency aliases.

Cross-cutting aliases (DB session, pagination, current user) live in
`app/core/dependencies.py`; resource-specific ones live here, per the API skill.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlmodel import col

from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.filters import ConversationFilters
from app.core.conversations.filters import ConversationGroupFilters
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.evaluations.access import caller_can_manage_groups
from app.core.evaluations.dependencies import GroupLookup
from app.core.evaluations.dependencies import require_object_or_global
from app.core.evaluations.models import Evaluation
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.scenarios import get_scenario_by_id

ConversationFiltersDep = Annotated[ConversationFilters, Depends()]
ConversationGroupFiltersDep = Annotated[ConversationGroupFilters, Depends()]


async def group_of_path_evaluation(evaluation_id: UUID, caller: CurrentUserDep, db: DbSession) -> GroupLookup:
    """Resolve the parent group of the `evaluation_id` in the path, under the caller's visibility.

    Resolving the evaluation under visibility preserves the 404-before-403 ordering: one the
    caller cannot see reads as missing rather than announcing itself with a 403.
    """

    async def _lookup() -> UUID | None:
        evaluation = await get_evaluation(
            db,
            evaluation_id,
            caller_id=caller.id,
            can_manage=caller_can_manage_groups(caller),
            with_models=False,
        )
        return evaluation.evaluation_group_id

    return _lookup


async def group_of_path_scenario(scenario_id: UUID, caller: CurrentUserDep, db: DbSession) -> GroupLookup:
    """Resolve the parent group of the `scenario_id` in the path, under the caller's visibility.

    The conversation/group create routes address their scenario directly. Scoped like
    `group_of_path_evaluation` rather than the two id-only lookups below: the scenario
    resolves only when visible, so one the caller cannot see reads as missing instead of
    announcing itself with a 403.
    """

    async def _lookup() -> UUID | None:
        scenario = await get_scenario_by_id(
            db, scenario_id, caller_id=caller.id, can_manage=caller_can_manage_groups(caller)
        )
        statement = select(col(Evaluation.evaluation_group_id)).where(col(Evaluation.id) == scenario.evaluation_id)
        return (await db.execute(statement)).scalar_one_or_none()

    return _lookup


async def group_of_path_conversation(conversation_id: UUID, db: DbSession) -> GroupLookup:
    """Resolve the parent group of the `conversation_id` in the path."""

    async def _lookup() -> UUID | None:
        statement = (
            select(col(Evaluation.evaluation_group_id))
            .join(Conversation, col(Conversation.evaluation_id) == col(Evaluation.id))
            .where(col(Conversation.id) == conversation_id)
        )
        return (await db.execute(statement)).scalar_one_or_none()

    return _lookup


async def group_of_path_conversation_group(conversation_group_id: UUID, db: DbSession) -> GroupLookup:
    """Resolve the parent group of the `conversation_group_id` in the path."""

    async def _lookup() -> UUID | None:
        statement = (
            select(col(Evaluation.evaluation_group_id))
            .join(ConversationGroup, col(ConversationGroup.evaluation_id) == col(Evaluation.id))
            .where(col(ConversationGroup.id) == conversation_group_id)
        )
        return (await db.execute(statement)).scalar_one_or_none()

    return _lookup


def require_conversation_permission(permission: Permission) -> Callable[..., Awaitable[SessionUser]]:
    """Gate a route carrying `evaluation_id` on ``permission`` held globally or in the parent group."""
    return require_object_or_global(permission, group_of_path_evaluation)
