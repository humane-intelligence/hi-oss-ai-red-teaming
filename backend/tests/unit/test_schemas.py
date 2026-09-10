"""Pure-logic tests for shared schemas (`Problem`, `Page`, `PaginationParams`)."""

import pytest
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import BaseModel
from pydantic import ValidationError

from app.core.schemas import Page
from app.core.schemas import PaginationParams
from app.core.schemas import Problem
from app.core.schemas import ProblemErrorItem


class ProblemFactory(ModelFactory[Problem]):
    __model__ = Problem


class ItemFactory(ModelFactory[ProblemErrorItem]):
    __model__ = ProblemErrorItem


class _SmallItem(BaseModel):
    id: int
    label: str


class _SmallItemFactory(ModelFactory[_SmallItem]):
    __model__ = _SmallItem


@pytest.mark.unit
def test_problem_roundtrips_through_json() -> None:
    problem = ProblemFactory.build()

    assert Problem.model_validate(problem.model_dump()) == problem


@pytest.mark.unit
def test_problem_error_item_roundtrips() -> None:
    item = ItemFactory.build()

    assert ProblemErrorItem.model_validate(item.model_dump()) == item


@pytest.mark.unit
def test_page_holds_typed_items() -> None:
    items = _SmallItemFactory.batch(3)

    page = Page[_SmallItem](items=items, total=10, limit=3, offset=0)

    assert page.items == items
    assert page.total == 10


@pytest.mark.unit
def test_pagination_params_defaults() -> None:
    params = PaginationParams()

    assert params.limit == 20
    assert params.offset == 0


@pytest.mark.unit
@pytest.mark.parametrize("limit", [0, -1, 101, 9999])
def test_pagination_params_rejects_out_of_range_limit(limit: int) -> None:
    with pytest.raises(ValidationError):
        PaginationParams(limit=limit)


@pytest.mark.unit
@pytest.mark.parametrize("offset", [-1, -100])
def test_pagination_params_rejects_negative_offset(offset: int) -> None:
    with pytest.raises(ValidationError):
        PaginationParams(offset=offset)
