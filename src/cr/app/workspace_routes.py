"""Dashboard routes for workspaces: provider connections, custom tiers, repo routing.

Every route is scoped to one account and checks that the caller may manage
it. Keys travel one way: in with a test or a save, never back out — responses
carry the last four characters only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func

from cr import vault
from cr.app import workspace as ws
from cr.config import settings
from cr.store.models import Run

log = logging.getLogger(__name__)


def actor(request: Request) -> ws.Actor:
    """The caller. With sign-in unconfigured (`cr serve` on a laptop) the
    owner of the machine is the operator, so they may manage every account."""
    from cr.app import accounts
    from cr.app.authroutes import current_user

    if not accounts.configured(settings):
        return ws.Actor(login="local", is_admin=True)
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in to manage workspaces")
    return ws.Actor(
        login=user.login,
        is_admin=user.is_admin,
        orgs=list(user.orgs or []),
        admin_orgs=list(getattr(user, "admin_orgs", None) or []),
    )


def _scoped(account: str, who: ws.Actor) -> str:
    """Changes need the owner or an org admin."""
    try:
        ws.require_manage(who, account)
    except ws.Forbidden as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return account


def _visible(account: str, who: ws.Actor) -> str:
    """Reads need membership."""
    try:
        ws.require_view(who, account)
    except ws.Forbidden as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return account


_ACCOUNT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


@dataclass(frozen=True)
class Scope:
    """Which workspace a dashboard read covers. `account=None` is every
    workspace, which only CR administrators may ask for."""

    account: str | None
    actor: ws.Actor

    def runs(self) -> list[Any]:
        return [] if self.account is None else [func.lower(Run.account) == self.account.lower()]

    def repo(self, column: Any) -> list[Any]:
        """For tables keyed by `owner/name` rather than by account."""
        if self.account is None:
            return []
        return [func.lower(column).like(self.account.lower() + "/%")]

    def allows(self, account: str) -> bool:
        return self.account is None or self.account.lower() == (account or "").lower()


def workspace_scope(request: Request, account: str | None = None) -> Scope:
    """The one rule behind every dashboard read: you see a workspace only if
    you belong to it. Enforced here, so a hand-edited URL changes nothing."""
    who = actor(request)
    if not account or account == "*":
        if who.is_admin:
            return Scope(None, who)
        raise HTTPException(
            status_code=403 if account == "*" else 400,
            detail="choose one of your workspaces",
        )
    if not _ACCOUNT.fullmatch(account):
        raise HTTPException(status_code=400, detail="not a GitHub account name")
    return Scope(_visible(account, who), who)


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, ws.RateLimited):
        return HTTPException(status_code=429, detail=str(e))
    if isinstance(e, vault.VaultUnavailable):
        log.error("vault unavailable: %s", e)
        return HTTPException(
            status_code=503,
            detail="Saving API keys is not set up on this server yet. Ask an administrator "
            "to configure CR_SECRETS_KEY.",
        )
    if isinstance(e, vault.SecretUnreadable):
        return HTTPException(
            status_code=409, detail="This key can no longer be read. Remove it and add it again."
        )
    return HTTPException(status_code=400, detail=str(e))


class SaveConnectionIn(ws.ConnectionDraft):
    receipt: str = ""


class GuidelinesIn(BaseModel):
    repo: str
    text: str = Field(default="", max_length=20_000)


class SampleIn(BaseModel):
    model: str


class RoutingIn(BaseModel):
    # Tier forced onto every repository of the account; null: CR's managed review.
    all: int | None = None
    # Tier per repository, overriding `all`.
    repos: dict[str, int] = Field(default_factory=dict)


router = APIRouter(prefix="/api/workspaces")


@router.get("")
def list_workspaces(who: ws.Actor = Depends(actor)) -> dict[str, Any]:
    slug = settings.github_app_slug
    return {
        "login": who.login,
        "is_admin": who.is_admin,
        "workspaces": ws.workspaces_for(who),
        "install_url": f"https://github.com/apps/{slug}/installations/new" if slug else "",
    }


@router.get("/{account}")
def workspace(account: str, who: ws.Actor = Depends(actor)) -> dict[str, Any]:
    acct = _visible(account, who)
    return {**ws.overview(settings, acct), "can_manage": ws.can_manage(who, acct)}


@router.post("/{account}/connections/test")
async def test_new_connection(
    account: str, body: ws.ConnectionDraft, who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return await ws.test_connection(settings, acct, body)
    except (ws.WorkspaceError, vault.VaultUnavailable, vault.SecretUnreadable) as e:
        raise _fail(e) from e


@router.post("/{account}/connections/{conn_id}/test")
async def test_saved_connection(
    account: str, conn_id: int, body: ws.ConnectionDraft, who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return await ws.test_connection(settings, acct, body, conn_id=conn_id)
    except (ws.WorkspaceError, vault.VaultUnavailable, vault.SecretUnreadable) as e:
        raise _fail(e) from e


@router.post("/{account}/connections")
def create_connection(
    account: str, body: SaveConnectionIn, who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return ws.save_connection(settings, acct, body, body.receipt, by=who.login)
    except (ws.WorkspaceError, vault.VaultUnavailable, vault.SecretUnreadable) as e:
        raise _fail(e) from e


@router.put("/{account}/connections/{conn_id}")
def update_connection(
    account: str, conn_id: int, body: SaveConnectionIn, who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return ws.save_connection(settings, acct, body, body.receipt, conn_id=conn_id, by=who.login)
    except (ws.WorkspaceError, vault.VaultUnavailable, vault.SecretUnreadable) as e:
        raise _fail(e) from e


@router.delete("/{account}/connections/{conn_id}")
def delete_connection(account: str, conn_id: int, who: ws.Actor = Depends(actor)) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        ws.delete_connection(acct, conn_id)
    except ws.WorkspaceError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"deleted": conn_id}


def _spec(body: dict[str, Any]) -> ws.TierSpec:
    try:
        return ws.TierSpec.model_validate(body)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise HTTPException(
            status_code=400, detail=f"{where}: {first.get('msg')}" if where else first.get("msg")
        ) from e


@router.post("/{account}/tiers")
def create_tier(
    account: str, body: dict[str, Any], who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return ws.save_tier(acct, _spec(body), by=who.login)
    except ws.WorkspaceError as e:
        raise _fail(e) from e


@router.put("/{account}/tiers/{tier_id}")
def update_tier(
    account: str, tier_id: int, body: dict[str, Any], who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return ws.save_tier(acct, _spec(body), tier_id=tier_id, by=who.login)
    except ws.WorkspaceError as e:
        raise _fail(e) from e


@router.delete("/{account}/tiers/{tier_id}")
def delete_tier(account: str, tier_id: int, who: ws.Actor = Depends(actor)) -> dict[str, Any]:
    from cr.store import db as store

    acct = _scoped(account, who)
    if not store.delete_custom_tier(acct, tier_id):
        raise HTTPException(status_code=404, detail="that tier does not exist")
    return {"deleted": tier_id}


@router.put("/{account}/routing")
def put_routing(account: str, body: RoutingIn, who: ws.Actor = Depends(actor)) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return {"routing": ws.save_routing(acct, body.all, body.repos, by=who.login)}
    except ws.WorkspaceError as e:
        raise _fail(e) from e


@router.put("/{account}/guidelines")
def put_guidelines(
    account: str, body: GuidelinesIn, who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        text = ws.save_guidelines(acct, body.repo, body.text, by=who.login)
    except ws.WorkspaceError as e:
        raise _fail(e) from e
    return {"repo": body.repo.strip(), "text": text}


@router.post("/{account}/connections/{conn_id}/sample-review")
async def sample_review(
    account: str, conn_id: int, body: SampleIn, who: ws.Actor = Depends(actor)
) -> dict[str, Any]:
    acct = _scoped(account, who)
    try:
        return await ws.sample_review(settings, acct, conn_id, body.model)
    except (ws.WorkspaceError, vault.VaultUnavailable, vault.SecretUnreadable) as e:
        raise _fail(e) from e
