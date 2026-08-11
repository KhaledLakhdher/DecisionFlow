"""Registration, login, sessions and token lifecycle."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import PASSWORD, auth_header, register_account


async def test_health_is_live(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_reports_postgres(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["postgres"] == "ok"


async def test_register_creates_workspace_and_owner(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    assert tokens["active_role"] == "owner"
    assert tokens["active_org_id"]
    assert tokens["token_type"] == "bearer"


async def test_register_rejects_duplicate_email(client: AsyncClient, unique_email) -> None:
    email = unique_email()
    await register_account(client, email)

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Impostor",
            "organization_name": "Other Co",
        },
    )
    assert response.status_code == 409


async def test_register_rejects_short_password(client: AsyncClient, unique_email) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email(),
            "password": "short",
            "full_name": "Test",
            "organization_name": "Test Co",
        },
    )
    assert response.status_code == 422


async def test_email_is_case_insensitive(client: AsyncClient, unique_email) -> None:
    email = unique_email()
    await register_account(client, email)

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email.upper(),
            "password": PASSWORD,
            "full_name": "Impostor",
            "organization_name": "Other Co",
        },
    )
    assert response.status_code == 409, "uppercase variant must collide with the original"


async def test_login_succeeds_with_correct_password(client: AsyncClient, unique_email) -> None:
    email = unique_email()
    await register_account(client, email)

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_login_rejects_wrong_password(client: AsyncClient, unique_email) -> None:
    email = unique_email()
    await register_account(client, email)

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "not-the-password"}
    )
    assert response.status_code == 401


async def test_login_does_not_leak_account_existence(client: AsyncClient, unique_email) -> None:
    """An unknown address and a wrong password must be indistinguishable."""
    known = unique_email()
    await register_account(client, known)

    wrong_password = await client.post(
        "/api/v1/auth/login", json={"email": known, "password": "not-the-password"}
    )
    unknown_account = await client.post(
        "/api/v1/auth/login", json={"email": unique_email(), "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_account.status_code == 401
    assert wrong_password.json() == unknown_account.json()


async def test_me_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_rejects_malformed_token(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_me_returns_identity_and_memberships(client: AsyncClient, unique_email) -> None:
    email = unique_email()
    tokens = await register_account(client, email)

    response = await client.get("/api/v1/auth/me", headers=auth_header(tokens))
    assert response.status_code == 200

    body = response.json()
    assert body["user"]["email"] == email
    assert len(body["memberships"]) == 1
    assert body["active_role"] == "owner"


async def test_refresh_rotates_the_token(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200
    assert response.json()["refresh_token"] != tokens["refresh_token"]


async def test_refresh_replay_revokes_the_token_family(client: AsyncClient, unique_email) -> None:
    """Presenting a consumed refresh token means it leaked; cut every session."""
    tokens = await register_account(client, unique_email())

    first = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    rotated = first.json()["refresh_token"]

    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401

    # The legitimately-rotated token must die alongside the replayed one.
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
    assert after.status_code == 401


async def test_access_token_is_not_accepted_as_refresh(client: AsyncClient, unique_email) -> None:
    """Token type confusion would be a privilege escalation."""
    tokens = await register_account(client, unique_email())

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401


async def test_logout_revokes_and_is_idempotent(client: AsyncClient, unique_email) -> None:
    tokens = await register_account(client, unique_email())
    refresh = tokens["refresh_token"]

    body = {"refresh_token": refresh}
    assert (await client.post("/api/v1/auth/logout", json=body)).status_code == 204
    assert (await client.post("/api/v1/auth/refresh", json=body)).status_code == 401
    assert (await client.post("/api/v1/auth/logout", json=body)).status_code == 204
