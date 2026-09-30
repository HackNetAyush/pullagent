"""Read-mostly API behind the dashboard.

Queries the same store the CLI writes to, so a review started from a terminal or
from CI shows up live without any extra plumbing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, select

from cr.config import TIERS, settings
from cr.llm.client import RATES
from cr.store import db as store
from cr.store.models import FindingRow, PRState, Run, Suppression

log = logging.getLogger(__name__)

app = FastAPI(title="CR dashboard", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

STALE_AFTER = timedelta(minutes=20)

# A kill rate over a handful of findings says nothing. Do not flag below this.
MIN_KILL_RATE_SAMPLE = 20


# Pagination defaults. A table that silently shows "the first 50 of who knows
# how many" is worse than one that says 50 of 3,214 — every list endpoint
# returns a total so the UI can page honestly.
DEFAULT_LIMIT = 25
MAX_LIMIT = 200


def page(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
    }


def _bounds(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def guard(request: Request) -> None:
    """Require a signed-in user once sign-in is configured.

    Deliberately permissive when it is not: `cr serve` on a laptop has no
    OAuth app and no users, and demanding a login there would lock the owner
    out of their own machine. The moment CR_GITHUB_CLIENT_ID is set — which is
    the moment the dashboard is reachable by anyone else — every read below
    requires a session.
    """
    from cr.app import accounts
    from cr.app.authroutes import current_user

    if not accounts.configured(settings):
        return
    if current_user(request) is None:
        raise HTTPException(status_code=401, detail="sign in to view this")


# Every read below is behind the guard. Health stays open so a load balancer
# can probe it without credentials.
api = APIRouter(dependencies=[Depends(guard)])


def _run_json(r: Run) -> dict[str, Any]:
    started = r.created_at.replace(tzinfo=UTC) if r.created_at else None
    # A process that died mid-review leaves 'running' behind forever. Show it as
    # stalled rather than pretending work is still happening.
    stalled = bool(r.status == "running" and started and datetime.now(UTC) - started > STALE_AFTER)
    return {
        "id": r.id,
        "repo": r.repo,
        "pr": r.pr_number,
        "tier": r.tier,
        "model": r.model,
        "source": r.source,
        "actor": r.actor or "",
        "status": "stalled" if stalled else r.status,
        "stage": r.stage,
        "stage_index": r.stage_index,
        "stages": store.STAGES,
        "posted": r.posted,
        "killed": r.killed_by_verifier,
        "cost": round(r.cost_usd, 4),
        "elapsed": round(r.elapsed_s, 1),
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_read": r.cache_read_tokens,
        "cache_write": r.cache_write_tokens,
        "cache_hit": r.cache_hit,
        "cached_cost_usd": round(r.cached_cost_usd, 4),
        "judge_cost": round(r.judge_cost_usd, 4),
        "error": r.error or "",
        "started_at": started.isoformat() if started else None,
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "provider": settings.provider}


@api.get("/api/overview")
def overview(days: int = 30) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    # Everything must be materialised inside the session: ORM attributes cannot
    # be refreshed once it closes (DetachedInstanceError).
    with store.session() as s:
        runs = list(
            s.execute(select(Run).where(Run.created_at >= since.replace(tzinfo=None))).scalars()
        )
        done = [r for r in runs if r.status == "done"]
        active = sum(1 for r in runs if r.status == "running")

        posted = sum(r.posted for r in done)
        killed = sum(r.killed_by_verifier for r in done)
        judged = sum(r.verified_count or (r.posted + r.killed_by_verifier) for r in done)
        cost = sum(r.cost_usd for r in done)
        cache_read = sum(r.cache_read_tokens for r in done)
        cache_all = cache_read + sum(r.cache_write_tokens + r.input_tokens for r in done)

        n_sup, sup_hits = s.execute(
            select(func.count(Suppression.id), func.sum(Suppression.hits))
        ).one()

        by_day: dict[str, dict[str, float]] = {}
        for r in done:
            key = (r.created_at or datetime.now(UTC)).date().isoformat()
            d = by_day.setdefault(key, {"cost": 0.0, "runs": 0, "posted": 0})
            d["cost"] += r.cost_usd
            d["runs"] += 1
            d["posted"] += r.posted

        severity = dict(
            s.execute(
                select(FindingRow.severity, func.count(FindingRow.id))
                .where(FindingRow.was_posted == 1)
                .group_by(FindingRow.severity)
            ).all()
        )
        by_source: dict[str, float] = {}
        for r in done:
            by_source[r.source] = round(by_source.get(r.source, 0.0) + r.cost_usd, 4)

        by_model = dict(
            s.execute(
                select(Run.model, func.sum(Run.cost_usd))
                .where(Run.status == "done")
                .group_by(Run.model)
            ).all()
        )

        return {
            "window_days": days,
            "runs": len(done),
            "active": active,
            "posted": posted,
            "killed": killed,
            # Denominator is findings the verifier judged, not everything we
            # declined to post. Budget trims were confirmed real.
            "kill_rate": killed / judged if judged else 0.0,
            "kill_rate_sample": judged,
            # Below this the rate is noise: three small reviews is not evidence
            # that the verifier is rubber-stamping.
            "kill_rate_reliable": judged >= MIN_KILL_RATE_SAMPLE,
            "cost": round(cost, 4),
            "cost_per_review": round(cost / len(done), 4) if done else 0.0,
            "cache_hit": cache_read / cache_all if cache_all else 0.0,
            "suppressions": n_sup or 0,
            "suppression_hits": sup_hits or 0,
            "series": [
                {"date": k, **{kk: round(vv, 4) for kk, vv in v.items()}}
                for k, v in sorted(by_day.items())
            ],
            "severity": severity,
            "cost_by_model": {k: round(v, 4) for k, v in by_model.items()},
            "cost_by_source": by_source,
        }


@api.get("/api/runs")
def runs(
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    repo: str | None = None,
    status: str | None = None,
    source: str | None = None,
    tier: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """One page of runs, newest first, with the total behind it."""
    limit, offset = _bounds(limit, offset)
    with store.session() as s:
        where = []
        if repo:
            where.append(Run.repo == repo)
        if status:
            where.append(Run.status == status)
        if source:
            where.append(Run.source == source)
        if tier:
            where.append(Run.tier == tier)
        if q:
            where.append(Run.repo.ilike(f"%{q}%"))

        total = s.execute(select(func.count(Run.id)).where(*where)).scalar_one()
        rows = s.execute(
            select(Run).where(*where).order_by(desc(Run.id)).limit(limit).offset(offset)
        ).scalars()
        return page([_run_json(r) for r in rows], total, limit, offset)


@api.get("/api/runs/facets")
def run_facets() -> dict[str, list[str]]:
    """Distinct values for the filter dropdowns, so the UI never guesses."""
    with store.session() as s:

        def distinct(col) -> list[str]:
            return sorted(v for (v,) in s.execute(select(col).distinct()).all() if v)

        return {
            "repos": distinct(Run.repo),
            "statuses": distinct(Run.status),
            "sources": distinct(Run.source),
            "tiers": distinct(Run.tier),
        }


@api.get("/api/runs/active")
def active_runs() -> list[dict[str, Any]]:
    with store.session() as s:
        q = select(Run).where(Run.status == "running").order_by(desc(Run.id))
        return [_run_json(r) for r in s.execute(q).scalars()]


@api.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    with store.session() as s:
        r = s.get(Run, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        findings = s.execute(select(FindingRow).where(FindingRow.run_id == run_id)).scalars()
        return {
            **_run_json(r),
            "findings": [
                {
                    "id": f.id,
                    "claim": f.claim,
                    "failure_scenario": f.failure_scenario,
                    "category": f.category,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "file": f.file,
                    "line": f.line,
                    "found_by": f.found_by,
                    "posted": bool(f.was_posted),
                    "fingerprint": f.fingerprint,
                }
                for f in findings
            ],
        }


@api.get("/api/suppressions")
def suppressions(
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    repo: str | None = None,
    reason: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    limit, offset = _bounds(limit, offset)
    with store.session() as s:
        where = []
        if repo:
            where.append(Suppression.repo == repo)
        if reason:
            where.append(Suppression.reason == reason)
        if q:
            where.append(Suppression.claim.ilike(f"%{q}%"))

        total = s.execute(select(func.count(Suppression.id)).where(*where)).scalar_one()
        rows = s.execute(
            select(Suppression)
            .where(*where)
            .order_by(desc(Suppression.id))
            .limit(limit)
            .offset(offset)
        ).scalars()
        return page(
            [
                {
                    "id": x.id,
                    "repo": x.repo,
                    "fingerprint": x.fingerprint,
                    "reason": x.reason,
                    "file": x.file,
                    "claim": (x.claim or "")[:240],
                    "note": (x.note or "")[:240],
                    "hits": x.hits,
                    "pr": x.pr_number,
                }
                for x in rows
            ],
            total,
            limit,
            offset,
        )


@api.get("/api/findings")
def findings(
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    repo: str | None = None,
    severity: str | None = None,
    category: str | None = None,
    posted: bool | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """Findings across every run — the view that answers "what does CR
    actually say about my code", which a per-run list cannot."""
    limit, offset = _bounds(limit, offset)
    with store.session() as s:
        where = []
        if repo:
            where.append(FindingRow.repo == repo)
        if severity:
            where.append(FindingRow.severity == severity)
        if category:
            where.append(FindingRow.category == category)
        if posted is not None:
            where.append(FindingRow.was_posted == (1 if posted else 0))
        if q:
            where.append(FindingRow.claim.ilike(f"%{q}%"))

        total = s.execute(select(func.count(FindingRow.id)).where(*where)).scalar_one()
        rows = s.execute(
            select(FindingRow, Run.pr_number, Run.tier)
            .join(Run, Run.id == FindingRow.run_id)
            .where(*where)
            .order_by(desc(FindingRow.id))
            .limit(limit)
            .offset(offset)
        ).all()
        return page(
            [
                {
                    "id": f.id,
                    "run_id": f.run_id,
                    "repo": f.repo,
                    "pr": pr,
                    "tier": tier,
                    "claim": f.claim,
                    "failure_scenario": f.failure_scenario,
                    "category": f.category,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "file": f.file,
                    "line": f.line,
                    "found_by": f.found_by,
                    "posted": bool(f.was_posted),
                    "fingerprint": f.fingerprint,
                }
                for f, pr, tier in rows
            ],
            total,
            limit,
            offset,
        )


