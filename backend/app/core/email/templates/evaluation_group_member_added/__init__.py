"""Evaluation-group membership notice — sent when an active user is added to a group."""

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class EvaluationGroupMemberAddedContext(BaseModel):
    inviter_name: str
    group_title: str
    role_names: list[str]


template = EmailTemplate(name="evaluation_group_member_added", context_schema=EvaluationGroupMemberAddedContext)
