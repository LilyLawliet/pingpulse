"""Pydantic schemas for the webhook payload and dashboard API."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PipelineStage = Literal["LEAD", "QUALIFIED", "DEMO_BOOKED", "CLOSED"]


# ----------------------------- Twilio webhook -----------------------------
class TwilioWebhookPayload(BaseModel):
    """The x-www-form-urlencoded body Twilio POSTs for an inbound WhatsApp message.

    Field names are Twilio's PascalCase keys; aliases let FastAPI bind the form
    directly while the rest of the codebase uses snake_case.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    message_sid: str = Field(default="", alias="MessageSid")
    account_sid: str = Field(default="", alias="AccountSid")
    from_number: str = Field(default="", alias="From")
    to_number: str = Field(default="", alias="To")
    body: str = Field(default="", alias="Body")
    profile_name: str | None = Field(default=None, alias="ProfileName")
    wa_id: str | None = Field(default=None, alias="WaId")
    num_media: int = Field(default=0, alias="NumMedia")

    # The whole form, kept so media fields (MediaUrl0..N, MediaContentType0..N)
    # can be read without declaring every numbered key.
    raw: dict[str, str] = Field(default_factory=dict)

    @property
    def clean_from(self) -> str:
        """`whatsapp:+1555...` -> `+1555...`"""
        return self.from_number.replace("whatsapp:", "").strip()

    @property
    def clean_to(self) -> str:
        return self.to_number.replace("whatsapp:", "").strip()


# ----------------------------- Organizations -----------------------------
class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sales_prompt: str = Field(min_length=1)
    target_tone: str | None = None
    product_rules: str | None = None


class OrganizationUpdate(BaseModel):
    name: str | None = None
    sales_prompt: str | None = None
    target_tone: str | None = None
    product_rules: str | None = None


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    sales_prompt: str
    target_tone: str | None = None
    product_rules: str | None = None
    created_at: datetime


# ------------------------------- Contacts --------------------------------
class ContactCreate(BaseModel):
    phone_number: str = Field(min_length=3, max_length=50)
    name: str | None = None
    organization_id: uuid.UUID | None = None
    pipeline_stage: PipelineStage = "LEAD"


class ContactUpdate(BaseModel):
    name: str | None = None
    pipeline_stage: PipelineStage | None = None
    organization_id: uuid.UUID | None = None
    city: str | None = None
    shoe_size: str | None = None
    category_interest: str | None = None
    colour_preference: str | None = None
    budget_note: str | None = None


class CustomerProfile(BaseModel):
    """What the agent has learned about a customer. Every field is optional —
    the extractor returns only what was actually stated."""

    city: str | None = None
    shoe_size: str | None = None
    category_interest: str | None = None
    colour_preference: str | None = None
    budget_note: str | None = None

    def known(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v}


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID | None = None
    phone_number: str
    name: str | None = None
    pipeline_stage: str
    city: str | None = None
    shoe_size: str | None = None
    category_interest: str | None = None
    colour_preference: str | None = None
    budget_note: str | None = None
    created_at: datetime


# ------------------------------- Messages --------------------------------
class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    sender: str
    content: str
    twilio_sid: str | None = None
    # SENT | QUEUED | FAILED. The dashboard shows a different mark for each,
    # so a reply still waiting on a retry is never presented as delivered.
    delivery_status: str = "SENT"
    media_urls: list[str] = Field(default_factory=list)
    created_at: datetime


class OutboundMessageRequest(BaseModel):
    """Manual send from the dashboard (operator takeover)."""

    contact_id: uuid.UUID
    content: str = Field(min_length=1)


# ------------------------------- LLM logs --------------------------------
class LLMLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID | None = None
    provider: str
    prompt_used: str
    raw_response: str
    latency_ms: int
    error: str | None = None
    created_at: datetime


# ------------------------------- Health ----------------------------------
class ComponentHealth(BaseModel):
    status: Literal["ok", "degraded", "error", "skipped"]
    detail: str | None = None
    latency_ms: int | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "error"]
    app: str
    version: str
    checked_at: datetime
    components: dict[str, ComponentHealth]


# ------------------------------- LLM result ------------------------------
class GenerationResult(BaseModel):
    provider: str
    text: str
    prompt_used: str
    latency_ms: int
    fallback_used: bool = False
    error: str | None = None
