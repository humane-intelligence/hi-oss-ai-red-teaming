"""Reviewer-unassignment notice — sent when a reviewer is removed from a flagged submission."""

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class ReviewUnassignedContext(BaseModel):
    assignee_name: str
    assigner_name: str
    evaluation_title: str


template = EmailTemplate(name="review_unassigned", context_schema=ReviewUnassignedContext)
