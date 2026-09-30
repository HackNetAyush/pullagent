"""Exact-input review cache. Never reuse findings across changed code or policy."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from cr.config import Settings, TierConfig
from cr.llm.prefix import PRContext, RepoContext
from cr.models import ReviewResult
from cr.repo import default_cache_dir

log = logging.getLogger(__name__)


def implementation_hash() -> str:
    root = Path(__file__).parents[1]
    # Changes to retrieval, models, prompts, transport or filtering invalidate old reviews.
    paths = sorted(root.rglob("*.py"))
    return hashlib.sha256(
        b"".join(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes() for p in paths)
    ).hexdigest()


def review_key(
    repo: RepoContext, pr: PRContext, tier: TierConfig, cfg: Settings, head_sha: str = ""
) -> str:
    data = {
        "version": implementation_hash(),
        "repo": asdict(repo),
        "pr": asdict(pr),
        "tier": tier.model_dump(),
        "head_sha": head_sha,
        "threshold": cfg.min_confidence,
        "chunk_chars": cfg.finder_chunk_chars,
        "provider": cfg.provider,
        "endpoint": cfg.azure_base_url or cfg.azure_resource,
        "finder_endpoint": cfg.finder_base_url,
        "verifier_endpoint": cfg.verifier_base_url,
        "openai_endpoint": cfg.openai_base_url,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def load(key: str, ttl_s: int) -> ReviewResult | None:
    path = default_cache_dir() / "reviews" / f"{key}.json"
    try:
        if ttl_s <= 0 or time.time() - path.stat().st_mtime > ttl_s:
            return None
        result = ReviewResult.model_validate_json(path.read_text(encoding="utf-8"))
        if result.review_key != key or result.errors:
            return None
        return result
    except (OSError, ValueError):
        return None


def save(result: ReviewResult) -> None:
    if result.errors or not result.review_key:
        return
    path = default_cache_dir() / "reviews" / f"{result.review_key}.json"
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as f:
            tmp = Path(f.name)
            f.write(result.model_dump_json())
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("review cache unavailable: %s", exc)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
