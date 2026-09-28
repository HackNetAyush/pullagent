# Implementation Backlog

> Every item names the library that already solves it. Items are IDs so they can
> become issues directly. Versions verified on PyPI 2026-09-28.

---

## 0. The build-vs-buy rule

We write code for exactly three things: **the finding schema, the prompts, and the
gate.** That is the product. Everything else — diff parsing, graph building, queues,
sandboxes, GitHub plumbing, tracing — is someone else's solved problem and we take it.

Where this bites immediately: **`src/cr/diff.py` is currently a hand-rolled regex
unified-diff parser I wrote in Phase 0.** That was wrong by this rule. `unidiff`
(1.0.1) is mature and handles renames, mode changes, binary markers, and
`\ No newline at end of file`. CR-03 replaces it.

---

## 1. The agent-framework decision

The most consequential "don't reinvent" call, so here it is explicitly.

**We use two different LLM surfaces for two genuinely different jobs:**

| Job | Surface | Why |
|---|---|---|
| **Finder fan-out and verification** (stages 6–7) | Native `anthropic` SDK + our `PrefixBuilder` | These are single-shot structured calls over a *byte-identical cached prefix*. There is no loop to orchestrate. Any framework that owns request construction takes away `cache_control` placement, and that is the 82%. |
| **Deep investigation** (stage 6, Phase 3+) | **`claude-agent-sdk`** (0.2.160) | Here the reviewer genuinely needs to explore: read the callers, grep for usages, run a test. That *is* an agent loop, and the SDK ships the whole harness — Read/Grep/Glob/Bash, context compaction, subagents, permission hooks, sessions. This is the part CodeRabbit hand-rolled with generated shell commands. |

The trade is deliberate: inside the Agent SDK we give up fine-grained cache control,
but investigation is inherently variable-context so caching would not have helped
there anyway. Caching matters exactly where we keep control of it.

**Permission hooks are the reason this is safe.** The Agent SDK lets us gate every
Bash call before execution — which is how untrusted fork code gets reviewed without
handing the model an unsandboxed shell (CR-12).

### Explicitly rejected

| Rejected | Why |
|---|---|
| **LangChain / LangGraph** | Orchestration we don't need — our fan-out is `asyncio.gather`. LangGraph's durable execution is real, but `arq` and Temporal do it better without coupling persistence to an LLM framework. |
| **CrewAI / AutoGen** | Model role-playing agents that converse. Our "specialists" are one prompt each over a shared prefix; they never talk to each other, and making them do so would cost tokens for nothing. |
| **LiteLLM / Pydantic AI / Instructor** | All three abstract over request construction, which means abstracting over `cache_control`, `effort`, and `thinking`. Structured outputs we already get natively via `messages.parse()`. This is the same trap PR-Agent fell into. |

---

## 2. Backlog

Legend: **P** = phase (from `ARCHITECTURE.md` §6) · effort in dev-days.

### Ingestion — the GitHub App

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-01 | Webhook ingress: HMAC verify, dedup on `delivery_id`, 202 immediately | `fastapi` + `githubkit` (0.16.1) — typed webhook models, App JWT → installation-token exchange, auto token refresh. **Not PyGithub** (sync, weak App auth) | 4 | 2 |
| CR-02 | Job queue with per-repo concurrency and singleflight locks | `arq` (0.28.0) — asyncio-native, Redis. Locks via `redis.lock` | 4 | 2 |
| CR-03 | **Replace the hand-rolled diff parser** | `unidiff` (1.0.1) + edge cases from `vendor/pr_agent/git_patch_processing.py` | 3 | 1 |
| CR-04 | Inline comment posting with diff-position anchoring | `githubkit` + port `find_line_number_of_relevant_line_in_file` from vendor. GitHub anchors committable comments by **offset into the hunk**, not file line | 4 | 2 |
| CR-05 | Debounce rapid pushes; cancel superseded runs | arq job IDs keyed `(pr_id, head_sha)` + `asyncio.CancelledError` at stage boundaries | 4 | 2 |
| CR-06 | Check Run API status reporting (in-progress / neutral / failure) | `githubkit` | 4 | 1 |
| CR-07 | `/ask`, `/review`, `/explain` comment commands | Reuse `servers/github_app.py` dispatch shape from vendor | 4 | 2 |

### Sandbox and untrusted code

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-08 | Warm bare mirror + `git worktree` per review | `pygit2` or plain subprocess. Mirror on host, never writable from sandbox | 5 | 3 |
| CR-09 | Container isolation with overlayfs | `docker` SDK + **gVisor** (`runsc`) runtime. No egress by default, 10-min hard timeout | 5 | 4 |
| CR-10 | **Escape hatch: managed sandboxes** | `e2b-code-interpreter` (2.10.0) or Modal — skips all of CR-08/09 for the first customers. Take this if sandbox infra is blocking revenue | 5 | 1 |
| CR-11 | Firecracker microVMs | Only once CR-09 is the bottleneck. CodeRabbit's choice | 7+ | 10 |
| CR-12 | Permission hooks on every Bash call in investigation | `claude-agent-sdk` `canUseTool` hook + allowlist. **Blocks CR-30** | 5 | 2 |
| CR-13 | Credential firewall — GitHub token never enters the sandbox | Git operations proxy through the host | 5 | 2 |

