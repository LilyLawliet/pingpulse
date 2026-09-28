"""ORM models.

Tenancy rule: every tenant-owned table carries `organization_id`, and every
query that reaches one goes through a dependency that pins it to the caller's
active organization. Rows are never addressed by primary key alone.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, DateTime, TypeDecorator

from app.database import Base

# The operator's board. Nine stages rather than the four this shipped with,
# and per-organization rather than global: a roofer's board and a salon's do
# not carry the same columns, which is what `tenant_pipelines` exists for.
#
# These are the defaults an organization starts from. The keys are what a
# contact stores, so they stay stable; the labels are what a person reads, and
# a tenant may change the label, the order, the colour or the set itself.
DEFAULT_PIPELINE = (
    # key, label, colour, outcome
    ("NEW_LEAD", "New lead", "slate", None),
    ("CONTACTED", "Contacted", "sky", None),
    ("QUALIFIED", "Qualified", "cyan", None),
    ("ESTIMATE_SCHEDULED", "Estimate scheduled", "violet", "booked"),
    ("ESTIMATE_SENT", "Estimate sent", "amber", None),
    ("FOLLOW_UP", "Follow-up", "orange", None),
    ("WON", "Won", "emerald", "won"),
    ("LOST", "Lost", "rose", "lost"),
    ("UNQUALIFIED", "Unqualified", "zinc", "unqualified"),
)

# Where the four original stages land. Existing contacts are moved by the
# migration rather than left pointing at a column that no longer exists - there
# is a live client whose board must still have everyone on it after deploy.
#
# CLOSED becomes WON because the agent's own state machine has no losing end
# state: it reaches CLOSED only by way of READY_TO_BUY.
LEGACY_PIPELINE = {
    "LEAD": "NEW_LEAD",
    "QUALIFIED": "QUALIFIED",
    "DEMO_BOOKED": "ESTIMATE_SCHEDULED",
    "CLOSED": "WON",
}

PIPELINE_STAGES = tuple(key for key, _label, _colour, _outcome in DEFAULT_PIPELINE)

# What a stage means for counting. A board can be renamed and reordered freely,
# but analytics needs to know which column is a sale and which is a dead end,
# and asking the label would break the moment somebody translates it.
#
# "booked" is the answer to the client's complaint that one metric called
# "Booked" said nothing: an appointment, a job and a sale are three businesses'
# words for the same milestone, so the column carries the meaning and the
# screen shows whatever that tenant calls it.
PIPELINE_OUTCOMES = ("booked", "won", "lost", "unqualified")

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

    user: Mapped["User | None"] = relationship()


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

    # The secret in the calendar subscription URL, so the diary can be read by
    # the phone the business actually runs its day from. Null until somebody
    # asks for a feed - which is different from set up and empty - and
    # rotatable, because a subscription link that has been forwarded is a
    # link that has to be revocable without touching anything else.
    #
    # It is in the URL because that is all a subscribing calendar client can
    # send: it fetches for years and can never be prompted for a login.
    calendar_token: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )

    # How this shop writes, learned from replies a person at the shop actually
    # typed, and approved by a person before it takes effect.
    #
    # Style only, never facts. Content comes from product_rules and the
    # knowledge base; a voice example carrying a price would hand the model a
    # number with no product attached to it.
    #
    # Null means the agent writes in its default voice, which is what every
    # tenant already running keeps until somebody chooses otherwise.
    # How the agent is allowed to behave: business hours, service areas, the
    # services on offer, when to hand over to a person, what it must never
    # promise. A dict because the shape differs by industry and because every
    # one of these is injected into the prompt rather than branched on in code.
    agent_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Who to interrupt, and about what. {"events": [...], "email": "..."}.
    # Separate from agent_config because that shapes what the agent says to
    # customers and this shapes what the shop hears about - two settings that
    # get edited by different people for different reasons.
    notify_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # IANA name. Needed before the agent can offer an appointment time or
    # honour business hours - "9am" is meaningless without it, and the server
    # runs in UTC.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )

    voice_style: Mapped[str | None] = mapped_column(Text)
    voice_examples: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    voice_learned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    # Null until a QR pairing has been scanned. The handset reports its own
    # number when the session authenticates, so asking the operator to type it
    # first only created a second, unverified copy of a fact the bridge was
    # about to supply - and that copy is what let one handset be claimed twice
    # under two spellings. A Twilio channel still supplies it, because there is
    # no scan to learn it from.
    phone_number: Mapped[str | None] = mapped_column(String(50), index=True)

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
class TenantPipeline(Base):
    """One column on one organization's board.

    The board used to be a constant, which is right until the second industry
    arrives: "Estimate sent" means everything to a contractor and nothing to a
    salon. Rows here let each organization keep its own columns, in its own
    order, in its own words.

    `key` is what a contact stores and `label` is what a person reads, and they
    are deliberately separate. Renaming a column on screen must not orphan
    every contact standing in it, and translating a board must not change what
    the analytics count.
    """

    __tablename__ = "tenant_pipelines"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_pipeline_org_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    colour: Mapped[str] = mapped_column(
        String(16), nullable=False, default="slate", server_default="slate"
    )

    # 'booked' | 'won' | 'lost' | 'unqualified' | null. See PIPELINE_OUTCOMES.
    outcome: Mapped[str | None] = mapped_column(String(16))

    # Where a brand-new contact lands. Exactly one row per organization should
    # carry this; the seeder sets it on the first stage.
    is_entry: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = _now_column()

    organization: Mapped["Organization"] = relationship()


class CRMContact(Base):
    """A lead. Unique per organization, not globally — the same person may
    talk to two different businesses on the platform."""

    __tablename__ = "crm_contacts"
    __table_args__ = (
        UniqueConstraint("organization_id", "phone_number", name="uq_contact_org_phone"),
        # Every analytics query has the same shape: one tenant, one span of
        # time. The organization index alone still scans a tenant's whole
        # history to answer "the last seven days".
        Index("ix_contacts_org_created", "organization_id", "created_at"),
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
        String(50), nullable=False, default="NEW_LEAD", server_default="NEW_LEAD"
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

    # ---------------------------------------------------------------- profile
    # What a person needs on screen to act on a lead, rather than what the
    # agent happened to extract. These are columns and not entries in `memory`
    # because they are filtered and sorted on, and because an operator types
    # them in directly.
    company: Mapped[str | None] = mapped_column(String(255))
    service_requested: Mapped[str | None] = mapped_column(String(255))
    project_address: Mapped[str | None] = mapped_column(Text)
    budget: Mapped[str | None] = mapped_column(String(120))

    # What this job is actually worth, once somebody knows. Deliberately not
    # `budget`, which is free text holding whatever the customer said ("under
    # 5k-ish"), and deliberately never written by the model: a revenue figure
    # on a dashboard is acted on, and an inferred one is a guess wearing a
    # number's clothes. A person types this in.
    #
    # In the organization's own currency. A shop trades in one.
    deal_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    timeline: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str | None] = mapped_column(String(80))
    photo_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Anything this industry needs that the columns above do not name. Kept
    # deliberately loose: the alternative is a migration every time a tenant
    # asks for one more field.
    custom_fields: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # What the qualification run has collected so far - job type, location,
    # scope, ownership and the rest. A dict rather than columns because which
    # slots matter is a per-tenant question.
    qualification: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # A short standing summary of the conversation, rewritten as it moves on,
    # so somebody opening a thread does not have to read it from the top.
    summary: Mapped[str | None] = mapped_column(Text)

    # ---------------------------------------------------------------- control
    # Human takeover. False means the agent stays out of this conversation
    # entirely: inbound messages are recorded and shown, and nothing is
    # generated. Checked before any reply is composed, not after.
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # When an operator last opened this thread. Unread is derived from it
    # rather than stored as a flag, because a flag and the messages it
    # describes drift apart the first time anything writes one without the
    # other.
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---------------------------------------------------------------- consent
    # Somebody who said STOP. Nothing outbound may be sent to them again -
    # not a follow-up, not a broadcast, not an agent reply - and the timestamp
    # is kept because "when did they opt out" is the question asked when a
    # complaint arrives.
    opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    opt_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    __table_args__ = (
        Index("ix_messages_org_created", "organization_id", "created_at"),
    )

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


# ============================== Operations ================================
class AuditLog(Base):
    """Who changed what, and to what.

    Written for the configuration changes that alter how the agent treats
    customers - a prompt edit, a pipeline reshuffle, a voice being applied, a
    takeover. Not a general request log: an audit trail nobody can read is the
    same as no audit trail, so only the things somebody would later need to
    account for go in here.

    `user_id` is nullable and set null on delete, because the record of a
    change must outlive the account that made it.
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_id: Mapped[str | None] = mapped_column(String(64))

    # Before and after, for the fields that moved. Whole objects are not
    # stored: a prompt is long, and the question is always what changed.
    changes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _now_column()


