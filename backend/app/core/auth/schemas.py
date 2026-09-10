"""Request/response schemas and in-process identity types for the auth module."""

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Any
from uuid import UUID

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import EmailStr
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator
from pydantic import model_validator

from app.core.auth.models import InvitationStatus
from app.core.auth.models import SettableUserStatus
from app.core.auth.models import UserStatus
from app.core.auth.password_policy import NewPassword
from app.core.auth.services.tokens import project_expired
from app.core.bulk import BulkRequest
from app.core.bulk import BulkRow
from app.core.organizations.schemas import OrganizationBase
from app.core.terms.schemas import TermsDocumentSummary

if TYPE_CHECKING:
    from app.core.auth.models import Invitation
    from app.core.auth.models import Role
    from app.core.auth.models import User
    from app.core.auth.services.invitations import InvitationPreview as InvitationPreviewData


def _normalize_email(value: str) -> str:
    # Postgres's default collation is case-sensitive, so the partial unique
    # index on `users.email` treats "Ada@x.com" and "ada@x.com" as distinct.
    # Canonicalise here so the uniqueness contract holds end-to-end.
    return value.strip().lower()


NormalizedEmail = Annotated[EmailStr, AfterValidator(_normalize_email)]


class MeResponse(BaseModel):
    """Body of `/v1/auth/me` — the caller's identity plus their RBAC state.

    Session fields (`id`, `email`, `email_verified`, `provider`) are projected
    from the current bearer token. `provider` describes how this session was
    authenticated, not who the user is: an OIDC provider name or `"local"`
    for password auth.

    `first_name` / `last_name` / `has_password`, like `roles` and
    `permissions`, are resolved from the live database, not the token, so a
    rename or role change refetches fresh without waiting for the JWT to be
    re-minted. `permissions` is the flattened union across `roles` (the same
    value the JWT `permissions` claim carries at mint time) — the FE checks it
    as one flat set. For a session with no backing DB row (the user was
    soft-deleted after the token was minted) everything falls back to the
    token: names from the claims, `roles` empty, `permissions` the JWT claim,
    `has_password` false.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
                "email": "ada@example.com",
                "email_verified": True,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "provider": "google",
                "has_password": False,  # nosec B105 — a flag about the credential, not a credential
                "roles": [
                    {
                        "id": "a1b2c3d4-1111-2222-3333-444455556666",
                        "name": "red_teamer",
                        "display_name": "Red Teamer",
                    }
                ],
                "permissions": ["conversations:create", "flags:create"],
            }
        }
    )

    id: UUID = Field(description="Server-assigned user identifier.", examples=["f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b"])
    email: EmailStr = Field(description="Primary email address.", examples=["ada@example.com"])
    email_verified: bool = Field(
        default=False,
        description="Whether the email was verified by the authenticating provider.",
        examples=[True],
    )
    first_name: str | None = Field(default=None, description="Given name, if known.", examples=["Ada"])
    last_name: str | None = Field(default=None, description="Family name, if known.", examples=["Lovelace"])
    provider: str = Field(
        description='Mechanism that authenticated this session — OIDC provider name (e.g. "google") or "local".',
        examples=["google"],
    )
    has_password: bool = Field(
        default=False,
        description=(
            "Whether the account has a local password set, resolved live from the database. "
            "False for passwordless accounts (IdP-only, or an OIDC activation cleared it) — "
            "they cannot use the password-change endpoint."
        ),
        examples=[False],
    )
    organization: OrganizationBase | None = Field(
        default=None,
        description=(
            "Organization the caller belongs to, resolved live from the database. "
            "Null when orgless or when the organization has been deleted."
        ),
    )
    roles: list[RoleSummary] = Field(
        default_factory=list,
        description="Roles currently held by the caller (identity + label), resolved live from the database.",
    )
    permissions: list[str] = Field(
        default_factory=list,
        description=(
            "Effective permissions — the flattened union across the caller's roles. "
            "Same strings as the JWT `permissions` claim and the `/roles` catalog. "
            "Gate the UI on this union, not on a single role's permissions."
        ),
        examples=[["conversations:create", "flags:create"]],
    )
    consent_terms: bool = Field(
        default=False,
        description=(
            "Whether the account has accepted a version of the terms of service. Derived from the "
            "acceptance record itself, so it cannot disagree with it."
        ),
        examples=[True],
    )
    consent_emails: bool = Field(
        default=False,
        description=(
            "Whether the user agreed to receive email beyond the transactional kind. Recorded only — "
            "every template the platform sends today is transactional and goes out regardless."
        ),
        examples=[False],
    )
    accepted_terms: TermsDocumentSummary | None = Field(
        default=None,
        description=(
            "The version this account actually accepted. Null when it never accepted one, or when "
            "that version was tombstoned straight in the database (nothing in the API deletes one), "
            "which leaves `terms_accepted_at` set on its own. Not necessarily the current one: a "
            "client that names the current version next to `terms_accepted_at` states something "
            "false about the consent record whenever a newer version has landed."
        ),
    )
    terms_accepted_at: datetime | None = Field(
        default=None,
        description=(
            "When the account accepted the version it has on record (UTC), or null if never. Stays "
            "set when `accepted_terms` reads null because that version was tombstoned."
        ),
    )
    terms_acceptance_required: bool = Field(
        default=False,
        description=(
            "Whether the client must block the app on the acceptance gate: a version is published "
            "and this account has not accepted it. False while the platform has published none. "
            "A convenience, not the enforcement — while it is true, every authenticated endpoint "
            "except the ones that clear it answers 403 regardless of what the client does."
        ),
        examples=[False],
    )


class MeUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/auth/me`. All fields optional.

    Names and the email-consent flag — the caller's own row is the only target and
    self-service never touches role assignment. Terms consent is not here: it is granted
    against a specific version through `POST /me/terms`, and withdrawing it is account
    deletion, not a toggle.
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"first_name": "Ada", "last_name": "L.", "consent_emails": True}}
    )

    first_name: str | None = Field(default=None, max_length=255, description="New given name.", examples=["Ada"])
    last_name: str | None = Field(default=None, max_length=255, description="New family name.", examples=["L."])
    consent_emails: bool | None = Field(
        default=None,
        description=(
            "Whether to receive email beyond the transactional kind. Recorded only, not yet enforced. "
            "Omit to leave unchanged; `null` is rejected."
        ),
        examples=[True],
    )

    @field_validator("consent_emails")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # Backs a NOT NULL column, so `null` has no meaning — without this it parses as
        # "omitted" and the request is a silent no-op (same rule as `RoleUpdate`).
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class PasswordChange(BaseModel):
    """Payload accepted by `POST /v1/auth/me/password`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "current_password": "correct horse battery staple",  # nosec B105
                "password": "supersecret-12345",  # nosec B105
            }
        }
    )

    current_password: SecretStr = Field(description="The password currently on the account.")
    password: NewPassword = Field(description="New password.")


