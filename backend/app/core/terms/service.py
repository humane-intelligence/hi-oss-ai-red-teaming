"""Publishing Terms-of-Service versions and recording which one a user accepted."""

from datetime import UTC
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import User
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.logging import get_logger
from app.core.pagination import paginate
from app.core.schemas import ProblemErrorItem
from app.core.terms.models import TermsDocument

logger = get_logger(__name__)


class DuplicateTermsVersionError(ConflictError):
    """A live version already carries that string — 409, field-addressable.

    Versions are the handle an acceptance record points at, so two documents may not share
    one. The `errors[]` entry mirrors `PasswordPolicyError` so the publish form can show the
    failure on the field that caused it.
    """

    def __init__(self, version: str) -> None:
        detail = f"A terms version {version!r} already exists."
        super().__init__(detail)
        self.errors = [ProblemErrorItem(loc=["body", "version"], msg=detail, type="terms_version_taken")]


class ConsentRequiredError(BadRequestError):
    """An onboarding form declined the published terms — 400, field-addressable.

    A 400 rather than a 422 for the same reason `CurrentPasswordError` is: the shape carries
    an `errors[]` entry the form maps onto its own checkbox.
    """

    def __init__(self) -> None:
        detail = "The terms of service must be accepted."
        super().__init__(detail)
        self.errors = [ProblemErrorItem(loc=["body", "consent_terms"], msg=detail, type="consent_terms_required")]


class TermsAcceptanceRequiredError(ForbiddenError):
    """The caller owes an acceptance of the current version — 403, machine-recognisable.

    The only 403 in the codebase that carries a problem `type`: a client has to tell it from a
    permission refusal without matching on `detail`, because the two want opposite handling —
    one is a dead end, the other is cleared by accepting. Everything a caller needs to clear it
    (`GET /auth/me`, the terms reads, `POST /auth/me/terms`) is exempt from the check.
    """

    type = "urn:redteam:error:terms-acceptance-required"

    def __init__(self) -> None:
        super().__init__("Accept the current terms of service to continue.")


class StaleTermsVersionError(ConflictError):
    """The accepted id is not the current version — 409.

    Raised when a publish lands between the client rendering a document and the user
    accepting it. Auto-accepting the newer text on their behalf is the one thing consent
    must never do, so the client refetches and re-renders instead.
    """

    def __init__(self) -> None:
        super().__init__("Those terms are no longer the current version.")


def _current_terms_select() -> Select[tuple[TermsDocument]]:
    """Newest live version first, limit 1 — the ordering rule, in one place."""
    return TermsDocument.live_select().order_by(col(TermsDocument.published_at).desc(), col(TermsDocument.id)).limit(1)


async def get_current_terms(session: AsyncSession) -> TermsDocument | None:
    """The newest live version, or `None` while the platform has published none.

    `None` is the shipped state: consent has nothing to point at, so the registration
    checkbox and the acceptance gate both stay out of the way until an admin publishes.
    Newest `published_at` wins rather than an `is_current` flag — derived state cannot drift.
    """
    return (await session.execute(_current_terms_select())).scalars().first()


async def current_terms_id(session: AsyncSession) -> UUID | None:
    """The current version's id, without hydrating its text.

    `content` is capped at 200k chars and the per-request check only ever compares a UUID,
    so the row-hydrating `get_current_terms` is the wrong shape for that path.
    """
    statement = _current_terms_select().with_only_columns(col(TermsDocument.id))
    return (await session.execute(statement)).scalars().first()


async def get_terms(session: AsyncSession, terms_id: UUID) -> TermsDocument | None:
    """One live version by id, or `None` — a version tombstoned in the database reads as missing."""
    statement = TermsDocument.live_select().where(col(TermsDocument.id) == terms_id)
    return (await session.execute(statement)).scalar_one_or_none()


def acceptance_required(user: User, current: TermsDocument | None) -> bool:
    """Whether the gate must block `user` — a published version their record doesn't match."""
    return current is not None and user.accepted_terms_id != current.id


