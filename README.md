# CR — high-precision AI code review

An AI code reviewer that optimises for **precision, not recall**. Three comments
that are right beat twenty that are mostly noise.

Design docs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (what and why),
[`docs/PIPELINE.md`](docs/PIPELINE.md) (execution, caching, concurrency), and
[`docs/BACKLOG.md`](docs/BACKLOG.md) (what to build, with the library for each).

---

## Quick start

```bash
uv sync --extra dev
cp .env.example .env          # add your Anthropic key
uv run cr review              # review uncommitted changes
uv run cr review --base main  # review main..HEAD
uv run cr tiers               # show the routing table
```

## What it does today (Phase 0–2)

```
git diff → triage → find (N specialists) → verify (adversarial) → gate → render
```

- **Triage** routes on hunk/file counts and path sensitivity. Lockfiles, generated
  files, and vendored code exit at T0 having spent **$0.00** and made zero model calls.
- **Find** runs N specialist lenses concurrently over one shared, cached prompt prefix.
- **Verify** tries to *refute* each finding in a fresh context, defaulting to refuted
  under uncertainty. Majority-refute kills it; so does a tie.
- **Gate** ranks survivors by confidence × severity and enforces a hard comment budget.

## Status

| Phase | | |
|---|---|---|
| 0 | CLI spike | ✅ |
| 1 | Eval harness | 🔜 **next** |
| 2 | Precision gate | ✅ schema, verifier, budget |
| 3 | Context engine (compression, tree-sitter graph, lint subtraction) | ⬜ |
| 4 | GitHub App (webhook → queue → worker → inline comments) | ⬜ |
| 5 | Sandbox + scale | ⬜ |
| 6 | Learning loop | ⬜ |
| 7 | Cost tuning | ⬜ |
| 8 | VS Code extension | ⬜ |

## Layout

```
src/cr/
  models.py        finding schema — the thesis as types
  config.py        tier routing table
  triage.py        stage 1 — T0/T1/T2/T3, no model calls
  diff.py          local git diff → DiffSet
  llm/
    prefix.py      ★ cached prompt-prefix builder (two breakpoints)
    client.py      ★ Anthropic wrapper + staggered fan-out
  review/
    prompts.py     ★ finder and verifier prompts — this file is the product
    engine.py      stages 6–8
  cli.py           `cr review`
vendor/pr_agent/   MIT algorithms lifted from PR-Agent (see its README)
evals/             precision/recall/cost harness (phase 1)
```

The three ★ files are where the leverage is. Read `prefix.py` and `client.py`
together — they implement one idea, and getting it wrong costs 82% of the input
budget with no error message.

## Two invariants you must not break

**1. Cache-prefix byte stability.** A timestamp, UUID, or unsorted dict anywhere
in a cached block silently sets `cache_read_input_tokens` to zero. `prefix.py`
raises `CacheInvalidatorError` at build time to stop this reaching production, and
`tests/test_prefix.py` guards the layout. If `cr review` prints a low cache-hit
ratio, something got past both.

**2. Staggered fan-out.** A cache entry is only readable once the first response
*begins streaming*. Firing all N specialists at once on a cold prefix means all N
pay full price. `LLMClient.fanout` starts one pass, waits for its first event, then
releases the rest. Do not "optimise" that await away.

## Development

```bash
uv run pytest          # 25 tests, no API key needed
uv run ruff check src/ tests/
uv run mypy src/
```

Iterate on prompts through the CLI against a local diff. Never by pushing to GitHub.

## Licence and attribution

`vendor/pr_agent/` contains files from [PR-Agent](https://github.com/The-PR-Agent/pr-agent)
under the MIT licence — see `NOTICE` and `vendor/pr_agent/README.md`.