class LoginRequest(BaseModel):
    """Payload accepted by `POST /v1/auth/login` — email + password credentials."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "ada@example.com",
                "password": "correct horse battery staple",  # nosec B105
            }
        }
    )

    email: NormalizedEmail = Field(description="Primary email address.", examples=["ada@example.com"])
    password: SecretStr = Field(description="Plaintext password.")


class TokenResponse(BaseModel):
    """Body of a successful login response — OIDC callback or email + password."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXAiOiJhY2Nlc3MifQ...",  # nosec B105
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXAiOiJyZWZyZXNoIn0...",  # nosec B105
                "expires_in": 3600,
            }
        }
    )

    access_token: str = Field(
        description="Signed session JWT. Send as `Authorization: Bearer <token>` on subsequent requests.",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."],  # nosec B105
    )
    refresh_token: str = Field(
        description="Signed JWT to exchange for a new access token once `expires_in` elapses.",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXAiOiJyZWZyZXNoIn0..."],  # nosec B105
    )
    expires_in: int = Field(
        description="Lifetime of the access token in seconds, from issuance.",
        examples=[3600],
    )


class RefreshRequest(BaseModel):
    """Payload accepted by `POST /v1/auth/refresh` — the refresh token to exchange."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXAiOiJyZWZyZXNoIn0...",  # nosec B105
            }
        }
    )

    refresh_token: str = Field(
        min_length=1,
        description="Signed refresh JWT previously issued in a `TokenResponse`.",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXAiOiJyZWZyZXNoIn0..."],  # nosec B105
    )


class SessionUser(BaseModel):
    """Identity attached to `request.state.user` by `AuthMiddleware`.

    Materialised from JWT claims only — middleware never reads the DB, and
    `permissions` is frozen at mint time, so a claim change lags up to the JWT TTL
    unless something revokes the session. Revoked: force-logout, a status change to
    inactive, a self-service password change, a reset confirm, and role deactivation /
    deletion / permission removal (`services/roles.py`). Not revoked: a user
    soft-delete and a role unassignment — both lag the full TTL.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    email: EmailStr
    email_verified: bool
    first_name: str | None
    last_name: str | None
    provider: str
    permissions: frozenset[str] = Field(default_factory=frozenset)
    # Original-login instant (epoch seconds), carried by refresh tokens to cap
    # absolute session lifetime. `None` on access tokens — they don't carry it.
    auth_time: int | None = None
    # Token issuance instant (epoch seconds, the `iat` claim) — present on both
    # token types. Compared against the per-user revocation marker so a
    # force-logout invalidates every session minted at or before it.
    issued_at: int | None = None

    @property
    def full_name(self) -> str | None:
        """First and last name joined, or None when neither is set."""
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) if parts else None

    @property
    def display_name(self) -> str:
        """Human label for greetings / notifications — full name, else the email."""
        return self.full_name or self.email


