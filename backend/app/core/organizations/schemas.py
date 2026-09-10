"""Request/response schemas for the organizations API.

The service-owned update contract (`OrganizationUpdateChanges`) stays in
`services/organizations.py` — these are the HTTP-facing shapes only.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.core.organizations.models import Organization


class OrganizationCreate(BaseModel):
    """Payload accepted by `POST /v1/organizations`."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"name": "Acme Corp", "description": "Acme's red-teaming tenant."}}
    )

    name: str = Field(
        max_length=255, description="Organization name. Must be unique among live rows.", examples=["Acme Corp"]
    )
    description: str | None = Field(
        default=None,
        max_length=1024,
        description="Optional free-text description.",
        examples=["Acme's red-teaming tenant."],
    )


class OrganizationUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/organizations/{organization_id}`. All fields optional."""

    model_config = ConfigDict(json_schema_extra={"example": {"name": "Acme Corporation"}})

    name: str | None = Field(
        default=None, max_length=255, description="New name. Omit to leave unchanged.", examples=["Acme Corporation"]
    )
    description: str | None = Field(
        default=None, max_length=1024, description="New description. Explicit null clears it.", examples=["Updated."]
    )


class OrganizationBase(BaseModel):
    """Slim identity projection of an `Organization` — embedded where a response references one (e.g. a user's org)."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "name": "Acme Corp"}}
    )

    id: UUID = Field(description="Server-assigned organization identifier.")
    name: str = Field(description="Organization name.", examples=["Acme Corp"])

    @classmethod
    def from_organization(cls, organization: Organization) -> OrganizationBase:
        """Project an `Organization` ORM row into the slim embedded shape."""
        return cls(id=organization.id, name=organization.name)

    @classmethod
    def from_optional_live(cls, organization: Organization | None) -> OrganizationBase | None:
        """Project a *related* org for embedding, treating a soft-deleted (or absent) org as no relation.

        The None/soft-deleted guard is the generic `BaseModel.live`; this is the
        authoritative soft-delete rule for the embed (a many-to-one `with_live`
        does not reliably filter across identity-map states).
        """
        org = Organization.live(organization)
        return cls.from_organization(org) if org is not None else None


class OrganizationResponse(OrganizationBase):
    """Public view of an `Organization` row."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "name": "Acme Corp",
                "description": "Acme's red-teaming tenant.",
                "created_at": "2026-01-01T12:00:00Z",
                "updated_at": "2026-01-02T09:30:00Z",
            }
        }
    )

    description: str | None = Field(default=None, description="Free-text description, if any.")
    created_at: datetime = Field(description="UTC timestamp of creation.", examples=["2026-01-01T12:00:00Z"])
    updated_at: datetime = Field(description="UTC timestamp of the last update.", examples=["2026-01-02T09:30:00Z"])
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who deleted the organization; null on a live row, or when the delete predates this field.",
    )

    @classmethod
    def from_organization(cls, organization: Organization) -> OrganizationResponse:
        """Project an `Organization` ORM row into the public response shape."""
        return cls(
            id=organization.id,
            name=organization.name,
            description=organization.description,
            created_at=organization.created_at,
            updated_at=organization.updated_at,
            deleted_at=organization.deleted_at,
            deleted_by_id=organization.deleted_by_id,
        )


class OrganizationMemberAdd(BaseModel):
    """Payload accepted by `POST /v1/organizations/{organization_id}/members`."""

    model_config = ConfigDict(json_schema_extra={"example": {"user_id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b"}})

    user_id: UUID = Field(description="Identifier of the user to assign to this organization.")
