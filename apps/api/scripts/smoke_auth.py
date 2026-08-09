"""End-to-end smoke test for the auth and tenancy layer.

Runs against a live server rather than a test client, so it exercises the real
middleware, exception handlers, and database. Safe to re-run: every run uses a
fresh randomised email.

    python scripts/smoke_auth.py [--base-url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import secrets
import sys
from typing import Any

import httpx

PASSWORD = "correct-horse-battery-staple"  # noqa: S105 - throwaway test fixture

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}{f' — {detail}' if detail else ''}")


def section(title: str) -> None:
    print(f"\n{title}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    api = f"{args.base_url}/api/v1"
    suffix = secrets.token_hex(4)
    owner_email = f"owner-{suffix}@example.com"
    invitee_email = f"invitee-{suffix}@example.com"

    with httpx.Client(timeout=30.0) as client:
        # -- health ------------------------------------------------------
        section("Health")
        r = client.get(f"{api}/health")
        check("liveness returns 200", r.status_code == 200, r.text)

        r = client.get(f"{api}/health/ready")
        ready: dict[str, Any] = r.json()
        check("postgres reachable", ready.get("checks", {}).get("postgres") == "ok", r.text)

        # -- registration ------------------------------------------------
        section("Registration")
        r = client.post(
            f"{api}/auth/register",
            json={
                "email": owner_email,
                "password": PASSWORD,
                "full_name": "Ada Lovelace",
                "organization_name": "Analytical Engines Ltd",
            },
        )
        check("register returns 201", r.status_code == 201, r.text)
        tokens = r.json()
        access = tokens["access_token"]
        refresh = tokens["refresh_token"]
        org_id = tokens["active_org_id"]
        check("registered as owner", tokens["active_role"] == "owner", r.text)
        check("workspace assigned", bool(org_id), r.text)

        auth = {"Authorization": f"Bearer {access}"}

        # -- duplicate registration is rejected ---------------------------
        r = client.post(
            f"{api}/auth/register",
            json={
                "email": owner_email,
                "password": PASSWORD,
                "full_name": "Impostor",
                "organization_name": "Other Co",
            },
        )
        check("duplicate email rejected with 409", r.status_code == 409, r.text)

        # -- identity ----------------------------------------------------
        section("Identity")
        r = client.get(f"{api}/auth/me", headers=auth)
        check("me returns 200", r.status_code == 200, r.text)
        me = r.json()
        check("me reports correct email", me["user"]["email"] == owner_email, r.text)
        check("me lists one membership", len(me["memberships"]) == 1, r.text)

        r = client.get(f"{api}/auth/me")
        check("me without token returns 401", r.status_code == 401, r.text)

        r = client.get(f"{api}/auth/me", headers={"Authorization": "Bearer garbage"})
        check("me with bad token returns 401", r.status_code == 401, r.text)

        # -- login -------------------------------------------------------
        section("Login")
        r = client.post(f"{api}/auth/login", json={"email": owner_email, "password": PASSWORD})
        check("login succeeds", r.status_code == 200, r.text)

        r = client.post(f"{api}/auth/login", json={"email": owner_email, "password": "wrong-password"})
        check("wrong password returns 401", r.status_code == 401, r.text)

        r = client.post(
            f"{api}/auth/login",
            json={"email": f"ghost-{suffix}@example.com", "password": PASSWORD},
        )
        check("unknown user returns 401 (no enumeration)", r.status_code == 401, r.text)

        # -- workspaces ---------------------------------------------------
        section("Workspaces")
        r = client.post(f"{api}/orgs", json={"name": "Second Workspace"}, headers=auth)
        check("create second workspace returns 201", r.status_code == 201, r.text)
        second_org_id = r.json()["id"]

        r = client.get(f"{api}/orgs", headers=auth)
        check("lists both workspaces", len(r.json()) == 2, r.text)

        r = client.post(f"{api}/auth/switch-org", json={"org_id": second_org_id}, headers=auth)
        check("switch workspace succeeds", r.status_code == 200, r.text)
        check("switched token targets new workspace", r.json()["active_org_id"] == second_org_id)

        r = client.post(
            f"{api}/auth/switch-org",
            json={"org_id": "00000000-0000-0000-0000-000000000000"},
            headers=auth,
        )
        check("switching to a foreign workspace returns 404", r.status_code == 404, r.text)

        # -- invitations --------------------------------------------------
        section("Invitations")
        r = client.post(
            f"{api}/orgs/current/invitations",
            json={"email": invitee_email, "role": "analyst"},
            headers=auth,
        )
        check("owner can invite", r.status_code == 201, r.text)
        invite_token = r.json()["invite_token"]

        r = client.post(
            f"{api}/orgs/current/invitations",
            json={"email": invitee_email, "role": "owner"},
            headers=auth,
        )
        check("inviting directly as owner is rejected", r.status_code == 422, r.text)

        r = client.post(
            f"{api}/auth/accept-invite",
            json={"token": invite_token, "password": PASSWORD, "full_name": "Grace Hopper"},
        )
        check("invitee can accept", r.status_code == 200, r.text)
        invitee_tokens = r.json()
        check("invitee joins as analyst", invitee_tokens["active_role"] == "analyst", r.text)

        r = client.post(
            f"{api}/auth/accept-invite",
            json={"token": invite_token, "password": PASSWORD},
        )
        check("invite cannot be reused", r.status_code == 409, r.text)

        r = client.get(f"{api}/orgs/current/members", headers=auth)
        check("workspace now has two members", len(r.json()) == 2, r.text)

        # -- authorization -------------------------------------------------
        section("Authorization")
        invitee_auth = {"Authorization": f"Bearer {invitee_tokens['access_token']}"}
        r = client.post(
            f"{api}/orgs/current/invitations",
            json={"email": f"nope-{suffix}@example.com", "role": "viewer"},
            headers=invitee_auth,
        )
        check("analyst cannot invite (403)", r.status_code == 403, r.text)

        # -- refresh rotation ------------------------------------------------
        section("Refresh rotation")
        r = client.post(f"{api}/auth/refresh", json={"refresh_token": refresh})
        check("refresh succeeds", r.status_code == 200, r.text)
        rotated = r.json()["refresh_token"]
        check("a new refresh token is issued", rotated != refresh)

        r = client.post(f"{api}/auth/refresh", json={"refresh_token": refresh})
        check("replaying the old refresh token fails", r.status_code == 401, r.text)

        # Replay detection revokes the whole family, so the rotated token must
        # now be dead too.
        r = client.post(f"{api}/auth/refresh", json={"refresh_token": rotated})
        check("replay revokes the entire token family", r.status_code == 401, r.text)

        # -- logout ------------------------------------------------------------
        section("Logout")
        r = client.post(f"{api}/auth/login", json={"email": owner_email, "password": PASSWORD})
        fresh_refresh = r.json()["refresh_token"]

        r = client.post(f"{api}/auth/logout", json={"refresh_token": fresh_refresh})
        check("logout returns 204", r.status_code == 204, r.text)

        r = client.post(f"{api}/auth/refresh", json={"refresh_token": fresh_refresh})
        check("refresh after logout fails", r.status_code == 401, r.text)

        r = client.post(f"{api}/auth/logout", json={"refresh_token": fresh_refresh})
        check("logout is idempotent", r.status_code == 204, r.text)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
