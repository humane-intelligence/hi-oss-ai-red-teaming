"""Per-role transcript visibility — the permission gate, the owner scope, and group visibility.

Message text reaches clients through more surfaces than this module covers: the owner-scoped
conversation history, the review-scoped `/submissions/{id}` detail and `/submissions/{id}/messages`
transcript (all three pinned here), plus `GET /review-queue` and the `/message-flags` surface, which
project `MessageBase.content` too and are **not** covered here. The conversation-*group* reads are
pinned here as well — they carry no message content, but they share the `read_any` widening, and
`services/groups._scope` keeps its own copy of it.

Each case varies one axis — role, ownership, or group access level — so removing any predicate they rest on
(the route gate, the `user_id` owner check, its `conversations:read_any` widening,
`group_visible_to`) fails at least one test.
"""

from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.annotations.models import FlaggedMessage
from app.core.annotations.models import MessageFlag
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.users import create_user as create_user_service
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from app.core.reviews.models import Review
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with
from tests.api.v1.conftest import make_role
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _evaluation_for(
    db_session: AsyncSession,
    *,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
) -> Evaluation:
    group = await persist_evaluation_group(db_session, access_level=access_level)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _assignment(db_session: AsyncSession, evaluation_id: UUID) -> EvaluationAiModel:
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation_id, model_id=model.id)
    db_session.add(assignment)
    await db_session.flush()
    await db_session.refresh(assignment)
    return assignment


async def _conversation_owned_by(db_session: AsyncSession, *, owner_id: UUID, evaluation: Evaluation) -> Conversation:
    assignment = await _assignment(db_session, evaluation.id)
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    group_row = ConversationGroup(user_id=owner_id, evaluation_id=evaluation.id, name="group", scenario_id=scenario.id)
    db_session.add(group_row)
    await db_session.flush()
    row = Conversation(
        user_id=owner_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=group_row.id,
        scenario_id=scenario.id,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _seed_messages(db_session: AsyncSession, conversation_id: UUID, count: int) -> list[Message]:
    messages = []
    for index in range(count):
        turn = Turn(conversation_id=conversation_id, turn_index=index)
        db_session.add(turn)
        await db_session.flush()
        message = Message(
            turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content=f"msg {index}"
        )
        db_session.add(message)
        messages.append(message)
    await db_session.flush()
    return messages


async def _flag_for(
    db_session: AsyncSession, *, conversation: Conversation, evaluation: Evaluation, created_by_id: UUID
) -> MessageFlag:
    row = MessageFlag(
        reason="exploit-worthy",
        created_by_id=created_by_id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=evaluation.evaluation_group_id,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _flag_messages(db_session: AsyncSession, flag: MessageFlag, messages: list[Message]) -> None:
    for message in messages:
        db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=message.id))
    await db_session.flush()


async def _grant_in_group(db_session: AsyncSession, group_id: UUID, user_id: UUID, role: Role) -> None:
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group_id, user_id, [role])
    await db_session.flush()


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def _get_conversation_messages(
    client: AsyncClient, user: User, evaluation_id: UUID, conversation_id: UUID
) -> Response:
    return await client.get(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}/messages", headers=bearer(user)
    )


async def _get_submission_messages(client: AsyncClient, user: User, submission_id: UUID) -> Response:
    return await client.get(f"/api/v1/submissions/{submission_id}/messages", headers=bearer(user))


async def _get_submission_detail(client: AsyncClient, user: User, submission_id: UUID) -> Response:
    return await client.get(f"/api/v1/submissions/{submission_id}", headers=bearer(user))


# --- the owner-scoped conversation history ------------------------------------

# A conversation owned by another user in a visible public group, none of the callers holding an
# in-group role on it. 403 = the role holds no `conversations:read`; 404 = it does, but the owner
# predicate excludes the row (no existence leak); 200 = the `evaluation_groups:manage` break-glass
# lifts owner *and* visibility. `owner` lands on 404, not 200: `conversations:read_any` is
# object-scoped, so carrying it in the JWT grants nothing without an in-group assignment.
_FOREIGN_CONVERSATION_EXPECTATIONS = [
    (SystemRole.ADMIN, status.HTTP_200_OK),
    (SystemRole.OWNER, status.HTTP_404_NOT_FOUND),
    (SystemRole.RED_TEAMER, status.HTTP_404_NOT_FOUND),
    (SystemRole.ANNOTATOR, status.HTTP_403_FORBIDDEN),
    (SystemRole.VIEWER, status.HTTP_403_FORBIDDEN),
]


