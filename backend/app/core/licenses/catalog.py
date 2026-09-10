"""Curated data-license catalog — code-shipped seed data for the `data_licenses` table.

The curated set is the well-known Creative Commons / Open Data Commons / CDLA data licenses the
platform ships with, plus the `NONE` sentinel — the curated entry standing for data that carries no
licence at all, and the one entry `is_selectable_platform_default` rejects. `sync_licenses` (see
`service.py`) upserts them into the table, keyed by the
deterministic `curated_license_id`. Curated rows carry `created_by_id IS NULL` (an owner can't edit
them); user-authored licenses are plain table rows with no catalog entry. Software licenses
(MIT/Apache/BSD, the GPL family, …) are excluded — they govern code, not data. The platform default
is `CC-BY-4.0`.

Each entry's `content` holds the full legal text — empty until curated, except the `NONE` entry,
which has no external text to source and carries its own; `sync_licenses` writes it into the row.
"""

import uuid
from dataclasses import dataclass

# The shipped platform default — also the env default for `Settings.platform_default_data_license`.
# Must be a member of the curated set below (validated at startup).
DEFAULT_DATA_LICENSE_SPDX_ID = "CC-BY-4.0"

# Catalog key of the "no licence" entry — a group pointing at it carries no licence, and its
# evaluations and conversations inherit that instead of the platform default.
NO_LICENSE_SPDX_ID = "NONE"

# Fixed namespace so a curated license's row id is derived deterministically from its SPDX id
# (`curated_license_id`) — identical in the seed migration, `sync_licenses`, and any backfill,
# with no stored UUID literals to keep in sync.
_CURATED_NAMESPACE = uuid.UUID("b1e7c0de-0104-4104-8104-000000000104")


@dataclass(frozen=True, slots=True)
class CuratedLicense:
    """One curated license — metadata plus its full legal `content`.

    `spdx_id` is the catalog key; `publishes_spdx=False` keeps it out of the stored row's `spdx_id`
    column, so the entry reads as an unidentified licence everywhere the column is projected.
    """

    spdx_id: str
    name: str
    short_description: str
    version: str | None = None
    reference_url: str | None = None
    content: str = ""
    publishes_spdx: bool = True
    # Licences whose terms forbid redistributing raw conversations. Defaults False: adding a curated
    # entry never silently changes how transcripts are stored.
    protects_conversation_data: bool = False

    def __post_init__(self) -> None:
        """A published licence must name where its canonical text lives.

        `reference_url` is optional only for an entry that publishes no SPDX id: it stands for the
        absence of a licence, so there is no external document to point at. Without this, a published
        entry whose `content` is still empty would reach the console as a bare label with no route to
        the terms. `version` is optional throughout — a licence without a version number is normal.
        """
        if self.publishes_spdx and not self.reference_url:
            raise ValueError(f"Curated licence '{self.spdx_id}' publishes an SPDX id but no reference_url.")


def curated_license_id(spdx_id: str) -> uuid.UUID:
    """Deterministic `data_licenses.id` for a curated license (stable across envs and reseeds)."""
    return uuid.uuid5(_CURATED_NAMESPACE, spdx_id)


