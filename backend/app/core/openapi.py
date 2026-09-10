"""Central OpenAPI metadata: description, tags, and reusable error responses.

Kept separate from `app/main.py` so the values can be imported by tests and
by future tooling (e.g. a static spec dumper) without booting the full app.
"""

import os
from typing import Any

from fastapi import status

from app.core.schemas import Problem

APP_TITLE = "AI red teaming backend"

APP_SUMMARY = "Backend API for AI red-teaming workflows: conversations, evaluations, notes, analytics."


def display_version(git_sha: str) -> str:
    """Short SHA for display, or ``"dev"`` when unset (local / untagged build)."""
    return git_sha[:7] if git_sha else "dev"


def app_version() -> str:
    """OpenAPI ``info.version`` string.

    Reads ``os.environ`` (not ``Settings``): runs at app-construction before
    ``get_settings()`` and must import without a full env. On deploy the value is
    the runtime ``GIT_SHA`` that ``deploy.sh`` writes to ``.env``.
    """
    return display_version(os.environ.get("GIT_SHA", ""))


APP_DESCRIPTION = """
Backend service for AI red-teaming workflows.

## Conventions

- **Versioning.** Domain endpoints live under `/api/v1/...`. Health probes
  (`/health`, `/ready`) are top-level and unversioned.
- **Errors.** Every non-2xx response uses the RFC 7807 *Problem Details*
  envelope and is served with `Content-Type: application/problem+json`.
  See the `Problem` schema below.
- **Pagination.** List endpoints take `limit` (1..100, default 20) and
  `offset` (>=0, default 0) query parameters and return a `Page` wrapper
  with `items`, `total`, `limit`, and `offset`.
- **Content type.** Requests and successful responses use `application/json`.
- **Terms of service.** Once a version is published, every authenticated
  endpoint answers `403` with
  `type: urn:redteam:error:terms-acceptance-required` until the caller has
  accepted the current one. `GET /api/v1/auth/me` reports what is owed
  (`terms_acceptance_required`), `GET /api/v1/terms/current` and
  `GET /api/v1/terms/{terms_id}` serve the text, and
  `POST /api/v1/auth/me/terms` records the acceptance — those four keep
  answering, so the state is always clearable. Public endpoints are outside
  the gate entirely: they never resolve the dependency that enforces it,
  except where one authorises on identity rather than on a key: minting and
  fetching a signed URL for a *private* image both bind it to the caller, and
  both answer the same refusal. The refusal is cross-cutting and therefore documented here rather
  than on each endpoint.

## Errors

A typical error response looks like:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "User 7f3a not found.",
  "instance": "/api/v1/auth/users/7f3a"
}
```

Validation errors (422) additionally include an `errors` array with one
entry per offending field, matching Pydantic's `ValidationError.errors()`
shape (`loc`, `msg`, `type`).
"""

CONTACT: dict[str, str] = {
    "name": "AI Red Teaming Backend",
    "url": "https://github.com/humane-intelligence/hi-oss-ai-red-teaming",
}

