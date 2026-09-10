"""Send one sample of every registered email template through the configured backend.

Local styling-review helper: run against the compose Mailpit sink via
``make emailpreview`` (forces ``EMAIL_BACKEND=smtp`` + Mailpit connection env),
then eyeball the results at http://localhost:8025. Bypasses Celery and the
``outbound_emails`` audit table on purpose — this is a render+deliver preview,
not the production send path.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from app.core.config import get_settings
from app.core.email.backends import get_email_backend
from app.core.email.templates import TEMPLATES
from app.core.email.templates import render_template
from app.core.email.templates import validate_context

RECIPIENT = "preview@example.com"

_EXPIRES_AT = datetime.now(UTC) + timedelta(hours=24)

# A list value renders one preview per variant — for templates whose branches differ enough
# that eyeballing one of them says nothing about the other.
SAMPLE_CONTEXTS: dict[str, dict[str, Any] | list[dict[str, Any]]] = {
    "account_activated": {"user_name": "Ada Lovelace"},
    "email_verification": {
        "verify_url": "https://app.example.com/verify-email?token=sample-token",
        "expires_at": _EXPIRES_AT,
    },
    "evaluation_group_invitation": {
        "inviter_name": "Grace Hopper",
        "group_title": "Spring 2026 Safety Evaluation",
        "accept_url": "https://app.example.com/invitations/sample-token",
        "expires_at": _EXPIRES_AT,
        "role_names": ["Red Teamer", "Annotator"],
    },
    "evaluation_group_member_added": {
        "inviter_name": "Grace Hopper",
        "group_title": "Spring 2026 Safety Evaluation",
        "role_names": ["Red Teamer"],
    },
    "password_reset": {
        "recipient_name": "Ada Lovelace",
        "reset_url": "https://app.example.com/reset-password?token=sample-token",
        "expires_at": _EXPIRES_AT,
    },
    "platform_invitation": {
        "inviter_name": "Grace Hopper",
        "accept_url": "https://app.example.com/invitations/sample-token",
        "expires_at": _EXPIRES_AT,
        "role_names": ["Red Teamer"],
    },
    "review_assigned": {
        "assignee_name": "Ada Lovelace",
        "assigner_name": "Grace Hopper",
        "evaluation_title": "Prompt-Injection Robustness",
    },
    "review_unassigned": {
        "assignee_name": "Ada Lovelace",
        "assigner_name": "Grace Hopper",
        "evaluation_title": "Prompt-Injection Robustness",
    },
    "export_ready": {
        "recipient_name": "Ada Lovelace",
        "export_label": "Demo Evaluation - Conversations Report (CSV) · 2026-07-24 13:09 UTC",
        "target_url": "https://app.example.com/evaluations/3f2a0000-0000-0000-0000-000000000000",
        "retention_hours": 24,
    },
    "export_failed": {
        "recipient_name": "Ada Lovelace",
        "export_label": "Demo Evaluation - Flags Report (CSV) · 2026-07-24 13:09 UTC",
        "error": "Export generation failed.",
        "target_url": "https://app.example.com/evaluations/3f2a0000-0000-0000-0000-000000000000",
    },
    "model_inactivity_alert": [
        {
            "recipient_name": "Ada Lovelace",
            "model_name": "Llama 3.1 8B (self-hosted)",
            "idle_hours": 72,
            "last_used_at": datetime(2026, 8, 16, 9, 14, tzinfo=UTC),
            "last_warmup_at": datetime(2026, 8, 19, 10, 41, tzinfo=UTC),
            "still_warmed": True,
            "target_url": "https://app.example.com/ai-models/4b1c0000-0000-0000-0000-000000000000",
        },
        {
            "recipient_name": "Ada Lovelace",
            "model_name": "Llama 3.1 8B (self-hosted)",
            "idle_hours": 72,
            "last_used_at": datetime(2026, 8, 16, 9, 14, tzinfo=UTC),
            "last_warmup_at": None,
            "still_warmed": False,
            "target_url": "https://app.example.com/ai-models/4b1c0000-0000-0000-0000-000000000000",
        },
    ],
}


def main() -> None:
    backend_name = get_settings().email_backend
    if backend_name not in ("smtp", "console"):
        msg = f"Refusing to send previews via {backend_name!r} — a real provider would deliver test mail"
        raise SystemExit(msg)

    missing = TEMPLATES.keys() - SAMPLE_CONTEXTS.keys()
    if missing:
        msg = f"No sample context for template(s) {sorted(missing)} — add them to SAMPLE_CONTEXTS"
        raise SystemExit(msg)

    backend = get_email_backend()
    sent = 0
    for name in sorted(TEMPLATES):
        sample = SAMPLE_CONTEXTS[name]
        for context in sample if isinstance(sample, list) else [sample]:
            backend.send(render_template(name, RECIPIENT, validate_context(name, context)))
            sent += 1
            print(f"sent {name} -> {RECIPIENT}")  # noqa: T201
    print(f"{sent} emails sent — open http://localhost:8025")  # noqa: T201


if __name__ == "__main__":
    main()