class SystemError(Base):
    """An operational failure worth a person seeing.

    These already reach the container logs, which is the wrong place for them:
    a client cannot read those, and by the time anybody does the question has
    become "why did messages stop yesterday". Recorded per organization so the
    dashboard can answer that without an engineer.

    `organization_id` is nullable because some failures - a bridge that will
    not start, a broker that is gone - belong to no tenant in particular.
    """

    __tablename__ = "system_errors"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # 'whatsapp' | 'calendar' | 'llm' | 'delivery' | 'system'
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)

    # Set when an operator has seen it, so a list of failures can be worked
    # through rather than only accumulated.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()


# ---------------------------------------------------------------- appointments
# What an appointment can be. The distinction matters to the customer more than
# to us: somebody expecting a phone call at 2pm and somebody expecting a van at
# their house at 2pm want very different things from the same row, and the
# client's own complaint began with a confirmation that named neither.
APPOINTMENT_KINDS = ("phone", "onsite", "video", "other")

# Confirmed means it exists and is expected to happen. Pending means the
# booking operation has not completed yet and nobody may be told it has.
# Failed is kept rather than deleted, because "we tried to book you and it did
# not work" is a thing somebody has to be able to find out about afterwards.
APPOINTMENT_STATUSES = ("pending", "confirmed", "cancelled", "failed")

