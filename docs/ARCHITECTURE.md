# Production PR Review Agent — Architecture & Build Plan

> Status: design locked, pre-implementation.
> Research base: CodeRabbit pipeline teardown, PR-Agent (Qodo) source read at `pr_agent/algo/*`,
> model/pricing data as of 2026-09.

---

## 1. What we are actually competing against

### 1.1 How CodeRabbit works (verified)

It is **not** an agentic loop around a diff. It is a 5-stage pipeline where most of the
engineering is context preparation *before* any frontier model is called:

| Stage | What happens |
|---|---|
| 1. Webhook + queue | Cloud Run → Cloud Tasks. ~10 reviews/sec, 200+ instances. Queue absorbs burst. |
| 2. Sandbox | Throwaway microVM per review, 8 vCPU / 32 GB, repo held in RAM, 1h timeout. Double isolation (microVM + Jailkit) because external-contributor code is untrusted. 20–50 linters/SAST tools run here in parallel. |
| 3. Context build | 10–15 data points assembled: diff, **live code graph** (built fresh per review, not pre-indexed embeddings), Jira/Linear tickets, CI logs, lint output, team's past review preferences. Cheap models compress a 4,000-line file down to the few functions the change touches. |
| 4. Agentic review | A planner splits the review into a task graph. The agent then investigates by **writing shell commands** (`cat`, `grep`, `ast-grep`, `gh`) rather than calling predefined tools — no tool schemas, no MCP. Recursion is depth-capped. |
| 5. Judge + post | A *separate verification model* scores each finding against the gathered evidence and drops unprovable ones. Then formats: walkthrough, inline comments, one-click fixes. |

Across one review it runs **7–8 models** with hidden routing — cheap models for compression,
frontier models for investigation (~5x the cost of mid-tier), a judge for the gate. That
hidden ensemble is what lets them sell flat per-seat pricing. Latency is deliberately 1–5
minutes. Secondary memory is a LanceDB vector store over past reviews.

### 1.2 Where it actually loses

This is our wedge. An independent audit of 28 PRs / 32,784 lines found:

- **35%** genuine quality improvements
- **21%** nitpicking
- **15%** useless noise
- remainder: thoughtful-but-wrong assumptions

The three recurring reasons teams churn off it in 2026: **comment noise**, **diff-only
context** (it reviews the diff, not the repo), and **per-seat pricing that scales with
headcount rather than with value delivered**.

> **Thesis: the market does not need higher recall. It needs higher precision.**
> A tool that posts 3 comments and is right 9 times out of 10 beats one that posts 20
> comments and is right 1 in 3 — even though the second finds more bugs. We optimise one
> north-star metric: **actioned-comment rate** (comments that produce a code change or an
> explicit "good catch"), not bugs found.

### 1.3 Why this is different from "Claude with a code-review skill"

Claude Code's `/code-review` skill is a *single agent, single context, interactive, local*.
It is excellent, and it is not a product. What a production CR tool must own on top:

| Concern | Claude skill | What a product must add |
|---|---|---|
| Trigger | You type it | Webhook, queue, retry, idempotency, concurrency limits |
| Context | Your working tree | Clone + sandbox + code graph + CI logs + tickets, for a repo it has never seen |
| Untrusted code | Your machine, your trust | Fork PRs execute attacker-controlled code → microVM isolation is mandatory |
| State | None | Incremental review across force-pushes, comment dedup, resolved/reopened tracking |
| Learning | None | Per-repo suppression memory from 👎 and resolved threads |
| Cost | Your subscription | Unit economics that must hold at 10k reviews/day |
| Determinism | N/A | The same PR must not produce wildly different comments on re-run |

So: we reuse the *reasoning quality* and build everything around it.

### 1.4 What PR-Agent already solved (steal these — MIT licence)

Read directly from source. These are the non-obvious, hard-won parts:

