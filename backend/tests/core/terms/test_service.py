"""Integration tests for `app.core.terms.service`."""

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
import time_machine
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.terms.models import TermsDocument
from app.core.terms.service import ConsentRequiredError
from app.core.terms.service import DuplicateTermsVersionError
from app.core.terms.service import StaleTermsVersionError
from app.core.terms.service import accept_terms
from app.core.terms.service import acceptance_required
from app.core.terms.service import acceptance_required_for
from app.core.terms.service import current_terms_id
from app.core.terms.service import get_current_terms
from app.core.terms.service import list_terms
from app.core.terms.service import publish_terms
from app.core.terms.service import record_signup_consent
from app.core.terms.service import resolve_signup_consent


@pytest.mark.integration
async def test_get_current_terms_is_none_until_something_is_published(db_session: AsyncSession) -> None:
    assert await get_current_terms(db_session) is None


@pytest.mark.integration
async def test_get_current_terms_returns_the_newest_version(db_session: AsyncSession) -> None:
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        latest = await publish_terms(db_session, version="2.0", content="Second")

    current = await get_current_terms(db_session)

    assert current is not None
    assert current.id == latest.id


@pytest.mark.integration
async def test_get_current_terms_skips_a_soft_deleted_version(db_session: AsyncSession) -> None:
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        first = await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        newest = await publish_terms(db_session, version="2.0", content="Second")
    newest.soft_delete(None)
    await db_session.flush()

    current = await get_current_terms(db_session)

    assert current is not None
    assert current.id == first.id


@pytest.mark.integration
async def test_current_terms_id_agrees_with_the_row_and_skips_a_soft_deleted_version(
    db_session: AsyncSession,
) -> None:
    # The projected form drops the columns loader options attach to, so its soft-delete filter
    # holds only as long as `live_select`'s survives `with_only_columns`.
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        first = await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        newest = await publish_terms(db_session, version="2.0", content="Second")

    assert await current_terms_id(db_session) == newest.id

    newest.soft_delete(None)
    await db_session.flush()

    assert await current_terms_id(db_session) == first.id


@pytest.mark.integration
async def test_current_terms_id_is_none_until_something_is_published(db_session: AsyncSession) -> None:
    assert await current_terms_id(db_session) is None


@pytest.mark.integration
async def test_publish_terms_rejects_a_duplicate_live_version(db_session: AsyncSession) -> None:
    await publish_terms(db_session, version="1.0", content="First")

    with pytest.raises(DuplicateTermsVersionError):
        await publish_terms(db_session, version="1.0", content="Different text, same handle")


@pytest.mark.integration
async def test_publish_terms_reuses_a_version_string_freed_by_a_soft_delete(db_session: AsyncSession) -> None:
    first = await publish_terms(db_session, version="1.0", content="First")
    first.soft_delete(None)
    await db_session.flush()

    again = await publish_terms(db_session, version="1.0", content="Republished")

    assert again.id != first.id


@pytest.mark.integration
async def test_list_terms_orders_newest_first(db_session: AsyncSession) -> None:
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="2.0", content="Second")

    rows, total = await list_terms(db_session, limit=20, offset=0)

    assert total == 2
    assert [row.version for row in rows] == ["2.0", "1.0"]


@pytest.mark.integration
async def test_accept_terms_records_the_version_and_the_moment(db_session: AsyncSession, active_user: User) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    with time_machine.travel(now, tick=False):
        await accept_terms(db_session, active_user, terms_id=document.id)

    assert active_user.accepted_terms_id == document.id
    assert active_user.terms_accepted_at == now


@pytest.mark.integration
async def test_accept_terms_keeps_the_first_timestamp_on_a_repeat(db_session: AsyncSession, active_user: User) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")
    first = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    with time_machine.travel(first, tick=False):
        await accept_terms(db_session, active_user, terms_id=document.id)

    with time_machine.travel(datetime(2026, 9, 3, 12, 0, tzinfo=UTC), tick=False):
        await accept_terms(db_session, active_user, terms_id=document.id)

    assert active_user.terms_accepted_at == first


@pytest.mark.integration
async def test_accept_terms_refuses_a_superseded_version(db_session: AsyncSession, active_user: User) -> None:
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        stale = await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="2.0", content="Second")

    with pytest.raises(StaleTermsVersionError):
        await accept_terms(db_session, active_user, terms_id=stale.id)

    assert active_user.accepted_terms_id is None


@pytest.mark.integration
async def test_accept_terms_refuses_an_unknown_id(db_session: AsyncSession, active_user: User) -> None:
    await publish_terms(db_session, version="1.0", content="Terms")

    with pytest.raises(StaleTermsVersionError):
        await accept_terms(db_session, active_user, terms_id=uuid4())


