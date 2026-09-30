# Execution & Caching Model

> Companion to `ARCHITECTURE.md`. This answers: what happens on one PR, what happens when
> three land at once on a big monorepo, exactly what is cached where, and which LLM
> capabilities we exploit.

---

## 1. One PR, end to end

Target: **p50 90 seconds, p95 3 minutes** from webhook to first comment on a warm repo.

| # | Stage | Cold repo | Warm repo | Notes |
|---|---|---:|---:|---|
| 0 | Webhook → 202 → queue | 50ms | 50ms | HMAC verify, dedup on `delivery_id`, never block |
| 1 | Triage | 200ms | 200ms | Metadata only. **T0 exits here** with a status check, $0 spent |
| 2 | Worktree into sandbox | 90s | **2s** | Warm mirror + `git worktree`. See §3.1 |
| 3 | Deterministic analysis | 40s | **6s** | Changed files only; base results cached |
| 4 | Graph slice | 120s | **3s** | Base graph cached; only the PR delta is parsed. See §3.2 |
| 5 | Context build + compress | 8s | 8s | Haiku passes, parallel |
| 6 | Find (fan-out) | 35s | 35s | N specialists over one cached prefix |
| 7 | Verify | 25s | 25s | Adversarial refutation, parallel |
| 8 | Gate | 300ms | 300ms | Suppression, dedup, budget |
| 9 | Post | 2s | 2s | Inline comments + walkthrough |

Stages 3 and 4 run **concurrently**. Stage 2 is the cold-start killer, which is why §3.1 is
the highest-value infrastructure work in the whole system.

---

## 2. The five cache layers

These are genuinely independent — different keys, different stores, different lifetimes.
Conflating them is the most common way this kind of system ends up slow *and* expensive.

| # | Layer | Key | Store | TTL | Saves |
|---|---|---|---|---|---|
| L1 | Repo mirror | `repo_id` | Host disk | Evict at 30d idle | 90s → 2s clone |
| L2 | Code graph | `repo_id + commit_sha` | Object store + Postgres index | LRU, ~50 SHAs/repo | 120s → 3s |
| L3a | Prompt prefix — repo | byte-exact `tools+system` | Anthropic, **1h TTL** | 1 hour | ~8K tok × every pass, every PR |
| L3b | Prompt prefix — PR | byte-exact `…+messages[0]` | Anthropic, **5m TTL** | 5 minutes | ~20K tok × every pass |
| L4 | Analysis results | `blob_sha + tool_ver + config_hash` | Postgres | Indefinite | Lint/SAST on unchanged files |
| L5 | Finding verdicts | `finding_fingerprint + blob_sha` | Postgres | Per PR lifetime | Re-verification on force-push |

### 2.1 L3 — the prompt-cache layout (the money maker)

The ordering below is not cosmetic. Render order is `tools` → `system` → `messages`, and
caching is a **prefix match from byte 0**, so anything volatile placed early destroys
everything after it.

```
tools:        [fixed superset, sorted by name]        ─┐
system:       [CR agent preamble]                      │  L3a — repo-level
              [repo coding guidelines]                 │  shared by EVERY pass
              [repo conventions + graph overview]      │  on EVERY PR in this repo
              ══════════ cache breakpoint 1 ═══════════╧  (1h TTL)

messages[0]:  [PR diff, compressed]                   ─┐
              [graph slice: callers, impls, co-change] │  L3b — PR-level
              [lint/SAST output]                       │  shared by every pass
              ══════════ cache breakpoint 2 ═══════════╧  on THIS PR (5m TTL)

messages[1]:  [role instruction: "you are the          ←  volatile, ~200 tok
               concurrency specialist…" | "refute
               this finding: …"]
```

**The key design win:** because the role instruction lives in `messages[1]` and not in
`system`, the **finder and verifier passes share the same two cache entries.** One prefix
serves all 14 calls in a T3 review.

> This does *not* violate D3's "fresh context" requirement. Fresh context means the verifier
> never sees the **finder's reasoning** — it still sees the same code. Independence comes
> from withholding the argument, not the evidence.

Cost for a **T3** review (6 finders + 8 verifiers = 14 passes over a 25K prefix):

| | Input cost |
|---|---:|
| Naive (resend prefix 14×) | 14 × 25K × $3/M = **$1.050** |
| Cached | write 25K×1.25×$3/M = $0.094 + 13 reads × 25K×0.1×$3/M = $0.098 = **$0.191** |
| | **82% saving** |

**Consequence worth internalising: once caching is on, output tokens dominate.** Input drops
to $0.19 while 14 passes of output run ~$0.17–0.31. So the *next* optimisation after caching
is not more caching — it is tighter output: structured findings instead of prose, terse
verifier verdicts (~300 tokens), and no "explain your reasoning" in the visible response.

