"""Self-service evaluation-group membership — the `join` action.

Owner/admin-initiated member CRUD lives in the generic object-role layer
(`app/core/auth/object_roles/service.py`); this module holds only the
group-specific policy for a caller adding *themselves*: joinable iff the group is
`published` and self-joinable — `public` (open platform-wide) or `organization`
(open to the owning org's members), but never `invitation_only`.
Visibility/existence is enforced upstream by the route gate, so this never
re-checks the 404 surface — and since an `organization` group is visible only to
its own org, reaching here already implies the caller belongs to it.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import ObjectMember
from app.core.auth.object_roles.service import add_member
from app.core.auth.services.roles import get_participant_default_role
from app.core.auth.services.users import get_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError


async def join_evaluation_group(session: AsyncSession, group: EvaluationGroup, *, caller_id: UUID) -> ObjectMember:
    """Add the caller to a self-joinable, published group as the participant default role.

    The granted role is resolved from the `is_participant_default` flag (seeded onto
    `red_teamer`, operator-reassignable). Check constraints keep that role active and
    object-assignable — this path grants it directly rather than through
    `resolve_assignable_roles` — but nothing constrains its permission set: an active
    participant default with no permissions resolves fine and grants nothing.

    Self-joinable means `public` or `organization` — the latter is open to the
    owning org's members, and the route gate already restricts an `organization`
    group's visibility to that org, so reaching here implies the caller belongs to
    it (nothing extra to re-check). `invitation_only` is never self-joinable.

    Raises:
        ForbiddenError: The group is `invitation_only` — membership comes only by
            owner/admin invitation, never self-service.
        ConflictError: The group is not `published`, or the caller already holds a
            role on it (owners included — they hold `owner`).
    """
    if group.access_level == EvaluationGroupAccessLevel.INVITATION_ONLY:
        raise ForbiddenError("Invitation-only groups cannot be joined directly.")
    if group.status != PublicationStatus.PUBLISHED:
        raise ConflictError("Only published groups can be joined.")
    user = await get_user(session, caller_id)
    role = await get_participant_default_role(session)
    return await add_member(session, ObjectType.EVALUATION_GROUP, group.id, user, [role])
