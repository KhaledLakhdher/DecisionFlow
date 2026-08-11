"""Rate limiting on the authentication endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from redis.exceptions import RedisError

from decisionflow.core import ratelimit
from tests.conftest import PASSWORD, register_account


@pytest.fixture(autouse=True)
async def _require_redis() -> None:
    """Skip rather than fail when Redis is absent.

    `enforce` deliberately fails open, so without this the tests below would
    report a passing rate limiter that is not actually running.
    """
    try:
        await ratelimit.get_redis().ping()
    except (RedisError, OSError):
        pytest.skip("Redis is not reachable; rate limiting is disabled")


async def test_repeated_failed_logins_are_throttled(
    client: AsyncClient, unique_email
) -> None:
    email = unique_email()
    await register_account(client, email)

    statuses = [
        (
            await client.post(
                "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
            )
        ).status_code
        for _ in range(ratelimit.LOGIN_PER_ACCOUNT.limit + 2)
    ]

    assert 429 in statuses, f"expected throttling, got {statuses}"
    assert statuses[0] == 401, "the first attempt should be a normal rejection"


async def test_throttle_response_uses_the_standard_error_envelope(
    client: AsyncClient, unique_email
) -> None:
    email = unique_email()
    await register_account(client, email)

    response = None
    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.limit + 2):
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )
        if response.status_code == 429:
            break

    assert response is not None and response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


async def test_successful_login_clears_the_account_counter(
    client: AsyncClient, unique_email
) -> None:
    """A user who mistypes, then succeeds, must not be locked out afterwards."""
    email = unique_email()
    await register_account(client, email)

    for _ in range(ratelimit.LOGIN_PER_ACCOUNT.limit - 1):
        await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )

    good = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert good.status_code == 200

    # The counter was reset, so this must be judged on its merits, not throttled.
    again = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert again.status_code == 200