@pytest.mark.parametrize(
    ("role", "expected_status"),
    _FOREIGN_CONVERSATION_EXPECTATIONS,
    ids=[role.value for role, _ in _FOREIGN_CONVERSATION_EXPECTATIONS],
)
async def test_foreign_conversation_transcript_by_role(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    role: SystemRole,
    expected_status: int,
) -> None:
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-author@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    await _seed_messages(db_session, conversation.id, 2)
    caller = await _caller(db_session, system_roles[role.value], email="mv-caller@example.com")

    response = await _get_conversation_messages(auth_db_client, caller, evaluation.id, conversation.id)

    assert response.status_code == expected_status
    if expected_status == status.HTTP_200_OK:
        assert [message["content"] for message in response.json()["items"]] == ["msg 0", "msg 1"]
    else:
        _assert_problem(response, expected_status)


async def test_private_group_hides_own_conversation_from_non_member(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Owning the conversation is not enough — the parent group must still be visible.

    The owner predicate passes here and only `group_visible_to` refuses, so this is the case
    that separates the two.
    """
    caller = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-private-rt@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    await _seed_messages(db_session, conversation.id, 2)

    response = await _get_conversation_messages(auth_db_client, caller, evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_private_group_member_reads_own_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    red_teamer_role = system_roles[SystemRole.RED_TEAMER.value]
    caller = await _caller(db_session, red_teamer_role, email="mv-private-member@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, caller.id, red_teamer_role)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    await _seed_messages(db_session, conversation.id, 2)

    response = await _get_conversation_messages(auth_db_client, caller, evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    assert [message["content"] for message in response.json()["items"]] == ["msg 0", "msg 1"]


async def test_group_owner_reads_member_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The in-group `owner` role lifts the owner predicate on reads via `conversations:read_any`."""
    owner_role = system_roles[SystemRole.OWNER.value]
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-go-author@example.com")
    group_owner = await _caller(db_session, owner_role, email="mv-go-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, group_owner.id, owner_role)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    await _seed_messages(db_session, conversation.id, 2)

    response = await _get_conversation_messages(auth_db_client, group_owner, evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    assert [message["content"] for message in response.json()["items"]] == ["msg 0", "msg 1"]


async def test_group_edit_authority_alone_reads_no_member_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Editing a group is not reading its transcripts.

    `conversations:read_any` is deliberately not keyed on `evaluation_groups:update`, which is
    delegable — a custom role handed out to let someone rename a group must not carry every
    member's transcript with it.
    """
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-edit-author@example.com")
    editor = await _caller(db_session, system_roles[SystemRole.OWNER.value], email="mv-edit-caller@example.com")
    editor_in_group = await make_role(db_session, [Permission.EVALUATION_GROUPS_UPDATE.value])
    evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, editor.id, editor_in_group)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    await _seed_messages(db_session, conversation.id, 1)

    response = await _get_conversation_messages(auth_db_client, editor, evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_read_any_does_not_lift_the_write_scope(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """`read_any` widens reads only — a PATCH on a member's conversation still 404s."""
    owner_role = system_roles[SystemRole.OWNER.value]
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-wr-author@example.com")
    caller = await caller_with(
        db_session,
        Permission.CONVERSATIONS_READ,
        Permission.CONVERSATIONS_UPDATE,
        email="mv-wr-caller@example.com",
    )
    evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, caller.id, owner_role)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    await _seed_messages(db_session, conversation.id, 1)

    read = await _get_conversation_messages(auth_db_client, caller, evaluation.id, conversation.id)
    write = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation.id}",
        json={"title": "hijacked"},
        headers=bearer(caller),
    )

    assert read.status_code == status.HTTP_200_OK
    assert write.status_code == status.HTTP_404_NOT_FOUND