class RoleBase(BaseModel):
    """Identity fields shared by every role projection (`RoleResponse`, compact refs)."""

    id: UUID = Field(description="Server-assigned role identifier.", examples=["a1b2c3d4-1111-2222-3333-444455556666"])
    name: str = Field(description="Stable role slug — used as the machine identifier.", examples=["red_teamer"])


class RoleResponse(RoleBase):
    """Public view of a `Role` row, embedded in `UserResponse.roles`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4-1111-2222-3333-444455556666",
                "name": "red_teamer",
                "display_name": "Red Teamer",
                "description": "Participates as a red teamer in assigned evaluations.",
                "permissions": ["users:read", "users:update", "users:delete"],
                "is_system": True,
                "is_active": True,
                "is_default": True,
                "is_participant_default": True,
                "is_object_assignable": True,
            }
        }
    )

    display_name: str = Field(
        description="Human-readable role label for display.",
        examples=["Red Teamer"],
    )
    description: str | None = Field(
        default=None,
        description="Human-readable purpose of the role.",
        examples=["Participates as a red teamer in assigned evaluations."],
    )
    permissions: list[str] = Field(
        default_factory=list,
        description="Permission strings granted by this role.",
        examples=[["users:read", "users:update"]],
    )
    is_system: bool = Field(
        description="True for canonical platform roles; such roles are read-only.",
        examples=[True],
    )
    is_active: bool = Field(
        description="Whether the role is active; an inactive role grants nothing and can't be assigned.",
        examples=[True],
    )
    is_default: bool = Field(
        description="True for the single role auto-assigned to every new user.",
        examples=[True],
    )
    is_participant_default: bool = Field(
        description="True for the single role granted on evaluation-group self-join.",
        examples=[True],
    )
    is_object_assignable: bool = Field(
        description="Whether the role can be held as an in-group (object) role, not only globally.",
        examples=[True],
    )
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the role; null on a live row, or when the delete predates this field.",
    )

    @classmethod
    def from_role(cls, role: Role) -> RoleResponse:
        """Project a `Role` ORM row into the public response shape.

        `display_name` falls back to the slug `name` for ad-hoc/legacy roles
        that predate the column.
        """
        return cls(
            id=role.id,
            name=role.name,
            display_name=role.label,
            description=role.description,
            permissions=list(role.permissions),
            is_system=role.is_system,
            is_active=role.is_active,
            is_default=role.is_default,
            is_participant_default=role.is_participant_default,
            is_object_assignable=role.is_object_assignable,
            deleted_at=role.deleted_at,
            deleted_by_id=role.deleted_by_id,
        )


class RoleSummary(RoleBase):
    """Slim role projection embedded wherever a response lists a user's roles.

    Four embed sites: identity (`/auth/me`, user responses) and object
    membership (`ObjectMemberResponse`, `GroupInvitationResponse`).
    Identity + display label only: no `permissions` (clients gate on the
    flattened `permissions` union, e.g. `MeResponse.permissions`), no
    `description` / `is_system`. The full role object with its grant set is
    served by the `/roles` catalog (`RoleResponse`).
    """

    display_name: str = Field(
        description="Human-readable role label for display.",
        examples=["Red Teamer"],
    )

    @classmethod
    def from_role(cls, role: Role) -> RoleSummary:
        """Project a `Role` ORM row into the slim embedded shape (`display_name` falls back to the slug)."""
        return cls(id=role.id, name=role.name, display_name=role.label)

    @classmethod
    def from_roles(cls, roles: list[Role]) -> list[RoleSummary]:
        """Project only the live, active held roles (a deactivated/deleted role grants nothing)."""
        return [cls.from_role(role) for role in roles if role.is_active and role.deleted_at is None]


class RoleCreate(BaseModel):
    """Payload for `POST /v1/roles` — creates an operator-defined (custom) role.

    The role is always active, non-default, and non-system. `permissions` must be
    known and delegable (not `users:update` / `roles:manage` / `users:manage_admin`);
    the service validates them and rejects a reserved or duplicate `name`.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "name": "lead_reviewer",
                "display_name": "Lead Reviewer",
                "description": "Reviews flagged submissions across groups.",
                "permissions": ["reviews:read", "reviews:annotate"],
            }
        },
    )

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
        description="Stable slug: lowercase letter first, then lowercase letters/digits/`_`/`-`.",
        examples=["lead_reviewer"],
    )
    display_name: str = Field(
        min_length=1, max_length=128, description="Human-readable label.", examples=["Lead Reviewer"]
    )
    description: str | None = Field(default=None, max_length=255, description="Optional purpose of the role.")
    permissions: list[str] = Field(
        default_factory=list,
        description="Permission strings to grant. Must be known and delegable.",
        examples=[["reviews:read", "reviews:annotate"]],
    )