async def acceptance_required_for(session: AsyncSession, user_id: UUID) -> bool:
    """Whether `user_id` owes an acceptance — the row-free form, for the per-request check.

    Same rule as `acceptance_required`, but reading one column from each side rather than two
    hydrated rows: this runs on every authenticated request, and which document is current stays
    in one place by going through `_current_terms_select`. The two differ on one case, below.

    A token outliving a soft delete owes nothing: the account is gone, so the route's own lookup
    answers with the 404 it documents. Refusing it for consent instead would tell the caller to
    accept terms it cannot accept — `POST /me/terms` 404s for the same missing row. Opening here
    exposes nothing a token that outlives a soft delete does not already reach: routes that read a
    `User` row 404, and those that read only the token's claims never saw the deletion anyway.
    """
    current_id = await current_terms_id(session)
    if current_id is None:
        return False
    statement = User.live_select().where(col(User.id) == user_id).with_only_columns(col(User.accepted_terms_id))
    # `first()` on the row, not the scalar: a missing row and a row with no acceptance both scalar
    # to None, and they answer differently.
    row = (await session.execute(statement)).first()
    if row is None:
        return False
    return row[0] != current_id


async def resolve_signup_consent(
    session: AsyncSession, *, consent_terms: bool, terms_id: UUID | None
) -> TermsDocument | None:
    """The document an onboarding form's consent may be recorded against, or `None` if none is published.

    Symmetric with `accept_terms`, and for the same reason: consent is only ever recorded
    against the version the client actually rendered. Resolving "the current version" at write
    time instead would stamp a publish that landed between render and submit onto the account
    as if the user had read the new text, and `acceptance_required` would then report the
    account satisfied — so the gate would not re-prompt either, and the wrong consent would
    stand silently.

    Returns `None` while nothing is published: the checkbox never renders, so its absence must
    not block a signup. Callers run this before any account lookup, so every refusal hinges on
    the payload plus published state alone and adds no enumeration signal.

    Raises:
        ConsentRequiredError: Terms are published and the form declined them.
        StaleTermsVersionError: The form accepted something other than the current version,
            `terms_id` omitted included.
    """
    current = await get_current_terms(session)
    if current is None:
        return None
    if not consent_terms:
        raise ConsentRequiredError
    if terms_id != current.id:
        raise StaleTermsVersionError
    return current


async def list_terms(session: AsyncSession, *, limit: int, offset: int) -> tuple[list[TermsDocument], int]:
    """One page of published versions, newest first — the admin's history view."""
    statement = TermsDocument.live_select().order_by(col(TermsDocument.published_at).desc(), col(TermsDocument.id))
    return await paginate(session, statement, limit=limit, offset=offset)


async def publish_terms(session: AsyncSession, *, version: str, content: str) -> TermsDocument:
    """Insert a version, which by being newest becomes the current one.

    Every account whose acceptance points at an older version is re-prompted from the next
    request on, so this is a platform-wide event rather than a quiet settings write — hence
    the log line.

    Raises:
        DuplicateTermsVersionError: A live document already carries `version`.
    """
    document = TermsDocument(version=version, content=content, published_at=datetime.now(UTC))
    session.add(document)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise DuplicateTermsVersionError(version) from exc
    logger.info("terms.published", terms_id=str(document.id), version=document.version)
    return document


async def accept_terms(session: AsyncSession, user: User, *, terms_id: UUID) -> TermsDocument:
    """Record `user`'s acceptance of `terms_id`, which must be the current version.

    Idempotent: re-accepting the version already on the account leaves the original
    timestamp alone, so a double-submit cannot backdate — or forward-date — the record.

    Raises:
        StaleTermsVersionError: `terms_id` is not the current version (or none is published).
    """
    current = await get_current_terms(session)
    if current is None or current.id != terms_id:
        raise StaleTermsVersionError
    if user.accepted_terms_id == current.id:
        return current
    user.accepted_terms_id = current.id
    user.terms_accepted_at = datetime.now(UTC)
    session.add(user)
    await session.flush()
    logger.info("terms.accepted", user_id=str(user.id), version=current.version)
    return current


def record_signup_consent(user: User, *, document: TermsDocument | None, consent_emails: bool, now: datetime) -> None:
    """Stamp the consent `resolve_signup_consent` validated onto `user`; the caller flushes.

    A mutator, not a write path, so the onboarding flows fold it into the same INSERT/UPDATE
    they were already issuing and stamp it with the same `now` as the rest of the row. A
    `None` document means nothing is published — only the email flag is recorded.
    """
    user.consent_emails = consent_emails
    if document is not None:
        user.accepted_terms_id = document.id
        user.terms_accepted_at = now
