"""Issue an access token for a client.

Authentication is token-only: there is no sign-up and no password reset, so
this script is how a client gets in. Run it, hand the printed token over once,
and they paste it into the desktop app.

    python scripts/create_token.py --client "Aurora Retail" --days 30
    python scripts/create_token.py --client "Lumen Analytics" --months 6
    python scripts/create_token.py --client "Aurora Retail" --days 30 --org "Aurora Retail"

    python scripts/create_token.py --list              # show issued tokens
    python scripts/create_token.py --revoke pp_live_x  # lock a client out now

A token is bound to a user account, because that is what carries organization
membership — the thing every tenant-scoped query filters on. `--org` creates or
reuses an account for the client and puts it in that organization. Without it
the token authenticates but has no tenant to read, which the API reports
plainly rather than failing somewhere further in.

On the server, run it inside the backend container:

    docker compose -f docker-compose.prod.yml exec -T backend \\
      python - < scripts/create_token.py --client "Name" --days 30

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
    Organization,
    OrganizationMember,
    User,
)
from app.security import generate_token  # noqa: E402


def slugify(name: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in name.strip()]
    return "".join(keep).strip("-").replace("--", "-") or "client"


async def resolve_user(session, client_name: str, org_name: str | None) -> User:
    """The account this token acts as, created if it does not exist yet.

    The email is synthetic and unreachable on purpose: it is an internal
    identifier, not a login. Nothing is ever sent to it, because there is no
    password to reset and no email flow left in the system.
    """
    email = f"{slugify(client_name)}@token.pingpulse.local"

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    if user is None:
        user = User(
            email=email,
            full_name=client_name,
            # Not a hash of anything — there is no password to check it
            # against. The column is NOT NULL and predates token auth.
            password_hash="!token-only",
        )
        session.add(user)
        await session.flush()
        print(f"  created account   {email}")
    else:
        print(f"  reusing account   {email}")

    if org_name:
        organization = (
            await session.execute(
                select(Organization).where(Organization.name == org_name)
            )
        ).scalar_one_or_none()
        if organization is None:
            raise SystemExit(
                f"  no organization named {org_name!r}. Seed it first, or omit --org."
            )

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
            print(f"  granted OWNER of  {organization.name}")
        user.active_organization_id = organization.id

    return user


async def create(args: argparse.Namespace) -> int:
    if args.months:
        # Calendar months are not a fixed length; 30 days each is close enough
        # for an access window and avoids a dependency on dateutil.
        days = args.months * 30
        window = f"{args.months} month{'s' if args.months != 1 else ''}"
    else:
        days = args.days
        window = f"{days} day{'s' if days != 1 else ''}"

    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    token = generate_token()

    async with SessionLocal() as session:
        user = await resolve_user(session, args.client, args.org)
        session.add(
            AccessToken(
                token=token,
                client_name=args.client,
                expires_at=expires_at,
                is_active=True,
                user_id=user.id,
            )
        )
        await session.commit()

    print()
    print("  " + "=" * 66)
    print(f"  ACCESS TOKEN FOR: {args.client}")
    print("  " + "=" * 66)
    print()
    print(f"  {token}")
    print()
    print(f"  valid for : {window}")
    print(f"  expires   : {expires_at:%Y-%m-%d %H:%M} UTC")
    if args.org:
        print(f"  organization: {args.org}")
    print()
    print("  Give this to the client once — it is not stored anywhere you can")
    print("  read it back from, and re-running this issues a different token.")
    print("  " + "=" * 66)
    return 0


async def list_tokens(_args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(AccessToken).order_by(AccessToken.created_at.desc())
            )
        ).scalars().all()

    if not rows:
        print("  no tokens issued yet")
        return 0

    print(f"  {'CLIENT':<24} {'TOKEN':<20} {'EXPIRES':<12} {'STATUS'}")
    print("  " + "-" * 70)
    for row in rows:
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if not row.is_active:
            state = "revoked"
        elif expires <= now:
            state = "expired"
        else:
            state = f"active ({(expires - now).days}d left)"
        # Only a prefix: a full token in a terminal is a credential on screen.
        print(f"  {row.client_name[:23]:<24} {row.token[:16] + '...':<20} "
              f"{expires:%Y-%m-%d}   {state}")
    return 0


async def revoke(args: argparse.Namespace) -> int:
    async with SessionLocal() as session:
        record = await session.get(AccessToken, args.revoke)
        if record is None:
            print(f"  no such token: {args.revoke[:16]}...")
            return 1
        record.is_active = False
        await session.commit()
        print(f"  revoked token for {record.client_name!r} — effective immediately")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Issue, list and revoke PingPulse access tokens.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--client", help="Client name this token is issued to")
    parser.add_argument("--days", type=int, default=30, help="Validity in days (default 30)")
    parser.add_argument("--months", type=int, help="Validity in months (overrides --days)")
    parser.add_argument("--org", help="Organization the client should land in")
    parser.add_argument("--list", action="store_true", help="List issued tokens")
    parser.add_argument("--revoke", metavar="TOKEN", help="Revoke a token immediately")

    args = parser.parse_args()

    if args.list:
        return asyncio.run(list_tokens(args))
    if args.revoke:
        return asyncio.run(revoke(args))
    if not args.client:
        parser.error("--client is required when issuing a token")
    if args.days <= 0 and not args.months:
        parser.error("--days must be positive")
    return asyncio.run(create(args))


if __name__ == "__main__":
    sys.exit(main())
