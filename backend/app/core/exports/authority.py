"""The shared owner/admin authority gate for exports.

Creating a job, downloading its file, and running it in the worker all gate on the *same* rule:
resolve the scope's target group (an evaluation's parent, or the group itself) under the caller's
visibility, then assert in-group `owner` authority (or the `evaluation_groups:manage` break-glass)
over it. Defining it once here keeps the three call sites from silently diverging on a
security-relevant check.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.evaluations.services.evaluation_groups import assert_group_write_access
from app.core.evaluations.services.evaluation_groups import get_evaluation_group
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.exceptions import NotFoundError

# The object-scope permission the export gate keys on: the in-group `owner` role grants
# `evaluation_groups:export` while lesser in-group roles do not, and the `evaluation_groups:manage`
# break-glass lifts it for admins. A dedicated permission rather than a general edit right, since
# the bundle carries every member's transcripts, flags and reviews.
_EXPORT_AUTHORITY = Permission.EVALUATION_GROUPS_EXPORT


async def assert_export_authority(
    session: AsyncSession,
    *,
    caller_id: UUID,
    evaluation_id: UUID | None,
    evaluation_group_id: UUID | None,
    can_manage: bool,
    missing_message: str,
) -> UUID:
    """Resolve the export's target group under the caller's visibility and assert owner/admin authority.

    Returns the resolved group id. Enforces visibility first (loading the evaluation or group
    raises `NotFoundError` if the caller can't see it), then object-scope owner authority
    (`assert_group_write_access` raises `ForbiddenError` if visible-but-not-owner) — the same
    404/403 split as the read endpoints.

    Args:
        session: Async DB session.
        caller_id: The caller whose visibility + object authority is checked.
        evaluation_id: Scope target when exporting one evaluation (mutually exclusive with group).
        evaluation_group_id: Scope target when exporting a whole group.
        can_manage: Whether the caller holds the `evaluation_groups:manage` break-glass.
        missing_message: `NotFoundError` message when the group can't be seen at all.

    Raises:
        NotFoundError: No scope target, or the target is not visible to the caller.
        ForbiddenError: The caller can see the group but is not its owner/admin.
    """
    if evaluation_id is not None:
        evaluation = await get_evaluation(session, evaluation_id, caller_id=caller_id, can_manage=can_manage)
        group_id = evaluation.evaluation_group_id
    elif evaluation_group_id is not None:
        await get_evaluation_group(session, evaluation_group_id, caller_id=caller_id, can_manage=can_manage)
        group_id = evaluation_group_id
    else:  # the ExportJobCreate validator guarantees exactly one scope target
        raise NotFoundError("Export scope target missing.")
    await assert_group_write_access(
        session,
        group_id=group_id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=_EXPORT_AUTHORITY,
        missing_message=missing_message,
    )
    return group_id