APPOINTMENT_CONFIRMED = "confirmed"
APPOINTMENT_PENDING = "pending"
APPOINTMENT_CANCELLED = "cancelled"
APPOINTMENT_FAILED = "failed"


class Appointment(Base):
    """One booking, and the only thing allowed to say one exists.

    Every confirmation the customer reads is rendered from a row here. That is
    the whole point of the table: the agent used to assemble a date and a time
    out of nothing and state them as fact, and there was no record to check it
    against - so "is my appointment confirmed?" had no answer except whatever
    the model wrote next.

    Times are stored in UTC and rendered in the organization's zone. The zone
    is copied onto the row rather than read from the organization at display
    time, because a shop that moves timezone must not silently reschedule
    every appointment it has already agreed with somebody.

    A cancelled row is updated, never deleted. A customer who asks "did you
    cancel that?" is owed an answer, and a missing row cannot give one.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointments_org_start", "organization_id", "starts_at"),
        Index("ix_appointments_contact", "contact_id", "starts_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("crm_contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The zone this was agreed in, frozen at the moment of agreement.
    timezone_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )

    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="onsite", server_default="onsite"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )

    # Where, for anything the customer has to be present at. Free text because
    # an address, a site name and "your Miami property" are all answers a shop
    # might legitimately give.
    location: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)

    # Who arranged it: the agent, a person on the dashboard, or an import.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="agent", server_default="agent"
    )

    # Why it failed, when it did. Read by the operator, not the customer.
    failure_reason: Mapped[str | None] = mapped_column(String(300))

    # The appointment this one replaced, so a reschedule is a chain rather
    # than two unrelated rows nobody can tell apart.
    replaces_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("appointments.id", ondelete="SET NULL")
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()

    contact: Mapped["CRMContact"] = relationship()

    @property
    def is_live(self) -> bool:
        """Does this row mean somebody is expected? Only confirmed counts."""
        return self.status == APPOINTMENT_CONFIRMED


class StageEvent(Base):
    """One contact moving from one column to another, and when.

    The board only ever held where a lead *is*. That answers the question the
    inbox asks and none of the questions a funnel asks: how many leads got as
    far as qualified, how many stalled at the estimate, how long it takes to
    go from a first message to a win. None of that is recoverable from a
    single current-stage column, because the moment a lead moves the previous
    answer is gone.

    So every move is written down as it happens. Only real transitions: a lead
    arriving is not one, because `created_at` on the contact already says when
    that happened and a second row saying the same thing would be one more
    place for the two to disagree. `from_stage` stays nullable for the case
    where a lead is imported straight onto a column partway down the board.

    Nothing was backfilled. Rows created before this existed have no history
    and inventing dates for them would put numbers on a chart that never
    happened - the funnel reads a contact's current stage as well as its
    events, so an untracked lead still counts where it stands, and only the
    timing of its journey is unknown rather than wrong.
    """

    __tablename__ = "stage_events"
    __table_args__ = (
        Index("ix_stage_events_org_at", "organization_id", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("crm_contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Null on the first event: there was nowhere to come from.
    from_stage: Mapped[str | None] = mapped_column(String(50))
    to_stage: Mapped[str] = mapped_column(String(50), nullable=False)

    # STAGE_SOURCES. Who moved it, not which code path - "the agent decided"
    # and "somebody dragged it" are different facts about the same lead.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="agent", server_default="agent"
    )
    at: Mapped[datetime] = _now_column()


# Who moved a lead. `agent` is the model reading a conversation, `operator` a
# person on the dashboard, `system` an import or a board being rewritten under
# contacts that were standing on it.
STAGE_AGENT = "agent"
STAGE_OPERATOR = "operator"
STAGE_SYSTEM = "system"
STAGE_SOURCES = (STAGE_AGENT, STAGE_OPERATOR, STAGE_SYSTEM)


# ============================ Notifications ===============================
class PushSubscription(Base):
    """One browser that has agreed to be interrupted.

    A push subscription is issued by the browser's own push service and is
    useless to anyone else, but it is still a durable handle on a person's
    device, so it is scoped to an organization and deleted the moment that
    service says it has expired.

    `endpoint` is unique rather than (organization, endpoint): the same browser
    resubscribing must replace its old row, not accumulate them, or one
    escalation arrives four times.
    """

    __tablename__ = "push_subscriptions"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_endpoint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(255), nullable=False)
    # Only so a person can tell two of their own devices apart when revoking.
    label: Mapped[str | None] = mapped_column(String(120))

    # Consecutive failures. A push service that says "gone" deletes the row
    # outright; this catches the slower kind of death, where a device stops
    # accepting anything without ever being declared dead.
    failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()


class Notification(Base):
    """Something worth interrupting somebody over, and whether it got through.

    Written before it is sent rather than after. The row is what the delivery
    task reads, so a broker that is down means a notification that goes out
    late rather than one that is lost - and the same row is what stops the
    second, third and fourth copy of the same alert from being sent, because
    "have we already told them about this contact" is a question with an
    answer in the database rather than in some worker's memory.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_org_event_at", "organization_id", "event", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("crm_contacts.id", ondelete="CASCADE"), index=True
    )

    # One of NOTIFY_EVENTS.
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # What happened on each channel: {"push": "2 of 2", "email": "skipped"}.
    # Kept because "I never got told" is the complaint, and without this there
    # is no way to tell a failed send from a notification nobody looked at.
    delivery: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()


