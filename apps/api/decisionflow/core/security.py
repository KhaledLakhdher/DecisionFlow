"""Password hashing and JWT issuance/verification.

Argon2id for passwords (memory-hard, the current OWASP recommendation) and
short-lived HS256 access tokens paired with longer-lived refresh tokens.

Refresh tokens carry a `jti` so a specific session can be revoked without
invalidating every token the user holds; the server-side record lives in the
`refresh_tokens` table.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from decisionflow.core.config import settings
from decisionflow.core.errors import AuthenticationError

TokenType = Literal["access", "refresh"]

# Defaults follow the argon2-cffi maintainers' recommended profile.
_hasher = PasswordHasher()


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against its hash. Never raises on a bad password."""
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def password_needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with parameters weaker than the current profile."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_access_token(
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID | None = None,
    role: str | None = None,
) -> str:
    """Mint a short-lived access token.

    `org_id` and `role` are embedded so the common path — authorize a request
    against the active workspace — needs no membership lookup. They are only a
    cache: anything that mutates a membership revokes the user's refresh
    tokens, so a stale role cannot outlive the access token TTL.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": secrets.token_urlsafe(16),
    }
    if org_id is not None:
        payload["org"] = str(org_id)
    if role is not None:
        payload["role"] = role
    return _encode(payload)


def create_refresh_token(*, user_id: uuid.UUID) -> tuple[str, str, datetime]:
    """Mint a refresh token.

    Returns `(token, jti, expires_at)`. The caller persists `jti` and
    `expires_at`; the raw token is never stored.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.refresh_token_ttl_days)
    jti = secrets.token_urlsafe(32)
    token = _encode(
        {
            "sub": str(user_id),
            "type": "refresh",
            "iat": now,
            "exp": expires_at,
            "jti": jti,
        }
    )
    return token, jti, expires_at


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a token, or raise AuthenticationError.

    Signature, expiry and token type are all checked. Confusing an access
    token for a refresh token is a privilege escalation, so the type check is
    not optional.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Token is invalid.") from exc

    if payload.get("type") != expected_type:
        raise AuthenticationError(f"Expected a {expected_type} token.")

    return payload


def generate_invite_token() -> str:
    """URL-safe, single-use token emailed to an invitee."""
    return secrets.token_urlsafe(32)
