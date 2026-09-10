"""Password reset — sent by the self-service request endpoint and by the admin-triggered ones.

`triggered_by_admin` switches the copy for a reset the recipient never asked for;
it must not tell them they requested one.
"""

from datetime import datetime

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class PasswordResetContext(BaseModel):
    recipient_name: str | None
    reset_url: str
    expires_at: datetime
    triggered_by_admin: bool = False


template = EmailTemplate(name="password_reset", context_schema=PasswordResetContext)
