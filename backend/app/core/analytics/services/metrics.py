"""Aggregate metrics for a whole event (`EvaluationGroup`) — pure async functions.

`evaluation_group_metrics` computes the group roll-up plus the per-evaluation /
per-scenario / per-task breakdown from the existing tables in a fixed number of
grouped queries (independent of group size — no N+1). Every count is scoped to
the group and filtered to live rows; the caller-authorization is the route's job.

Both entry points take an optional ``viewer_id``: when set (a `personal`-scope
read — a `members_personal_metrics` group seen by a `view_personal_metrics`
member), every contribution breakdown is filtered to that user's own submissions,
conversations, and the reviews of their submissions, while structural counts (the
member roster, scenario/task shape, model-assignment counts) stay group-wide. When
``None`` the numbers are the full event-wide aggregate.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import BigInteger
from sqlalchemy import ColumnElement
from sqlalchemy import Date
from sqlalchemy import Numeric
from sqlalchemy import Select
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.models import AiModel
from app.core.analytics.schemas import ActivityMetrics
from app.core.analytics.schemas import DailySubmissionPoint
from app.core.analytics.schemas import EvaluationGroupMetricsResponse
from app.core.analytics.schemas import EvaluationMetrics
from app.core.analytics.schemas import EvaluationMetricsResponse
from app.core.analytics.schemas import ExploitPromptCountBucket
from app.core.analytics.schemas import ExploitsByModelMetrics
from app.core.analytics.schemas import GroupTokensByModelMetrics
from app.core.analytics.schemas import MemberMetrics
from app.core.analytics.schemas import ReviewMetrics
from app.core.analytics.schemas import ScenarioMetrics
from app.core.analytics.schemas import SubmissionMetrics
from app.core.analytics.schemas import TaskMetrics
from app.core.analytics.schemas import TokenMetrics
from app.core.analytics.schemas import TokensByModelMetrics
from app.core.annotations.enums import FlagStatus
from app.core.annotations.models import FlaggedMessage
from app.core.annotations.models import MessageFlag
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.models import ObjectRoleAssignment
from app.core.auth.object_roles.registry import ObjectType
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.enums import MetricsScope
from app.core.evaluations.models import EVALUATION_DEFAULT_ORDER
from app.core.evaluations.models import TASK_DEFAULT_ORDER
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.models import Review

_COMPLETED_REVIEW_STATUSES = (ReviewStatus.APPROVED, ReviewStatus.REJECTED)

# How far back a group's declared start date may open the timeline before its first submission.
# It bounds only that leading context: an older submission still pulls the axis back to itself.
_TIMELINE_CONTEXT_DAYS = 365

_ZERO_REVIEWS = ReviewMetrics(
    total=0, completed=0, pending=0, successful_exploit=0, unique_exploit=0, valid_submission=0
)

_ZERO_TOKENS = TokenMetrics(
    prompt_tokens=0,
    completion_tokens=0,
    total_tokens=0,
    messages_with_usage=0,
    conversations_with_usage=0,
    avg_tokens_per_message=0.0,
    avg_tokens_per_conversation=0.0,
)


def _submissions(by_status: dict[FlagStatus, int]) -> SubmissionMetrics:
    return SubmissionMetrics(
        total=sum(by_status.values()),
        pending=by_status.get(FlagStatus.PENDING, 0),
        approved=by_status.get(FlagStatus.APPROVED, 0),
        rejected=by_status.get(FlagStatus.REJECTED, 0),
    )


async def _members(session: AsyncSession, group_id: UUID) -> MemberMetrics:
    # One round-trip: fetch each (user, role) membership held by a live user — live
    # assignments only, mirroring the member-list scope (`list_members` joins live users)
    # so the metric matches the roster — then aggregate in Python. `total` is the distinct
    # users; `by_role` counts distinct users *per role*, so a member with several roles
    # inflates `sum(by_role) >= total` — intended (see MemberMetrics). Always group-wide,
    # even on a personal-scope read: a roster headcount is structural context.
    rows = (
        await session.execute(
            select(col(ObjectRoleAssignment.user_id), col(Role.name))
            .join(Role, col(ObjectRoleAssignment.role_id) == col(Role.id))
            .join(User, col(ObjectRoleAssignment.user_id) == col(User.id))
            .where(
                col(ObjectRoleAssignment.object_type) == ObjectType.EVALUATION_GROUP,
                col(ObjectRoleAssignment.object_id) == group_id,
                col(ObjectRoleAssignment.deleted_at).is_(None),
                col(User.deleted_at).is_(None),
            )
        )
    ).all()
    users_by_role: dict[str, set[UUID]] = defaultdict(set)
    all_users: set[UUID] = set()
    for user_id, role_name in rows:
        users_by_role[role_name].add(user_id)
        all_users.add(user_id)
    return MemberMetrics(
        total=len(all_users),
        by_role={role_name: len(users) for role_name, users in users_by_role.items()},
    )


async def _submission_breakdown(
    session: AsyncSession, eval_ids: list[UUID], *, viewer_id: UUID | None = None
) -> tuple[dict[FlagStatus, int], dict[UUID, dict[FlagStatus, int]], dict[UUID, int], dict[UUID, int]]:
    """Grouped submission (`MessageFlag`) counts folded into every breakdown level.

    Scoped to flags with a *live* parent — a live evaluation of this group + a live
    conversation — mirroring the flags read-path. Without the live-conversation join
    and the live-evaluation scope the group roll-up would count submissions on a
    soft-deleted evaluation/conversation that the per-evaluation rows (which iterate
    live evaluations only) and the flags endpoint both hide, so the headline number
    would silently disagree with itself and the rest of the app. ``viewer_id``, when
    given, narrows to submissions that user authored (personal scope).

    Returns:
        (group_status, per_eval_status, per_scenario_total, per_task_total).
    """
    group_submission_status: dict[FlagStatus, int] = defaultdict(int)
    eval_submission_status: dict[UUID, dict[FlagStatus, int]] = defaultdict(lambda: defaultdict(int))
    scenario_submission_total: dict[UUID, int] = defaultdict(int)
    task_submission_total: dict[UUID, int] = defaultdict(int)
    if not eval_ids:
        return group_submission_status, eval_submission_status, scenario_submission_total, task_submission_total

    statement = (
        select(
            col(MessageFlag.evaluation_id),
            col(MessageFlag.scenario_id),
            col(MessageFlag.task_id),
            col(MessageFlag.status),
            func.count(),
        )
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .where(
            col(MessageFlag.evaluation_id).in_(eval_ids),
            col(MessageFlag.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
        )
        .group_by(
            col(MessageFlag.evaluation_id),
            col(MessageFlag.scenario_id),
            col(MessageFlag.task_id),
            col(MessageFlag.status),
        )
    )
    if viewer_id is not None:
        statement = statement.where(col(MessageFlag.created_by_id) == viewer_id)
    submission_rows = (await session.execute(statement)).all()
    for evaluation_id, scenario_id, task_id, flag_status, count in submission_rows:
        group_submission_status[flag_status] += count
        eval_submission_status[evaluation_id][flag_status] += count
        if scenario_id is not None:
            scenario_submission_total[scenario_id] += count
        if task_id is not None:
            task_submission_total[task_id] += count
    return group_submission_status, eval_submission_status, scenario_submission_total, task_submission_total


async def _submissions_by_day(
    session: AsyncSession, eval_ids: list[UUID], *, viewer_id: UUID | None = None
) -> dict[UUID, dict[date, tuple[int, int]]]:
    """Per-evaluation daily submission counts, each paired with the exploits confirmed among them.

    Buckets by UTC calendar day: `created_at` is a `timestamptz`, and truncating one resolves
    against the session time zone, which no part of this stack pins — so the column is converted
    explicitly. Scoped like `_submission_breakdown` (live flag, live conversation, live evaluation),
    so a day can never total more than the headline submission count. The exploit tally counts the
    *submission*, not its reviews, so several reviewers confirming one flag still move the day by
    one. ``viewer_id``, when given, narrows to submissions that user authored (personal scope).

    Returns:
        Days keyed by evaluation, each day mapped to `(submissions, successful_exploits)`.
    """
    if not eval_ids:
        return {}

    day = cast(func.timezone("UTC", col(MessageFlag.created_at)), Date)
    statement = (
        select(
            col(MessageFlag.evaluation_id),
            day,
            func.count(),
            func.count().filter(_has_successful_exploit_review(eval_ids)),
        )
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .where(
            col(MessageFlag.evaluation_id).in_(eval_ids),
            col(MessageFlag.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
        )
        .group_by(col(MessageFlag.evaluation_id), day)
    )
    if viewer_id is not None:
        statement = statement.where(col(MessageFlag.created_by_id) == viewer_id)

    by_evaluation: dict[UUID, dict[date, tuple[int, int]]] = defaultdict(dict)
    for evaluation_id, bucket, submissions, exploits in (await session.execute(statement)).all():
        by_evaluation[evaluation_id][bucket] = (submissions, exploits)
    return by_evaluation


def _timeline_window(start_date: date | None, end_date: date | None, observed: set[date], *, today: date) -> list[date]:
    """The dense date axis for a group's timeline, oldest first.

    Every observed submission day falls inside the window, so no submission is left off the series:
    the group's declared dates add empty context around the data but never clip it. The trailing edge
    is `today`, or the end date once it has passed — stretched forward to the newest observed day when
    that is later, so a submission after the declared end stretches the axis rather than falling off it.
    A declared start earlier than `_TIMELINE_CONTEXT_DAYS` before *that* edge is pulled forward to the
    bound — so the empty lead-in is capped at a year, not eliminated: a group declared two years ago
    whose data is recent still opens on a year of zeros.

    Empty when there is nothing to plot: no start date and no submission, or a window that has not
    opened yet because the group's start date is still in the future.
    """
    context_end = min(end_date, today) if end_date else today
    # Anchor the clamp to the axis's own end: measuring from `context_end` clamps against an edge the
    # axis may not have, leaving the leading edge where an ancient `end_date` put it.
    end = max(context_end, max(observed, default=context_end))
    # Capping at the distance to `date.min` keeps a hand-dated year-1 draft from overflowing.
    lead_in = min(_TIMELINE_CONTEXT_DAYS, (end - date.min).days)
    context_start = max(start_date, end - timedelta(days=lead_in)) if start_date else None

    earliest_observed = min(observed, default=None)
    starts = [candidate for candidate in (context_start, earliest_observed) if candidate is not None]
    if not starts:
        return []

    start = min(starts)
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _daily_series(counts: dict[date, tuple[int, int]], window: list[date]) -> list[DailySubmissionPoint]:
    """Zero-filled points across ``window``.

    Days outside the window are dropped, so the window has to be derived from the same counts.
    """
    return [
        DailySubmissionPoint(
            day=bucket,
            submissions=counts.get(bucket, (0, 0))[0],
            exploited_submissions=counts.get(bucket, (0, 0))[1],
        )
        for bucket in window
    ]


def _evaluation_series(counts: dict[date, tuple[int, int]]) -> list[DailySubmissionPoint]:
    """The per-evaluation form: only the days that carry a submission, oldest first."""
    return _daily_series(counts, sorted(counts))


async def _scenario_structure(session: AsyncSession, eval_ids: list[UUID]) -> tuple[list[Scenario], list[Task]]:
    """Live scenarios (position order) and their live tasks for the given evaluations."""
    if not eval_ids:
        return [], []
    scenarios = list(
        (
            await session.execute(
                Scenario.live_select()
                .where(col(Scenario.evaluation_id).in_(eval_ids))
                .order_by(col(Scenario.evaluation_id), col(Scenario.position))
            )
        )
        .scalars()
        .all()
    )
    scenario_ids = [scenario.id for scenario in scenarios]
    if not scenario_ids:
        return scenarios, []
    tasks = list(
        (
            await session.execute(
                Task.live_select()
                .where(col(Task.scenario_id).in_(scenario_ids))
                .order_by(col(Task.scenario_id), *TASK_DEFAULT_ORDER)
            )
        )
        .scalars()
        .all()
    )
    return scenarios, tasks


async def _review_breakdown(
    session: AsyncSession, eval_ids: list[UUID], *, viewer_id: UUID | None = None
) -> dict[UUID, ReviewMetrics]:
    """Per-evaluation review tallies, scoped to reviews with a *live* parent flag + conversation.

    Soft-deleting a flag (or its conversation, e.g. a model-unassign cascade) doesn't cascade to
    the flag's reviews, so without this live-parent join a review on a dead flag/conversation would
    stay counted forever and diverge from every read path. ``viewer_id``, when given, narrows to
    reviews *of that user's own submissions* (the flag author is `viewer_id`) — the personal-scope
    "how are my submissions scoring" slice, matching the red-teamer's own `reviews:read` scope.
    """
    if not eval_ids:
        return {}
    statement = (
        select(
            col(Review.evaluation_id),
            func.count(),
            func.count().filter(col(Review.status).in_(_COMPLETED_REVIEW_STATUSES)),
            func.count().filter(col(Review.successful_exploit).is_(True)),
            func.count().filter(col(Review.unique_exploit).is_(True)),
            func.count().filter(col(Review.valid_submission).is_(True)),
        )
        .join(MessageFlag, col(Review.message_flag_id) == col(MessageFlag.id))
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .where(
            col(Review.evaluation_id).in_(eval_ids),
            col(Review.deleted_at).is_(None),
            col(MessageFlag.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
        )
        .group_by(col(Review.evaluation_id))
    )
    if viewer_id is not None:
        statement = statement.where(col(MessageFlag.created_by_id) == viewer_id)
    review_rows = (await session.execute(statement)).all()
    return {
        evaluation_id: ReviewMetrics(
            total=total,
            completed=completed,
            pending=total - completed,
            successful_exploit=successful,
            unique_exploit=unique,
            valid_submission=valid,
        )
        for evaluation_id, total, completed, successful, unique, valid in review_rows
    }


async def _activity_breakdown(
    session: AsyncSession, eval_ids: list[UUID], *, viewer_id: UUID | None = None
) -> tuple[dict[UUID, int], dict[UUID, int]]:
    """Per-evaluation (conversations, messages) counts over live rows.

    ``viewer_id``, when given, narrows to that user's own conversations (personal scope) —
    both the conversation count and the messages within them.
    """
    if not eval_ids:
        return {}, {}
    conversation_statement = (
        select(col(Conversation.evaluation_id), func.count())
        .where(col(Conversation.evaluation_id).in_(eval_ids), col(Conversation.deleted_at).is_(None))
        .group_by(col(Conversation.evaluation_id))
    )
    message_statement = (
        select(col(Conversation.evaluation_id), func.count(col(Message.id)))
        .select_from(Message)
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .join(Conversation, col(Turn.conversation_id) == col(Conversation.id))
        .where(
            col(Conversation.evaluation_id).in_(eval_ids),
            col(Message.deleted_at).is_(None),
            col(Turn.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
        )
        .group_by(col(Conversation.evaluation_id))
    )
    if viewer_id is not None:
        conversation_statement = conversation_statement.where(col(Conversation.user_id) == viewer_id)
        message_statement = message_statement.where(col(Conversation.user_id) == viewer_id)
    conversation_rows = (await session.execute(conversation_statement)).all()
    eval_conversations = {row[0]: row[1] for row in conversation_rows}
    message_rows = (await session.execute(message_statement)).all()
    eval_messages = {row[0]: row[1] for row in message_rows}
    return eval_conversations, eval_messages


def _usage_int(field: str) -> ColumnElement[int]:
    """One numeric field of a message's `extra["usage"]`, NULL unless it holds a usable count.

    `extra` is persisted verbatim from the provider response and `Usage` bounds nothing, so the
    value is untrusted: absent, not a number, negative, or past any integer width. Postgres *raises*
    on a cast it cannot make, which would take every metrics read for that message's group down
    permanently — `extra` is not editable through any API, so the only repair is manual SQL.

    So the type is checked before the cast and the range before the narrowing one: a non-number and
    anything outside `[0, 2**63 - 1]` read as NULL, which the aggregates skip. A negative is refused
    rather than summed — reported input tokens are not a debit. The range test sits in a nested
    `case` because Postgres does not guarantee `AND` operand order, so folding it into the outer
    condition would leave the `numeric` cast reachable for a JSON string. A **fractional** value is
    the one thing that converts instead of nulling: `numeric → bigint` rounds half away from zero.

    The guard belongs here rather than at the write edge: rejecting a bogus count while persisting
    the reply would fail the red-teamer's chat over an accounting field.
    """
    value = col(Message.extra)["usage"][field]
    numeric_value = cast(value.as_string(), Numeric)
    return case(
        (
            func.jsonb_typeof(value) == "number",
            case((numeric_value.between(0, 2**63 - 1), cast(numeric_value, BigInteger)), else_=None),
        ),
        else_=None,
    )


def _has_usage() -> ColumnElement[bool]:
    """Whether the message reported at least one usage value the guard accepts.

    The denominator both averages divide by. Key presence alone is not enough: a message whose
    every usage value is unusable would sit in the denominator with nothing in the numerator,
    understating the average instead of reading as unmeasured — the opposite of how a usage-free
    prefix is treated in `_exploit_prompt_counts`. Since `_usage_int` nulls an out-of-range or
    non-numeric value, presence has to be asked of the accepted values, not of the key.
    """
    return or_(
        _usage_int("prompt_tokens").is_not(None),
        _usage_int("completion_tokens").is_not(None),
        _usage_int("total_tokens").is_not(None),
    )


def _message_total_tokens() -> ColumnElement[int]:
    """A message's cost-bearing total: the reported total, else prompt + completion.

    The litellm adapter maps the three fields independently, so a provider may report any
    subset. Summing the stored `total_tokens` alone would silently drop a message that only
    reported one side.

    A reported zero falls through to the derived sum as well: it is indistinguishable from an
    unset total, and preferring it would zero out a message whose prompt/completion counts are
    right there — the same silent drop the fallback exists to prevent.
    """
    return func.coalesce(
        func.nullif(_usage_int("total_tokens"), 0),
        func.coalesce(_usage_int("prompt_tokens"), 0) + func.coalesce(_usage_int("completion_tokens"), 0),
    )


class TokenAggregates(NamedTuple):
    """The five `_token_aggregates` columns, in select order."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    messages_with_usage: int
    conversations_with_usage: int


