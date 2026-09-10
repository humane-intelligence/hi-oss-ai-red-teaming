"""Response schemas for evaluation-group aggregate metrics.

One `EvaluationGroupMetricsResponse` carries the whole-event roll-up plus the
per-evaluation breakdown (and, within it, by-scenario / by-task submission
counts) so the frontend gets group- and evaluation-level numbers in one call.
The service (`app.core.analytics.services.metrics`) computes these live; nothing
is persisted.
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.core.evaluations.enums import MetricsScope


class MemberMetrics(BaseModel):
    """Members of the group, from the object-role assignments.

    `total` is the distinct headcount; `by_role` counts memberships per role. A member
    may hold several in-group roles, so a user is counted once in `total` but once under
    *each* of their roles — meaning `sum(by_role.values()) >= total`. Read them
    independently; don't sum `by_role` to recover `total`.

    Always the group-wide roster, even on a `personal`-scope read — a headcount is
    structural context, not one of the caller's own contributions.
    """

    total: int = Field(description="Distinct users holding at least one in-group role.")
    by_role: dict[str, int] = Field(
        description=(
            "Member count keyed by in-group role name (e.g. `owner`, `red_teamer`). A member with "
            "multiple roles is counted under each, so this can sum to more than `total`."
        ),
    )


class SubmissionMetrics(BaseModel):
    """Submission (`MessageFlag`) counts, split by review status."""

    total: int = Field(description="All live submissions in scope.")
    pending: int = Field(description="Submissions awaiting a verdict.")
    approved: int = Field(description="Submissions marked approved.")
    rejected: int = Field(description="Submissions marked rejected.")


class ReviewMetrics(BaseModel):
    """Review completion and exploit-verdict tallies."""

    total: int = Field(description="All live reviews in scope (assignments + verdicts).")
    completed: int = Field(description="Reviews with a recorded verdict (approved or rejected).")
    pending: int = Field(description="Reviews still awaiting a verdict.")
    successful_exploit: int = Field(
        description=(
            "Reviews whose verdict marked the exploit successful. Counts verdicts, not submissions, so "
            "several reviewers confirming one submission count several times — unlike "
            "`DailySubmissionPoint.exploited_submissions`, which counts each submission once."
        )
    )
    unique_exploit: int = Field(description="Reviews whose verdict marked the exploit unique.")
    valid_submission: int = Field(description="Reviews whose verdict marked the submission valid.")


class ActivityMetrics(BaseModel):
    """Engagement volume — conversations started and messages exchanged."""

    conversations: int = Field(description="Live conversations in scope.")
    messages: int = Field(description="Live messages across those conversations.")


class DailySubmissionPoint(BaseModel):
    """One UTC calendar day of an event's submission timeline."""

    day: date = Field(description="The UTC calendar day these counts fall on.", examples=["2026-08-03"])
    submissions: int = Field(description="Live submissions created on this day.")
    exploited_submissions: int = Field(
        description=(
            "Of those submissions, the ones carrying a confirmed successful-exploit review. Counted once per "
            "submission however many reviewers confirmed it, and dated by the submission rather than the "
            "review — so it is never greater than `submissions`. Deliberately *not* the same measure as "
            "`reviews.successful_exploit`, which counts verdicts and so runs higher when several reviewers "
            "confirm one submission."
        )
    )