Revised per-tier reality (this supersedes the estimate in `ARCHITECTURE.md` §3.2):

| Tier | Finders | Verifiers | Input | Output | **Total** |
|---|---:|---:|---:|---:|---:|
| T1 | 1 (Haiku) | 1 | $0.01 | $0.01 | **~$0.02** |
| T2 | 3 | 4 | $0.14 | $0.09 | **~$0.23** |
| T3 | 6 | 8 | $0.19 | $0.31 | **~$0.50** |

### 2.2 Two cache traps that silently cost you everything

**Trap 1 — concurrent writes can't read each other.** A cache entry is only readable once the
first response *begins streaming*. If you fire all 6 specialists simultaneously on a cold
prefix, all 6 pay full price and you write the cache 6 times. The 82% saving becomes 0%, with
no error and no warning.

> **Fan-out must be staggered:** fire specialist #1 → await its first streamed token → then
> fire #2–#6 in parallel. Costs ~1.5s of latency. Saves ~60% of the review's input cost.

**Trap 2 — silent invalidators.** Any of these in the prefix and `cache_read_input_tokens`
quietly goes to zero:

- a timestamp, PR number, run ID, or UUID anywhere in `system`
- `json.dumps()` without `sort_keys=True`, or iterating a `set`
- **a tool list that varies by repo language** — always send the same sorted superset and let
  the model ignore irrelevant tools. A varying subset fragments the cache per language
  combination, which is the worst of both worlds.
- the 20-block lookback: each breakpoint walks back at most 20 content blocks, so long
  investigation loops need an intermediate breakpoint every ~15 blocks

> **CI gate:** assert `usage.cache_read_input_tokens > 0` on a repeated-prefix test. This is
> the only way you find out before the bill does.

Also note the minimum cacheable prefix is model-dependent and **not monotonic**: Opus 5 = 512
tokens, Sonnet 5 = 1024, Haiku 4.5 = **4096**. A 3K-token prefix caches on Sonnet 5 and
silently does not on Haiku 4.5.

---

## 3. Big codebase: what breaks and how we fix it

### 3.1 L1 — warm mirror + worktree (never clone per review)

A 2 GB monorepo clone is 60–120s. Doing it per review is the single worst thing you can do.

```
Host (trusted)                          Sandbox (untrusted)
├── /mirrors/acme-monorepo.git          ┌──────────────────────┐
│     bare mirror, git fetch on push    │ overlayfs            │
│                                       │  lower: worktree(ro) │
├── git worktree add --detach <sha> ───►│  upper: writable     │
│                                       │                      │
└── GitHub token NEVER enters sandbox   └──────────────────────┘
```

- Mirror is fetched incrementally on every push webhook, so it is near-current at review time.
- Each review gets a `git worktree` at the head SHA — no history re-copy.
- The worktree is exposed to the sandbox through an **overlayfs**: read-only lower layer, a
  writable upper layer for build artifacts. The mirror is never writable from inside.
- **No credentials cross the boundary.** Git operations proxy through the host.

This matters because a fork PR is attacker-controlled code and we run linters and build
tooling over it.

### 3.2 L2 — persistent base graph + delta (not "fresh per review")

CodeRabbit builds a fresh code graph per review. On a 450K-file monorepo that is minutes of
work repeated for every PR. We do better without losing freshness:

```
Nightly:   full graph of `main` → object store, keyed by SHA        (Batch API territory)
Per push:  graph(main@new) = graph(main@old) + reparse(changed files)
Per PR:    graph(PR) = graph(merge_base) + reparse(PR's ~20 files)   ← 3 seconds
```

**When the merge-base isn't cached** (PR branched from an older commit), we do *not* rebuild.
We find the **nearest cached ancestor** and apply the commit delta forward:

```
cached: graph(main@abc)
PR2 merge-base: main@def, 40 commits later
→ reparse only files touched in abc..def, then apply PR2's delta
```

Freshness is exact, because the delta is exact. This is the difference between a tool that
works on a monorepo and one that times out on it.

**Graph contents** (kept deliberately small): symbol table (name → file:line:kind), reference
edges, import graph, and a co-change matrix from `git log --name-only` over ~6 months.

### 3.3 Context selection at scale

In a 450K-file repo, a 200-line diff could have relevant context anywhere. Blind retrieval
fails. We rank candidates by four signals, then compress:

| Signal | Source | Catches |
|---|---|---|
| 1-hop graph neighbours | symbol graph | callers of changed functions, implementers of changed interfaces |
| Co-change lift | `git log` history | coupling the AST can't see — config that must change with a schema |
| Covering tests | graph + path heuristics | which tests should have been updated |
| Ownership recency | CODEOWNERS + blame | who knows this code, what changed near it lately |

