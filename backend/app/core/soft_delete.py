"""Per-statement soft-delete filtering — no global state, no listener.

`BaseModel.live_select` / `live_update` pre-attach `with_live(cls)` for the
target class. When one statement loads more than one soft-delete-aware model
(e.g. an eager-loaded relationship), attach `with_live(OtherModel)` to
`.options(...)` per extra class:

    stmt = User.live_select().options(
        selectinload(User.roles),
        with_live(Role),
    )

Raw Core (`text(...)`, plain `Table.update()`) bypasses the ORM execute
pipeline and is NOT filtered — add the predicate by hand.

To query tombstoned rows, use [restore.py](restore.py) (window- and
deleter-scoped) or write plain `select(Model)` / `update(Model)`.
"""

from typing import TYPE_CHECKING

from sqlalchemy.orm import LoaderCriteriaOption
from sqlalchemy.orm import with_loader_criteria
from sqlmodel import col

if TYPE_CHECKING:
    from app.core.base_model import BaseModel


def with_live(model: type[BaseModel]) -> LoaderCriteriaOption:
    """`with_loader_criteria` option filtering soft-deleted rows of `model`.

    Attach to `.options(...)` when a statement loads more than one soft-
    delete-aware model — each class needs its own criteria. `include_aliases`
    is on so the predicate follows relationship joins and aliased uses.

    The criteria lambda is intentionally unannotated: SQLAlchemy copies
    `__annotations__` off the callable when instrumenting it, which would
    eagerly resolve a `BaseModel` reference here and break the import cycle
    with [base_model.py](base_model.py).
    """
    return with_loader_criteria(
        model,
        lambda cls: col(cls.deleted_at).is_(None),
        include_aliases=True,
    )