@pytest.mark.integration
async def test_accept_terms_refuses_while_nothing_is_published(db_session: AsyncSession, active_user: User) -> None:
    with pytest.raises(StaleTermsVersionError):
        await accept_terms(db_session, active_user, terms_id=uuid4())


@pytest.mark.integration
async def test_acceptance_is_required_only_while_the_record_lags(db_session: AsyncSession, active_user: User) -> None:
    assert acceptance_required(active_user, None) is False

    document = await publish_terms(db_session, version="1.0", content="Terms")
    assert acceptance_required(active_user, document) is True

    await accept_terms(db_session, active_user, terms_id=document.id)
    assert acceptance_required(active_user, document) is False

    newer = await publish_terms(db_session, version="2.0", content="Reworded")
    assert acceptance_required(active_user, newer) is True


@pytest.mark.integration
async def test_acceptance_required_for_tracks_the_record_without_a_projected_user(
    db_session: AsyncSession, active_user: User
) -> None:
    """The row-free form the per-request check uses has to answer exactly as `acceptance_required`."""
    assert await acceptance_required_for(db_session, active_user.id) is False

    document = await publish_terms(db_session, version="1.0", content="Terms")
    assert await acceptance_required_for(db_session, active_user.id) is True

    await accept_terms(db_session, active_user, terms_id=document.id)
    await db_session.flush()
    assert await acceptance_required_for(db_session, active_user.id) is False

    await publish_terms(db_session, version="2.0", content="Reworded")
    assert await acceptance_required_for(db_session, active_user.id) is True


@pytest.mark.integration
async def test_acceptance_required_for_asks_nothing_of_an_account_that_is_gone(
    db_session: AsyncSession, active_user: User
) -> None:
    # A token outliving a soft delete: the route's own lookup owns that case and answers 404. A
    # consent refusal here would instead tell the caller to accept terms `POST /me/terms` will
    # 404 on — an instruction that cannot be followed. The soft-deleted row is the case that
    # matters, because it only reads as absent while `live_select`'s filter survives the
    # column narrowing.
    await publish_terms(db_session, version="1.0", content="Terms")
    assert await acceptance_required_for(db_session, active_user.id) is True

    active_user.deleted_at = datetime.now(UTC)
    db_session.add(active_user)
    await db_session.flush()

    assert await acceptance_required_for(db_session, active_user.id) is False
    assert await acceptance_required_for(db_session, uuid4()) is False


@pytest.mark.integration
async def test_resolve_signup_consent_is_none_while_nothing_is_published(db_session: AsyncSession) -> None:
    assert await resolve_signup_consent(db_session, consent_terms=False, terms_id=None) is None


@pytest.mark.integration
async def test_resolve_signup_consent_refuses_a_decline_once_published(db_session: AsyncSession) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")

    with pytest.raises(ConsentRequiredError):
        await resolve_signup_consent(db_session, consent_terms=False, terms_id=document.id)


@pytest.mark.integration
async def test_resolve_signup_consent_refuses_a_superseded_version(db_session: AsyncSession) -> None:
    """The whole point of the id: a publish between form render and submit must not be
    recorded as if the user had read the new text."""
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        stale = await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="2.0", content="Second")

    with pytest.raises(StaleTermsVersionError):
        await resolve_signup_consent(db_session, consent_terms=True, terms_id=stale.id)


@pytest.mark.integration
async def test_resolve_signup_consent_refuses_an_omitted_version(db_session: AsyncSession) -> None:
    await publish_terms(db_session, version="1.0", content="Terms")

    with pytest.raises(StaleTermsVersionError):
        await resolve_signup_consent(db_session, consent_terms=True, terms_id=None)


@pytest.mark.integration
async def test_resolve_signup_consent_returns_the_current_version(db_session: AsyncSession) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")

    resolved = await resolve_signup_consent(db_session, consent_terms=True, terms_id=document.id)

    assert resolved is not None
    assert resolved.id == document.id


@pytest.mark.unit
def test_record_signup_consent_stamps_the_resolved_version() -> None:
    user = User(email="ada@example.com")
    document = TermsDocument(version="1.0", content="Terms", published_at=datetime(2026, 1, 1, tzinfo=UTC))
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    record_signup_consent(user, document=document, consent_emails=True, now=now)

    assert user.accepted_terms_id == document.id
    assert user.terms_accepted_at == now
    assert user.consent_emails is True


@pytest.mark.unit
def test_record_signup_consent_stores_only_the_email_flag_without_a_document() -> None:
    user = User(email="ada@example.com")

    record_signup_consent(user, document=None, consent_emails=True, now=datetime(2026, 9, 2, tzinfo=UTC))

    assert user.accepted_terms_id is None
    assert user.terms_accepted_at is None
    assert user.consent_emails is True