@api.get("/api/repos")
def repos(limit: int = DEFAULT_LIMIT, offset: int = 0, q: str | None = None) -> dict[str, Any]:
    """Per-repository rollup: the unit a team actually thinks in."""
    limit, offset = _bounds(limit, offset)
    with store.session() as s:
        where = [Run.repo.ilike(f"%{q}%")] if q else []
        agg = (
            select(
                Run.repo.label("repo"),
                func.count(Run.id).label("runs"),
                func.sum(Run.cost_usd).label("cost"),
                func.sum(Run.posted).label("posted"),
                func.sum(Run.killed_by_verifier).label("killed"),
                func.max(Run.created_at).label("last_run"),
            )
            .where(*where)
            .group_by(Run.repo)
            .subquery()
        )
        total = s.execute(select(func.count()).select_from(agg)).scalar_one()
        rows = s.execute(select(agg).order_by(desc(agg.c.cost)).limit(limit).offset(offset)).all()

        sup = dict(
            s.execute(
                select(Suppression.repo, func.count(Suppression.id)).group_by(Suppression.repo)
            ).all()
        )
        prs = dict(
            s.execute(select(PRState.repo, func.count(PRState.id)).group_by(PRState.repo)).all()
        )
        return page(
            [
                {
                    "repo": r.repo,
                    "runs": r.runs,
                    "cost": round(r.cost or 0.0, 4),
                    "posted": r.posted or 0,
                    "killed": r.killed or 0,
                    "suppressions": sup.get(r.repo, 0),
                    "pull_requests": prs.get(r.repo, 0),
                    "last_run": r.last_run.replace(tzinfo=UTC).isoformat() if r.last_run else None,
                }
                for r in rows
            ],
            total,
            limit,
            offset,
        )


