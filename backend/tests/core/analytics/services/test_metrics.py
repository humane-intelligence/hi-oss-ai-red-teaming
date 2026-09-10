"""Integration tests for `evaluation_group_metrics` — the whole-event aggregate.

Seeds a group with two evaluations, scenarios+tasks, members across roles,
flags spread over scenarios/tasks/statuses, reviews with mixed verdicts, and
conversations+messages, then asserts the group roll-up, the per-evaluation
split, the by-scenario / by-task counts, and that soft-deleted rows are excluded.
"""

from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import NamedTuple
from uuid import UUID
from uuid import uuid4

import pytest
import time_machine
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.analytics.services.metrics import evaluation_group_metrics
from app.core.analytics.services.metrics import evaluation_metrics
from app.core.annotations.enums import FlagStatus
from app.core.annotations.models import FlaggedMessage
from app.core.annotations.models import MessageFlag
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import SystemRole
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.enums import MetricsScope
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.models import Review
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


class Seeded(NamedTuple):
    """A seeded group plus the entity ids the assertions key off."""

    group: EvaluationGroup
    ids: dict[str, UUID]


async def _user(db: AsyncSession) -> User:
    user = User(email=f"{uuid4().hex[:8]}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _evaluation(db: AsyncSession, group: EvaluationGroup, *, mask_models: bool = False) -> Evaluation:
    evaluation = Evaluation(
        title=f"Eval {uuid4().hex[:6]}",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        mask_models_enabled=mask_models,
    )
    db.add(evaluation)
    await db.flush()
    return evaluation


async def _scenario(db: AsyncSession, evaluation: Evaluation, position: int) -> Scenario:
    scenario = Scenario(name=f"Scenario {position}", description="d", evaluation_id=evaluation.id, position=position)
    db.add(scenario)
    await db.flush()
    return scenario


async def _task(db: AsyncSession, scenario: Scenario) -> Task:
    task = Task(name=f"Task {uuid4().hex[:6]}", description="d", scenario_id=scenario.id)
    db.add(task)
    await db.flush()
    return task


async def _ai_model(db: AsyncSession) -> AiModel:
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db.add(model)
    await db.flush()
    return model


async def _assign_model(
    db: AsyncSession, evaluation: Evaluation, *, display_mask: str | None = None, model: AiModel | None = None
) -> EvaluationAiModel:
    """Assign a model to ``evaluation``, minting a fresh registry row unless ``model`` is given.

    Passing ``model`` is what lets a test assign the *same* underlying model to two evaluations —
    the shape the group-level per-model roll-up has to merge into one row.
    """
    if model is None:
        model = await _ai_model(db)
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id, model_display_mask=display_mask)
    db.add(assignment)
    await db.flush()
    return assignment


async def _conversation_with_messages(
    db: AsyncSession,
    evaluation: Evaluation,
    assignment: EvaluationAiModel,
    owner: User,
    *,
    scenario: Scenario | None = None,
    n_messages: int,
) -> Conversation:
    """``scenario`` tags the conversation; leave it out and a fresh scenario is minted for it."""
    if scenario is None:
        scenario = await _scenario(db, evaluation, 0)
    conversation_group = ConversationGroup(
        user_id=owner.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=owner.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    db.add(conversation)
    await db.flush()
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db.add(turn)
    await db.flush()
    for i in range(n_messages):
        db.add(
            Message(
                turn_id=turn.id,
                role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
                status=MessageStatus.COMPLETE,
                content="x",
            )
        )
    await db.flush()
    return conversation


class Reply(NamedTuple):
    """One assistant turn for `_conversation_with_usage`.

    ``usage`` is the `extra["usage"]` payload the streaming finalize would have written —
    `None` means the provider reported none, which `_drop_nulls` persists as a *missing*
    key rather than an empty object (see `services/generation.py`).

    Values are `Any`, not `int`, because the payload is persisted verbatim from a provider
    response: an out-of-range count, a fraction or a string are all shapes the store accepts.
    """

    usage: dict[str, Any] | None = None
    status: MessageStatus = MessageStatus.COMPLETE


async def _conversation_with_usage(
    db: AsyncSession,
    evaluation: Evaluation,
    assignment: EvaluationAiModel,
    owner: User,
    *,
    replies: list[Reply],
    scenario: Scenario | None = None,
) -> Conversation:
    """A conversation whose assistant messages carry token usage, one turn per reply.

    Mirrors the production shape rather than a flat message list: each reply is a user
    prompt (never carrying usage) plus the assistant message that answers it, so a test
    asserting "only assistant messages report usage" is meaningful. ``scenario`` sets
    `Conversation.scenario_id`, which is what the per-scenario token grouping keys off;
    leave it out and a fresh scenario is minted for the conversation.
    """
    if scenario is None:
        scenario = await _scenario(db, evaluation, 0)
    conversation_group = ConversationGroup(
        user_id=owner.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=owner.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    db.add(conversation)
    await db.flush()
    for turn_index, reply in enumerate(replies):
        turn = Turn(conversation_id=conversation.id, turn_index=turn_index)
        db.add(turn)
        await db.flush()
        db.add(Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content="q", extra={}))
        db.add(
            Message(
                turn_id=turn.id,
                role=MessageRole.ASSISTANT,
                status=reply.status,
                content="a",
                extra={"usage": reply.usage} if reply.usage is not None else {},
            )
        )
    await db.flush()
    return conversation


async def _flag(  # noqa: PLR0913 — keyword-only args mirror the flag's denormalised parent columns
    db: AsyncSession,
    *,
    author: User,
    conversation: Conversation,
    evaluation: Evaluation,
    group: EvaluationGroup,
    scenario: Scenario,
    task: Task | None,
    status: FlagStatus,
    created_at: datetime | None = None,
) -> MessageFlag:
    """Persist a submission. ``created_at`` must be passed to place it on a specific day — the
    column carries a `server_default`, so every flag otherwise lands on the database's today."""
    flag = MessageFlag(
        reason="r",
        created_by_id=author.id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=group.id,
        scenario_id=scenario.id,
        task_id=task.id if task is not None else None,
        status=status,
    )
    if created_at is not None:
        flag.created_at = created_at
    db.add(flag)
    await db.flush()
    return flag


async def _review(
    db: AsyncSession,
    *,
    flag: MessageFlag,
    reviewer: User,
    status: ReviewStatus,
    successful: bool | None,
    unique: bool | None,
    valid: bool | None,
) -> Review:
    review = Review(
        message_flag_id=flag.id,
        reviewer_id=reviewer.id,
        assigned_by_id=reviewer.id,
        evaluation_id=flag.evaluation_id,
        status=status,
        successful_exploit=successful,
        unique_exploit=unique,
        valid_submission=valid,
    )
    db.add(review)
    await db.flush()
    return review


@pytest.fixture
async def seeded_group(db_session: AsyncSession, system_roles: dict[str, Role]) -> Seeded:
    """A fully-populated event: 2 evaluations, scenarios/tasks, members, flags, reviews, activity."""
    group = await persist_evaluation_group(db_session)  # grants creator the in-group `owner` role

    # Participants: owner (from persist) + 2 red_teamers + 1 annotator.
    for _ in range(2):
        rt = await _user(db_session)
        await grant_roles(
            db_session, ObjectType.EVALUATION_GROUP, group.id, rt.id, [system_roles[SystemRole.RED_TEAMER.value]]
        )
    annotator = await _user(db_session)
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, group.id, annotator.id, [system_roles[SystemRole.ANNOTATOR.value]]
    )
    author = await _user(db_session)
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, group.id, author.id, [system_roles[SystemRole.RED_TEAMER.value]]
    )

    # --- Evaluation 1: 2 models, scenarios S1(T1,T2)/S2(no tasks)/Szero(Tzero). ---
    e1 = await _evaluation(db_session, group)
    a1 = await _assign_model(db_session, e1)
    await _assign_model(db_session, e1)
    s1 = await _scenario(db_session, e1, 0)
    t1 = await _task(db_session, s1)
    t2 = await _task(db_session, s1)
    s2 = await _scenario(db_session, e1, 1)
    s_zero = await _scenario(db_session, e1, 2)
    await _task(db_session, s_zero)  # zero-submission task must still appear
    conv1 = await _conversation_with_messages(db_session, e1, a1, author, scenario=s1, n_messages=2)

    await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s1,
        task=t1,
        status=FlagStatus.PENDING,
    )
    f_approved = await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s1,
        task=t1,
        status=FlagStatus.APPROVED,
    )
    f_t2 = await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s1,
        task=t2,
        status=FlagStatus.APPROVED,
    )
    await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s2,
        task=None,
        status=FlagStatus.REJECTED,
    )
    # A soft-deleted flag must NOT be counted.
    deleted_flag = await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s1,
        task=t1,
        status=FlagStatus.APPROVED,
    )
    deleted_flag.soft_delete(None)
    db_session.add(deleted_flag)
    await db_session.flush()

    # E1 reviews.
    await _review(
        db_session,
        flag=f_approved,
        reviewer=annotator,
        status=ReviewStatus.APPROVED,
        successful=True,
        unique=True,
        valid=True,
    )
    await _review(
        db_session,
        flag=f_t2,
        reviewer=annotator,
        status=ReviewStatus.REJECTED,
        successful=False,
        unique=None,
        valid=True,
    )
    await _review(
        db_session,
        flag=f_approved,
        reviewer=author,
        status=ReviewStatus.PENDING,
        successful=None,
        unique=None,
        valid=None,
    )

    # --- Evaluation 2: 1 model, scenario S3(T3). ---
    e2 = await _evaluation(db_session, group)
    a2 = await _assign_model(db_session, e2)
    s3 = await _scenario(db_session, e2, 0)
    t3 = await _task(db_session, s3)
    conv2 = await _conversation_with_messages(db_session, e2, a2, author, scenario=s3, n_messages=1)

    f_e2_approved = await _flag(
        db_session,
        author=author,
        conversation=conv2,
        evaluation=e2,
        group=group,
        scenario=s3,
        task=t3,
        status=FlagStatus.APPROVED,
    )
    await _flag(
        db_session,
        author=author,
        conversation=conv2,
        evaluation=e2,
        group=group,
        scenario=s3,
        task=None,
        status=FlagStatus.PENDING,
    )
    await _review(
        db_session,
        flag=f_e2_approved,
        reviewer=annotator,
        status=ReviewStatus.APPROVED,
        successful=True,
        unique=False,
        valid=True,
    )

    return Seeded(
        group=group,
        ids={
            "e1": e1.id,
            "e2": e2.id,
            "s1": s1.id,
            "s2": s2.id,
            "s_zero": s_zero.id,
            "s3": s3.id,
            "t1": t1.id,
            "t2": t2.id,
            "t3": t3.id,
        },
    )


