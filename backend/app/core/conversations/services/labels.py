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
    """One label as read: the live category it sits under and the value set."""

    category_id: UUID
    name: str
    value: str


async def set_group_labels(session: AsyncSession, group: ConversationGroup, labels: Mapping[UUID, str]) -> None:
    """Replace the group's labels with `labels` (category id → value); a blank value clears that category.

    A category with values only takes one of them (matched ignoring case, stored as the category
    spells it); one without takes any single line of text.

    Raises:
        BadRequestError: If a category is not a live category of the group's evaluation, or a
            value is outside its category's list.
    """
    categories = {row.id: row for row in await list_label_categories(session, group.evaluation_id)}
    rows: list[ConversationGroupLabel] = []
    for category_id, raw in labels.items():
        category = categories.get(category_id)
        if category is None:
            raise BadRequestError(f"Label category {category_id} is not defined on this evaluation.")
        value = sanitise_single_line(raw)
        if not value:
            continue
        if category.values:
            match = next((allowed for allowed in category.values if allowed.casefold() == value.casefold()), None)
            if match is None:
                raise BadRequestError(f"{value!r} is not one of the values of label {category.name!r}.")
            value = match
        rows.append(ConversationGroupLabel(conversation_group_id=group.id, category_id=category_id, value=value))
    await session.execute(
        delete(ConversationGroupLabel).where(col(ConversationGroupLabel.conversation_group_id) == group.id)
    )
    session.add_all(rows)
    await session.flush()


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
        .order_by(func.lower(col(EvaluationLabelCategory.name)), col(EvaluationLabelCategory.id))
    )
    grouped: dict[UUID, list[GroupLabel]] = defaultdict(list)
    for group_id, category_id, name, value in result.all():
        grouped[group_id].append(GroupLabel(category_id=category_id, name=name, value=value))
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


def label_map(labels: list[GroupLabel]) -> dict[str, str]:
    """Labels as `{category name: value}` — the shape the exports print."""
    return {label.name: label.value for label in labels}
