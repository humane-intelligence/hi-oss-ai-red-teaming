"""Model-inactivity notice — sent to admins when a warmup-enabled model has gone unused."""

from datetime import datetime

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class ModelInactivityAlertContext(BaseModel):
    recipient_name: str
    model_name: str
    idle_hours: int
    last_used_at: datetime | None
    last_warmup_at: datetime | None
    still_warmed: bool
    target_url: str


template = EmailTemplate(name="model_inactivity_alert", context_schema=ModelInactivityAlertContext)
