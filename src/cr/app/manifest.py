"""One-click App creation, via GitHub's manifest flow.

Creating a GitHub App by hand is eleven form fields, a permissions matrix, an
events checklist, a generated private key you must download and then paste
into an env var without mangling its newlines, and a webhook secret you have
to invent. Every one of those is a chance to get it wrong in a way that fails
silently an hour later.

The manifest flow replaces all of it: we describe the App we want, GitHub
shows the user one confirmation page, and hands back the id, the private key
and a webhook secret it generated itself. Three requests, no copy-paste.

    GET  /app/setup           -> a form that POSTs the manifest to GitHub
    GET  /app/setup/callback  -> exchange the code, write credentials, done

Security properties this flow needs, and how each is met:

* **The callback must not be forgeable.** A `state` nonce is generated here,
  held in memory, and required to match — so a link someone else sends you
  cannot write credentials into your server.
* **The code is single-use and short-lived.** GitHub enforces that; we also
  drop the state after one use.
* **The setup routes must not be reachable in production.** They refuse to
  serve once the App is configured, and `CR_APP_ALLOW_SETUP=false` turns them
  off entirely.
* **The private key lands in a 0600 file**, never in a log line and never in
  a response body.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"

# Permissions the App asks for, and why each one is needed. Anything not on
# this list is something CR cannot do, which is the point of listing them.
PERMISSIONS: dict[str, str] = {
    # Read the code under review, clone it, build the symbol index.
    "contents": "read",
    # Read the PR and post the review with its inline threads.
    "pull_requests": "write",
    # PR-level comments: `@pullagent` commands and their answers.
    "issues": "write",
    # Report "reviewing / done / failed" as a check run, so a clean review has
    # somewhere to land that is not another comment.
    "checks": "write",
    # Implicit for any App, listed for honesty.
    "metadata": "read",
    # Org roles at sign-in: members may view an org's dashboard, only its
    # admins may change its API keys, tiers and routing.
    "members": "read",
}

# Events to subscribe to. `installation` and `installation_repositories` are
# deliberately absent: GitHub sends those to every App unconditionally and
# rejects a manifest that asks for them ("Default events unsupported"). They
# still arrive, and `events.decide()` still handles them — subscribing is the
# part that is not allowed, not receiving.
EVENTS = [
    "pull_request",
    "pull_request_review_comment",
    "issue_comment",
]

# Delivered to every App whether or not it asks. Listed so the set of events
# this App actually handles stays readable in one place.
IMPLICIT_EVENTS = ["installation", "installation_repositories"]

STATE_TTL_S = 900


class SetupError(RuntimeError):
    pass


@dataclass
class SetupFlow:
    """Holds the one-time nonces for in-progress App creations."""

    _states: dict[str, float] = field(default_factory=dict)

    def issue_state(self) -> str:
        self._prune()
        state = secrets.token_urlsafe(32)
        self._states[state] = time.time() + STATE_TTL_S
        return state

    def consume_state(self, state: str | None) -> None:
        """Single-use, time-limited. Raises unless this server issued it."""
        self._prune()
        if not state or self._states.pop(state, None) is None:
            raise SetupError(
                "this setup link did not come from this server, or it has expired. "
                "Open /app/setup again."
            )

    def _prune(self) -> None:
        now = time.time()
        for k in [k for k, exp in self._states.items() if exp < now]:
            self._states.pop(k, None)


def build_manifest(
    public_url: str, *, webhook_url: str = "", name: str = "PullAgent"
) -> dict[str, Any]:
    """The App we want GitHub to create.

    Two URLs, because they are reached by different parties and are not always
    the same host:

    * `public_url` is where the *browser* is sent back after GitHub creates the
      App, so `http://localhost:8000` is perfectly valid — it is your own
      browser making that request.
    * `webhook_url` is where *GitHub's servers* POST events, so it must be
      reachable from the internet.

    A tunnel (`cloudflared`, `ngrok`) serves both from one host and needs only
    `public_url`. A webhook relay (smee.io) gives you a public inbox that is
    not your server, so the two differ and `webhook_url` is passed explicitly.
    """
    base = public_url.rstrip("/")
    hook = (webhook_url or f"{base}/webhook").rstrip("/")
    return {
        "name": name,
        "url": base,
        "hook_attributes": {"url": hook, "active": True},
        "redirect_url": f"{base}/app/setup/callback",
        # Back to the dashboard after an install, which checks GitHub for it
        # rather than waiting on the webhook.
        "setup_url": f"{base}/app/installed",
        "setup_on_update": True,
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": EVENTS,
        "description": (
            "High-precision AI code review. Finds concrete defects, verifies them before "
            "posting, and replies when you push back."
        ),
    }


async def exchange_code(code: str, *, api_url: str = API) -> dict[str, Any]:
    """Trade the temporary code for the App's permanent credentials.

    One shot: GitHub invalidates the code immediately, so a failure here means
    starting the flow again rather than retrying.
    """
    async with httpx.AsyncClient(base_url=api_url, timeout=30.0) as c:
        r = await c.post(
            f"/app-manifests/{code}/conversions",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if r.status_code >= 400:
        raise SetupError(f"GitHub rejected the conversion ({r.status_code}): {r.text[:200]}")
    return r.json()


def save_credentials(data: dict[str, Any], path: Path) -> Path:
    """Write the App credentials to a private file.

    0600 before the bytes, not after: a world-readable window of even a few
    milliseconds is a window. On Windows, `chmod` is close to a no-op, so the
    file inherits the user profile's ACL — which is why the setup flow is
    localhost-only and the docs point at a secret manager for production.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "app_id": data.get("id"),
        "slug": data.get("slug", ""),
        "name": data.get("name", ""),
        "client_id": data.get("client_id", ""),
        "client_secret": data.get("client_secret", ""),
        "webhook_secret": data.get("webhook_secret", ""),
        "pem": data.get("pem", ""),
        "html_url": data.get("html_url", ""),
        "owner": (data.get("owner") or {}).get("login", ""),
    }

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    log.info("wrote App credentials to %s (app_id=%s)", path, payload["app_id"])
    return path


