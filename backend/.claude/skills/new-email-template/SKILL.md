---
name: new-email-template
description: Use when adding a new transactional email (verification, password reset, invitation, reviewer-assigned, etc.). Covers the folder layout, the context schema, the Jinja files, registration mechanics, and how callers invoke `send_email()`. Trigger whenever a task introduces or renames a templated mail.
---

# Adding a new email template

## Where the template lives

Every template is its own sub-package under [app/core/email/templates/](../../../app/core/email/templates/):

```
app/core/email/templates/
├── __init__.py              # registry + render — do not edit when adding a template
├── base.html                # shared frame (logo header, amber CTA styling, footer) — don't edit per template
├── _macros.html             # heading / paragraph / note / button blocks — reuse, don't reinvent
├── _footer.txt              # shared plain-text footer — included by every body.txt
├── assets/logo.png          # brand logo, shipped inline as cid:logo on every mail
└── <name>/
    ├── __init__.py          # Python: context schema + EmailTemplate definition
    ├── subject.txt          # Jinja — single line, rendered then `.strip()`-ed
    ├── body.html            # Jinja — extends base.html, fills {% block content %}
    └── body.txt             # Jinja — plain-text body, includes _footer.txt
```

The folder name **is** the template name (it doubles as the Jinja path prefix used by `render_template`). Keep it `lowercase_snake` and stable — it ends up in `outbound_emails.template_name` rows, so renames cause analytics churn.

## The package `__init__.py`

```python
"""<one-line description of when this mail is sent>."""

from pydantic import BaseModel

from app.core.email.templates import EmailTemplate


class <Name>Context(BaseModel):
    """Context for the `<name>` template."""

    user_name: str
    verification_url: str  # signed, single-use link


template = EmailTemplate(
    name="<name>",
    context_schema=<Name>Context,
)
```

Rules:

- **Module-level `template`**: must be named exactly `template` and be an `EmailTemplate` instance. The registry walks every sub-package on import and reads `getattr(module, "template", None)` — anything else is ignored.
- **`name` must match the folder name**. Mismatch is silent — Jinja will look up `<template.name>/subject.txt`, and the folder must exist there.
- **Duplicate `template.name` across packages raises `RuntimeError` at process start** — loud failure, by design.
- **`context_schema`** must be a Pydantic `BaseModel` subclass. Field-level `description=...` shows up in any later JSON-Schema export; field types double as validation.
- **Do not put `to` in the context.** The recipient is a separate argument to `send_email()` so a Jinja template can never accidentally leak it into a different field.

## The Jinja files

`subject.txt` (one line, rendered then `.strip()`-ed):

```jinja
Welcome, {{ user_name }}
```

`body.html` — **extend `base.html`** and fill `{% block content %}` from the shared macros. Don't hand-roll `<html>`/`<head>`, the logo, or the footer: `base.html` owns the frame and `render_template` attaches the logo as `cid:logo`. Autoescape is **on** for `.html`, so `{{ vars }}` are escaped automatically:

```jinja
{% extends "base.html" %}
{% from "_macros.html" import heading, paragraph, note, button %}
{% block content %}
{{ heading("Verify your email address") }}
{% call paragraph() %}Hi {{ user_name }}, confirm your email to finish setting up your account.{% endcall %}
{{ button(verification_url, "Verify email address") }}
{% call note() %}This link expires on {{ expires_at | datetimefmt }}.{% endcall %}
{% endblock %}
```

The macros (in `_macros.html`) keep every mail on one type scale — use them instead of raw inline styles:

- `heading(text)` — the centered H1.
- `{% call paragraph() %}…{% endcall %}` — a body paragraph (put inline markup / `{% if %}` inside the call block; `{{ vars }}` are still escaped).
- `{% call note() %}…{% endcall %}` — the small muted line (expiry / security notes).
- `button(url, label)` — the amber CTA. For a mail with no token link, point it at `brand.site_url` (a "Sign in" button); omit it entirely if there's no action.

`body.txt` (autoescape **off** — plain text) mirrors the HTML and ends by including the shared footer:

```jinja
Hi {{ user_name }},

Confirm your email to finish setting up your account:
{{ verification_url }}

{% include "_footer.txt" %}
```

