"""Organization CRUD + membership service — pure async functions over an `AsyncSession`.

Routers stay thin: they raise `APIError` subclasses for known failure modes and
let the error handler in `app.core.error_handlers` translate them into RFC 7807
responses. Soft-deleted rows are filtered per-statement via the `Organization.live_*`
factories on `BaseModel`.

Membership is a single nullable FK on `User` (one user, one org), so assigning a
member is just stamping `User.organization_id`; the object-role machinery is not
involved.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import User
from app.core.auth.services.users import user_projection_options
from app.core.evaluations.models import EvaluationGroup
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.organizations.filters import OrganizationFilters
from app.core.organizations.filters import OrganizationOrderBy
from app.core.organizations.models import Organization
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row


class OrganizationUpdateChanges(BaseModel):
    """Service-owned update contract — `extra="forbid"` so drift from `OrganizationUpdate` fails loudly.

    Build from `payload.model_dump(exclude_unset=True)` so `model_fields_set`
    separates "omitted" from "explicit None".
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None


async def create_organization(session: AsyncSession, *, name: str, description: str | None = None) -> Organization:
    """Create and persist a new `Organization`.

    Raises:
        ConflictError: If ``name`` is already taken by a live organization.
    """
    organization = Organization(name=name, description=description)
    session.add(organization)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"An organization named {name!r} already exists.") from exc
    await session.refresh(organization, attribute_names=["created_at", "updated_at"])
    return organization


async def get_organization(session: AsyncSession, organization_id: UUID, *, for_update: bool = False) -> Organization:
    """Fetch one live (non-soft-deleted) `Organization` by id.

    Raises:
        NotFoundError: If no live row matches ``organization_id``.
    """
    statement = Organization.live_select().where(col(Organization.id) == organization_id)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    organization = result.scalar_one_or_none()
    if organization is None:
        raise NotFoundError(f"Organization {organization_id} not found.")
    return organization


def _apply_organization_filters(
    statement: Select[tuple[Organization]], filters: OrganizationFilters
) -> Select[tuple[Organization]]:
    """Append a `WHERE` per supplied filter; leave omitted ones alone."""
    if filters.name is not None:
        statement = statement.where(col(Organization.name).ilike(f"%{filters.name}%", escape="\\"))
    return statement