class RoleUpdate(BaseModel):
    """Payload for `PATCH /v1/roles/{role_id}` — all fields optional.

    Custom roles accept `display_name` / `description` / `permissions` /
    `is_object_assignable` edits; system roles accept only `is_active` / `is_default` /
    `is_participant_default` (their code-owned fields reject edits, and their
    object-assignability is re-synced from code on every deploy). `is_default: true`
    makes this the sole new-user default and `is_participant_default: true` the sole
    group self-join default, each clearing the previous holder; neither flag can be
    cleared directly (reassign instead).

    Omitting a field leaves it unchanged. `description` is the only nullable column, so
    it is the only field where an explicit `null` means something (it clears); every
    other field rejects `null` rather than silently ignoring it.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {"is_active": False}})

    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="New label. Omit to leave unchanged; `null` is rejected.",
    )
    description: str | None = Field(
        default=None, max_length=255, description="New purpose; `null` clears the existing one."
    )
    permissions: list[str] | None = Field(
        default=None,
        description="Replacement permission set (custom roles only). Omit to leave unchanged; `null` is rejected.",
    )
    is_active: bool | None = Field(
        default=None, description="Activate or deactivate the role. Omit to leave unchanged; `null` is rejected."
    )
    is_default: bool | None = Field(
        default=None,
        description="Set this role as the sole new-user default. Omit to leave unchanged; `null` is rejected.",
    )
    is_participant_default: bool | None = Field(
        default=None,
        description=(
            "Set this role as the sole group self-join participant default. Omit to leave unchanged; "
            "`null` is rejected."
        ),
    )
    is_object_assignable: bool | None = Field(
        default=None,
        description=(
            "Allow this role to be held as an in-group (object) role, not only globally (custom roles only). "
            "Omit to leave unchanged; `null` is rejected."
        ),
    )

    @field_validator(
        "display_name", "permissions", "is_active", "is_default", "is_participant_default", "is_object_assignable"
    )
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # These back NOT NULL columns (or a flag setter), so `null` has no meaning — without
        # this it parses as "omitted" and the request is a silent no-op. `description` is
        # nullable and legitimately clears.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class UserBase(BaseModel):
    """Base identity fields shared by every user projection (`UserResponse`, compact refs)."""

    id: UUID = Field(description="Server-assigned user identifier.", examples=["f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b"])
    email: EmailStr = Field(description="Primary email address.", examples=["ada@example.com"])
    first_name: str | None = Field(default=None, description="Given name, if known.", examples=["Ada"])
    last_name: str | None = Field(default=None, description="Family name, if known.", examples=["Lovelace"])
    status: UserStatus = Field(description="Lifecycle state of the account.", examples=[UserStatus.ACTIVE])


class UserInvitationInfo(BaseModel):
    """Invitation state of an account, embedded in `UserResponse`.

    Projects the newest **platform-scoped** invitation issued for the user —
    object-scoped ones (an evaluation-group invite) are a separate lifecycle and
    are deliberately excluded. `status` is the *effective* one: a `pending` row
    past `expires_at` reads as `expired` without the row being rewritten.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "pending",
                "expires_at": "2026-01-08T12:00:00Z",
            }
        }
    )

    status: InvitationStatus = Field(
        description="Effective invitation status — `pending` past its expiry reads as `expired`.",
        examples=[InvitationStatus.PENDING],
    )
    expires_at: datetime = Field(
        description="UTC timestamp the invitation stops being acceptable.",
        examples=["2026-01-08T12:00:00Z"],
    )

    @classmethod
    def from_invitation(cls, invitation: Invitation) -> UserInvitationInfo:
        """Project an `Invitation` row, collapsing an overdue `pending` into `expired`."""
        return cls(
            status=project_expired(
                invitation.status,
                invitation.expires_at,
                pending=InvitationStatus.PENDING,
                expired=InvitationStatus.EXPIRED,
            ),
            expires_at=invitation.expires_at,
        )


