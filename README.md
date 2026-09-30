# CR — high-precision AI code review

An AI code reviewer that finds concrete defects and verifies them before posting.
Recall and precision are measured separately; neither is improved by hiding failures.
See [benchmarking and cost controls](docs/BENCHMARKING.md) for stage traces,
repeat-review caching, and the reproducible CodeRabbit development pilot.

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

**3.** Open a PR. That's it — no server, no database, no App registration.
(Or skip the workflow file entirely and [install the GitHub App](#or-install-it-as-a-github-app).)

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

## Or install it as a GitHub App

One install, every repo, no workflow file — and three things the Action cannot
do: review only what changed on a push, answer when you reply to a comment,
and review fork PRs.

GitHub's servers have to reach you, so put a relay or a tunnel in front of
localhost. A relay needs no install and its URL survives restarts:

```bash
# grab a free inbox at https://smee.io/new, then, in two terminals:
npx smee-client --url https://smee.io/<id> --target http://localhost:8010/webhook
uv run cr app serve --port 8010 --webhook-url https://smee.io/<id>
```

Then open <http://localhost:8010/app/setup> and press the button: GitHub
creates the App through its manifest flow, generating the private key and the
webhook secret itself, so there is nothing to copy by hand.
Full guide: [`docs/GITHUB_APP.md`](docs/GITHUB_APP.md).

### Choosing between them

| | Action | App |
|---|---|---|
| Setup | a workflow file per repo | one install |
| Infrastructure | none | one process + a public URL |
| Review on push | whole PR again | **only the new commits** |
| Replies to your comments | no | **yes, and it withdraws when you are right** |
| `@pullagent ask` / `@pullagent review` | no | **yes** |
| Fork PRs | **skipped** | reviewed |
| Isolation | ephemeral VM per run | the host (see below) |

The Action skips fork PRs because `pull_request` gives forks a read-only token
and no secrets — correct, since reviewing untrusted code in a privileged
context is how you leak an API key. The App reviews them because it never
*executes* repository code: fork branches are fetched, checked out and read by
the symbol index and by linters that parse rather than run. That boundary is
the security story until the sandbox lands (`docs/BACKLOG.md` CR-08…CR-13);
`CR_APP_REVIEW_FORKS=false` opts out.

Same engine underneath either way — `cr.review.engine` does not know which
one called it.

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
- **Verify** checks small batches independently through multiple lenses.
  Disagreements and uncertainty receive an evidence adjudication; unresolved
  findings are not posted. API failures are reported as incomplete reviews.
- **Gate** ranks survivors by confidence × severity and enforces a hard comment
  budget. The budget is a feature.
- **Post** submits one review with inline threads — not N separate comments, which
  generate N notifications and read as spam. Each comment carries a fingerprint
  marker, so re-running on a new push never reposts the same finding.

T0 and valid exact-input replays cost **$0 in model calls**. Fresh-review cost depends
on diff/context size, model output and provider cache hits; inspect `--trace` for
measured per-call token estimates rather than assuming a fixed tier price.

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

Seven routed pages, not one scrolling column:

| | |
|---|---|
| **Overview** | spend and reviews per day, findings by severity, where the money goes |
| **Reviews** | every run, filterable by repo / status / tier / source |
| **Findings** | every defect across all runs, including the ones held back |
| **Repositories** | per-repo rollup — the unit a team actually budgets in |
| **Suppressions** | what a human rejected, why, and how often it has fired since |
| **Queue** | in-flight and superseded jobs, so a stuck review is visible |
| **Access** | the account allowlist (administrators only) |

Every table pages server-side and reports a real total, so "25 of 3,214" is
honest rather than "the first 50 and who knows". Filters live in the URL, so a
filtered view is a shareable link.

It reads the same store the CLI writes to, so a review started from a terminal
or from CI appears live with no extra wiring. When `CR_GITHUB_CLIENT_ID` is set
— the moment the dashboard is reachable by anyone else — every read requires a
signed-in session; on a laptop with no OAuth app it stays open, because
demanding a login there would lock you out of your own machine.

**Chart colours are validated, not chosen.** The palette in `theme.css` is run
through the colourblind-separation and contrast checks; severity is one hue
that darkens with seriousness rather than four unrelated colours, status
colours are reserved and always paired with a text label, and cost and
run-count are two charts because they are two scales and must never share an
axis.

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
| [`docs/GITHUB_APP.md`](docs/GITHUB_APP.md) | Installing and running the App |
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
  cli.py           `cr review`, `cr review-pr`, `cr app serve`
  app/             the GitHub App — webhook ingress, queue, conversation
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
