"""Service tests for the annotator candidate pool and its assignment gate.

The picker (`list_group_annotators`) and the gate (`is_assignable_annotator`)
share their per-branch predicates and both run under `User.live_select()`, so
they must agree on who is assignable — including the soft-deleted-user exclusion.
The only caller of the gate (the reviews assign route) loads a *live* reviewer
before calling it, so this exclusion has no live caller path today; these tests
pin the gate's behaviour directly, where the route would otherwise mask it.
"""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.services.annotators import is_assignable_annotator
from app.core.evaluations.services.annotators import list_group_annotators
from app.core.organizations.models import Organization
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _annotator(db_session: AsyncSession, annotator_role: Role) -> User:
    return await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[annotator_role])


async def _reviewer_role(
    db_session: AsyncSession, *, name: str, is_active: bool = True, in_group: bool = False
) -> Role:
    # `in_group` shapes the role the way `resolve_assignable_roles` requires for an
    # object-role grant, so a test granting it in-group pins a state the API can reach.
    permissions = [Permission.REVIEWS_ANNOTATE.value]
    if in_group:
        permissions.append(Permission.EVALUATION_GROUPS_READ.value)
    role = Role(
        name=name,
        permissions=permissions,
        is_system=False,
        is_active=is_active,
        is_object_assignable=in_group,
    )
    db_session.add(role)
    await db_session.flush()
    return role


async def _soft_delete(db_session: AsyncSession, user: User) -> None:
    user.soft_delete(None)
    db_session.add(user)
    await db_session.flush()


async def _picker_ids(db_session: AsyncSession, group: EvaluationGroup) -> set:
    items, _ = await list_group_annotators(db_session, group, limit=100, offset=0)
    return {user.id for user in items}


async def test_gate_excludes_soft_deleted_public_annotator(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    live = await _annotator(db_session, system_roles["annotator"])
    gone = await _annotator(db_session, system_roles["annotator"])  # holds the global role, then soft-deleted
    await _soft_delete(db_session, gone)

    # Soft-deleting the user leaves their `UserRole` row intact, so the gate must
    # filter on user-liveness — not just role-holding — to match the picker.
    assert await is_assignable_annotator(db_session, group, live.id) is True
    assert await is_assignable_annotator(db_session, group, gone.id) is False
    assert await _picker_ids(db_session, group) == {live.id}


async def test_gate_excludes_soft_deleted_invitation_only_member(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    live = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[system_roles["red_teamer"]])
    gone = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[system_roles["red_teamer"]])
    for member in (live, gone):
        await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles["annotator"]])
    # A soft-deleted member keeps a live in-group assignment; the gate must still
    # drop them (the assignment outlives the user row).
    await _soft_delete(db_session, gone)

    assert await is_assignable_annotator(db_session, group, live.id) is True
    assert await is_assignable_annotator(db_session, group, gone.id) is False
    assert await _picker_ids(db_session, group) == {live.id}


async def test_picker_search_narrows_public_pool(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    alice = await create_user(db_session, email="annalice@example.com", roles=[system_roles["annotator"]])
    await create_user(db_session, email="annbob@example.com", roles=[system_roles["annotator"]])

    items, total = await list_group_annotators(db_session, group, search="ALICE", limit=100, offset=0)

    assert total == 1
    assert {user.id for user in items} == {alice.id}


async def test_picker_search_narrows_invitation_only_members(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    alice = await create_user(db_session, email="invalice@example.com", roles=[system_roles["red_teamer"]])
    bob = await create_user(db_session, email="invbob@example.com", roles=[system_roles["red_teamer"]])
    for member in (alice, bob):
        await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles["annotator"]])

    items, total = await list_group_annotators(db_session, group, search="invalice", limit=100, offset=0)

    assert total == 1
    assert {user.id for user in items} == {alice.id}


async def test_picker_search_narrows_organization_pool(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    org = Organization(name="Acme Corp")
    db_session.add(org)
    await db_session.flush()
    await db_session.refresh(org)
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.ORGANIZATION)
    group.organization_id = org.id
    db_session.add(group)
    await db_session.flush()
    alice = await create_user(db_session, email="orgalice@example.com", roles=[system_roles["annotator"]])
    bob = await create_user(db_session, email="orgbob@example.com", roles=[system_roles["annotator"]])
    for member in (alice, bob):
        member.organization_id = org.id
        db_session.add(member)
    await db_session.flush()

    items, total = await list_group_annotators(db_session, group, search="orgalice", limit=100, offset=0)

    assert total == 1
    assert {user.id for user in items} == {alice.id}


