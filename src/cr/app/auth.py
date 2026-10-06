"""GitHub App authentication.

Three credentials, three lifetimes, and the whole security posture follows from
keeping them straight:

1. **The private key** is the only long-lived secret. It never leaves this
   process, is never logged, and is never written anywhere by the App itself —
   only the setup flow writes it, once, to a 0600 file.
2. **The App JWT** is derived from that key, signed RS256, and lives ten
   minutes. It can do exactly two things: list installations and mint
   installation tokens. It can never read or write a repository.
3. **The installation token** is what actually touches a repo. It lives one
   hour, and we scope it down further — to the single repository in the event
   — so a bug in the review path cannot reach a repo it was not invited to.

Tokens are cached in memory only, keyed by what they were scoped to, and
expired a minute early so a token never dies mid-request.

`Secret` wraps every credential so that an accidental log line, traceback or
`repr()` prints a redaction instead of the key. Python tracebacks print
argument values, and a private key in a crash report is a breach.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt

from cr.config import Settings
from cr.config import settings as default_settings

log = logging.getLogger(__name__)

API = "https://api.github.com"

# GitHub rejects a JWT with exp more than 10 minutes out. Stay under it, and
# back-date iat: GitHub rejects tokens issued in its own future, which a clock
# a few seconds fast will produce on every single request.
JWT_TTL_S = 540
JWT_SKEW_S = 60

# Refresh this long before actual expiry, so an in-flight request never dies
# holding a token that expired between the check and the call.
TOKEN_SKEW_S = 120


class AuthError(RuntimeError):
    """The App cannot authenticate. Distinct from a repo-level 403."""


class InstallationGone(AuthError):
    """GitHub says the installation does not exist: the App was uninstalled."""

    def __init__(self, installation_id: int) -> None:
        super().__init__(
            f"installation {installation_id} does not exist for this App. "
            "It was probably uninstalled."
        )
        self.installation_id = installation_id


# The installation listing stops after this many pages of 100. A listing that
# fills them all may be cut short, so nothing can be read into what it lacks.
INSTALLATION_PAGES = 100

# A GitHub login: what may go into an API path. Anything else is refused
# before it reaches GitHub.
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class Secret:
    """A string that refuses to print itself.

    Wrapping is not paranoia theatre: `httpx` puts request objects in
    tracebacks, `dataclasses` generate `__repr__` from fields, and logging a
    dict of config is a thing everyone does eventually.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def fingerprint(self) -> str:
        """A stable, non-reversible id, so two secrets can be compared in logs."""
        return hashlib.sha256(self._value.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class InstallationToken:
    token: Secret
    expires_at: float
    repositories: tuple[str, ...] = ()

    def alive(self, *, skew: float = TOKEN_SKEW_S) -> bool:
        return time.time() < self.expires_at - skew


@dataclass
class AppAuth:
    """Mints installation tokens from the App private key.

    One instance per process. Safe to share across concurrent requests: the
    per-key locks make a cold cache produce one token exchange, not N.
    """

    app_id: str
    private_key: Secret
    api_url: str = API
    user_agent: str = "cr-review-app"

    _jwt: tuple[str, float] | None = field(default=None, repr=False)
    _tokens: dict[tuple[int, tuple[str, ...]], InstallationToken] = field(
        default_factory=dict, repr=False
    )
    _locks: dict[tuple[int, tuple[str, ...]], asyncio.Lock] = field(
        default_factory=dict, repr=False
    )
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    # Told when GitHub says an installation no longer exists, so the record
    # of it can be dropped by whoever owns that record.
    on_gone: Callable[[int], None] | None = field(default=None, repr=False)

    @classmethod
    def from_settings(cls, s: Settings | None = None) -> AppAuth:
        s = s or default_settings
        key = s.app_private_key()
        if not s.github_app_id or not key:
            raise AuthError(
                "GitHub App is not configured. Set CR_GITHUB_APP_ID and "
                "CR_GITHUB_APP_PRIVATE_KEY (or _PATH), or run the setup flow at /app/setup."
            )
        return cls(app_id=str(s.github_app_id).strip(), private_key=Secret(key))

    # --- transport ----------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                headers={
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": self.user_agent,
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # --- the App JWT --------------------------------------------------------

    def app_jwt(self) -> str:
        """A short-lived RS256 assertion proving we hold the App private key."""
        now = int(time.time())
        if self._jwt is not None and now < self._jwt[1]:
            return self._jwt[0]
        try:
            token = jwt.encode(
                {"iat": now - JWT_SKEW_S, "exp": now + JWT_TTL_S, "iss": self.app_id},
                self.private_key.get(),
                algorithm="RS256",
            )
        except Exception as exc:  # noqa: BLE001 - surface a usable message, not a crypto trace
            raise AuthError(
                f"could not sign the App JWT ({type(exc).__name__}). The private key must be "
                "the unmodified PEM GitHub gave you."
            ) from exc
        # Drop it well before exp so a long request cannot outlive it.
        self._jwt = (token, now + JWT_TTL_S - 120)
        return token

    # --- installation tokens ------------------------------------------------

    async def installation_token(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> str:
        """A repo-scoped, one-hour token for `installation_id`.

        `repositories` takes bare repo names (`api`, not `acme/api`) and is the
        least-privilege lever that matters here: an installation granted twenty
        repos still hands the review path a token for exactly one.
        """
        scope = tuple(sorted(repositories or ()))
        cache_key = (installation_id, scope)

        cached = self._tokens.get(cache_key)
        if cached is not None and cached.alive():
            return cached.token.get()

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            # Another waiter may have refreshed it while we queued.
            cached = self._tokens.get(cache_key)
            if cached is not None and cached.alive():
                return cached.token.get()
            fresh = await self._mint(installation_id, scope)
            self._tokens[cache_key] = fresh
            return fresh.token.get()

    async def _mint(self, installation_id: int, scope: tuple[str, ...]) -> InstallationToken:
        body: dict[str, object] = {}
        if scope:
            body["repositories"] = list(scope)

        r = await self._http().post(
            f"/app/installations/{installation_id}/access_tokens",
            json=body,
            headers={"Authorization": f"Bearer {self.app_jwt()}"},
        )
        if r.status_code == 401:
            raise AuthError(
                "GitHub rejected the App JWT. Check CR_GITHUB_APP_ID matches the private key."
            )
        if r.status_code == 404:
            self.forget(installation_id)
            if self.on_gone is not None:
                try:
                    self.on_gone(installation_id)
                except Exception as e:  # noqa: BLE001 - the 404 is the news; report it
                    log.warning("could not record installation %s as gone: %s", installation_id, e)
            raise InstallationGone(installation_id)
        if r.status_code == 422 and scope:
            # The installation does not grant one of these repos. Retry
            # unscoped rather than failing: a narrower token is an
            # optimisation, and a missed review is not an acceptable price.
            log.warning(
                "installation %s does not grant %s; falling back to an unscoped token",
                installation_id,
                ", ".join(scope),
            )
            return await self._mint(installation_id, ())
        if r.status_code >= 400:
            raise AuthError(f"token exchange failed ({r.status_code}): {r.text[:200]}")

        data = r.json()
        expires_at = _parse_expiry(data.get("expires_at"))
        log.info(
            "minted installation token id=%s scope=%s ttl=%.0fs",
            installation_id,
            ",".join(scope) or "all",
            expires_at - time.time(),
        )
        return InstallationToken(
            token=Secret(data["token"]), expires_at=expires_at, repositories=scope
        )

    def forget(self, installation_id: int) -> None:
        """Drop cached tokens for an installation that was revoked or suspended."""
        for key in [k for k in self._tokens if k[0] == installation_id]:
            self._tokens.pop(key, None)

    # --- App-level reads ----------------------------------------------------

    def _as_app(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.app_jwt()}"}

    async def app_metadata(self) -> dict:
        r = await self._http().get("/app", headers=self._as_app())
        r.raise_for_status()
        return r.json()

    async def all_installations(self) -> tuple[list[dict], bool]:
        """Every installation of this App, and whether the listing is complete.

        Only a complete listing says anything about what is missing from it.
        A failed page raises rather than returning what was read so far.
        """
        out: list[dict] = []
        for page in range(1, INSTALLATION_PAGES + 1):
            r = await self._http().get(
                "/app/installations",
                params={"per_page": 100, "page": page},
                headers=self._as_app(),
            )
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            if len(batch) < 100:
                return out, True
        return out, False

    async def list_installations(self) -> list[dict]:
        return (await self.all_installations())[0]

    async def installation(self, installation_id: int) -> dict | None:
        """One installation by id, or None if it does not exist for this App."""
        r = await self._http().get(
            f"/app/installations/{int(installation_id)}", headers=self._as_app()
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def account_installation(self, login: str, account_type: str = "") -> dict | None:
        """This App's installation on one account, or None if it has none.

        One call however many accounts have installed the App, which is what
        makes it cheap enough to ask whenever someone opens the dashboard.
        GitHub has a route for users and one for organisations; when the kind
        of account is unknown, both are tried.
        """
        if not _LOGIN.match(login or ""):
            raise ValueError(f"not a GitHub login: {login!r}")
        kinds = ["orgs", "users"] if account_type == "Organization" else ["users", "orgs"]
        if account_type in ("User", "Organization"):
            kinds = kinds[:1]
        for kind in kinds:
            r = await self._http().get(f"/{kind}/{login}/installation", headers=self._as_app())
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return r.json()
        return None

    # --- webhook deliveries -------------------------------------------------

    async def hook_config(self) -> dict:
        """Where GitHub sends this App's webhooks. Includes no secret."""
        r = await self._http().get("/app/hook/config", headers=self._as_app())
        r.raise_for_status()
        return r.json()

    async def hook_deliveries(self, *, cursor: str = "") -> tuple[list[dict], str]:
        """One page of recent webhook deliveries, newest first, and the cursor
        for the next page ("" when there is none)."""
        params: dict[str, str | int] = {"per_page": 100}
        if cursor:
            params["cursor"] = cursor
        r = await self._http().get("/app/hook/deliveries", params=params, headers=self._as_app())
        r.raise_for_status()
        nxt = r.links.get("next", {}).get("url", "")
        following = parse_qs(urlsplit(nxt).query).get("cursor", [""])[0] if nxt else ""
        return r.json(), following

    async def list_installation_repos(self, installation_id: int) -> list[str]:
        """Full slugs of every repo an installation can reach.

        Needed on start-up: `installation_repositories` webhooks only ever
        carry deltas, so a server that was down during an install would
        otherwise never learn which repos it owns.
        """
        token = await self.installation_token(installation_id)
        out: list[str] = []
        page = 1
        while page <= 20:
            r = await self._http().get(
                "/installation/repositories",
                params={"per_page": 100, "page": page},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code >= 400:
                log.warning(
                    "could not list repos for installation %s: %s",
                    installation_id,
                    r.status_code,
                )
                break
            data = r.json()
            repos = data.get("repositories", [])
            out.extend(x["full_name"] for x in repos)
            if len(repos) < 100:
                break
            page += 1
        return out


def _parse_expiry(value: object) -> float:
    """GitHub sends ISO-8601 with a Z. Fall back to one hour on anything odd —
    a slightly early refresh is free; treating an unparseable date as forever
    is an outage an hour later."""
    from datetime import datetime

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + 3600
