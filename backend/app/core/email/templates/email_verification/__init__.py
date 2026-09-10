"""Email verification — sent by the self-registration endpoint."""

from datetime import datetime

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class EmailVerificationContext(BaseModel):
    verify_url: str
    expires_at: datetime


template = EmailTemplate(name="email_verification", context_schema=EmailVerificationContext)
