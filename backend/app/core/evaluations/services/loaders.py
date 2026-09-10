"""Shared eager-load builders for evaluation reads.

Lives apart from the `evaluations` / `evaluation_groups` services so both can
import it without those two importing each other (which would cycle:
`evaluations` needs `assert_group_write_access` from `evaluation_groups`, and
`evaluation_groups` needs this loader for the detail embed). Pure query options —
no session, no I/O.
"""

from sqlalchemy.orm import Load
from sqlalchemy.orm import selectinload

from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel


def evaluation_models_loader() -> Load:
    """Loader chain for an `Evaluation`'s live assignments and each one's `ai_model`.

    Single source of the `Evaluation.models -> ai_model` eager-load, reused by the
    standalone evaluation reads (`services.evaluations`) and the group-detail embed
    one hop up (`selectinload(EvaluationGroup.evaluations).options(...)` in
    `services.evaluation_groups`). Pair with statement-level
    `with_live(EvaluationAiModel)`; grow this when the projection needs another
    relationship so both read paths stay in sync.
    """
    # Two independent stub gaps suppressed here:
    #   invalid-argument-type — the `InstrumentedAttribute` args (`Evaluation.models`,
    #     `EvaluationAiModel.ai_model`) are narrower than the stub's declared param.
    #   invalid-return-type — the chained `.selectinload` is typed as `_AbstractLoad`,
    #     but the runtime value is the concrete `Load` this function returns.
    return selectinload(Evaluation.models).selectinload(EvaluationAiModel.ai_model)  # ty: ignore[invalid-argument-type, invalid-return-type]
