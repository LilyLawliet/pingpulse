"""Provisioning a client, which is the one path a client cannot recover from.

Everything else in the system is visible to whoever is watching it. A client
provisioned wrongly is not: the token works, the dashboard opens, and the fault
only shows up when a real customer messages and the reply goes out on the wrong
number — or does not go out at all.

Two properties matter enough to pin down:

  * omitting the WhatsApp number leaves the tenant with no channel, so the
    client chooses Twilio or WhatsApp Web themselves and whichever they connect
    claims the number. A number invented on their behalf is a second channel
    the dashboard then has to disconnect;
  * the preset actually reaches the organization, because the sales prompt is
    the entire difference between a retail agent and a generic one, and nothing
    downstream would notice it was wrong.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "onboard_client.py"


def _load():
    """Import the script by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("onboard_client", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["onboard_client"] = module
    spec.loader.exec_module(module)
    return module


needs_script = pytest.mark.skipif(not SCRIPT.is_file(), reason="scripts/ not present")


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        name="Test Retail", preset="retail", prompt=None, tone=None,
        currency="AED", language="en", price_list=None, domain=None,
        knowledge=None, whatsapp_number=None, twilio_sid="", twilio_token="",
        days=180, months=12, public_url="https://example.invalid",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
async def provision(tmp_path, monkeypatch):
    """Run the script against a throwaway database of its own."""
    from app.database import Base

    module = _load()
    url = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/onboard.db"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(module, "SessionLocal", factory)

    yield module, factory
    await engine.dispose()


# ------------------------------------------------------- the client chooses
@needs_script
async def test_no_number_means_no_channel(provision, capsys):
    """The client connects their own, so nothing is claimed on their behalf."""
    from app.models import ChannelConfig

    module, factory = provision
    assert await module.run(_args()) == 0

    async with factory() as session:
        channels = (await session.execute(select(ChannelConfig))).scalars().all()

    assert channels == [], "a channel nobody asked for is one they must disconnect"
    assert "connects Twilio or WhatsApp Web themselves" in capsys.readouterr().out


@needs_script
async def test_a_number_still_connects_a_channel(provision):
    """The Twilio path is unchanged: given a number, it is claimed."""
    from app.models import ChannelConfig

    module, factory = provision
    assert await module.run(_args(whatsapp_number="+14155238886")) == 0

    async with factory() as session:
        channels = (await session.execute(select(ChannelConfig))).scalars().all()

    assert [c.phone_number for c in channels] == ["+14155238886"]


# ------------------------------------------------------------- the preset
@needs_script
async def test_the_retail_preset_reaches_the_organization(provision):
    """Nothing downstream would notice a generic prompt on a retail client."""
    from app.models import Organization

    module, factory = provision
    await module.run(_args())

    async with factory() as session:
        organization = (await session.execute(select(Organization))).scalar_one()

    assert "a retail shop" in organization.sales_prompt
    assert "Test Retail" in organization.sales_prompt, "the shop names itself"
    assert organization.default_currency == "AED"


@needs_script
async def test_the_token_is_bound_to_that_organization(provision):
    """A token pointing at no one cannot be scoped to a tenant at all."""
    from app.models import AccessToken, OrganizationMember

    module, factory = provision
    await module.run(_args())

    async with factory() as session:
        token = (await session.execute(select(AccessToken))).scalar_one()
        member = (await session.execute(select(OrganizationMember))).scalar_one()

    assert token.token.startswith("pp_live_")
    assert token.is_active is True
    assert token.user_id == member.user_id
    assert member.role == "OWNER"


# --------------------------------------------------------- refusing nonsense
@needs_script
def test_credentials_without_a_number_are_refused():
    """Twilio credentials belong to the number they send from.

    Accepting them alone would store a SID against no channel, which sends
    nothing and reads as working.
    """
    module = _load()
    argv = ["onboard_client.py", "--name", "X", "--twilio-sid", "AC1", "--twilio-token", "t"]

    with pytest.raises(SystemExit) as raised:
        sys.argv = argv
        module.main()

    assert raised.value.code == 2
