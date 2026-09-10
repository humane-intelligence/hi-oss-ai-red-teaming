"""Tests for app.core.email.templates — registry, render, validate."""

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import TemplateNotFound
from jinja2 import UndefinedError
from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import ValidationError

from app.core.email import templates as templates_module
from app.core.email.templates import TEMPLATES
from app.core.email.templates import render_template
from app.core.email.templates import validate_context


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "email_from": "noreply@example.com",
        "frontend_base_url": "https://app.example.com",
        "email_footer_address": None,
        "brand_company": "Humane Intelligence",
        "brand_product": "AI Red Teaming",
    }
    return SimpleNamespace(**{**base, **overrides})


@pytest.mark.unit
def test_validate_context_returns_normalized_dict() -> None:
    out = validate_context("account_activated", {"user_name": "Ada"})

    assert out == {"user_name": "Ada", "password_cleared": False}


@pytest.mark.unit
def test_validate_context_rejects_missing_field() -> None:
    with pytest.raises(ValidationError):
        validate_context("account_activated", {})


@pytest.mark.unit
def test_validate_context_unknown_template_raises_key_error() -> None:
    with pytest.raises(KeyError, match="Unknown email template"):
        validate_context("does-not-exist", {})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "context", "expected"),
    [
        pytest.param(
            "platform_invitation",
            {
                "inviter_name": "Bob",
                "accept_url": "https://app.example.com/accept-invitation?token=abc",
                "expires_at": "2026-05-29T10:00:00Z",
                "role_names": ["annotator"],
            },
            {
                "inviter_name": "Bob",
                "accept_url": "https://app.example.com/accept-invitation?token=abc",
                "expires_at": "2026-05-29T10:00:00Z",
                "role_names": ["annotator"],
            },
            id="platform_invitation",
        ),
        pytest.param(
            "email_verification",
            {
                "verify_url": "https://app.example.com/verify-email?token=abc",
                "expires_at": "2026-05-29T10:00:00Z",
            },
            {
                "verify_url": "https://app.example.com/verify-email?token=abc",
                "expires_at": "2026-05-29T10:00:00Z",
            },
            id="email_verification",
        ),
    ],
)
def test_validate_context_normalizes_datetime(template: str, context: dict, expected: dict) -> None:
    # validate_context runs model_dump(mode="json") so the datetime crosses the
    # Celery broker as an ISO string — verify that's what comes out, exactly
    # (no stray keys in the normalized context).
    out = validate_context(template, context)

    assert out == expected


@pytest.mark.unit
def test_render_account_activated_produces_subject_and_bodies() -> None:
    message = render_template("account_activated", "rcpt@example.com", {"user_name": "Ada"})

    assert message.to == "rcpt@example.com"
    assert message.subject == "Welcome to AI Red Teaming, Ada"
    assert "Hi Ada" in message.body_text
    assert "Hi Ada" in message.body_html
    assert "<p " in message.body_html
    assert message.from_addr == "noreply@example.com"
    # Omitted entirely (as opposed to explicitly `False`) — `render_template` doesn't
    # apply the schema's default, so the template must tolerate that itself.
    assert "clears any password" not in message.body_text


@pytest.mark.unit
def test_render_account_activated_notes_the_cleared_password_when_flagged() -> None:
    message = render_template("account_activated", "rcpt@example.com", {"user_name": "Ada", "password_cleared": True})

    assert "clears any password" in message.body_text
    assert "clears any password" in message.body_html


@pytest.mark.unit
def test_render_wraps_html_in_base_frame_with_inline_logo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(templates_module, "get_settings", _settings)

    message = render_template("account_activated", "rcpt@example.com", {"user_name": "Ada"})

    assert "<!DOCTYPE html>" in message.body_html
    assert 'src="cid:logo"' in message.body_html
    # Footer brand line + site label derived from frontend_base_url.
    assert "Humane Intelligence" in message.body_html
    assert "app.example.com" in message.body_html
    assert [image.cid for image in message.inline_images] == ["logo"]
    assert message.inline_images[0].data[:4] == b"\x89PNG"


@pytest.mark.unit
def test_render_footer_address_shown_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(templates_module, "get_settings", lambda: _settings(email_footer_address="1 Example St, City"))
    with_address = render_template("account_activated", "rcpt@example.com", {"user_name": "Ada"})
    assert "1 Example St, City" in with_address.body_html
    assert "1 Example St, City" in with_address.body_text

    monkeypatch.setattr(templates_module, "get_settings", lambda: _settings(email_footer_address=None))
    without_address = render_template("account_activated", "rcpt@example.com", {"user_name": "Ada"})
    assert "Example St" not in without_address.body_html
    assert "Example St" not in without_address.body_text


@pytest.mark.unit
def test_render_unknown_template_raises() -> None:
    with pytest.raises(TemplateNotFound):
        render_template("nope", "rcpt@example.com", {})