- **Asymmetric + dynamic context** (`algo/git_patch_processing.py`). Diff hunks get *more*
  context before the change than after, and the window expands until it hits the enclosing
  function/class rather than a fixed ±3 lines. Capped so it cannot run away.
- **Compression strategy** (`algo/pr_processing.py`, 915 LOC). Sort files by the repo's
  dominant languages → within a language sort by token count descending → pack until a token
  buffer → overflow files become a bare `other modified files` list → deletions collapse into
  a single `deleted files` list. Deletion-only hunks are dropped entirely.
- **Self-reflection** (`tools/pr_code_suggestions.py`). Findings are fed *back* to the model
  in a second call, scored 0–10 with a rationale, re-ranked, and anything scoring 0 is
  dropped. Crucially it scores them **all at once**, so the model has comparative context.
- **Cross-run dedup** (`algo/inline_comment_dedup.py`). Two SHA-256 fingerprints per comment
  — one over (file, line, normalised prose), one over (file, line, normalised suggestion
  code) — matched with **OR** semantics, embedded as an HTML-comment marker in the posted
  body. Catches both "same prose, new code" and "same code, new prose" restatements, which
  are the two ways an LLM restates a finding across runs.
- **Finding state machine** (`algo/review_finding_state.py`). ACTIVE/RESOLVED persisted in a
  versioned marker on the review comment; reconciles across runs and can reopen.
- **Model routing** (`algo/model_routing.py`). Routes on **hunk count and file count** taken
  from the provider diff — deliberately *not* token count, so routing never depends on which
  model's tokenizer you would have used.
- **Token budget with fallback** (`algo/token_budget.py`). A `FallbackEligibleError` that
  distinguishes "this model cannot fit it" from "this request is broken", so only the former
  falls through to the next model.

Do not re-derive these. Port them.

---

## 2. Our differentiation — five decisions

### D1. The deterministic layer runs first, and *subtracts*

