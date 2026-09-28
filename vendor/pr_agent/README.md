# Vendored from PR-Agent (MIT)

Source: https://github.com/The-PR-Agent/pr-agent — licence in `LICENSE.pr-agent`.

These files are **reference copies, not yet wired in.** They import from
`pr_agent.*` (config_loader, log, types), so they will not run as-is. Phase 3
rewires their imports onto our `cr.*` equivalents; until then treat this
directory as documentation-that-compiles-elsewhere.

Vendor, do not fork. The reason we copied files instead of forking the project is
that PR-Agent's architecture serves a different thesis — no sandbox, no code
graph, no verification stage, no state store. Forking means doing surgery on
someone else's abstractions forever.

| File | LOC | Why we want it | Wire up in |
|---|---:|---|---|
| `git_patch_processing.py` | 535 | Asymmetric/dynamic context expansion and hunk parsing. Handles `\ No newline at end of file`, CRLF, and Unicode line separators (`\x1c \x1d \x1e \x85` U+2028 U+2029) — each of those is a bug someone found in production | Phase 3 |
| `token_budget.py` | 658 | Budget accounting + `FallbackEligibleError`, which distinguishes "this model can't fit it" from "this request is broken" so only the former falls through to a fallback model | Phase 3 |
| `review_finding_state.py` | 371 | ACTIVE/RESOLVED marker reconciliation across runs | Phase 4 |
| `inline_comment_dedup.py` | 326 | Dual SHA-256 fingerprint (prose + suggestion code) matched with OR semantics — catches both ways an LLM restates a finding | Phase 4 |
| `language_handler.py` | 135 | Repo language prioritisation, drives packing order | Phase 3 |
| `file_filter.py` | 94 | Glob filtering | Phase 3 |
| `model_routing.py` | 80 | Hunk/file-count routing. Already reimplemented in `cr/triage.py` — kept for reference | done |

## Still to lift (not yet copied)

- `algo/pr_processing.py` (915 LOC) — the compression/packing core. Adapt rather
  than copy: the structure is right but it must feed our cache-prefix builder.
- `servers/github_app.py` (615 LOC) — webhook dispatch skeleton, HMAC verification,
  handlers for PR-opened / comments / review-submitted / push. Re-target to our queue.
- `git_providers/github_provider.py` — we want ~400 LOC of it, principally
  **`find_line_number_of_relevant_line_in_file`**. GitHub's committable comments
  are anchored by *offset into the diff hunk*, not file line number. This is the
  fiddliest thing in the entire GitHub API surface and it is already solved here.

## Deliberately not taking

- **LiteLLM** — it abstracts away cache breakpoints, effort levels, and thinking
  configuration, which are exactly the things we are optimising.
- Their dynaconf/TOML config sprawl.
- Their tool/prompt structure and agent loop.