class UserResponse(UserBase):
    """Public view of a `User` row returned by `/v1/auth/users` endpoints.

    `email_verified` is a boolean projection of the underlying
    `User.email_verified_at` timestamp — true iff verification has happened.
    Build instances via `UserResponse.from_user(user)` to keep that
    projection in one place. `roles` requires `user.roles` to be eagerly
    loaded — callers under an async session must pre-load via `selectinload`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
                "email": "ada@example.com",
                "email_verified": True,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "status": "active",
                "roles": [
                    {
                        "id": "a1b2c3d4-1111-2222-3333-444455556666",
                        "name": "admin",
                        "display_name": "Admin",
                    }
                ],
                "created_at": "2026-01-01T12:00:00Z",
                "updated_at": "2026-01-02T09:30:00Z",
            }
        }
    )

    email_verified: bool = Field(description="Whether the email has been verified.", examples=[True])
    organization: OrganizationBase | None = Field(
        default=None,
        description="Organization the user belongs to, or null when orgless or the organization has been deleted.",
    )
    roles: list[RoleSummary] = Field(
        default_factory=list,
        description="Roles assigned to this user (identity + label). Every live user has at least one.",
    )
    invitation: UserInvitationInfo | None = Field(
        default=None,
        description=(
            "Newest **platform** invitation issued for this account, or null when it has none — including "
            "an account invited only into an evaluation group, whose invitation is object-scoped. A null "
            "here therefore does not imply the account was never invited; read `status` for that."
        ),
    )
    created_at: datetime = Field(description="UTC timestamp of creation.", examples=["2026-01-01T12:00:00Z"])
    updated_at: datetime = Field(description="UTC timestamp of the last update.", examples=["2026-01-02T09:30:00Z"])
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the account; null on a live row, or when the delete predates this field.",
    )

    @classmethod
    def from_user(cls, user: User) -> UserResponse:
        """Project a `User` ORM row into the public response shape.

        Centralizes the `email_verified_at` → `email_verified` collapse so
        callers don't have to remember the rule at every conversion site.
        Requires `user.roles`, `user.organization`, and `user.invitations` to be
        eagerly loaded; a soft-deleted org is projected as no organization
        (`from_optional_live`).
        """
        return cls(
            id=user.id,
            email=user.email,
            email_verified=user.email_verified_at is not None,
            first_name=user.first_name,
            last_name=user.last_name,
            status=user.status,
            organization=OrganizationBase.from_optional_live(user.organization),
            roles=RoleSummary.from_roles(user.roles),
            invitation=UserInvitationInfo.from_invitation(user.invitations[0]) if user.invitations else None,
            created_at=user.created_at,
            updated_at=user.updated_at,
            deleted_at=user.deleted_at,
            deleted_by_id=user.deleted_by_id,
        )


class UserUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/auth/users/{user_id}`. All fields optional.

    Supplying `role_ids` replaces the user's full role set; the new set must
    contain at least one role (a user is never left role-less).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "first_name": "Ada",
                "last_name": "L.",
                "role_ids": ["a1b2c3d4-1111-2222-3333-444455556666"],
            }
        }
    )

    first_name: str | None = Field(default=None, max_length=255, description="New given name.", examples=["Ada"])
    last_name: str | None = Field(default=None, max_length=255, description="New family name.", examples=["L."])
    role_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        description="Replacement set of role IDs. Omit to leave unchanged; if supplied, must be non-empty.",
        examples=[["a1b2c3d4-1111-2222-3333-444455556666"]],
    )


class UserStatusChange(BaseModel):
    """Payload accepted by `POST /v1/auth/users/{user_id}/status`."""

    model_config = ConfigDict(json_schema_extra={"example": {"status": "inactive"}})

    status: SettableUserStatus = Field(
        description=(
            "Target account status. Only `active` and `inactive` are settable — an account still "
            "onboarding (`invited` / `pending`) is owned by the invitation and verification flows."
        ),
        examples=["inactive"],
    )


class UserStatusTarget(BaseModel):
    """A user and the status to put them in — bulk row and its result.

    The target status is per row because `BulkRequest` carries no batch-level
    fields; a UI activating a selection fills every row with the same value.
    """

    user_id: UUID = Field(description="User whose account status will be changed.")
    status: SettableUserStatus = Field(
        description="Target account status for this row. Only `active` and `inactive` are settable.",
        examples=["inactive"],
    )


class PasswordResetTarget(BaseModel):
    """A user to mail a password-reset link to — bulk row and its result."""

    user_id: UUID = Field(description="User who will be sent a password-reset link.")


class ForceLogoutTarget(BaseModel):
    """A user whose active sessions are to be revoked — bulk row and its result."""

    user_id: UUID = Field(description="User whose active sessions will be revoked.")


class InvitationCreate(BaseModel):
    """One invitee row of `POST /v1/auth/invitations/bulk`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "ada@example.com",
                "role_ids": ["a1b2c3d4-1111-2222-3333-444455556666"],
            },
        },
    )

    email: NormalizedEmail = Field(
        description="Recipient email. Normalized to lowercase.", examples=["ada@example.com"]
    )
    role_ids: list[UUID] = Field(
        min_length=1,
        description="Roles to assign to the invited user. At least one live role id is required.",
        examples=[["a1b2c3d4-1111-2222-3333-444455556666"]],
    )


