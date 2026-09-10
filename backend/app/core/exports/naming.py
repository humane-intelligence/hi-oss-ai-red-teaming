"""Human-readable names for a finished export — the download filename and the notification label.

Both drop the raw UUID in favour of the target's title + the template's catalog name + a request
timestamp (`created_at`, UTC), so a report reads as "Demo Evaluation - Flags Report (CSV)" rather
than "evaluation-<uuid>-flags.csv". The timestamp comes from the job's `created_at` so the download
filename (built at download time) and the email/notification label (built at completion) agree.
"""

import re
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.exports.catalog import get_export


def _slug(text: str) -> str:
    """Filesystem-safe slug: lowercase, non-alphanumerics collapsed to single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "export"


def _template_name(template: str) -> str:
    export = get_export(template)
    return export.name if export is not None else template.replace("_", " ").title()


async def resolve_scope_title(
    session: AsyncSession, *, evaluation_id: UUID | None, evaluation_group_id: UUID | None
) -> str | None:
    """The evaluation or group title for the export's scope.

    Ignores soft-delete so a name still resolves for a scope tombstoned between the export
    request and its completion. Not a disclosure: this title only ever reaches the requester
    who exported that scope (the notification/email recipient), so it surfaces nothing they
    didn't already have — the same reasoning under which the license-lineage read ignores a
    tombstoned parent's ``deleted_at``.
    """
    if evaluation_id is not None:
        stmt = select(Evaluation.title).where(col(Evaluation.id) == evaluation_id)
    elif evaluation_group_id is not None:
        stmt = select(EvaluationGroup.title).where(col(EvaluationGroup.id) == evaluation_group_id)
    else:
        return None
    return (await session.execute(stmt)).scalar_one_or_none()


def report_filename(*, scope_title: str | None, template: str, extension: str, created_at: datetime) -> str:
    """Download attachment name: ``<title-slug>-<template>-<YYYYMMDD-HHMM>.<ext>`` (UTC stamp)."""
    base = _slug(scope_title) if scope_title else "export"
    return f"{base}-{template}-{created_at:%Y%m%d-%H%M}.{extension}"


def report_label(*, scope_title: str | None, template: str, export_format: str, created_at: datetime) -> str:
    """Human display label: ``<Title> - <Template> Report (<FORMAT>) · <YYYY-MM-DD HH:MM> UTC``.

    The title is user-set and unvalidated for control chars; this label flows into an email Subject
    header (which rejects CR/LF), so collapse any whitespace run (incl. newlines) to a single space.
    """
    title = " ".join((scope_title or "Your export").split())
    return f"{title} - {_template_name(template)} Report ({export_format.upper()}) · {created_at:%Y-%m-%d %H:%M} UTC"
