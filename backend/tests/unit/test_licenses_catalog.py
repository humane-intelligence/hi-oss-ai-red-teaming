"""Unit tests for the curated license catalog (pure logic, no I/O)."""

import pytest

from app.core.licenses.catalog import CURATED_BY_SPDX
from app.core.licenses.catalog import CURATED_LICENSES
from app.core.licenses.catalog import DEFAULT_DATA_LICENSE_SPDX_ID
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import CuratedLicense
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.catalog import effective_license
from app.core.licenses.catalog import is_curated_spdx
from app.core.licenses.catalog import is_selectable_platform_default


@pytest.mark.unit
def test_default_license_is_curated() -> None:
    # The shipped/env default must be a curated licence, else the platform default can't resolve.
    assert is_curated_spdx(DEFAULT_DATA_LICENSE_SPDX_ID)
    assert DEFAULT_DATA_LICENSE_SPDX_ID == "CC-BY-4.0"


@pytest.mark.unit
def test_is_curated_spdx_accepts_known_and_rejects_unknown() -> None:
    assert is_curated_spdx("CC0-1.0")
    assert not is_curated_spdx("NOPE")
    assert not is_curated_spdx("")


@pytest.mark.unit
def test_curated_license_id_is_deterministic_and_distinct() -> None:
    assert curated_license_id("CC-BY-4.0") == curated_license_id("CC-BY-4.0")
    assert curated_license_id("CC-BY-4.0") != curated_license_id("CC0-1.0")


@pytest.mark.unit
def test_curated_set_is_unique_and_has_metadata() -> None:
    ids = [lic.spdx_id for lic in CURATED_LICENSES]
    licensing = [lic for lic in CURATED_LICENSES if is_selectable_platform_default(lic.spdx_id)]

    assert len(ids) == len(set(ids))
    assert "CC-BY-4.0" in ids
    # The version/reference-url metadata below holds for real licences only; exactly one
    # entry (the no-license sentinel) is excluded.
    assert len(licensing) == len(ids) - 1
    for lic in licensing:
        assert lic.name
        assert lic.version
        assert lic.reference_url is not None
        assert lic.reference_url.startswith("https://")
    assert CURATED_BY_SPDX["CC-BY-4.0"].name.startswith("Creative Commons")


@pytest.mark.unit
def test_no_license_sentinel_is_curated_but_not_a_selectable_default() -> None:
    assert is_curated_spdx(NO_LICENSE_SPDX_ID)
    assert not is_selectable_platform_default(NO_LICENSE_SPDX_ID)
    assert is_selectable_platform_default(DEFAULT_DATA_LICENSE_SPDX_ID)


@pytest.mark.unit
def test_no_license_sentinel_ships_its_own_text() -> None:
    # The one curated entry whose text is not pending curation: there is no legal text to
    # source, so the catalog carries it and the detail page never reads as "not on record yet".
    sentinel = CURATED_BY_SPDX[NO_LICENSE_SPDX_ID]

    assert sentinel.content
    assert sentinel.short_description.startswith("Closed data")
    assert [lic.spdx_id for lic in CURATED_LICENSES if lic.content] == [NO_LICENSE_SPDX_ID]


@pytest.mark.unit
def test_no_license_sentinel_carries_no_publishable_metadata() -> None:
    sentinel = CURATED_BY_SPDX[NO_LICENSE_SPDX_ID]

    assert sentinel.name == "No license"
    assert not sentinel.publishes_spdx
    assert sentinel.version is None
    assert sentinel.reference_url is None
    assert all(lic.publishes_spdx for lic in CURATED_LICENSES if lic.spdx_id != NO_LICENSE_SPDX_ID)


@pytest.mark.unit
def test_only_the_no_license_sentinel_protects_conversation_data() -> None:
    # Every other curated entry is an open data licence: it grants redistribution, so sealing the
    # transcripts it governs would protect nothing anyone agreed to keep. The sentinel grants none.
    protecting = [lic.spdx_id for lic in CURATED_LICENSES if lic.protects_conversation_data]

    assert protecting == [NO_LICENSE_SPDX_ID]


@pytest.mark.unit
def test_software_licenses_are_excluded() -> None:
    # The catalog is data-only — software licenses (permissive + copyleft) are not curated.
    ids = {lic.spdx_id for lic in CURATED_LICENSES}
    software = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "Unlicense", "GPL-3.0-only", "MPL-2.0"}
    assert ids.isdisjoint(software)
    for spdx_id in software:
        assert not is_curated_spdx(spdx_id)


@pytest.mark.unit
def test_effective_license_prefers_override_else_default() -> None:
    assert effective_license("a", default="d") == "a"
    assert effective_license(None, default="d") == "d"


@pytest.mark.unit
def test_effective_license_cascades_most_specific_first() -> None:
    # Overrides are passed most-specific-first (evaluation, then group): the first set one wins.
    assert effective_license("a", "b", default="d") == "a"
    assert effective_license(None, "b", default="d") == "b"
    assert effective_license(None, None, default="d") == "d"
    assert effective_license(default="d") == "d"


@pytest.mark.unit
def test_a_published_licence_must_name_its_canonical_text() -> None:
    # `version`/`reference_url` are optional only for the entry that publishes no SPDX id (it stands
    # for the absence of a licence). Without this, a published entry could reach the console with
    # neither `content` nor `reference_url` — a bare label with no route to the terms.
    with pytest.raises(ValueError, match="reference_url"):
        CuratedLicense(spdx_id="CC-BY-4.0", name="CC BY 4.0", short_description="s")

    # The sentinel shape stays constructible.
    assert CuratedLicense(spdx_id="NONE", name="No license", short_description="s", publishes_spdx=False)