# Tighter than the platform-wide `BULK_MAX_ROWS`: every row mails a real person, so a
# mistyped paste or a whole-company CSV must bounce at validation, not in the outbox.
MAX_INVITE_ROWS = 100


def assert_unique_invite_emails(emails: Iterable[str]) -> None:
    """Reject a repeated invitee within one bulk request.

    Two rows for one address can never both be meaningful — processing them anyway
    mails the invitee a dead accept link, because the later row revokes the earlier
    row's token. Emails are normalized before this runs, so case variants collide.

    Raises:
        ValueError: An address appears more than once.
    """
    seen: set[str] = set()
    for email in emails:
        if email in seen:
            msg = f"Duplicate email: {email!r}."
            raise ValueError(msg)
        seen.add(email)


class InvitationBulkRequest(BulkRequest[InvitationCreate]):
    """Bulk envelope of `POST /v1/auth/invitations/bulk` — the only way to invite."""

    rows: list[BulkRow[InvitationCreate]] = Field(
        min_length=1,
        max_length=MAX_INVITE_ROWS,
        description=f"Invitees, at least one and at most {MAX_INVITE_ROWS}.",
    )

    @model_validator(mode="after")
    def _validate_unique_emails(self) -> InvitationBulkRequest:
        assert_unique_invite_emails(row.data.email for row in self.rows)
        return self


