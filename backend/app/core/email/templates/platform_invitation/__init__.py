"""Platform invitation — sent by the invitation create endpoint."""

from datetime import datetime

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class PlatformInvitationContext(BaseModel):
    # Null for a self-service re-issue (`reissue_platform_invitation` with no
    # inviter): nobody triggered it on the account's behalf, so no name to carry.
    inviter_name: str | None = None
    accept_url: str
    expires_at: datetime
    role_names: list[str]


template = EmailTemplate(name="platform_invitation", context_schema=PlatformInvitationContext)
