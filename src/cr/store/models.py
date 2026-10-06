"""Persistence schema.

SQLite by default so it runs with zero infrastructure; point CR_DB_URL at
Postgres and nothing else changes. Everything here is keyed by finding
fingerprint, which is what makes suppression survive across runs and pushes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SCHEMA_VERSION = 9


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    # The workspace that owns the run: the repository's owner, the account the
    # App is installed on. Every dashboard query filters on it.
    account: Mapped[str] = mapped_column(String(255), default="", index=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    tier: Mapped[str] = mapped_column(String(8), default="")
    # 255: a run on a customer's own key records `conn:<id>:<model>`, and
    # model names on those connections run long.
    model: Mapped[str] = mapped_column(String(255), default="")

    # Where the run came from: pr | local | eval | bench. Without this the ledger
    # cannot tell a paid customer review from a benchmark sweep.
    source: Mapped[str] = mapped_column(String(16), default="pr", index=True)
    # Reserved for auth. Null until there is an identity to attribute a run to;
    # the column exists now so adding auth is not a migration.
    actor: Mapped[str] = mapped_column(String(128), default="", index=True)

    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    stage_index: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    posted: Mapped[int] = mapped_column(Integer, default=0)
    suppressed: Mapped[int] = mapped_column(Integer, default=0)
    killed_by_verifier: Mapped[int] = mapped_column(Integer, default=0)
    # Denominator for kill rate: findings the verifier actually judged.
    verified_count: Mapped[int] = mapped_column(Integer, default=0)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_s: Mapped[float] = mapped_column(Float, default=0.0)
    # A cache-hit run legitimately costs $0 / 0 tokens — without this the
    # dashboard cannot tell that apart from a review that just found nothing.
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    cached_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Set after the fact via add_judge_cost() — the benchmark's gold-label
    # scoring call happens after engine.review() has already returned and
    # closed this row, so it cannot be part of cost_usd at finish_run() time.
    # Real `cr review-pr` usage never sets this; only benchmark runs do.
    judge_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # "managed" (our keys) or "byok" (the customer's). Spend limits apply to
    # the first only; the second is tracked so the customer can see it.
    billing: Mapped[str] = mapped_column(String(16), default="managed", index=True)
    # [{"model", "label", "provider", "calls", "input_tokens", "output_tokens",
    #   "cost_usd", "priced"}] — see cr.models.ModelCost.
    model_costs: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    findings: Mapped[list[FindingRow]] = relationship(back_populates="run")


class FindingRow(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)

    claim: Mapped[str] = mapped_column(Text)
    failure_scenario: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    severity: Mapped[str] = mapped_column(String(16), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    file: Mapped[str] = mapped_column(String(512), default="")
    line: Mapped[int] = mapped_column(Integer, default=0)
    found_by: Mapped[str] = mapped_column(String(32), default="")

    was_posted: Mapped[int] = mapped_column(Integer, default=0)
    verdicts: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    run: Mapped[Run] = relationship(back_populates="findings")

    __table_args__ = (Index("ix_findings_repo_fp", "repo", "fingerprint"),)


class Suppression(Base):
    """A finding a human rejected. Never post its fingerprint again for this repo.

    This is the durable half of "learn from merged PRs": co-change updates itself
    from git history, but knowing a human said "not a bug" has to be stored.
    """

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    fingerprint: Mapped[str] = mapped_column(String(32))

    reason: Mapped[str] = mapped_column(String(32), default="")  # resolved | thumbs_down | manual
    claim: Mapped[str] = mapped_column(Text, default="")
    file: Mapped[str] = mapped_column(String(512), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("repo", "fingerprint", name="uq_suppression"),)


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")


# --- GitHub App -------------------------------------------------------------
#
# Four small tables, each earning its place in a specific failure mode.
# Everything below is written only by `cr app serve`; the CLI and the Action
# never touch it.


class Delivery(Base):
    """One row per webhook delivery GitHub has handed us.

    GitHub redelivers on timeout and on manual replay, and a redelivered
    `synchronize` is indistinguishable from a real new push. Without this table
    a flaky network turns into a duplicate paid review.
    """

    __tablename__ = "deliveries"

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(48), default="")
    action: Mapped[str] = mapped_column(String(48), default="")
    repo: Mapped[str] = mapped_column(String(255), default="", index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class Installation(Base):
    """An account that installed the App, and the repos it granted."""

    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    account: Mapped[str] = mapped_column(String(255), default="", index=True)
    account_type: Mapped[str] = mapped_column(String(32), default="")
    repos: Mapped[list] = mapped_column(JSON, default=list)
    # Selection is "all" or "selected". With "all", `repos` is only what we have
    # seen so far, never the authoritative list.
    repo_selection: Mapped[str] = mapped_column(String(16), default="selected")
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Account(Base):
    """A GitHub account the App is allowed to review for.

    This is the gate that makes the App safe to publish. Webhooks arrive per
    installation with no human in the loop, so an open App means any stranger
    can point their PR volume at our model budget. Nothing is reviewed for an
    account that is not `approved` here.

    Keyed by login rather than installation id on purpose: someone who is
    removed and reinstalls gets a new installation id but the same login, and
    an approval — or a denial — should survive that.
    """

    __tablename__ = "accounts"

    login: Mapped[str] = mapped_column(String(255), primary_key=True)
    # "pending" (asked, not yet decided), "approved", "denied".
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    account_type: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    # Who decided, for an audit trail that survives the admin leaving.
    decided_by: Mapped[str] = mapped_column(String(255), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(255), default="")
    # Counts events dropped while not approved, so the dashboard can show that
    # someone is knocking rather than leaving them silently ignored.
    blocked_events: Mapped[int] = mapped_column(Integer, default=0)
    last_blocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class User(Base):
    """A human who signed in with GitHub.

    Sign-in exists so someone can request access for their account and see
    their own runs. It is not what authorises a review — `Account` is.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)  # GitHub user id
    login: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    avatar_url: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # GitHub organisations the user belonged to at their last sign-in. This is
    # what lets someone manage their org's API keys and tiers: membership is
    # re-read on every sign-in, so leaving the org revokes it within one session.
    orgs: Mapped[list] = mapped_column(JSON, default=list)
    # The subset of `orgs` the user administers on GitHub. Members may view an
    # org's workspace; only its admins may change its keys, tiers and routing.
    admin_orgs: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class UserSession(Base):
    """A signed-in browser session.

    Server-side rather than a self-contained cookie so that revoking access is
    immediate: deleting the row logs the browser out on its next request, which
    a stateless JWT cannot do without a denylist that is this table anyway.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # random token
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PRState(Base):
    """What we last reviewed on a PR, so a push can be reviewed incrementally.

    `last_reviewed_sha` is the whole point: on `synchronize` we diff that
    against the new head and review only what the author actually changed,
    instead of paying for the entire PR again on every commit.
    """

    __tablename__ = "pr_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    pr_number: Mapped[int] = mapped_column(Integer)
    installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_reviewed_sha: Mapped[str] = mapped_column(String(64), default="")
    base_sha: Mapped[str] = mapped_column(String(64), default="")
    reviews: Mapped[int] = mapped_column(Integer, default=0)
    comments_posted: Mapped[int] = mapped_column(Integer, default=0)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("repo", "pr_number", name="uq_pr_state"),)


class Job(Base):
    """A queued unit of work.

    The queue is in-process, but the *record* is durable: a webhook answered
    202 and then lost to a restart is a review that silently never happens, and
    GitHub will not redeliver a 202. On start-up, `pending_jobs()` replays
    anything still unfinished.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # Singleflight identity, e.g. "review:acme/api#42". A newer job with the
    # same key supersedes an older one still in flight.
    key: Mapped[str] = mapped_column(String(320), index=True)
    installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repo: Mapped[str] = mapped_column(String(255), default="", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # queued | running | done | failed | superseded | skipped
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    # Earliest time this may start. Debounce writes a future value here.
    run_after: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProviderConnection(Base):
    """A customer's own provider account: one API key and the models they use
    through it (bring-your-own-key).

    The key is stored only as ciphertext (see `cr.vault`); `hint` is its last
    four characters, which is all the dashboard ever shows. `models` is the
    list the customer entered — catalog names or their own deployment names —
    each with what its connection test learned:

        [{"name": "gpt-6-luna", "effort": true, "tested_at": "..."}]

    An account may hold several connections to one provider, e.g. two Azure
    resources.
    """

    __tablename__ = "provider_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(64), default="")
    ciphertext: Mapped[str] = mapped_column(Text)
    hint: Mapped[str] = mapped_column(String(16), default="")
    # Azure only: the resource name the fixed URL template is built from.
    resource: Mapped[str] = mapped_column(String(64), default="")
    models: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CustomTier(Base):
    """A review configuration an account built for itself: which model and
    effort each agent uses, which lenses run, how many comments to post.
    `config` is a validated `cr.app.workspace.TierSpec`."""

    __tablename__ = "custom_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (UniqueConstraint("account", "name", name="uq_custom_tier"),)


class RoutingRules(Base):
    """Where an account forces its own tiers. A repository rule beats the
    account-wide one; with neither, the repository runs CR's managed presets.

        {"all": <tier id> | null, "repos": {"owner/name": <tier id>}}
    """

    __tablename__ = "routing_rules"

    account: Mapped[str] = mapped_column(String(255), primary_key=True)
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class KeyTest(Base):
    """One "test connection" probe, kept so the hourly limit holds across
    replicas and restarts rather than living in one process's memory."""

    __tablename__ = "key_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class RepoSettings(Base):
    """Per-repository review settings the owner writes, e.g. guidelines that go
    into every review's (cached) system prompt for that repository."""

    __tablename__ = "repo_settings"

    repo: Mapped[str] = mapped_column(String(255), primary_key=True)
    account: Mapped[str] = mapped_column(String(255), index=True)
    guidelines: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