Top-K candidates then go through a **Haiku compression pass**: a 4,000-line file becomes the
three functions that actually call the changed symbol.

> **Hard context cap at ~25K tokens even though we have 1M.** Having a 1M window is not a
> reason to use it — needle-in-a-haystack degradation is real and measurable, and cost scales
> linearly. Treat 1M as headroom for the rare enormous PR, not as an invitation.

---

## 4. Three PRs, three branches, one monorepo

The interesting case. Assume PR1, PR2, PR3 webhooks arrive within 10 seconds on a repo whose
caches are cold.

### 4.1 The stampede, and why naive concurrency is a disaster

Without protection, all three workers independently: clone the mirror (3 × 90s, 6 GB disk),
build the base graph (3 × 120s CPU), and cache-write the repo prompt prefix (3 × full price,
0 reads). Everything that should be shared is triplicated.

### 4.2 Singleflight on every shared resource

Each shared resource is guarded by a Redis lock keyed on the resource, not the job:

```
lock:mirror:{repo_id}            → PR1 fetches; PR2, PR3 wait ~2s, then reuse
lock:graph:{repo_id}:{base_sha}  → PR1 builds; PR2, PR3 wait, then read from object store
lock:prewarm:{repo_id}           → PR1 issues a `max_tokens: 0` request to write L3a;
                                    PR2, PR3 wait for it, then read the prefix at 0.1×
```

The `max_tokens: 0` pre-warm is worth calling out: it runs prefill and writes the cache
without generating any output, returning immediately with zero output tokens billed. One
cache write, then every PR in that repo for the next hour reads at 0.1×.

### 4.3 Timeline

```
t=0s    PR1, PR2, PR3 webhooks → queued
t=0.2s  All three triage. PR3 is a lockfile bump → T0, exits, $0.00.
t=1s    PR1 wins lock:mirror, fetches. PR2 waits.
t=3s    Mirror warm. Both create worktrees. PR1 wins lock:graph (shared merge-base).
t=6s    PR1 wins lock:prewarm → max_tokens:0 request writes L3a (8K tok, 1h TTL)
t=8s    L3a live. PR2 proceeds; every one of its passes now reads L3a at 0.1×.
t=45s   Both build their own L3b (per-PR, they differ). Staggered fan-out begins.
t=95s   PR1 posts 3 comments. PR2 posts 2.
```

Net effect: one mirror fetch, one graph build, one repo-prefix write — serving three PRs.
Sequential-looking, but the serialisation windows are 2–8 seconds, not minutes.

### 4.4 Concurrency limits

- **Per repo: 3–5 concurrent reviews.** Not 1 — PR3 must not queue behind PR1. Bounded by
  sandbox memory, not by correctness, because singleflight already handles sharing.
- **Per installation: a global cap** so one customer can't saturate the worker pool.
- **Per branch: strictly 1**, enforced by an idempotency key of `(pr_id, head_sha)`.

### 4.5 Rapid pushes — debounce and cancel

Developers push three times in a minute. Reviewing SHA #1 while SHA #3 exists wastes money
*and* posts comments on lines that have moved.

- **Debounce**: hold the job 20–30s; a newer push within the window replaces it.
- **Cancel superseded runs**: when a new head SHA arrives for a PR with a review in flight,
  cancel it at the next stage boundary. Partial spend is cheaper than a wrong review.
- **Incremental**: diff against the **last reviewed SHA**, not the base. Findings whose
  evidence files are byte-identical reuse their L5 verdict. The 5th push costs ~15% of the 1st.

---

## 5. Using the LLM capabilities properly

The API features below are not optional polish — each removes a class of failure or a chunk
of cost. This is what "utilising the model well" concretely means here.

| Capability | Where we use it | Why it matters |
|---|---|---|
| **Structured outputs** (`output_config.format` + Pydantic) | The finding schema, every verifier verdict | The API *enforces* the schema. No regex-scraping JSON out of prose, no parse-retry loop. Kills an entire class of production bug. |
| **Prompt caching, 2 breakpoints** | §2.1 | 82% of input cost |
| **Effort ladder** | `low` T1 finding · `medium` T2 finding + all verification · `high` T3 finding | Biggest quality/cost dial. `medium` beat `high` on both cost and recall for T2 on a real PR — swept, not assumed |
| **Batch API (50% off)** | Nightly full-repo audits, base graph summarisation, eval backfill, repo-convention extraction at install | Async work should never pay sync prices |
| **Programmatic tool calling** | "Check all 40 callers of this function" | Model writes a script; the 40 file reads execute in the code sandbox and **never enter context**. Otherwise that's 40 round-trips at ~2K tokens each |
| **Advisor tool** (`advisor_20260301`) | Sonnet 5 finder consults Opus 5 mid-turn on a hard call | Server-side, no round-trip. Cheap executor, expensive advisor **only when stuck** — instead of running the whole pass on Opus |
| **Context editing** (`clear_tool_uses`) | Long investigation loops | Old tool results get cleared so context doesn't bloat mid-review |
| **Token counting** (`count_tokens`) | Before every dispatch | Budget math is exact, not estimated. Never use `tiktoken` — it undercounts Claude by 15–20% |
| **Parallel tool use** | Investigation stage | All tool results go back in **one** user message. Splitting them across messages silently trains the model to stop parallelising |
| **Adaptive thinking, left on** | All finder/verifier passes | Control depth with `effort`, never by disabling. On Opus 5, disabling thinking makes tool calls leak into visible text as plain prose — the call silently never runs |

