"""Redis-backed fixed-window rate limiting.

Argon2 makes password guessing slow, not impossible — an unthrottled login
endpoint is still a credential-stuffing target. This puts a ceiling on attempts.

Two independent limits apply to login, and both matter:

  * per source address — stops one host grinding through a password list;
  * per account — stops a distributed attempt against a single high-value
    account, which the address limit alone would never see.

Fixed windows rather than a sliding log: one INCR plus one EXPIRE, no per-hit
storage, and the burst it permits at a window boundary (up to 2x the limit) is
irrelevant at these thresholds.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from decisionflow.core.config import settings
from decisionflow.core.errors import RateLimitedError
from decisionflow.core.logging import get_logger

log = get_logger(__name__)

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Lazily create the shared client.

    `decode_responses=True` so counters come back as str rather than bytes.
    """
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_dsn,
            encoding="utf-8",
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@dataclass(frozen=True, slots=True)
class RateLimit:
    limit: int
    window_seconds: int

    def describe(self) -> str:
        minutes = self.window_seconds // 60
        unit = f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes else (
            f"{self.window_seconds} seconds"
        )
        return f"{self.limit} attempts per {unit}"


# Tuned to be invisible to a human who mistypes a password and painful for a
# script. Registration is capped per address to blunt automated signup abuse.
LOGIN_PER_IP = RateLimit(limit=10, window_seconds=300)
LOGIN_PER_ACCOUNT = RateLimit(limit=5, window_seconds=300)
REGISTER_PER_IP = RateLimit(limit=5, window_seconds=3600)
INVITE_ACCEPT_PER_IP = RateLimit(limit=10, window_seconds=3600)


async def enforce(bucket: str, identifier: str, rule: RateLimit) -> None:
    """Count one attempt against `bucket:identifier`, raising if over the limit.

    Fails *open* when Redis is unreachable: a rate limiter outage should not
    become an authentication outage. That is a deliberate availability trade —
    it does mean losing throttling exactly when infrastructure is unhealthy, so
    the failure is logged at warning level rather than swallowed.
    """
    if not settings.rate_limit_enabled:
        return

    key = f"ratelimit:{bucket}:{identifier}"
    try:
        client = get_redis()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, rule.window_seconds, nx=True)  # only on first hit
            count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        log.warning("ratelimit.unavailable", bucket=bucket, error=str(exc))
        return

    if int(count) > rule.limit:
        log.info("ratelimit.exceeded", bucket=bucket)
        raise RateLimitedError(
            f"Too many attempts. This endpoint allows {rule.describe()}."
        )


async def reset(bucket: str, identifier: str) -> None:
    """Clear a counter — called after a success so a legitimate user is not
    penalised for earlier typos."""
    with contextlib.suppress(RedisError, OSError):
        await get_redis().delete(f"ratelimit:{bucket}:{identifier}")
