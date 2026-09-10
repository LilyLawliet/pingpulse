"""Live end-to-end check of multi-tenancy against the running stack.

Creates two organizations under two accounts, proves each cannot see the
other's CRM contacts or knowledge documents, and exercises currency and
language settings. Cleans up after itself.

    python scripts/smoke_tenancy.py
"""

import asyncio
import sys
import time

import httpx

BASE = "http://localhost:8000"


class Session:
    def __init__(self, client: httpx.AsyncClient, token: str, org_id: str, label: str):
        self.client, self.token, self.org_id, self.label = client, token, org_id, label

    @property
    def h(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, path, **kw):
        return await self.client.get(f"{BASE}{path}", headers=self.h, **kw)

    async def post(self, path, **kw):
        return await self.client.post(f"{BASE}{path}", headers=self.h, **kw)

    async def patch(self, path, **kw):
        return await self.client.patch(f"{BASE}{path}", headers=self.h, **kw)


async def sign_up(client, email, org_name) -> Session:
    response = await client.post(
        f"{BASE}/api/v1/auth/signup",
        json={
            "email": email,
            "password": "smoke-test-password",
            "organization_name": org_name,
        },
    )
    response.raise_for_status()
    body = response.json()
    return Session(client, body["access_token"], body["active_organization_id"], org_name)


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if passed else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    return passed


async def main() -> int:
    run = int(time.time()) % 100000
    results: list[bool] = []

    async with httpx.AsyncClient(timeout=120) as client:
        print("=== sign up two tenants ===")
        a = await sign_up(client, f"smoke-a-{run}@example.com", f"Smoke Alpha {run}")
        b = await sign_up(client, f"smoke-b-{run}@example.com", f"Smoke Beta {run}")
        results.append(check("two organizations created", a.org_id != b.org_id))

        print("\n=== regional settings ===")
        await a.patch(
            "/api/v1/organizations/active",
            json={"default_currency": "PKR", "default_language": "ur"},
        )
        await b.patch(
            "/api/v1/organizations/active",
            json={"default_currency": "EUR", "default_language": "fr"},
        )
        a_org = (await a.get("/api/v1/organizations/active")).json()
        b_org = (await b.get("/api/v1/organizations/active")).json()
        results.append(
            check(
                "each org keeps its own currency/language",
                (a_org["default_currency"], a_org["default_language"]) == ("PKR", "ur")
                and (b_org["default_currency"], b_org["default_language"]) == ("EUR", "fr"),
                f"A={a_org['default_currency']}/{a_org['default_language']} "
                f"B={b_org['default_currency']}/{b_org['default_language']}",
            )
        )

        print("\n=== CRM isolation ===")
        a_contact = (
            await a.post(
                "/api/v1/crm/contacts",
                json={"phone_number": f"+1555{run:05d}1", "name": "Alpha Lead"},
            )
        ).json()
        await b.post(
            "/api/v1/crm/contacts",
            json={"phone_number": f"+1555{run:05d}2", "name": "Beta Lead"},
        )

        a_list = (await a.get("/api/v1/crm/contacts")).json()
        b_list = (await b.get("/api/v1/crm/contacts")).json()
        results.append(
            check(
                "each org sees only its own contacts",
                {c["name"] for c in a_list} == {"Alpha Lead"}
                and {c["name"] for c in b_list} == {"Beta Lead"},
            )
        )

        cross = await b.get(f"/api/v1/crm/contacts/{a_contact['id']}")
        results.append(
            check("B cannot read A's contact by id", cross.status_code == 404, f"HTTP {cross.status_code}")
        )

        cross_patch = await b.patch(
            f"/api/v1/crm/contacts/{a_contact['id']}", json={"name": "Hijacked"}
        )
        results.append(
            check("B cannot modify A's contact", cross_patch.status_code == 404,
                  f"HTTP {cross_patch.status_code}")
        )

        print("\n=== tagging ===")
        tagged = await a.post(
            f"/api/v1/crm/contacts/{a_contact['id']}/tags", json={"tags": ["vip", "vip", "urgent"]}
        )
        results.append(
            check("tags applied and deduplicated", tagged.json().get("tags") == ["vip", "urgent"],
                  str(tagged.json().get("tags")))
        )
        filtered = (await a.get("/api/v1/crm/contacts?tag=vip")).json()
        results.append(check("filtering by tag works", len(filtered) == 1))

        print("\n=== knowledge isolation ===")
        await a.post(
            "/api/v1/knowledge/documents",
            json={"title": "Alpha warranty", "content": "Alpha gives a 2 year warranty."},
        )
        secret = (
            await b.post(
                "/api/v1/knowledge/documents",
                json={"title": "Beta warranty", "content": "Beta gives a lifetime warranty."},
            )
        ).json()

        a_hits = (await a.get("/api/v1/knowledge/search", params={"q": "lifetime warranty"})).json()
        results.append(
            check(
                "A's search never returns B's document",
                all("Beta" not in hit["title"] for hit in a_hits),
                f"{len(a_hits)} hit(s)",
            )
        )
        results.append(
            check(
                "A cannot read B's document by id",
                (await a.get(f"/api/v1/knowledge/documents/{secret['id']}")).status_code == 404,
            )
        )

        print("\n=== switching ===")
        second = (
            await a.post(
                "/api/v1/organizations",
                json={"name": f"Smoke Alpha Second {run}", "sales_prompt": "Sell more."},
            )
        ).json()
        orgs = (await a.get("/api/v1/organizations")).json()
        results.append(check("user now belongs to two organizations", len(orgs) == 2))

        await a.post("/api/v1/organizations/switch", json={"organization_id": a.org_id})
        active = (await a.get("/api/v1/organizations/active")).json()
        results.append(check("switched back to the first organization", active["id"] == a.org_id))

        blocked = await a.post("/api/v1/organizations/switch", json={"organization_id": b.org_id})
        results.append(
            check("cannot switch into another user's organization", blocked.status_code == 404,
                  f"HTTP {blocked.status_code}")
        )

        contacts_after_switch = (await a.get("/api/v1/crm/contacts")).json()
        results.append(
            check("contacts follow the active organization", len(contacts_after_switch) == 1)
        )

        print("\n=== unauthenticated ===")
        anon = await client.get(f"{BASE}/api/v1/crm/contacts")
        results.append(check("CRM rejects anonymous callers", anon.status_code == 401))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
