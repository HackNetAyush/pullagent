"""Read-mostly API behind the dashboard.

Queries the same store the CLI writes to, so a review started from a terminal or
from CI shows up live without any extra plumbing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, select

from cr.config import TIERS, settings
from cr.llm.client import RATES
from cr.store import db as store
from cr.store.models import FindingRow, Run, Suppression

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


def _run_json(r: Run) -> dict[str, Any]:
    started = r.created_at.replace(tzinfo=UTC) if r.created_at else None
    # A process that died mid-review leaves 'running' behind forever. Show it as
    # stalled rather than pretending work is still happening.
    stalled = bool(
        r.status == "running" and started and datetime.now(UTC) - started > STALE_AFTER
    )
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
        "error": r.error or "",
        "started_at": started.isoformat() if started else None,
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "provider": settings.provider}


@app.get("/api/overview")
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


@app.get("/api/runs")
def runs(limit: int = 50, repo: str | None = None) -> list[dict[str, Any]]:
    with store.session() as s:
        q = select(Run).order_by(desc(Run.id)).limit(min(limit, 200))
        if repo:
            q = q.where(Run.repo == repo)
        return [_run_json(r) for r in s.execute(q).scalars()]


@app.get("/api/runs/active")
def active_runs() -> list[dict[str, Any]]:
    with store.session() as s:
        q = select(Run).where(Run.status == "running").order_by(desc(Run.id))
        return [_run_json(r) for r in s.execute(q).scalars()]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    with store.session() as s:
        r = s.get(Run, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        findings = s.execute(
            select(FindingRow).where(FindingRow.run_id == run_id)
        ).scalars()
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


@app.get("/api/suppressions")
def suppressions(limit: int = 100) -> list[dict[str, Any]]:
    with store.session() as s:
        rows = s.execute(
            select(Suppression).order_by(desc(Suppression.id)).limit(min(limit, 500))
        ).scalars()
        return [
            {
                "id": x.id,
                "repo": x.repo,
                "fingerprint": x.fingerprint,
                "reason": x.reason,
                "file": x.file,
                "claim": (x.claim or "")[:240],
                "hits": x.hits,
                "pr": x.pr_number,
            }
            for x in rows
        ]


@app.get("/api/config")
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


def mount_ui(app_: FastAPI, dist: Path) -> None:
    """Serve the built dashboard, with SPA fallback for client-side routes."""
    if not dist.is_dir():
        return
    app_.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app_.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        candidate = dist / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


_DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
mount_ui(app, _DIST)
