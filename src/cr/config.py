"""Configuration. Tier routing lives here because it is a product decision."""

from __future__ import annotations

import os

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class TierConfig(BaseModel):
    name: str
    model: str
    effort: str
    finders: list[str]
    verifier_lenses: list[str]
    max_comments: int


def _model(env_var: str, default: str) -> str:
    """Model IDs are env-overridable because Microsoft Foundry routes by
    *deployment name*, which may not match the canonical model ID."""
    return os.environ.get(env_var, "").strip() or default


# Defaults assume Claude Sonnet 5 and Opus 5 are available. There is deliberately
# no Haiku tier: not every deployment has one, and Sonnet at effort=low is close
# enough in cost while removing a model from the required set.
MODEL_SMALL = _model("CR_MODEL_SMALL", "claude-sonnet-5")
MODEL_STANDARD = _model("CR_MODEL_STANDARD", "claude-sonnet-5")
MODEL_DEEP = _model("CR_MODEL_DEEP", "claude-sonnet-5")

# T3 escalates verification only — the finders stay on the cheaper model.
T3_VERIFIER_MODEL = _model("CR_MODEL_VERIFIER", "claude-opus-5")


# ARCHITECTURE.md §3.2. Costs in PIPELINE.md §2.1 assume prompt caching is working.
TIERS: dict[str, TierConfig] = {
    # Small, low-blast-radius changes.
    "T1": TierConfig(
        name="T1",
        model=MODEL_SMALL,
        effort="low",
        finders=["correctness"],
        verifier_lenses=["evidence"],
        max_comments=3,
    ),
    # ~$0.23 — the 80% case.
    "T2": TierConfig(
        name="T2",
        model=MODEL_STANDARD,
        effort="high",
        finders=["correctness", "api_contract", "test_coverage"],
        verifier_lenses=["correctness", "reachability"],
        max_comments=6,
    ),
    # ~$0.50 — auth, payments, migrations, concurrency, or very large diffs.
    "T3": TierConfig(
        name="T3",
        model=MODEL_DEEP,
        effort="xhigh",
        finders=[
            "correctness",
            "security",
            "concurrency",
            "api_contract",
            "test_coverage",
            "performance",
        ],
        verifier_lenses=["correctness", "reachability", "evidence"],
        max_comments=8,
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CR_", env_file=".env", extra="ignore")

    # "anthropic" (first-party API) or "foundry" (Claude on Microsoft Foundry).
    provider: str = "anthropic"

    anthropic_api_key: str | None = None

    # Microsoft Foundry. Set CR_AZURE_RESOURCE for the standard endpoint shape,
    # or CR_AZURE_BASE_URL to point at it directly.
    azure_api_key: str | None = None
    azure_resource: str | None = None
    azure_base_url: str | None = None

    # Triage thresholds (hunks/files, deliberately NOT token counts — routing must
    # not depend on which model's tokenizer you would have used).
    t1_max_hunks: int = 8
    t1_max_files: int = 3
    t3_min_hunks: int = 40

    # Paths that force T3 regardless of size.
    sensitive_patterns: tuple[str, ...] = (
        "auth",
        "login",
        "session",
        "password",
        "crypto",
        "payment",
        "billing",
        "migration",
        "migrations",
        "permission",
        "acl",
        "token",
    )

    # Paths that never warrant a review (T0 — exits before any model call).
    skip_patterns: tuple[str, ...] = (
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "go.sum",
        "/vendor/",
        "/node_modules/",
        "/dist/",
        "/build/",
        ".min.js",
        ".min.css",
        ".snap",
        "_pb2.py",
        ".generated.",
    )

    # Hard context ceiling. We have 1M available; using it degrades quality and
    # costs linearly. Treat the window as headroom for the rare huge PR.
    max_context_tokens: int = 25_000

    # Findings below this confidence are dropped before verification.
    min_confidence: float = 0.35


settings = Settings()
