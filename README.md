# CR — high-precision AI code review

An AI code reviewer that optimises for **precision, not recall**. Three comments
that are right beat twenty that are mostly noise.

---

## Ship it on your repo in 5 minutes

**1.** Add your provider key as a repo secret.

*Microsoft Foundry (Claude on Azure):* secret `AZURE_API_KEY`.
*First-party Anthropic:* secret `ANTHROPIC_API_KEY`.

**2.** Drop this in `.github/workflows/review.yml`:

```yaml
name: CR review
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: cr-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    if: github.event.pull_request.head.repo.full_name == github.repository
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: YOUR_ORG/CR@main
        with:
          provider: foundry
          azure-api-key: ${{ secrets.AZURE_API_KEY }}
          azure-resource: sweden-foundary
```

<details>
<summary>First-party Anthropic instead</summary>

```yaml
      - uses: YOUR_ORG/CR@main
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```
</details>

**3.** Open a PR. That's it — no server, no database, no GitHub App registration.

### Warm a repo first (enterprise flow)

```bash
uv run cr index acme/api        # clone + index. One time per repo.
uv run cr cache-info            # what is cached on disk
```

Cold: clone + index the whole repo. Warm: every later review fetches only the
delta and reuses the index, so it starts in seconds *and* sees cross-file context
— callers of changed symbols, and files that historically change alongside them.

Reviews work without this; they just see the diff alone. `cr index` is what turns
a diff reader into something that can say "this change breaks three callers".

**The index is ours, on disk — not model memory.** The model is stateless and
remembers nothing between calls. The index exists so we can *select* the right
20K tokens of context out of a large repo quickly.

### Try it locally first

```bash
uv sync
cp .env.example .env    # fill in your provider credentials
export GITHUB_TOKEN=ghp_...

uv run cr doctor                              # verify provider + caching first
uv run cr review                              # uncommitted changes
uv run cr review --base main                  # main..HEAD
uv run cr review-pr --pr owner/repo#42 --dry-run   # a real PR, printed not posted
```

Always `--dry-run` the first time.

---

## Why a GitHub Action, not a GitHub App

The Action skips the webhook server, the queue, Postgres, the App registration,
*and* the sandbox — Actions already gives you an ephemeral VM per run. The same
`cr` core powers both, so the App (`docs/BACKLOG.md` CR-01…CR-07) is a delivery
change, not a rewrite.

The trade-off is real and worth knowing: **fork PRs are not reviewed.** The
workflow's `if:` guard skips them, because `pull_request` gives forks a read-only
token and no secrets. That is the correct behaviour until the sandbox lands
(CR-08…CR-13) — reviewing untrusted code without isolation is how you leak an API
key.

---

## How it works

```
PR diff → triage → lint (subtracts) → find (N lenses) → verify (refute) → gate → post
```

- **Triage** routes on hunk/file counts and path sensitivity. Lockfiles, generated
  files and vendored code exit at T0 having spent **$0.00** and made zero model calls.
- **Lint** runs `ruff`/`semgrep` when available and builds a *suppression list* —
  the model is forbidden from commenting on anything a linter already caught. This
  deletes the nitpick class at zero token cost.
- **Find** runs N specialist lenses concurrently over one shared, cached prompt
  prefix. Prompted for coverage, never for filtering.
- **Verify** tries to *refute* each finding in a fresh context, defaulting to
  refuted under uncertainty. Majority-refute kills it; so does a tie.
- **Gate** ranks survivors by confidence × severity and enforces a hard comment
  budget. The budget is a feature.
- **Post** submits one review with inline threads — not N separate comments, which
  generate N notifications and read as spam. Each comment carries a fingerprint
  marker, so re-running on a new push never reposts the same finding.

Cost: **$0.00** (T0) · **~$0.02** (T1) · **~$0.23** (T2) · **~$0.50** (T3).

## Providers