LICENSE_INFO: dict[str, str] = {
    "name": "Apache 2.0",
    "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
}

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "auth", "description": "Bearer-session identity and login flows."},
    {"name": "users", "description": "User account management — CRUD over `User` records."},
    {"name": "roles", "description": "Role catalog — the assignable roles backing user role assignment."},
    {"name": "permissions", "description": "Permission catalog — every permission string with its description."},
    {
        "name": "organizations",
        "description": "Organizations — tenancy roots that scope evaluation groups; admin-managed.",
    },
    {
        "name": "invitations",
        "description": "Platform invitation lifecycle — issue, verify, accept.",
    },
    {
        "name": "models",
        "description": (
            "LLM model registry — provider endpoints, parameters, and credentials available to the platform."
        ),
    },
    {
        "name": "evaluations",
        "description": "Evaluations and their assigned models.",
    },
    {
        "name": "exports",
        "description": "CSV exports of evaluation data — templated, scoped to what the caller may read.",
    },
    {
        "name": "evaluation-groups",
        "description": "Evaluation groups — top-level red-teaming engagement containers.",
    },
    {
        "name": "scenarios",
        "description": "Scenarios — the individual, reorderable challenges that make up an evaluation.",
    },
    {
        "name": "tasks",
        "description": "Tasks — the simple name + description sub-level of a scenario.",
    },
    {
        "name": "conversations",
        "description": "Conversations — red-teaming sessions, their grouping, and message history.",
    },
    {
        "name": "chat",
        "description": "Streaming chat — token-by-token model replies over Server-Sent Events.",
    },
    {
        "name": "message-flags",
        "description": "Message flags — marking individual model responses as exploit-worthy.",
    },
    {
        "name": "notes",
        "description": "Notes — an annotator's free-text remarks on a selection of a conversation's messages.",
    },
    {
        "name": "annotations",
        "description": "Per-message annotations — one label on one message from one author, catalog-picked or ad hoc.",
    },
    {
        "name": "annotation-labels",
        "description": "The shared annotation-label vocabulary — the suggestions the label picker offers.",
    },
    {
        "name": "task-completions",
        "description": "Task completions — a red-teamer checking off a scenario's tasks per conversation.",
    },
    {
        "name": "reviews",
        "description": "Reviewer verdicts on flagged submissions, and the awaiting-review queue.",
    },
    {
        "name": "licenses",
        "description": "Data licenses — CRUD over the licenses an evaluation can be set to (curated + user-authored).",
    },
    {
        "name": "platform-settings",
        "description": (
            "Platform-wide settings singleton — admin read/override of platform policy (data "
            "licensing, registration), plus an unauthenticated signup-availability subset."
        ),
    },
    {
        "name": "terms",
        "description": (
            "Terms of service — versioned documents, the current one readable unauthenticated, plus admin publication."
        ),
    },
    {
        "name": "saved-views",
        "description": "Saved views — a user's named filter/sort/column state for any list view.",
    },
    {
        "name": "notifications",
        "description": "In-app notification feed — a user's own messages, with read/unread state.",
    },
    {
        "name": "analytics",
        "description": "Aggregate metrics and reporting over evaluation results.",
    },
    {
        "name": "audit",
        "description": "Audit log — append-only trail of sensitive actions and accesses (admin-only).",
    },
    {
        "name": "images",
        "description": "Generic image upload + serving (covers, avatars, icons) behind a swappable storage backend.",
    },
]
# Add a tag entry in the same diff that introduces the first endpoint of a new
# domain (auth, conversations, evaluations, …). Don't pre-register placeholders.


def problem_response(description: str) -> dict[str, Any]:
    """Build a `responses=` entry that points at the RFC 7807 `Problem` schema.

    `model=Problem` registers the schema in `components.schemas`; the explicit
    `content` block attaches a `$ref` to it under `application/problem+json`
    (the media type served by `ProblemResponse`). FastAPI also keeps the
    default `application/json` entry, but both point at the same schema.
    """
    return {
        "model": Problem,
        "description": description,
        "content": {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}},
        },
    }


# Baseline `responses=` for every operation: 422 (any validated input can
# trip Pydantic) and 500 (unhandled exception). Endpoints opt into other
# codes per route, e.g.:
#     responses=COMMON_ERROR_RESPONSES | {
#         status.HTTP_404_NOT_FOUND: problem_response("Thing not found."),
#     }
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: problem_response("Request failed validation."),
    status.HTTP_500_INTERNAL_SERVER_ERROR: problem_response("Unexpected server error."),
}

# Every authenticated route can answer this once a version of the terms is published, as can the
# private branch of a public route that authorises on identity (`api/v1/images.py`) — see the
# `Terms of service` convention in `APP_DESCRIPTION`. Routes that already declare a 403 for their
# permission gate keep that description; the map is keyed by status, and the convention carries the
# second reason. This is for the handful that could not 403 before and now can.
TERMS_REFUSED: dict[str, Any] = problem_response(
    "Caller has not accepted the current terms of service (`type: urn:redteam:error:terms-acceptance-required`)."
)

# The `text/event-stream` 200 for SSE endpoints, described by hand (SSE has no JSON body
# model). Pair it with `response_class=EventSourceResponse` on the route: that makes
# `text/event-stream` the success media type, so FastAPI does not also emit a phantom
# `application/json` for the 200. Shared by the streaming endpoints.
SSE_RESPONSE: dict[str, Any] = {
    "description": "Server-sent event stream: `delta` events, a terminal `done`, or an `error` event.",
    "content": {"text/event-stream": {"schema": {"type": "string"}}},
}
