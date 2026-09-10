"""Export-ready notice — sent when a requested data-export job finishes generating."""

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class ExportReadyContext(BaseModel):
    recipient_name: str
    export_label: str
    target_url: str
    retention_hours: int


template = EmailTemplate(name="export_ready", context_schema=ExportReadyContext)
