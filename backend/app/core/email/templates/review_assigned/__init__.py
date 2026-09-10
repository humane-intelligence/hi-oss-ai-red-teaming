"""Reviewer-assignment notice — sent when a reviewer is assigned to a flagged submission."""

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class ReviewAssignedContext(BaseModel):
    assignee_name: str
    assigner_name: str
    evaluation_title: str


template = EmailTemplate(name="review_assigned", context_schema=ReviewAssignedContext)
