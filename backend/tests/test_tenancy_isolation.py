"""Organization A must never reach Organization B's data.

Each test drives the real HTTP surface with two signed-in tenants, so it
exercises the dependency chain that actually enforces isolation rather than
trusting the query layer in isolation.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def _make_contact(tenant, phone="+10000000001", name="Lead"):
    response = await tenant.post(
        "/api/v1/crm/contacts", json={"phone_number": phone, "name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _make_document(tenant, title, content):
    response = await tenant.post(
        "/api/v1/knowledge/documents", json={"title": title, "content": content}
    )
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------ CRM contacts -------------------------------
async def test_contacts_list_is_scoped_to_the_active_organization(org_a, org_b):
    await _make_contact(org_a, "+15550000001", "Alpha Lead")
    await _make_contact(org_b, "+15550000002", "Beta Lead")

    a_names = {c["name"] for c in (await org_a.get("/api/v1/crm/contacts")).json()}
    b_names = {c["name"] for c in (await org_b.get("/api/v1/crm/contacts")).json()}

    assert a_names == {"Alpha Lead"}
    assert b_names == {"Beta Lead"}


async def test_reading_another_orgs_contact_by_id_is_not_found(org_a, org_b):
    victim = await _make_contact(org_b, "+15550000003", "Beta Secret")

    response = await org_a.get(f"/api/v1/crm/contacts/{victim['id']}")

    # 404, not 403 — existence is not disclosed across the boundary.
    assert response.status_code == 404


async def test_updating_another_orgs_contact_is_not_found(org_a, org_b):
    victim = await _make_contact(org_b, "+15550000004", "Beta Secret")

    response = await org_a.patch(
        f"/api/v1/crm/contacts/{victim['id']}", json={"name": "Hijacked"}
    )

    assert response.status_code == 404
    # And the row is untouched.
    unchanged = (await org_b.get(f"/api/v1/crm/contacts/{victim['id']}")).json()
    assert unchanged["name"] == "Beta Secret"


async def test_deleting_another_orgs_contact_is_not_found(org_a, org_b):
    victim = await _make_contact(org_b, "+15550000005", "Beta Secret")

    assert (await org_a.delete(f"/api/v1/crm/contacts/{victim['id']}")).status_code == 404
    assert (await org_b.get(f"/api/v1/crm/contacts/{victim['id']}")).status_code == 200


async def test_tagging_another_orgs_contact_is_not_found(org_a, org_b):
    victim = await _make_contact(org_b, "+15550000006", "Beta Secret")

    response = await org_a.post(
        f"/api/v1/crm/contacts/{victim['id']}/tags", json={"tags": ["stolen"]}
    )

    assert response.status_code == 404
    assert (await org_b.get(f"/api/v1/crm/contacts/{victim['id']}")).json()["tags"] == []


async def test_another_orgs_transcript_cannot_be_read(org_a, org_b):
    victim = await _make_contact(org_b, "+15550000007", "Beta Secret")

    response = await org_a.get(f"/api/v1/crm/contacts/{victim['id']}/messages")

    assert response.status_code == 404


async def test_summary_counts_only_the_callers_own_contacts(org_a, org_b):
    await _make_contact(org_a, "+15550000008", "A1")
    await _make_contact(org_a, "+15550000009", "A2")
    await _make_contact(org_b, "+15550000010", "B1")

    assert (await org_a.get("/api/v1/crm/contacts/summary")).json()["total"] == 2
    assert (await org_b.get("/api/v1/crm/contacts/summary")).json()["total"] == 1


async def test_the_same_phone_number_can_exist_in_two_organizations(org_a, org_b):
    """A shopper may deal with two businesses on the platform."""
    shared = "+15551112222"
    a_contact = await _make_contact(org_a, shared, "Known to Alpha")
    b_contact = await _make_contact(org_b, shared, "Known to Beta")

    assert a_contact["id"] != b_contact["id"]
    assert a_contact["organization_id"] != b_contact["organization_id"]


# --------------------------- Knowledge / vectors ---------------------------
async def test_knowledge_list_is_scoped(org_a, org_b, offline_embeddings):
    await _make_document(org_a, "Alpha returns", "Alpha accepts returns for 30 days.")
    await _make_document(org_b, "Beta returns", "Beta accepts returns for 7 days.")

    a_titles = {d["title"] for d in (await org_a.get("/api/v1/knowledge/documents")).json()}
    b_titles = {d["title"] for d in (await org_b.get("/api/v1/knowledge/documents")).json()}

    assert a_titles == {"Alpha returns"}
    assert b_titles == {"Beta returns"}


async def test_reading_another_orgs_document_is_not_found(org_a, org_b, offline_embeddings):
    secret = await _make_document(org_b, "Beta pricing", "Beta charges 999 per unit.")

    assert (await org_a.get(f"/api/v1/knowledge/documents/{secret['id']}")).status_code == 404


async def test_deleting_another_orgs_document_is_not_found(org_a, org_b, offline_embeddings):
    secret = await _make_document(org_b, "Beta pricing", "Beta charges 999 per unit.")

    assert (
        await org_a.delete(f"/api/v1/knowledge/documents/{secret['id']}")
    ).status_code == 404
    assert (await org_b.get(f"/api/v1/knowledge/documents/{secret['id']}")).status_code == 200


async def test_search_never_returns_another_organizations_document(
    org_a, org_b, offline_embeddings
):
    """The strongest possible match still must not cross the boundary."""
    await _make_document(
        org_b, "Beta warranty policy", "Beta offers a lifetime warranty on gearboxes."
    )
    await _make_document(org_a, "Alpha warranty policy", "Alpha offers a 2 year warranty.")

    hits = (await org_a.get("/api/v1/knowledge/search?q=lifetime warranty gearboxes")).json()

    assert hits, "Alpha should still match its own document"
    assert all("Beta" not in hit["title"] for hit in hits)
    assert all("gearboxes" not in hit["content"] for hit in hits)


async def test_search_finds_nothing_when_the_org_has_no_documents(
    org_a, org_b, offline_embeddings
):
    await _make_document(org_b, "Beta only", "Beta has all the knowledge.")

    assert (await org_a.get("/api/v1/knowledge/search?q=knowledge")).json() == []


# ------------------------------ Organizations ------------------------------
async def test_org_list_shows_only_memberships(org_a, org_b):
    a_names = {m["organization"]["name"] for m in (await org_a.get("/api/v1/organizations")).json()}

    assert a_names == {"Alpha Shoes"}
    assert "Beta Motors" not in a_names


async def test_cannot_read_an_organization_you_do_not_belong_to(org_a, org_b):
    response = await org_a.get(f"/api/v1/organizations/{org_b.organization_id}")

    assert response.status_code == 404


async def test_cannot_update_an_organization_you_do_not_belong_to(org_a, org_b):
    response = await org_a.patch(
        f"/api/v1/organizations/{org_b.organization_id}", json={"name": "Hijacked"}
    )

    assert response.status_code == 404
    assert (await org_b.get("/api/v1/organizations/active")).json()["name"] == "Beta Motors"


async def test_cannot_switch_into_an_organization_you_do_not_belong_to(org_a, org_b):
    response = await org_a.post(
        "/api/v1/organizations/switch", json={"organization_id": org_b.organization_id}
    )

    assert response.status_code == 404
    # Still pinned to their own organization.
    assert (await org_a.get("/api/v1/organizations/active")).json()["name"] == "Alpha Shoes"


async def test_members_list_is_scoped(org_a, org_b):
    a_members = (await org_a.get("/api/v1/organizations/active/members")).json()

    assert len(a_members) == 1
    assert a_members[0]["organization_id"] == org_a.organization_id


async def test_channels_are_scoped(org_a, org_b):
    await org_a.post(
        "/api/v1/organizations/active/channels", json={"phone_number": "+15557770001"}
    )
    await org_b.post(
        "/api/v1/organizations/active/channels", json={"phone_number": "+15557770002"}
    )

    a_numbers = {
        c["phone_number"]
        for c in (await org_a.get("/api/v1/organizations/active/channels")).json()
    }

    assert a_numbers == {"+15557770001"}


async def test_a_number_cannot_be_claimed_by_two_organizations(org_a, org_b):
    """Inbound routing depends on a number identifying exactly one tenant."""
    first = await org_a.post(
        "/api/v1/organizations/active/channels", json={"phone_number": "+15558880000"}
    )
    second = await org_b.post(
        "/api/v1/organizations/active/channels", json={"phone_number": "+15558880000"}
    )

    assert first.status_code == 201
    assert second.status_code == 409


# -------------------------- Dashboard endpoints ----------------------------
async def test_dashboard_stats_are_per_organization(org_a, org_b):
    await _make_contact(org_a, "+15559990001", "A1")

    a_stats = (await org_a.get("/api/v1/stats")).json()
    b_stats = (await org_b.get("/api/v1/stats")).json()

    assert a_stats["organization"] == "Alpha Shoes"
    assert b_stats["organization"] == "Beta Motors"
    assert a_stats["pipeline"].get("LEAD", 0) == 1
    assert b_stats["pipeline"].get("LEAD", 0) == 0


async def test_dashboard_contacts_are_scoped(org_a, org_b):
    await _make_contact(org_a, "+15559990002", "Alpha Only")
    await _make_contact(org_b, "+15559990003", "Beta Only")

    names = {c["name"] for c in (await org_a.get("/api/v1/contacts")).json()}

    assert names == {"Alpha Only"}


# ------------------------------ Authentication -----------------------------
async def test_crm_requires_authentication(client):
    assert (await client.get("/api/v1/crm/contacts")).status_code == 401


async def test_a_forged_token_is_rejected(client):
    response = await client.get(
        "/api/v1/crm/contacts", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401


async def test_revoking_membership_immediately_cuts_access(org_a, org_b, client):
    """A stale active_organization_id must not keep a removed member reading."""
    from sqlalchemy import delete

    from app.models import OrganizationMember

    await _make_contact(org_a, "+15551230000", "Alpha Lead")
    assert (await org_a.get("/api/v1/crm/contacts")).status_code == 200

    # Remove the membership directly, leaving active_organization_id pointing at it.
    session = client._transport.app.dependency_overrides  # noqa: SLF001 - fixture wiring
    from app.database import get_db

    generator = session[get_db]()
    db = await generator.__anext__()
    await db.execute(
        delete(OrganizationMember).where(
            OrganizationMember.organization_id == org_a.organization_id
        )
    )
    await db.flush()

    assert (await org_a.get("/api/v1/crm/contacts")).status_code == 403
