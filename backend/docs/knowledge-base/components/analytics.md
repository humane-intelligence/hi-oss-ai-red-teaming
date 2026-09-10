---
tags: [component, analytics, metrics, reporting]
aliases: [Analytics, metrics dashboard, evaluation dashboard, group dashboard]
---

# Analytics - aggregate metrics

The reporting layer. Two read-only dashboards roll up the runtime data an event produces — submissions ([MessageFlag](../data-models/message-flag.md)), reviewer verdicts ([Review](../data-models/review.md)), and engagement ([Conversation](../data-models/conversation.md) / [Message](../data-models/message.md)) — into aggregate numbers. Nothing is persisted: every count is computed live from the existing tables. Who sees them, and whether they see the full event or only their own slice, is set per group by an **access-aware visibility policy** (below) — no longer owner-only.

Code: `app/core/analytics/` (`schemas.py` = response models, `services/metrics.py` = the two pure async computations) + `app/api/v1/evaluation_group_metrics.py` and `app/api/v1/evaluation_metrics.py` (HTTP). OpenAPI tag `analytics`.

## Two dashboards

| Dashboard | Endpoint | Scope | Response |
|---|---|---|---|
| **Group** | `GET /api/v1/evaluation-groups/{group_id}/metrics` | Whole event — every evaluation in the group | `EvaluationGroupMetricsResponse` |
| **Evaluation** | `GET /api/v1/evaluations/{evaluation_id}/metrics` | One evaluation | `EvaluationMetricsResponse` |

The evaluation dashboard is the group dashboard's per-evaluation row scoped to one evaluation, computed by the same live-parent-joined helpers — so a single evaluation's numbers match its row in the group roll-up exactly. On top of that shared row the evaluation view adds two evaluation-specific exploit distributions the group view omits (below).

## What it reads across

The service walks four runtime contexts, always filtered to **live rows with a live parent** so the numbers never disagree with the read paths that surface them:

- **Members** — distinct in-group members from the [object-role assignments](object-roles-per-object-permissions.md) (`ObjectType.EVALUATION_GROUP`), scoped to live users to mirror the member roster (the `MemberMetrics` roster; always group-wide).
- **Submissions** — live [MessageFlag](../data-models/message-flag.md) rows with a live parent [Conversation](../data-models/conversation.md), split by `FlagStatus` (pending / approved / rejected), folded into group / per-evaluation / per-scenario / per-task totals in one grouped pass.
- **Reviews** — live [Review](../data-models/review.md) rows joined through a live flag + live conversation (a soft-deleted flag/conversation doesn't cascade to its reviews, so the join is what keeps a dead review from being counted forever).
- **Activity** — live conversation and message counts; messages include every role (user, assistant, system) — the engagement signal, not just model output.
- **Token spend** — read out of each assistant message's `extra["usage"]`, the generation metadata the write-path stores. See [Token metrics](#token-metrics) below.

Submissions and reviews are counted from the **denormalised parent columns** on `MessageFlag` / `Review` (`evaluation_id`, `scenario_id`, `task_id`), so no breakdown walks the conversation chain. The whole computation runs in a fixed number of grouped queries independent of group size — no N+1.

## Response fields

Both responses share the same building-block models (`app/core/analytics/schemas.py`):

- **`SubmissionMetrics`** — `total`, `pending`, `approved`, `rejected`.
- **`ReviewMetrics`** — `total`, `completed` (verdict recorded), `pending`, plus the verdict tallies `successful_exploit`, `unique_exploit`, `valid_submission`.
- **`ActivityMetrics`** — `conversations`, `messages`.
- **`ScenarioMetrics`** — `scenario_id`, `name`, `submissions_total`, a `tokens` block, and a `tasks` list of **`TaskMetrics`** (`task_id`, `name`, `submissions_total`). Zero-inclusive: scenarios/tasks with no submissions still appear.
- **`TokenMetrics`** — model token spend; see the section below.
- **`DailySubmissionPoint`** — one UTC calendar day: `day`, `submissions`, `exploited_submissions`; see the timeline section below.

Both responses lead with **`scope`** (a `MetricsScope`): `full` for the event-wide aggregate, or `personal` when the numbers are filtered to the caller's own contributions (see the auth policy below).

`EvaluationGroupMetricsResponse` carries `scope`, `group_id`, a **`MemberMetrics`** (`total` + `by_role`, **always the full group roster** even on a `personal` read — a headcount is structural context, not the caller's own data), group-wide `submissions` / `reviews` / `activity` / `tokens`, `tokens_by_model` + `tokens_by_model_withheld`, `submissions_by_day` (the timeline below), and an `evaluations` list of **`EvaluationMetrics`** (per-evaluation `evaluation_id`, `title`, `models_assigned`, `submissions`, `reviews`, `activity`, `tokens`, `scenarios`), in the group's evaluation order.

`EvaluationMetricsResponse` carries `scope` + the same per-evaluation row (`evaluation_id`, `title`, `models_assigned`, `submissions`, `reviews`, `activity`, `tokens`, `scenarios`) plus:

- `exploited_submissions` — distinct submissions with a confirmed successful-exploit review, the **headline exploit-rate numerator**. Counted once per submission regardless of how many reviewers confirmed it, and independent of the two distributions below so it can never silently disagree with either.
- `exploits_by_prompt_count` — a **`ExploitPromptCountBucket`** distribution (`prompt_count`, `exploit_count`, `avg_tokens_to_exploit`, `exploits_with_tokens`), ascending; `prompt_count` is the earliest flagged turn's index + 1 — the "effort to break" signal (1 = single-shot jailbreak, larger = multi-turn coax).
- `exploits_by_model` — a **`ExploitsByModelMetrics`** list (`evaluation_ai_model_id`, `model_alias`, `exploit_count`), descending by count, zero-inclusive over live assignments (a model that held shows `0`). The alias is **masked for a lesser member** and shown real only to a full-access `view_metrics` holder (owner/admin): the evaluation route computes `mask_model_names = mask_models_enabled and not has(view_metrics)`, so a `personal`-scope red-teamer sees the display mask (or `null` when no mask is set), the owner/admin sees the real alias.
- `tokens_by_model` — a **`TokensByModelMetrics`** list (`evaluation_ai_model_id`, `model_alias`, `tokens`), descending by total, zero-inclusive over live assignments and masked on the same rule as `exploits_by_model`.
- `submissions_by_day` — this evaluation's own timeline over the **parent group's** declared span (see below).

## Submissions-per-day timeline

Both dashboards carry a daily submission series, but in **two deliberately different shapes** — same element type (`DailySubmissionPoint`), different axis:

| Field | Where | Shape |
|---|---|---|
| `submissions_by_day` | group response, and the evaluation **response** | **dense** — the full zero-filled axis |
| `submissions_by_active_day` | each `EvaluationMetrics` row inside the group response | **sparse** — only days carrying at least one submission, no zero-filled gaps |

They are named apart on purpose: plot the sparse per-evaluation series against the group-level dense axis, which always contains every day the sparse one lists.

`_timeline_window` derives the dense axis, and the rules are easy to get wrong on a redraw:

- every observed submission day is inside the window — the group's declared dates add empty **context** around the data but never clip it;
- the trailing edge is `today`, or the declared `end_date` once it has passed, stretched forward to the newest observed day when that is later — a submission after the declared end stretches the axis rather than falling off it;
- a declared start earlier than a year before that edge is pulled forward, so the empty lead-in is **capped**, not eliminated: a group declared two years ago whose data is recent still opens on a year of zeros;
- the window is **empty** when there is nothing to plot — no start date and no submission, or a start date still in the future.

`exploited_submissions` on a point is counted **once per submission** however many reviewers confirmed it, and dated by the *submission*, not the review — so it is never greater than `submissions` on the same day. That makes it deliberately a different measure from `reviews.successful_exploit`, which counts **verdicts** and runs higher when several reviewers confirm one submission.

## Token metrics

`TokenMetrics` is read from each assistant message's `extra["usage"]` — so it reports what the *provider* said, not an estimate. Three things the field names don't say:

- **Only assistant messages report usage**, so `messages_with_usage` sits well below `activity.messages` and the two are **not comparable**. It also counts every billed generation, including attempts a regenerate later superseded, so it is not a count of the messages the transcript shows.
- **`prompt_tokens` re-counts resent history.** That makes the sum right for cost, and makes `avg_tokens_per_message` grow with conversation depth *by design*.
- **`total_tokens` is derived** per message: the reported total when present, else `prompt + completion`, so a one-sided report is not dropped. This is the cost-bearing number.

Both averages divide by the usage-bearing denominator beside them (`messages_with_usage` / `conversations_with_usage`), never by the full message or conversation count — a model with no working credential must read as *absent*, not as near-zero. A message whose every reported value is unusable (not a number, or outside `[0, 2**63 - 1]`) is excluded rather than counted with an empty numerator; on garbage input the three counts need not sum, since a refused value drops from its own field while a usable `total_tokens` on the same message still lands.

Two attribution rows sit on top, and they key on different things:

| Row | Keys on | Question it answers |
|---|---|---|
| `TokensByModelMetrics` (evaluation) | the **assignment** (`evaluation_ai_model_id`) | what did this assignment cost in this evaluation |
| `GroupTokensByModelMetrics` (group) | the **registry model** (`ai_model_id`) | what did this model cost across the whole event |

Both are zero-inclusive over live assignments, so an assigned-but-unused model reads as unused rather than missing; spend against a **since-removed** assignment is in the evaluation's `tokens` total but in no row, so the rows can sum to less than the total. Likewise `ScenarioMetrics.tokens` covers conversations tagged with that scenario, and `Conversation.scenario_id` is nullable — conversations started outside any scenario count toward the evaluation but land in no scenario row. There is **no per-task equivalent**: nothing links a message to a task.

**Masking withholds the group breakdown wholesale.** `GroupTokensByModelMetrics` correlates one model's cost *across evaluations*, which is precisely what per-evaluation model masking withholds — so `tokens_by_model` is empty unless the caller holds full `evaluation_groups:view_metrics` access or **no** evaluation in the group masks model names. One masking evaluation withholds the whole list. `tokens_by_model_withheld` distinguishes "withheld" from "no model assigned", so a per-model chart can say so instead of rendering as zero; never derive it from `models_assigned` sums. The `tokens` **total is never withheld** — only the attribution.

`avg_tokens_to_exploit` on a prompt-count bucket lets a shallow break be compared against a deep one rather than against the conversation's whole spend. Its unit is a **whole turn**: it sums every billed generation in the turns up to and including the exploiting one, so a regenerate *within* the exploiting turn counts even though it came after the break. It averages over `exploits_with_tokens` (an exploit counts as measured once any turn in its prefix reported usage), and is `null` — never `0` — when no exploit in the bucket reported usage, because a zero would read as *free* rather than as unmeasured. A partly-reported prefix is therefore an understatement, not a gap.

On personal scope, the service filters every *contribution* breakdown (submissions / reviews / activity, and the exploit distributions) by the caller's `viewer_id`, while the **structural** counts — the member roster, the scenario/task shape, the model-assignment counts — stay group-wide.

> `MemberMetrics.by_role` counts a member once **per role** they hold, so `sum(by_role.values()) >= total`. Read `total` and `by_role` independently — don't sum `by_role` to recover the headcount.

## Auth - access-aware metrics visibility

Metrics were once owner/admin-only. Now the **group configures its own audience**, and a member may be admitted to a *personal*-scoped view of their own data. Two axes drive it: a per-group access-level setting, and two object-scoped permissions.

**The group setting** — two NOT NULL columns on [EvaluationGroup](../data-models/evaluation-group.md), a `MetricsAccessLevel`:

- `metrics_access_during` — while the group runs.
- `metrics_access_after` — once it is finished (status `inactive`).

`MetricsAccessLevel` values: `owner_only`, `members_personal_metrics`, `all_members`, `inherit_group_access`. Deliberate default split — the **product default** is `members_personal_metrics`, but the **DB `server_default` is `owner_only`** (fail-closed backfill for pre-existing rows; the two are intentionally not aligned).

**The permissions**:

- `evaluation_groups:view_metrics` — the always-pass **FULL** capability (whole-event dashboards at every level). Conferred by the in-group **`owner`** role, held globally by **admin**, lifted by the `evaluation_groups:manage` break-glass. Kept distinct from `:update`.
- `evaluation_groups:view_personal_metrics` (**new**) — a gating permission read off the member's in-group roles; at the `members_personal_metrics` level it admits the member to the **PERSONAL**-scoped dashboards (their own submissions / conversations / reviews). Held by the `red_teamer` role today; a custom role opts in by carrying it, never by name. `viewer` deliberately lacks it (a viewer authors nothing, so a personal view would be empty).

**The policy** — `resolve_metrics_scope` (`app/core/evaluations/access.py`) returns a `MetricsScope` (`full` / `personal`) or `None` (deny). Visibility (the 404 gate) runs first and separately; this layers on top:

| Active level | owner / `manage` admin | member w/ `view_personal_metrics` | any other member | non-member who can see the group |
|---|---|---|---|---|
| `owner_only` | full | — (403) | — (403) | — (403) |
| `members_personal_metrics` | full | **personal** | — (403) | — (403) |
| `all_members` | full | full | full | — (403) |
| `inherit_group_access` | full | full | full | full |

The active level is `metrics_access_after` when the group is `inactive`, else `metrics_access_during`.

The group route gates via `GroupMetricsDep` (`require_group_metrics_access`), the evaluation route via `EvaluationMetricsDep` (`require_evaluation_metrics`, resolving the parent group's context). Both apply the `evaluation_groups:read` floor, then visibility (invisible → **404**), then the policy (denied → **403**). The resolved scope is carried as `viewer_id` (the caller's id on `personal`, else `None`), plus `full_model_access` on the group route (which decides whether `tokens_by_model` is served) and `mask_model_names` on the evaluation route. See [Evaluation domain](evaluation-domain.md), [RBAC - global roles](rbac-global-roles.md), and [Object roles - per-object permissions](object-roles-per-object-permissions.md).

## Related

- [Evaluation domain](evaluation-domain.md) — the groups / evaluations / scenarios / tasks the metrics roll up
- [Message flags](message-flags.md) — the submissions counted
- [Reviews - reviewer verdicts](reviews-reviewer-verdicts.md) — the verdict tallies (`successful_exploit`, `unique_exploit`, `valid_submission`)
- [Conversations](conversations.md) — the activity (conversations / messages) counted
- [Object roles - per-object permissions](object-roles-per-object-permissions.md) — where `evaluation_groups:view_metrics` is conferred
- [RBAC - global roles](rbac-global-roles.md) — the `view_metrics` / `manage` split
- [API - overview and conventions](api-overview-and-conventions.md)
