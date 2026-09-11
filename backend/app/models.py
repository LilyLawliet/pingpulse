"""ORM models.

Tenancy rule: every tenant-owned table carries `organization_id`, and every
query that reaches one goes through a dependency that pins it to the caller's
active organization. Rows are never addressed by primary key alone.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, DateTime, TypeDecorator

from app.database import Base

PIPELINE_STAGES = ("LEAD", "QUALIFIED", "DEMO_BOOKED", "CLOSED")

# How a tenant's WhatsApp is connected.
#   TWILIO     the official API; costs per message.
#   QR_SESSION a paired WhatsApp Web session; no per-message cost.
WHATSAPP_PROVIDERS = ("TWILIO", "QR_SESSION")
MEMBER_ROLES = ("OWNER", "ADMIN", "AGENT", "VIEWER")


class GUID(TypeDecorator):
    """Native PostgreSQL UUID, transparently stored as CHAR(36) on SQLite.

    Production DDL stays `uuid`; the test suite runs on SQLite, which cannot
    bind a uuid.UUID, so values are converted at the boundary instead.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


UUIDType = GUID()


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ============================== Identity ==================================
class User(Base):
    """A person. One login, many organizations."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    # Which organization this user is currently working in. Switching writes here.
    active_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = _now_column()

    memberships: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AccessToken(Base):
    """An issued access token. This is how a client authenticates — there are
    no passwords.

    Tokens are minted by `scripts/create_token.py`, handed to a client, and
    checked against this table on every request. Revoking one is a single
    `is_active = false`, which takes effect on the client's very next call —
    the reason for validating against the database rather than using a
    self-contained signed token that stays valid until it expires.

    `user_id` is not in the original spec but tenancy does not work without it.
    Every tenant-scoped query resolves the caller's organization through their
    membership, so a token that pointed at no one could not be scoped to a
    tenant at all. It carries the identity; the token carries the credential.
    """

    __tablename__ = "access_tokens"

    # The raw token is the primary key: one indexed lookup per request.
    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = _now_column()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    # Who this token acts as. Deleting the user takes their tokens with them.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # Useful operationally: shows whether an issued token was ever picked up.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # A licence is for one person and their team, not for passing around. Each
    # machine that uses the token claims a seat; once they are all claimed, a
    # new machine is refused rather than silently sharing the licence.
    max_devices: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )

    user: Mapped["User | None"] = relationship()
    devices: Mapped[list["TokenDevice"]] = relationship(
        back_populates="access_token", cascade="all, delete-orphan"
    )


class TokenDevice(Base):
    """One machine that has used a token.

    The desktop app generates a random id on first run and stores it locally,
    so the same installation keeps its seat across restarts while a copy of the
    token pasted on another machine asks for a new one.

    This is a licence control, not a security boundary: a determined user can
    clear their local id. It stops casual sharing — a token forwarded to five
    colleagues — which is what it is for.
    """

    __tablename__ = "token_devices"
    __table_args__ = (
        UniqueConstraint("token", "device_id", name="uq_token_device"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    token: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("access_tokens.token", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))

    first_seen_at: Mapped[datetime] = _now_column()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    access_token: Mapped["AccessToken"] = relationship(back_populates="devices")


class Organization(Base):
    """A tenant. Everything else in the system hangs off one of these."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sales_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    target_tone: Mapped[str | None] = mapped_column(String(255))
    product_rules: Mapped[str | None] = mapped_column(Text)

    # Regional settings, injected into every prompt this organization generates.
    default_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD", server_default="USD"
    )
    default_language: Mapped[str] = mapped_column(
        String(12), nullable=False, default="en", server_default="en"
    )
    # Where this organization's catalogue and policies are ingested from.
    primary_domain: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = _now_column()

    members: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    contacts: Mapped[list["CRMContact"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationMember(Base):
    """Which people may act inside which organization, and in what role."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_member_org_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="AGENT", server_default="AGENT"
    )
    created_at: Mapped[datetime] = _now_column()

    organization: Mapped["Organization"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")


# ============================== Channels ==================================
class ChannelConfig(Base):
    """How an organization is reached — one WhatsApp number per organization.

    Inbound routing uses this: the number Twilio delivered to identifies the
    tenant, so no global default is needed.
    """

    __tablename__ = "channel_configs"
    __table_args__ = (
        UniqueConstraint("channel", "phone_number", name="uq_channel_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(
        String(30), nullable=False, default="whatsapp", server_default="whatsapp"
    )
    provider: Mapped[str] = mapped_column(
        String(30), nullable=False, default="twilio", server_default="twilio"
    )
    phone_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # How this number is connected. Both providers write to the same tables,
    # so switching never hides or loses a conversation.
    whatsapp_provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="TWILIO", server_default="TWILIO"
    )

    # Optional per-tenant credentials; blank means use the platform defaults.
    # Only meaningful for TWILIO.
    account_sid: Mapped[str | None] = mapped_column(String(64))
    auth_token: Mapped[str | None] = mapped_column(String(128))

    # QR_SESSION only: what the bridge reports about the paired phone.
    #   PENDING / QR_READY / AUTHENTICATED / DISCONNECTED
    session_status: Mapped[str | None] = mapped_column(String(24))
    session_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = _now_column()

    organization: Mapped["Organization"] = relationship()


# ================================ CRM =====================================
class CRMContact(Base):
    """A lead. Unique per organization, not globally — the same person may
    talk to two different businesses on the platform."""

    __tablename__ = "crm_contacts"
    __table_args__ = (
        UniqueConstraint("organization_id", "phone_number", name="uq_contact_org_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phone_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # WhatsApp's privacy identifier for this person, when it addresses them by
    # one — "153231615328393" from 153231615328393@lid.
    #
    # It gets its own column because it is an identity, not incidental data. A
    # LID survives what a phone number does not: the same person switching the
    # account on their handset arrives under a different number, and without
    # this they become a second contact with a fifteen-digit identifier stored
    # where a dialable number should be. Matching on it is what keeps one
    # person one conversation.
    wa_lid: Mapped[str | None] = mapped_column(String(32), index=True)

    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    pipeline_stage: Mapped[str] = mapped_column(
        String(50), nullable=False, default="LEAD", server_default="LEAD"
    )

    # Free-form labels the operator applies from the CRM.
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    # The agent's own state machine, finer-grained than the CRM board above.
    # `pipeline_stage` stays the operator-facing rollup of this.
    sales_stage: Mapped[str] = mapped_column(
        String(30), nullable=False, default="NEW", server_default="NEW"
    )
    last_intent: Mapped[str | None] = mapped_column(String(120))
    next_action: Mapped[str | None] = mapped_column(String(120))

    # Structured customer memory. Each fact records where it came from and how
    # confident we are, so a stated budget outranks an inferred one, and a
    # withdrawn requirement is marked inactive rather than silently dropped.
    memory: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Free-form per-contact metadata. Holds the vision analysis of the last
    # image the customer sent, follow-up bookkeeping, and anything else that
    # does not deserve its own column. "metadata" is reserved on the
    # declarative base, so the attribute is renamed while the column is not.
    contact_metadata: Mapped[dict] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )

    # Facts learned from the conversation. Held on the contact rather than left
    # in the transcript, so they survive the chat-history window and carry over
    # to the customer's next conversation.
    city: Mapped[str | None] = mapped_column(String(120))
    shoe_size: Mapped[str | None] = mapped_column(String(20))
    category_interest: Mapped[str | None] = mapped_column(String(80))
    colour_preference: Mapped[str | None] = mapped_column(String(80))
    budget_note: Mapped[str | None] = mapped_column(String(160))

    created_at: Mapped[datetime] = _now_column()

    organization: Mapped["Organization"] = relationship(back_populates="contacts")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


# Kept so existing imports and call sites keep reading naturally.
Contact = CRMContact


# Who wrote a message.
#
# Until now there were two: the customer, and "the business". But the business
# speaks with two different voices — the model's, and a person's, when an
# operator takes a conversation over from the dashboard — and both were stored
# as SENDER_AGENT.
#
# Separating them matters beyond bookkeeping. The shop's own replies are the
# only honest record of how that shop actually talks to its customers, and that
# is what a persona should be learned from. Conflated with the model's output,
# learning from them means learning from the model's own words: a copy of a
# copy, drifting further from the shop with every round.
#
# It has to be recorded at the time. Nothing distinguishes an operator's reply
# from the agent's after both are written down as "agent", so rows created
# before this stay ambiguous forever. That is the reason this landed before the
# feature that needs it, rather than alongside it.
SENDER_CUSTOMER = "user"
SENDER_AGENT = "agent"
SENDER_OPERATOR = "operator"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("crm_contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SENDER_CUSTOMER | SENDER_AGENT | SENDER_OPERATOR. The last two are both
    # "the business", and telling them apart is the point: one is the model's
    # output, the other is a person typing in their own voice. See the constants
    # above for why that distinction cannot be recovered after the fact.
    sender: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    twilio_sid: Mapped[str | None] = mapped_column(String(100))

    # SENT | QUEUED | FAILED, for messages we sent. A null sid used to be the
    # only signal, and it could not tell "the transport was down and we will
    # try again" apart from "this will never arrive" — so the dashboard showed
    # both the same way. Inbound messages are SENT by definition: they arrived.
    delivery_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SENT", server_default="SENT"
    )

    # Attachments in either direction: images the customer sent (downloaded to
    # the media volume on D:) and product images the agent sent back.
    media_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = _now_column()

    contact: Mapped["CRMContact"] = relationship(back_populates="messages")
    llm_logs: Mapped[list["LLMLog"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class LLMLog(Base):
    __tablename__ = "llm_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # 'groq' | 'gemini'
    prompt_used: Mapped[str] = mapped_column(Text, nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Why a provider was abandoned. Without this a fallback reply looks
    # identical to a healthy one after the fact.
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_column()

    message: Mapped["Message | None"] = relationship(back_populates="llm_logs")


# ============================== Knowledge =================================
class KnowledgeDocument(Base):
    """A chunk of an organization's knowledge base, with its embedding.

    The embedding is stored as JSON rather than a native vector type so the
    same code runs on PostgreSQL and on the SQLite used by the test suite;
    similarity is computed in Python. `organization_id` is indexed because it
    is a filter on every single retrieval.
    """

    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255))

    # 'policy' for operational facts, 'product' for catalogue items. Product
    # rows carry media the agent can send straight back over WhatsApp.
    doc_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="policy", server_default="policy"
    )
    media_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Colour, fabric, price, collection, product URL - whatever the source gave.
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    embedding: Mapped[list[float] | None] = mapped_column(JSON)
    embedding_model: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _now_column()

    organization: Mapped["Organization"] = relationship()