CURATED_LICENSES: tuple[CuratedLicense, ...] = (
    CuratedLicense(
        spdx_id="CC-BY-4.0",
        name="Creative Commons Attribution 4.0 International",
        version="4.0",
        short_description="Share and adapt for any purpose, including commercially, with attribution.",
        reference_url="https://creativecommons.org/licenses/by/4.0/legalcode",
    ),
    CuratedLicense(
        spdx_id="CC-BY-SA-4.0",
        name="Creative Commons Attribution-ShareAlike 4.0 International",
        version="4.0",
        short_description="Attribution required; derivatives must use the same license.",
        reference_url="https://creativecommons.org/licenses/by-sa/4.0/legalcode",
    ),
    CuratedLicense(
        spdx_id="CC-BY-NC-4.0",
        name="Creative Commons Attribution-NonCommercial 4.0 International",
        version="4.0",
        short_description="Attribution required; non-commercial use only.",
        reference_url="https://creativecommons.org/licenses/by-nc/4.0/legalcode",
    ),
    CuratedLicense(
        spdx_id="CC-BY-ND-4.0",
        name="Creative Commons Attribution-NoDerivatives 4.0 International",
        version="4.0",
        short_description="Share for any purpose with attribution; no derivatives may be distributed.",
        reference_url="https://creativecommons.org/licenses/by-nd/4.0/legalcode",
    ),
    CuratedLicense(
        spdx_id="CC-BY-NC-SA-4.0",
        name="Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International",
        version="4.0",
        short_description="Attribution required; non-commercial only; derivatives must use the same license.",
        reference_url="https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode",
    ),
    CuratedLicense(
        spdx_id="CC-BY-NC-ND-4.0",
        name="Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International",
        version="4.0",
        short_description="Attribution required; non-commercial only; no derivatives may be distributed.",
        reference_url="https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode",
    ),
    CuratedLicense(
        spdx_id="CC0-1.0",
        name="Creative Commons Zero v1.0 Universal",
        version="1.0",
        short_description="Public-domain dedication — no rights reserved, no attribution required.",
        reference_url="https://creativecommons.org/publicdomain/zero/1.0/legalcode",
    ),
    CuratedLicense(
        spdx_id="ODbL-1.0",
        name="Open Database License v1.0",
        version="1.0",
        short_description="Share and adapt databases with attribution and share-alike.",
        reference_url="https://opendatacommons.org/licenses/odbl/1-0/",
    ),
    CuratedLicense(
        spdx_id="ODC-By-1.0",
        name="Open Data Commons Attribution License v1.0",
        version="1.0",
        short_description="Share and adapt databases with attribution.",
        reference_url="https://opendatacommons.org/licenses/by/1-0/",
    ),
    CuratedLicense(
        spdx_id="PDDL-1.0",
        name="Open Data Commons Public Domain Dedication and License v1.0",
        version="1.0",
        short_description="Public-domain dedication for databases — no rights reserved.",
        reference_url="https://opendatacommons.org/licenses/pddl/1-0/",
    ),
    CuratedLicense(
        spdx_id="CDLA-Permissive-2.0",
        name="Community Data License Agreement Permissive 2.0",
        version="2.0",
        short_description="Dataset license: use and share data and derivatives freely, attribution-light.",
        reference_url="https://cdla.dev/permissive-2-0/",
    ),
    CuratedLicense(
        spdx_id="CDLA-Sharing-1.0",
        name="Community Data License Agreement Sharing 1.0",
        version="1.0",
        short_description="Dataset license with copyleft: derived data must be shared under the same terms.",
        reference_url="https://cdla.dev/sharing-1-0/",
    ),
    CuratedLicense(
        spdx_id=NO_LICENSE_SPDX_ID,
        name="No license",
        short_description="Closed data — no sharing, publication, or reuse outside this engagement.",
        # One logical line per item, no hard wraps: the text renders in containers as narrow as a
        # dialog on a phone, where a fixed wrap column breaks mid-sentence and leaves hanging indents.
        content=(
            "NO LICENSE — CLOSED DATA\n"
            "\n"
            "1. No data license applies to this engagement's data.\n"
            "2. No rights to use, copy, publish, redistribute, adapt or otherwise reuse the data are granted.\n"
            "3. Access is limited to those authorised on the engagement — its members, assigned reviewers "
            "and platform administrators — and only through this platform. Viewing grants no right to share.\n"
            '4. Exports carry "No license" and remain the property of the engagement\'s owner, governed by '
            "the agreement between the parties rather than by this entry.\n"
            "5. To make the data shareable, select an explicit data license on the evaluation group or on "
            "an individual evaluation.\n"
        ),
        publishes_spdx=False,
        protects_conversation_data=True,
    ),
)

CURATED_BY_SPDX: dict[str, CuratedLicense] = {lic.spdx_id: lic for lic in CURATED_LICENSES}


def is_curated_spdx(spdx_id: str) -> bool:
    """Whether `spdx_id` names a curated catalog entry."""
    return spdx_id in CURATED_BY_SPDX


CURATED_BY_ID: dict[uuid.UUID, CuratedLicense] = {curated_license_id(lic.spdx_id): lic for lic in CURATED_LICENSES}


def curated_ships_content(license_id: uuid.UUID) -> bool:
    """Whether the catalog carries this row's text, i.e. `sync_licenses` rewrites it on every deploy.

    The one fact the edit guard and the console both need: for an entry shipping no text the resync
    leaves the column alone (an API-supplied text survives), and for one shipping text code stays the
    source of truth. A user-authored id is unknown here and answers `False`.
    """
    entry = CURATED_BY_ID.get(license_id)
    return bool(entry and entry.content)


def is_selectable_platform_default(spdx_id: str) -> bool:
    """Whether `spdx_id` may be the platform default — curated, and an actual licence.

    Validates the env platform default: the no-license sentinel is curated but selecting it
    platform-wide would unlicense everything that inherits.
    """
    return is_curated_spdx(spdx_id) and spdx_id != NO_LICENSE_SPDX_ID


def effective_license[T](*overrides: T | None, default: T) -> T:
    """Resolve an effective license from most-specific-first overrides, else the platform `default`.

    Overrides are passed most-specific-first (evaluation, then its group), mirroring the
    inference-params cascade direction. The single home for the two-state inheritance rule, reused
    by every projection path. Generic over the layer value — a licence id or a loaded `DataLicense`.
    """
    for override in overrides:
        if override is not None:
            return override
    return default