# What a shop can be told about. The doc asked for hot leads, appointment
# requests, angry customers and unhandled queries; the rest are the
# operational ones a client cannot otherwise find out about at all.
#
# Defaults lean quiet. A notification channel that cries wolf is turned off
# within a week and then the escalation that mattered is missed too, so only
# the events a person would actually want their evening interrupted for are
# on to begin with.
NOTIFY_EVENTS: tuple[tuple[str, str, bool], ...] = (
    # key, what it means, on by default
    # First because it is the one that matters most and the one the rest of
    # this system cannot otherwise see. Every other event here is triggered by
    # an inbound message; a number that has been logged out receives nothing,
    # so silence is the symptom and nothing would ever fire.
    ("whatsapp_down", "Your WhatsApp number has stopped working", True),
    ("escalation", "Somebody asked for a person, or complained", True),
    ("booking", "Somebody wants to book a time", True),
    ("delivery_failure", "A message could not be delivered", True),
    ("opt_out", "Somebody asked to stop being messaged", True),
    ("new_lead", "A new person messaged for the first time", False),
    # On by default. It was off, and it is the event that fires when the agent
    # is out of its depth - which is the exact moment a person needs to know.
    # A customer asked for a human, the agent could not give them one, and
    # nobody was told anything.
    ("unanswered", "The agent could not answer something", True),
    # The AI providers stopped answering and replies came from the documents
    # alone. Nothing else says so: the customer still gets an answer, which
    # is the point, and nobody would notice they were getting worse ones.
    ("ai_down", "The AI stopped answering, so replies are coming from your documents alone", True),
)
NOTIFY_KEYS = tuple(key for key, _, _ in NOTIFY_EVENTS)
