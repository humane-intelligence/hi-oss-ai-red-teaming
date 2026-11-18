"""A conversation group's labels — values set under its evaluation's label categories.

Labels are read and written here only; nothing in this module feeds a model prompt.
"""

from collections import defaultdict
from collections.abc import Collection
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import ConversationGroupLabel
from app.core.evaluations.models import EvaluationLabelCategory
from app.core.evaluations.services.label_categories import list_label_categories
from app.core.exceptions import BadRequestError
from app.core.helpers import sanitise_single_line


@dataclass(frozen=True, slots=True)
class GroupLabel:
    """One label as read: the live category it sits under and one value set there.

    `value` is `None` for a category selected on its own, which is all a category without values takes.
    """

    category_id: UUID
    name: str
    value: str | None


# How the bare selection of a category is stored: `value` is part of the primary key, so it cannot be null.
_BARE = ""


async def set_group_labels(
    session: AsyncSession, group: ConversationGroup, labels: Mapping[UUID, Collection[str]]
) -> bool:
    """Replace the group's labels with `labels` (category id → the values picked there).

    A category with values takes any number of them (matched ignoring case, stored as the category
    spells them). One without values is a marker: listing it, with no values, selects it. Either
    kind also accepts a value the group already holds there — a value later dropped from the
    category's list, or free text from before markers — so an old label never blocks relabelling
    the rest. A category left out, or one with values listed with none, is cleared.

    Returns:
        Whether the visible set changed; the group row itself is left alone, so a relabel bumps
        `updated_at` through `touch_conversation_group`.

    Raises:
        BadRequestError: If a category named with something to store is not a live category of the
            group's evaluation, or a value is outside its category's list and not one the group
            already holds there.
    """
    categories = {row.id: row for row in await list_label_categories(session, group.evaluation_id)}
    stored = await session.execute(
        select(col(ConversationGroupLabel.category_id), col(ConversationGroupLabel.value)).where(
            col(ConversationGroupLabel.conversation_group_id) == group.id
        )
    )
    # Rows under a deleted category are invisible already; they don't make an unchanged set a change.
    visible = {(category_id, value) for category_id, value in stored.tuples() if category_id in categories}
    wanted: set[tuple[UUID, str]] = set()
    for category_id, raw_values in labels.items():
        values = [value for value in (sanitise_single_line(raw) for raw in raw_values) if value]
        category = categories.get(category_id)
        if category is None:
            # Nothing to store first: clearing a category that was deleted meanwhile is already true.
            if not values:
                continue
            raise BadRequestError("A category is not defined on this evaluation; it may have been deleted.")
        held = [value for held_category, value in visible if held_category == category_id and value != _BARE]
        if not category.values and not values:
            wanted.add((category_id, _BARE))
        for value in values:
            match = next(
                (allowed for allowed in [*category.values, *held] if allowed.casefold() == value.casefold()), None
            )
            if match is None:
                raise BadRequestError(f"{value!r} is not one of the values of label {category.name!r}.")
            wanted.add((category_id, match))
    if wanted == visible:
        return False
    await session.execute(
        delete(ConversationGroupLabel).where(col(ConversationGroupLabel.conversation_group_id) == group.id)
    )
    session.add_all(
        ConversationGroupLabel(conversation_group_id=group.id, category_id=category_id, value=value)
        for category_id, value in sorted(wanted, key=lambda pair: (str(pair[0]), pair[1]))
    )
    await session.flush()
    return True


async def labels_by_group(session: AsyncSession, group_ids: Collection[UUID]) -> dict[UUID, list[GroupLabel]]:
    """The labels of each group under live categories, by category name — one query for any number of groups."""
    if not group_ids:
        return {}
    result = await session.execute(
        select(
            col(ConversationGroupLabel.conversation_group_id),
            col(EvaluationLabelCategory.id),
            col(EvaluationLabelCategory.name),
            col(ConversationGroupLabel.value),
        )
        .join(EvaluationLabelCategory, col(EvaluationLabelCategory.id) == col(ConversationGroupLabel.category_id))
        .where(
            col(ConversationGroupLabel.conversation_group_id).in_(group_ids),
            col(EvaluationLabelCategory.deleted_at).is_(None),
        )
        .order_by(
            func.lower(col(EvaluationLabelCategory.name)),
            col(EvaluationLabelCategory.id),
            func.lower(col(ConversationGroupLabel.value)),
        )
    )
    grouped: dict[UUID, list[GroupLabel]] = defaultdict(list)
    for group_id, category_id, name, value in result.all():
        grouped[group_id].append(GroupLabel(category_id=category_id, name=name, value=value or None))
    return grouped


async def labels_by_conversation(
    session: AsyncSession, conversation_ids: Collection[UUID]
) -> dict[UUID, list[GroupLabel]]:
    """The labels each conversation carries through its group — for read paths keyed by conversation."""
    if not conversation_ids:
        return {}
    pairs = (
        await session.execute(
            select(col(Conversation.id), col(Conversation.conversation_group_id)).where(
                col(Conversation.id).in_(conversation_ids)
            )
        )
    ).all()
    by_group = await labels_by_group(session, {group_id for _, group_id in pairs})
    return {conversation_id: by_group.get(group_id, []) for conversation_id, group_id in pairs}


def label_map(labels: list[GroupLabel]) -> dict[str, list[str]]:
    """Labels as `{category name: [values]}` — the shape the exports print; a bare category has an empty list."""
    mapped: dict[str, list[str]] = {}
    for label in labels:
        values = mapped.setdefault(label.name, [])
        if label.value is not None:
            values.append(label.value)
    return mapped