def load_credentials(path: Path) -> dict[str, Any] | None:
    path = Path(path).expanduser()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read App credentials at %s: %s", path, e)
        return None


def update_identity(path: Path, *, slug: str, name: str) -> bool:
    """Record the App's current slug and name in the saved credentials.

    Both change when the App is renamed on GitHub, and nothing tells us: no
    webhook fires. Only these two fields are rewritten; the secrets are left
    exactly as they were. Returns whether anything changed.
    """
    path = Path(path).expanduser()
    creds = load_credentials(path)
    if not creds or (creds.get("slug") == slug and creds.get("name") == name):
        return False
    creds["slug"], creds["name"] = slug, name
    # The file exists, so O_TRUNC keeps its 0600 mode.
    fd = os.open(str(path), os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, indent=2)
    return True


def apply_credentials(creds: dict[str, Any], settings: Any) -> None:
    """Load saved credentials into the live Settings object.

    Mutating settings in place is what lets the App start serving webhooks the
    moment setup finishes, with no restart. Env vars still win: if someone
    deliberately set CR_GITHUB_APP_ID, a stale file must not override it.
    """
    if not settings.github_app_id and creds.get("app_id"):
        settings.github_app_id = str(creds["app_id"])
    if not settings.github_app_private_key and creds.get("pem"):
        settings.github_app_private_key = creds["pem"]
    if not settings.github_webhook_secret and creds.get("webhook_secret"):
        settings.github_webhook_secret = creds["webhook_secret"]
    if not settings.github_app_slug and creds.get("slug"):
        settings.github_app_slug = creds["slug"]
    if not settings.github_client_id and creds.get("client_id"):
        settings.github_client_id = creds["client_id"]
    if not settings.github_client_secret and creds.get("client_secret"):
        settings.github_client_secret = creds["client_secret"]