class TokenMetrics(BaseModel):
    """Model token spend, read from each assistant message's `extra["usage"]`.

    Three things are not obvious from the field names. **Only assistant messages report usage**, so
    `messages_with_usage` sits well below `activity.messages` and the two are not comparable.
    **`prompt_tokens` re-counts resent history**, which makes the sum right for cost and makes
    `avg_tokens_per_message` grow with conversation depth by design. **`total_tokens` is derived**
    from `prompt + completion` when a provider reports no total, so a one-sided report is not
    dropped.

    Both averages divide by the usage-bearing denominator beside them, never by the full message or
    conversation count — a model with no working credential must read as absent, not as near-zero.
    On garbage input the three counts need not sum: a refused value drops from its own field while a
    usable `total_tokens` on the same message still lands.
    """

    prompt_tokens: int = Field(description="Reported input tokens, summed. Includes each request's resent history.")
    completion_tokens: int = Field(description="Reported output tokens, summed.")
    total_tokens: int = Field(
        description=(
            "Total tokens, summed per message as the reported total when present, else "
            "`prompt_tokens + completion_tokens`. This is the cost-bearing number."
        )
    )
    messages_with_usage: int = Field(
        description=(
            "Messages that reported at least one *usable* usage value — the denominator of "
            "`avg_tokens_per_message`. A message whose every reported value is unusable (not a number, "
            "or outside `[0, 2**63 - 1]`) is excluded rather than counted with nothing in the numerator. "
            "Counts every billed generation, including attempts a regenerate later superseded, so it "
            "is not a count of the messages the transcript shows and the two are not comparable."
        )
    )
    conversations_with_usage: int = Field(
        description="Conversations holding at least one message with usable reported usage — the "
        "denominator of `avg_tokens_per_conversation`."
    )
    avg_tokens_per_message: float = Field(
        description="`total_tokens / messages_with_usage`, or 0.0 when nothing reported usage."
    )
    avg_tokens_per_conversation: float = Field(
        description="`total_tokens / conversations_with_usage`, or 0.0 when nothing reported usage."
    )


class TaskMetrics(BaseModel):
    """By-task submission breakdown."""

    task_id: UUID = Field(description="The task the submissions targeted.")
    name: str = Field(description="Task name.")
    submissions_total: int = Field(description="Live submissions targeting this task.")


class ScenarioMetrics(BaseModel):
    """By-scenario submission breakdown, with its tasks."""

    scenario_id: UUID = Field(description="The scenario the submissions targeted.")
    name: str = Field(description="Scenario name.")
    submissions_total: int = Field(description="Live submissions targeting this scenario (any or no task).")
    tasks: list[TaskMetrics] = Field(description="Per-task breakdown; includes tasks with zero submissions.")
    tokens: TokenMetrics = Field(
        description=(
            "Token spend of conversations tagged with this scenario. `Conversation.scenario_id` is "
            "nullable, so conversations started outside any scenario count toward the evaluation but "
            "land in no scenario row — these therefore legitimately sum to less than the evaluation "
            "total. There is no per-task equivalent: nothing links a message to a task."
        )
    )


class EvaluationMetrics(BaseModel):
    """One evaluation's roll-up, plus its by-scenario / by-task breakdown."""

    evaluation_id: UUID = Field(description="The evaluation these numbers belong to.")
    title: str = Field(description="Evaluation title.")
    models_assigned: int = Field(description="Live model assignments on this evaluation.")
    submissions: SubmissionMetrics
    reviews: ReviewMetrics
    activity: ActivityMetrics
    tokens: TokenMetrics
    scenarios: list[ScenarioMetrics] = Field(
        description="Per-scenario breakdown; includes scenarios with zero submissions."
    )
    submissions_by_active_day: list[DailySubmissionPoint] = Field(
        description=(
            "This evaluation's submission timeline, oldest first, **sparse** — only the days that carry "
            "at least one submission, with no zero-filled gaps. Named apart from the dense "
            "`submissions_by_day` arrays deliberately: same element type, different shape. Plot these "
            "against the group-level `submissions_by_day`, which carries the full zero-filled axis and "
            "always contains every day listed here."
        )
    )


