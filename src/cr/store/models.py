"""Persistence schema.

SQLite by default so it runs with zero infrastructure; point CR_DATABASE_URL at
Postgres and nothing else changes. Everything here is keyed by finding
fingerprint, which is what makes suppression survive across runs and pushes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
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

SCHEMA_VERSION = 3


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    tier: Mapped[str] = mapped_column(String(8), default="")
    model: Mapped[str] = mapped_column(String(64), default="")

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

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_s: Mapped[float] = mapped_column(Float, default=0.0)

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
