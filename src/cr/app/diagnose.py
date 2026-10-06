"""Is the GitHub App set up the way this server needs, and are webhooks arriving?

Everything here reads from GitHub as the App; nothing changes. The answers
are what an operator otherwise pieces together from three settings pages:
permissions and events the App lacks, where its webhooks go, and whether
GitHub's recent deliveries got through.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from cr.app import manifest
from cr.app.auth import AppAuth

_LEVEL = {"read": 1, "write": 2, "admin": 3}
WINDOW_S = 24 * 3600.0


@dataclass
class Check:
    ok: bool
    title: str
    detail: str = ""


@dataclass
class Report:
    app: str = ""
    hook_url: str = ""
    deliveries: Counter = field(default_factory=Counter)
    failures: list[dict[str, Any]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(c.ok for c in self.checks)


def mask_url(url: str) -> str:
    """A webhook URL with its path shortened: a relay's channel id (smee.io)
    is as good as a password to anyone who reads the log."""
    u = urlsplit(url or "")
    path = u.path if len(u.path) <= 12 else f"{u.path[:6]}…{u.path[-4:]}"
    return f"{u.scheme}://{u.netloc}{path}" if u.netloc else ""


def _epoch(stamp: str | None) -> float:
    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def permission_gaps(granted: dict[str, str]) -> list[str]:
    """Permissions the App needs and lacks, as "name: wanted (has level)"."""
    gaps = []
    for name, wanted in manifest.PERMISSIONS.items():
        has = granted.get(name, "")
        if _LEVEL.get(has, 0) < _LEVEL[wanted]:
            gaps.append(f"{name}: {wanted} (has {has or 'none'})")
    return gaps


def event_gaps(subscribed: list[str]) -> list[str]:
    return [e for e in manifest.EVENTS if e not in set(subscribed)]


async def diagnose(auth: AppAuth, *, recorded_installations: int | None = None) -> Report:
    r = Report()
    app = await auth.app_metadata()
    r.app = f"{app.get('name', '')} ({app.get('slug', '')})"

    gaps = permission_gaps(app.get("permissions") or {})
    r.checks.append(
        Check(
            not gaps,
            "permissions",
            "all granted"
            if not gaps
            else "missing " + "; ".join(gaps) + ". Add them under the App's "
            "Permissions & events, then each installation's owner must accept the change.",
        )
    )
    missing = event_gaps(app.get("events") or [])
    r.checks.append(
        Check(
            not missing,
            "events",
            "all subscribed"
            if not missing
            else "not subscribed to " + ", ".join(missing) + " (Permissions & events).",
        )
    )

    hook = await auth.hook_config()
    url = hook.get("url") or ""
    r.hook_url = mask_url(url)
    scheme = urlsplit(url).scheme
    host = urlsplit(url).netloc
    if not url:
        r.checks.append(Check(False, "webhook url", "none set: GitHub has nowhere to send events"))
    elif scheme != "https":
        r.checks.append(Check(False, "webhook url", "not https: payloads travel in the clear"))
    elif str(hook.get("insecure_ssl", "0")) != "0":
        r.checks.append(Check(False, "webhook url", "TLS verification is off in the App settings"))
    elif host.endswith("smee.io"):
        r.checks.append(
            Check(True, "webhook url", "a smee.io relay: events arrive only while smee-client runs")
        )
    elif host.split(":")[0] in ("localhost", "127.0.0.1"):
        r.checks.append(Check(False, "webhook url", "localhost: GitHub cannot reach it"))
    else:
        r.checks.append(Check(True, "webhook url", r.hook_url))

    horizon = time.time() - WINDOW_S
    rows, _ = await auth.hook_deliveries()
    attempts: dict[str, list[dict]] = {}
    for d in rows:
        if _epoch(d.get("delivered_at")) >= horizon and d.get("guid"):
            attempts.setdefault(d["guid"], []).append(d)
    for tries in attempts.values():
        if any(200 <= (t.get("status_code") or 0) < 300 for t in tries):
            r.deliveries["delivered"] += 1
        else:
            r.deliveries["failed"] += 1
            r.failures.append(max(tries, key=lambda t: _epoch(t.get("delivered_at"))))
    failed, delivered = r.deliveries["failed"], r.deliveries["delivered"]
    if not attempts:
        detail = "no deliveries in the last 24 hours"
    else:
        detail = f"{delivered} delivered, {failed} failed in the last 24 hours"
        if failed and not delivered:
            detail += f": GitHub cannot reach {host or 'the webhook url'}"
    r.checks.append(Check(failed == 0, "deliveries", detail))

    if recorded_installations is not None:
        listing, complete = await auth.all_installations()
        live = len(listing)
        same = live == recorded_installations
        r.checks.append(
            Check(
                same or not complete,
                "installations",
                f"{live} on GitHub, {recorded_installations} recorded here"
                + ("" if same else ": restarting the server reconciles them"),
            )
        )
    return r