class ExploitPromptCountBucket(BaseModel):
    """One point of the exploits-by-prompt-count distribution.

    `prompt_count` is how many prompts it took to reach the first exploit-flagged message in
    a conversation (the earliest flagged turn's index + 1) — the "effort to break" signal:
    a 1 means a single-shot jailbreak, a larger number a multi-turn coax.
    """

    prompt_count: int = Field(
        description="Prompts to the first exploit-flagged message (earliest flagged turn index + 1)."
    )
    exploit_count: int = Field(description="Successful exploits that first landed at this prompt count.")
    avg_tokens_to_exploit: float | None = Field(
        description=(
            "Mean tokens one exploit in this bucket cost to reach, so a shallow break can be compared "
            "against a deep one rather than against the conversation's whole spend. Averaged over "
            "`exploits_with_tokens`, not over `exploit_count`; `null` when no exploit here reported "
            "usage — a zero would read as free rather than as unmeasured.\n\n"
            "The unit is a **whole turn**: it sums every billed generation in the turns up to and "
            "including the exploiting one. So a regenerate *within* the exploiting turn is included "
            "even though it came after the break, and a turn whose provider reported nothing "
            "contributes zero rather than excluding the exploit — an exploit counts as measured once "
            "any turn in its prefix reported usage, which makes a partly-reported prefix an "
            "understatement rather than a gap."
        )
    )
    exploits_with_tokens: int = Field(
        description=(
            "How many of this bucket's exploits had at least one usage-reporting turn in their prefix, "
            "i.e. the denominator `avg_tokens_to_exploit` divides by. Below `exploit_count` when a "
            "provider reported nothing at all for an exploit."
        )
    )


class ExploitsByModelMetrics(BaseModel):
    """Successful exploits attributed to one assigned model (zero-inclusive, like the scenario rows)."""

    evaluation_ai_model_id: UUID = Field(description="The evaluation's model assignment these exploits were against.")
    model_alias: str | None = Field(
        description=(
            "The assigned model's alias, or its display mask when the evaluation masks models and the "
            "caller is not a full-access `view_metrics` holder (owner/admin); `null` when masked with no "
            "mask set. Only the owner/admin ever sees the real alias under masking."
        )
    )
    exploit_count: int = Field(description="Successful exploits recorded against this model (0 for a model that held).")


class TokensByModelMetrics(BaseModel):
    """Token spend attributed to one assigned model (zero-inclusive, like the exploit rows).

    Every live assignment appears, so a model that was assigned but never used shows zeroes rather
    than vanishing from the list. Spend against an assignment that was since removed is in the
    evaluation's `tokens` total but in no row here, so the rows can sum to less than that total.
    """

    evaluation_ai_model_id: UUID = Field(description="The evaluation's model assignment this spend was against.")
    model_alias: str | None = Field(
        description=(
            "The assigned model's alias, or its display mask when the evaluation masks models and the "
            "caller is not a full-access `view_metrics` holder (owner/admin); `null` when masked with no "
            "mask set. Only the owner/admin ever sees the real alias under masking."
        )
    )
    tokens: TokenMetrics


class GroupTokensByModelMetrics(BaseModel):
    """Group-wide token spend attributed to one underlying registry model.

    The evaluation-level sibling `TokensByModelMetrics` keys on the *assignment*; this keys on the
    `AiModel` itself, so a model assigned to several of the group's evaluations is one row summing
    all of them — "what did this model cost across the event", the question the group dashboard
    asks. `EvaluationAiModelView` withholds exactly this correlation under masking, which is why
    the whole list is withheld rather than masked per row (see `tokens_by_model` on the response).
    """

    ai_model_id: UUID = Field(description="The registry model (`AiModel`) this spend was against.")
    model_alias: str = Field(description="The model's real alias — this list is only ever served unmasked.")
    tokens: TokenMetrics


