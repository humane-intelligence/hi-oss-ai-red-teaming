---
tags: [component, notifications, ui]
aliases: [Notifications, in-app notifications, notification feed, bell]
---

# Notifications (in-app feed)

A per-user in-app message feed: the bell in the console, its unread badge, and the list behind it. A notification is delivered to exactly one user, carries a short `name` + optional `description`, and optionally points at a domain row so the UI can deep-link. Strictly personal data — like [Saved views](saved-views.md), nobody (not even an admin) reads another user's feed.

Code: `app/core/notifications/` (logic) + `app/api/v1/notifications.py` (HTTP, mounted under `/api/v1/notifications`, tag `notifications`). Table: [Notification](../data-models/notification.md).

## Why it exists

Some events concern a user who isn't in the request that caused them: their evaluation was approved by a moderator, their group was rejected, the export they asked for finished generating. Email alone is a poor fit (it leaves the product), so those events also land as a row the console can show on the next page load.

## No HTTP create — rows are minted internally

There is deliberately **no** `POST /notifications`. A caller can read their feed and flip its read state, nothing else. Two internal writers exist: the async service function `create_notification` (the normal path) and one **direct insert** from the email worker (`app/core/email/tasks.py`), which runs on a sync session and therefore cannot call it.

`app/core/notifications/services/notifications.py`
```python
async def create_notification(
    session: AsyncSession,
    *,
    user_id: UUID,
    name: str,
    description: str | None = None,
    object_type: NotificationObjectType | None = None,
    object_id: UUID | None = None,
) -> Notification:
    ...
    session.add(notification)
    await session.flush()
```

It **flushes but never commits**, so the notification lands atomically with the domain transaction that emitted it — a rolled-back verdict or export finalize announces nothing.

## Who emits what

| Emitter | Event | `object_type` | Guard |
|---|---|---|---|
| `evaluations/services/approval.py` | evaluation approved / rejected | `evaluation` | skipped when the actor **is** the owner (a self-verdict is not news — admin holds both `evaluations:create` and `evaluations:approve`) |
| `evaluations/services/publication.py` (`_notify_owner`) | group approved / changes requested / rejected | `evaluation_group` | same actor-is-owner skip |
| `exports/notifications.py` (`emit_export_completion`) | export ready / failed / timed out | `evaluation` or `evaluation_group` (the job's scope) | none — the requester asked for the export, so they are always the intended recipient |
| `email/__init__.py` (`send_email_best_effort`) | a mail was never queued (render blew up in the savepoint) | none | only when the send names a `requested_by_user_id` **and** carries no `batch_key` (a batch caller aggregates its own notice) |
| `email/tasks.py` (`_notify_requester`) | delivery finally failed (non-transient, or retries exhausted) | none | only on the `failed` **transition**, and only for the first failing row of a `batch_key` |
| `ai_gateway/services/inactivity.py` (`check_model_inactivity`) | a warmup-enabled model has gone unused past its own `inactivity_alert_hours` | `ai_model` (deep-links the model page) | once per episode, and one row per recipient — every holder of `models:update` + `models:read` |

The export path is the fullest example: one audit row + one notification + one email per terminal transition, all no-commit so they land with the status flip. See [Exports (CSV / JSON)](exports.md). The two email emitters are the mirror case — telling the *requester* that a mail they triggered will not arrive; details and the one-notice-per-batch rule in [Email](email.md).

## Scoping — owner-only, no break-glass

Every read and the mark write go through one predicate:

```python
def _scope(statement, *, user_id: UUID):
    return statement.where(col(Notification.user_id) == user_id)
```

Another user's notification reads as **404** (no existence leak). A filter can never widen the scope — the service applies it regardless of what the query params say.

## Marking read/unread

`POST /notifications/mark` is a set-based bulk flip, not the platform `BulkRequest` envelope: `ids` selects rows, an **empty `ids` means all of the caller's**, and `read` is the target state. The `UPDATE` matches only rows in the *opposite* state, so:

- an already-read row keeps its original `read_at` (re-marking doesn't restamp it),
- the returned `updated` count reflects real state changes, not rows touched.

`read_at` is the storage; the wire exposes both `read_at` and the derived boolean `read`.

## Endpoints

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/notifications` | `notifications:read` | `Page[NotificationResponse]`, newest-first (`order_by` ∈ `created_at` / `-created_at`, default `-created_at`); filters `read` (bool) and `object_type` |
| POST | `/notifications/mark` | `notifications:update` | `{ids, read}` → `{updated}`; empty `ids` = all |
| GET | `/notifications/{notification_id}` | `notifications:read` | 404 when not the caller's |

**The unread count is `total` on `?read=false`** — there is no separate count endpoint, because `total` already reflects the filtered set.

## Permissions — every role, owner-scoped

Only two members: `notifications:read` and `notifications:update`, held by **all** canonical roles (`admin`, `owner`, `red_teamer`, `annotator`, `viewer`). No `create` (rows are internal) and no `delete` (there is no user-facing delete). Same rationale as `saved_views:*`. See [RBAC - global roles](rbac-global-roles.md).

## Related

- [Notification](../data-models/notification.md) — the `notifications` table
- [Saved views](saved-views.md) — the other strictly-personal, all-roles context
- [Evaluation domain](evaluation-domain.md) — the approval/publication verdicts that emit notifications
- [Exports (CSV / JSON)](exports.md) — the completion announcement (audit + notification + email)
- [Email](email.md) — the mail side of the same events
- [RBAC - global roles](rbac-global-roles.md) — `notifications:*`
- [API - overview and conventions](api-overview-and-conventions.md)
- [Data model overview](../data-models/data-model-overview.md)