### 5.1 The three prompt rules that matter most

1. **Prompt finders for coverage, never for filtering.** Current models follow "only report
   high-severity issues" *literally* — they investigate just as hard, find the bug, then
   decline to report it. Measured recall collapses while the model got better. Ask for
   everything with confidence + severity attached; filter in the verifier.
2. **Delete every "double-check your work" instruction.** These models self-verify unprompted.
   Telling them to do it causes over-verification and burns tokens for zero quality gain. Our
   verification is a separate stage on purpose.
3. **Never disable thinking to save money.** Use `effort: low`. Disabling it on Opus 5 causes
   two silent failure modes: tool calls emitted as plain text (the call never runs, no error),
   and `<thinking>` tags leaking into output.

---

## 6. What we reuse from PR-Agent

**Licence: MIT** (not Apache-2.0 as stated in an earlier draft) — permissive, attribution only.

Approach: **vendor the algorithms, do not fork the project.** Forking means inheriting an
architecture built for a different thesis — no sandbox, no code graph, no verification stage,
no state store — and doing surgery on someone else's abstractions forever.

### Take (~3,300 LOC of hard-won algorithm)

| File | LOC | What it gives us |
|---|---:|---|
| `algo/git_patch_processing.py` | 535 | Asymmetric/dynamic context expansion, hunk parsing. Handles `\ No newline at end of file`, CRLF, and Unicode line separators (`\x1c\x1d\x1e\x85  `) — every one is a bug found the hard way |
| `algo/token_budget.py` | 658 | Budget accounting + `FallbackEligibleError` (distinguishes "model can't fit it" from "request is broken") |
| `algo/review_finding_state.py` | 371 | ACTIVE/RESOLVED marker reconciliation across runs |
| `algo/inline_comment_dedup.py` | 326 | Dual SHA-256 fingerprint with OR-semantics — take near-verbatim |
| `algo/language_handler.py` | 135 | Repo language prioritisation for packing order |
| `algo/file_filter.py` | 94 | Glob filtering |
| `algo/model_routing.py` | 80 | Hunk/file-count routing (tokenizer-independent by design) |

### Adapt (structure is right, our thesis differs)

| File | LOC | Change |
|---|---:|---|
| `algo/pr_processing.py` | 915 | Compression/packing core is excellent; rewire to our budget model and cache-prefix builder |
| `servers/github_app.py` | 615 | Webhook dispatch skeleton — PR-opened / comments / review-submitted / push handlers, HMAC verification. Re-target to our queue |
| `git_providers/github_provider.py` | 2,163 | We want ~400 LOC of it — principally `find_line_number_of_relevant_line_in_file`, which solves **diff-position anchoring**. GitHub committable comments are anchored by offset into the diff hunk, *not* file line number. This is the fiddliest thing in the entire GitHub API surface |

### Do not take

Their config system (dynaconf/TOML sprawl), **LiteLLM** (we need native-SDK control over cache
breakpoints, effort, and thinking — LiteLLM abstracts away exactly the things we're
optimising), their tool/prompt structure, and their agent loop.

**Net: ~5,000 LOC we don't write**, and more importantly a set of edge cases we don't
rediscover in production.

---

## 7. What to measure

Nothing above is tunable without these. Every stage emits to a `runs` table.

| Metric | Target | Why |
|---|---|---|
| **Actioned-comment rate** | > 60% | The north star. Everything else is a means to it |
| Comments per PR | ≤ 4 median | Noise is the competitor's failure mode; it must not be ours |
| `cache_read_input_tokens` ratio | > 70% of input | If this drops, a silent invalidator shipped |
| Cost per review, p50 / p95 | < $0.15 / < $0.80 | Unit economics at 10k reviews/day |
| Time to first comment, p95 | < 3 min | |
| L1/L2 hit rate | > 95% | Cold-start rate on active repos |
| Verifier kill rate | 40–60% | Below 40% the verifier is rubber-stamping; above 60% the finders are too noisy |

That last one is subtle and worth watching from day one — it tells you *which half of the
pipeline* to fix when precision drops.