@app.get("/api/me")
def me() -> dict[str, Any]:
    """Who the viewer is, for a dashboard served without the GitHub App.

    `cr app serve` registers a real implementation ahead of this one. This
    exists so `cr serve` on a laptop — which has no OAuth app, no users and no
    sessions — still answers the question the UI asks on first paint, instead
    of falling through to the SPA catch-all and handing the client HTML where
    it expected JSON.
    """
    from cr.app import accounts

    return {"signed_in": False, "sign_in_configured": accounts.configured(settings)}


@api.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "provider": settings.provider,
        "tiers": {
            name: {
                "model": t.model,
                "effort": t.effort,
                "finders": t.finders,
                "verifiers": t.verifier_lenses,
                "max_comments": t.max_comments,
            }
            for name, t in TIERS.items()
        },
        "rates": RATES,
    }


app.include_router(api)


def mount_ui(app_: FastAPI, dist: Path) -> None:
    """Serve the built dashboard, with SPA fallback for client-side routes.

    Two different caching rules, because the two kinds of file have opposite
    needs. Asset filenames carry a content hash, so they can be cached
    forever. `index.html` must not be: it is the file that *names* the current
    asset hash, and a browser holding yesterday's copy will keep loading
    yesterday's bundle against today's API. That mismatch is invisible until
    the frontend calls an endpoint the old backend did not have — and then it
    surfaces as an unexplained JSON parse error.
    """
    if not dist.is_dir():
        return
    app_.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app_.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        # Client-side routes get index.html; unknown API routes must 404 as
        # JSON. Without this an unmatched /api/* returns the SPA shell with a
        # 200 and the caller reports a JSON parse error instead of a 404.
        if path.startswith(("api/", "auth/", "webhook")):
            raise HTTPException(status_code=404, detail=f"no such endpoint: /{path}")
        candidate = dist / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(
            dist / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )


_DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
mount_ui(app, _DIST)
