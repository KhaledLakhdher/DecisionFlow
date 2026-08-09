"""Request and response bodies for authentication and workspace membership."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from decisionflow.db.models.tenancy import Role

# Long enough to resist offline cracking, short enough that argon2 does not
# become a denial-of-service vector on very long inputs.
PASSWORD_MIN = 12
PASSWORD_MAX = 128


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=PASSWORD_MAX)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str


class SwitchOrgRequest(BaseModel):
    org_id: uuid.UUID


class InviteRequest(BaseModel):
    email: EmailStr
    role: Role = Role.VIEWER

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("role")
    @classmethod
    def reject_owner(cls, value: Role) -> Role:
        # Ownership transfers are a separate, deliberate operation — never a
        # side effect of sending an invitation.
        if value is Role.OWNER:
            raise ValueError("Cannot invite a user directly as owner.")
        return value


class AcceptInviteRequest(BaseModel):
    token: str
    # Supplied only when the invitee does not yet have an account.
    password: str | None = Field(default=None, min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    full_name: str | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class OrganizationOut(_ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    plan: str


class MembershipOut(_ORMModel):
    org_id: uuid.UUID
    role: Role
    organization: OrganizationOut


class UserOut(_ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    is_active: bool
    created_at: datetime


class MeOut(BaseModel):
    user: UserOut
    active_org_id: uuid.UUID | None
    active_role: Role | None
    memberships: list[MembershipOut]


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - scheme name, not a credential
    expires_in: int  # seconds until the access token expires
    active_org_id: uuid.UUID | None = None
    active_role: Role | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    joined_at: datetime


class InvitationOut(_ORMModel):
    id: uuid.UUID
    email: EmailStr
    role: Role
    expires_at: datetime
    accepted_at: datetime | None


class InvitationCreatedOut(BaseModel):
    invitation: InvitationOut
    # Returned only because there is no mail transport wired up yet; once
    # invitations are emailed this must stop crossing the API boundary.
    invite_token: str