@pytest.mark.unit
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_render_missing_context_field_raises(template: str) -> None:
    # Registry-driven, so a newly registered template is covered automatically.
    # StrictUndefined surfaces template bugs instead of silently emitting "".
    with pytest.raises(UndefinedError):
        render_template(template, "rcpt@example.com", {})


@pytest.mark.unit
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_registered_template_renders_with_valid_context(template: str) -> None:
    # Registry-driven render smoke: every registered template must produce a
    # subject and both bodies from any context its schema validates.
    context = ModelFactory.create_factory(TEMPLATES[template].context_schema).build().model_dump(mode="json")

    message = render_template(template, "rcpt@example.com", context)

    assert message.subject.strip()
    assert message.body_text.strip()
    assert message.body_html.strip()


@pytest.mark.unit
def test_render_html_escapes_context_value() -> None:
    message = render_template("account_activated", "rcpt@example.com", {"user_name": "<script>"})

    assert "<script>" not in message.body_html
    assert "&lt;script&gt;" in message.body_html
    # Plain-text body must NOT be escaped — autoescape only applies to .html.
    assert "<script>" in message.body_text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "context", "subject_fragment", "body_fragments", "forbidden_fragments"),
    [
        pytest.param(
            "platform_invitation",
            {
                "inviter_name": "Bob Smith",
                "accept_url": "https://app.example.com/accept-invitation?token=abc",
                "expires_at": "2026-05-29T10:00:00Z",
                "role_names": ["annotator", "owner"],
            },
            "Bob Smith invited you",
            [
                "Bob Smith",
                "annotator, owner",
                "https://app.example.com/accept-invitation?token=abc",
                # The datetimefmt filter formats the ISO string for users; raw form must not leak.
                "May 29, 2026 at 10:00 UTC",
            ],
            ["2026-05-29T10:00:00Z"],
            id="platform_invitation",
        ),
        pytest.param(
            "email_verification",
            {
                "verify_url": "https://app.example.com/verify-email?token=abc",
                "expires_at": "2026-05-29T10:00:00Z",
            },
            "Verify your email address - AI Red Teaming",
            ["https://app.example.com/verify-email?token=abc", "May 29, 2026 at 10:00 UTC"],
            ["2026-05-29T10:00:00Z"],
            id="email_verification",
        ),
        pytest.param(
            "password_reset",
            {
                "recipient_name": "Ada Lovelace",
                "reset_url": "https://app.example.com/reset?token=abc",
                "expires_at": "2026-05-26T18:00:00Z",
                "triggered_by_admin": False,
            },
            "Reset your AI Red Teaming password",
            ["Ada Lovelace", "https://app.example.com/reset?token=abc", "May 26, 2026 at 18:00 UTC"],
            ["2026-05-26T18:00:00Z"],
            id="password_reset",
        ),
        pytest.param(
            "evaluation_group_invitation",
            {
                "inviter_name": "Bob Smith",
                "group_title": "Spring Engagement",
                "accept_url": "https://app.example.com/accept-invitation?token=abc",
                "expires_at": "2026-06-17T10:00:00Z",
                "role_names": ["Red Teamer"],
            },
            "Bob Smith invited you to Spring Engagement",
            [
                "Bob Smith",
                "Spring Engagement",
                "Red Teamer",
                "https://app.example.com/accept-invitation?token=abc",
                "June 17, 2026 at 10:00 UTC",
            ],
            ["2026-06-17T10:00:00Z"],
            id="evaluation_group_invitation",
        ),
        pytest.param(
            "evaluation_group_member_added",
            {"inviter_name": "Bob Smith", "group_title": "Spring Engagement", "role_names": ["Annotator"]},
            "Spring Engagement",
            ["Bob Smith", "Spring Engagement", "Annotator"],
            [],
            id="evaluation_group_member_added",
        ),
        pytest.param(
            "review_assigned",
            {"assignee_name": "Ada Lovelace", "assigner_name": "Bob Smith", "evaluation_title": "Spring Engagement"},
            "Spring Engagement",
            ["Ada Lovelace", "Bob Smith", "Spring Engagement"],
            [],
            id="review_assigned",
        ),
        pytest.param(
            "review_unassigned",
            {"assignee_name": "Ada Lovelace", "assigner_name": "Bob Smith", "evaluation_title": "Spring Engagement"},
            "Spring Engagement",
            ["Ada Lovelace", "Bob Smith", "Spring Engagement"],
            [],
            id="review_unassigned",
        ),
        pytest.param(
            "export_ready",
            {
                "recipient_name": "Ada Lovelace",
                "export_label": "evaluation-42-flags.csv",
                "target_url": "https://app.example.com/evaluations/42",
                "retention_hours": 24,
            },
            'Your export "evaluation-42-flags.csv" is ready',
            [
                "Ada Lovelace",
                "evaluation-42-flags.csv",
                "https://app.example.com/evaluations/42",
                "available to download for 24 hours",
            ],
            [],
            id="export_ready",
        ),
        pytest.param(
            "export_failed",
            {
                "recipient_name": "Ada Lovelace",
                "export_label": "evaluation-42-flags.csv",
                "error": "The export target is no longer available.",
                "target_url": "https://app.example.com/evaluations/42",
            },
            'Your export "evaluation-42-flags.csv" failed',
            [
                "Ada Lovelace",
                "evaluation-42-flags.csv",
                "The export target is no longer available.",
                "https://app.example.com/evaluations/42",
            ],
            [],
            id="export_failed",
        ),
        pytest.param(
            "model_inactivity_alert",
            {
                "recipient_name": "Ada Lovelace",
                "model_name": "Llama 3.1 8B (self-hosted)",
                "idle_hours": 72,
                "last_used_at": datetime(2026, 8, 16, 9, 14, tzinfo=UTC),
                "last_warmup_at": datetime(2026, 8, 19, 10, 41, tzinfo=UTC),
                "still_warmed": True,
                "target_url": "https://app.example.com/ai-models/42",
            },
            '"Llama 3.1 8B (self-hosted)" has been idle for 72 hours',
            [
                "Ada Lovelace",
                "Llama 3.1 8B (self-hosted)",
                "72 hours",
                "August 16, 2026 at 09:14 UTC",
                "August 19, 2026 at 10:41 UTC",
                "still being warmed",
                "Disable it if nobody needs it",
                "https://app.example.com/ai-models/42",
            ],
            ["Nothing is warming it"],
            id="model_inactivity_alert",
        ),
        pytest.param(
            "model_inactivity_alert",
            {
                "recipient_name": "Ada Lovelace",
                "model_name": "Llama 3.1 8B (self-hosted)",
                "idle_hours": 72,
                "last_used_at": datetime(2026, 8, 16, 9, 14, tzinfo=UTC),
                "last_warmup_at": None,
                "still_warmed": False,
                "target_url": "https://app.example.com/ai-models/42",
            },
            '"Llama 3.1 8B (self-hosted)" has been idle for 72 hours',
            ["Nothing is warming it", "never warmed", "Disable it if nobody needs it"],
            ["still being warmed"],
            id="model_inactivity_alert_cold",
        ),
    ],
)
def test_render_template_includes_expected_content(
    template: str,
    context: dict,
    subject_fragment: str,
    body_fragments: list[str],
    forbidden_fragments: list[str],
) -> None:
    message = render_template(template, "rcpt@example.com", context)

    assert subject_fragment in message.subject
    for body in (message.body_html, message.body_text):
        for fragment in body_fragments:
            assert fragment in body
        for fragment in forbidden_fragments:
            assert fragment not in body


