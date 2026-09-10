"""Note-specific FastAPI dependency aliases.

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

from app.core.annotations.filters import AnnotationFilters
from app.core.annotations.filters import MessageFlagFilters
from app.core.annotations.filters import NoteFilters
from app.core.annotations.models import MessageFlag
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import holds_permission_anywhere
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.models import Conversation
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.evaluations.dependencies import GroupLookup
from app.core.evaluations.dependencies import require_object_or_global
from app.core.evaluations.models import Evaluation
from app.core.exceptions import ForbiddenError

MessageFlagFiltersDep = Annotated[MessageFlagFilters, Depends()]

NoteFiltersDep = Annotated[NoteFilters, Depends()]

AnnotationFiltersDep = Annotated[AnnotationFilters, Depends()]


async def group_of_flag_filters(db: DbSession, filters: MessageFlagFiltersDep) -> GroupLookup:
    """Resolve the group the flag listing's filters name — the direct one, else via a narrower id.

    The listing is flat, but its filters name the object, so authority is resolved against
    *that* group. An unfiltered listing names nothing and so still needs the global permission.
    Which rows come back is unchanged — the service applies its own scope (the caller's own
    flags under a visible group, both lifted for the `evaluation_groups:manage` break-glass).

    Rows cannot fall outside the group this authorizes against, because the filter that decides
    authority also constrains the rows, and `list_flags` applies every filter given. Mutually
    contradictory filters (`evaluation_group_id=G1` with a `conversation_id` in G2) are not
    cross-validated here — they simply intersect to an empty page.

    First match wins rather than falling through: an `evaluation_id` that resolves to nothing
    refuses instead of trying `conversation_id`, so a stale id cannot borrow another filter's
    authority.
    """

    async def _lookup() -> UUID | None:
        if filters.evaluation_group_id is not None:
            return filters.evaluation_group_id
        if filters.evaluation_id is not None:
            statement = select(col(Evaluation.evaluation_group_id)).where(col(Evaluation.id) == filters.evaluation_id)
            return (await db.execute(statement)).scalar_one_or_none()
        if filters.conversation_id is not None:
            statement = (
                select(col(Evaluation.evaluation_group_id))
                .join(Conversation, col(Conversation.evaluation_id) == col(Evaluation.id))
                .where(col(Conversation.id) == filters.conversation_id)
            )
            return (await db.execute(statement)).scalar_one_or_none()
        return None

    return _lookup


async def group_of_path_flag(flag_id: UUID, db: DbSession) -> GroupLookup:
    """Resolve the group of the `flag_id` in the path, from the flag's denormalised column.

    Unscoped by design, and unfiltered by liveness: the gate only needs to know *which* group
    to ask the object-role layer about. The service re-resolves the row under the caller's
    scope with liveness enforced, so a soft-deleted flag still reads as 404 there rather than
    turning into a 403 here.
    """

    async def _lookup() -> UUID | None:
        statement = select(col(MessageFlag.evaluation_group_id)).where(col(MessageFlag.id) == flag_id)
        return (await db.execute(statement)).scalar_one_or_none()

    return _lookup


def require_flag_list_permission(permission: Permission) -> Callable[..., Awaitable[SessionUser]]:
    """Gate the flag listing on ``permission`` held globally **or** in the filtered group."""
    return require_object_or_global(permission, group_of_flag_filters)


def require_flag_permission(permission: Permission) -> Callable[..., Awaitable[SessionUser]]:
    """Gate a single-flag route on ``permission`` held globally or in that flag's group."""
    return require_object_or_global(permission, group_of_path_flag)


def require_flag_create_permission(permission: Permission) -> Callable[..., Awaitable[SessionUser]]:
    """Gate flag creation on ``permission`` held globally or through any in-group role.

    Coarse because create names its target in the **body**, and declaring a body in the gate
    would make FastAPI validate it before authorizing — 422 instead of 403, parsing untrusted
    input ahead of the permission check. It grants nothing extra: the service admits a flag
    only on a conversation the caller owns under a group still visible to them.
    """

    async def _check(caller: CurrentUserDep, db: DbSession) -> SessionUser:
        if permission in caller.permissions or await holds_permission_anywhere(
            db, caller.id, ObjectType.EVALUATION_GROUP, permission
        ):
            return caller
        raise ForbiddenError(f"Caller lacks the '{permission}' permission.")

    return _check
