"""Workspace membership, roles, invitations and removal."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import PASSWORD, auth_header, register_account


async def _invite_and_accept(
    client: AsyncClient,
    owner_headers: dict[str, str],
    email: str,
    role: str = "analyst",
) -> dict[str, Any]:
    """Invite `email` into the owner's workspace and accept as a new account."""
    invite = await client.post(
        "/api/v1/orgs/current/invitations",
        json={"email": email, "role": role},
        headers=owner_headers,
    )
    assert invite.status_code == 201, invite.text

    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": invite.json()["invite_token"],
            "password": PASSWORD,
            "full_name": "Invited Person",
        },
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def _member_ids(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    response = await client.get("/api/v1/orgs/current/members", headers=headers)
    assert response.status_code == 200, response.text
    return {m["email"]: m["user_id"] for m in response.json()}


# --------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------
async def test_create_and_list_workspaces(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)

    created = await client.post("/api/v1/orgs", json={"name": "Second"}, headers=headers)
    assert created.status_code == 201

    listed = await client.get("/api/v1/orgs", headers=headers)
    assert len(listed.json()) == 2


async def test_workspaces_with_the_same_name_get_distinct_slugs(
    client: AsyncClient, unique_email
) -> None:
    """The slug collision path must resolve, not error."""
    tokens = await register_account(client, unique_email(), org_name="Acme")
    headers = auth_header(tokens)

    for _ in range(3):
        response = await client.post("/api/v1/orgs", json={"name": "Acme"}, headers=headers)
        assert response.status_code == 201, response.text

    listed = await client.get("/api/v1/orgs", headers=headers)
    slugs = [org["slug"] for org in listed.json()]
    assert len(slugs) == len(set(slugs)), f"slugs must be unique, got {slugs}"


async def test_switch_workspace(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    headers = auth_header(tokens)

    created = await client.post("/api/v1/orgs", json={"name": "Second"}, headers=headers)
    second_id = created.json()["id"]

    switched = await client.post(
        "/api/v1/auth/switch-org", json={"org_id": second_id}, headers=headers
    )
    assert switched.status_code == 200
    assert switched.json()["active_org_id"] == second_id


async def test_cannot_switch_to_a_workspace_you_do_not_belong_to(
    client: AsyncClient, unique_email
) -> None:
    tokens = await register_account(client, unique_email())
    outsider = await register_account(client, unique_email())

    response = await client.post(
        "/api/v1/auth/switch-org",
        json={"org_id": outsider["active_org_id"]},
        headers=auth_header(tokens),
    )
    # 404 rather than 403: confirming the workspace exists is itself a leak.
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------
async def test_invite_flow_adds_a_member(client: AsyncClient, unique_email) -> None:
    owner = await register_account(client, unique_email())
    headers = auth_header(owner)

    invited = await _invite_and_accept(client, headers, unique_email())
    assert invited["active_role"] == "analyst"

    members = await client.get("/api/v1/orgs/current/members", headers=headers)
    assert len(members.json()) == 2


async def test_invitation_cannot_be_reused(client: AsyncClient, unique_email) -> None:
    owner = await register_account(client, unique_email())
    email = unique_email()

    invite = await client.post(
        "/api/v1/orgs/current/invitations",
        json={"email": email, "role": "viewer"},
        headers=auth_header(owner),
    )
    token = invite.json()["invite_token"]

    first = await client.post(
        "/api/v1/auth/accept-invite", json={"token": token, "password": PASSWORD}
    )
    assert first.status_code == 200

    second = await client.post(
        "/api/v1/auth/accept-invite", json={"token": token, "password": PASSWORD}
    )
    assert second.status_code == 409


async def test_cannot_invite_directly_as_owner(client: AsyncClient, unique_email) -> None:
    owner = await register_account(client, unique_email())

    response = await client.post(
        "/api/v1/orgs/current/invitations",
        json={"email": unique_email(), "role": "owner"},
        headers=auth_header(owner),
    )
    assert response.status_code == 422


async def test_analyst_cannot_invite(client: AsyncClient, unique_email) -> None:
    owner = await register_account(client, unique_email())
    analyst = await _invite_and_accept(client, auth_header(owner), unique_email())

    response = await client.post(
        "/api/v1/orgs/current/invitations",
        json={"email": unique_email(), "role": "viewer"},
        headers=auth_header(analyst),
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
async def test_owner_can_promote_a_member(client: AsyncClient, unique_email) -> None:
    owner = await register_account(client, unique_email())
    headers = auth_header(owner)
    invitee_email = unique_email()
    await _invite_and_accept(client, headers, invitee_email)

    ids = await _member_ids(client, headers)
    response = await client.patch(
        f"/api/v1/orgs/current/members/{ids[invitee_email]}/role",
        json={"role": "admin"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_nobody_can_change_their_own_role(client: AsyncClient, unique_email) -> None:
    email = unique_email()
    owner = await register_account(client, email)
    headers = auth_header(owner)

    ids = await _member_ids(client, headers)
    response = await client.patch(
        f"/api/v1/orgs/current/members/{ids[email]}/role",
        json={"role": "viewer"},
        headers=headers,
    )
    assert response.status_code == 422


async def test_admin_cannot_grant_or_revoke_ownership(client: AsyncClient, unique_email) -> None:
    """An admin must not be able to escalate to owner, nor evict one."""
    owner_email = unique_email()
    owner = await register_account(client, owner_email)
    owner_headers = auth_header(owner)

    admin_email = unique_email()
    await _invite_and_accept(client, owner_headers, admin_email, role="admin")

    ids = await _member_ids(client, owner_headers)
    admin_login = await client.post(
        "/api/v1/auth/login", json={"email": admin_email, "password": PASSWORD}
    )
    admin_headers = auth_header(admin_login.json())

    demote_owner = await client.patch(
        f"/api/v1/orgs/current/members/{ids[owner_email]}/role",
        json={"role": "viewer"},
        headers=admin_headers,
    )
    assert demote_owner.status_code == 403

    grant_ownership = await client.patch(
        f"/api/v1/orgs/current/members/{ids[owner_email]}/role",
        json={"role": "owner"},
        headers=admin_headers,
    )
    assert grant_ownership.status_code == 403


async def test_demotion_takes_effect_immediately(client: AsyncClient, unique_email) -> None:
    """The demoted user's existing access token must stop carrying admin rights.

    This is the regression guard for trusting the token's role claim: the
    token below was minted while they were an admin and has not expired.
    """
    owner = await register_account(client, unique_email())
    owner_headers = auth_header(owner)

    admin_email = unique_email()
    admin_tokens = await _invite_and_accept(client, owner_headers, admin_email, role="admin")
    admin_headers = auth_header(admin_tokens)

    # Confirm the token currently works for an admin-only action.
    allowed = await client.post(
        "/api/v1/orgs/current/invitations",
        json={"email": unique_email(), "role": "viewer"},
        headers=admin_headers,
    )
    assert allowed.status_code == 201

    ids = await _member_ids(client, owner_headers)
    demote = await client.patch(
        f"/api/v1/orgs/current/members/{ids[admin_email]}/role",
        json={"role": "viewer"},
        headers=owner_headers,
    )
    assert demote.status_code == 200

    # Same token, now insufficient.
    denied = await client.post(
        "/api/v1/orgs/current/invitations",
        json={"email": unique_email(), "role": "viewer"},
        headers=admin_headers,
    )
    assert denied.status_code == 403


# --------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------
async def test_owner_can_remove_a_member(client: AsyncClient, unique_email) -> None:
    owner = await register_account(client, unique_email())
    headers = auth_header(owner)
    invitee_email = unique_email()
    await _invite_and_accept(client, headers, invitee_email)

    ids = await _member_ids(client, headers)
    removed = await client.delete(
        f"/api/v1/orgs/current/members/{ids[invitee_email]}", headers=headers
    )
    assert removed.status_code == 204

    remaining = await client.get("/api/v1/orgs/current/members", headers=headers)
    assert len(remaining.json()) == 1


async def test_removal_revokes_workspace_access_immediately(
    client: AsyncClient, unique_email
) -> None:
    """A removed member's unexpired token must no longer reach workspace data."""
    owner = await register_account(client, unique_email())
    owner_headers = auth_header(owner)

    member_email = unique_email()
    member_tokens = await _invite_and_accept(client, owner_headers, member_email)
    member_headers = auth_header(member_tokens)

    assert (
        await client.get("/api/v1/orgs/current/members", headers=member_headers)
    ).status_code == 200

    ids = await _member_ids(client, owner_headers)
    await client.delete(f"/api/v1/orgs/current/members/{ids[member_email]}", headers=owner_headers)

    after = await client.get("/api/v1/orgs/current/members", headers=member_headers)
    assert after.status_code == 403


async def test_analyst_cannot_remove_others(client: AsyncClient, unique_email) -> None:
    owner_email = unique_email()
    owner = await register_account(client, owner_email)
    owner_headers = auth_header(owner)

    analyst_tokens = await _invite_and_accept(client, owner_headers, unique_email())
    ids = await _member_ids(client, owner_headers)

    response = await client.delete(
        f"/api/v1/orgs/current/members/{ids[owner_email]}",
        headers=auth_header(analyst_tokens),
    )
    assert response.status_code == 403


async def test_admin_cannot_remove_an_owner(client: AsyncClient, unique_email) -> None:
    owner_email = unique_email()
    owner = await register_account(client, owner_email)
    owner_headers = auth_header(owner)

    admin_email = unique_email()
    admin_tokens = await _invite_and_accept(client, owner_headers, admin_email, role="admin")

    ids = await _member_ids(client, owner_headers)
    response = await client.delete(
        f"/api/v1/orgs/current/members/{ids[owner_email]}",
        headers=auth_header(admin_tokens),
    )
    assert response.status_code == 403


async def test_any_member_can_leave(client: AsyncClient, unique_email) -> None:
    owner = await register_account(client, unique_email())
    owner_headers = auth_header(owner)

    member_email = unique_email()
    member_tokens = await _invite_and_accept(client, owner_headers, member_email)
    ids = await _member_ids(client, owner_headers)

    response = await client.delete(
        f"/api/v1/orgs/current/members/{ids[member_email]}",
        headers=auth_header(member_tokens),
    )
    assert response.status_code == 204


async def test_the_last_owner_cannot_leave(client: AsyncClient, unique_email) -> None:
    """Otherwise the workspace is stranded with nobody able to administer it."""
    owner_email = unique_email()
    owner = await register_account(client, owner_email)
    headers = auth_header(owner)

    ids = await _member_ids(client, headers)
    response = await client.delete(
        f"/api/v1/orgs/current/members/{ids[owner_email]}", headers=headers
    )
    assert response.status_code == 422
