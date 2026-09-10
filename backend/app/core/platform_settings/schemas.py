"""Request/response models for the platform-settings singleton."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from app.core.auth.password_policy import MAX_PASSWORD_LENGTH
from app.core.auth.password_policy import MIN_PASSWORD_LENGTH
from app.core.platform_settings.models import PlatformSettings


class PlatformSettingsResponse(BaseModel):
    """Platform-wide settings — data licensing, registration, password policy, reset throttling."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "default_license_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "invite_only": False,
                "email_verification_ttl_hours": 24,
                "password_min_length": 8,  # nosec B105
                "password_require_uppercase": False,  # nosec B105
                "password_require_digit": False,  # nosec B105
                "password_require_symbol": False,  # nosec B105
                "password_reset_cooldown_seconds": 60,  # nosec B105
                "password_reset_max_per_day": 5,  # nosec B105
                "updated_at": "2026-06-24T08:00:00Z",
            }
        }
    )

    default_license_id: UUID = Field(description="Licence id of the platform default data license.")
    invite_only: bool = Field(
        description="Invite-only mode — open self-signup (`POST /api/v1/auth/register`) is refused while set."
    )
    email_verification_ttl_hours: int = Field(
        description="How long a self-signup email-verification link stays valid, in hours."
    )
    password_min_length: int = Field(description="Minimum length of a newly set password, in characters.")
    password_require_uppercase: bool = Field(description="Whether a new password must contain an uppercase letter.")
    password_require_digit: bool = Field(description="Whether a new password must contain a digit.")
    password_require_symbol: bool = Field(
        description="Whether a new password must contain a symbol (anything that is not a letter or a digit)."
    )
    password_reset_cooldown_seconds: int = Field(
        description="Minimum gap between two self-service password-reset requests for one account, in seconds; "
        "`0` disables it. Admin-triggered sends ignore the gap but start it."
    )
    password_reset_max_per_day: int = Field(
        description="How many self-service password-reset requests one account can make per rolling 24 hours. "
        "Admin-triggered sends are not bound by the cap but consume it."
    )
    updated_at: datetime | None = Field(
        default=None,
        description="When the settings were last overridden; `null` means they are still the shipped defaults.",
    )

    @classmethod
    def from_model(cls, settings: PlatformSettings) -> Self:
        # A transient (never-overridden) instance has no updated_at — surface null.
        return cls(
            default_license_id=settings.default_license_id,
            invite_only=settings.invite_only,
            email_verification_ttl_hours=settings.email_verification_ttl_hours,
            password_min_length=settings.password_min_length,
            password_require_uppercase=settings.password_require_uppercase,
            password_require_digit=settings.password_require_digit,
            password_require_symbol=settings.password_require_symbol,
            password_reset_cooldown_seconds=settings.password_reset_cooldown_seconds,
            password_reset_max_per_day=settings.password_reset_max_per_day,
            updated_at=settings.updated_at,
        )


class PublicPasswordPolicyResponse(BaseModel):
    """The password rules a client can state up front, so a user isn't told them by a rejection."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "min_length": 8,
                "require_uppercase": False,
                "require_digit": False,
                "require_symbol": False,
            }
        }
    )

    min_length: int = Field(description="Minimum length of a new password, in characters.")
    require_uppercase: bool = Field(description="Whether a new password must contain an uppercase letter.")
    require_digit: bool = Field(description="Whether a new password must contain a digit.")
    require_symbol: bool = Field(
        description="Whether a new password must contain a symbol (anything that is not a letter or a digit)."
    )

    @classmethod
    def from_model(cls, settings: PlatformSettings) -> Self:
        return cls(
            min_length=settings.password_min_length,
            require_uppercase=settings.password_require_uppercase,
            require_digit=settings.password_require_digit,
            require_symbol=settings.password_require_symbol,
        )


class PublicPlatformSettingsResponse(BaseModel):
    """Unauthenticated subset of platform settings — safe to expose to anonymous clients."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "signup_enabled": True,
                "password_policy": {
                    "min_length": 8,
                    "require_uppercase": False,
                    "require_digit": False,
                    "require_symbol": False,
                },
            }
        }
    )

    signup_enabled: bool = Field(
        description="Whether open self-signup (`POST /api/v1/auth/register`) is enabled — "
        "the inverse of invite-only mode."
    )
    password_policy: PublicPasswordPolicyResponse = Field(
        description="Rules every screen that sets a password must satisfy. The reset-throttling "
        "settings are deliberately not here — they are operational, not part of the form contract."
    )


class PlatformSettingsUpdate(BaseModel):
    """Patch platform settings. Omit a field to leave it unchanged."""

    model_config = ConfigDict(extra="forbid")

    default_license_id: UUID | None = Field(
        default=None,
        description=(
            "Licence id (`GET /api/v1/licenses`). Omit to leave unchanged; must reference a live "
            "licence other than `No license`, which cannot be the platform default."
        ),
    )
    invite_only: bool | None = Field(
        default=None,
        description="Enable/disable invite-only mode (refuse open self-signup). Omit to leave unchanged.",
    )
    email_verification_ttl_hours: int | None = Field(
        default=None,
        ge=1,
        le=8760,
        description="Verification-link lifetime in hours (1-8760). Omit to leave unchanged.",
    )
    # Bounded by what the set-password schemas already carry into the contract — raising the
    # floor here can only narrow what those accept, and no setting can promise a password
    # longer than the cap those reject at.
    password_min_length: int | None = Field(
        default=None,
        ge=MIN_PASSWORD_LENGTH,
        le=MAX_PASSWORD_LENGTH,
        description=f"Minimum password length in characters "
        f"({MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH}). Omit to leave unchanged.",
    )
    password_require_uppercase: bool | None = Field(
        default=None,
        description="Require an uppercase letter in new passwords. Omit to leave unchanged.",
    )
    password_require_digit: bool | None = Field(
        default=None,
        description="Require a digit in new passwords. Omit to leave unchanged.",
    )
    password_require_symbol: bool | None = Field(
        default=None,
        description="Require a symbol (non-letter, non-digit) in new passwords. Omit to leave unchanged.",
    )
    password_reset_cooldown_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
        description="Minimum gap between two self-service reset requests for one account, in seconds "
        "(0-3600; 0 disables). Admin-triggered sends ignore the gap but start it. Omit to leave unchanged.",
    )
    password_reset_max_per_day: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Self-service reset requests one account can make per rolling 24 hours (1-100). "
        "Admin-triggered sends are not bound by the cap but consume it. Omit to leave unchanged.",
    )

    @field_validator("*")
    @classmethod
    def _reject_explicit_null(cls, value: UUID | bool | int | None) -> UUID | bool | int | None:
        # Omitted → default None, validator not run → "unchanged". An explicit null is
        # rejected (every knob always has a value — there is no "clear" state). Registered on
        # `*` because every field here is a knob, so a newly added one can't miss the rule.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value
