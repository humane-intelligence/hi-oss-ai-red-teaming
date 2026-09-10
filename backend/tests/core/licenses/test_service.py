"""Integration tests for the licenses service — license CRUD + the curated-catalog sync."""

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.config import get_settings
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.services.evaluation_groups import create_evaluation_group
from app.core.evaluations.services.evaluation_groups import create_evaluation_group_draft
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.licenses.catalog import CURATED_LICENSES
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.models import DataLicense
from app.core.licenses.schemas import LicenseCreate
from app.core.licenses.schemas import LicenseUpdate
from app.core.licenses.service import create_data_license
from app.core.licenses.service import get_data_license
from app.core.licenses.service import get_default_license
from app.core.licenses.service import list_data_licenses
from app.core.licenses.service import soft_delete_data_license
from app.core.licenses.service import sync_licenses
from app.core.licenses.service import update_data_license
from app.core.licenses.service import validate_license_ref
from app.core.platform_settings.service import update_platform_settings
from app.core.restore import restore_cutoff

# These suites exercise the live set; the window is inert but the services now
# require it explicitly, as the routes derive it.
_CUTOFF = restore_cutoff(get_settings())


async def _user(db: AsyncSession) -> User:
    user = User(email=f"{uuid4().hex[:8]}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _own_license(db: AsyncSession, owner: User, *, name: str = "Acme 1.0") -> DataLicense:
    return await create_data_license(
        db,
        caller_id=owner.id,
        name=name,
        version="1.0",
        short_description="internal",
        content="TEXT",
        reference_url=None,
    )


@pytest.mark.integration
async def test_create_data_license_is_owned_and_uncurated(db_session: AsyncSession) -> None:
    owner = await _user(db_session)

    lic = await _own_license(db_session, owner)

    assert lic.created_by_id == owner.id
    assert lic.spdx_id is None  # user-authored — not a curated row


@pytest.mark.integration
async def test_owner_can_update_own_license(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    lic = await _own_license(db_session, owner)

    updated = await update_data_license(
        db_session, lic, LicenseUpdate(name="Renamed"), caller_id=owner.id, can_manage=False
    )

    assert updated.name == "Renamed"


@pytest.mark.integration
async def test_owner_cannot_update_others_license(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    other = await _user(db_session)
    lic = await _own_license(db_session, owner)

    with pytest.raises(ForbiddenError):
        await update_data_license(db_session, lic, LicenseUpdate(name="x"), caller_id=other.id, can_manage=False)


@pytest.mark.integration
async def test_curated_metadata_not_editable_even_by_manager(db_session: AsyncSession) -> None:
    # A curated licence's metadata is managed in code (catalog.py + sync_licenses); the API refuses
    # to edit it for everyone, including a `licenses:manage` holder — a resync would revert it.
    # Its `content` is the one exception (see the tests below): the catalog ships none for most
    # entries, so a resync has nothing to revert it with.
    owner = await _user(db_session)
    curated = await get_data_license(db_session, curated_license_id("CC0-1.0"))

    with pytest.raises(ForbiddenError):
        await update_data_license(db_session, curated, LicenseUpdate(name="x"), caller_id=owner.id, can_manage=False)
    with pytest.raises(ForbiddenError):
        await update_data_license(db_session, curated, LicenseUpdate(name="x"), caller_id=owner.id, can_manage=True)


@pytest.mark.integration
async def test_delete_platform_default_is_conflict(db_session: AsyncSession) -> None:
    # The 409 default-guard applies to a user-authored licence set as the default (curated is
    # blocked earlier by the curated-managed-in-code rule).
    owner = await _user(db_session)
    own = await _own_license(db_session, owner)
    await update_platform_settings(db_session, default_license_id=own.id)

    with pytest.raises(ConflictError):
        await soft_delete_data_license(db_session, own, caller_id=owner.id, can_manage=False)


@pytest.mark.integration
async def test_owner_can_delete_own_but_curated_is_blocked(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    own = await _own_license(db_session, owner)
    curated = await get_data_license(db_session, curated_license_id("CC0-1.0"))

    with pytest.raises(ForbiddenError):
        await soft_delete_data_license(db_session, curated, caller_id=owner.id, can_manage=False)
    with pytest.raises(ForbiddenError):  # not even a manager may delete a curated licence
        await soft_delete_data_license(db_session, curated, caller_id=owner.id, can_manage=True)

    await soft_delete_data_license(db_session, own, caller_id=owner.id, can_manage=False)
    assert own.deleted_at is not None


@pytest.mark.integration
async def test_validate_license_ref(db_session: AsyncSession) -> None:
    await validate_license_ref(db_session, curated_license_id("CC-BY-4.0"))  # live curated — no raise

    with pytest.raises(BadRequestError):
        await validate_license_ref(db_session, uuid4())


@pytest.mark.integration
async def test_sync_licenses_is_idempotent(db_session: AsyncSession) -> None:
    # Curated rows are already seeded per worker; a resync must not duplicate them.
    count = await sync_licenses(db_session)
    rows, total = await list_data_licenses(db_session, limit=100, offset=0, deleted_cutoff=_CUTOFF)

    assert count == len(CURATED_LICENSES)
    assert total == len(CURATED_LICENSES)
    spdx_ids = [lic.spdx_id for lic in rows]
    assert len(spdx_ids) == len(set(spdx_ids))


@pytest.mark.integration
async def test_sync_licenses_clears_both_tombstone_columns(db_session: AsyncSession) -> None:
    # A revived curated row must not keep the actor who deleted it, or `deleted_by_id`
    # outlives the tombstone it belongs to.
    rows, _ = await list_data_licenses(db_session, limit=1, offset=0, deleted_cutoff=_CUTOFF)
    curated = rows[0]
    curated.soft_delete(uuid4())
    db_session.add(curated)
    await db_session.flush()

    await sync_licenses(db_session)
    await db_session.refresh(curated)

    assert curated.deleted_at is None
    assert curated.deleted_by_id is None


@pytest.mark.integration
async def test_sync_licenses_stores_no_spdx_id_for_the_no_license_sentinel(db_session: AsyncSession) -> None:
    # The stored `spdx_id` stays NULL, so the export columns print the name, not "NONE".
    await sync_licenses(db_session)

    sentinel = await db_session.get(DataLicense, curated_license_id(NO_LICENSE_SPDX_ID))

    assert sentinel is not None
    assert sentinel.spdx_id is None
    assert sentinel.name == "No license"
    assert sentinel.created_by_id is None
    # Its text is code-shipped, so the row carries it after a sync like any other field.
    assert "CLOSED DATA" in sentinel.content


@pytest.mark.integration
async def test_manager_may_add_text_to_a_curated_license(db_session: AsyncSession) -> None:
    admin = await _user(db_session)
    curated = await get_data_license(db_session, curated_license_id("CC0-1.0"))

    updated = await update_data_license(
        db_session, curated, LicenseUpdate(content="CC0 TEXT"), caller_id=admin.id, can_manage=True
    )

    assert updated.content == "CC0 TEXT"


@pytest.mark.integration
async def test_curated_text_edit_requires_manage(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    curated = await get_data_license(db_session, curated_license_id("ODbL-1.0"))

    with pytest.raises(ForbiddenError):
        await update_data_license(db_session, curated, LicenseUpdate(content="x"), caller_id=owner.id, can_manage=False)


@pytest.mark.integration
async def test_curated_text_edit_cannot_smuggle_other_fields(db_session: AsyncSession) -> None:
    # Allowing `name` here would recreate the bug the curated rule exists for: a resync reverts it.
    admin = await _user(db_session)
    curated = await get_data_license(db_session, curated_license_id("PDDL-1.0"))

    with pytest.raises(ForbiddenError):
        await update_data_license(
            db_session,
            curated,
            LicenseUpdate(content="x", name="Renamed"),
            caller_id=admin.id,
            can_manage=True,
        )


@pytest.mark.integration
async def test_sync_licenses_keeps_text_the_catalog_does_not_ship(db_session: AsyncSession) -> None:
    # Without this, an admin-supplied text vanishes silently at the next deploy.
    admin = await _user(db_session)
    curated = await get_data_license(db_session, curated_license_id("CC-BY-SA-4.0"))
    await update_data_license(
        db_session, curated, LicenseUpdate(content="ADMIN TEXT"), caller_id=admin.id, can_manage=True
    )

    await sync_licenses(db_session)
    await db_session.refresh(curated)

    assert curated.content == "ADMIN TEXT"


@pytest.mark.integration
async def test_sync_licenses_overwrites_text_the_catalog_ships(db_session: AsyncSession) -> None:
    # The other arm: where the catalog carries the text, code stays the source of truth.
    sentinel = await get_data_license(db_session, curated_license_id(NO_LICENSE_SPDX_ID))
    sentinel.content = "STALE"
    db_session.add(sentinel)
    await db_session.flush()

    await sync_licenses(db_session)
    await db_session.refresh(sentinel)

    assert "CLOSED DATA" in sentinel.content


@pytest.mark.integration
async def test_empty_patch_on_curated_is_a_bad_request(db_session: AsyncSession) -> None:
    # `content` is the only editable field, so a body that sets nothing asks for something the
    # endpoint cannot do — say so instead of answering 200 to a no-op.
    admin = await _user(db_session)
    curated = await get_data_license(db_session, curated_license_id("CDLA-Permissive-2.0"))

    with pytest.raises(BadRequestError):
        await update_data_license(db_session, curated, LicenseUpdate(), caller_id=admin.id, can_manage=True)
    # The 400 lands before the permission branch, so a caller without `manage` gets the same answer.
    with pytest.raises(BadRequestError):
        await update_data_license(db_session, curated, LicenseUpdate(), caller_id=admin.id, can_manage=False)


@pytest.mark.integration
async def test_empty_patch_on_an_own_license_stays_a_no_op(db_session: AsyncSession) -> None:
    # The house convention for a PATCH that sets nothing (see platform-settings) is a 200 that
    # writes nothing; only the curated row, whose editable surface is a single field, diverges.
    owner = await _user(db_session)
    lic = await _own_license(db_session, owner)

    updated = await update_data_license(db_session, lic, LicenseUpdate(), caller_id=owner.id, can_manage=False)

    assert updated.name == "Acme 1.0"


@pytest.mark.integration
async def test_list_does_not_load_the_license_text(db_session: AsyncSession) -> None:
    # The list projection carries `has_content`, not the text: loading the column would ship every
    # licence's full legal text from Postgres for a boolean, on a query the picker refetches per
    # form mount. `raiseload` makes a future consumer of `lic.content` fail loudly, not silently
    # per row.
    rows, _ = await list_data_licenses(db_session, limit=100, offset=0, deleted_cutoff=_CUTOFF)

    assert rows
    for lic in rows:
        state = sa_inspect(lic)
        assert state is not None
        assert "content" in state.unloaded
    # `raiseload`, not a plain `defer`: reaching for the text must be refused outright. Matched on
    # the message because a plain `defer` also raises an `InvalidRequestError` here — `MissingGreenlet`,
    # from the lazy load it would attempt — so the class alone does not tell the two apart.
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = rows[0].content


@pytest.mark.integration
async def test_has_content_comes_from_sql_not_from_the_text(db_session: AsyncSession) -> None:
    # The flag rides on the mapper (`DataLicense.has_text`), so it is answerable on a row whose text
    # is deferred — which is what lets every projection path leave the column behind.
    rows, _ = await list_data_licenses(db_session, limit=100, offset=0, deleted_cutoff=_CUTOFF)
    by_id = {lic.id: lic for lic in rows}

    assert by_id[curated_license_id(NO_LICENSE_SPDX_ID)].has_text is True
    assert by_id[curated_license_id("CC0-1.0")].has_text is False
    state = sa_inspect(by_id[curated_license_id(NO_LICENSE_SPDX_ID)])
    assert state is not None
    assert "content" in state.unloaded


@pytest.mark.integration
async def test_curated_text_the_catalog_ships_is_not_editable(db_session: AsyncSession) -> None:
    # The arm `test_sync_licenses_overwrites_text_the_catalog_ships` pins: for an entry carrying its
    # own text, code is the source of truth. Accepting the edit would answer 200 to a write the next
    # deploy reverts, so the guard refuses it — including for a `licenses:manage` holder.
    sentinel = await get_data_license(db_session, curated_license_id(NO_LICENSE_SPDX_ID))
    before = sentinel.content

    with pytest.raises(ForbiddenError, match="shipped in the catalog"):
        await update_data_license(
            db_session, sentinel, LicenseUpdate(content="ours"), caller_id=uuid4(), can_manage=True
        )

    assert sentinel.content == before


@pytest.mark.integration
async def test_a_derived_license_missing_from_the_catalog_is_a_server_fault(db_session: AsyncSession) -> None:
    # `synclicenses` not run: the id was derived by the server, so blaming the caller with a 400
    # would send an operator after a client bug — and a 4xx never reaches error reporting. Driven
    # through the group create, not the helper, so the wiring is what the test pins.
    owner = User(email="lic-derive@example.com", hashed_password="x", is_active=True)
    db_session.add(owner)
    await db_session.flush()
    sentinel = await get_data_license(db_session, curated_license_id(NO_LICENSE_SPDX_ID))
    await db_session.delete(sentinel)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="run synclicenses"):
        await create_evaluation_group(
            db_session,
            caller_id=owner.id,
            title="Private",
            description="d",
            access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
            start_date=datetime.now(UTC).date(),
        )


@pytest.mark.integration
async def test_the_draft_route_also_reports_a_missing_derived_license_as_a_server_fault(
    db_session: AsyncSession,
) -> None:
    # The draft creator carries its own copy of the derive-and-check wiring, and it is the path where
    # `access_level` is implicit — so a title-only draft on an unsynced environment must fail the same
    # way, not with an FK `IntegrityError` from the insert.
    owner = User(email="lic-derive-draft@example.com", hashed_password="x", is_active=True)
    db_session.add(owner)
    await db_session.flush()
    sentinel = await get_data_license(db_session, curated_license_id(NO_LICENSE_SPDX_ID))
    await db_session.delete(sentinel)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="run synclicenses"):
        await create_evaluation_group_draft(db_session, caller_id=owner.id, title="Draft")


@pytest.mark.integration
async def test_the_platform_default_is_fetched_without_its_text(db_session: AsyncSession) -> None:
    # The default is the row nearly every group/evaluation projection resolves, so it is the hottest
    # of the deferred paths — and the only one no test covered.
    lic = await get_default_license(db_session)

    state = sa_inspect(lic)
    assert state is not None
    assert "content" in state.unloaded
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = lic.content


@pytest.mark.integration
async def test_empty_patch_on_a_curated_license_the_catalog_ships_text_for_is_a_bad_request(
    db_session: AsyncSession,
) -> None:
    # Same 400 as any other curated row: an empty body is a request-shape problem, so it is answered
    # before the row's own rules. Ordering the ships-text refusal first made the documented 400
    # unreachable for this entry.
    admin = await _user(db_session)
    sentinel = await get_data_license(db_session, curated_license_id(NO_LICENSE_SPDX_ID))

    with pytest.raises(BadRequestError, match="only editable field"):
        await update_data_license(db_session, sentinel, LicenseUpdate(), caller_id=admin.id, can_manage=True)


@pytest.mark.integration
async def test_a_curated_metadata_edit_is_refused_by_field_not_by_permission(db_session: AsyncSession) -> None:
    # Without `licenses:manage` the caller used to be told only managers may edit "the text" — about a
    # field they never sent, and one no permission unlocks. The answer must name the field.
    owner = await _user(db_session)
    curated = await get_data_license(db_session, curated_license_id("CC-BY-ND-4.0"))

    with pytest.raises(ForbiddenError, match="only 'content' is editable"):
        await update_data_license(db_session, curated, LicenseUpdate(name="x"), caller_id=owner.id, can_manage=False)


@pytest.mark.unit
def test_a_blank_license_text_is_rejected() -> None:
    # `min_length` counts characters, so whitespace passes it — while `length(content) > 0` still
    # flips `has_text`, i.e. the reader is offered a dialog onto an empty document.
    with pytest.raises(ValidationError, match="must not be blank"):
        LicenseUpdate(content="   ")
    with pytest.raises(ValidationError, match="must not be blank"):
        LicenseCreate(name="n", short_description="s", content="\n\t ")


@pytest.mark.integration
async def test_resync_reconciles_the_curated_flag(db_session: AsyncSession) -> None:
    # The catalog owns the flag on curated rows, so a drifted row must come back to the catalog's value.
    await sync_licenses(db_session)
    curated = CURATED_LICENSES[0]
    row = await get_data_license(db_session, curated_license_id(curated.spdx_id))
    row.protects_conversation_data = not curated.protects_conversation_data
    await db_session.flush()

    await sync_licenses(db_session)

    await db_session.refresh(row)
    assert row.protects_conversation_data is curated.protects_conversation_data


@pytest.mark.integration
async def test_resync_leaves_a_user_authored_flag_alone(db_session: AsyncSession) -> None:
    # A user-authored licence has no catalog entry, so the reconciler must not reach it — otherwise the
    # owner's setting survives the API call and dies at the next deploy.
    owner = await _user(db_session)
    authored = await _own_license(db_session, owner, name="Acme Confidential 1.0")
    authored.protects_conversation_data = True
    await db_session.flush()

    await sync_licenses(db_session)

    await db_session.refresh(authored)
    assert authored.protects_conversation_data is True