async def test_group_rollup_totals(db_session: AsyncSession, seeded_group: Seeded) -> None:
    result = await evaluation_group_metrics(db_session, seeded_group.group)

    assert result.group_id == seeded_group.group.id
    assert result.scope is MetricsScope.FULL
    # owner (creator) + 2 red_teamers + 1 annotator + the author (also a red_teamer).
    assert result.members.total == 5
    assert result.members.by_role == {"owner": 1, "red_teamer": 3, "annotator": 1}
    # 6 live flags (the 7th is soft-deleted): 2 pending, 3 approved, 1 rejected.
    assert result.submissions.total == 6
    assert result.submissions.pending == 2
    assert result.submissions.approved == 3
    assert result.submissions.rejected == 1
    # 4 reviews: 3 completed (approved/rejected), 1 pending; verdicts 2/1/3.
    assert result.reviews.total == 4
    assert result.reviews.completed == 3
    assert result.reviews.pending == 1
    assert result.reviews.successful_exploit == 2
    assert result.reviews.unique_exploit == 1
    assert result.reviews.valid_submission == 3
    # Activity: 2 conversations, 3 messages (2 in E1 + 1 in E2).
    assert result.activity.conversations == 2
    assert result.activity.messages == 3


async def test_per_evaluation_split(db_session: AsyncSession, seeded_group: Seeded) -> None:
    ids = seeded_group.ids
    result = await evaluation_group_metrics(db_session, seeded_group.group)
    by_eval = {evaluation.evaluation_id: evaluation for evaluation in result.evaluations}

    assert set(by_eval) == {ids["e1"], ids["e2"]}

    e1 = by_eval[ids["e1"]]
    assert e1.models_assigned == 2
    assert (e1.submissions.total, e1.submissions.pending, e1.submissions.approved, e1.submissions.rejected) == (
        4,
        1,
        2,
        1,
    )
    assert (e1.reviews.total, e1.reviews.completed, e1.reviews.pending) == (3, 2, 1)
    assert (e1.reviews.successful_exploit, e1.reviews.unique_exploit, e1.reviews.valid_submission) == (1, 1, 2)
    assert (e1.activity.conversations, e1.activity.messages) == (1, 2)

    e2 = by_eval[ids["e2"]]
    assert e2.models_assigned == 1
    assert (e2.submissions.total, e2.submissions.approved, e2.submissions.pending) == (2, 1, 1)
    assert (e2.reviews.total, e2.reviews.completed, e2.reviews.successful_exploit) == (1, 1, 1)
    assert (e2.activity.conversations, e2.activity.messages) == (1, 1)


async def test_by_scenario_and_task_breakdown_includes_zero_rows(
    db_session: AsyncSession, seeded_group: Seeded
) -> None:
    ids = seeded_group.ids
    result = await evaluation_group_metrics(db_session, seeded_group.group)
    by_eval = {evaluation.evaluation_id: evaluation for evaluation in result.evaluations}

    e1_scenarios = {scenario.scenario_id: scenario for scenario in by_eval[ids["e1"]].scenarios}
    # All three E1 scenarios appear, including the zero-submission one.
    assert set(e1_scenarios) == {ids["s1"], ids["s2"], ids["s_zero"]}
    assert e1_scenarios[ids["s1"]].submissions_total == 3  # f_pending + f_approved + f_t2
    assert e1_scenarios[ids["s2"]].submissions_total == 1
    assert e1_scenarios[ids["s_zero"]].submissions_total == 0

    s1_tasks = {task.task_id: task for task in e1_scenarios[ids["s1"]].tasks}
    assert s1_tasks[ids["t1"]].submissions_total == 2  # soft-deleted flag excluded
    assert s1_tasks[ids["t2"]].submissions_total == 1
    # The zero-submission scenario still lists its task at zero.
    assert e1_scenarios[ids["s_zero"]].tasks[0].submissions_total == 0

    e2_scenarios = {scenario.scenario_id: scenario for scenario in by_eval[ids["e2"]].scenarios}
    assert e2_scenarios[ids["s3"]].submissions_total == 2  # f_e2_approved (task) + pending (no task)
    s3_tasks = {task.task_id: task for task in e2_scenarios[ids["s3"]].tasks}
    assert s3_tasks[ids["t3"]].submissions_total == 1


async def test_empty_group_returns_zeroed_metrics(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)

    result = await evaluation_group_metrics(db_session, group)

    assert result.members.total == 1  # only the auto-assigned owner
    assert result.submissions.total == 0
    assert result.reviews.total == 0
    assert result.activity == result.activity.model_validate({"conversations": 0, "messages": 0})
    assert result.evaluations == []


async def test_group_rollup_excludes_flags_on_soft_deleted_parents(db_session: AsyncSession) -> None:
    # Regression: the group submission roll-up must join live parents like the flags
    # read-path — a flag on a soft-deleted evaluation or conversation is excluded, so the
    # group total matches the per-evaluation rows (and the rest of the app) rather than
    # silently over-counting.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)

    # Live evaluation + a flag on a live conversation → counted.
    live_eval = await _evaluation(db_session, group)
    live_assignment = await _assign_model(db_session, live_eval)
    live_scenario = await _scenario(db_session, live_eval, position=0)
    live_conv = await _conversation_with_messages(db_session, live_eval, live_assignment, author, n_messages=1)
    await _flag(
        db_session,
        author=author,
        conversation=live_conv,
        evaluation=live_eval,
        group=group,
        scenario=live_scenario,
        task=None,
        status=FlagStatus.APPROVED,
    )

    # (a) Flag on a soft-deleted evaluation → excluded (its id is not a live eval).
    dead_eval = await _evaluation(db_session, group)
    dead_assignment = await _assign_model(db_session, dead_eval)
    dead_scenario = await _scenario(db_session, dead_eval, position=0)
    dead_eval_conv = await _conversation_with_messages(db_session, dead_eval, dead_assignment, author, n_messages=1)
    await _flag(
        db_session,
        author=author,
        conversation=dead_eval_conv,
        evaluation=dead_eval,
        group=group,
        scenario=dead_scenario,
        task=None,
        status=FlagStatus.PENDING,
    )
    dead_eval.soft_delete(None)
    db_session.add(dead_eval)

    # (b) Flag on a soft-deleted conversation (evaluation still live) → excluded.
    dead_conv = await _conversation_with_messages(db_session, live_eval, live_assignment, author, n_messages=1)
    await _flag(
        db_session,
        author=author,
        conversation=dead_conv,
        evaluation=live_eval,
        group=group,
        scenario=live_scenario,
        task=None,
        status=FlagStatus.REJECTED,
    )
    dead_conv.soft_delete(None)
    db_session.add(dead_conv)
    await db_session.flush()

    result = await evaluation_group_metrics(db_session, group)

    # Only the single live-parent flag is counted, and the group total equals the sum
    # of the per-evaluation rows — no self-inconsistency.
    assert result.submissions.total == 1
    assert result.submissions.approved == 1
    assert sum(evaluation.submissions.total for evaluation in result.evaluations) == result.submissions.total


async def test_review_rollup_excludes_reviews_on_soft_deleted_parents(db_session: AsyncSession) -> None:
    # Regression: review tallies must join the live parent flag + conversation like
    # reviews/access.py — soft-deleting a flag (or its conversation, e.g. a model-unassign
    # cascade) doesn't cascade to the flag's own reviews, so a review whose parent is dead
    # must be excluded rather than counted forever (which would diverge from every read path).
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)

    live_eval = await _evaluation(db_session, group)
    live_assignment = await _assign_model(db_session, live_eval)
    live_scenario = await _scenario(db_session, live_eval, position=0)
    live_conv = await _conversation_with_messages(db_session, live_eval, live_assignment, author, n_messages=1)

    # Live flag + review → counted.
    live_flag = await _flag(
        db_session,
        author=author,
        conversation=live_conv,
        evaluation=live_eval,
        group=group,
        scenario=live_scenario,
        task=None,
        status=FlagStatus.APPROVED,
    )
    await _review(
        db_session,
        flag=live_flag,
        reviewer=reviewer,
        status=ReviewStatus.APPROVED,
        successful=True,
        unique=True,
        valid=True,
    )

    # (a) Review on a soft-deleted flag (conversation still live) → excluded.
    dead_flag = await _flag(
        db_session,
        author=author,
        conversation=live_conv,
        evaluation=live_eval,
        group=group,
        scenario=live_scenario,
        task=None,
        status=FlagStatus.APPROVED,
    )
    await _review(
        db_session,
        flag=dead_flag,
        reviewer=reviewer,
        status=ReviewStatus.REJECTED,
        successful=True,
        unique=True,
        valid=True,
    )
    dead_flag.soft_delete(None)
    db_session.add(dead_flag)

    # (b) Review on a flag whose conversation is soft-deleted (flag itself live) → excluded.
    dead_conv = await _conversation_with_messages(db_session, live_eval, live_assignment, author, n_messages=1)
    flag_on_dead_conv = await _flag(
        db_session,
        author=author,
        conversation=dead_conv,
        evaluation=live_eval,
        group=group,
        scenario=live_scenario,
        task=None,
        status=FlagStatus.APPROVED,
    )
    await _review(
        db_session,
        flag=flag_on_dead_conv,
        reviewer=reviewer,
        status=ReviewStatus.APPROVED,
        successful=True,
        unique=True,
        valid=True,
    )
    dead_conv.soft_delete(None)
    db_session.add(dead_conv)
    await db_session.flush()

    result = await evaluation_group_metrics(db_session, group)

    # 3 reviews exist, but two sit on a soft-deleted flag / conversation — only the single
    # live-parent review survives the join. This is the assertion with teeth: it would be 3
    # without the live-parent join.
    assert result.reviews.total == 1
    assert result.reviews.completed == 1
    assert result.reviews.successful_exploit == 1
    # The same live-parent filter must hold on the per-evaluation row the group total is
    # summed from. (This is not a group-vs-sum tautology — both derive from `eval_reviews`;
    # it pins the surviving count to the live evaluation's own breakdown row.)
    by_eval = {evaluation.evaluation_id: evaluation for evaluation in result.evaluations}
    assert by_eval[live_eval.id].reviews.total == 1


