"""Test fixtures: an in-memory SQLite database standing in for PostgreSQL.

The suite must run without Docker, so `get_db` is overridden with a SQLite
session and every outbound network call (Twilio, Groq, Gemini) is stubbed.
"""

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

# The suite must not need a live PostgreSQL, so the startup migration is off.
settings.auto_migrate_on_startup = False

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    app.dependency_overrides.clear()


# --------------------------- tenancy helpers ------------------------------
@pytest_asyncio.fixture
async def offline_embeddings(monkeypatch):
    """Deterministic vectors — no network, no API keys, stable assertions."""
    from app.services import embeddings

    async def fake_embed(text: str):
        return embeddings.hashed_embedding(text), embeddings.FALLBACK_MODEL

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    monkeypatch.setattr("app.services.retrieval.embed", fake_embed)
    return fake_embed


class Tenant:
    """A signed-in user with their own organization, ready to make requests."""

    def __init__(self, client, token, user_id, organization_id, email):
        self._client = client
        self.token = token
        self.user_id = user_id
        self.organization_id = organization_id
        self.email = email

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, url, **kwargs):
        return await self._client.get(url, headers=self.headers, **kwargs)

    async def post(self, url, **kwargs):
        return await self._client.post(url, headers=self.headers, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._client.patch(url, headers=self.headers, **kwargs)

    async def delete(self, url, **kwargs):
        return await self._client.delete(url, headers=self.headers, **kwargs)


async def make_tenant(client, email: str, organization_name: str) -> Tenant:
    response = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": email.split("@")[0],
            "organization_name": organization_name,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return Tenant(
        client=client,
        token=body["access_token"],
        user_id=body["user_id"],
        organization_id=body["active_organization_id"],
        email=email,
    )


@pytest_asyncio.fixture
async def org_a(client):
    return await make_tenant(client, "owner-a@example.com", "Alpha Shoes")


@pytest_asyncio.fixture
async def org_b(client):
    return await make_tenant(client, "owner-b@example.com", "Beta Motors")


@pytest_asyncio.fixture
async def default_org(db_session):
    """One organization, for tests that drive the pipeline directly.

    Inbound routing needs a tenant to deliver to; with exactly one
    organization and no channel configured, the webhook falls back to it.
    """
    from app.models import Organization

    organization = Organization(
        name="Test Shop",
        sales_prompt="Sell things.",
        target_tone="Warm",
        default_currency="USD",
        default_language="en",
    )
    db_session.add(organization)
    await db_session.flush()
    return organization


@pytest.fixture(autouse=True)
def offline_by_default(monkeypatch):
    """Unit tests must not touch the network or the broker.

    Both providers are pointed at a failure, which makes the analyzer and the
    profile extractor take their deterministic fallback paths. A test wanting a
    specific model response patches `_call_groq` itself; that patch is applied
    after this one, so it wins.
    """

    async def offline(*args, **kwargs):
        raise RuntimeError("network disabled in tests")

    async def no_vision(*args, **kwargs):
        return {}

    from app.services import llm_service, vision

    # Patched at the HTTP layer rather than at `_call_groq`, so the key
    # rotation and fallback logic above it still runs and stays testable.
    monkeypatch.setattr(llm_service, "_groq_once", offline)
    monkeypatch.setattr(llm_service, "_gemini_once", offline)
    monkeypatch.setattr(vision, "analyse_image", no_vision)

    # No Redis in unit tests; queuing is covered by the live suite.
    monkeypatch.setattr(settings, "followups_enabled", False)
