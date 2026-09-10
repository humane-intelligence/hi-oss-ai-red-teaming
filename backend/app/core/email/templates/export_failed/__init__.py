"""Export-failed notice — sent when a requested data-export job ends in a terminal failure."""

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class ExportFailedContext(BaseModel):
    recipient_name: str
    export_label: str
    error: str
    target_url: str


template = EmailTemplate(name="export_failed", context_schema=ExportFailedContext)