class EvaluationMetricsResponse(BaseModel):
    """Single-evaluation aggregate — one group-dashboard row scoped to this evaluation, plus exploit distributions.

    The roll-up (submissions / reviews / activity / scenarios) mirrors the group dashboard's
    per-evaluation breakdown; on top of it this view adds the two evaluation-specific
    distributions: successful exploits by prompt count and by assigned model, and a daily submission
    timeline over the parent group's declared span.

    `scope` reports whether the numbers are the full event-wide aggregate or filtered
    to the caller's own contributions (a `members_personal_metrics` group + a
    `view_personal_metrics` member).
    """

    scope: MetricsScope = Field(
        description="`full` for the event-wide aggregate, or `personal` when filtered to the caller's own data."
    )
    evaluation_id: UUID = Field(description="The evaluation these metrics summarise.")
    title: str = Field(description="Evaluation title.")
    models_assigned: int = Field(description="Live model assignments on this evaluation.")
    submissions: SubmissionMetrics
    reviews: ReviewMetrics
    activity: ActivityMetrics
    tokens: TokenMetrics
    exploited_submissions: int = Field(
        description=(
            "Distinct submissions with a confirmed successful exploit (the headline exploit-rate numerator). "
            "Counted once per submission regardless of how many reviewers confirmed it. The two exploit "
            "*distributions* below are derived independently of it, so neither can drag it off; "
            "`submissions_by_day` measures the same thing per day and sums back to it."
        )
    )
    scenarios: list[ScenarioMetrics] = Field(
        description="Per-scenario breakdown; includes scenarios with zero submissions."
    )
    submissions_by_day: list[DailySubmissionPoint] = Field(
        description=(
            "This evaluation's submission timeline, oldest first: one entry per UTC day, zero-filled. "
            "The window is the parent group's declared span, widened by *this* evaluation's own "
            "submissions — the group dashboard's axis is widened by every evaluation's, so the two "
            "are not ordered: this one can be narrower, and it can also open earlier, since each axis "
            "caps its year of lead-in against its own trailing edge. Empty while there is nothing to "
            "plot: no group start date and no "
            "submission, or a group with no submissions whose start date is still in the future. On a "
            "`personal` read the "
            "widening uses the caller's own submissions, so two members can see different spans."
        )
    )
    exploits_by_prompt_count: list[ExploitPromptCountBucket] = Field(
        description="Successful-exploit distribution by prompt count, ascending; only counts with an exploit."
    )
    exploits_by_model: list[ExploitsByModelMetrics] = Field(
        description="Successful exploits per assigned model, descending by count; includes models with zero exploits."
    )
    tokens_by_model: list[TokensByModelMetrics] = Field(
        description="Token spend per assigned model, descending by total; includes models with zero spend."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scope": "full",
                "evaluation_id": "2f1a8e10-1111-2222-3333-444455556666",
                "title": "Prompt injection round 1",
                "models_assigned": 2,
                "submissions": {"total": 8, "pending": 2, "approved": 5, "rejected": 1},
                "reviews": {
                    "total": 6,
                    "completed": 5,
                    "pending": 1,
                    "successful_exploit": 4,
                    "unique_exploit": 2,
                    "valid_submission": 5,
                },
                "activity": {"conversations": 14, "messages": 210},
                "tokens": {
                    "prompt_tokens": 11200,
                    "completion_tokens": 3100,
                    "total_tokens": 14300,
                    "messages_with_usage": 104,
                    "conversations_with_usage": 14,
                    "avg_tokens_per_message": 137.5,
                    "avg_tokens_per_conversation": 1021.4,
                },
                "exploited_submissions": 4,
                "submissions_by_day": [
                    {"day": "2026-08-03", "submissions": 5, "exploited_submissions": 3},
                    {"day": "2026-08-04", "submissions": 0, "exploited_submissions": 0},
                    {"day": "2026-08-05", "submissions": 3, "exploited_submissions": 1},
                ],
                "scenarios": [
                    {
                        "scenario_id": "3c2b9f21-2222-3333-4444-555566667777",
                        "name": "System-prompt leak",
                        "submissions_total": 5,
                        "tasks": [
                            {
                                "task_id": "4d3ca032-3333-4444-5555-666677778888",
                                "name": "Extract hidden instructions",
                                "submissions_total": 3,
                            }
                        ],
                        "tokens": {
                            "prompt_tokens": 4100,
                            "completion_tokens": 1200,
                            "total_tokens": 5300,
                            "messages_with_usage": 38,
                            "conversations_with_usage": 5,
                            "avg_tokens_per_message": 139.5,
                            "avg_tokens_per_conversation": 1060.0,
                        },
                    }
                ],
                "exploits_by_prompt_count": [
                    {"prompt_count": 1, "exploit_count": 3, "avg_tokens_to_exploit": 142.0, "exploits_with_tokens": 3},
                    {"prompt_count": 2, "exploit_count": 1, "avg_tokens_to_exploit": 438.0, "exploits_with_tokens": 1},
                ],
                "exploits_by_model": [
                    {
                        "evaluation_ai_model_id": "5e4db143-4444-5555-6666-777788889999",
                        "model_alias": "gpt-4o-mini",
                        "exploit_count": 3,
                    },
                    {
                        "evaluation_ai_model_id": "6f5ec254-5555-6666-7777-888899990000",
                        "model_alias": "claude-haiku",
                        "exploit_count": 1,
                    },
                ],
                "tokens_by_model": [
                    {
                        "evaluation_ai_model_id": "5e4db143-4444-5555-6666-777788889999",
                        "model_alias": "gpt-4o-mini",
                        "tokens": {
                            "prompt_tokens": 8100,
                            "completion_tokens": 2300,
                            "total_tokens": 10400,
                            "messages_with_usage": 76,
                            "conversations_with_usage": 10,
                            "avg_tokens_per_message": 136.8,
                            "avg_tokens_per_conversation": 1040.0,
                        },
                    }
                ],
            }
        }
    )