async def list_organizations(
    session: AsyncSession,
    *,
    filters: OrganizationFilters,
    order_by: OrganizationOrderBy = "name",
    limit: int,
    offset: int,
    deleted: bool = False,
    deleted_cutoff: datetime,
) -> tuple[list[Organization], int]:
    """Return one page of live `Organization` rows matching ``filters``.

    ``deleted`` swaps in the tombstones still inside the restore window, keeping
    ``order_by`` as given (pass `-deleted_at` for newest-first). No deleter predicate:
    the route gates the whole view on `organizations:delete`, which is also the only
    tier `restore_organization` accepts.
    ``deleted_cutoff`` comes from the route, like `get_restorable_organization`'s, so the
    listing and the restore cannot disagree on the window.
    """
    statement = deleted_select(Organization, deleted_cutoff, deleted_by=None) if deleted else Organization.live_select()
    statement = _apply_organization_filters(statement, filters)
    statement = apply_order_by(statement, Organization, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def update_organization(
    session: AsyncSession, organization: Organization, changes: OrganizationUpdateChanges
) -> Organization:
    """Apply ``changes`` to ``organization`` and persist them.

    Writes only fields in `changes.model_fields_set` — omitted fields stay,
    explicit `None` clears the nullable `description`.

    Raises:
        ConflictError: If the new ``name`` collides with another live organization.
    """
    for field in changes.model_fields_set:
        setattr(organization, field, getattr(changes, field))
    session.add(organization)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("Organization update violates the name uniqueness constraint.") from exc
    await session.refresh(organization, attribute_names=["updated_at"])
    return organization


async def soft_delete_organization(session: AsyncSession, organization: Organization, *, by_id: UUID) -> Organization:
    """Mark ``organization`` as soft-deleted by stamping `deleted_at`.

    Blocked while any live `EvaluationGroup` still references the org: a soft-delete
    does **not** fire the FK `SET NULL` (that is hard-delete only), so a tombstoned
    org would leave its `organization`-access groups dark (fail-closed, invisible to
    everyone) and — worse — un-editable, since every group write re-validates that a
    set `organization_id` is live (`_validate_group_org`). Forcing the operator to
    reassign/clear those groups first preserves the invariant "a live group's org
    reference is live". Member *users* still keep their `organization_id` pointing at
    the tombstone (benign — they fail-closed to orgless; member detachment is
    deferred).

    Raises:
        ConflictError: A live evaluation group still references the organization.
    """
    referencing = await session.scalar(
        select(func.count())
        .select_from(EvaluationGroup)
        .where(
            col(EvaluationGroup.organization_id) == organization.id,
            col(EvaluationGroup.deleted_at).is_(None),
        )
    )
    if referencing:
        raise ConflictError(
            f"Organization is still referenced by {referencing} evaluation group(s); "
            "reassign or clear them before deleting."
        )
    organization.soft_delete(by_id)
    session.add(organization)
    await session.flush()
    await session.refresh(organization)
    return organization


async def get_restorable_organization(
    session: AsyncSession, organization_id: UUID, *, deleted_cutoff: datetime
) -> Organization:
    """Fetch the tombstoned organization ``organization_id`` inside the restore window.

    No deleter predicate: `organizations:delete` is the only tier that reaches either the
    deleted listing or the restore, so scoping to the caller's own deletes would hide rows
    from someone allowed to restore them.

    Raises:
        NotFoundError: Never deleted, or deleted longer than the window ago.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(Organization, deleted_cutoff, deleted_by=None)
        .where(col(Organization.id) == organization_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    organization = (await session.execute(statement)).scalar_one_or_none()
    if organization is None:
        raise NotFoundError(f"No restorable organization {organization_id} was deleted within the restore window.")
    return organization


async def restore_organization(session: AsyncSession, organization: Organization) -> Organization:
    """Clear ``organization``'s tombstone.

    Members come back with it and need no repair: the delete deliberately leaves
    `User.organization_id` pointing at the tombstoned row (they fail closed to orgless
    while it is dead), so reviving the org re-resolves every one of those pointers. The
    delete's own guard means there is nothing else to undo — it refuses while any live
    evaluation group references the org, so no group was left dangling.

    Raises:
        ConflictError: If a live organization already holds the name.
    """
    await restore_row(
        session, organization, conflict_message=f"An organization named {organization.name!r} already exists."
    )
    await session.refresh(organization, attribute_names=["updated_at"])
    return organization


async def list_organization_members(
    session: AsyncSession, organization_id: UUID, *, limit: int, offset: int
) -> tuple[list[User], int]:
    """Return one page of live users belonging to ``organization_id``, roles + org eager-loaded.

    The org is eager-loaded so each user projects without a lazy load; the
    soft-delete rule is applied in the projection (`OrganizationBase.from_optional_live`).
    """
    statement = (
        User.live_select()
        .options(*user_projection_options())
        .where(col(User.organization_id) == organization_id)
        .order_by(col(User.created_at), col(User.id))
    )
    return await paginate(session, statement, limit=limit, offset=offset)


async def assign_member(session: AsyncSession, organization: Organization, user: User) -> User:
    """Move ``user`` into ``organization`` (overwrites any prior org — one user, one org).

    Sets the relationship (not just the FK) so the returned `user.organization` reflects
    the new, live org for the response projection. Idempotent.
    """
    user.organization = organization
    session.add(user)
    await session.flush()
    await session.refresh(user, attribute_names=["updated_at"])
    return user


async def remove_member(session: AsyncSession, organization: Organization, user: User) -> None:
    """Detach ``user`` from ``organization`` (clears the org link).

    Raises:
        NotFoundError: If ``user`` does not currently belong to ``organization`` —
            so a wrong (org, user) pair reads as missing rather than silently no-op.
    """
    if user.organization_id != organization.id:
        raise NotFoundError(f"User {user.id} is not a member of organization {organization.id}.")
    user.organization = None
    session.add(user)
    await session.flush()
