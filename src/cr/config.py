"""Configuration. Tier routing lives here because it is a product decision."""

from __future__ import annotations

from pathlib import Path

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
    # None means "verify on the same model as the finders". Set this to escalate
    # (or, as with T4, to keep verification on a different provider entirely).
    verifier_model: str | None = None
    verification_batch_size: int = Field(default=6, ge=1, le=12)
    verifier_effort: str = "medium"
    # High-effort reasoning shares this ceiling with the structured answer.
    # A 16k cap exhausted all three live T2 finder calls before JSON was emitted.
    finder_max_tokens: int = Field(default=32000, ge=1024)
    verifier_max_tokens: int = Field(default=12000, ge=1024)


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

    # --- GitHub App (`cr app serve`) ---------------------------------------
    # Set by hand, or written for you by the /app/setup manifest flow. Blank is
    # fine: everything else in CR (CLI, Action) works without an App.
    github_app_id: str | None = None
    github_app_slug: str = ""
    # OAuth credentials, used for "sign in with GitHub" on the dashboard. The
    # manifest flow already receives these when the App is created; they are
    # exposed as settings so a deployment can inject them from Key Vault.
    github_client_id: str = ""
    github_client_secret: str = ""
    # Signed-in sessions last this long before the browser must sign in again.
    session_ttl_s: int = Field(default=14 * 24 * 3600, ge=300)
    # Logins seeded as administrators on first sign-in. Without at least one,
    # nobody can approve an account and a public App reviews nothing.
    admin_logins: str = ""
    # The PEM itself, or a path to it. A PEM pasted into an env var usually
    # arrives backslash-escaped or base64-encoded; `app_private_key()` repairs
    # both, because a mis-encoded key should not look like a missing one.
    github_app_private_key: str | None = None
    github_app_private_key_path: Path | None = None
    github_webhook_secret: str | None = None

    # Where the manifest flow persists what GitHub hands back. Credentials never
    # go in the repo; this defaults under the existing cache dir.
    app_credentials_path: Path | None = None

    # Review only what changed since the last reviewed head, instead of the
    # whole PR again. Findings are deduped by fingerprint either way, so the
    # difference is cost, not correctness.
    app_incremental: bool = True
    # Draft PRs are work in progress. Review on `ready_for_review` instead.
    app_review_drafts: bool = False
    # Fork PRs are read, cloned and linted, never executed. Turn this off if you
    # do not want untrusted branches touching the host at all.
    app_review_forks: bool = True
    # Whole PR reviews in flight at once, across all installations.
    app_max_concurrent_reviews: int = Field(default=2, ge=1, le=16)
    # Wait this long after a push before reviewing: a rapid series of commits
    # collapses into one review of the final head.
    app_debounce_s: float = Field(default=20.0, ge=0.0, le=600.0)
    # What addresses the bot in a PR comment: "@pullagent review", "@pullagent ask ...".
    app_command_prefix: str = "@pullagent"
    # Reply to human replies on our own review threads.
    app_reply_to_comments: bool = True
    app_reply_model: str | None = None
    # Serve the one-click App-creation flow at /app/setup. Turn off once created.
    app_allow_setup: bool = True
    # Review only for accounts an admin has approved. On by default because the
    # cost of forgetting it on a public App is a stranger spending the model
    # budget, while the cost of it being on unnecessarily is one `cr app approve`.
    app_require_approval: bool = True
    # Where the store lives. Blank means SQLite under the cache dir, which is
    # right for a laptop and wrong for a container with no persistent disk.
    # Deployed: postgresql+psycopg://user:pass@host:5432/cr?sslmode=require
    db_url: str | None = None
    # Azure Service Bus connection string. Set it and the queue becomes
    # cross-process, which is what lets more than one replica exist without two
    # of them reviewing the same push. Blank keeps the in-process queue.
    servicebus_connection: str | None = None
    servicebus_queue: str = "cr-jobs"
    # "web" serves HTTP and produces jobs; "worker" only consumes them; "all"
    # does both, which is the single-container and laptop shape.
    app_role: str = "all"
    # Index every repo the App is installed on, up front, so the first PR is warm.
    app_index_on_install: bool = True

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

    # T4 (experimental): finders on an OpenAI-Responses-API-shaped deployment,
    # verification stays on Claude (model_standard). Not the same wire protocol
    # as the Foundry Claude deployments above, so it gets its own credentials —
    # defaulting to the Azure ones for convenience, but independently rotatable.
    model_t4: str = "gpt-6-luna"
    openai_api_key: str | None = None
    openai_base_url: str | None = None

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

    # Diff content that forces T3 regardless of size (ARCHITECTURE.md §3.2 promises
    # this for concurrency; the path-based check above cannot catch it since a file
    # like `flusher.py` never mentions concurrency in its name). T2 has no
    # "concurrency" finder lens, so a race or process-lifecycle bug in a change this
    # small would otherwise only get correctness/api_contract/state_and_security eyes.
    concurrency_patterns: tuple[str, ...] = (
        "import threading",
        "import multiprocessing",
        "from threading import",
        "from multiprocessing import",
        "from concurrent.futures import",
        "import concurrent.futures",
        "ThreadPoolExecutor",
        "ProcessPoolExecutor",
        "threading.Lock",
        "threading.RLock",
        "threading.Thread",
        "multiprocessing.Process",
        "multiprocessing.get_context",
        "asyncio.Lock(",
        "asyncio.Semaphore(",
        "asyncio.gather(",
        "os.fork(",
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
    max_concurrency: int = Field(default=2, ge=1, le=32)
    review_cache: bool = True
    review_cache_ttl_s: int = Field(default=86400, ge=0)
    finder_chunk_chars: int = Field(default=60000, ge=4000)

    def endpoint_for(self, role: str) -> tuple[str | None, str | None]:
        """Return (base_url, api_key) for 'finder' or 'verifier', with fallback."""
        base = getattr(self, f"{role}_base_url", None) or self.azure_base_url
        key = getattr(self, f"{role}_api_key", None) or self.azure_api_key
        return base, key

    def openai_endpoint(self) -> tuple[str | None, str]:
        """Return (base_url, api_key) for the Responses-API-shaped deployments
        (T4). Falls back to the Azure Foundry credentials/resource for
        convenience, but CR_OPENAI_API_KEY/CR_OPENAI_BASE_URL rotate independently."""
        base = self.openai_base_url or (
            f"https://{self.azure_resource}.services.ai.azure.com/openai/v1"
            if self.azure_resource
            else None
        )
        key = self.openai_api_key or self.azure_api_key or ""
        return base, key

    def app_private_key(self) -> str | None:
        """The App's RSA private key as PEM text, from the env var or the file.

        Env vars cannot hold real newlines in most deployment UIs, so a PEM
        arrives either base64-encoded or with literal backslash-n. Both are
        normalised here; a key that is merely mis-encoded should not look like a
        missing key.
        """
        raw = self.github_app_private_key
        if raw:
            key = raw.strip()
            if "-----BEGIN" not in key:
                import base64
                import binascii

                try:
                    key = base64.b64decode(key, validate=True).decode("utf-8")
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    return None
            return key.replace("\\n", "\n")
        if self.github_app_private_key_path:
            path = Path(self.github_app_private_key_path).expanduser()
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None

    def app_configured(self) -> bool:
        return bool(self.github_app_id and self.app_private_key())

    def credentials_path(self) -> Path:
        if self.app_credentials_path:
            return Path(self.app_credentials_path).expanduser()
        from cr.repo import default_cache_dir  # noqa: PLC0415 - avoids an import cycle

        return default_cache_dir() / "github-app.json"

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
        # effort="medium", not "high": at "high", three finder calls alone burned
        # 86K output/thinking tokens (85% of a $1.57 review) for 12 raw candidates.
        # Confirmed on the same PR at "medium": cost $1.57 -> $0.93 (-41%), time
        # 638s -> 382s (-40%), core recall 0.6 -> 0.8 (better, not worse).
        "T2": TierConfig(
            name="T2",
            model=s.model_standard,
            effort="medium",
            finders=["correctness", "api_contract", "state_and_security"],
            verifier_lenses=["correctness", "reachability"],
            max_comments=12,
        ),
        # ~$0.50 — auth, payments, migrations, concurrency, or very large diffs.
        # effort="high", not "xhigh": T2's finder cost dropped 41% and recall
        # *improved* going high -> medium on the same PR (see above), and
        # ARCHITECTURE.md §3.4 says "Default to high; use xhigh only for T3" —
        # xhigh was T3's one deliberate escalation above that default. T3 already
        # escalates verification to Opus 5, a heavier model; stacking xhigh
        # reasoning on top of that model spends twice. Pending: a real validation
        # run at "high" to confirm recall holds here the way it did at T2.
        "T3": TierConfig(
            name="T3",
            model=s.model_deep,
            effort="high",
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
            # T3 escalates verification only — the finders stay on the cheaper model.
            verifier_model=s.model_verifier,
            finder_max_tokens=64000,
        ),
        # Experimental: finders on GPT-6 Luna (Azure Foundry, Responses API) at
        # roughly 1/30th Sonnet 5's per-token price; verification stays on Claude
        # Sonnet 5 as an independent adversarial check. No shared-prefix cache
        # exists across this provider boundary, so unlike T1-T3 there is no
        # discount to assume here — see PIPELINE.md §2.2 and client.py's
        # `fanout()` docstring for why that trick is Anthropic-stream-specific.
        # Opt-in only (`cr review --tier T4`) — triage never routes here.
        "T4": TierConfig(
            name="T4",
            model=s.model_t4,
            effort="high",
            finders=["correctness", "api_contract", "test_coverage"],
            verifier_lenses=["correctness", "reachability"],
            max_comments=6,
            verifier_model=s.model_standard,
        ),
    }


TIERS: dict[str, TierConfig] = build_tiers(settings)