### Deterministic analysis (D1 — the layer that *subtracts*)

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-14 | **Normalise every tool to SARIF** — parse one format, not N | `sarif-tools` (3.0.5). Every modern linter emits SARIF. This is the single highest-leverage decision in this section | 3 | 2 |
| CR-15 | Language-detected toolchain runner | `ruff`, `biome`/`oxlint` (much faster than eslint), `golangci-lint`, `clippy`, `tsc` — all via subprocess, all SARIF out | 3 | 3 |
| CR-16 | SAST | `semgrep` (1.178.0) — multi-language, huge rule registry, SARIF native | 3 | 1 |
| CR-17 | Secret scanning | `gitleaks` or `trufflehog` (SARIF) | 3 | 1 |
| CR-18 | Dependency/CVE scan | `trivy` or `osv-scanner` | 6 | 1 |
| CR-19 | **Build the suppression list from SARIF rule IDs** and inject into `PRContext.suppressed_rules` | Already plumbed in `prefix.py` — just needs feeding | 3 | 1 |
| CR-20 | Cache results by `blob_sha + tool_version + config_hash` (L4) | Postgres | 3 | 1 |

### Code graph

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-21 | Symbol index: defs, refs, imports | **`tree-sitter-language-pack` (1.20.0)** — 371 pre-compiled grammars, zero build step. Do **not** manage per-language grammar wheels | 3 | 4 |
| CR-22 | Structural pattern queries | `ast-grep-py` (0.45.3) — the same tool CodeRabbit's agent shells out to | 3 | 2 |
| CR-23 | Persistent base graph + delta; nearest-cached-ancestor walk | Object store (S3/R2) keyed by SHA + Postgres index. See `PIPELINE.md` §3.2 | 3 | 5 |
| CR-24 | Co-change matrix from `git log --name-only` | `pygit2` + a lift calculation. Catches coupling the AST cannot see | 3 | 2 |
| CR-25 | Precise cross-file xrefs for supported languages | **SCIP** (`scip-python`, `scip-typescript`, `scip-java`) — Sourcegraph's index format. Strictly better than hand-rolled reference resolution where an indexer exists; tree-sitter remains the fallback | 6 | 5 |

### Context engine

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-26 | Wire vendored compression + asymmetric dynamic context | `vendor/pr_agent/{pr_processing,git_patch_processing}.py` — rewire imports onto `cr.*` | 3 | 3 |
| CR-27 | Candidate ranking: graph neighbours, co-change, covering tests, ownership | Ours. See `PIPELINE.md` §3.3 | 3 | 3 |
| CR-28 | Haiku compression pass — 4,000-line file → the 3 relevant functions | Existing `LLMClient`, `claude-haiku-4-5`, effort `low` | 3 | 2 |
| CR-29 | Exact token budgeting | Anthropic `count_tokens` API. **Never `tiktoken`** — it undercounts Claude by 15–20%, more on code | 3 | 1 |

### Review engine

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-30 | **Investigation stage** — agentic repo exploration between find and verify | `claude-agent-sdk` (0.2.160) with Read/Grep/Glob/Bash, sandbox-scoped, permission-hooked. Depends on CR-12 | 3 | 5 |
| CR-31 | Repo-level prefix pre-warm, singleflighted | `PrefixBuilder.warm_payload()` exists; needs the Redis lock and a scheduler | 7 | 1 |
| CR-32 | Batch API for async work — nightly audits, graph summarisation, eval backfill | `anthropic` Batches API, **50% off**. Never for PR reviews (latency) | 7 | 2 |
| CR-33 | Advisor tool — Sonnet finder consults Opus 5 mid-turn only when stuck | `advisor_20260301`, server-side, no round-trip. Cheaper than running the pass on Opus | 7 | 1 |
| CR-34 | Programmatic tool calling for wide sweeps ("check all 40 callers") | `code_execution_20260120` + `allowed_callers`. Intermediate reads never enter context | 3 | 2 |
| CR-35 | Per-repo prompt overrides from `.cr.yaml` | `pydantic` + `ruamel.yaml`. Must stay inside the cached repo block | 6 | 2 |

### State and persistence

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-36 | Schema: runs, findings, verdicts, suppressions, repos, installations | `sqlalchemy` 2.0 async + `asyncpg` + `alembic` | 4 | 3 |
| CR-37 | Cross-run comment dedup | Port `vendor/pr_agent/inline_comment_dedup.py` — dual fingerprint, OR semantics | 4 | 1 |
| CR-38 | ACTIVE/RESOLVED finding state across force-pushes | Port `vendor/pr_agent/review_finding_state.py` | 4 | 2 |
| CR-39 | Incremental review — diff vs last reviewed SHA, reuse L5 verdicts | Ours | 4 | 3 |
| CR-40 | Suppression memory (D5) with semantic near-match | `pgvector` + Anthropic embeddings, keyed on fingerprint | 6 | 3 |

