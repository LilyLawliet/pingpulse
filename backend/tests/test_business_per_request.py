"""Each request names the business it is for, so tabs never cross."""

import uuid


async def test_each_request_can_name_its_business(org_a, org_b):
    """Two tabs on two businesses each read their own, whatever was switched last."""
    from app.models import Organization, OrganizationMember, User

    from .conftest import _session_for

    session = _session_for(org_a._client)
    user = await session.get(User, uuid.UUID(str(org_a.user_id)))
    second = Organization(name="Second shop", sales_prompt="Second.")
    session.add(second)
    await session.flush()
    session.add(OrganizationMember(organization_id=second.id, user_id=user.id, role="OWNER"))
    await session.commit()

    as_first = (await org_a.get("/api/v1/organizations/active")).json()
    as_second = (
        await org_a._client.get(
            "/api/v1/organizations/active", headers={**org_a.headers, "X-Organization-Id": str(second.id)}
        )
    ).json()
    assert as_first["id"] == org_a.organization_id
    assert as_second["name"] == "Second shop"

    theirs = await org_a._client.get(
        "/api/v1/organizations/active", headers={**org_a.headers, "X-Organization-Id": org_b.organization_id}
    )
    assert theirs.status_code == 403, "a business the account is not in was read"