Linters, type-checkers, and SAST run before any model call. Their output is injected into
context **and** used as a suppression list: the model is forbidden from commenting on any
class of issue a configured linter already covers. This deletes the entire "nitpick" bucket
(21% of CodeRabbit's output) at zero token cost. If `ruff` or `eslint` catches it, we do not
spend a frontier token on it.

### D2. Every finding carries a proof obligation

A finding is not "this looks wrong". It is a structured object:

```jsonc
{
  "claim": "…",
  "failure_scenario": "concrete inputs/state → wrong output or crash",
  "evidence": [{ "file": "…", "lines": [40, 52], "why": "…" }],
  "confidence": 0.0,
  "category": "correctness|security|perf|…"
}
```

A finding with no reachable `failure_scenario` is dropped before the verifier even sees it.

### D3. Adversarial verification, not self-scoring

PR-Agent scores its own suggestions; CodeRabbit runs a judge. We go one step further: the
verifier is prompted to **refute**, runs in a *fresh context* with only the evidence the
finder cited plus the ability to re-read the repo, and defaults to `refuted: true` under
uncertainty. For high-severity findings, three verifiers with **distinct lenses**
(correctness / security / does-it-actually-reproduce) vote; majority-refute kills it.

We would rather burn 3x on verification and post 3 comments than post 20.

### D4. Cached-prefix fan-out — the cost architecture

The expensive thing is not the model; it is re-sending 25K tokens of context to every
specialist. So we build the context payload **once**, write it to the prompt cache, and fan
out N specialist passes that all read the same prefix at 0.1x price.

| | 5 specialist passes over a 25K-token prefix |
|---|---|
| Naive | 5 × 25K × $3/M = **$0.375** |
| Cached prefix | write 25K×1.25×$3/M = $0.094, then 5 reads × 25K×0.1×$3/M = $0.038 → **$0.13** |

**~65% saving**, and it compounds: follow-up commits on the same PR, `/ask` chat replies, and
the verifier passes all reuse the same cached prefix. This is the single highest-leverage cost
decision in the system.

Cache-correctness rules (absolute — one byte breaks everything after it):

- Render order is `tools` → `system` → `messages`. Stable content must physically precede volatile content.
- **No timestamps, UUIDs, or per-request IDs anywhere in the prefix.**
- Tools serialised deterministically (sort by name). Never change the tool set mid-conversation.
- JSON dumped with sorted keys.
- Assert on `usage.cache_read_input_tokens` in CI — if it is 0 across repeated runs, a silent invalidator shipped.

Minimum cacheable prefix is model-dependent and **not monotonic**: Opus 5 = 512 tokens,
Sonnet 5 = 1024, Haiku 4.5 = 4096. A 3K-token prefix caches on Sonnet 5 and silently does not
on Haiku 4.5 — no error, just `cache_creation_input_tokens: 0`.

### D5. Suppression memory keyed by fingerprint

When a human resolves a thread, 👎s a comment, or replies "not a bug", store the finding's
fingerprint plus a compact rationale, scoped to the repo. Future findings matching that
fingerprint are suppressed pre-post. Cheap (Postgres + pgvector), measurable, and it directly
attacks the "it keeps telling me the same wrong thing" complaint.

---

## 3. Model strategy

### 3.1 Pricing reality (per 1M tokens)

| Model | Context | Input | Output | Cache read | Notes |
|---|---:|---:|---:|---:|---|
| Claude Opus 5 | 1M | $5.00 | $25.00 | $0.50 | Thinking on by default; full `low…max` effort ladder |
| Claude Sonnet 5 | 1M | $3.00 | $15.00 | $0.30 | Near-Opus on coding/agentic work; `xhigh` effort available |
| Claude Haiku 4.5 | 200K | $1.00 | $5.00 | $0.10 | Compression, classification, triage only |

Cache **writes** cost 1.25x (5-min TTL) or 2x (1-hour TTL). Break-even is 2 reads at 5-min, 3
reads at 1-hour. The Batch API is **50% off** but async (up to 24h) — wrong for PR reviews,
right for nightly repo audits and code-graph summarisation.

### 3.2 Three-tier routing

Route on **hunk count × file count × blast radius** (PR-Agent's approach — tokenizer-independent):

| Tier | Trigger | Model | Effort | Finders / verifiers | Est. cost/review |
|---|---|---|---|---|---|
| **T0 — skip** | Lockfiles, generated files, pure renames, `.md`-only, vendored dirs | *none* | — | 0 / 0 | **$0.00** |
| **T1 — light** | ≤ 8 hunks, ≤ 3 files, no security-sensitive path | Haiku 4.5 finder → Sonnet 5 verifier | low | 1 / 1 | ~$0.02 |
| **T2 — standard** | The 80% case | Sonnet 5 finder fan-out → Sonnet 5 verifier | medium | 3 / 4 | ~$0.23 (measured: $0.93 on a dense real PR, down from $1.57 at `high`) |
| **T3 — deep** | Touches auth / payments / migrations / concurrency, or > 40 hunks | Sonnet 5 finders → **Opus 5** verifier + synthesis | high | 6 / 8 | ~$0.50 (was `xhigh`; lowered on the same reasoning as T2, not yet validated at T3) |

> Costs are computed in `PIPELINE.md` §2.1 with prompt caching applied. Note that **once
> caching is on, output tokens dominate** — input drops ~82% while output does not, so the
> next optimisation after caching is tighter structured output, not more caching.

T0 is free money — a meaningful share of real-world PRs are dependency bumps and generated
files. CodeRabbit charges per seat and reviews them anyway.

### 3.3 Which models to pick — the honest three-way

Ranked for *this* workload:

1. **Claude Sonnet 5 as the workhorse, Opus 5 as the escalation.** Recommended. Sonnet 5
   reaches what was Opus-tier quality on coding and agentic work at $3/$15, has the full
   effort ladder including `xhigh`, a 1M context window, and a 1024-token cache minimum that
   makes D4 work. Opus 5 only where blast radius justifies it.
   **Critical for code review specifically:** current models follow "only report high-severity
   issues" instructions *literally*, which depresses measured recall even though bug-finding
   improved. Prompt the finder for **coverage** (report everything, with confidence and
   severity attached) and do all filtering in the verifier stage. Never ask a single pass to
   both find and self-censor.
2. **Haiku 4.5 for everything that is not judgement** — context compression, file triage,
   language classification, commit-message summarisation, the whole T1 tier. At $1/$5 this is
   where CodeRabbit's "cheap models compress before the frontier model sees it" trick lives.
3. **Keep a provider abstraction, but do not start multi-provider.** Define `LLMProvider` as
   an interface on day one (costs you one file) so an enterprise customer can point at
   Bedrock/Vertex or their own gateway. But ship v1 single-provider — prompt-caching
   semantics, effort levels, and thinking configuration differ enough between vendors that a
   premature abstraction will cost you the 65% cache saving. If a customer demands their own
   cloud, Claude runs on Bedrock and Vertex; note Bedrock model IDs take an `anthropic.`
   prefix and several features (Batch API, Files API, web search) are unavailable there.

### 3.4 Effort tuning (non-obvious, costs real money if wrong)

- `max` overthinks and shows diminishing returns. `xhigh` was tried for T3 and dropped — no
  tier uses it now (see below).
- `low` and `medium` are unusually strong on the current generation — this used to be a
  hypothesis, now it's measured: T2 at `medium` cost 41% less and had *better* recall than
  `high` on the same real PR. Sweep before assuming a tier needs more than `medium`.
- **Do not lower effort to shorten comments** — it does not reliably work. Control verbosity
  with an explicit conciseness instruction in the prompt instead.
- **Delete any "double-check your work / verify before responding" instruction.** Current
  models self-verify unprompted; telling them to do it causes over-verification and burns
  tokens for no quality gain. Our verification is a separate stage, deliberately.
- At `xhigh`, set `max_tokens` ≥ 64K or output truncates mid-finding.

---

## 4. System architecture

```
GitHub ──webhook──> Ingress (verify HMAC, dedup by delivery-id, 202 immediately)
                         │
                         ▼
                    Redis / BullMQ  ── per-repo concurrency=1, global rate limit
                         │
                         ▼
          ┌──────── Review Worker ────────────────────────────────┐
          │                                                       │
          │  1. TRIAGE         route tier; T0 short-circuits here  │
          │  2. SANDBOX        clone into isolated microVM         │
          │  3. DETERMINISTIC  linters / SAST / tsc in parallel    │
          │  4. GRAPH          tree-sitter symbol index + co-change│
          │  5. CONTEXT        compress → build cacheable prefix   │
          │  6. FIND           N specialists, cached-prefix fan-out│
          │  7. VERIFY         adversarial refutation + voting     │
          │  8. GATE           suppression, dedup, comment budget  │
          │  9. POST           inline comments + walkthrough       │
          │                                                       │
          └───────────────────────────────────────────────────────┘
                         │
              Postgres (+pgvector) ── findings, state, learnings, metrics
                Object store       ── graph cache, run artifacts, traces
```

### 4.1 Stage notes

**1. Triage.** Cheapest stage, highest ROI. Classify from metadata alone — file globs,
hunk/file counts, path sensitivity. T0 exits with a single "no review needed" status check
and zero token spend. Never let a lockfile reach stage 6.

**2. Sandbox.** Non-negotiable for fork PRs: that is attacker-controlled code and we run
linters and build tooling over it. Start with **gVisor-hardened containers** (pragmatic,
~1 week), migrate to **Firecracker microVMs** once you have paying customers (CodeRabbit's
choice, ~4 weeks). Rules: no network egress by default, read-only mount except a scratch dir,
hard 10-minute timeout, and **no credentials inside the sandbox** — the GitHub token stays in
the worker and all git operations proxy through it.

**3. Deterministic.** Language-detected toolchain: `ruff`+`mypy`, `eslint`+`tsc`,
`golangci-lint`, `clippy`, plus `semgrep` for SAST across all of them. Runs in parallel with
stage 4. Output feeds context *and* the suppression list (D1).

**4. Graph.** `tree-sitter` symbol index (definitions, references, imports), built
**incrementally and cached per repo commit**, not rebuilt per review. Plus `git log --name-only`
co-change analysis to find files that historically change together with the touched ones —
this is what catches "you changed this signature, these three unrelated callers break".
Persist to object store keyed by merge-base SHA; a warm repo skips this stage almost entirely.

**5. Context.** Port PR-Agent's compression and asymmetric dynamic context wholesale. Output
is a byte-stable prefix: `[system][coding guidelines][repo conventions][graph slice][lint
output][full diff]`, with the cache breakpoint on the last block. Volatile content (the
specific question for this pass) goes *after* the breakpoint.

**6. Find.** Specialists run concurrently against the cached prefix, each a different lens:
correctness · security · concurrency/race · API-contract & breaking-change · test-coverage ·
performance. Each returns structured findings (D2). Prompted for **coverage, not filtering**.

**7. Verify.** Per finding, a fresh-context refuter with repo read access. High-severity →
three distinct lenses, majority vote. This stage is where the product's reputation is made.

**8. Gate.** Apply suppression memory → cross-run fingerprint dedup (PR-Agent's OR-semantics
double fingerprint) → reconcile ACTIVE/RESOLVED state → enforce a **hard comment budget**
(default: 6 inline comments; anything past the top 6 by confidence × severity goes into a
collapsed "also considered" section). The budget is a feature, not a limitation.

**9. Post.** Inline comments anchored to diff lines, plus one summary comment carrying the
walkthrough, the state marker, and the audit trail (which models ran, what they cost, how long
— transparency is itself a differentiator).

### 4.2 Incremental review

On force-push or new commits: diff against the **last reviewed SHA**, not the base. Re-run
only findings whose evidence files changed; everything else keeps its existing state. This
makes the 5th push on a PR cost roughly 15% of the first one.

---

## 5. Tech stack — decisions

| Layer | Choice | Why |
|---|---|---|
| Language | **TypeScript, Node 22** | Best-in-class GitHub App tooling (Octokit, `@octokit/webhooks`; App auth / JWT / installation tokens are solved). Same language as the VS Code extension, so the core engine is shared rather than reimplemented. Strong typing on the finding schema matters when it crosses nine stages. |
| Monorepo | pnpm workspaces + Turborepo | `core` shared by app/CLI/extension without publishing. |
| API / webhook | Fastify | Raw-body access for HMAC verification without ceremony; fast. |
| Queue | BullMQ on Redis | Per-repo concurrency keys, retries with backoff, delayed jobs, dead-letter queue — all built in. |
| DB | Postgres 16 + pgvector | Findings, state, learnings, metrics, and the semantic suppression index in one place. Do not add a separate vector DB until there is a reason. |
| Parsing | tree-sitter (`web-tree-sitter`) | One grammar set across ~40 languages; no per-language parser zoo. |
| Sandbox | gVisor containers → Firecracker | See 4.1. |
| LLM | Anthropic SDK behind a thin `LLMProvider` interface | See 3.3. |
| Observability | OpenTelemetry + a `runs` table | Every stage traced with tokens, cost, latency, cache-hit rate. **You cannot tune what you do not measure — this is not optional infrastructure.** |
| Local surfaces | CLI (`cr review`) + VS Code extension | Both import `@cr/core` and run the same engine against a local `git diff`. This is your dev loop — never iterate on prompts by pushing to GitHub. |

### 5.1 Repo layout

```
packages/
  core/          # the review engine — stages 1..8, provider-agnostic, no GitHub I/O
    triage/  sandbox/  analyzers/  graph/  context/  find/  verify/  gate/
  providers/     # git host adapters: github (v1), gitlab, bitbucket
  llm/           # LLMProvider interface + anthropic impl + cache-prefix builder
  store/         # Postgres schema, migrations, repositories
apps/
  webhook/       # Fastify ingress → queue
  worker/        # BullMQ consumer → core
  cli/           # local review against working tree — the prompt dev loop
  vscode/        # extension, thin wrapper over cli
  dashboard/     # metrics: actioned-rate, cost/review, p95 latency
evals/
  fixtures/      # ~200 real PRs with human-labelled ground truth
  runner.ts      # precision/recall/cost harness — gates every prompt change
```

### 5.2 Evaluation harness — build this *second*, not last

Before writing the review prompts, build `evals/`. Collect 150–200 real merged PRs where you
know what the bugs were — mine them from revert commits and hotfixes, which are labelled
ground truth for free. Every prompt or routing change must report:

- **precision** (actioned / posted) — the number that matters
- **recall** on known-bug PRs
- **cost per review** (p50 / p95)
- **cache hit rate**
- **latency** p95

Without this you are tuning prompts by vibes, which is how you end up shipping 15% noise.

---

## 6. Build order

| Phase | Weeks | Deliverable | Done when |
|---|---|---|---|
| **0. Spike** | 1 | CLI: `cr review` on a local diff → findings on stdout. Single Sonnet 5 call, no verification. | It says something non-obvious about your own repo. |
| **1. Eval harness** | 1 | `evals/` with 50 labelled PRs + the metrics table. | You can measure a prompt change. |
| **2. The gate** | 2 | Finding schema, adversarial verifier, comment budget, self-reflection. | Precision > 70% on the eval set. |
| **3. Context engine** | 2 | Port PR-Agent compression + dynamic context; tree-sitter graph; lint subtraction. | Recall climbs without precision dropping. |
| **4. GitHub App** | 2 | Webhook → queue → worker → inline comments. Dedup + state markers. | It reviews a real PR in your own repo end-to-end. |
| **5. Sandbox + scale** | 2 | gVisor isolation, per-repo concurrency, retries, idempotency. | A fork PR from an untrusted account is safe. |
| **6. Learning loop** | 1 | Suppression memory from 👎/resolved. Dashboard. | Repeat false positives stop recurring. |
| **7. Cost** | 1 | Tier routing, cached-prefix fan-out, T0 skips. | < $0.15 median cost/review at target precision. |
| **8. VS Code** | 1 | Extension over the CLI. | Pre-push review without a round-trip. |

Phases 0–2 are the whole product thesis. If precision has not cleared 70% by the end of phase
2, the answer is better verification, not more context.

---

## 7. Pricing implication

Per-seat pricing is CodeRabbit's third churn driver, and our T0/T1 routing makes marginal cost
genuinely low. Price **per reviewed PR** with a generous free tier for OSS, or per repo — it
aligns cost with value, undercuts seat-based pricing for large teams, and the T0 skip means a
team's dependabot traffic bills them nothing.

---

## 8. Open decisions

1. **Stack confirmation** — TypeScript is the recommendation (§5). Python is defensible if the
   team is Python-first and gets you PR-Agent's code as a direct starting point, at the cost of
   a weaker GitHub App ecosystem and a second language for the extension.
2. **SaaS-only vs self-hostable from day one** — self-hosting is a stated enterprise blocker
   for competitors and a real wedge, but it doubles the packaging surface. Recommendation: SaaS
   first, but design the worker so it *can* run in a customer VPC (no hard dependency on
   managed services); ship self-host at phase 6+.
3. **GitLab / Bitbucket** — the `providers/` seam exists from day one, but do not implement
   until a customer asks.
