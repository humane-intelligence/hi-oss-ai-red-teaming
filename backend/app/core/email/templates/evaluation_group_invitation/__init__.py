"""Evaluation-group invitation — sent when a new/onboarding user is invited to a group."""

from datetime import datetime

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class EvaluationGroupInvitationContext(BaseModel):
    inviter_name: str
    group_title: str
    accept_url: str
    expires_at: datetime
    role_names: list[str]


template = EmailTemplate(name="evaluation_group_invitation", context_schema=EvaluationGroupInvitationContext)
