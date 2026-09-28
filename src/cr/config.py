"""Configuration. Tier routing lives here because it is a product decision."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER = "<<< FILL ME >>>"


class TierConfig(BaseModel):
    name: str
    model: str
    effort: str
    finders: list[str]
    verifier_lenses: list[str]
    max_comments: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CR_", env_file=".env", extra="ignore")

    # "anthropic" (first-party API) or "foundry" (Claude on Microsoft Foundry).
    provider: str = "anthropic"

    anthropic_api_key: str | None = None

    # Accepts bare GITHUB_TOKEN too, so .env and CI env vars both work.
    github_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("CR_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"),
    )

    # Foundry: set resource for the standard endpoint shape, or base_url directly.
    azure_api_key: str | None = None
    azure_resource: str | None = None
    azure_base_url: str | None = None

    # Per-role overrides; blank falls back to the shared values above.
    finder_base_url: str | None = None
    finder_api_key: str | None = None
    verifier_base_url: str | None = None
    verifier_api_key: str | None = None

    # Foundry deployment names. Must be Settings fields, not os.environ lookups:
    # pydantic-settings loads .env into this object, not the process env.
    model_small: str = "claude-sonnet-5"
    model_standard: str = "claude-sonnet-5"
    model_deep: str = "claude-sonnet-5"
    model_verifier: str = "claude-opus-5"

    # Hunks/files, not token counts: routing must not depend on a tokenizer.
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

    # Hard ceiling. 1M is available but using it degrades quality and costs linearly.
    max_context_tokens: int = 25_000

    # Findings below this confidence are dropped before verification.
    min_confidence: float = 0.35

    def endpoint_for(self, role: str) -> tuple[str | None, str | None]:
        """Return (base_url, api_key) for 'finder' or 'verifier', with fallback."""
        base = getattr(self, f"{role}_base_url", None) or self.azure_base_url
        key = getattr(self, f"{role}_api_key", None) or self.azure_api_key
        return base, key

    def unfilled(self) -> list[str]:
        """Settings still holding the .env placeholder. A placeholder key looks
        like a broken endpoint, so report it by name."""
        out = []
        for name in ("azure_api_key", "azure_resource", "azure_base_url", "anthropic_api_key"):
            value = getattr(self, name, None)
            if value and PLACEHOLDER.strip("<> ") in str(value):
                out.append(f"CR_{name.upper()}")
        return out


settings = Settings()


def build_tiers(s: Settings) -> dict[str, TierConfig]:
    """ARCHITECTURE.md §3.2. Costs in PIPELINE.md §2.1 assume caching is working."""
    return {
        # Small, low-blast-radius changes.
        "T1": TierConfig(
            name="T1",
            model=s.model_small,
            effort="low",
            finders=["correctness"],
            verifier_lenses=["evidence"],
            max_comments=3,
        ),
        # ~$0.23 — the 80% case.
        "T2": TierConfig(
            name="T2",
            model=s.model_standard,
            effort="high",
            finders=["correctness", "api_contract", "test_coverage"],
            verifier_lenses=["correctness", "reachability"],
            max_comments=6,
        ),
        # ~$0.50 — auth, payments, migrations, concurrency, or very large diffs.
        "T3": TierConfig(
            name="T3",
            model=s.model_deep,
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


TIERS: dict[str, TierConfig] = build_tiers(settings)

# T3 escalates verification only — the finders stay on the cheaper model.
T3_VERIFIER_MODEL = settings.model_verifier