**The `brand` context is injected on every render** (you don't declare it in your schema): `brand.company` (operating entity, from `BRAND_COMPANY`), `brand.product` (platform name, from `BRAND_PRODUCT`), `brand.site_url`, `brand.site_label`, `brand.footer_address` (optional, from `EMAIL_FOOTER_ADDRESS`). Use `brand.site_url` for sign-in CTAs; the footer renders itself.

All three files are **required**. `_jinja_env` uses `StrictUndefined`, so any unresolved variable raises at render time (loud failure → matches our Zen-of-Python preference). Don't ship templates with `{{ optional_field | default(...) }}` patterns when you can just put the field in the context.

## Context storage

The validated `context` is stored on the `OutboundEmail` row as-is and the Celery task reads it back from there at send time. That means any value you put in `context` lands in Postgres in clear text, so **do not include secrets** (raw reset tokens, one-time codes, link tokens you can't regenerate) in `context`.

For a template that needs a link-borne secret (an invitation / verification / reset URL), pass it via the `secret_context=` keyword argument instead. Those keys are validated against the same schema but kept **out of** `OutboundEmail.context` — they ride the Celery task signature (a transient broker message, gone once acked; no result backend persists it) and are merged back into the render context only at delivery time. The persisted `context` is therefore a non-secret subset that intentionally does not round-trip through `validate_context`. See `accept_url` / `verify_url` / `reset_url` in the invitation, registration, and password-reset services for the pattern.

`validate_context` runs `model_dump(mode="json")`, so `datetime` / `UUID` / `Decimal` fields in the schema are coerced to JSON-safe strings before they hit the DB or Jinja. If you want a nice date format in the body, format it caller-side or via a Jinja filter — by render time it's already a string.

## How callers invoke `send_email()`

```python
from app.core.email import send_email

email_id = await send_email(
    session,
    "<name>",
    user.email,
    {"user_name": user.display_name},
    secret_context={"verification_url": signed_url},  # link-borne secret — never persisted
)
await session.commit()  # caller owns the commit — without it the row vanishes
```

`send_email()`:

1. Validates the template + context. Raises `KeyError` if `template_name` isn't in `TEMPLATES`, `pydantic.ValidationError` if `context` doesn't match the schema.
2. Renders the template up front (discarding the result) so `TemplateNotFound`, `UndefinedError`, or an invalid recipient address fail synchronously here instead of in the worker.
3. Inserts a `queued` row into `outbound_emails` with the validated **non-secret** context (any `secret_context` keys are stripped from what's persisted).
4. Flushes (so `email_id` is set), then `apply_async(..., countdown=1)` on `send_email_task` with `(email_id, secret_context)` — the secret travels in the task signature, not the DB row.

**Caller is responsible for the commit.** A 1-second countdown on the task is the only thing protecting the worker from racing the commit — if you forget `await session.commit()`, the worker picks up `email_id`, finds nothing in the DB (transaction rolled back on session close), and logs `email.task.row_missing`. See [app/core/email/tasks.py](../../../app/core/email/tasks.py).

## Tests

Add at least one render test in [tests/email/test_templates.py](../../../tests/email/test_templates.py) covering:

- Happy path: render returns a non-empty subject + matching `to` + non-empty bodies.
- Each Jinja variable from the context appears in the rendered HTML / text.
- Missing required context field raises (Pydantic `ValidationError`).

The shared frame (base.html, `cid:logo`, footer, `brand` context) is already covered by the frame tests in `test_templates.py` — you only need to test your template's own vars, not re-assert the frame.

Renderer tests are `@pytest.mark.unit` — no DB / Redis needed. For end-to-end behaviour against `send_email()` + `send_email_task`, the existing fixtures in [tests/email/test_tasks.py](../../../tests/email/test_tasks.py) already cover the lifecycle; you don't need a per-template integration test unless this mail has unusual side-effects.

**Add a sample context to `SAMPLE_CONTEXTS` in [scripts/preview_emails.py](../../../scripts/preview_emails.py)** — `make emailpreview` (one sample of every template into the local Mailpit, http://localhost:8025) fails loudly on a template without one. It's also the fastest way to eyeball your new template rendered in a real inbox.

## Don'ts

- **Don't rebuild the frame.** No `<html>`/`<head>`, logo, or footer in your body — `extend base.html` and use the macros. Editing `base.html` / `_macros.html` restyles **every** mail, so change them deliberately, not to tweak one template.
- **No DB-managed templates.** Templates are code (Jinja files on disk). A DB row that names a non-existent folder would 500 on send.
- **No `to` in the context dict.** Recipient is a function argument, not a template variable.
- **No silent fallback for missing context keys.** `StrictUndefined` is intentional.
- **No secrets in `context`.** It's persisted to Postgres in clear text. Pass link-borne secrets via `secret_context=` instead — they ride the task signature and never hit the row.
- **Don't edit `templates/__init__.py` to register your template.** Auto-discovery does it; manual registration is dead code waiting to drift.