class PasswordResetBulkRequest(BulkRequest[PasswordResetTarget]):
    """Bulk envelope of `POST /v1/auth/users/password-reset`.

    Shares `MAX_INVITE_ROWS` with the invitation bulks for the same reason — every
    row mails a real person, so an over-broad selection must bounce at validation
    rather than in the outbox.
    """

    rows: list[BulkRow[PasswordResetTarget]] = Field(
        min_length=1,
        max_length=MAX_INVITE_ROWS,
        description=f"Users to send a reset link to, at least one and at most {MAX_INVITE_ROWS}.",
    )

    @model_validator(mode="after")
    def _validate_unique_users(self) -> PasswordResetBulkRequest:
        """Reject a repeated user within one request.

        Issuing a token revokes the account's previous one, so two rows for one user
        mail two links of which the first is already dead — the same trap
        `assert_unique_invite_emails` exists to close. `ForceLogoutTarget` is
        idempotent per row and needs no such guard; `UserStatusTarget` only for
        repeated *identical* rows — two rows setting opposite statuses land on the
        last one, but keep the session revocation the intermediate `inactive` row
        already performed.
        """
        seen: set[UUID] = set()
        for row in self.rows:
            if row.data.user_id in seen:
                msg = f"Duplicate user_id: {row.data.user_id}."
                raise ValueError(msg)
            seen.add(row.data.user_id)
        return self


class InvitationResponse(BaseModel):
    """Public view of an `Invitation` row. Never carries the raw token."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "11111111-1111-1111-1111-111111111111",
                "user_id": "22222222-2222-2222-2222-222222222222",
                "email": "ada@example.com",
                "status": "pending",
                "expires_at": "2026-05-29T10:00:00Z",
                "created_at": "2026-05-22T10:00:00Z",
                "invited_by_user_id": "33333333-3333-3333-3333-333333333333",
            },
        },
    )

    id: UUID = Field(description="Invitation row identifier.")
    user_id: UUID = Field(description="Identifier of the user the invitation targets.")
    email: EmailStr = Field(description="Recipient email (mirrors the user's primary address).")
    status: InvitationStatus = Field(description="Lifecycle state of the invitation.")
    expires_at: datetime = Field(description="UTC timestamp after which the token stops working.")
    created_at: datetime = Field(description="UTC timestamp the invitation was issued.")
    invited_by_user_id: UUID | None = Field(
        default=None,
        description="Identifier of the admin who issued the invitation, if known.",
    )

    @classmethod
    def from_invitation(cls, invitation: Invitation, user: User) -> InvitationResponse:
        """Project an `Invitation` + its target `User` into the public response shape."""
        return cls(
            id=invitation.id,
            user_id=user.id,
            email=user.email,
            status=invitation.status,
            expires_at=invitation.expires_at,
            created_at=invitation.created_at,
            invited_by_user_id=invitation.invited_by_user_id,
        )


class InvitationPreview(BaseModel):
    """Body of `GET /v1/auth/invitations/accept` — public-safe fields the FE renders."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "ada@example.com",
                "inviter_name": "Bob Smith",
                "role_names": ["annotator"],
                "expires_at": "2026-05-29T10:00:00Z",
            },
        },
    )

    email: EmailStr = Field(description="Email the invitation was sent to.")
    inviter_name: str | None = Field(
        default=None,
        description='Display name or email of the admin who issued the invitation; "null" for system-issued invites.',
    )
    role_names: list[str] = Field(description="Roles that will be active once the invitation is accepted.")
    expires_at: datetime = Field(description="UTC timestamp after which the token stops working.")

    @classmethod
    def from_preview(cls, preview: InvitationPreviewData) -> InvitationPreview:
        """Project the service-layer dataclass into the public response shape."""
        return cls(
            email=preview.email,
            inviter_name=preview.inviter_name,
            role_names=preview.role_names,
            expires_at=preview.expires_at,
        )