class LiveAssignment(NamedTuple):
    """One live model assignment of an evaluation, as `_live_assignments` selects it."""

    assignment_id: UUID
    model_alias: str
    model_display_mask: str | None


def _token_aggregates() -> tuple[ColumnElement[Any], ...]:
    """The five aggregate columns every token grouping selects, in `_token_metrics` order.

    Each is filtered to usage-bearing messages so a usage-free reply contributes by
    construction rather than by arithmetic accident — and so the two counts are the honest
    denominators the averages divide by.
    """
    return (
        func.coalesce(func.sum(_usage_int("prompt_tokens")).filter(_has_usage()), 0),
        func.coalesce(func.sum(_usage_int("completion_tokens")).filter(_has_usage()), 0),
        func.coalesce(func.sum(_message_total_tokens()).filter(_has_usage()), 0),
        func.count().filter(_has_usage()),
        func.count(func.distinct(col(Conversation.id))).filter(_has_usage()),
    )


def _live_message_tokens(*keys: Any) -> Select[Any]:
    """Token aggregates keyed by ``keys`` over live message → turn → conversation rows.

    Deliberately the same join and liveness filters as `_activity_breakdown`, so a token sum can
    never disagree with the message count rendered beside it: a message under a soft-deleted turn
    or conversation drops out of both. Callers add their own evaluation scope and viewer filter.
    """
    return (
        select(*keys, *_token_aggregates())
        .select_from(Message)
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .join(Conversation, col(Turn.conversation_id) == col(Conversation.id))
        .where(
            col(Message.deleted_at).is_(None),
            col(Turn.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
        )
        .group_by(*keys)
    )


def _token_metrics(row: Sequence[Any]) -> TokenMetrics:
    """Build `TokenMetrics` from a `_token_aggregates` row, dividing by the usage-bearing counts."""
    aggregates = TokenAggregates(*row)
    messages, conversations = aggregates.messages_with_usage, aggregates.conversations_with_usage
    return TokenMetrics(
        prompt_tokens=aggregates.prompt_tokens,
        completion_tokens=aggregates.completion_tokens,
        total_tokens=aggregates.total_tokens,
        messages_with_usage=messages,
        conversations_with_usage=conversations,
        avg_tokens_per_message=(aggregates.total_tokens / messages) if messages else 0.0,
        avg_tokens_per_conversation=(aggregates.total_tokens / conversations) if conversations else 0.0,
    )


async def _token_breakdown(
    session: AsyncSession, eval_ids: list[UUID], *, viewer_id: UUID | None = None
) -> tuple[dict[UUID, TokenMetrics], dict[UUID, TokenMetrics]]:
    """Per-evaluation and per-scenario token spend — two grouped queries for the whole group.

    ``viewer_id``, when given, narrows to that user's own conversations: token spend is a
    contribution metric, so a personal-scope read must not surface the event's whole cost.
    Conversations with no scenario are excluded from the per-scenario map (nothing links them to
    one), which is why the scenario rows can sum to less than their evaluation.
    """
    if not eval_ids:
        return {}, {}
    evaluation_statement = _live_message_tokens(col(Conversation.evaluation_id)).where(
        col(Conversation.evaluation_id).in_(eval_ids)
    )
    scenario_statement = _live_message_tokens(col(Conversation.scenario_id)).where(
        col(Conversation.evaluation_id).in_(eval_ids), col(Conversation.scenario_id).is_not(None)
    )
    if viewer_id is not None:
        evaluation_statement = evaluation_statement.where(col(Conversation.user_id) == viewer_id)
        scenario_statement = scenario_statement.where(col(Conversation.user_id) == viewer_id)
    eval_rows = (await session.execute(evaluation_statement)).all()
    scenario_rows = (await session.execute(scenario_statement)).all()
    return (
        {row[0]: _token_metrics(row[1:]) for row in eval_rows},
        {row[0]: _token_metrics(row[1:]) for row in scenario_rows},
    )


async def _group_tokens_by_model(
    session: AsyncSession, eval_ids: list[UUID], *, viewer_id: UUID | None = None
) -> list[GroupTokensByModelMetrics]:
    """Group-wide token spend per registry model — two grouped queries, zero-inclusive.

    Keyed on `AiModel` rather than on the assignment, so a model assigned to several of the group's
    evaluations is one row summing all of them: the group dashboard asks what a model cost across
    the event, not what one assignment cost. Every model assigned to a live evaluation appears,
    including unused ones, ordered by spend then alias for a stable tie-break. ``viewer_id`` narrows
    the spend to that user's own conversations (contribution metric) while the roster stays whole.

    Callers must gate this on the masking rule (`evaluation_group_metrics`): the row itself is the
    cross-evaluation model correlation that per-evaluation masking withholds, so it is served
    unmasked or not at all.
    """
    if not eval_ids:
        return []
    assignments = (
        await session.execute(
            select(col(EvaluationAiModel.model_id), col(AiModel.model_alias))
            .join(AiModel, col(EvaluationAiModel.model_id) == col(AiModel.id))
            .where(
                col(EvaluationAiModel.evaluation_id).in_(eval_ids),
                col(EvaluationAiModel.deleted_at).is_(None),
            )
            .distinct()
        )
    ).all()
    token_statement = (
        _live_message_tokens(col(EvaluationAiModel.model_id))
        .join(EvaluationAiModel, col(Conversation.evaluation_ai_model_id) == col(EvaluationAiModel.id))
        .where(col(Conversation.evaluation_id).in_(eval_ids))
    )
    if viewer_id is not None:
        token_statement = token_statement.where(col(Conversation.user_id) == viewer_id)
    tokens_by_model = {row[0]: _token_metrics(row[1:]) for row in (await session.execute(token_statement)).all()}
    metrics = [
        GroupTokensByModelMetrics(
            ai_model_id=model_id,
            model_alias=model_alias,
            tokens=tokens_by_model.get(model_id, _ZERO_TOKENS),
        )
        for model_id, model_alias in assignments
    ]
    # The id closes the sort, so equal spend and equal alias cannot reshuffle between two reads.
    metrics.sort(key=lambda m: (-m.tokens.total_tokens, m.model_alias, m.ai_model_id))
    return metrics


async def _models_breakdown(session: AsyncSession, eval_ids: list[UUID]) -> dict[UUID, int]:
    """Per-evaluation live model-assignment counts."""
    if not eval_ids:
        return {}
    model_rows = (
        await session.execute(
            select(col(EvaluationAiModel.evaluation_id), func.count())
            .where(col(EvaluationAiModel.evaluation_id).in_(eval_ids), col(EvaluationAiModel.deleted_at).is_(None))
            .group_by(col(EvaluationAiModel.evaluation_id))
        )
    ).all()
    return {row[0]: row[1] for row in model_rows}


def _scenario_metrics_for(
    evaluation_id: UUID,
    scenarios_by_evaluation: dict[UUID, list[Scenario]],
    tasks_by_scenario: dict[UUID, list[Task]],
    scenario_submission_total: dict[UUID, int],
    task_submission_total: dict[UUID, int],
    scenario_tokens: dict[UUID, TokenMetrics],
) -> list[ScenarioMetrics]:
    """Assemble one evaluation's by-scenario / by-task rows (zero-inclusive)."""
    return [
        ScenarioMetrics(
            scenario_id=scenario.id,
            name=scenario.name,
            submissions_total=scenario_submission_total.get(scenario.id, 0),
            tokens=scenario_tokens.get(scenario.id, _ZERO_TOKENS),
            tasks=[
                TaskMetrics(
                    task_id=task.id,
                    name=task.name,
                    submissions_total=task_submission_total.get(task.id, 0),
                )
                for task in tasks_by_scenario.get(scenario.id, [])
            ],
        )
        for scenario in scenarios_by_evaluation.get(evaluation_id, [])
    ]


async def evaluation_group_metrics(
    session: AsyncSession, group: EvaluationGroup, *, viewer_id: UUID | None = None, full_model_access: bool = False
) -> EvaluationGroupMetricsResponse:
    """Compute the whole-event aggregate for ``group``.

    Message counts include every live message (user, assistant, system) — the
    engagement signal, not just model output. Submissions/reviews are counted from
    the denormalised parent columns on `MessageFlag` / `Review`, so the breakdown
    never walks the conversation chain. ``viewer_id`` set narrows every contribution
    breakdown to that user's own data (personal scope); the member roster and
    scenario/model structure stay group-wide either way.

    ``full_model_access`` is the caller's `evaluation_groups:view_metrics` holding, and it gates the
    per-model spend roll-up alone — everything else here is model-anonymous. It defaults to the
    *restrictive* value on purpose: forgetting to thread it through withholds attribution rather
    than correlating a masked evaluation's models across the group.
    """
    scope = MetricsScope.PERSONAL if viewer_id is not None else MetricsScope.FULL
    evaluations = (
        (
            await session.execute(
                Evaluation.live_select()
                .where(col(Evaluation.evaluation_group_id) == group.id)
                .order_by(*EVALUATION_DEFAULT_ORDER)
            )
        )
        .scalars()
        .all()
    )
    eval_ids = [evaluation.id for evaluation in evaluations]

    scenarios, tasks = await _scenario_structure(session, eval_ids)
    members = await _members(session, group.id)

    # 3. Submissions — one grouped pass, folded into every breakdown.
    (
        group_submission_status,
        eval_submission_status,
        scenario_submission_total,
        task_submission_total,
    ) = await _submission_breakdown(session, eval_ids, viewer_id=viewer_id)

    eval_reviews = await _review_breakdown(session, eval_ids, viewer_id=viewer_id)
    eval_conversations, eval_messages = await _activity_breakdown(session, eval_ids, viewer_id=viewer_id)
    eval_tokens, scenario_tokens = await _token_breakdown(session, eval_ids, viewer_id=viewer_id)
    eval_models = await _models_breakdown(session, eval_ids)

    # Fold the per-evaluation days into one group-wide series. Summing is exact because a flag
    # carries exactly one `evaluation_id`, so no submission is counted under two evaluations.
    eval_daily = await _submissions_by_day(session, eval_ids, viewer_id=viewer_id)
    group_daily: dict[date, tuple[int, int]] = {}
    for per_day in eval_daily.values():
        for bucket, (submissions, exploits) in per_day.items():
            running_submissions, running_exploits = group_daily.get(bucket, (0, 0))
            group_daily[bucket] = (running_submissions + submissions, running_exploits + exploits)
    window = _timeline_window(group.start_date, group.end_date, set(group_daily), today=datetime.now(UTC).date())

    # A row correlates one model's cost across evaluations, which is exactly what masking withholds.
    group_masks_models = any(evaluation.mask_models_enabled for evaluation in evaluations)
    tokens_by_model_withheld = group_masks_models and not full_model_access
    tokens_by_model = (
        [] if tokens_by_model_withheld else await _group_tokens_by_model(session, eval_ids, viewer_id=viewer_id)
    )

    # Assemble — evaluations in order, scenarios (position order) with their tasks, zero-filled.
    tasks_by_scenario: dict[UUID, list[Task]] = defaultdict(list)
    for task in tasks:
        tasks_by_scenario[task.scenario_id].append(task)
    scenarios_by_evaluation: dict[UUID, list[Scenario]] = defaultdict(list)
    for scenario in scenarios:
        scenarios_by_evaluation[scenario.evaluation_id].append(scenario)

    evaluation_metrics = [
        EvaluationMetrics(
            evaluation_id=evaluation.id,
            title=evaluation.title,
            models_assigned=eval_models.get(evaluation.id, 0),
            submissions=_submissions(eval_submission_status.get(evaluation.id, {})),
            reviews=eval_reviews.get(evaluation.id, _ZERO_REVIEWS),
            activity=ActivityMetrics(
                conversations=eval_conversations.get(evaluation.id, 0),
                messages=eval_messages.get(evaluation.id, 0),
            ),
            tokens=eval_tokens.get(evaluation.id, _ZERO_TOKENS),
            scenarios=_scenario_metrics_for(
                evaluation.id,
                scenarios_by_evaluation,
                tasks_by_scenario,
                scenario_submission_total,
                task_submission_total,
                scenario_tokens,
            ),
            submissions_by_active_day=_evaluation_series(eval_daily.get(evaluation.id, {})),
        )
        for evaluation in evaluations
    ]

    return EvaluationGroupMetricsResponse(
        scope=scope,
        group_id=group.id,
        members=members,
        submissions=_submissions(group_submission_status),
        reviews=ReviewMetrics(
            total=sum(review.total for review in eval_reviews.values()),
            completed=sum(review.completed for review in eval_reviews.values()),
            pending=sum(review.pending for review in eval_reviews.values()),
            successful_exploit=sum(review.successful_exploit for review in eval_reviews.values()),
            unique_exploit=sum(review.unique_exploit for review in eval_reviews.values()),
            valid_submission=sum(review.valid_submission for review in eval_reviews.values()),
        ),
        activity=ActivityMetrics(
            conversations=sum(eval_conversations.values()),
            messages=sum(eval_messages.values()),
        ),
        tokens=_token_metrics(
            (
                sum(t.prompt_tokens for t in eval_tokens.values()),
                sum(t.completion_tokens for t in eval_tokens.values()),
                sum(t.total_tokens for t in eval_tokens.values()),
                sum(t.messages_with_usage for t in eval_tokens.values()),
                sum(t.conversations_with_usage for t in eval_tokens.values()),
            )
        ),
        tokens_by_model=tokens_by_model,
        tokens_by_model_withheld=tokens_by_model_withheld,
        evaluations=evaluation_metrics,
        submissions_by_day=_daily_series(group_daily, window),
    )


def _has_successful_exploit_review(eval_ids: list[UUID] | None = None) -> ColumnElement[bool]:
    """Correlated EXISTS: the flag carries at least one live successful-exploit review.

    ``eval_ids`` narrows the subquery to those evaluations' reviews. It is logically redundant while
    `Review.evaluation_id` stays denormalised from its flag at create and is never re-stamped (were
    it to drift, this predicate would start dropping real exploits) — the correlation already
    restricts the subquery to one flag's reviews — but it changes how Postgres runs
    it: inside an aggregate `FILTER` the EXISTS cannot be flattened into a semi-join, so without
    the extra predicate the subplan reads the whole `reviews` table once per query instead of
    probing `ix_reviews_evaluation_id`. In a plain `WHERE` (the other callers) it flattens and the
    predicate buys nothing, so they omit it.

    The win is a function of how narrow the scope is and does not hold all the way up: on Postgres 18,
    a 10-evaluation group reads via `Bitmap Index Scan` at 54 ms → 7 ms, while a 500-evaluation group
    seq-scans either way and pays ~0.5 ms more planning against a ~290 ms query (measured 2026-08-10;
    the plans are in the knowledge base). No width threshold gates the predicate — half a millisecond
    does not buy a magic number in a module that has no other such switch.
    """
    predicates = [
        col(Review.message_flag_id) == col(MessageFlag.id),
        col(Review.successful_exploit).is_(True),
        col(Review.deleted_at).is_(None),
    ]
    if eval_ids is not None:
        predicates.append(col(Review.evaluation_id).in_(eval_ids))
    return exists().where(*predicates)


async def _exploited_submission_count(
    session: AsyncSession, evaluation_id: UUID, *, viewer_id: UUID | None = None
) -> int:
    """Distinct exploited submissions — live flags (live conversation) carrying a successful-exploit review.

    The single source of truth for the headline exploit rate. The by-model distribution keys its counts
    on *live* assignments and the by-prompt distribution on the *flagged-turn* chain, so each can omit an
    edge case (an exploit whose conversation points at a soft-deleted assignment; a flag whose flagged
    messages are all gone). This count depends on neither, so the rate can't silently disagree with — or
    have to be re-derived by summing — either chart. ``viewer_id``, when given, narrows to the caller's
    own submissions (personal scope).
    """
    statement = (
        select(func.count(func.distinct(col(MessageFlag.id))))
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .where(
            col(MessageFlag.evaluation_id) == evaluation_id,
            col(MessageFlag.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
            _has_successful_exploit_review(),
        )
    )
    if viewer_id is not None:
        statement = statement.where(col(MessageFlag.created_by_id) == viewer_id)
    return (await session.execute(statement)).scalar_one()


async def _exploits_by_prompt_count(
    session: AsyncSession, evaluation_id: UUID, *, viewer_id: UUID | None = None
) -> list[ExploitPromptCountBucket]:
    """Distribution of exploited submissions by how many prompts it took to first land one.

    An *exploited submission* is a live flag (with a live conversation) carrying at least one live
    successful-exploit review; its prompt count is the earliest flagged turn's index + 1 — how many
    prompts before the model first broke. Counts distinct flags (a second confirming reviewer does
    not inflate the histogram). Returns only prompt counts with at least one exploit, ascending.
    ``viewer_id``, when given, narrows to the caller's own submissions (personal scope).

    Each bucket also carries the mean tokens its exploits cost to reach — the spend of the turns up
    to and including the exploiting one, so it answers "what did breaking it on the Nth prompt
    cost". Two grouped queries whatever the exploit count: the per-turn spend of the involved
    conversations is fetched once and prefix-summed per flag in Python. Two flags sharing a
    conversation and a cutoff each carry that prefix — the figure is per exploit, not per message,
    so the same turns legitimately back both. The token query needs no ``viewer_id`` filter of its
    own: it is restricted to the conversations the (already viewer-narrowed) flag query returned, and
    flag authoring is owner-only (`annotations.services.message_flags.resolve_conversation_context`,
    even under the `evaluation_groups:manage` break-glass), so a viewer's flags only ever sit on that
    viewer's own conversations. A non-owner authoring path would silently widen this read.
    """
    statement = (
        select(col(MessageFlag.id), col(Conversation.id), func.min(col(Turn.turn_index)))
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .join(FlaggedMessage, col(FlaggedMessage.message_flag_id) == col(MessageFlag.id))
        .join(Message, col(FlaggedMessage.message_id) == col(Message.id))
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .where(
            col(MessageFlag.evaluation_id) == evaluation_id,
            col(MessageFlag.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
            col(Message.deleted_at).is_(None),
            col(Turn.deleted_at).is_(None),
            _has_successful_exploit_review(),
        )
        .group_by(col(MessageFlag.id), col(Conversation.id))
    )
    if viewer_id is not None:
        statement = statement.where(col(MessageFlag.created_by_id) == viewer_id)
    rows = (await session.execute(statement)).all()
    turn_spend = await _turn_spend_for_conversations(session, {conversation_id for _, conversation_id, _ in rows})

    histogram: dict[int, int] = defaultdict(int)
    spend: dict[int, list[int]] = defaultdict(list)
    for _flag_id, conversation_id, min_turn_index in rows:
        prompt_count = min_turn_index + 1
        histogram[prompt_count] += 1
        prefix = [
            (total, measured)
            for turn_index, (total, measured) in turn_spend.get(conversation_id, {}).items()
            if turn_index <= min_turn_index
        ]
        # A usage-free prefix means the provider reported nothing, not that the exploit was free.
        if any(measured for _total, measured in prefix):
            spend[prompt_count].append(sum(total for total, _measured in prefix))
    return [
        ExploitPromptCountBucket(
            prompt_count=prompt_count,
            exploit_count=histogram[prompt_count],
            avg_tokens_to_exploit=(
                sum(spend[prompt_count]) / len(spend[prompt_count]) if spend[prompt_count] else None
            ),
            exploits_with_tokens=len(spend[prompt_count]),
        )
        for prompt_count in sorted(histogram)
    ]


async def _turn_spend_for_conversations(
    session: AsyncSession, conversation_ids: set[UUID]
) -> dict[UUID, dict[int, tuple[int, int]]]:
    """Per-turn `(total_tokens, messages_with_usage)` for ``conversation_ids``, one grouped query.

    Goes through `_live_message_tokens`, so the same live message → turn → conversation chain the
    activity and token breakdowns use: a soft-deleted message stops counting toward the exploit it
    preceded, exactly as it stops counting toward the message total.
    """
    if not conversation_ids:
        return {}
    rows = (
        await session.execute(
            _live_message_tokens(col(Turn.conversation_id), col(Turn.turn_index)).where(
                col(Turn.conversation_id).in_(conversation_ids)
            )
        )
    ).all()
    spend: dict[UUID, dict[int, tuple[int, int]]] = defaultdict(dict)
    for conversation_id, turn_index, *aggregate_columns in rows:
        aggregates = TokenAggregates(*aggregate_columns)
        spend[conversation_id][turn_index] = (aggregates.total_tokens, aggregates.messages_with_usage)
    return spend


async def _live_assignments(session: AsyncSession, evaluation_id: UUID) -> Sequence[LiveAssignment]:
    """`(assignment_id, model_alias, model_display_mask)` per live assignment of ``evaluation_id``.

    The zero-inclusive roster both by-model distributions iterate. Shared so the two can never
    disagree about which models exist, and fetched once per request rather than once per chart.
    """
    rows = (
        await session.execute(
            select(col(EvaluationAiModel.id), col(AiModel.model_alias), col(EvaluationAiModel.model_display_mask))
            .join(AiModel, col(EvaluationAiModel.model_id) == col(AiModel.id))
            .where(
                col(EvaluationAiModel.evaluation_id) == evaluation_id,
                col(EvaluationAiModel.deleted_at).is_(None),
            )
        )
    ).all()
    return [LiveAssignment(*row) for row in rows]


async def _exploits_by_model(
    session: AsyncSession,
    evaluation_id: UUID,
    assignments: Sequence[LiveAssignment],
    *,
    viewer_id: UUID | None = None,
    mask_model_names: bool = False,
) -> list[ExploitsByModelMetrics]:
    """Successful-exploit count per assigned model (zero-inclusive over live assignments).

    Counts distinct exploited submissions grouped by the conversation's model assignment. Every
    live assignment appears — including models with zero exploits (the ones that held) — ordered
    by exploit count descending, then name for a stable tie-break. ``viewer_id``, when given,
    narrows the exploit counts to the caller's own submissions (personal scope) while every
    assignment still appears. ``mask_model_names`` surfaces each assignment's display mask (which
    may be `null`) in place of the real alias, for a caller who isn't a full-access `view_metrics`
    holder viewing a masked evaluation.
    """
    exploit_statement = (
        select(col(Conversation.evaluation_ai_model_id), func.count(func.distinct(col(MessageFlag.id))))
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .where(
            col(MessageFlag.evaluation_id) == evaluation_id,
            col(MessageFlag.deleted_at).is_(None),
            col(Conversation.deleted_at).is_(None),
            _has_successful_exploit_review(),
        )
        .group_by(col(Conversation.evaluation_ai_model_id))
    )
    if viewer_id is not None:
        exploit_statement = exploit_statement.where(col(MessageFlag.created_by_id) == viewer_id)
    exploit_rows = (await session.execute(exploit_statement)).all()
    exploits_by_assignment = {row[0]: row[1] for row in exploit_rows}
    metrics = [
        ExploitsByModelMetrics(
            evaluation_ai_model_id=assignment_id,
            model_alias=(model_display_mask if mask_model_names else model_alias),
            exploit_count=exploits_by_assignment.get(assignment_id, 0),
        )
        for assignment_id, model_alias, model_display_mask in assignments
    ]
    # Null-safe tie-break: a masked assignment with no mask set surfaces `None`.
    metrics.sort(key=lambda m: (-m.exploit_count, m.model_alias or ""))
    return metrics


async def _tokens_by_model(
    session: AsyncSession,
    evaluation_id: UUID,
    assignments: Sequence[LiveAssignment],
    *,
    viewer_id: UUID | None = None,
    mask_model_names: bool = False,
) -> list[TokensByModelMetrics]:
    """Token spend per assigned model (zero-inclusive over live assignments).

    Structurally the `_exploits_by_model` sibling, over the same `_live_assignments` roster: every
    live assignment appears — including a model nobody spent tokens against, so the console can show
    "assigned but unused" rather than omitting the row — ordered by spend descending, then name for a
    stable tie-break. Spend against an assignment that was since removed reaches no row (the roster
    is live-only) while staying in the evaluation's `tokens` total, so the rows can sum to less.
    ``viewer_id`` narrows the spend to the caller's own conversations while every assignment still
    appears; ``mask_model_names`` surfaces each assignment's display mask (which may be `null`) in
    place of the real alias, for a caller who isn't a full-access `view_metrics` holder viewing a
    masked evaluation.
    """
    token_statement = _live_message_tokens(col(Conversation.evaluation_ai_model_id)).where(
        col(Conversation.evaluation_id) == evaluation_id
    )
    if viewer_id is not None:
        token_statement = token_statement.where(col(Conversation.user_id) == viewer_id)
    token_rows = (await session.execute(token_statement)).all()
    tokens_by_assignment = {row[0]: _token_metrics(row[1:]) for row in token_rows}
    metrics = [
        TokensByModelMetrics(
            evaluation_ai_model_id=assignment_id,
            model_alias=(model_display_mask if mask_model_names else model_alias),
            tokens=tokens_by_assignment.get(assignment_id, _ZERO_TOKENS),
        )
        for assignment_id, model_alias, model_display_mask in assignments
    ]
    # A masked assignment surfaces a `None` alias, so the id closes the sort for a stable order.
    metrics.sort(key=lambda m: (-m.tokens.total_tokens, m.model_alias or "", m.evaluation_ai_model_id))
    return metrics


async def evaluation_metrics(
    session: AsyncSession,
    evaluation: Evaluation,
    *,
    group: EvaluationGroup,
    viewer_id: UUID | None = None,
    mask_model_names: bool = False,
) -> EvaluationMetricsResponse:
    """Aggregate metrics for a single evaluation — its group-dashboard row plus exploit distributions.

    Reuses the same live-parent-joined breakdown helpers as `evaluation_group_metrics`, so a single
    evaluation's numbers match its row in the group dashboard exactly, then adds the two
    evaluation-specific distributions (exploits by prompt count and by assigned model). ``group``
    supplies the timeline's declared span (its start and end dates); the axis is that span widened
    by *this* evaluation's own submissions, and the group dashboard's is widened by every
    evaluation's, so the two are not ordered — this one can be narrower, and it can also open
    earlier. ``viewer_id`` set narrows every contribution breakdown to that
    user's own data (personal scope);
    ``mask_model_names`` replaces the by-model distribution's real aliases with each assignment's
    display mask for a caller who isn't a full-access `view_metrics` holder.
    """
    if group.id != evaluation.evaluation_group_id:
        raise ValueError(f"Group {group.id} is not the parent of evaluation {evaluation.id}.")

    scope = MetricsScope.PERSONAL if viewer_id is not None else MetricsScope.FULL
    eval_ids = [evaluation.id]
    scenarios, tasks = await _scenario_structure(session, eval_ids)
    (
        _,
        eval_submission_status,
        scenario_submission_total,
        task_submission_total,
    ) = await _submission_breakdown(session, eval_ids, viewer_id=viewer_id)
    eval_reviews = await _review_breakdown(session, eval_ids, viewer_id=viewer_id)
    eval_conversations, eval_messages = await _activity_breakdown(session, eval_ids, viewer_id=viewer_id)
    eval_tokens, scenario_tokens = await _token_breakdown(session, eval_ids, viewer_id=viewer_id)
    eval_models = await _models_breakdown(session, eval_ids)
    daily = (await _submissions_by_day(session, eval_ids, viewer_id=viewer_id)).get(evaluation.id, {})
    # One roster for both by-model distributions — they must agree on which models exist.
    assignments = await _live_assignments(session, evaluation.id)

    tasks_by_scenario: dict[UUID, list[Task]] = defaultdict(list)
    for task in tasks:
        tasks_by_scenario[task.scenario_id].append(task)
    scenarios_by_evaluation: dict[UUID, list[Scenario]] = defaultdict(list)
    for scenario in scenarios:
        scenarios_by_evaluation[scenario.evaluation_id].append(scenario)

    return EvaluationMetricsResponse(
        scope=scope,
        evaluation_id=evaluation.id,
        title=evaluation.title,
        models_assigned=eval_models.get(evaluation.id, 0),
        submissions=_submissions(eval_submission_status.get(evaluation.id, {})),
        reviews=eval_reviews.get(evaluation.id, _ZERO_REVIEWS),
        activity=ActivityMetrics(
            conversations=eval_conversations.get(evaluation.id, 0),
            messages=eval_messages.get(evaluation.id, 0),
        ),
        tokens=eval_tokens.get(evaluation.id, _ZERO_TOKENS),
        scenarios=_scenario_metrics_for(
            evaluation.id,
            scenarios_by_evaluation,
            tasks_by_scenario,
            scenario_submission_total,
            task_submission_total,
            scenario_tokens,
        ),
        submissions_by_day=_daily_series(
            daily, _timeline_window(group.start_date, group.end_date, set(daily), today=datetime.now(UTC).date())
        ),
        exploited_submissions=await _exploited_submission_count(session, evaluation.id, viewer_id=viewer_id),
        exploits_by_prompt_count=await _exploits_by_prompt_count(session, evaluation.id, viewer_id=viewer_id),
        exploits_by_model=await _exploits_by_model(
            session, evaluation.id, assignments, viewer_id=viewer_id, mask_model_names=mask_model_names
        ),
        tokens_by_model=await _tokens_by_model(
            session, evaluation.id, assignments, viewer_id=viewer_id, mask_model_names=mask_model_names
        ),
    )
