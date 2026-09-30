# Measuring review quality and cost

The historical `scripts/coderabbit_bench_results.jsonl` is preserved. Its four
rows reported 10/24 golden-label matches, but it did not save CR findings or
verifier decisions. We cannot retrospectively identify exactly where its 14
unmatched labels were lost. Golden labels can describe the same underlying bug.

Do not interpret `matched golden labels / posted comments` as precision: one
comment can match multiple labels. An unmatched comment can also identify a real
unlabelled defect. The original CR and published CodeRabbit results used different
judges, so those scores are not a controlled head-to-head.

## What changed in the review pipeline

- Nearby location alone no longer deduplicates independent defects. Dedup requires
  matching claims/scenarios or matching fixes with substantial claim overlap.
- T2 retains three finder calls, using correctness, contracts, and state/security
  lenses. It can surface up to 12 verified comments instead of six.
- Verification batches up to six candidates per lens. Twelve candidates need four
  initial verifier requests instead of 24. Disagreement or missing evidence gets
  a separate evidence adjudication; uncertainty does not automatically become a
  confirmed bug. Batching can change model behaviour, so evaluate recall as well
  as request counts.
- Lower verifier effort and batching reduce potential spend. Actual savings
  depend on token usage; a request-count reduction is not a cost guarantee. A live
  16k/high-effort finder experiment truncated all three calls; it was rejected.
  T2 retains its 32k finder ceiling, and truncation is an explicit failed review.
- Large diffs are chunked rather than silently discarding later files. Large hunks
  retain recalculated line numbers. A single enormous line fails explicitly.
  Chunking may still weaken cross-chunk reasoning; use repository context.
- Deleted source files remain reviewable. Generated/lockfile exclusions still apply.
- Repository slices include line-numbered callers and callee definitions. Reusing
  an index repairs every changed file since its indexed commit, not just PR files.
  Historical co-change selection is bounded to ancestors of the reviewed commit.
- Every run retains raw findings, prefilter reasons, verifier decisions, cap trims,
  memory suppressions, per-call usage/cost, and failures. Incomplete CLI PR reviews
  are not posted and are not cached.

PR-Agent's existing vendored dedup and multi-diff strategies informed the design;
no additional package, licence change, or GitHub-side service is required. See
`NOTICE` and `vendor/pr_agent/` for existing attribution.

## Inspect a real review

```powershell
uv run cr review-pr --pr 'owner/repo#123' --tier T2 --dry-run --trace first.json
uv run cr review-pr --pr 'owner/repo#123' --tier T2 --dry-run --trace replay.json
uv run cr review-pr --pr 'owner/repo#123' --tier T2 --dry-run --refresh --trace fresh.json
```

`--dry-run` never posts a review. `--trace` saves the full result, including source
excerpts and findings, so treat it as repository data, not a public telemetry file.

The second identical PR review can use the exact-input cache for **$0 model spend**.
It expires after 24 hours by default (`CR_REVIEW_CACHE_TTL_S`). Disable with
`CR_REVIEW_CACHE=false`; use `--refresh` for an independent quality measurement.
Cache keys include commit, supplied context, tier/model/provider endpoints,
confidence threshold, chunk size, and a hash of CR's Python implementation.
Suppression memory is reapplied at replay time. Cache files are local repository
data under `CR_CACHE_DIR` (or the usual CR cache directory); persist and protect
that directory appropriately in CI. Local working-tree reviews and benchmark/eval
runs do not automatically replay cached results.

Different PRs/commits are not free. The reusable symbol index saves retrieval work;
provider prompt caching only discounts matching prefixes within its TTL and minimum
size requirements. It is not durable model memory. CR reports estimated token cost
using its configured model rate table, not your provider invoice; one-hour cache
writes are accounted separately from five-minute writes.

## Reproducible CodeRabbit development pilot

```powershell
# Public read-only downloads; no LLM spend, no PR creation or posting.
uv run python scripts/coderabbit_bench_v2.py --prepare

# One fresh review by default, retaining all intermediate artifacts.
uv run python scripts/coderabbit_bench_v2.py --run --pr 'getsentry/sentry#93824'

# Original four-PR pilot. The spend stop is checked BETWEEN PRs, not a hard cap.
# See "The 50-PR gold corpus" below for the full set.
uv run python scripts/coderabbit_bench_v2.py --run --limit 4 --max-spend 4
```

Inputs live in `.cache/coderabbit-pilot/`, pinned to immutable fork base/head SHAs
and a benchmark-data commit. Preparation verifies CodeRabbit inline comments refer
to that head. Runs never pass gold labels or CodeRabbit comments to CR. `--graph`
opts into cloning/indexing the pinned benchmark forks; the default is diff-only to
keep the pilot comparable to the previous CR input mode.

The shared judge sees anonymous, content-sorted candidates from both tools, with
the same model/prompt/labels. For these four rows CodeRabbit candidates are unique
published TP/FP summaries reconstructed from the previous evaluation, not a fresh
CodeRabbit run; some published duplicate candidates are absent from these summaries.
The other 46 rows parse CodeRabbit's published review directly — see "The 50-PR gold
corpus" below. Either way this is a useful diagnostic comparison, **not an official
leaderboard score**.