class InvitationAccept(BaseModel):
    """Payload accepted by `POST /v1/auth/invitations/accept`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "token": "rUvY9N6_…",  # nosec B105
                "password": "supersecret-12345",  # nosec B105
                "first_name": "Ada",
                "last_name": "Lovelace",
                "consent_terms": True,
                "consent_emails": False,
                "terms_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            },
        },
    )

    token: str = Field(min_length=1, description="Raw invitation token from the accept URL.")
    password: NewPassword = Field(description="New password.")
    first_name: str | None = Field(default=None, max_length=255, description="Given name. Optional.")
    last_name: str | None = Field(default=None, max_length=255, description="Family name. Optional.")
    consent_terms: bool = Field(
        default=False,
        description=(
            "Whether the user accepts the current terms of service. Required while a version is "
            "published — a false value is refused with 400 (`errors[].type` is "
            "`consent_terms_required`). Ignored while the platform has published none."
        ),
        examples=[True],
    )
    consent_emails: bool = Field(
        default=False,
        description="Whether to receive email beyond the transactional kind. Recorded only, not yet enforced.",
        examples=[False],
    )
    terms_id: UUID | None = Field(
        default=None,
        description=(
            "The terms version being accepted — the `id` from `GET /api/v1/terms/current`. Required "
            "alongside `consent_terms` while a version is published: consent is recorded against the "
            "version the form actually rendered, so a stale (or omitted) id is refused with 409 rather "
            "than silently recorded against newer text."
        ),
    )


class RegisterRequest(BaseModel):
    """Payload accepted by `POST /v1/auth/register` — public self-signup."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "ada@example.com",
                "password": "supersecret-12345",  # nosec B105
                "first_name": "Ada",
                "last_name": "Lovelace",
                "consent_terms": True,
                "consent_emails": False,
                "terms_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            },
        },
    )

    email: NormalizedEmail = Field(
        description="Primary email address. Normalized to lowercase.", examples=["ada@example.com"]
    )
    password: NewPassword = Field(description="New password.")
    first_name: str | None = Field(default=None, max_length=255, description="Given name. Optional.")
    last_name: str | None = Field(default=None, max_length=255, description="Family name. Optional.")
    consent_terms: bool = Field(
        default=False,
        description=(
            "Whether the user accepts the current terms of service. Required while a version is "
            "published — a false value is refused with 400 (`errors[].type` is "
            "`consent_terms_required`). Ignored while the platform has published none."
        ),
        examples=[True],
    )
    consent_emails: bool = Field(
        default=False,
        description="Whether to receive email beyond the transactional kind. Recorded only, not yet enforced.",
        examples=[False],
    )
    terms_id: UUID | None = Field(
        default=None,
        description=(
            "The terms version being accepted — the `id` from `GET /api/v1/terms/current`. Required "
            "alongside `consent_terms` while a version is published: consent is recorded against the "
            "version the form actually rendered, so a stale (or omitted) id is refused with 409 rather "
            "than silently recorded against newer text."
        ),
    )


class EmailVerifyRequest(BaseModel):
    """Payload accepted by `POST /v1/auth/register/verify`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "token": "rUvY9N6_…",  # nosec B105
            },
        },
    )

    token: str = Field(min_length=1, description="Raw verification token from the email link.")


class ResendVerificationRequest(BaseModel):
    """Payload accepted by `POST /v1/auth/register/resend`."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"email": "ada@example.com"}},
    )

    email: NormalizedEmail = Field(description="Email of the pending self-signup.", examples=["ada@example.com"])


class PasswordResetRequest(BaseModel):
    """Payload accepted by `POST /v1/auth/password-resets/request`."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"email": "ada@example.com"}},
    )

    email: NormalizedEmail = Field(description="Account email.", examples=["ada@example.com"])


class PasswordResetConfirm(BaseModel):
    """Payload accepted by `POST /v1/auth/password-resets/confirm`."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "token": "rUvY9N6_…",  # nosec B105
                "password": "supersecret-12345",  # nosec B105
            },
        },
    )

    token: str = Field(min_length=1, description="Raw token from the password-reset email.")
    password: NewPassword = Field(description="New password.")
