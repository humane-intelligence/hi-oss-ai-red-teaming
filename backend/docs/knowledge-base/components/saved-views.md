---
tags: [component, saved-views, ui]
aliases: [Saved views, SavedView, saved list views]
---

# Saved views

A user's named list-view state — filters, sort, search, hidden columns, pagination — persisted so they can recall an arrangement later. One table serves **every** savable console list; the backend never learns which list is which beyond an opaque key. This is a small, self-contained bounded context.

Code: `app/core/saved_views/` (logic) + `app/api/v1/saved_views.py` (HTTP, mounted under `/api/v1/saved-views`, tag `saved-views`). Table: [SavedView](../data-models/saved-view.md).

## Why it exists

Every list console (evaluations, flags, ai-models, users, …) has the same knobs: a filter set, an `order_by`, a search term, hidden columns, a page window. A saved view captures that arrangement under a name so the operator can jump back to it. The motivating use is a red-teamer or reviewer who keeps returning to the same filtered slice of a big list.

## One table, list-agnostic

The design decision that shapes everything: **no per-endpoint coupling.** A view carries a `resource` string key (which list) and a `state` JSONB envelope (the arrangement). On recall the frontend reads the state back and re-issues the *normal* list request built from it — the backend adds no special recall path. Making a new list savable is a one-line `SavedViewResource` enum member; no migration, no new route.

The `state` envelope is standardized by `SavedViewState` (`extra="forbid"`) but its values are the client's:

| Field | Meaning |
|---|---|
| `order_by` | sort key, `-` prefix = descending (pattern `^-?[a-zA-Z_]\w*$`) |
| `filters` | per-list filter values — an opaque `dict`, not interpreted by the backend |
| `hidden_columns` | ids of columns hidden in table views (visibility, not order) |
| `search` | free-text term (≤255) |
| `limit` / `offset` | pagination, mirroring the platform list params (`1–100` / `≥0`) |

The serialized envelope is capped at **64 KiB** on both create and update (`_assert_state_size`), so an authenticated caller can't persist a multi-MB or deeply-nested blob.

## Scoping — strictly personal, no break-glass

Every read and write is owner-scoped through the single predicate `_scope(statement, *, caller_id)` → `WHERE created_by_id == caller_id`. There is **no admin break-glass** and **no default-view concept** — a saved view is nobody else's business, so even an admin can't read another user's views, and there is no `is_default` flag or set-default route. Another user's view reads as **404** (no existence leak). Mutation paths load the row `for_update` (a row lock).

## Service — key functions

`app/core/saved_views/services/saved_views.py`:

| Function | What it does |
|---|---|
| `get_saved_view(session, view_id, *, caller_id, for_update=False)` | One **live** view scoped to the caller; foreign/absent → `NotFoundError`. |
| `list_saved_views(session, *, caller_id, filters, order_by, limit, offset)` | A page of the caller's live views, optionally narrowed to `filters.resource`. |
| `create_saved_view(session, draft, *, caller_id)` | Insert; a duplicate `(resource, name)` `IntegrityError` on the partial-unique index → `ConflictError` (409). |
| `update_saved_view(session, view, changes)` | Writes only `changes.model_fields_set` (omitted stays unchanged); `name`/`state` editable, `resource` immutable; dup → 409. |
| `soft_delete_saved_view(session, view)` | Stamps `deleted_at`, freeing the name for reuse. |

> The 409 message on update reads `changes.name`, not `view.name`: a failed flush expires the instance, so touching its attributes would reload on a poisoned session.

## Endpoints

`/api/v1/saved-views`, tag `saved-views`. Every route is permission-gated **and** owner-scoped by the service — the permission gates "can I manage my own views at all", the scope confines it to the caller's rows.

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/saved-views` | `saved_views:create` | 201 + `Location`; 409 on duplicate `(resource, name)` |
| GET | `/saved-views` | `saved_views:read` | `Page[SavedViewResponse]`; `?resource=` filter; `order_by` default `name` |
| GET | `/saved-views/{view_id}` | `saved_views:read` | 404 if not owned |
| PATCH | `/saved-views/{view_id}` | `saved_views:update` | replaces `state` wholesale; 404 / 409 |
| DELETE | `/saved-views/{view_id}` | `saved_views:delete` | 204 soft-delete |

`PATCH` replaces `state` in full (not a deep merge) and rejects an explicit `null` on `name`/`state` (both back NOT NULL columns) with 422 — omit a field to leave it unchanged. Per-resource listing is the `?resource=` filter, not a nested route.

## Permissions — every role, owner-scoped

The `saved_views:{read,create,update,delete}` family is held by **all** canonical roles (`admin`, `owner`, `red_teamer`, `annotator`, `viewer`) — like `organizations:read`, it is personal data anyone can manage. The [notification feed](notifications.md) is the other context built on the same "personal data, every role, owner-scoped, no break-glass" shape. See [RBAC - global roles](rbac-global-roles.md).

## Related

- [SavedView](../data-models/saved-view.md) — the `saved_views` table
- [Pagination, bulk and soft-delete](pagination-bulk-and-soft-delete.md) — the list vocabulary the `state` envelope mirrors
- [RBAC - global roles](rbac-global-roles.md) — the `saved_views:*` family (all roles)
- [API - overview and conventions](api-overview-and-conventions.md)
- [Data model overview](../data-models/data-model-overview.md)