async def _exploited_at_turn(  # noqa: PLR0913 — keyword-only args describe the shape of the seeded submission; bundling them into a carrier would just shift the surface area
    db: AsyncSession,
    *,
    evaluation: Evaluation,
    assignment: EvaluationAiModel,
    author: User,
    reviewer: User,
    group: EvaluationGroup,
    turn_index: int,
    successful: bool = True,
    usages: list[dict[str, int] | None] | None = None,
) -> Conversation:
    """Seed one submission whose flagged message sits at ``turn_index``, with a review verdict.

    Builds a conversation with turns 0..turn_index (one message each), flags the message at
    ``turn_index``, and records a review — a successful exploit unless ``successful`` is False.
    Returns the conversation so a caller can soft-delete it to exercise the live-parent joins.

    ``usages`` puts token usage on the turns, index-aligned (an entry of `None` is a turn whose
    provider reported none). A list longer than ``turn_index`` + 1 also creates turns *after* the
    flagged one — the shape that separates a cost-to-exploit from a whole-conversation sum.
    """
    usages = usages or []
    conversation_scenario = await _scenario(db, evaluation, 0)
    conversation_group = ConversationGroup(
        user_id=author.id, evaluation_id=evaluation.id, name="g", scenario_id=conversation_scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=author.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=conversation_scenario.id,
    )
    db.add(conversation)
    await db.flush()
    flagged_message: Message | None = None
    for index in range(max(turn_index + 1, len(usages))):
        turn = Turn(conversation_id=conversation.id, turn_index=index)
        db.add(turn)
        await db.flush()
        usage = usages[index] if index < len(usages) else None
        message = Message(
            turn_id=turn.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETE,
            content="x",
            extra={"usage": usage} if usage is not None else {},
        )
        db.add(message)
        await db.flush()
        if index == turn_index:
            flagged_message = message
    assert flagged_message is not None
    flag = MessageFlag(
        reason="r",
        created_by_id=author.id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=group.id,
        status=FlagStatus.APPROVED,
    )
    db.add(flag)
    await db.flush()
    db.add(FlaggedMessage(message_flag_id=flag.id, message_id=flagged_message.id))
    await db.flush()
    await _review(
        db,
        flag=flag,
        reviewer=reviewer,
        status=ReviewStatus.APPROVED,
        successful=successful,
        unique=successful,
        valid=True,
    )
    return conversation


async def _conversation_with_turn_messages(
    db: AsyncSession,
    *,
    evaluation: Evaluation,
    assignment: EvaluationAiModel,
    author: User,
    turn_indices: list[int],
) -> tuple[Conversation, dict[int, Message]]:
    """A conversation with one assistant message per turn in ``turn_indices``; returns the messages by turn.

    Lets a test flag a subset of turns on one flag (multi-turn selection) or hang several reviews off
    one flag — the shapes `_exploited_at_turn` (one message, one review) can't express.
    """
    conversation_scenario = await _scenario(db, evaluation, 0)
    conversation_group = ConversationGroup(
        user_id=author.id, evaluation_id=evaluation.id, name="g", scenario_id=conversation_scenario.id
    )
    db.add(conversation_group)
    await db.flush()
    conversation = Conversation(
        user_id=author.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=conversation_scenario.id,
    )
    db.add(conversation)
    await db.flush()
    messages: dict[int, Message] = {}
    for index in turn_indices:
        turn = Turn(conversation_id=conversation.id, turn_index=index)
        db.add(turn)
        await db.flush()
        message = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="x")
        db.add(message)
        await db.flush()
        messages[index] = message
    return conversation, messages


async def test_exploit_distributions_dedup_multiple_confirming_reviewers(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # One submission, two reviewers both confirming a successful exploit. The count(distinct MessageFlag.id)
    # / EXISTS design must count it ONCE across every exploit surface — a second confirming review can't
    # inflate the headline, the by-model chart, or the by-prompt chart.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer_one = await _user(db_session)
    reviewer_two = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    conversation, messages = await _conversation_with_turn_messages(
        db_session, evaluation=evaluation, assignment=assignment, author=author, turn_indices=[0]
    )
    flag = MessageFlag(
        reason="r",
        created_by_id=author.id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=group.id,
        status=FlagStatus.APPROVED,
    )
    db_session.add(flag)
    await db_session.flush()
    db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=messages[0].id))
    await db_session.flush()
    for reviewer in (reviewer_one, reviewer_two):
        await _review(
            db_session,
            flag=flag,
            reviewer=reviewer,
            status=ReviewStatus.APPROVED,
            successful=True,
            unique=True,
            valid=True,
        )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.exploited_submissions == 1
    assert [(b.prompt_count, b.exploit_count) for b in result.exploits_by_prompt_count] == [(1, 1)]
    assert {m.evaluation_ai_model_id: m.exploit_count for m in result.exploits_by_model} == {assignment.id: 1}


