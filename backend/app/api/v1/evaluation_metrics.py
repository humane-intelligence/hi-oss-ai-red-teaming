"""Single-evaluation metrics endpoint — `/api/v1/evaluations/{evaluation_id}/metrics`.

Mirrors the group dashboard scoped to one evaluation: the same submission / review / activity /
scenario roll-up and daily submission timeline, plus the evaluation-specific exploit
distributions (by prompt count, by model).
Gated like the group dashboard (`EvaluationMetricsDep`): the parent group's configured
`metrics_access_during` / `metrics_access_after` levels decide the audience, including the
`members_personal_metrics` `personal`-scope read. Model names in the by-model distribution are
masked for any caller who isn't a full-access `view_metrics` holder when the evaluation masks models.
"""

from fastapi import APIRouter
from fastapi import status

from app.core.analytics.schemas import EvaluationMetricsResponse
from app.core.analytics.services.metrics import evaluation_metrics
from app.core.dependencies import DbSession
from app.core.evaluations.dependencies import EvaluationMetricsDep
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response

router = APIRouter(prefix="/evaluations/{evaluation_id}/metrics", tags=["analytics"])


@router.get(
    "",
    response_model=EvaluationMetricsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get evaluation metrics",
    description=(
        "Return aggregate metrics for a single evaluation: submission and review/exploit tallies, "
        "activity volume, token spend (whole-evaluation, per scenario, and per assigned model), a "
        "day-by-day submission timeline (`submissions_by_day`, over the parent group's declared span "
        "widened by this evaluation's own submissions), and the exploit distributions by prompt count "
        "and by assigned model — the prompt-count buckets also carry what an exploit cost to reach. "
        "The audience is set by the parent group's "
        "`metrics_access_during` / `metrics_access_after` levels; the group owner (and "
        "`evaluation_groups:manage` break-glass admins) can always read the full aggregate. At "
        "`members_personal_metrics`, a `view_personal_metrics` member gets a `personal`-scope read "
        "(the `scope` field) of their own data. Model names in the by-model distribution are shown "
        "as their display mask to any non-owner/admin caller when the evaluation masks models."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response(
            "Caller lacks the `evaluation_groups:read` floor, or does not pass the parent group's "
            "configured metrics-access level (and is neither its owner nor an "
            "`evaluation_groups:manage` break-glass admin)."
        ),
        status.HTTP_404_NOT_FOUND: problem_response("Evaluation not found, or its group is not visible to the caller."),
    },
)
async def get_evaluation_metrics_endpoint(
    view: EvaluationMetricsDep,
    db: DbSession,
) -> EvaluationMetricsResponse:
    """Aggregate metrics for a single evaluation.

    Counts every member's submissions, reviews, and activity on this evaluation. The parent
    group's `metrics_access_during` / `metrics_access_after` levels decide who may read it; the
    group's owner and break-glass `evaluation_groups:manage` admins always see the full aggregate,
    a `members_personal_metrics` member sees a `personal`-scope read of their own data, and model
    names are masked for any non-owner/admin caller when the evaluation masks models.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `evaluation_groups:read`, or can see the
      group but does not pass its configured metrics-access level.
    * **404 Not Found** — the evaluation does not exist, or its group is not visible to the caller.
    """
    return await evaluation_metrics(
        db,
        view.evaluation,
        group=view.context.group,
        viewer_id=view.viewer_id,
        mask_model_names=view.mask_model_names,
    )
