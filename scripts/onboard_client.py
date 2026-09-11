"""Set a client up end to end, in one command.

Creating a client touches four things, and only the first has a screen in the
dashboard: the organization, its WhatsApp channel and Twilio credentials, its
knowledge base, and an access token. Doing that by hand across an API client
and a psql prompt is slow and easy to get half-right — a channel without
credentials, or a token bound to no organization, both fail confusingly later.

    python scripts/onboard_client.py \\
        --name "Aurora Retail" \\
        --twilio-sid ACxxxxxxxx --twilio-token xxxxxxxx \\
        --whatsapp-number +14155238886 \\
        --currency AED --language en \\
        --knowledge ./client-docs \\
        --months 6

    python scripts/onboard_client.py --name "Aurora Retail" --show

Omit --whatsapp-number to hand over a client with no channel connected. They then
pick Twilio or WhatsApp Web themselves in the dashboard, and whichever they connect
claims the number for them:

    python scripts/onboard_client.py --name "Aurora Retail" --preset retail --months 12

Re-running is safe: an existing organization is updated rather than duplicated,
and its channel is re-pointed rather than clashing. A fresh token is issued
each run, because that is the only way to hand one over.

On the server, run it inside the backend container:

    docker compose -f deploy/gcp-vm/docker-compose.prod.yml exec -T backend \\
      python /dev/stdin --name "Client" ... < scripts/onboard_client.py

Nothing is written outside Drive D:.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AccessToken,
    ChannelConfig,
    KnowledgeDocument,
    Organization,
    OrganizationMember,
    User,
)
from app.security import generate_token  # noqa: E402
from app.services import retrieval  # noqa: E402

DEFAULT_PROMPT = """You are the sales assistant for {name}.

You are the business. You answer questions yourself, right now, from the facts you
are given. You never say a colleague will follow up and you never promise a callback.

Quote exact prices and availability. If someone asks to speak to a person or book a
call, send the booking link you are given rather than promising anything.

Never invent a product, a price or a delivery promise that is not in the facts below."""

RETAIL_PROMPT = """You are the sales assistant for {name}, a retail shop, answering
customers on WhatsApp.

You are the shop. You answer from the catalogue and policies you are given, right now,
in your own words. You never say a colleague will get back to them and you never
promise a callback.

How to sell:
  * Open by finding out what they are actually after — the item, the size, the colour,
    roughly what they want to spend. Ask one question at a time; this is a chat, not a
    form.
  * Quote exact prices and exact availability from the catalogue. If something is out
    of stock, say so plainly and offer the nearest thing you do have.
  * Remember what they have already told you. Asking a customer their size twice is the
    fastest way to lose them.
  * Be direct about delivery time, payment and returns when asked, using the policies
    you are given.
  * When they are ready, tell them clearly what happens next. If they ask for a person
    or want to book, send the booking link you are given rather than promising anything.