@pytest.mark.unit
def test_render_platform_invitation_without_inviter_name_uses_generic_wording() -> None:
    """A self-service re-issue has no inviter — the template degrades gracefully."""
    context = {
        "inviter_name": None,
        "accept_url": "https://app.example.com/accept-invitation?token=abc",
        "expires_at": "2026-05-29T10:00:00Z",
        "role_names": ["red_teamer"],
    }
    message = render_template("platform_invitation", "rcpt@example.com", context)

    assert message.subject == "You've been invited to AI Red Teaming"
    assert "None" not in message.subject
    for body in (message.body_html, message.body_text):
        assert "You have been invited" in body
        assert "None" not in body
        assert "red_teamer" in body
        assert "https://app.example.com/accept-invitation?token=abc" in body


@pytest.mark.unit
def test_render_password_reset_handles_anonymous_recipient() -> None:
    context = {
        "recipient_name": None,
        "reset_url": "https://app.example.com/reset?token=abc",
        "expires_at": "2026-05-26T18:00:00Z",
        "triggered_by_admin": False,
    }
    message = render_template("password_reset", "ada@example.com", context)

    assert "Hi," in message.body_text
    assert "Hi," in message.body_html


@pytest.mark.unit
def test_render_password_reset_admin_triggered_does_not_claim_the_user_asked() -> None:
    """A recipient who never clicked "forgot password" must not be told they requested one."""
    context = {
        "recipient_name": "Ada Lovelace",
        "reset_url": "https://app.example.com/reset?token=abc",
        "expires_at": "2026-05-26T18:00:00Z",
        "triggered_by_admin": True,
    }
    message = render_template("password_reset", "ada@example.com", context)

    for body in (message.body_text, message.body_html):
        assert "An administrator has started a password reset" in body
        assert "We received a request" not in body
        assert "you can safely ignore this email" not in body
        assert "contact your administrator" in body