# --- the two pages ----------------------------------------------------------
#
# Deliberately plain HTML with no assets: this runs before anything is
# configured, often over a tunnel, and a page that needs a CDN is a page that
# fails when you most need it to work.


_STYLE = """
:root { color-scheme: light dark; }
body { font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, sans-serif;
       max-width: 46rem; margin: 6vh auto; padding: 0 1.5rem; }
h1 { font-size: 1.5rem; margin-bottom: .25rem; }
p.sub { opacity: .7; margin-top: 0; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .875em; }
pre { padding: .75rem 1rem; border-radius: .5rem; overflow-x: auto;
      background: rgba(127,127,127,.12); }
button { font: inherit; font-weight: 600; padding: .6rem 1.1rem; border-radius: .5rem;
         border: 0; background: #6e40c9; color: #fff; cursor: pointer; }
ul { padding-left: 1.2rem; } li { margin: .3rem 0; }
.ok { color: #1a7f37; } .warn { color: #9a6700; }
"""


def setup_page(
    public_url: str, state: str, *, webhook_url: str = "", name: str = "PullAgent"
) -> str:
    """A form that POSTs the manifest to GitHub. One button, no fields."""
    manifest = json.dumps(build_manifest(public_url, webhook_url=webhook_url, name=name))
    hook = (webhook_url or f"{public_url.rstrip('/')}/webhook").rstrip("/")
    local = hook.startswith(("http://localhost", "http://127.0.0.1", "http://0.0.0.0"))
    warning = (
        '<p class="warn"><strong>That webhook URL is not reachable from the internet.</strong> '
        "GitHub will create the App, but no event will ever arrive. Restart with "
        "<code>--webhook-url</code> pointing at a tunnel or relay, or edit the URL on the "
        "App&rsquo;s settings page afterwards.</p>"
        if local
        else ""
    )
    perms = "\n".join(
        f"<li><code>{escape(k)}: {escape(v)}</code></li>" for k, v in PERMISSIONS.items()
    )
    return f"""<title>Create the CR GitHub App</title>
<style>{_STYLE}</style>
<h1>Create your CR GitHub App</h1>
<p class="sub">GitHub creates the App, generates the key and the webhook secret,
and hands them back. Nothing to copy by hand.</p>

<p>Webhooks will be delivered to <code>{escape(hook)}</code>.</p>
{warning}

<p>It will ask for:</p>
<ul>{perms}</ul>

<form action="https://github.com/settings/apps/new?state={escape(state)}" method="post">
  <input type="hidden" name="manifest" value='{escape(manifest, quote=True)}'>
  <button type="submit">Create it on GitHub &rarr;</button>
</form>

<p class="sub">To create it on an organisation instead, add
<code>?org=your-org</code> to this page's URL.</p>
"""


def done_page(creds: dict[str, Any], path: Path) -> str:
    slug = creds.get("slug", "")
    install = (
        f"https://github.com/apps/{slug}/installations/new"
        if slug
        else creds.get("html_url", "https://github.com/settings/apps")
    )
    return f"""<title>CR App created</title>
<style>{_STYLE}</style>
<h1 class="ok">App created</h1>
<p class="sub"><strong>{escape(creds.get("name", "CR"))}</strong>
&middot; app id <code>{escape(str(creds.get("app_id", "")))}</code></p>

<p>Credentials were written to <code>{escape(str(path))}</code> and loaded. This server is
already verifying webhook signatures with the new secret &mdash; no restart needed.</p>

<p><a href="{escape(install)}"><button type="button">Install it on your account
&rarr;</button></a></p>

<h2>Then</h2>
<p>Open a pull request. PullAgent reviews it, and re-reviews the delta every time you push.
Reply to any of its comments and it will answer.</p>

<p class="warn">Keep that credentials file. Re-running setup creates a second App,
not a new key for this one. For anything beyond your own machine, move these into a
secret manager and set <code>CR_APP_ALLOW_SETUP=false</code>.</p>
"""
