---
tags: [component, conversations]
aliases: [Conversation groups, conversation-groups]
---

# Conversation groups

The grouping bucket every [conversation](conversations.md) lives in — a **required** parent that bundles a red-teamer's conversations within one evaluation. Comparative model testing (one conversation per model on the same prompt, grouped here) is the motivating case, but a group is just a named bucket: it owns a `name` + the evaluation (and an optional shared scenario), while each member conversation keeps its own inference-params layer and message history. Routes: `app/api/v1/conversation_groups.py`; service: `app/core/conversations/services/groups.py`; model: [ConversationGroup](../data-models/conversation-group.md).

## Endpoints

Nested under the evaluation (like conversations) plus a flat cross-evaluation list. Reads gate on `conversations:read`, writes on the matching `conversations:{create,update,delete}`.

| Method + path | What | Permission |
|---|---|---|
| `POST /api/v1/scenarios/{scenario_id}/conversation-groups` | Create a group + **one conversation per `models[]` entry** (≥1; `evaluation_ai_model_id` + optional `parameters` + optional per-conversation `title`), `name`; the shared `scenario_id` comes from the path | `conversations:create` |
| `GET /api/v1/evaluations/{id}/conversation-groups` | List in the evaluation (`-created_at`); unknown/hidden evaluation → 404 | `conversations:read` |
| `GET /api/v1/evaluations/{id}/conversation-groups/{gid}` | One group + its member conversations | `conversations:read` |
| `PATCH /api/v1/evaluations/{id}/conversation-groups/{gid}` | **Rename only** (links + member set immutable) | `conversations:update` |
| `DELETE /api/v1/evaluations/{id}/conversation-groups/{gid}` | Soft-delete + **cascade** to member conversations | `conversations:delete` |
| `GET /api/v1/conversation-groups` | Flat cross-evaluation list, filters `evaluation_id` / `scenario_id` (hidden-eval filter → empty page, not 404) | `conversations:read` |

## Membership and invariants

A group never holds zero live conversations and never overflows:

- **Add** — the [conversation](conversations.md) create endpoint requires a `conversation_group_id`; the group is row-locked (`acquire_group_for_conversation`) and its size cap enforced.
- **Move** — the conversation PATCH moves a conversation to another group of the **same evaluation and the same scenario** (a group's conversations all share its scenario, so a target group on another scenario is a **409**); if the move empties the previous group, that group is soft-deleted (`soft_delete_empty_groups`).
- **Remove** — deleting a conversation soft-deletes it; if it was the group's last live member, the group is soft-deleted too.
- **Cap** — `MAX_CONVERSATION_GROUP_SIZE` (default 4) bounds members; a full target is a **409** on batch-create, add, and move. See [Configuration (Settings)](configuration-settings.md).

> **The create moved under the scenario.** `scenario_id` is now a required NOT NULL column on both `conversation_groups` and `conversations`, so the create is posted to `/api/v1/scenarios/{scenario_id}/conversation-groups` and the evaluation is derived from the resolved scenario — mirroring where the conversation create went. Every model entry is validated against that scenario's evaluation (a mismatch is a 404).

## Scope

The service scopes every read/write to the caller's own rows **and** the parent evaluation group's visibility via the shared `join_visible_evaluation_group`; the `evaluation_groups:manage` break-glass lifts both, so a manager sees and acts on any user's groups.

## Related

- [Conversations](conversations.md)
- [ConversationGroup](../data-models/conversation-group.md)
- [Evaluation domain](evaluation-domain.md)
- [Scenario](../data-models/scenario.md) — the challenge a group is created under
- [Restore - reading tombstones back](restore-soft-deleted-items.md) — restoring the last member revives a pruned group
- [Configuration (Settings)](configuration-settings.md)
- [API - overview and conventions](api-overview-and-conventions.md)