| | `CR_PROVIDER` | Notes |
|---|---|---|
| Microsoft Foundry | `foundry` | Keeps explicit `cache_control`, structured outputs and the effort ladder, so the cached-prefix design survives intact. No Batch API. Caching is beta there — run `cr doctor` to confirm it is actually on. |
| Anthropic first-party | `anthropic` | Everything, including Batch. |

Model IDs are env-overridable (`CR_MODEL_STANDARD`, `CR_MODEL_DEEP`,
`CR_MODEL_VERIFIER`) because Foundry routes by *deployment name*, which need not
match the canonical model ID. Defaults: Sonnet 5 for finders, Opus 5 for T3
verification. **No Haiku tier** — not every deployment has one, and Sonnet at
`effort=low` is close enough in cost without adding a required model.

## Dashboard

```bash
cd dashboard && npm install && npm run build && cd ..
uv run cr serve            # http://127.0.0.1:8000
```

For UI development, run the API and Vite separately — Vite proxies `/api`:

```bash
uv run cr serve            # terminal 1
cd dashboard && npm run dev   # terminal 2, http://localhost:5173
```

Shows in-flight reviews with their current stage, spend over time, verifier kill
rate, cache-hit health, findings by severity, and the suppression memory. It
reads the same SQLite store the CLI writes to, so a review started from a
terminal or from CI appears live with no extra wiring.

## Learning and measurement

```bash
cr learn --pr owner/repo#42   # record resolved threads + thumbs-down as suppressions
cr stats                      # runs, cost, how often suppressions fired
cr eval --runs 3              # score against PRs whose bugs are known
```

Rejections are stored per repo by finding fingerprint and filtered out of every
later review, so the same wrong comment never comes back. SQLite by default;
point `CR_DATABASE_URL` at Postgres and nothing else changes.

`cr eval` is the only reason any quality claim here is trustworthy. Recall is
solid; precision is a **lower bound**, since an unmatched finding may be a real
defect nobody labelled.

Current score on `evals/fixtures/turbo-chat-1.json` (3 planted bugs, 3 runs):
**100% recall, every run.**

## Design docs

| | |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | What we're building and why |
| [`docs/PIPELINE.md`](docs/PIPELINE.md) | Execution, caching, concurrency at scale |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | 57 items, each with the library that solves it |

## Layout

```
src/cr/
  models.py        finding schema — the thesis as types
  repo.py          warm bare mirror + throwaway worktree per review
  graph.py         tree-sitter symbol index, persisted per commit
  warm.py          clone + index orchestration, base-graph + delta
  config.py        tier routing table
  triage.py        T0/T1/T2/T3, no model calls
  diff.py          unidiff parsing + commentable-line map
  lint.py          deterministic layer (D1) — subtracts from the model
  github.py        PR fetch, inline review posting, dedup
  llm/prefix.py    ★ cached prompt-prefix builder (two breakpoints)
  llm/client.py    ★ Anthropic wrapper + staggered fan-out
  review/prompts.py ★ finder and verifier prompts — this file is the product
  review/engine.py  find → prefilter → verify → gate
  cli.py           `cr review`, `cr review-pr`
vendor/pr_agent/   MIT algorithms lifted from PR-Agent
```

## Two invariants you must not break

**1. Cache-prefix byte stability.** A timestamp, UUID or unsorted dict anywhere in
a cached block silently sets `cache_read_input_tokens` to zero. `prefix.py` raises
`CacheInvalidatorError` at build time; `tests/test_prefix.py` guards the layout.

**2. Staggered fan-out.** A cache entry is only readable once the first response
*begins streaming*. Firing all N specialists at once on a cold prefix means all N
pay full price. `LLMClient.fanout` starts one pass, waits for its first event, then
releases the rest. Do not "optimise" that await away.

## Development

```bash
uv run pytest                      # 33 tests, no API key needed
uv run ruff check src/ tests/
```

## Licence and attribution

`vendor/pr_agent/` contains files from [PR-Agent](https://github.com/The-PR-Agent/pr-agent)
under the MIT licence — see `NOTICE` and `vendor/pr_agent/README.md`.