async def test_in_group_red_teamer_starts_a_conversation_without_the_global_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """An in-group role confers its capability on that group's routes.

    The JWT carries global roles only, so before the object-aware gate a user added to a group
    as `red_teamer` was refused with "Caller lacks the 'conversations:create' permission" —
    the global `owner` role does not grant it. The in-group role does.
    """
    red_teamer_role = system_roles[SystemRole.RED_TEAMER.value]
    caller = await _caller(db_session, system_roles[SystemRole.OWNER.value], email="mv-ig-create@example.com")
    evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, caller.id, red_teamer_role)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversation-groups",
        json={
            "name": "s1",
            "models": [{"evaluation_ai_model_id": str(assignment.id)}],
        },
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_201_CREATED


async def test_in_group_red_teamer_lists_flags_filtered_by_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The flag listing authorizes against the group its filter names, not the JWT alone."""
    red_teamer_role = system_roles[SystemRole.RED_TEAMER.value]
    caller = await _caller(db_session, system_roles[SystemRole.OWNER.value], email="mv-flags-ig@example.com")
    evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, caller.id, red_teamer_role)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    messages = await _seed_messages(db_session, conversation.id, 1)
    flag = await _flag_for(db_session, conversation=conversation, evaluation=evaluation, created_by_id=caller.id)
    await _flag_messages(db_session, flag, messages)

    response = await auth_db_client.get(
        "/api/v1/message-flags", params={"conversation_id": str(conversation.id)}, headers=bearer(caller)
    )

    assert response.status_code == status.HTTP_200_OK
    assert [item["id"] for item in response.json()["items"]] == [str(flag.id)]


async def test_flag_listing_refuses_when_the_filtered_group_grants_nothing(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """No in-group role on the filtered group and none globally — still a refusal."""
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-flags-a@example.com")
    caller = await _caller(db_session, system_roles[SystemRole.OWNER.value], email="mv-flags-denied@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)

    response = await auth_db_client.get(
        "/api/v1/message-flags", params={"conversation_id": str(conversation.id)}, headers=bearer(caller)
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_non_member_without_the_global_permission_cannot_start_a_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The object-aware gate widens the source of the permission, never drops the requirement."""
    caller = await _caller(db_session, system_roles[SystemRole.OWNER.value], email="mv-ig-denied@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversation-groups",
        json={"name": "s1", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_non_member_without_the_global_permission_gets_404_on_an_invisible_scenario(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """404 before 403: the object arm must not confirm a scenario the caller cannot see.

    The sibling above is the visible case, where falling through to the object arm is a
    403. Here the parent group is `invitation_only`, so `group_of_path_scenario` resolves
    the scenario under visibility and fails first — the only test pinning that scoping,
    since a caller holding `conversations:create` globally short-circuits the gate
    (`require_object_or_global`) and never reaches the lookup at all.
    """
    caller = await _caller(db_session, system_roles[SystemRole.OWNER.value], email="mv-ig-hidden@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversation-groups",
        json={"name": "s1", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_break_glass_alone_does_not_confer_conversation_write(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """`evaluation_groups:manage` is delegable, so the object arm must not accept it as a capability."""
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-bg-author@example.com")
    caller = await caller_with(db_session, Permission.EVALUATION_GROUPS_MANAGE, email="mv-bg-caller@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()

    create = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversation-groups",
        json={"name": "s1", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
        headers=bearer(caller),
    )
    update = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation.id}",
        json={"title": "hijacked"},
        headers=bearer(caller),
    )

    assert create.status_code == status.HTTP_403_FORBIDDEN
    assert update.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(update, status.HTTP_403_FORBIDDEN)


async def test_break_glass_alone_does_not_confer_flag_reads(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Same rule on the flag listing: the break-glass key is not a stand-in for `flags:read`."""
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-bgf-author@example.com")
    caller = await caller_with(db_session, Permission.EVALUATION_GROUPS_MANAGE, email="mv-bgf-caller@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)

    response = await auth_db_client.get(
        "/api/v1/message-flags", params={"conversation_id": str(conversation.id)}, headers=bearer(caller)
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_read_any_does_not_reach_across_groups(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The lift is per group: `owner` on group A reads nothing in group B.

    The single failure mode `conversations:read_any` introduces — `groups_granting` returning the
    wrong group's ids would widen every group at once, and the owner predicate would no longer
    hide the row.
    """
    owner_role = system_roles[SystemRole.OWNER.value]
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-xg-author@example.com")
    group_owner = await _caller(db_session, owner_role, email="mv-xg-owner@example.com")
    owned_evaluation = await _evaluation_for(db_session)
    other_evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, owned_evaluation.evaluation_group_id, group_owner.id, owner_role)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=other_evaluation)
    await _seed_messages(db_session, conversation.id, 1)

    response = await _get_conversation_messages(auth_db_client, group_owner, other_evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_group_owner_reads_member_conversation_groups(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """The `read_any` lift on the *group* scope, which the conversation cases do not reach.

    `services/groups._scope` carries its own copy of the widening, so the conversation-side tests
    leave it unpinned: the nested list, the flat list and `get_conversation_group` all read through
    this one. The group page and the "another member's" marker read from it too.
    """
    owner_role = system_roles[SystemRole.OWNER.value]
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-grp-author@example.com")
    group_owner = await _caller(db_session, owner_role, email="mv-grp-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, group_owner.id, owner_role)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)

    nested = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups", headers=bearer(group_owner)
    )
    detail = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation.conversation_group_id}",
        headers=bearer(group_owner),
    )
    flat = await auth_db_client.get("/api/v1/conversation-groups", headers=bearer(group_owner))

    assert nested.status_code == status.HTTP_200_OK
    assert [item["id"] for item in nested.json()["items"]] == [str(conversation.conversation_group_id)]
    assert detail.status_code == status.HTTP_200_OK
    assert flat.status_code == status.HTTP_200_OK
    assert [item["id"] for item in flat.json()["items"]] == [str(conversation.conversation_group_id)]


async def test_group_read_any_does_not_reach_across_groups(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Per group on the group scope too — `owner` on group A sees no group in B."""
    owner_role = system_roles[SystemRole.OWNER.value]
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-grpx-author@example.com")
    group_owner = await _caller(db_session, owner_role, email="mv-grpx-owner@example.com")
    owned_evaluation = await _evaluation_for(db_session)
    other_evaluation = await _evaluation_for(db_session)
    await _grant_in_group(db_session, owned_evaluation.evaluation_group_id, group_owner.id, owner_role)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=other_evaluation)

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{other_evaluation.id}/conversation-groups/{conversation.conversation_group_id}",
        headers=bearer(group_owner),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


# --- the review-scoped submission surfaces ------------------------------------

# A submission whose flag another user authored, in a visible public group. 403 = no
# `reviews:read`; 404 = holds it but is neither a reviewer (`reviews:create`) nor the flag's
# author; 200 = reviewer or break-glass manager. Detail and transcript share the scope, so
# both routes are asserted per role.
_FOREIGN_SUBMISSION_EXPECTATIONS = [
    (SystemRole.ADMIN, status.HTTP_200_OK),
    (SystemRole.OWNER, status.HTTP_200_OK),
    (SystemRole.RED_TEAMER, status.HTTP_404_NOT_FOUND),
    (SystemRole.ANNOTATOR, status.HTTP_200_OK),
    (SystemRole.VIEWER, status.HTTP_403_FORBIDDEN),
]


@pytest.mark.parametrize(
    ("role", "expected_status"),
    _FOREIGN_SUBMISSION_EXPECTATIONS,
    ids=[role.value for role, _ in _FOREIGN_SUBMISSION_EXPECTATIONS],
)
async def test_foreign_submission_surfaces_by_role(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    role: SystemRole,
    expected_status: int,
) -> None:
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-sub-author@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    messages = await _seed_messages(db_session, conversation.id, 2)
    flag = await _flag_for(db_session, conversation=conversation, evaluation=evaluation, created_by_id=author.id)
    await _flag_messages(db_session, flag, messages)
    caller = await _caller(db_session, system_roles[role.value], email="mv-sub-caller@example.com")

    transcript = await _get_submission_messages(auth_db_client, caller, flag.id)
    detail = await _get_submission_detail(auth_db_client, caller, flag.id)

    assert transcript.status_code == expected_status
    assert detail.status_code == expected_status
    if expected_status == status.HTTP_200_OK:
        assert [message["content"] for message in transcript.json()["items"]] == ["msg 0", "msg 1"]
        # Unordered: the selection sorts on `created_at` first, which ties for rows written in
        # one transaction, so the seeded pair falls through to a uuid4 tiebreak.
        assert {message["content"] for message in detail.json()["submission"]["messages"]} == {"msg 0", "msg 1"}
    else:
        _assert_problem(transcript, expected_status)
        _assert_problem(detail, expected_status)


async def test_private_group_hides_submission_from_non_member_reviewer(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Holding `reviews:create` does not reach into a private group the reviewer isn't in."""
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-priv-sub-a@example.com")
    annotator = await _caller(db_session, system_roles[SystemRole.ANNOTATOR.value], email="mv-priv-sub-r@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    messages = await _seed_messages(db_session, conversation.id, 1)
    flag = await _flag_for(db_session, conversation=conversation, evaluation=evaluation, created_by_id=author.id)
    await _flag_messages(db_session, flag, messages)

    transcript = await _get_submission_messages(auth_db_client, annotator, flag.id)
    detail = await _get_submission_detail(auth_db_client, annotator, flag.id)

    assert transcript.status_code == status.HTTP_404_NOT_FOUND
    assert detail.status_code == status.HTTP_404_NOT_FOUND


async def test_private_group_member_reviewer_reads_submission(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    annotator_role = system_roles[SystemRole.ANNOTATOR.value]
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-priv-mem-a@example.com")
    annotator = await _caller(db_session, annotator_role, email="mv-priv-mem-r@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await _grant_in_group(db_session, evaluation.evaluation_group_id, annotator.id, annotator_role)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    messages = await _seed_messages(db_session, conversation.id, 1)
    flag = await _flag_for(db_session, conversation=conversation, evaluation=evaluation, created_by_id=author.id)
    await _flag_messages(db_session, flag, messages)

    response = await _get_submission_messages(auth_db_client, annotator, flag.id)

    assert response.status_code == status.HTTP_200_OK
    assert [message["content"] for message in response.json()["items"]] == ["msg 0"]


async def test_review_assignment_alone_grants_no_transcript_access(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Being the assigned reviewer is not sufficient — `reviews:create` is what decides.

    `scope_review_flags` never reads the `Review` table, so a red-teamer named as reviewer on
    someone else's flag still gets a 404. Together with the annotator row above (200 with no
    assignment at all) this pins assignment as neither necessary nor sufficient — the answer to
    the requirement's "annotator assigned to a flag" wording.
    """
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-assign-a@example.com")
    reviewer = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-assign-r@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    messages = await _seed_messages(db_session, conversation.id, 1)
    flag = await _flag_for(db_session, conversation=conversation, evaluation=evaluation, created_by_id=author.id)
    await _flag_messages(db_session, flag, messages)
    db_session.add(
        Review(message_flag_id=flag.id, reviewer_id=reviewer.id, assigned_by_id=author.id, evaluation_id=evaluation.id)
    )
    await db_session.flush()

    response = await _get_submission_messages(auth_db_client, reviewer, flag.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_submission_transcript_returns_the_whole_conversation_not_only_flagged(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    """Pins a **gap**, not intent: the reviewer transcript is not narrowed to the flagged selection.

    The requirement asks that an annotator "can only see messages that were flagged". The submission
    *detail* honours that — it projects the flag's own selection — but the transcript route reads
    the full parent conversation, deliberately, so a reviewer can judge an exploit in context
    (`mergeTranscript` on the client labels rows `context` / `flagged` / `superseded`). Every other
    case in this module flags every seeded message, which cannot tell the two apart; this one
    flags exactly one of three so the difference is asserted rather than assumed. Narrowing the
    route would flip this test — which is the point.
    """
    author = await _caller(db_session, system_roles[SystemRole.RED_TEAMER.value], email="mv-mixed-author@example.com")
    annotator = await _caller(db_session, system_roles[SystemRole.ANNOTATOR.value], email="mv-mixed-r@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=author.id, evaluation=evaluation)
    messages = await _seed_messages(db_session, conversation.id, 3)
    flag = await _flag_for(db_session, conversation=conversation, evaluation=evaluation, created_by_id=author.id)
    await _flag_messages(db_session, flag, [messages[1]])

    transcript = await _get_submission_messages(auth_db_client, annotator, flag.id)
    detail = await _get_submission_detail(auth_db_client, annotator, flag.id)

    assert transcript.status_code == status.HTTP_200_OK
    assert detail.status_code == status.HTTP_200_OK
    assert [message["content"] for message in transcript.json()["items"]] == ["msg 0", "msg 1", "msg 2"]
    # The detail surface *is* narrowed to the selection — the split the requirement's wording misses.
    assert [message["content"] for message in detail.json()["submission"]["messages"]] == ["msg 1"]