Never invent a product, a price, a size, a stock level or a delivery promise that is not
in the facts below. If you do not know, say you will check rather than guessing — a wrong
price quoted on WhatsApp is one the customer will hold the shop to."""

PRESETS = {"retail": RETAIL_PROMPT, "general": DEFAULT_PROMPT}


def slugify(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name.strip()).strip("-") or "client"


def step(number: int, total: int, text: str) -> None:
    print(f"\n\033[1;36m[{number}/{total}]\033[0m {text}")


def ok(text: str) -> None:
    print(f"    \033[1;32mok\033[0m {text}")


def note(text: str) -> None:
    print(f"    \033[1;33m--\033[0m {text}")


async def upsert_organization(session, args) -> Organization:
    organization = (
        await session.execute(select(Organization).where(Organization.name == args.name))
    ).scalar_one_or_none()

    if organization is None:
        organization = Organization(
            name=args.name,
            sales_prompt=args.prompt or PRESETS[args.preset].format(name=args.name),
        )
        session.add(organization)
        ok(f"created organization {args.name!r}")
    else:
        if args.prompt:
            organization.sales_prompt = args.prompt
        elif args.preset != "general":
            organization.sales_prompt = PRESETS[args.preset].format(name=args.name)
        ok(f"reusing organization {args.name!r}")

    organization.default_currency = args.currency
    organization.default_language = args.language
    if args.tone:
        organization.target_tone = args.tone
    if args.price_list:
        organization.product_rules = Path(args.price_list).read_text(encoding="utf-8")
        ok(f"loaded price list from {args.price_list}")
    if args.domain:
        organization.primary_domain = args.domain

    await session.flush()
    ok(f"currency {args.currency}, language {args.language}")
    return organization


async def attach_channel(session, organization: Organization, args) -> None:
    number = args.whatsapp_number.replace("whatsapp:", "").strip()

    existing = (
        await session.execute(
            select(ChannelConfig).where(ChannelConfig.phone_number == number)
        )
    ).scalar_one_or_none()

    if existing is not None and existing.organization_id != organization.id:
        raise SystemExit(
            f"    number {number} is already connected to another organization. "
            "A WhatsApp number routes to exactly one tenant."
        )

    channel = existing or ChannelConfig(
        organization_id=organization.id,
        channel="whatsapp",
        provider="twilio",
        phone_number=number,
    )
    channel.organization_id = organization.id
    channel.account_sid = args.twilio_sid
    channel.auth_token = args.twilio_token
    channel.is_active = True

    if existing is None:
        session.add(channel)
    await session.flush()

    if args.twilio_sid and args.twilio_token:
        ok(f"{number} using the client's own Twilio account ({args.twilio_sid[:10]}…)")
    else:
        # Half a credential pair is worse than none: it silently falls back.
        note(f"{number} will send on the platform Twilio account (no client credentials given)")


async def load_knowledge(session, organization: Organization, folder: str) -> int:
    path = Path(folder)
    if not path.exists():
        raise SystemExit(f"    {folder} does not exist")

    files = sorted(
        f for f in path.rglob("*")
        if f.is_file() and f.suffix.lower() in {".txt", ".md"}
    )
    if not files:
        note(f"no .txt or .md files found in {folder}")
        return 0

    for document in files:
        stored = await retrieval.index_document(
            session,
            organization_id=organization.id,
            title=document.stem.replace("-", " ").replace("_", " ").title(),
            content=document.read_text(encoding="utf-8", errors="replace"),
            source=str(document.name),
        )
        stored.doc_type = "policy"
        ok(f"indexed {document.name}")

    await session.flush()
    return len(files)


async def issue_token(session, organization: Organization, args) -> tuple[str, datetime]:
    email = f"{slugify(args.name)}@token.pingpulse.local"

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    if user is None:
        # Synthetic and unreachable on purpose: an internal identifier, not a
        # login. There is no password and no email flow left in the system.
        user = User(email=email, full_name=args.name, password_hash="!token-only")
        session.add(user)
        await session.flush()

    member = (
        await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.user_id == user.id,
                OrganizationMember.organization_id == organization.id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        session.add(
            OrganizationMember(
                organization_id=organization.id, user_id=user.id, role="OWNER"
            )
        )
    user.active_organization_id = organization.id

    days = args.months * 30 if args.months else args.days
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    token = generate_token()

    session.add(
        AccessToken(
            token=token,
            client_name=args.name,
            expires_at=expires_at,
            is_active=True,
            user_id=user.id,
        )
    )
    await session.flush()
    ok(f"token valid until {expires_at:%Y-%m-%d}")
    return token, expires_at


async def show(args) -> int:
    async with SessionLocal() as session:
        organization = (
            await session.execute(
                select(Organization).where(Organization.name == args.name)
            )
        ).scalar_one_or_none()
        if organization is None:
            print(f"  no organization named {args.name!r}")
            return 1

        channels = (
            await session.execute(
                select(ChannelConfig).where(
                    ChannelConfig.organization_id == organization.id
                )
            )
        ).scalars().all()
        documents = (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.organization_id == organization.id
                )
            )
        ).scalars().all()

        print(f"\n  {organization.name}")
        print(f"    currency / language : {organization.default_currency} / {organization.default_language}")
        print(f"    knowledge documents : {len(documents)}")
        for channel in channels:
            byok = "client's own Twilio" if channel.account_sid else "platform Twilio"
            print(f"    channel             : {channel.phone_number}  ({byok})")
        if not channels:
            print("    channel             : none connected — inbound messages cannot route here")
    return 0


async def run(args) -> int:
    total = 4 if args.knowledge else 3

    async with SessionLocal() as session:
        step(1, total, "Organization")
        organization = await upsert_organization(session, args)

        step(2, total, "WhatsApp channel")
        if args.whatsapp_number:
            await attach_channel(session, organization, args)
        else:
            # Deliberately left empty. A client who is going to pair their own
            # handset over WhatsApp Web has no number to give here, and one
            # invented for them would be a second channel the dashboard then
            # has to disconnect — the number a tenant is reached on is decided
            # by whichever method they connect, not before.
            note("none — the client connects Twilio or WhatsApp Web themselves")

        loaded = 0
        if args.knowledge:
            step(3, total, "Knowledge base")
            loaded = await load_knowledge(session, organization, args.knowledge)

        step(total, total, "Access token")
        token, expires_at = await issue_token(session, organization, args)

        await session.commit()

    number = (args.whatsapp_number or "").replace("whatsapp:", "").strip()
    webhook = f"{args.public_url.rstrip('/')}/api/v1/whatsapp/webhook"

    print("\n  " + "=" * 68)
    print(f"  {args.name.upper()} IS READY")
    print("  " + "=" * 68)
    print(f"\n  Access token   {token}")
    print(f"  Expires        {expires_at:%Y-%m-%d}")
    print(f"  WhatsApp       {number or 'the client connects their own'}")
    if loaded:
        print(f"  Knowledge      {loaded} document(s) indexed")
    print("\n  Give the client:")
    print("    1. PingPulse_Setup.exe, or the dashboard in a browser")
    print("    2. the access token above")

    if number:
        print("\n  Set in THEIR Twilio console (Messaging -> your sender -> webhook):")
        print(f"    {webhook}")
        print("\n  Signature validation is on, so it must be that exact URL —")
        print("  a mismatched scheme, host or trailing slash rejects every message.")
    else:
        print("\n  They connect WhatsApp themselves, in Settings:")
        print("    WhatsApp Web  scan the QR with the handset they sell from.")
        print("    Twilio        their own SID and auth token, then set the")
        print(f"                  webhook to {webhook}")
        print("\n  Either one claims the number for them alone. Connecting one")
        print("  disconnects the other, so there is never a question of which")
        print("  number a reply goes out on.")
    print("  " + "=" * 68)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Onboard a PingPulse client: organization, channel, knowledge, token.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", required=True, help="Client / business name")

    parser.add_argument("--twilio-sid", default="", help="Client's Twilio Account SID")
    parser.add_argument("--twilio-token", default="", help="Client's Twilio Auth Token")
    parser.add_argument(
        "--whatsapp-number",
        help=(
            "Their WhatsApp sender, e.g. +14155238886. Omit it to create the "
            "client with no channel, so they connect Twilio or WhatsApp Web "
            "themselves from the dashboard."
        ),
    )

    parser.add_argument("--currency", default="USD", help="ISO code, default USD")
    parser.add_argument("--language", default="en", help="Default reply language, default en")
    parser.add_argument("--tone", help="How the agent should sound")
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="general",
        help="Sales prompt to start from; default general",
    )
    parser.add_argument("--prompt", help="Override the preset entirely")
    parser.add_argument("--price-list", help="Text file of products and prices")
    parser.add_argument("--domain", help="The client's website")
    parser.add_argument("--knowledge", help="Folder of .txt/.md policies and FAQs")

    parser.add_argument("--days", type=int, default=180, help="Token validity, default 180")
    parser.add_argument("--months", type=int, help="Token validity in months")
    parser.add_argument(
        "--public-url",
        default="https://pingpulse.duckdns.org",
        help="Deployment base URL, used to print the webhook",
    )
    parser.add_argument("--show", action="store_true", help="Show a client's current setup")

    args = parser.parse_args()

    if args.show:
        return asyncio.run(show(args))

    if args.twilio_sid and not args.whatsapp_number:
        parser.error(
            "--twilio-sid needs --whatsapp-number: credentials belong to the "
            "number they send from. Omit both to let the client connect their own."
        )
    if bool(args.twilio_sid) != bool(args.twilio_token):
        parser.error(
            "--twilio-sid and --twilio-token must be given together: a tenant auth "
            "token only works with that tenant's SID"
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
