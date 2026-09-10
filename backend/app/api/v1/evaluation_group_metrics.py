"""Evaluation-group metrics endpoint — `/api/v1/evaluation-groups/{group_id}/metrics`.

A single read that returns the whole-event aggregate: group-level roll-up plus
the per-evaluation / per-scenario / per-task breakdown and a day-by-day
submission timeline at both levels, so the frontend gets group- and
evaluation-level numbers in one call. The audience is configured per
group: `metrics_access_during` / `metrics_access_after` decide who may read the
numbers beside the always-passing owner and `evaluation_groups:manage` break-glass
(`GroupMetricsDep` → `resolve_metrics_scope`). A `members_personal_metrics` group
serves each `view_personal_metrics` member a `personal`-scope read filtered to
their own contributions (`GroupMetricsView.viewer_id`).
"""

from fastapi import APIRouter
from fastapi import status

from app.core.analytics.schemas import EvaluationGroupMetricsResponse
from app.core.analytics.services.metrics import evaluation_group_metrics
from app.core.dependencies import DbSession
from app.core.evaluations.dependencies import GroupMetricsDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter(prefix="/evaluation-groups/{group_id}/metrics", tags=["analytics"])


@router.get(
    "",
    response_model=EvaluationGroupMetricsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get evaluation-group metrics",
    description=(
        "Return aggregate metrics for a whole event (evaluation group): members by role, "
        "submission and review/exploit tallies, activity volume, a day-by-day submission timeline "
        "(`submissions_by_day`, with the exploits confirmed among each day's submissions), and a "
        "per-evaluation breakdown with by-scenario / by-task submission counts and its own "
        "timeline. The audience is set by the group's "
        "`metrics_access_during` / `metrics_access_after` levels; the group owner (and "
        "`evaluation_groups:manage` break-glass admins) can always read the full aggregate. At "
        "`members_personal_metrics`, a `view_personal_metrics` member gets a `personal`-scope read "
        "(the `scope` field) filtered to their own submissions, conversations, and reviews. "
        "`tokens` reports the group's model spend; `tokens_by_model` breaks it down per registry "
        "model and is served only to a full-access `evaluation_groups:view_metrics` caller, or when "
        "no evaluation in the group masks model names — a row correlates one model's cost across "
        "evaluations, which per-evaluation masking withholds. The `tokens` total is never withheld."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response(
            "Caller lacks the `evaluation_groups:read` floor, or does not pass the group's "
            "configured metrics-access level (and is neither its owner nor an "
            "`evaluation_groups:manage` break-glass admin)."
        ),
        status.HTTP_404_NOT_FOUND: problem_response("Evaluation group not found, or not visible to the caller."),
    },
)
async def get_evaluation_group_metrics_endpoint(
    view: GroupMetricsDep,
    db: DbSession,
) -> EvaluationGroupMetricsResponse:
    """Aggregate metrics for a whole event.

    Counts every member's submissions, reviews, and activity across the
    group's evaluations, and lays the submissions out day by day. Who may
    read them is the group's own call:
    `metrics_access_during` applies while the group runs, `metrics_access_after`
    once it is finished (`inactive`) — from `owner_only` up to
    `inherit_group_access` (whoever can see the group, per its `access_level`).
    The owner and break-glass admins always see the full aggregate; a
    `members_personal_metrics` member gets a `personal`-scope read of their own data.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluation_groups:read`, or can see the
      group but does not pass its configured metrics-access level.
    * **404 Not Found** — the group does not exist or is not visible to the caller.
    """
    return await evaluation_group_metrics(
        db, view.context.group, viewer_id=view.viewer_id, full_model_access=view.full_model_access
    )