async def test_exploits_by_prompt_count_uses_earliest_flagged_turn(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A single flag spanning two turns: prompt count is the EARLIEST flagged turn (3 → 4 prompts),
    # not the latest. Pins func.min(turn_index) — with one flagged message per flag, min↔max would be
    # indistinguishable; here max would wrongly report 6.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    conversation, messages = await _conversation_with_turn_messages(
        db_session, evaluation=evaluation, assignment=assignment, author=author, turn_indices=[3, 5]
    )
    flag = MessageFlag(
        reason="r",
        created_by_id=author.id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=group.id,
        status=FlagStatus.APPROVED,
    )
    db_session.add(flag)
    await db_session.flush()
    db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=messages[3].id))
    db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=messages[5].id))
    await db_session.flush()
    await _review(
        db_session, flag=flag, reviewer=reviewer, status=ReviewStatus.APPROVED, successful=True, unique=True, valid=True
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert [(b.prompt_count, b.exploit_count) for b in result.exploits_by_prompt_count] == [(4, 1)]
    assert result.exploited_submissions == 1


async def test_evaluation_metrics_matches_group_row(db_session: AsyncSession, seeded_group: Seeded) -> None:
    # The single-evaluation roll-up must equal that evaluation's row in the group dashboard —
    # both go through the same shared breakdown helpers, so they can't diverge.
    ids = seeded_group.ids
    group_result = await evaluation_group_metrics(db_session, seeded_group.group)
    group_e1 = next(e for e in group_result.evaluations if e.evaluation_id == ids["e1"])

    e1 = await db_session.get(Evaluation, ids["e1"])
    assert e1 is not None
    result = await evaluation_metrics(db_session, e1, group=seeded_group.group)

    assert result.evaluation_id == ids["e1"]
    assert result.models_assigned == group_e1.models_assigned
    assert result.submissions == group_e1.submissions
    assert result.reviews == group_e1.reviews
    assert result.activity == group_e1.activity
    assert result.scenarios == group_e1.scenarios


async def test_exploits_by_prompt_count_distribution(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    # One exploit at turn 0 (1 prompt), two at turn 2 (3 prompts), and one non-successful
    # verdict at turn 5 (must NOT appear — only confirmed exploits count).
    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=0,
    )
    for _ in range(2):
        await _exploited_at_turn(
            db_session,
            evaluation=evaluation,
            assignment=assignment,
            author=author,
            reviewer=reviewer,
            group=group,
            turn_index=2,
        )
    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=5,
        successful=False,
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert [(b.prompt_count, b.exploit_count) for b in result.exploits_by_prompt_count] == [(1, 1), (3, 2)]


async def test_exploits_by_model_is_zero_inclusive_and_ranked(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    exploited = await _assign_model(db_session, evaluation)
    held = await _assign_model(db_session, evaluation)  # never exploited → must still appear, at 0

    for turn_index in (0, 1):
        await _exploited_at_turn(
            db_session,
            evaluation=evaluation,
            assignment=exploited,
            author=author,
            reviewer=reviewer,
            group=group,
            turn_index=turn_index,
        )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert {m.evaluation_ai_model_id: m.exploit_count for m in result.exploits_by_model} == {
        exploited.id: 2,
        held.id: 0,
    }
    # Ranked by exploit count descending — the fallen model leads, the model that held is last.
    assert result.exploits_by_model[0].evaluation_ai_model_id == exploited.id
    assert result.exploits_by_model[-1].exploit_count == 0


async def test_evaluation_metrics_empty_is_zeroed(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _evaluation(db_session, group)

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.models_assigned == 0
    assert result.submissions.total == 0
    assert result.reviews.total == 0
    assert result.activity == result.activity.model_validate({"conversations": 0, "messages": 0})
    assert result.exploited_submissions == 0
    assert result.scenarios == []
    assert result.exploits_by_prompt_count == []
    assert result.exploits_by_model == []


async def test_personal_scope_filters_group_metrics_to_the_viewer(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A `personal`-scope read (viewer_id set) narrows every contribution breakdown to that
    # user's own submissions, conversations, and the reviews of their submissions — while the
    # member roster stays group-wide. Full scope sees everyone's.
    group = await persist_evaluation_group(db_session)
    evaluation = await _evaluation(db_session, group)
    scenario = await _scenario(db_session, evaluation, 0)
    assignment = await _assign_model(db_session, evaluation)
    alice = await _user(db_session)
    bob = await _user(db_session)
    reviewer = await _user(db_session)

    # Alice: 1 conversation (2 messages), 1 flag, 1 review of her flag.
    alice_conv = await _conversation_with_messages(db_session, evaluation, assignment, alice, n_messages=2)
    alice_flag = await _flag(
        db_session,
        author=alice,
        conversation=alice_conv,
        evaluation=evaluation,
        group=group,
        scenario=scenario,
        task=None,
        status=FlagStatus.APPROVED,
    )
    await _review(
        db_session,
        flag=alice_flag,
        reviewer=reviewer,
        status=ReviewStatus.APPROVED,
        successful=True,
        unique=True,
        valid=True,
    )

    # Bob: 1 conversation (1 message), 2 flags, 1 review of one of his flags.
    bob_conv = await _conversation_with_messages(db_session, evaluation, assignment, bob, n_messages=1)
    bob_flag = await _flag(
        db_session,
        author=bob,
        conversation=bob_conv,
        evaluation=evaluation,
        group=group,
        scenario=scenario,
        task=None,
        status=FlagStatus.APPROVED,
    )
    await _flag(
        db_session,
        author=bob,
        conversation=bob_conv,
        evaluation=evaluation,
        group=group,
        scenario=scenario,
        task=None,
        status=FlagStatus.PENDING,
    )
    await _review(
        db_session,
        flag=bob_flag,
        reviewer=reviewer,
        status=ReviewStatus.REJECTED,
        successful=False,
        unique=None,
        valid=True,
    )

    full = await evaluation_group_metrics(db_session, group)
    personal = await evaluation_group_metrics(db_session, group, viewer_id=alice.id)

    # Full: 3 flags, 2 conversations, 3 messages, 2 reviews.
    assert full.scope is MetricsScope.FULL
    assert full.submissions.total == 3
    assert (full.activity.conversations, full.activity.messages) == (2, 3)
    assert full.reviews.total == 2

    # Personal (Alice): only her 1 flag, 1 conversation, 2 messages, and the 1 review of her flag.
    assert personal.scope is MetricsScope.PERSONAL
    assert personal.submissions.total == 1
    assert personal.submissions.approved == 1
    assert (personal.activity.conversations, personal.activity.messages) == (1, 2)
    assert personal.reviews.total == 1
    assert personal.reviews.successful_exploit == 1
    # The member roster is structural — identical under either scope.
    assert personal.members.total == full.members.total


async def test_personal_scope_filters_evaluation_exploits_to_the_viewer(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Personal scope at the evaluation level narrows the exploit distributions and the headline
    # count to the viewer's own submissions.
    group = await persist_evaluation_group(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)
    alice = await _user(db_session)
    bob = await _user(db_session)
    reviewer = await _user(db_session)

    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=alice,
        reviewer=reviewer,
        group=group,
        turn_index=0,
    )
    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=bob,
        reviewer=reviewer,
        group=group,
        turn_index=1,
    )

    full = await evaluation_metrics(db_session, evaluation, group=group)
    personal = await evaluation_metrics(db_session, evaluation, group=group, viewer_id=alice.id)

    assert full.scope is MetricsScope.FULL
    assert full.exploited_submissions == 2
    assert {m.evaluation_ai_model_id: m.exploit_count for m in full.exploits_by_model} == {assignment.id: 2}

    assert personal.scope is MetricsScope.PERSONAL
    assert personal.exploited_submissions == 1  # only Alice's exploit
    assert {m.evaluation_ai_model_id: m.exploit_count for m in personal.exploits_by_model} == {assignment.id: 1}
    # Alice's exploit landed at turn 0 → 1 prompt.
    assert [(b.prompt_count, b.exploit_count) for b in personal.exploits_by_prompt_count] == [(1, 1)]


async def test_exploits_by_model_masks_names_when_requested(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # With mask_model_names, the by-model distribution surfaces each assignment's display mask
    # (or `null` when none is set) instead of the real alias; without it, the real alias shows.
    group = await persist_evaluation_group(db_session)
    evaluation = await _evaluation(db_session, group)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    masked = await _assign_model(db_session, evaluation, display_mask="Model A")
    unmasked = await _assign_model(db_session, evaluation)  # no display mask set

    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=masked,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=0,
    )

    real = await evaluation_metrics(db_session, evaluation, group=group)
    real_names = {m.evaluation_ai_model_id: m.model_alias for m in real.exploits_by_model}
    # Unmasked: real aliases, never the display mask.
    assert real_names[masked.id] is not None
    assert real_names[masked.id] != "Model A"
    assert real_names[unmasked.id] is not None

    hidden = await evaluation_metrics(db_session, evaluation, group=group, mask_model_names=True)
    hidden_names = {m.evaluation_ai_model_id: m.model_alias for m in hidden.exploits_by_model}
    # Masked: the display mask, or `null` when the assignment has none.
    assert hidden_names[masked.id] == "Model A"
    assert hidden_names[unmasked.id] is None


async def test_exploit_distributions_exclude_soft_deleted_conversations(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Both new aggregations join the live parent conversation — an exploit on a soft-deleted
    # conversation (e.g. a model-unassign cascade) must drop out of *both* distributions, exactly
    # like the review rollup. Guards the `Conversation.deleted_at` join from silent removal.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=0,
    )
    dead_conversation = await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=4,
    )
    dead_conversation.soft_delete(None)
    db_session.add(dead_conversation)
    await db_session.flush()

    result = await evaluation_metrics(db_session, evaluation, group=group)

    # Only the live-parent exploit (turn 0 → 1 prompt) survives; the dead one (turn 4) is gone.
    assert [(b.prompt_count, b.exploit_count) for b in result.exploits_by_prompt_count] == [(1, 1)]
    assert {m.evaluation_ai_model_id: m.exploit_count for m in result.exploits_by_model} == {assignment.id: 1}
    # The headline count joins the live parent conversation too — the dead exploit drops out.
    assert result.exploited_submissions == 1


async def test_token_metrics_roll_up_through_the_hierarchy(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The group total must equal the sum of its evaluations, and an evaluation's scenario rows must
    # not exceed it — the consistency property that catches a sum scoped by FK alone instead of by
    # the live-parent join the sibling `activity` count uses.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    first = await _evaluation(db_session, group)
    second = await _evaluation(db_session, group)
    scenario = await _scenario(db_session, first, position=0)
    assignment_a = await _assign_model(db_session, first)
    assignment_b = await _assign_model(db_session, second)

    await _conversation_with_usage(
        db_session,
        first,
        assignment_a,
        owner,
        scenario=scenario,
        replies=[Reply({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})],
    )
    await _conversation_with_usage(
        db_session,
        second,
        assignment_b,
        owner,
        replies=[Reply({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})],
    )

    result = await evaluation_group_metrics(db_session, group)
    by_evaluation = {row.evaluation_id: row for row in result.evaluations}

    assert result.tokens.total_tokens == 25
    assert result.tokens.prompt_tokens == 17
    assert result.tokens.completion_tokens == 8
    assert result.tokens.total_tokens == sum(row.tokens.total_tokens for row in result.evaluations)
    assert by_evaluation[first.id].tokens.total_tokens == 15
    assert by_evaluation[second.id].tokens.total_tokens == 10
    scenario_rows = by_evaluation[first.id].scenarios
    assert [row.tokens.total_tokens for row in scenario_rows] == [15]
    assert sum(row.tokens.total_tokens for row in scenario_rows) <= by_evaluation[first.id].tokens.total_tokens


async def test_token_metrics_exclude_messages_without_usage(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # `_drop_nulls` leaves `extra` without a "usage" key when the provider reported none, so such a
    # message must contribute neither tokens nor a denominator slot. Counting it would silently
    # deflate every average — the failure mode a `COALESCE(..., 0)` into the average produces.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[Reply({"total_tokens": 12}), Reply(None)],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 12
    assert result.tokens.messages_with_usage == 1
    # Four messages exist (two prompts + two replies); only one reported usage.
    assert result.activity.messages == 4
    assert result.tokens.avg_tokens_per_message == pytest.approx(12.0)


async def test_token_metrics_derive_total_from_partial_usage(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # `_to_usage` maps each field independently, so a provider may report a subset. Summing the
    # stored `total_tokens` alone would drop this message entirely; the derived total keeps it.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[Reply({"prompt_tokens": 9}), Reply({"prompt_tokens": 2, "completion_tokens": 3})],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    # 9 (prompt only) + 5 (prompt + completion, no total reported) — neither row carries `total_tokens`.
    assert result.tokens.total_tokens == 14
    assert result.tokens.prompt_tokens == 11
    assert result.tokens.completion_tokens == 3
    assert result.tokens.messages_with_usage == 2


async def test_token_metrics_count_non_complete_replies_without_usage(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Only a `complete` reply ever carries usage — `error` stores a structured error, `interrupted`
    # and `streaming` store nothing. Such replies still exist as messages, so they raise the
    # activity count while contributing no tokens and no denominator slot.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[
            Reply({"total_tokens": 20}),
            Reply(None, status=MessageStatus.ERROR),
            Reply(None, status=MessageStatus.INTERRUPTED),
            Reply(None, status=MessageStatus.STREAMING),
        ],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 20
    assert result.tokens.messages_with_usage == 1
    assert result.activity.messages == 8
    assert result.tokens.avg_tokens_per_message == pytest.approx(20.0)


async def test_token_metrics_exclude_soft_deleted_parents(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The recurring soft-delete cascade class: tokens must drop out through the live-parent join,
    # exactly like `activity.messages`, not via any column on the message itself.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, assignment, owner, replies=[Reply({"total_tokens": 30})])
    dead = await _conversation_with_usage(
        db_session, evaluation, assignment, owner, replies=[Reply({"total_tokens": 400})]
    )
    dead.soft_delete(None)
    db_session.add(dead)
    await db_session.flush()

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 30
    assert result.tokens.conversations_with_usage == 1


async def test_tokens_by_model_is_zero_inclusive(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    # Every live assignment appears, including a model nobody spent tokens against — the same
    # zero-inclusive property `exploits_by_model` has, so the console can show "this model was
    # assigned but unused" instead of omitting the row.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    used = await _assign_model(db_session, evaluation)
    unused = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, used, owner, replies=[Reply({"total_tokens": 8})])

    result = await evaluation_metrics(db_session, evaluation, group=group)
    totals = {row.evaluation_ai_model_id: row.tokens.total_tokens for row in result.tokens_by_model}

    assert totals == {used.id: 8, unused.id: 0}


async def test_tokens_by_model_masks_names_when_requested(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # `mask_model_names` defaults to the permissive value, so a missing thread-through would leak the
    # real alias of a masked evaluation's model alongside its cost. Guards that default.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    masked = await _assign_model(db_session, evaluation, display_mask="Model A")
    unmasked = await _assign_model(db_session, evaluation)  # no display mask set

    await _conversation_with_usage(db_session, evaluation, masked, owner, replies=[Reply({"total_tokens": 11})])

    real = await evaluation_metrics(db_session, evaluation, group=group)
    real_names = {row.evaluation_ai_model_id: row.model_alias for row in real.tokens_by_model}
    assert real_names[masked.id] != "Model A"
    assert real_names[masked.id] is not None

    hidden = await evaluation_metrics(db_session, evaluation, group=group, mask_model_names=True)
    hidden_names = {row.evaluation_ai_model_id: row.model_alias for row in hidden.tokens_by_model}
    assert hidden_names[masked.id] == "Model A"
    assert hidden_names[unmasked.id] is None


async def test_personal_scope_filters_token_metrics_to_the_viewer(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Tokens are a contribution metric, so `viewer_id` must narrow them — its permissive `None`
    # default would otherwise show a personal-scope member the whole event's spend. The assignment
    # roster stays structural: both models still appear.
    #
    # The scenario is load-bearing: the per-evaluation and per-scenario spend are two separate
    # statements narrowed in the same `if`, so without a scenario here the scenario half of the
    # narrowing is unpinned and could be dropped silently — a member would then read every user's
    # spend per scenario while their evaluation total stayed correctly narrowed.
    group = await persist_evaluation_group(db_session)
    mine = await _user(db_session)
    theirs = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)
    scenario = await _scenario(db_session, evaluation, 0)

    await _conversation_with_usage(
        db_session, evaluation, assignment, mine, replies=[Reply({"total_tokens": 5})], scenario=scenario
    )
    await _conversation_with_usage(
        db_session, evaluation, assignment, theirs, replies=[Reply({"total_tokens": 500})], scenario=scenario
    )

    full = await evaluation_metrics(db_session, evaluation, group=group)
    personal = await evaluation_metrics(db_session, evaluation, group=group, viewer_id=mine.id)

    assert full.tokens.total_tokens == 505
    assert personal.tokens.total_tokens == 5
    assert personal.tokens.conversations_with_usage == 1
    assert [row.tokens.total_tokens for row in full.scenarios] == [505]
    assert [row.tokens.total_tokens for row in personal.scenarios] == [5]
    personal_by_model = {row.evaluation_ai_model_id: row.tokens.total_tokens for row in personal.tokens_by_model}
    assert personal_by_model == {assignment.id: 5}


async def test_token_averages_divide_by_the_usage_bearing_denominator(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The averages the requirement asks for. Dividing by every message (or every conversation) instead of
    # by the ones that reported usage silently deflates both numbers — and a credential-less model
    # produces nothing but usage-free replies, so that mistake reads as ~0 rather than as missing data.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[Reply({"total_tokens": 10}), Reply({"total_tokens": 30}), Reply(None)],
    )
    await _conversation_with_usage(db_session, evaluation, assignment, owner, replies=[Reply(None)])

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 40
    assert result.tokens.messages_with_usage == 2
    assert result.tokens.conversations_with_usage == 1
    assert result.tokens.avg_tokens_per_message == pytest.approx(20.0)
    assert result.tokens.avg_tokens_per_conversation == pytest.approx(40.0)


async def test_group_tokens_by_model_merges_the_same_model_across_evaluations(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The primary ask, at group level: "how many tokens did THIS model cost across the
    # event". A model assigned to two evaluations must land in ONE row summing both — keying the
    # roll-up on the assignment instead would list it twice and answer a different question.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    shared = await _ai_model(db_session)
    first = await _evaluation(db_session, group)
    second = await _evaluation(db_session, group)
    in_first = await _assign_model(db_session, first, model=shared)
    in_second = await _assign_model(db_session, second, model=shared)
    unused = await _assign_model(db_session, first)

    await _conversation_with_usage(db_session, first, in_first, owner, replies=[Reply({"total_tokens": 10})])
    await _conversation_with_usage(db_session, second, in_second, owner, replies=[Reply({"total_tokens": 30})])

    result = await evaluation_group_metrics(db_session, group, full_model_access=True)
    totals = {row.ai_model_id: row.tokens.total_tokens for row in result.tokens_by_model}

    assert totals == {shared.id: 40, unused.model_id: 0}
    # The rows must reconcile with the headline they sit next to, not merely be internally tidy.
    assert sum(row.tokens.total_tokens for row in result.tokens_by_model) == result.tokens.total_tokens


async def test_group_tokens_by_model_hides_the_breakdown_when_any_evaluation_masks(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Fail-closed on the correlation the contract explicitly avoids: `EvaluationAiModelView` states
    # an assignment id "addresses the assignment without correlating the underlying model across
    # evaluations", and a group-level per-model row IS that correlation. So for a caller without
    # full metrics access, ONE masking evaluation withholds the whole breakdown — the group total
    # stays, only the attribution goes. Guards the permissive default on `full_model_access`.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    masking = await _evaluation(db_session, group, mask_models=True)
    open_eval = await _evaluation(db_session, group)
    hidden = await _assign_model(db_session, masking)
    shown = await _assign_model(db_session, open_eval)

    await _conversation_with_usage(db_session, masking, hidden, owner, replies=[Reply({"total_tokens": 7})])
    await _conversation_with_usage(db_session, open_eval, shown, owner, replies=[Reply({"total_tokens": 5})])

    member = await evaluation_group_metrics(db_session, group)

    assert member.tokens_by_model == []
    assert member.tokens.total_tokens == 12

    privileged = await evaluation_group_metrics(db_session, group, full_model_access=True)

    assert {row.ai_model_id for row in privileged.tokens_by_model} == {hidden.model_id, shown.model_id}
    assert all(row.model_alias for row in privileged.tokens_by_model)


async def test_group_tokens_by_model_is_visible_to_a_member_when_nothing_is_masked(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The other half of the fail-closed rule: withholding must be triggered by masking, not by
    # merely lacking full access. With nothing masked anywhere, model identity is not a secret in
    # this group, so a plain metrics-reader still gets the attribution.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, assignment, owner, replies=[Reply({"total_tokens": 9})])

    result = await evaluation_group_metrics(db_session, group)

    assert [(row.ai_model_id, row.tokens.total_tokens) for row in result.tokens_by_model] == [(assignment.model_id, 9)]


async def test_group_tokens_by_model_narrows_to_the_viewer_in_personal_scope(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Spend is a contribution metric, so the per-model roll-up must honour `viewer_id` too — its
    # permissive `None` default would otherwise show a personal-scope member the whole event's cost
    # broken down by model. The model still appears; only its number narrows.
    group = await persist_evaluation_group(db_session)
    mine = await _user(db_session)
    theirs = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, assignment, mine, replies=[Reply({"total_tokens": 5})])
    await _conversation_with_usage(db_session, evaluation, assignment, theirs, replies=[Reply({"total_tokens": 500})])

    personal = await evaluation_group_metrics(db_session, group, viewer_id=mine.id, full_model_access=True)

    assert [(row.ai_model_id, row.tokens.total_tokens) for row in personal.tokens_by_model] == [
        (assignment.model_id, 5)
    ]


async def test_group_tokens_by_model_drops_soft_deleted_evaluations(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The recurring bug class: an aggregate scoped only by FK would keep counting a soft-deleted
    # evaluation's spend, so the per-model rows would disagree with the group total beside them.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    live = await _evaluation(db_session, group)
    dead = await _evaluation(db_session, group)
    live_assignment = await _assign_model(db_session, live)
    dead_assignment = await _assign_model(db_session, dead)

    await _conversation_with_usage(db_session, live, live_assignment, owner, replies=[Reply({"total_tokens": 4})])
    await _conversation_with_usage(db_session, dead, dead_assignment, owner, replies=[Reply({"total_tokens": 100})])
    dead.soft_delete(None)
    await db_session.flush()

    result = await evaluation_group_metrics(db_session, group, full_model_access=True)
    totals = {row.ai_model_id: row.tokens.total_tokens for row in result.tokens_by_model}

    assert totals == {live_assignment.model_id: 4}
    assert sum(row.tokens.total_tokens for row in result.tokens_by_model) == result.tokens.total_tokens == 4


async def test_exploit_bucket_averages_tokens_spent_up_to_the_exploiting_turn(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # "3 prompts · ~438 tokens": what breaking the model on the third prompt cost. The turns AFTER
    # the exploit are the red-teamer exploring a hole they already found — counting them would make
    # the number a whole-conversation sum, incomparable between buckets and no longer a cost to break.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=2,
        usages=[
            {"total_tokens": 120},
            {"total_tokens": 150},
            {"total_tokens": 168},  # the flagged turn — counted
            {"total_tokens": 200},  # after the exploit — must not count
            {"total_tokens": 240},
        ],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)
    bucket = result.exploits_by_prompt_count[0]

    assert (bucket.prompt_count, bucket.exploit_count) == (3, 1)
    assert bucket.avg_tokens_to_exploit == pytest.approx(438.0)
    assert bucket.exploits_with_tokens == 1


async def test_exploit_bucket_averages_across_its_exploits(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A bucket holds N exploits, so the token figure must be a mean per exploit — a raw sum would
    # grow with the exploit count the bar already shows, reading "expensive" where it means "many".
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    for total in (100, 300):
        await _exploited_at_turn(
            db_session,
            evaluation=evaluation,
            assignment=assignment,
            author=author,
            reviewer=reviewer,
            group=group,
            turn_index=0,
            usages=[{"total_tokens": total}],
        )

    result = await evaluation_metrics(db_session, evaluation, group=group)
    bucket = result.exploits_by_prompt_count[0]

    assert (bucket.prompt_count, bucket.exploit_count) == (1, 2)
    assert bucket.avg_tokens_to_exploit == pytest.approx(200.0)
    assert bucket.exploits_with_tokens == 2


async def test_exploit_bucket_reports_no_average_when_nothing_reported_usage(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A zero here would read as "this exploit was free" instead of "the provider reported nothing",
    # so the bucket must say the cost is unknown while still counting the exploit.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=0,
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)
    bucket = result.exploits_by_prompt_count[0]

    assert bucket.exploit_count == 1
    assert bucket.avg_tokens_to_exploit is None
    assert bucket.exploits_with_tokens == 0


async def test_exploit_bucket_averages_only_the_exploits_that_reported_usage(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Mixed bucket: dividing by every exploit would halve the figure with an unmeasured one, so the
    # denominator is what actually reported — and the response returns it, since "~200" over 1 of 2
    # exploits is a different claim from "~200" over both.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=0,
        usages=[{"total_tokens": 200}],
    )
    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=0,
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)
    bucket = result.exploits_by_prompt_count[0]

    assert bucket.exploit_count == 2
    assert bucket.avg_tokens_to_exploit == pytest.approx(200.0)
    assert bucket.exploits_with_tokens == 1


async def test_exploit_bucket_tokens_exclude_soft_deleted_messages(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The recurring bug class: the prefix sum must join the live chain like the counts beside it, or
    # a tombstoned message keeps inflating the cost of an exploit it no longer belongs to.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    conversation = await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=1,
        usages=[{"total_tokens": 90}, {"total_tokens": 10}],
    )
    doomed = (
        await db_session.execute(
            select(Message)
            .join(Turn, col(Message.turn_id) == col(Turn.id))
            .where(col(Turn.conversation_id) == conversation.id, col(Turn.turn_index) == 0)
        )
    ).scalar_one()
    doomed.soft_delete(None)
    await db_session.flush()

    result = await evaluation_metrics(db_session, evaluation, group=group)
    bucket = result.exploits_by_prompt_count[0]

    # 90 is gone with its message; only the flagged turn's 10 remains.
    assert bucket.avg_tokens_to_exploit == pytest.approx(10.0)
    assert bucket.exploits_with_tokens == 1


async def test_token_metrics_survive_a_usage_count_beyond_int32(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # `extra` is persisted verbatim from the provider, and `Usage` bounds nothing, so a count above
    # 2**31-1 reaches the store. Reading it with an int4 cast makes Postgres raise, which takes the
    # whole group's dashboard down for good — there is no API path to repair `extra`.
    #
    # The second reply carries a count past int64: `bigint` raises on it exactly as `int4` raises on
    # the first, so the range has to be checked before the cast, not just the JSON type. Its two
    # out-of-range fields must read as NULL while its one usable field still counts.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[
            Reply({"prompt_tokens": 2_000_000_000, "completion_tokens": 1_000_000_000, "total_tokens": 3_000_000_000}),
            Reply({"prompt_tokens": 2**63, "completion_tokens": 5, "total_tokens": 2**63 + 10}),
        ],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    # Reply 2 contributes only its completion count; its unusable total falls back to 0 + 5.
    assert result.tokens.total_tokens == 3_000_000_005
    assert result.tokens.prompt_tokens == 2_000_000_000
    assert result.tokens.completion_tokens == 1_000_000_005
    assert result.tokens.messages_with_usage == 2


async def test_messages_with_usage_excludes_a_message_with_no_usable_value(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A key-presence denominator counts a message whose every reported value is unusable, so the
    # average is divided by a message that contributed nothing — understating instead of reading as
    # unmeasured. The opposite of how a usage-free exploit prefix is treated.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[
            Reply({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}),
            Reply({"prompt_tokens": "abc", "completion_tokens": 2**63, "total_tokens": -1}),
        ],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 14
    assert result.tokens.messages_with_usage == 1
    assert result.tokens.avg_tokens_per_message == 14.0


async def test_token_metrics_refuse_a_negative_usage_count(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # "Reported input tokens, summed" is not a debit: a negative count is a broken provider payload,
    # and summing it would silently reduce a real total.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[
            Reply({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}),
            Reply({"prompt_tokens": -5, "completion_tokens": 7, "total_tokens": 2}),
        ],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    # The negative prompt count drops out; the second reply's own total (2) is usable and stands.
    assert result.tokens.prompt_tokens == 100
    assert result.tokens.total_tokens == 122


async def test_token_metrics_skip_a_usage_value_that_is_not_a_number(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Same untrusted-payload class, different shape: a provider (or a proxy in front of one) can put
    # a string or an object where a count belongs. Such a value must contribute nothing rather than
    # abort the aggregate — and the message's usable fields must still count.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[Reply({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": "abc"})],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    # `total_tokens` is unusable, so the derived total falls back to prompt + completion.
    assert result.tokens.total_tokens == 10
    assert result.tokens.prompt_tokens == 7
    assert result.tokens.messages_with_usage == 1


async def test_exploit_cost_survives_an_unusable_usage_value_in_the_prefix(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The same JSONB read feeds the exploit-cost prefix, so a poisoned row there breaks the exploit
    # histogram too — a second endpoint down from one bad provider reply.
    group = await persist_evaluation_group(db_session)
    author = await _user(db_session)
    reviewer = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _exploited_at_turn(
        db_session,
        evaluation=evaluation,
        assignment=assignment,
        author=author,
        reviewer=reviewer,
        group=group,
        turn_index=1,
        usages=[{"total_tokens": 3_000_000_000}, {"total_tokens": 5}],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)
    bucket = result.exploits_by_prompt_count[0]

    assert bucket.prompt_count == 2
    assert bucket.avg_tokens_to_exploit == pytest.approx(3_000_000_005.0)


async def test_token_metrics_convert_a_fractional_usage_count(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A fraction is still a JSON number, so it takes the cast path rather than the type guard.
    # Pinned because the read deliberately routes through `numeric`: casting text straight to an
    # integer type rejects "10.5" outright.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, assignment, owner, replies=[Reply({"total_tokens": 10.5})])

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 11
    assert result.tokens.messages_with_usage == 1


async def test_token_metrics_exclude_messages_under_a_soft_deleted_turn(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The turn is the middle link of the live chain and the only one of the three with no test of
    # its own: `Conversation` and `Message` are covered elsewhere, so dropping the turn filter would
    # have left the token sum disagreeing with the message count beside it, unnoticed.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    conversation = await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[Reply({"total_tokens": 90}), Reply({"total_tokens": 10})],
    )
    doomed = (
        await db_session.execute(
            select(Turn).where(col(Turn.conversation_id) == conversation.id, col(Turn.turn_index) == 0)
        )
    ).scalar_one()
    doomed.soft_delete(None)
    await db_session.flush()

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 10
    assert result.tokens.messages_with_usage == 1
    # The invariant the shared helper exists for: spend and the message count drop the same rows.
    assert result.activity.messages == 2


async def test_tokens_by_model_drops_a_soft_deleted_assignment(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The roster is live-only, so an unassigned model must leave the chart — otherwise a removed
    # assignment lingers as a phantom row. Its spend stays in the evaluation total, which is why the
    # rows are documented as able to sum to less.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    kept = await _assign_model(db_session, evaluation)
    removed = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, kept, owner, replies=[Reply({"total_tokens": 7})])
    await _conversation_with_usage(db_session, evaluation, removed, owner, replies=[Reply({"total_tokens": 90})])
    removed.soft_delete(None)
    await db_session.flush()

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert [row.evaluation_ai_model_id for row in result.tokens_by_model] == [kept.id]
    assert result.tokens.total_tokens == 97


async def test_group_tokens_by_model_drops_a_soft_deleted_assignment(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Group level keys on the registry model, so the same rule has to hold one level up: a model
    # whose only assignment was removed must not appear at all.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    kept = await _assign_model(db_session, evaluation)
    removed = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, kept, owner, replies=[Reply({"total_tokens": 7})])
    await _conversation_with_usage(db_session, evaluation, removed, owner, replies=[Reply({"total_tokens": 90})])
    removed.soft_delete(None)
    await db_session.flush()

    result = await evaluation_group_metrics(db_session, group, full_model_access=True)

    assert [row.ai_model_id for row in result.tokens_by_model] == [kept.model_id]


async def test_tokens_by_model_orders_by_spend_then_name(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Both docstrings promise "spend descending, then name" and every other assertion collects the
    # rows into a dict, so the order was structurally unobservable — reversing the sort kept the
    # suite green. The list comparison here is what makes the promise falsifiable.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    big = await _assign_model(db_session, evaluation)
    small = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(db_session, evaluation, small, owner, replies=[Reply({"total_tokens": 5})])
    await _conversation_with_usage(db_session, evaluation, big, owner, replies=[Reply({"total_tokens": 500})])

    result = await evaluation_metrics(db_session, evaluation, group=group)
    group_result = await evaluation_group_metrics(db_session, group, full_model_access=True)

    assert [row.evaluation_ai_model_id for row in result.tokens_by_model] == [big.id, small.id]
    assert [row.ai_model_id for row in group_result.tokens_by_model] == [big.model_id, small.model_id]


async def test_token_metrics_prefer_a_disagreeing_reported_total(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A provider's own total wins when it disagrees with prompt + completion — a reasoning model
    # bills thought tokens in the total only. The existing fixtures all report agreeing values, so
    # inverting the precedence was invisible.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[Reply({"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1500})],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 1500
    assert result.tokens.prompt_tokens == 1000


async def test_token_metrics_fall_back_when_the_reported_total_is_zero(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A reported zero is indistinguishable from an unset total, so preferring it would zero out a
    # message whose prompt/completion counts are right there — the same silent drop the fallback
    # exists to prevent.
    group = await persist_evaluation_group(db_session)
    owner = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)

    await _conversation_with_usage(
        db_session,
        evaluation,
        assignment,
        owner,
        replies=[Reply({"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 0})],
    )

    result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.tokens.total_tokens == 1020


# A fixed "now" so the window's end is deterministic; every seeded submission carries an explicit
# `created_at`, so the database clock never decides which day a row lands on.
TIMELINE_NOW = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
TIMELINE_TODAY = TIMELINE_NOW.date()
GROUP_START = date(2026, 5, 18)
EARLY_DAY = date(2026, 5, 15)  # before the group's declared start date
BUSY_DAY = GROUP_START
QUIET_DAY = date(2026, 5, 19)
LAST_DAY = TIMELINE_TODAY


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 9, 30, tzinfo=UTC)


class TimelineSeed(NamedTuple):
    """A group whose submissions are spread over known days."""

    group: EvaluationGroup
    e1: Evaluation
    e2: Evaluation
    author: User
    other: User
    conversation: Conversation
    scenario: Scenario


@pytest.fixture
async def timeline_group(db_session: AsyncSession, system_roles: dict[str, Role]) -> TimelineSeed:
    """Four submissions: one before the start date, two on the start date, one today.

    E1 carries the first three (one of them confirmed as an exploit by two reviewers, so the
    de-duplication has something to bite on); E2 carries today's, authored by a second user so the
    personal-scope filter has something to hide.
    """
    group = await persist_evaluation_group(db_session, start_date=GROUP_START)
    author = await _user(db_session)
    other = await _user(db_session)
    reviewer_a = await _user(db_session)
    reviewer_b = await _user(db_session)
    for member in (author, other):
        await grant_roles(
            db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles[SystemRole.RED_TEAMER.value]]
        )

    e1 = await _evaluation(db_session, group)
    a1 = await _assign_model(db_session, e1)
    s1 = await _scenario(db_session, e1, 0)
    conv1 = await _conversation_with_messages(db_session, e1, a1, author, scenario=s1, n_messages=2)

    await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s1,
        task=None,
        status=FlagStatus.APPROVED,
        created_at=_at(EARLY_DAY),
    )
    exploited = await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s1,
        task=None,
        status=FlagStatus.APPROVED,
        created_at=_at(BUSY_DAY),
    )
    await _flag(
        db_session,
        author=author,
        conversation=conv1,
        evaluation=e1,
        group=group,
        scenario=s1,
        task=None,
        status=FlagStatus.PENDING,
        created_at=_at(BUSY_DAY),
    )
    # Two reviewers confirm the same exploit — the day must still count it once.
    for reviewer in (reviewer_a, reviewer_b):
        await _review(
            db_session,
            flag=exploited,
            reviewer=reviewer,
            status=ReviewStatus.APPROVED,
            successful=True,
            unique=True,
            valid=True,
        )

    e2 = await _evaluation(db_session, group)
    a2 = await _assign_model(db_session, e2)
    s2 = await _scenario(db_session, e2, 0)
    conv2 = await _conversation_with_messages(db_session, e2, a2, other, n_messages=1)
    await _flag(
        db_session,
        author=other,
        conversation=conv2,
        evaluation=e2,
        group=group,
        scenario=s2,
        task=None,
        status=FlagStatus.PENDING,
        created_at=_at(LAST_DAY),
    )

    return TimelineSeed(group=group, e1=e1, e2=e2, author=author, other=other, conversation=conv1, scenario=s1)


async def test_group_timeline_is_dense_and_starts_at_the_earliest_submission(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    # Dense from the earliest submission (which predates the declared start date) through today.
    assert [point.day for point in result.submissions_by_day] == [
        date(2026, 5, 15),
        date(2026, 5, 16),
        date(2026, 5, 17),
        date(2026, 5, 18),
        date(2026, 5, 19),
        date(2026, 5, 20),
    ]
    assert [(point.submissions, point.exploited_submissions) for point in result.submissions_by_day] == [
        (1, 0),
        (0, 0),
        (0, 0),
        (2, 1),
        (0, 0),
        (1, 0),
    ]


async def test_group_timeline_totals_match_the_headline_submission_count(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    assert sum(point.submissions for point in result.submissions_by_day) == result.submissions.total


async def test_timeline_counts_a_double_confirmed_exploit_once(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    busy = next(point for point in result.submissions_by_day if point.day == BUSY_DAY)
    # Two reviewers confirmed it, so the per-(flag, reviewer) tally is 2 — the timeline stays at 1.
    assert result.reviews.successful_exploit == 2
    assert busy.exploited_submissions == 1


async def test_per_evaluation_timeline_is_sparse_and_sums_to_the_group_series(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    by_evaluation = {evaluation.evaluation_id: evaluation for evaluation in result.evaluations}
    e1_series = by_evaluation[timeline_group.e1.id].submissions_by_active_day
    e2_series = by_evaluation[timeline_group.e2.id].submissions_by_active_day

    # Only days that carry a submission — no zero-fill at this level.
    assert [(point.day, point.submissions, point.exploited_submissions) for point in e1_series] == [
        (EARLY_DAY, 1, 0),
        (BUSY_DAY, 2, 1),
    ]
    assert [(point.day, point.submissions, point.exploited_submissions) for point in e2_series] == [(LAST_DAY, 1, 0)]

    per_day: dict[date, int] = {}
    for series in (e1_series, e2_series):
        for point in series:
            per_day[point.day] = per_day.get(point.day, 0) + point.submissions
    assert per_day == {point.day: point.submissions for point in result.submissions_by_day if point.submissions > 0}


async def test_personal_scope_hides_another_authors_days(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group, viewer_id=timeline_group.author.id)

    assert result.scope == MetricsScope.PERSONAL
    # The other author's submission is zeroed out of the group series...
    assert next(point for point in result.submissions_by_day if point.day == LAST_DAY).submissions == 0
    assert sum(point.submissions for point in result.submissions_by_day) == result.submissions.total == 3
    # ...and out of its evaluation's series entirely.
    by_evaluation = {evaluation.evaluation_id: evaluation for evaluation in result.evaluations}
    assert by_evaluation[timeline_group.e2.id].submissions_by_active_day == []


async def test_timeline_excludes_submissions_on_a_soft_deleted_conversation(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    timeline_group.conversation.soft_delete(None)
    db_session.add(timeline_group.conversation)
    await db_session.flush()

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    # E1's three submissions hung off that conversation; only E2's today remains, and the window
    # now starts at the group's declared start date rather than the vanished early submission.
    assert [(point.day, point.submissions) for point in result.submissions_by_day] == [
        (GROUP_START, 0),
        (QUIET_DAY, 0),
        (LAST_DAY, 1),
    ]
    assert sum(point.submissions for point in result.submissions_by_day) == result.submissions.total


async def test_timeline_of_a_group_without_submissions_is_zero_filled_from_its_start_date(
    db_session: AsyncSession,
) -> None:
    group = await persist_evaluation_group(db_session, start_date=GROUP_START)
    await _evaluation(db_session, group)

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, group)

    assert [(point.day, point.submissions) for point in result.submissions_by_day] == [
        (GROUP_START, 0),
        (QUIET_DAY, 0),
        (LAST_DAY, 0),
    ]


async def test_timeline_of_a_group_without_evaluations_still_spans_its_dates(db_session: AsyncSession) -> None:
    # Same axis as the previous test, deliberately: the window comes from the group's own dates, so
    # "no evaluations at all" and "evaluations with no submissions yet" are indistinguishable to a
    # caller — there is nothing for it to tell apart.
    group = await persist_evaluation_group(db_session, start_date=GROUP_START)

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, group)

    assert [(point.day, point.submissions) for point in result.submissions_by_day] == [
        (GROUP_START, 0),
        (QUIET_DAY, 0),
        (LAST_DAY, 0),
    ]


async def test_timeline_of_a_dateless_draft_without_submissions_is_empty(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, start_date=GROUP_START)
    group.start_date = None  # a partial draft, saved before its dates were chosen
    db_session.add(group)
    await db_session.flush()

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, group)

    assert result.submissions_by_day == []


async def test_group_timeline_sums_two_evaluations_active_on_the_same_day(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    # The group fold must add across evaluations; with each evaluation on its own day, summing and
    # overwriting are indistinguishable.
    a2 = await _assign_model(db_session, timeline_group.e2)
    s2 = await _scenario(db_session, timeline_group.e2, 1)
    conv = await _conversation_with_messages(db_session, timeline_group.e2, a2, timeline_group.other, n_messages=1)
    await _flag(
        db_session,
        author=timeline_group.other,
        conversation=conv,
        evaluation=timeline_group.e2,
        group=timeline_group.group,
        scenario=s2,
        task=None,
        status=FlagStatus.PENDING,
        created_at=_at(BUSY_DAY),
    )

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    busy = next(point for point in result.submissions_by_day if point.day == BUSY_DAY)
    by_evaluation = {evaluation.evaluation_id: evaluation for evaluation in result.evaluations}
    assert busy.submissions == 3
    assert [point.submissions for point in by_evaluation[timeline_group.e1.id].submissions_by_active_day] == [1, 2]
    assert sum(point.submissions for point in result.submissions_by_day) == result.submissions.total


async def test_timeline_excludes_a_soft_deleted_submission(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    deleted = await _flag(
        db_session,
        author=timeline_group.author,
        conversation=timeline_group.conversation,
        evaluation=timeline_group.e1,
        group=timeline_group.group,
        scenario=timeline_group.scenario,
        task=None,
        status=FlagStatus.APPROVED,
        created_at=_at(QUIET_DAY),
    )
    deleted.soft_delete(None)
    db_session.add(deleted)
    await db_session.flush()

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    # The quiet day stays empty, and the series still agrees with the headline count.
    assert next(point for point in result.submissions_by_day if point.day == QUIET_DAY).submissions == 0
    assert sum(point.submissions for point in result.submissions_by_day) == result.submissions.total


async def test_timeline_stops_at_the_groups_end_date(db_session: AsyncSession, system_roles: dict[str, Role]) -> None:
    # Pins the `end_date` half of the window wiring: the unit tests cover the helper, only this
    # reaches the column. Needs a group whose submissions all predate its end date, or the
    # data-extends-the-axis rule would hide a dropped argument.
    group = await persist_evaluation_group(
        db_session,
        start_date=TIMELINE_TODAY - timedelta(days=10),
        end_date=TIMELINE_TODAY - timedelta(days=5),
    )
    author = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)
    scenario = await _scenario(db_session, evaluation, 0)
    conversation = await _conversation_with_messages(db_session, evaluation, assignment, author, n_messages=1)
    await _flag(
        db_session,
        author=author,
        conversation=conversation,
        evaluation=evaluation,
        group=group,
        scenario=scenario,
        task=None,
        status=FlagStatus.PENDING,
        created_at=_at(TIMELINE_TODAY - timedelta(days=8)),
    )

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, group)

    assert result.submissions_by_day[0].day == TIMELINE_TODAY - timedelta(days=10)
    assert result.submissions_by_day[-1].day == TIMELINE_TODAY - timedelta(days=5)


async def test_timeline_buckets_by_utc_day_under_a_non_utc_session(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    # `SET LOCAL` so the change dies with this test's transaction. Kiritimati is UTC+14, so a
    # 23:30Z submission is "tomorrow" locally — a naive truncation would move its bar.
    await db_session.execute(text("SET LOCAL TIME ZONE 'Pacific/Kiritimati'"))
    # Without this the test would pass even if the SET silently had no effect, proving nothing.
    assert (await db_session.execute(text("SHOW TimeZone"))).scalar_one() == "Pacific/Kiritimati"
    await _flag(
        db_session,
        author=timeline_group.author,
        conversation=timeline_group.conversation,
        evaluation=timeline_group.e1,
        group=timeline_group.group,
        scenario=timeline_group.scenario,
        task=None,
        status=FlagStatus.PENDING,
        created_at=datetime(QUIET_DAY.year, QUIET_DAY.month, QUIET_DAY.day, 23, 30, tzinfo=UTC),
    )

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    assert next(point for point in result.submissions_by_day if point.day == QUIET_DAY).submissions == 1
    assert next(point for point in result.submissions_by_day if point.day == LAST_DAY).submissions == 1


async def test_timeline_drops_an_exploit_whose_review_was_withdrawn(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    reviews = (
        (await db_session.execute(Review.live_select().where(col(Review.evaluation_id) == timeline_group.e1.id)))
        .scalars()
        .all()
    )
    assert len(reviews) == 2
    for review in reviews:
        review.soft_delete(None)
        db_session.add(review)
    await db_session.flush()

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_group_metrics(db_session, timeline_group.group)

    # Unassigning every reviewer soft-deletes their rows; the day keeps its submissions and loses
    # the exploit.
    busy = next(point for point in result.submissions_by_day if point.day == BUSY_DAY)
    assert (busy.submissions, busy.exploited_submissions) == (2, 0)


async def test_evaluation_timeline_spans_the_group_span_widened_by_its_own_data(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    # The axis is the group's *declared* span widened by this evaluation's own submissions — not by
    # the group's. E1 owns EARLY_DAY, which predates the declared start, so its axis opens there.
    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_metrics(db_session, timeline_group.e1, group=timeline_group.group)

    assert [(point.day, point.submissions, point.exploited_submissions) for point in result.submissions_by_day] == [
        (EARLY_DAY, 1, 0),
        (date(2026, 5, 16), 0, 0),
        (date(2026, 5, 17), 0, 0),
        (BUSY_DAY, 2, 1),
        (QUIET_DAY, 0, 0),
        (LAST_DAY, 0, 0),
    ]
    assert sum(point.submissions for point in result.submissions_by_day) == result.submissions.total
    # The bars and the headline exploit count come from different queries with different predicates,
    # so pin that they reconcile.
    assert sum(point.exploited_submissions for point in result.submissions_by_day) == result.exploited_submissions == 1


async def test_evaluation_timeline_is_not_widened_by_a_sibling_evaluations_days(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    # E2's only submission is today, so its axis opens at the group's declared start — it does NOT
    # stretch back to EARLY_DAY just because sibling E1 was active then. The group dashboard's axis
    # does, because it folds every evaluation's days; the two are deliberately not the same span.
    with time_machine.travel(TIMELINE_NOW, tick=False):
        group_result = await evaluation_group_metrics(db_session, timeline_group.group)
        result = await evaluation_metrics(db_session, timeline_group.e2, group=timeline_group.group)

    assert [point.day for point in result.submissions_by_day] == [GROUP_START, QUIET_DAY, LAST_DAY]
    assert group_result.submissions_by_day[0].day == EARLY_DAY
    assert sum(point.submissions for point in result.submissions_by_day) == result.submissions.total


async def test_evaluation_timeline_stops_at_the_groups_end_date(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The `end_date` half of the window wiring, on the *evaluation* call site — the group call site
    # has its own twin.
    group = await persist_evaluation_group(
        db_session,
        start_date=TIMELINE_TODAY - timedelta(days=10),
        end_date=TIMELINE_TODAY - timedelta(days=5),
    )
    author = await _user(db_session)
    evaluation = await _evaluation(db_session, group)
    assignment = await _assign_model(db_session, evaluation)
    scenario = await _scenario(db_session, evaluation, 0)
    conversation = await _conversation_with_messages(db_session, evaluation, assignment, author, n_messages=1)
    await _flag(
        db_session,
        author=author,
        conversation=conversation,
        evaluation=evaluation,
        group=group,
        scenario=scenario,
        task=None,
        status=FlagStatus.PENDING,
        created_at=_at(TIMELINE_TODAY - timedelta(days=8)),
    )

    with time_machine.travel(TIMELINE_NOW, tick=False):
        result = await evaluation_metrics(db_session, evaluation, group=group)

    assert result.submissions_by_day[0].day == TIMELINE_TODAY - timedelta(days=10)
    assert result.submissions_by_day[-1].day == TIMELINE_TODAY - timedelta(days=5)


async def test_evaluation_timeline_narrows_under_personal_scope(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    with time_machine.travel(TIMELINE_NOW, tick=False):
        full = await evaluation_metrics(db_session, timeline_group.e1, group=timeline_group.group)
        personal = await evaluation_metrics(
            db_session, timeline_group.e1, group=timeline_group.group, viewer_id=timeline_group.other.id
        )

    assert personal.scope == MetricsScope.PERSONAL
    # Read against the full scope, not against zero: an all-zero assertion on its own would also
    # hold if the series were broken and never populated at all.
    assert next(point.submissions for point in full.submissions_by_day if point.day == BUSY_DAY) == 2
    assert next(point.submissions for point in personal.submissions_by_day if point.day == BUSY_DAY) == 0
    # `other` authored nothing on E1, so every day is zero — but the axis still opens at the group's
    # declared start, because that half comes from the group's dates, not from the caller's data.
    assert personal.submissions.total == 0
    assert {point.submissions for point in personal.submissions_by_day} == {0}
    assert personal.submissions_by_day[0].day == GROUP_START


async def test_evaluation_metrics_rejects_a_group_that_is_not_the_parent(
    db_session: AsyncSession, timeline_group: TimelineSeed
) -> None:
    # The group is what supplies the declared span, so passing the wrong one would silently draw the
    # axis of a different event. The service refuses instead — and nothing else in this file exercises
    # that raise, so without this case the guard is uncovered under branch coverage.
    stranger = await persist_evaluation_group(db_session, title="Not the parent")

    with pytest.raises(ValueError, match="is not the parent of evaluation"):
        await evaluation_metrics(db_session, timeline_group.e1, group=stranger)
