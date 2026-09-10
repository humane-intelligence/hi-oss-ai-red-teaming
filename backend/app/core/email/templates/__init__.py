"""Templated email registry — sub-packages auto-register via a module-level ``template`` export.

Adding a new email: create ``<name>/`` here with:

* ``<name>/__init__.py``:

      from pydantic import BaseModel
      from app.core.email.templates import EmailTemplate

      class FooContext(BaseModel): ...
      template = EmailTemplate(name="<name>", context_schema=FooContext)

* ``<name>/subject.txt`` — Jinja-rendered Subject (one line).
* ``<name>/body.html`` — ``{% extends "base.html" %}``; fill ``{% block content %}``
  from the ``_macros.html`` blocks (heading / paragraph / note / button).
* ``<name>/body.txt`` — plain-text body; end with ``{% include "_footer.txt" %}``.

The folder name must match ``template.name`` (it's also the Jinja path prefix
the renderer uses). The shared frame (``base.html`` / ``_macros.html`` /
``_footer.txt``) and the injected ``brand`` context give every mail one look —
see the ``new-email-template`` skill.
"""

import importlib
import pkgutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import StrictUndefined
from jinja2 import select_autoescape
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import EmailStr

from app.core.config import get_settings
from app.core.email.backends import EmailMessage
from app.core.email.backends import InlineImage

_TEMPLATES_DIR = Path(__file__).parent

_LOGO_CID = "logo"
_LOGO_BYTES = (_TEMPLATES_DIR / "assets" / "logo.png").read_bytes()


def _datetimefmt(value: str | datetime) -> str:
    """Render an ISO-string or datetime as a user-facing UTC timestamp.

    `validate_context` runs `model_dump(mode="json")`, so datetime fields
    arrive at the template as ISO strings; handle both for safety.

    TODO(user-tz): showing literal "UTC" is not user-friendly. Once we
    capture per-user timezone (signup-time browser TZ or profile setting),
    thread it into the template context and format here in the recipient's
    local zone instead.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%B %d, %Y at %H:%M UTC")


_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(enabled_extensions=("html",), default=False),
    undefined=StrictUndefined,
    keep_trailing_newline=False,
)
_jinja_env.filters["datetimefmt"] = _datetimefmt


class EmailTemplate(BaseModel):
    """Per-template metadata. ``context_schema`` doubles as both validator and JSON-Schema source."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    context_schema: type[BaseModel]


TEMPLATES: dict[str, EmailTemplate] = {}


def _require_template(template_name: str) -> EmailTemplate:
    template = TEMPLATES.get(template_name)
    if template is None:
        msg = f"Unknown email template: {template_name!r}"
        raise KeyError(msg)
    return template


def validate_context(template_name: str, context: dict[str, Any]) -> dict[str, Any]:
    """Run `context` through the template's Pydantic schema; return the normalized dict.

    `mode="json"` coerces values to JSON-safe primitives **before** the dict
    crosses into Celery — `datetime` → ISO string, `UUID` → str, `Decimal` →
    str, `Enum` → `.value`. Without this, a schema with such a field would
    crash at `apply_async` on the JSON serialiser; with it, the worker /
    Jinja template receives the serialised form (so e.g. nice date formatting
    is the caller's responsibility or a Jinja filter's, not pydantic's).
    """
    template = _require_template(template_name)
    return template.context_schema.model_validate(context).model_dump(mode="json")


def _brand_context() -> dict[str, Any]:
    """Brand vars merged into every render under the ``brand`` key.

    Sourced from settings at render time (logo, brand name, footer link /
    optional address) so per-template context schemas stay clean and none of
    this is persisted on the ``OutboundEmail`` audit row.
    """
    settings = get_settings()
    site_url = settings.frontend_base_url
    return {
        "company": settings.brand_company,
        "product": settings.brand_product,
        "site_url": site_url,
        "site_label": urlparse(site_url).netloc or site_url,
        "footer_address": settings.email_footer_address,
    }


def render_template(template_name: str, to: EmailStr, context: dict[str, Any]) -> EmailMessage:
    """Render the named template into a ready-to-send ``EmailMessage``.

    ``to`` is a parameter (not in ``context``) so the template body cannot
    accidentally see / leak the recipient. ``from_addr`` always comes from
    ``Settings.email_from`` — per-template overrides will land when the
    first template needs one. Every render is enriched with the ``brand``
    context and ships the brand logo as an inline (``cid:logo``) image, so
    the shared ``base.html`` frame renders identically for all templates.

    Raises:
        jinja2.TemplateNotFound: no folder matching ``template_name``.
    """
    settings = get_settings()
    render_context = {**context, "brand": _brand_context()}

    subject = _jinja_env.get_template(f"{template_name}/subject.txt").render(render_context).strip()
    body_text = _jinja_env.get_template(f"{template_name}/body.txt").render(render_context)
    body_html = _jinja_env.get_template(f"{template_name}/body.html").render(render_context)

    return EmailMessage(
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        from_addr=settings.email_from,
        inline_images=[InlineImage(cid=_LOGO_CID, data=_LOGO_BYTES)],
    )


def _register_templates() -> None:
    """Auto-discover submodules and collect their ``template`` module-level export."""
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        template = getattr(module, "template", None)
        if not isinstance(template, EmailTemplate):
            continue
        if template.name in TEMPLATES:
            msg = f"Duplicate template name {template.name!r} from module {module_info.name!r}"
            raise RuntimeError(msg)
        TEMPLATES[template.name] = template


_register_templates()
