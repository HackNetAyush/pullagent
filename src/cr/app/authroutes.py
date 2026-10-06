"""HTTP surface for sign-in and the account allowlist.

Split out of `service.py` because the ingress there has one job — verify,
dedup, enqueue, 202 — and burying an OAuth dance in the middle of it would make
the part that has ten seconds to respond harder to read than it deserves.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from cr.app import accounts
from cr.config import Settings
from cr.store import db as store
from cr.store.models import User

log = logging.getLogger(__name__)


def _base_url(request: Request, configured: str) -> str:
    """The externally visible origin.

    Behind Container Apps ingress the app sees plain HTTP on an internal port,
    so `request.base_url` would hand GitHub an `http://` callback it refuses to
    redirect to. The configured public URL wins whenever we have one.
    """
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def current_user(request: Request) -> User | None:
    return store.session_user(request.cookies.get(accounts.SESSION_COOKIE) or "")


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")
    return user


def build_auth_router(get_settings, get_public_url) -> APIRouter:
    """Routes for sign-in and allowlist management.

    Takes callables rather than values so the router can be built before the
    service has resolved its public URL at start-up.
    """
    r = APIRouter()

    def _s() -> Settings:
        return get_settings()

    def _secure(request: Request) -> bool:
        return _base_url(request, get_public_url()).startswith("https://")

    # --- sign-in ------------------------------------------------------------

    @r.get("/auth/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        s = _s()
        if not accounts.configured(s):
            return JSONResponse(
                {
                    "error": "sign-in is not configured",
                    "detail": "set CR_GITHUB_CLIENT_ID and CR_GITHUB_CLIENT_SECRET, or "
                    "re-run the /app/setup flow which receives them from GitHub",
                },
                status_code=503,
            )
        state = accounts.new_state()
        redirect_uri = f"{_base_url(request, get_public_url())}/auth/callback"
        resp = RedirectResponse(accounts.authorize_url(s, state, redirect_uri), status_code=302)
        resp.set_cookie(
            accounts.STATE_COOKIE,
            state,
            max_age=accounts.STATE_TTL_S,
            httponly=True,
            secure=_secure(request),
            samesite="lax",
        )
        return resp

    @r.get("/auth/callback", include_in_schema=False)
    async def callback(request: Request, code: str = "", state: str = "") -> Response:
        s = _s()
        expected = request.cookies.get(accounts.STATE_COOKIE) or ""
        # Compare before touching GitHub: a mismatched state means this redirect
        # was not started by this browser, and there is nothing to exchange.
        if not state or not expected or state != expected:
            return JSONResponse({"error": "invalid sign-in state, try again"}, status_code=400)
        if not code:
            return JSONResponse({"error": "no code returned by GitHub"}, status_code=400)

        redirect_uri = f"{_base_url(request, get_public_url())}/auth/callback"
        try:
            token = await accounts.exchange(s, code, redirect_uri)
            identity = await accounts.identify(token)
        except accounts.OAuthError as e:
            log.warning("sign-in failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        # Read on every sign-in, so leaving an org revokes access to its keys
        # and tiers by the next session. `memberships` never raises.
        member_of = await accounts.memberships(token, identity.login)
        orgs = [m["login"] for m in member_of if m.get("type") == "Organization"]
        admin_orgs = [
            m["login"]
            for m in member_of
            if m.get("type") == "Organization" and m.get("role") == "admin"
        ]
        session_token = accounts.sign_in(identity, s, orgs=orgs, admin_orgs=admin_orgs)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(
            accounts.SESSION_COOKIE,
            session_token,
            max_age=s.session_ttl_s,
            httponly=True,
            secure=_secure(request),
            samesite="lax",
        )
        resp.delete_cookie(accounts.STATE_COOKIE)
        return resp

    @r.post("/auth/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        if token := request.cookies.get(accounts.SESSION_COOKIE):
            store.delete_session(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(accounts.SESSION_COOKIE)
        return resp

    @r.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        user = current_user(request)
        if user is None:
            return {"signed_in": False, "sign_in_configured": accounts.configured(_s())}
        return {
            "signed_in": True,
            "sign_in_configured": True,
            "login": user.login,
            "name": user.name,
            "avatar_url": user.avatar_url,
            "is_admin": user.is_admin,
        }

    # --- requesting access --------------------------------------------------

    @r.get("/api/me/accounts")
    async def my_accounts(user: User = Depends(require_user)) -> dict[str, Any]:
        """The caller's own account plus its current allowlist status."""
        rows = {a.login.lower(): a for a in store.accounts()}
        mine = rows.get(user.login.lower())
        return {
            "login": user.login,
            "status": mine.status if mine else "unknown",
        }

    @r.post("/api/access-requests")
    async def request_access(
        payload: dict[str, Any], user: User = Depends(require_user)
    ) -> dict[str, Any]:
        """Ask an admin to approve an account.

        A request never grants anything — it only creates a `pending` row. The
        one thing it must not do is overwrite a decision that already exists,
        or anyone denied could clear their own denial by asking again.
        """
        login = str(payload.get("login") or user.login).strip()
        if not login:
            raise HTTPException(status_code=400, detail="login is required")

        existing = store.account_status(login)
        if existing in {"approved", "denied"}:
            return {"login": login, "status": existing, "changed": False}

        status = store.record_account(
            login,
            status="pending",
            account_type=str(payload.get("account_type") or ""),
            note=str(payload.get("note") or "")[:2000],
            requested_by=user.login,
        )
        log.info("access requested for %s by %s", login, user.login)
        return {"login": login, "status": status, "changed": True}

    # --- administration -----------------------------------------------------

    @r.get("/api/admin/accounts")
    async def list_accounts(
        status: str | None = None, _: User = Depends(require_admin)
    ) -> list[dict[str, Any]]:
        return [
            {
                "login": a.login,
                "status": a.status,
                "account_type": a.account_type,
                "note": a.note,
                "requested_by": a.requested_by,
                "decided_by": a.decided_by,
                "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                "blocked_events": a.blocked_events,
                "last_blocked_at": a.last_blocked_at.isoformat() if a.last_blocked_at else None,
            }
            for a in store.accounts(status)
        ]

    @r.post("/api/admin/accounts/{login}")
    async def decide(
        login: str, payload: dict[str, Any], admin: User = Depends(require_admin)
    ) -> dict[str, Any]:
        status = str(payload.get("status") or "").lower()
        if status not in {"approved", "denied", "pending"}:
            raise HTTPException(
                status_code=400, detail="status must be approved, denied or pending"
            )
        resulting = store.record_account(
            login,
            status=status,
            note=str(payload.get("note") or "") or None,
            decided_by=admin.login,
        )
        log.info("account %s set to %s by %s", login, resulting, admin.login)
        return {"login": login, "status": resulting}

    return r
