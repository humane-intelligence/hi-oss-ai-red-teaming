"""The platform-settings singleton — a one-row table (django-solo analogue).

Holds platform-wide policy that ships with a default but can be overridden and persisted:
data licensing, registration policy, password policy, and password-reset throttling. The
single-row invariant is enforced by a fixed sentinel primary key (`PLATFORM_SETTINGS_ID`)
plus the service being the only writer — see [service.py](service.py). No `server_default`
on `default_license_id`: the shipped default resolves from
`Settings.platform_default_data_license` (env) to the matching curated row at write time,
not baked into DDL, so it can change without a migration.

Every non-bookkeeping field here is a knob the service and the audit snapshot pick up by
derivation (`SETTINGS_KNOBS`), so adding one is a model change plus a migration.

This module must not import `app.core.auth.password_policy`: that module imports this one (its
`PasswordPolicy.from_settings` takes a row), so the reverse edge would close a cycle. The
password defaults below therefore repeat the floor that module owns — the two are held together
by `test_the_knob_default_tracks_the_schema_floor`, not by proximity. `schemas.py` is free to
import it — nothing on the auth side imports the schemas, so that direction stays acyclic.
"""

import uuid

from sqlmodel import Field

from app.core.base_model import BaseModel

# Fixed primary key of the one and only settings row. The service inserts/reads at this id
# exclusively, so there can never be a second row.
PLATFORM_SETTINGS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class PlatformSettings(BaseModel, table=True):
    __tablename__ = "platform_settings"

    # The platform default data license → `data_licenses.id` (a curated row). NOT NULL. No cascade —
    # the default must always resolve; deleting the referenced license is blocked by the service.
    default_license_id: uuid.UUID = Field(foreign_key="data_licenses.id", nullable=False, index=True)
    # Invite-only mode: open self-signup (`POST /auth/register`) is refused while set.
    invite_only: bool = Field(default=False, sa_column_kwargs={"nullable": False, "server_default": "false"})
    # Lifetime of email-verification links (hours). The DDL default only covers rows materialized
    # before this column existed; a transient read carries the env default instead (see service).
    email_verification_ttl_hours: int = Field(default=24, sa_column_kwargs={"nullable": False, "server_default": "24"})
    # Password policy. The defaults reproduce the shipped behaviour exactly: the length floor the
    # set-password schemas already carry, and no character-class requirements (the denylist and
    # identity-similarity rules are unconditional and deliberately not knobs — see password_policy).
    password_min_length: int = Field(default=8, sa_column_kwargs={"nullable": False, "server_default": "8"})
    password_require_uppercase: bool = Field(
        default=False, sa_column_kwargs={"nullable": False, "server_default": "false"}
    )
    password_require_digit: bool = Field(default=False, sa_column_kwargs={"nullable": False, "server_default": "false"})
    password_require_symbol: bool = Field(
        default=False, sa_column_kwargs={"nullable": False, "server_default": "false"}
    )
    # Password-reset throttling, measured against the account's own reset tokens. 0 seconds
    # disables the cooldown; the daily cap has no off value.
    password_reset_cooldown_seconds: int = Field(
        default=60, sa_column_kwargs={"nullable": False, "server_default": "60"}
    )
    password_reset_max_per_day: int = Field(default=5, sa_column_kwargs={"nullable": False, "server_default": "5"})
