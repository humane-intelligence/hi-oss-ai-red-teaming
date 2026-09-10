"""Reusable request/response shapes for object-member endpoints.

Object-type agnostic — a concrete route (e.g. evaluation-group members) mounts
these under its own path and supplies the `ObjectType`.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.core.auth.schemas import RoleSummary
from app.core.auth.schemas import UserBase

if TYPE_CHECKING:
    from app.core.auth.models import User
    from app.core.auth.object_roles.service import ObjectMember


class UserRef(UserBase):
    """Base identity of a member user, embedded in member responses."""

    @classmethod
    def from_user(cls, user: User) -> UserRef:
        """Project a `User` ORM row into the compact reference shape."""
        return cls(
            id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            status=user.status,
        )


class ObjectMemberResponse(BaseModel):
    """One member of an object and the set of roles they hold on it."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user": {
                    "id": "a1b2c3d4-1111-2222-3333-444455556666",
                    "email": "ada@example.com",
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "status": "active",
                },
                "roles": [
                    {"id": "b2c3d4e5-2222-3333-4444-555566667777", "name": "red_teamer", "display_name": "Red Teamer"},
                ],
            }
        }
    )

    user: UserRef = Field(description="Base identity of the member user.")
    roles: list[RoleSummary] = Field(description="Roles the user holds on this object.")

    @classmethod
    def from_member(cls, member: ObjectMember) -> ObjectMemberResponse:
        """Project an `ObjectMember` into the response shape."""
        return cls(user=UserRef.from_user(member.user), roles=RoleSummary.from_roles(member.roles))


class ObjectMemberCreate(BaseModel):
    """Body for assigning a user one or more roles on an object.

    Once a user holds any role on the object, those roles define their *entire*
    effective permission set there — never combined with their global
    permissions.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "a1b2c3d4-1111-2222-3333-444455556666",
                "role_ids": ["b2c3d4e5-2222-3333-4444-555566667777"],
            }
        }
    )

    user_id: UUID = Field(
        description="User to grant roles to.",
        examples=["a1b2c3d4-1111-2222-3333-444455556666"],
    )
    role_ids: list[UUID] = Field(
        min_length=1,
        description="Roles to grant; must reference roles assignable on this object type.",
        examples=[["b2c3d4e5-2222-3333-4444-555566667777"]],
    )


class ObjectMemberUpdate(BaseModel):
    """Body for replacing a member's role set on an object wholesale."""

    model_config = ConfigDict(json_schema_extra={"example": {"role_ids": ["b2c3d4e5-2222-3333-4444-555566667777"]}})

    role_ids: list[UUID] = Field(
        min_length=1,
        description="The complete set of roles the member should hold; replaces the current set.",
        examples=[["b2c3d4e5-2222-3333-4444-555566667777"]],
    )
