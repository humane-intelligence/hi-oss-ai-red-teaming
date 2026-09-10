---
tags: [component, annotations, conversations, evaluations]
aliases: [Task completions, checking off tasks, TaskCompletion]
---

# Task completions

A red-teamer checks off the scenario [tasks](../data-models/task.md) they've covered inside a conversation. The `annotations` context's second feature after [message flags](message-flags.md). It reuses the flag scoping model wholesale — owner + parent-group visibility — and adds nothing to the permission vocabulary.

Code: `app/core/annotations/services/task_completions.py` + `app/api/v1/task_completions.py`. Model: [TaskCompletion](../data-models/task-completion.md).

## Why it exists

A scenario is a checklist of tasks. Working a conversation against a model, the red-teamer marks which tasks they've probed. The per-conversation checklist is the source of truth; the group view rolls it up into a "how many conversations completed each task" progress read. Because a [Conversation](../data-models/conversation.md) has a single owner, "per conversation" is "per user" — no participants table, no shared checkbox.

## Toggle, not status

There is no status column: **a live row means done**, un-checking **soft-deletes** it. `PUT` is an idempotent check-off (an existing live row is returned, 200 — not 201), `DELETE` an idempotent un-check (a no-op when nothing is checked). Uniqueness is enforced per `(conversation, task)` over live rows by a partial-unique index (the owner is left out — the conversation already implies the user). See [TaskCompletion](../data-models/task-completion.md).

## Scoping — owner on writes, visibility on reads

`_scope` shares one implementation with `message_flags._scope` and `notes._scope` — the `join_conversation_scoped` helper in `app/core/evaluations/access.py`: `join_visible_evaluation_group` on the denormalised `evaluation_id` + a live `Conversation` join + `created_by_id == caller` (unless `can_manage`). Every child that denormalizes those three columns reads through it, so a predicate can't be added to one entity and forgotten on another.

- **Authoring is owner-only.** `complete_task` / `uncomplete_task` resolve the conversation through `resolve_conversation_context` (requires `Conversation.user_id == caller`); an unreachable conversation is 404. The `evaluation_groups:manage` break-glass does **not** lift this — a manager can read others' completions but never check off in someone else's conversation.
- **Reads take the break-glass.** The two GETs pass `can_manage`, so a manager sees any visible group's completions.
- Parent liveness (conversation / evaluation / group) is always enforced, so a soft-deleted ancestor hides the completion with no dedicated cascade hook — exactly like a flag.

`complete_task` validates the task is a **live** task of the conversation's scenario (`_assert_task_in_conversation_scenario`) → 404 otherwise; `uncomplete_task` skips that check (an unknown task is just a 204 no-op).

## Endpoints

The routes hang off the existing `/conversations` and `/conversation-groups` parents (the router carries no prefix of its own), tag `task-completions`, under `/api/v1`.

| Method | Path | Permission | Notes |
|---|---|---|---|
| PUT | `/conversations/{conversation_id}/completed-tasks/{task_id}` | `conversations:update` | check off (idempotent, 200); 404 if unreachable or task not in scenario |
| DELETE | `/conversations/{conversation_id}/completed-tasks/{task_id}` | `conversations:update` | un-check (idempotent, 204) |
| GET | `/conversations/{conversation_id}/completed-tasks` | `conversations:read` | the caller's completions for one conversation (unpaginated) |
| GET | `/conversation-groups/{conversation_group_id}/completed-tasks` | `conversations:read` | `GroupTaskCompletionRollup`: `total_conversations` + per-task counts |

Reads **never 404**: an unreachable conversation yields an empty list, an unreachable group rolls up as `total_conversations: 0` (never confirms a foreign resource's existence). The roll-up returns only tasks with ≥1 completion; the client defaults the rest to 0 (mirroring `flag_counts_by_message`).

> The group roll-up scopes by each conversation's **current** group (the live `Conversation` join), not the completion's denormalised `conversation_group_id` — so a conversation moved between groups counts correctly. The denormalised column is still surfaced on the response as a create-time snapshot.

## Permissions — reuse `conversations:*`

No new permission. Writes gate on `conversations:update`, reads on `conversations:read` — held by `red_teamer` and `admin`; `owner` / `annotator` / `viewer` hold neither, so they can't use these routes. The read break-glass is `evaluation_groups:manage` (admin). See [RBAC - global roles](rbac-global-roles.md).

## Related

- [TaskCompletion](../data-models/task-completion.md) — the `task_completions` table
- [Message flags](message-flags.md) — the sibling annotations feature whose scope model this reuses
- [Notes](notes.md) — the third user of the shared `join_conversation_scoped` scope
- [Task](../data-models/task.md) / [Scenario](../data-models/scenario.md) — the checklist being completed
- [Conversations](conversations.md) — the owner-scoped anchor
- [RBAC - global roles](rbac-global-roles.md) — the reused `conversations:*` split
- [API - overview and conventions](api-overview-and-conventions.md)
