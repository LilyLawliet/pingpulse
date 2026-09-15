"""Schemas for identity, organizations, the CRM and the knowledge base."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Role = Literal["OWNER", "ADMIN", "AGENT", "VIEWER"]
# A stage key, not a fixed set. Which stages exist is now a per-organization
# question - a contractor's board and a salon's do not carry the same columns -
# so a Literal here would reject a tenant's own stage as an invalid value.
# Membership is checked against that organization's board at the endpoint,
# where the tenant is known, rather than by the type.
PipelineStage = Annotated[str, Field(min_length=1, max_length=40)]


# ------------------------------- Identity ---------------------------------
class TokenLoginRequest(BaseModel):
    """The only credential the system accepts."""

    token: str = Field(min_length=8, max_length=128)


class TokenSessionOut(BaseModel):
    """What a valid token resolves to.

    The token is echoed back so the desktop app can store exactly what the
    server accepted rather than whatever the user pasted, whitespace included.
    """

    token: str
    token_type: str = "bearer"
    client_name: str
    expires_at: datetime
    user_id: uuid.UUID | None = None
    active_organization_id: uuid.UUID | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    active_organization_id: uuid.UUID | None = None
    created_at: datetime


# ----------------------------- Organizations ------------------------------
class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sales_prompt: str = Field(default="You are a helpful sales agent.", min_length=1)
    target_tone: str | None = None
    product_rules: str | None = None
    default_currency: str = Field(default="USD", min_length=3, max_length=3)
    default_language: str = Field(default="en", min_length=2, max_length=12)


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    sales_prompt: str | None = None
    target_tone: str | None = None
    product_rules: str | None = None
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)
    default_language: str | None = Field(default=None, min_length=2, max_length=12)


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sales_prompt: str
    target_tone: str | None = None
    product_rules: str | None = None
    default_currency: str
    default_language: str
    created_at: datetime


class OrganizationMembershipOut(BaseModel):
    """An organization plus the caller's standing in it."""

    organization: OrganizationOut
    role: Role
    is_active: bool


class SwitchOrganizationRequest(BaseModel):
    organization_id: uuid.UUID


class InviteMemberRequest(BaseModel):
    email: EmailStr
    role: Role = "AGENT"


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    created_at: datetime


# ------------------------------ Channels ----------------------------------
class ChannelConfigCreate(BaseModel):
    phone_number: str = Field(min_length=3, max_length=50)
    channel: str = Field(default="whatsapp", max_length=30)
    provider: str = Field(default="twilio", max_length=30)
    # TWILIO or QR_SESSION. Defaults to the sanctioned transport.
    whatsapp_provider: str = Field(default="TWILIO", max_length=20)
    account_sid: str | None = None
    auth_token: str | None = None


class ChannelConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    channel: str
    provider: str
    whatsapp_provider: str = "TWILIO"
    phone_number: str
    is_active: bool
    created_at: datetime
    # QR_SESSION only; null for Twilio channels.
    session_status: str | None = None
    session_connected_at: datetime | None = None
    # Never serialised: the auth token is write-only.
    account_sid: str | None = None


# --------------------------------- CRM ------------------------------------
class CRMContactCreate(BaseModel):
    phone_number: str = Field(min_length=3, max_length=50)
    name: str | None = None
    email: EmailStr | None = None
    pipeline_stage: PipelineStage = "NEW_LEAD"
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None


class CRMContactUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    pipeline_stage: PipelineStage | None = None
    notes: str | None = None
    city: str | None = None
    shoe_size: str | None = None
    category_interest: str | None = None
    colour_preference: str | None = None
    budget_note: str | None = None

    # What a person needs on screen to act on a lead, typed in directly rather
    # than waited for the agent to extract.
    company: str | None = Field(default=None, max_length=255)
    service_requested: str | None = Field(default=None, max_length=255)
    project_address: str | None = None
    budget: str | None = Field(default=None, max_length=120)
    timeline: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, max_length=80)
    custom_fields: dict | None = None
    assigned_to: uuid.UUID | None = None


class TagRequest(BaseModel):
    tags: list[str] = Field(min_length=1)


class CRMContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    phone_number: str
    # WhatsApp's privacy identifier, when it addressed this person by one.
    # The dashboard reads it to tell a real number from a placeholder: where
    # phone_number equals this, WhatsApp has not told us the number yet and
    # showing it as one would be showing something undialable.
    wa_lid: str | None = None
    name: str | None = None
    email: str | None = None
    pipeline_stage: str
    # The agent's own state machine, plus what it has learned and decided.
    sales_stage: str = "NEW"
    last_intent: str | None = None
    next_action: str | None = None
    memory: dict = Field(default_factory=dict)
    # Vision analysis of the last photo they sent, plus follow-up bookkeeping.
    # The column is "metadata"; the ORM attribute is renamed because the
    # declarative base reserves that name.
    metadata: dict = Field(default_factory=dict, validation_alias="contact_metadata")
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    city: str | None = None
    shoe_size: str | None = None
    category_interest: str | None = None
    colour_preference: str | None = None
    budget_note: str | None = None

    # The structured profile the lead drawer shows.
    company: str | None = None
    service_requested: str | None = None
    project_address: str | None = None
    budget: str | None = None
    timeline: str | None = None
    source: str | None = None
    photo_urls: list[str] = Field(default_factory=list)
    custom_fields: dict = Field(default_factory=dict)
    qualification: dict = Field(default_factory=dict)
    summary: str | None = None

    # False when a person has taken this conversation over: messages still
    # arrive and are shown, nothing is generated for them.
    ai_enabled: bool = True
    assigned_to: uuid.UUID | None = None
    last_read_at: datetime | None = None

    # They asked us to stop. Nothing outbound may reach them again.
    opt_out: bool = False
    opt_out_at: datetime | None = None

    created_at: datetime


class CRMSummary(BaseModel):
    total: int
    by_stage: dict[str, int]
    by_tag: dict[str, int]


# ------------------------------ Knowledge ---------------------------------
class KnowledgeDocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    source: str | None = None


class KnowledgeDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    title: str
    content: str
    source: str | None = None
    embedding_model: str | None = None
    created_at: datetime


class RetrievedChunk(BaseModel):
    id: uuid.UUID
    title: str
    content: str
    score: float
    vector_score: float
    keyword_score: float
