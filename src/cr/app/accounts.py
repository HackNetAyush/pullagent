"""Sign-in, sessions, and the account allowlist.

Publishing the App is what makes this necessary. A private App can only be
installed by its owner, so the webhook receiving an event was itself the proof
that the event was wanted. A public App has no such proof: anyone can install
it, webhooks arrive with no human in the loop, and every one of them spends our
model budget. So the ingress asks one question before anything costs money —
*is this account approved?* — and the answer lives in the `accounts` table.

Two identities are deliberately kept apart:

**The account** (`accounts`) is what authorises a review. It is a GitHub
org or user login, approved by an admin, and it is what the webhook checks.

**The user** (`users`, `sessions`) is a human who signed in with GitHub. Signing
in lets someone request access for their account and read their own runs. It
authorises nothing on its own — a signed-in stranger still gets no reviews.

Keeping them separate is what stops "I signed in" from being mistaken for "my
org is approved", which is the failure mode that would quietly reopen the door
this module exists to close.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

from cr.config import Settings
from cr.store import db as store

log = logging.getLogger(__name__)

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"

SESSION_COOKIE = "cr_session"
# The OAuth `state` parameter, round-tripped through a cookie so the callback
# can prove the redirect it received is the one this browser started.
STATE_COOKIE = "cr_oauth_state"
STATE_TTL_S = 600


class OAuthError(RuntimeError):
    """Sign-in failed in a way the user needs to be told about."""


@dataclass(frozen=True)
class Identity:
    """Who GitHub says this is."""

    id: int
    login: str
    name: str = ""
    avatar_url: str = ""
    email: str = ""


def configured(s: Settings) -> bool:
    return bool(s.github_client_id and s.github_client_secret)


def admin_logins(s: Settings) -> set[str]:
    """Logins seeded as admins, lowercased.

    Seeding by login rather than user id is a deliberate trade: ids are stable
    and logins can be renamed, but nobody knows their own numeric id, and an
    operator who cannot bootstrap the first admin has a service that approves
    nothing. The window is narrow because the value is only read at sign-in.
    """
    return {p.strip().lower() for p in (s.admin_logins or "").split(",") if p.strip()}


def authorize_url(s: Settings, state: str, redirect_uri: str) -> str:
    """Where to send the browser to start sign-in.

    No scopes are requested. We want identity and nothing else — the App's own
    installation token already grants every repository permission the reviewer
    needs, so asking a human for repo access would be taking more than we use.
    """
    from urllib.parse import urlencode

    return f"{GITHUB_AUTHORIZE}?" + urlencode(
        {
            "client_id": s.github_client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "allow_signup": "false",
        }
    )


def new_state() -> str:
    return secrets.token_urlsafe(32)


async def exchange(s: Settings, code: str, redirect_uri: str) -> str:
    """Trade the callback code for a user access token."""
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": s.github_client_id,
                "client_secret": s.github_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if r.status_code != 200:
        raise OAuthError(f"GitHub rejected the code exchange ({r.status_code})")
    payload = r.json()
    if token := payload.get("access_token"):
        return str(token)
    # GitHub returns 200 with an error body for a stale or replayed code.
    raise OAuthError(payload.get("error_description") or "no access token returned")


async def identify(token: str) -> Identity:
    """Who the token belongs to."""
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=20.0) as c:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        r = await c.get("/user", headers=headers)
        if r.status_code != 200:
            raise OAuthError(f"could not read the signed-in user ({r.status_code})")
        u = r.json()
    return Identity(
        id=int(u["id"]),
        login=u.get("login") or "",
        name=u.get("name") or "",
        avatar_url=u.get("avatar_url") or "",
        email=u.get("email") or "",
    )


async def memberships(token: str, login: str) -> list[dict[str, Any]]:
    """Accounts this user can plausibly speak for: themselves, plus their orgs.

    Used to stop someone requesting access for an org they have nothing to do
    with. It is not a security boundary — an admin still approves every request
    by hand — it just keeps the queue honest.
    """
    out: list[dict[str, Any]] = [{"login": login, "type": "User", "role": "admin"}]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        async with httpx.AsyncClient(base_url=GITHUB_API, timeout=20.0) as c:
            # Memberships carry the role, which decides who may change an
            # org's keys and routing. It needs the App's "Members: read"
            # permission; without it, fall back to plain membership, where
            # everyone is a member - able to view, never to manage.
            r = await c.get(
                "/user/memberships/orgs",
                headers=headers,
                params={"state": "active", "per_page": 100},
            )
            if r.status_code == 200:
                out += [
                    {
                        "login": m["organization"]["login"],
                        "type": "Organization",
                        "role": "admin" if m.get("role") == "admin" else "member",
                    }
                    for m in r.json()
                    if m.get("organization", {}).get("login")
                ]
                return out
            log.info("org roles unavailable for %s (HTTP %s)", login, r.status_code)
            r = await c.get("/user/orgs", headers=headers, params={"per_page": 100})
            if r.status_code == 200:
                out += [
                    {"login": o["login"], "type": "Organization", "role": "member"}
                    for o in r.json()
                ]
    except Exception as e:  # noqa: BLE001 - a missing org list must not block sign-in
        log.warning("could not read orgs for %s: %s", login, e)
    return out


def sign_in(
    identity: Identity,
    s: Settings,
    orgs: list[str] | None = None,
    admin_orgs: list[str] | None = None,
) -> str:
    """Record the user and open a session. Returns the session token.

    `orgs` replaces the stored membership list when given; it is what decides
    which organisations' API keys and tiers this user may manage.
    """
    seeded = identity.login.lower() in admin_logins(s)
    # Never demote: an admin promoted through the UI must not be reset by a
    # deployment whose CR_ADMIN_LOGINS no longer lists them.
    store.upsert_user(
        identity.id,
        login=identity.login,
        name=identity.name,
        avatar_url=identity.avatar_url,
        email=identity.email,
        is_admin=True if seeded else None,
        orgs=orgs,
        admin_orgs=admin_orgs,
    )
    token = secrets.token_urlsafe(32)
    store.create_session(token, identity.id, s.session_ttl_s)
    log.info("signed in %s (admin=%s)", identity.login, seeded)
    return token


def allowed(login: str) -> bool:
    """Is this account approved to have its pull requests reviewed?

    Everything that is not an explicit approval is a refusal, including the
    store being unreachable. That is the whole point of the gate.
    """
    return store.account_status(login) == "approved"


def account_for(payload: dict[str, Any]) -> tuple[str, str]:
    """(login, type) of the account an event belongs to.

    Webhook payloads carry `installation` as little more than an id, so the
    owning account comes from the repository for repo-scoped events, and from
    `installation.account` for install/uninstall events which have no
    repository at all.
    """
    repo = payload.get("repository") or {}
    owner = repo.get("owner") or {}
    if login := owner.get("login"):
        return str(login), str(owner.get("type") or "")
    # Some payload shapes (and every hand-written one) carry `full_name` without
    # an owner object. Deriving the login from it keeps the gate closed on the
    # right account rather than falling through to an empty string, which would
    # lump every such event under one nameless entry.
    if "/" in str(repo.get("full_name") or ""):
        return str(repo["full_name"]).split("/", 1)[0], ""
    acct = ((payload.get("installation") or {}).get("account")) or {}
    return str(acct.get("login") or ""), str(acct.get("type") or "")