### Observability and evaluation

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-41 | **LLM tracing, prompt versioning, eval storage** | **`langfuse` (4.15.6)** — self-hostable, OSS, and covers tracing + prompt management + datasets in one. Alternative: `arize-phoenix` (20.16.0). Do not build a dashboard | 1 | 2 |
| CR-42 | App tracing and metrics | `opentelemetry-sdk` with GenAI semantic conventions | 4 | 2 |
| CR-43 | **Eval harness: precision, recall, cost, cache-hit, p95** | `pytest` + Langfuse datasets. Consider `inspect-ai` (0.3.271) if evals get rigorous | 1 | 4 |
| CR-44 | Mine labelled fixtures from revert/hotfix commits | `pygit2` — free ground truth. Target 150–200 PRs | 1 | 3 |
| CR-45 | **Record/replay LLM responses so CI is fast and deterministic** | `pytest-recording` (0.13.4) + `respx` (0.23.1). The `anthropic` SDK is httpx-based, so both work | 1 | 2 |
| CR-46 | Cache-health CI gate — assert `cache_read_input_tokens > 0` | pytest. Cheapest insurance in the repo | 1 | 0.5 |

### Reliability and enterprise readiness

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-47 | Retries and backoff for non-SDK calls | `stamina` (26.1.0). **Do not wrap the Anthropic SDK** — it already retries 429/5xx | 4 | 1 |
| CR-48 | Per-installation rate limiting and spend caps | `aiolimiter` (1.3.0) + Postgres spend ledger | 6 | 2 |
| CR-49 | Per-tenant data isolation and configurable retention | Postgres RLS; delete diffs after N days, keep only fingerprints | 6 | 3 |
| CR-50 | Secrets management for GitHub App private keys | AWS Secrets Manager / Vault. Never env vars in prod | 5 | 2 |
| CR-51 | Audit log — who reviewed what, which model, what it cost | Postgres append-only. Required for enterprise procurement | 6 | 2 |
| CR-52 | **Durable execution** if cancellation/compensation gets hairy | `temporalio` (1.33.0). Only if arq's semantics stop being enough — do not adopt preemptively | 7+ | 5 |
| CR-53 | Self-host packaging | Docker Compose then Helm. Worker must not hard-depend on managed services | 7+ | 4 |
| CR-54 | Dashboard: actioned rate, cost/review, kill rate, cache hit | Next.js or Streamlit for v1. Langfuse covers much of this already | 6 | 3 |

### Local surfaces

| ID | Item | Library / approach | P | Eff |
|---|---|---|---|---|
| CR-55 | CLI polish — `--json`, `--fix`, exit codes for CI | `typer` + `rich` (in place) | 2 | 1 |
| CR-56 | VS Code extension | TypeScript, shells out to `cr --json`. Ship via `uv tool install` or a bundled runtime | 8 | 5 |
| CR-57 | Pre-commit / pre-push hook | `pre-commit` | 8 | 0.5 |

---

## 3. Dependency additions

```toml
# Phase 1 — evals and observability (do this first)
langfuse>=4.15
pytest-recording>=0.13
respx>=0.23

# Phase 3 — context engine
unidiff>=1.0
tree-sitter-language-pack>=1.20
ast-grep-py>=0.45
pygit2>=1.15
claude-agent-sdk>=0.2

# Phase 4 — the GitHub App
githubkit>=0.16
arq>=0.28
sqlalchemy[asyncio]>=2.0
asyncpg>=0.30
alembic>=1.14
pgvector>=0.3
stamina>=26.1
aiolimiter>=1.3
opentelemetry-sdk>=1.29
```

External binaries (sandbox image, not Python deps): `semgrep`, `gitleaks`, `trivy`,
`ruff`, `biome`, `golangci-lint`, `runsc`.

---

## 4. Suggested order

Effort totals ≈ **140 dev-days**, but the ordering matters more than the total:

1. **CR-43, CR-44, CR-45, CR-46, CR-41** — the eval harness and tracing. *Everything*
   after this is measurable; nothing before it is. Roughly 11 days and it changes how
   every later decision gets made.
2. **CR-03, CR-14, CR-15, CR-19** — real diff parsing and the SARIF suppression loop.
   This is the cheapest precision win available: it deletes the nitpick class outright.
3. **CR-21, CR-23, CR-26, CR-27** — the context engine. Recall goes up without
   precision going down.
4. **CR-01 → CR-07, CR-36 → CR-39** — the GitHub App and state. Now it is a product.
5. **CR-08 → CR-13** — sandbox. Required before a single fork PR touches it.
6. Everything else, driven by what the metrics say is broken.

**Do not reorder 1 ahead of anything.** Shipping prompts before the eval harness is
exactly how a reviewer ends up at 15% noise and nobody notices for three months.