class EvaluationGroupMetricsResponse(BaseModel):
    """Whole-event aggregate: the group roll-up and the per-evaluation breakdown.

    `scope` reports whether the numbers are the full event-wide aggregate or filtered
    to the caller's own contributions (a `members_personal_metrics` group + a
    `view_personal_metrics` member). `members` is always the full roster regardless.
    """

    scope: MetricsScope = Field(
        description="`full` for the event-wide aggregate, or `personal` when filtered to the caller's own data."
    )
    group_id: UUID = Field(description="The evaluation group (event) these metrics summarise.")
    members: MemberMetrics = Field(description="The group's member roster by role (always group-wide).")
    submissions: SubmissionMetrics = Field(description="Group-wide submission totals.")
    reviews: ReviewMetrics = Field(description="Group-wide review totals.")
    activity: ActivityMetrics = Field(description="Group-wide activity totals.")
    tokens: TokenMetrics = Field(
        description="Group-wide token spend — equal to the sum of the per-evaluation `tokens.total_tokens`."
    )
    tokens_by_model: list[GroupTokensByModelMetrics] = Field(
        description=(
            "Group-wide spend per registry model, descending; zero-inclusive over the models assigned "
            "to the group's live evaluations, so an assigned-but-unused model reads as unused rather "
            "than missing. **Empty** unless the caller holds full `evaluation_groups:view_metrics` "
            "access or no evaluation in the group masks model names: a row correlates one model's cost "
            "across evaluations, which is precisely what per-evaluation masking withholds, so one "
            "masking evaluation withholds the whole list. `tokens` is unaffected — only the "
            "attribution is withheld, never the total."
        )
    )
    tokens_by_model_withheld: bool = Field(
        description=(
            "Whether `tokens_by_model` is empty *because* the attribution is withheld, rather than "
            "because no model is assigned. Render the two differently: withheld means the spend "
            "exists but cannot be attributed, so a per-model chart should say so instead of reading "
            "as zero. Never derive this from `models_assigned` sums."
        )
    )
    evaluations: list[EvaluationMetrics] = Field(
        description="Per-evaluation breakdown, in the group's evaluation order."
    )
    submissions_by_day: list[DailySubmissionPoint] = Field(
        description=(
            "Group-wide submission timeline, oldest first: one entry per UTC day of the window, "
            "zero-filled. The window runs from the earlier of the group's start date and its first "
            "submission to the later of its last submission and today (or the group's end date, once "
            "that has passed); a start date more than a year before that end contributes only the last "
            "year of empty lead-in, but no submission is ever left outside. So the series and "
            "`submissions.total` agree, up to a submission committed between the two reads that backs "
            "them. Empty while there is nothing to plot: a "
            "group with neither a start date nor a submission, or one whose start date is still in the "
            "future. On a `personal` read the window is derived from the caller's own submissions, so "
            "two members can see different spans. Each evaluation's own sparse "
            "`submissions_by_active_day` plots against this axis."
        )
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "scope": "full",
                "group_id": "6f9619ff-8b86-d011-b42d-00cf4fc964ff",
                "members": {"total": 7, "by_role": {"owner": 1, "red_teamer": 5, "annotator": 1}},
                "submissions": {"total": 12, "pending": 4, "approved": 6, "rejected": 2},
                "reviews": {
                    "total": 9,
                    "completed": 7,
                    "pending": 2,
                    "successful_exploit": 5,
                    "unique_exploit": 3,
                    "valid_submission": 6,
                },
                "activity": {"conversations": 20, "messages": 340},
                # Equal to the one evaluation listed below, since `tokens` is documented as the sum
                # of the per-evaluation totals and this example carries a single evaluation.
                "tokens": {
                    "prompt_tokens": 11200,
                    "completion_tokens": 3100,
                    "total_tokens": 14300,
                    "messages_with_usage": 104,
                    "conversations_with_usage": 14,
                    "avg_tokens_per_message": 137.5,
                    "avg_tokens_per_conversation": 1021.4,
                },
                "tokens_by_model": [
                    {
                        "ai_model_id": "9a8b7c6d-1111-2222-3333-444455556666",
                        "model_alias": "gpt-4o-mini",
                        "tokens": {
                            "prompt_tokens": 11200,
                            "completion_tokens": 3100,
                            "total_tokens": 14300,
                            "messages_with_usage": 104,
                            "conversations_with_usage": 14,
                            "avg_tokens_per_message": 137.5,
                            "avg_tokens_per_conversation": 1021.4,
                        },
                    }
                ],
                "tokens_by_model_withheld": False,
                "evaluations": [
                    {
                        "evaluation_id": "2f1a8e10-1111-2222-3333-444455556666",
                        "title": "Prompt injection round 1",
                        "models_assigned": 2,
                        "submissions": {"total": 8, "pending": 2, "approved": 5, "rejected": 1},
                        "reviews": {
                            "total": 6,
                            "completed": 5,
                            "pending": 1,
                            "successful_exploit": 4,
                            "unique_exploit": 2,
                            "valid_submission": 5,
                        },
                        "activity": {"conversations": 14, "messages": 210},
                        "tokens": {
                            "prompt_tokens": 11200,
                            "completion_tokens": 3100,
                            "total_tokens": 14300,
                            "messages_with_usage": 104,
                            "conversations_with_usage": 14,
                            "avg_tokens_per_message": 137.5,
                            "avg_tokens_per_conversation": 1021.4,
                        },
                        "scenarios": [
                            {
                                "scenario_id": "3c2b9f21-2222-3333-4444-555566667777",
                                "name": "System-prompt leak",
                                "submissions_total": 5,
                                "tasks": [
                                    {
                                        "task_id": "4d3ca032-3333-4444-5555-666677778888",
                                        "name": "Extract hidden instructions",
                                        "submissions_total": 3,
                                    }
                                ],
                                "tokens": {
                                    "prompt_tokens": 4100,
                                    "completion_tokens": 1200,
                                    "total_tokens": 5300,
                                    "messages_with_usage": 38,
                                    "conversations_with_usage": 5,
                                    "avg_tokens_per_message": 139.5,
                                    "avg_tokens_per_conversation": 1060.0,
                                },
                            }
                        ],
                        "submissions_by_active_day": [
                            {"day": "2026-08-03", "submissions": 5, "exploited_submissions": 3},
                            {"day": "2026-08-05", "submissions": 3, "exploited_submissions": 1},
                        ],
                    }
                ],
                "submissions_by_day": [
                    {"day": "2026-08-03", "submissions": 7, "exploited_submissions": 3},
                    {"day": "2026-08-04", "submissions": 0, "exploited_submissions": 0},
                    {"day": "2026-08-05", "submissions": 5, "exploited_submissions": 1},
                ],
            }
        }
    )