Results include strict/core/all-category golden-label recall, candidate counts,
and a `missed` list locating each unmatched label at `not_found`, `prefilter`,
`verification`, or `comment_cap`. Candidate gold-match counts are not called
precision. A human should audit semantic matches and unmatched findings before
making quality claims. Label duplicates and dubious labels need adjudication too.

Reviews are saved before judging. Repeating the same script/version resumes saved
work without repaying for reviews or judges. Fresh reviews also test an exact-input
replay and save its zero-cost result separately; cached replays never count as new
quality samples. Use `--run-id repeat-2` for an intentional independent paid repeat,
preserving previous artifacts. The runner stops after an incomplete review instead
of spending on the next PR with the same broken configuration.

Before claiming superiority: freeze the configuration, test additional unseen PRs,
use the same source snapshots and judge for both tools, human-audit findings, measure
candidate-level precision separately from gold recall, and report repeated-run
variance plus actual costs. The original four PRs are development data, not a held-out set; the 46 added later
are closer to unseen but were still drawn from the same public dataset.

## The 50-PR gold corpus

```powershell
# Public read-only downloads; no LLM spend, no GitHub writes.
uv run python scripts/prepare_gold_corpus.py

# Narrow to one dataset repo, or to specific PRs.
uv run python scripts/prepare_gold_corpus.py --key grafana
uv run python scripts/prepare_gold_corpus.py --pr 'keycloak/keycloak#36880'
```

`prepare_gold_corpus.py` pins every PR in the Martian offline gold set that has a
CodeRabbit fork in the `code-review-benchmark` org. That is **50 PRs carrying 173
gold labels (139 strict, 158 core)**, against the four PRs and 24 labels the
original pilot used. Inputs land in `.cache/coderabbit-pilot/` in the schema
`coderabbit_bench_v2.py --run` already reads, so the runner, judge and scoring are
unchanged and previously pinned inputs are never rewritten.

Each fork accumulates dependabot PRs; the seeded benchmark PR is the lowest-numbered
non-dependabot one. Pinning fails loudly if any CodeRabbit inline comment refers to
a commit other than the pinned head, so the baseline and the reviewed diff always
describe the same code.

### How the CodeRabbit baseline is built, and why it differs across rows

CodeRabbit splits one review across two surfaces: inline comments ("Actionable
comments posted: N") and collapsed sections inside the single review body
(nitpicks, outside-diff-range, duplicates). `scripts/rabbit_baseline.py` parses
both. It makes no model calls and invents nothing — every candidate is a
markdown-stripped excerpt of text CodeRabbit actually published at the pinned head.

The `🔇 Additional comments` section is CodeRabbit's LGTM/praise channel, not defect
claims. It is parsed, counted as `rabbit_praise_excluded`, and kept out of the
candidate universe. Including it would have inflated keycloak#37429 from 9
candidates to 50 and fed the blind judge dozens of non-claims.

Rows therefore carry one of two `baseline_source` values, and **candidate counts are
not comparable between them**:

| `baseline_source` | Rows | Construction |
| --- | --- | --- |
| `reconstructed_published_summaries` | 4 | Previous evaluation's model-atomized TP/FP summaries, via `coderabbit_bench_v2.py --prepare` |
| `parsed_published_review` | 46 | Deterministic parse of CodeRabbit's published review, via `prepare_gold_corpus.py` |

On the four overlapping PRs the parser reproduces the old unique-summary counts
exactly for keycloak#37429 (9), sentry#93824 (4) and grafana#79265 (7). cal.com#11059
yields 15 against 17 because the old evaluation split compound comments into atomic
claims — one inline comment covering HTTP method, header casing and array headers
became three. Gold **recall** is unaffected by that difference: the judge matches each
gold label against every candidate, and a compound candidate containing the claim
still matches. Only candidate counts shift, and those were never precision.

### Rows that are missing data, not measurements

CodeRabbit acknowledged the review request on grafana#90939, #94942 and #97529 but
never posted a review. That is not evidence it found nothing. Those three are pinned
with `rabbit_status: "no_review_posted"`, and their score rows carry an explicit
caveat. **Exclude them from any head-to-head** — scoring them as zero-recall
CodeRabbit runs would bias the comparison toward CR by six gold labels.

### Before running the whole corpus

50 PRs is roughly 12× the previous pilot's review spend, and the runner's
`--max-spend` is a between-PR stop, not a provider hard cap. Start with `--key` or
`--pr` subsets. The corpus is development data: the original four PRs were used while
tuning the pipeline, so treat the other 46 as the closer thing to unseen and report
them separately rather than pooling all 50 into one headline number.

Dataset and category definitions:
[Martian offline benchmark](https://github.com/withmartian/code-review-benchmark/blob/main/offline/README.md).
Provider cache semantics:
[Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
