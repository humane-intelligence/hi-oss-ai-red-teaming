"""Auth identity records: `User`, `Role`, `UserRole`, `Invitation`, `ProviderIdentity`."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field
from sqlmodel import Relationship
from sqlmodel import SQLModel

from app.core.auth.object_roles.registry import ObjectType
from app.core.base_model import BaseModel
from app.core.organizations.models import Organization


class UserStatus(StrEnum):
    """Lifecycle state of a `User` account.

    `invited` is the default for admin-provisioned accounts that haven't
    completed onboarding yet; `pending` covers self-signup before email
    verification; `active` is a usable account; `inactive` is a deactivated
    one (distinct from soft-deleted, which uses `BaseModel.deleted_at`).
    """

    ACTIVE = "active"
    PENDING = "pending"
    INVITED = "invited"
    INACTIVE = "inactive"


type SettableUserStatus = Literal[UserStatus.ACTIVE, UserStatus.INACTIVE]
"""The two statuses an admin switches an account between.

`invited` / `pending` are owned by the onboarding flows: forcing `active` would
leave a passwordless account that still cannot log in, and overwriting either one
would discard onboarding state irrecoverably.
"""


class UserRole(SQLModel, table=True):
    """Many-to-many join between `User` and `Role`.

    Composite PK `(user_id, role_id)` blocks duplicate assignments. No timestamps —
    junction rows are not primary table models in the sense `BaseModel` encodes.
    """

    __tablename__ = "user_roles"

    user_id: uuid.UUID = Field(
        foreign_key="users.id",
        primary_key=True,
        nullable=False,
        ondelete="CASCADE",
    )
    role_id: uuid.UUID = Field(
        foreign_key="roles.id",
        primary_key=True,
        nullable=False,
        ondelete="CASCADE",
    )


class User(BaseModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        # Partial unique index: a soft-deleted account must not block
        # re-registering the same email. Uniqueness applies only to rows
        # where deleted_at IS NULL.
        Index(
            "ix_users_email",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_users_email_trgm",
            "email",
            postgresql_using="gin",
            postgresql_ops={"email": "gin_trgm_ops"},
        ),
        Index(
            "ix_users_first_name_trgm",
            "first_name",
            postgresql_using="gin",
            postgresql_ops={"first_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_users_last_name_trgm",
            "last_name",
            postgresql_using="gin",
            postgresql_ops={"last_name": "gin_trgm_ops"},
        ),
    )

    email: str = Field(max_length=320, nullable=False)
    email_verified_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    # Nullable so passwordless / external-IdP-only accounts are representable
    # without committing to a specific hash format here.
    password: str | None = Field(default=None, max_length=255)
    # Stamped when an OIDC activation wipes an existing password — lets self-service
    # password-reset distinguish "recovering a credential" from "never had one".
    password_cleared_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    # Tenancy: the organization the user belongs to. Nullable so self-signup /
    # legacy accounts are orgless (platform-wide scope); admins assign it later.
    # `SET NULL` so soft/hard-deleting an org never orphans a user row.
    organization_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="organizations.id",
        ondelete="SET NULL",
        nullable=True,
        index=True,
    )
    status: UserStatus = Field(
        default=UserStatus.INVITED,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            UserStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="userstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True},
    )
    # The Terms-of-Service version this account accepted, and when. The FK *is* the consent
    # record — a separate `consent_terms` boolean could disagree with it, so the API derives
    # that flag from this column instead. RESTRICT: a hard delete of a version must not erase
    # the consent that points at it.
    accepted_terms_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="terms_documents.id",
        ondelete="RESTRICT",
        nullable=True,
        index=True,
    )
    terms_accepted_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    # Answer to "send me email", recorded but not yet enforced anywhere: every template the
    # platform sends today is transactional (verification, invitation, reset, activation) and
    # goes out regardless. Enforcement belongs with the first optional template.
    consent_emails: bool = Field(default=False, sa_column_kwargs={"nullable": False, "server_default": "false"})

    # Loads via this relationship do NOT filter soft-deleted Role rows — attach
    # `with_live(Role)` to `.options(...)` (see app/core/soft_delete.py).
    roles: list[Role] = Relationship(back_populates="users", link_model=UserRole)
    # The owning organization. Eager-load with `selectinload(User.organization)`;
    # the response projection treats a soft-deleted org as no organization
    # (`OrganizationBase.from_optional_live`) — a many-to-one `with_live` doesn't
    # reliably filter across identity-map states, so the projection is the rule.
    organization: Organization | None = Relationship()
    # Platform-scoped invitations only (`object_type IS NULL`) — a group invite is a
    # different lifecycle and must not surface as the account's invitation state. Newest
    # first, so the response projection reads `invitations[0]`; `revoked_at` nulls-first
    # breaks the tie a re-invite creates (it revokes and re-issues in one transaction,
    # and `created_at` defaults to `now()`, which is transaction-start time), keeping the
    # live row ahead of the one just revoked. `foreign_keys` is required: `Invitation`
    # FKs `users` twice (invitee and inviter).
    invitations: list[Invitation] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "and_(User.id == Invitation.user_id, Invitation.object_type.is_(None))",
            "foreign_keys": "[Invitation.user_id]",
            "order_by": (
                "[desc(Invitation.created_at), nullsfirst(asc(Invitation.revoked_at)), desc(Invitation.expires_at)]"
            ),
            "viewonly": True,
        },
    )

    @property
    def full_name(self) -> str | None:
        """First and last name joined, or None when neither is set."""
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) if parts else None

    @property
    def display_name(self) -> str:
        """Human label for greetings / notifications — full name, else the email."""
        return self.full_name or self.email


class Role(BaseModel, table=True):
    __tablename__ = "roles"
    __table_args__ = (
        Index(
            "ix_roles_name",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # At most one live role may be the new-user default.
        Index(
            "ix_roles_is_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default AND deleted_at IS NULL"),
        ),
        # At most one live role may be the group self-join participant default.
        Index(
            "ix_roles_is_participant_default",
            "is_participant_default",
            unique=True,
            postgresql_where=text("is_participant_default AND deleted_at IS NULL"),
        ),
        # A default role must stay active: an inactive default makes its resolver
        # (get_default_role / get_participant_default_role) raise, 500-ing every
        # registration, group invite, or self-join.
        CheckConstraint("NOT (is_default AND NOT is_active)", name="default_role_active"),
        CheckConstraint("NOT (is_participant_default AND NOT is_active)", name="participant_default_role_active"),
        # Self-join grants the participant default as an *object* role, so it must be
        # object-assignable. Structural because self-join resolves the flag directly
        # instead of going through `resolve_assignable_roles`.
        CheckConstraint(
            "NOT (is_participant_default AND NOT is_object_assignable)",
            name="participant_default_role_assignable",
        ),
    )

    name: str = Field(max_length=64, nullable=False)
    # Stable slug `name` is the machine key (lookups, permission checks);
    # `display_name` is the human-facing label shown in the API and emails.
    # Nullable so ad-hoc/legacy roles fall back to `name` at the read sites;
    # `sync_system_roles` always populates it for canonical roles.
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=255)
    permissions: list[str] = Field(
        default_factory=list,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'[]'::jsonb")},
    )
    # True for canonical roles upserted by `sync_system_roles`; flips to read-only
    # downstream so user-management endpoints can't mutate or delete them.
    is_system: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    # An inactive role grants no permissions and can't be assigned; it stays in
    # the catalog so it can be reactivated without losing existing assignments.
    is_active: bool = Field(
        default=True,
        sa_column_kwargs={"nullable": False, "server_default": text("true")},
    )
    # The role auto-assigned to every new user; the partial-unique index above
    # keeps at most one live default.
    is_default: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    # The role granted on evaluation-group self-join; the partial-unique index
    # above keeps at most one live participant default.
    is_participant_default: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    # Whether the role may be held as an object (in-group) role. Code-owned for
    # system roles (`sync_system_roles` re-converges it from the object-role
    # registry); an operator opt-in for custom ones.
    is_object_assignable: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )

    # Loads via this relationship do NOT filter soft-deleted User rows — attach
    # `with_live(User)` to `.options(...)` (see app/core/soft_delete.py).
    users: list[User] = Relationship(back_populates="roles", link_model=UserRole)

    @property
    def label(self) -> str:
        """Human-facing name for display — `display_name`, or the slug `name` when unset."""
        return self.display_name or self.name


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"


class EmailVerificationStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    EXPIRED = "expired"
    REVOKED = "revoked"


class PasswordResetTokenStatus(StrEnum):
    PENDING = "pending"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"


class Invitation(BaseModel, table=True):
    """Token storage is `sha256(raw_token).hexdigest()`; raw tokens never touch the DB."""

    __tablename__ = "invitations"
    __table_args__ = (
        Index(
            "ix_invitations_token_hash",
            "token_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    user_id: uuid.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    token_hash: str = Field(max_length=64, nullable=False)
    expires_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False},
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    invited_by_user_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="users.id",
        nullable=True,
    )
    accepted_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    status: InvitationStatus = Field(
        default=InvitationStatus.PENDING,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            InvitationStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="invitationstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True},
    )
    # Optional per-object scope (e.g. an evaluation group). Scope only — no role
    # column; roles are pre-assigned on `(object_type, object_id)` by the issuing
    # domain. NULL = plain platform invitation. `object_id` carries no FK (polymorphic).
    object_type: ObjectType | None = Field(
        default=None,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            ObjectType,
            values_callable=lambda enum: [m.value for m in enum],
            name="objecttype",
        ),
        sa_column_kwargs={"nullable": True},
    )
    object_id: uuid.UUID | None = Field(default=None, nullable=True)


class EmailVerification(BaseModel, table=True):
    """Self-signup email-verification token.

    Lifecycle diverges from `Invitation` — never re-issued on re-registration
    the way invitations are on re-invite, and there is no inviter to record —
    so it gets its own table rather than overloading `Invitation`.
    """

    __tablename__ = "email_verifications"
    __table_args__ = (
        Index(
            "ix_email_verifications_token_hash",
            "token_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    user_id: uuid.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    token_hash: str = Field(max_length=64, nullable=False)
    expires_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False},
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    verified_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    status: EmailVerificationStatus = Field(
        default=EmailVerificationStatus.PENDING,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            EmailVerificationStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="emailverificationstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True},
    )


class PasswordResetToken(BaseModel, table=True):
    """Single-use password-reset link. Mirror of `Invitation`: raw token only in mail, sha256 in DB."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index(
            "ix_password_reset_tokens_token_hash",
            "token_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    user_id: uuid.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    token_hash: str = Field(max_length=64, nullable=False)
    expires_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False},
    )
    used_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    status: PasswordResetTokenStatus = Field(
        default=PasswordResetTokenStatus.PENDING,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            PasswordResetTokenStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="passwordresettokenstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True},
    )


class ProviderIdentity(BaseModel, table=True):
    """An external login identity linked to a local `User`.

    Lookup at login is by `(provider, subject)`; resolution returns `user_id`.
    Linking a second mechanism to the same human is another row with the same `user_id`.

    `provider` is an OIDC provider name (e.g. `"google"`, `"github"`); see the
    `subject` field below for what identifies the account within it. Only
    external logins write rows here: a password login resolves its user by
    email and mints from the `User` row directly, with no row in this table.
    """

    __tablename__ = "provider_identities"
    __table_args__ = (
        # Partial unique index, mirroring `ix_users_email`: one live row per IdP
        # account, while a soft-deleted one must not block re-linking it later.
        Index(
            "ix_provider_identities_provider_subject",
            "provider",
            "subject",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    provider: str = Field(max_length=64, nullable=False)
    # The IdP's stable user id (`sub`), never the email — an email can be
    # reassigned at the provider, `sub` cannot.
    subject: str = Field(max_length=255, nullable=False)
    user_id: uuid.UUID = Field(
        foreign_key="users.id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
