"""Account-activated email.

Sent when an onboarding account (email verification, invitation accept, or
first OIDC login) becomes active.
"""

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class AccountActivatedContext(BaseModel):
    """Context for the `account_activated` template."""

    user_name: str
    # Only true on the OIDC path — activation there also clears any password already
    # on the row (planted-credential defense); the body notes it so the recipient isn't
    # left wondering why their old password stopped working.
    password_cleared: bool = False


template = EmailTemplate(name="account_activated", context_schema=AccountActivatedContext)