async def test_picker_search_treats_metacharacters_literally(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A lone '%' must not act as a wildcard (would otherwise return every annotator).
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    await create_user(db_session, email="meta@example.com", roles=[system_roles["annotator"]])

    items, total = await list_group_annotators(db_session, group, search="%", limit=100, offset=0)

    assert total == 0
    assert items == []


async def test_picker_search_metacharacters_literal_organization_pool(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Search is escaped inline on every branch, so a lone '%' must stay literal here too —
    # otherwise it matches the whole org pool.
    org = Organization(name="Meta Corp")
    db_session.add(org)
    await db_session.flush()
    await db_session.refresh(org)
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.ORGANIZATION)
    group.organization_id = org.id
    db_session.add(group)
    await db_session.flush()
    user = await create_user(db_session, email="orgmeta@example.com", roles=[system_roles["annotator"]])
    user.organization_id = org.id
    db_session.add(user)
    await db_session.flush()

    items, total = await list_group_annotators(db_session, group, search="%", limit=100, offset=0)

    assert total == 0
    assert items == []


async def test_custom_role_granting_reviews_annotate_is_in_pool(db_session: AsyncSession) -> None:
    # The pool is capability-based: a custom (non-annotator) role granting
    # reviews:annotate makes its holder assignable.
    role = await _reviewer_role(db_session, name="custom-reviewer")
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    user = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])

    assert await is_assignable_annotator(db_session, group, user.id) is True
    assert user.id in await _picker_ids(db_session, group)


async def test_reviews_create_without_annotate_is_excluded(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # reviews:create alone (owner holds it) does not make a user assignable — only
    # reviews:annotate does, so decoupling from the annotator role didn't broaden the pool.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    owner = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[system_roles["owner"]])

    assert await is_assignable_annotator(db_session, group, owner.id) is False
    assert owner.id not in await _picker_ids(db_session, group)


async def test_inactive_role_granting_reviews_annotate_is_excluded(db_session: AsyncSession) -> None:
    # An inactive role grants nothing, so its holder drops out of the pool.
    role = await _reviewer_role(db_session, name="inactive-reviewer", is_active=False)
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    user = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])

    assert await is_assignable_annotator(db_session, group, user.id) is False
    assert user.id not in await _picker_ids(db_session, group)


async def test_soft_deleted_role_granting_reviews_annotate_is_excluded(db_session: AsyncSession) -> None:
    # deleted_at IS NULL is now the only liveness filter on the reviewer-role query;
    # a soft-deleted role leaves its user_roles rows behind, so this must drop its holders.
    role = await _reviewer_role(db_session, name="deleted-reviewer")
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    user = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])
    role.soft_delete(None)
    db_session.add(role)
    await db_session.flush()

    assert await is_assignable_annotator(db_session, group, user.id) is False
    assert user.id not in await _picker_ids(db_session, group)


async def test_custom_in_group_role_granting_reviews_annotate_is_in_pool(db_session: AsyncSession) -> None:
    # The in-group (object-role) branch is capability-based too: a custom role
    # granting reviews:annotate, assigned in-group, makes an invitation-only member assignable.
    base = Role(name="member-base", permissions=[], is_system=False)
    db_session.add(base)
    reviewer = await _reviewer_role(db_session, name="custom-ig-reviewer", in_group=True)
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    user = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[base])
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, user.id, [reviewer])

    assert await is_assignable_annotator(db_session, group, user.id) is True
    assert user.id in await _picker_ids(db_session, group)


async def test_in_group_reviewer_is_in_public_pool(db_session: AsyncSession) -> None:
    # public unions in-group holders (like organization): an in-group reviewer
    # lacking the global capability is still assignable.
    base = Role(name="public-member-base", permissions=[], is_system=False)
    db_session.add(base)
    reviewer = await _reviewer_role(db_session, name="public-ig-reviewer", in_group=True)
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    user = await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[base])
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, user.id, [reviewer])

    assert await is_assignable_annotator(db_session, group, user.id) is True
    assert user.id in await _picker_ids(db_session, group)
