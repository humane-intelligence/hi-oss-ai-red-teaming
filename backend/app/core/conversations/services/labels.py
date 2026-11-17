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


async def set_group_labels(session: AsyncSession, group: ConversationGroup, labels: Mapping[UUID, str]) -> bool:
    """Replace the group's labels with `labels` (category id → value); a blank value clears that category.

    A category with values only takes one of them (matched ignoring case, stored as the category
    spells it), or the value the group already holds there — so dropping a value from a category's
    list never blocks relabelling the group's other categories; one without takes any single line
    of text.

    Returns:
        Whether the visible set changed; the group row itself is left alone, so a relabel bumps
        `updated_at` through `touch_conversation_group`.

    Raises:
        BadRequestError: If a non-blank value names a category that is not a live category of the
            group's evaluation, or is outside its category's list and not the one the group
            already holds there.
    """
    categories = {row.id: row for row in await list_label_categories(session, group.evaluation_id)}
    stored = await session.execute(
        select(col(ConversationGroupLabel.category_id), col(ConversationGroupLabel.value)).where(
            col(ConversationGroupLabel.conversation_group_id) == group.id
        )
    )
    # Rows under a deleted category are invisible already; they don't make an unchanged set a change.
    visible = {category_id: value for category_id, value in stored.tuples() if category_id in categories}
    rows: list[ConversationGroupLabel] = []
    for category_id, raw in labels.items():
        value = sanitise_single_line(raw)
        # Blank first: clearing a category that was deleted meanwhile is already true, not an error.
        if not value:
            continue
        category = categories.get(category_id)
        if category is None:
            raise BadRequestError("A label category is not defined on this evaluation; it may have been deleted.")
        if category.values:
            kept = visible.get(category_id)
            allowed_here = [*category.values, *([kept] if kept is not None else [])]
            match = next((allowed for allowed in allowed_here if allowed.casefold() == value.casefold()), None)
            if match is None:
                raise BadRequestError(f"{value!r} is not one of the values of label {category.name!r}.")
            value = match
        rows.append(ConversationGroupLabel(conversation_group_id=group.id, category_id=category_id, value=value))
    if visible == {row.category_id: row.value for row in rows}:
        return False
    await session.execute(
        delete(ConversationGroupLabel).where(col(ConversationGroupLabel.conversation_group_id) == group.id)
    )
    session.add_all(rows)
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
