# The GitHub App

Install it once on your account. Every pull request gets reviewed, every push
gets the delta reviewed, and replies to its comments get answered.

```
webhook ─► verify ─► dedup ─► enqueue ─► 202 (in ~5ms)
                                 │
                       worker ─► fetch ─► triage ─► review ─► post ─► record
```

The Action (`README.md`) and the App run the same engine. The Action is
simpler and needs no server; the App adds the things that only exist when
nobody typed a command — incremental reviews, conversation, learning from
dismissals, and fork coverage.

---

## Set it up in five minutes

GitHub's servers have to be able to POST events to you, and localhost is not
on the internet. Pick one of two ways to fix that.

### Option A — a relay (no install, stable URL)

Get a free inbox at <https://smee.io/new>, then run two terminals:

```bash
npx smee-client --url https://smee.io/<id> --target http://localhost:8010/webhook
uv run cr app serve --port 8010 --webhook-url https://smee.io/<id>
```

The relay URL survives restarts, so the App keeps working tomorrow without
being edited. Your browser still talks to `localhost` for the setup flow —
only GitHub's traffic goes through the relay, which is why the two URLs are
configured separately.

### Option B — a tunnel (one public host for everything)

```bash
winget install --id Cloudflare.cloudflared          # once
cloudflared tunnel --url http://localhost:8010
uv run cr app serve --port 8010 --public-url https://<tunnel-host>
```

Quick tunnels get a new hostname on every restart, so you will have to update
the App's webhook URL each time. Fine for a first try, annoying as a habit.

### Then, either way

**1. Open <http://localhost:8010/app/setup>** and press the button. GitHub
creates the App, generates the private key and webhook secret, and hands them
back — written to `~/.cache/cr/github-app.json` with 0600 permissions and
loaded without a restart.

**2. Install it** on your account from the link on the next page.

**3. Open a pull request.**

Check it took:

```bash
uv run cr app status
```

Port 8010 is just an example; 8000 is often already in use on Windows.

### Doing it by hand instead

Create the App at <https://github.com/settings/apps/new> with the permissions
and events in the table below, then:

```bash
CR_GITHUB_APP_ID=123456
CR_GITHUB_APP_PRIVATE_KEY_PATH=/secure/path/key.pem
CR_GITHUB_WEBHOOK_SECRET=<a long random string>
```

The key may also go in `CR_GITHUB_APP_PRIVATE_KEY` directly, as raw PEM,
backslash-escaped PEM, or base64 — all three are accepted, because every
deployment platform mangles newlines differently.

---

## What it does

| Trigger | What happens |
|---|---|
| PR opened / reopened / ready for review | Full review |
| Push to the PR branch | **Incremental** review — only what changed since the last one |
| Reply to one of its comments | It answers, and withdraws the finding if you are right |
| `@pullagent review` | Full review now |
| `@pullagent incremental` | Just the delta |
| `@pullagent ask <question>` | Answers from the diff and the symbol index |
| `@pullagent ignore` (under a comment) | Suppresses that finding on this repo, permanently |
| App installed | Indexes the repos up front, so the first PR is not the slowest |

Every review also reports a check run, so a clean PR shows a green check
instead of a comment saying "no issues found".

### Incremental review

On a push, the App diffs the last reviewed head against the new one and
reviews only that. Ten commits on a PR cost ten small reviews, not ten full
ones.

Three details make it safe rather than merely cheap:

- **Anchors come from the whole PR diff**, not the slice, so a comment can
  still land on a line from an earlier commit.
- **Base-branch merges are filtered out.** A `git merge main` into the branch
  shows up in the comparison but was not written by the PR author; commenting
  on it would mean reviewing someone else's merged code.
- **Findings are deduped by fingerprint** against what is already on the PR,
  so a full re-review never reposts. Incremental is a cost decision, not a
  correctness one — `CR_APP_INCREMENTAL=false` reviews the whole PR each time
  and produces the same comments, for more money.

### Conversation

Reply to a finding and it reads the thread, the hunk it was anchored to, and
the PR, then answers in that thread.

The interesting case is disagreement. If you show the finding is wrong — a
guard it missed, a caller that cannot reach the code — it concedes **and
writes a `Suppression` row**, which is the same mechanism `cr learn` uses. The
finding is gone for that repo, for good. Conceding in prose while planning to
repost next week would be worse than not replying.

The opposite failure is the one prompts usually get wrong: a model asked "are
you sure?" folds and withdraws a correct finding. `REPLY_PREAMBLE` in
`review/prompts.py` addresses both directions explicitly, because a withdrawn
real defect is recorded and never raised again.

### Learning, before each review

Every review first reads resolved threads and 👎 reactions and records them as
suppressions. A finding you dismissed a minute ago cannot come back in the
review that starts thirty seconds later.

---

## Security

The threat model is that this service holds a credential which can write to
your repositories, and processes input from anyone who can open a pull
request.

**Credentials.** Three secrets, three lifetimes. The private key is the only
long-lived one; it never leaves the process and is wrapped in `Secret`, which
renders as `Secret(***)` in logs, reprs and tracebacks. From it we sign a
10-minute RS256 App JWT that can do exactly two things — list installations
and mint tokens. Tokens are 1-hour, **scoped to the single repository in the
event**, cached in memory only, and refreshed two minutes early.

**Webhooks.** HMAC-SHA256 over the raw body, compared with
`hmac.compare_digest`, before the JSON is parsed — verifying a re-serialised
body accepts forgeries whenever whitespace happens to match. A missing secret
is a hard failure, never a bypass: an unauthenticated ingress lets anyone on
the internet spend your model budget and comment as you.

**Replay.** Every delivery id is claimed in the `deliveries` table before any
work starts. GitHub redelivers on timeout and on manual replay, and a
redelivered `synchronize` is otherwise indistinguishable from a real push.

**Loops.** Bot senders are rejected by both `sender.type` and the `[bot]`
login suffix before anything else runs. Being wrong here costs an infinite
comment loop.

**Prompt injection.** PR descriptions, code, and comment bodies all reach the
model, and all of them are attacker-controlled on a fork PR. Every prompt
labels them as untrusted evidence and forbids obeying instructions found
inside them. This is mitigation, not proof — treat the review as advice, and
keep branch protection on.

**Fork PRs** are reviewed by default. Their code is fetched, checked out and
read — by the symbol index, and by linters that parse rather than execute.
Nothing from the repository is ever run. That boundary is the whole security
story until the sandbox lands (`BACKLOG.md` CR-08…CR-13), so it must not
quietly grow a build step. `CR_APP_REVIEW_FORKS=false` turns fork reviews off.

**The setup flow** is CSRF-protected with a single-use, 15-minute `state`
nonce, refuses to serve once credentials exist, and can be disabled with
`CR_APP_ALLOW_SETUP=false`. Beyond one machine, put the key in a secret
manager and turn setup off (`BACKLOG.md` CR-50).

---

## Configuration

| Variable | Default | |
|---|---|---|
| `CR_GITHUB_APP_ID` | — | From the App page, or written by the setup flow |
| `CR_GITHUB_APP_PRIVATE_KEY` | — | PEM: raw, `\n`-escaped, or base64 |
| `CR_GITHUB_APP_PRIVATE_KEY_PATH` | — | Or point at the file |
| `CR_GITHUB_WEBHOOK_SECRET` | — | **Required.** Without it every delivery is rejected |
| `CR_APP_INCREMENTAL` | `true` | Review only the delta on a push |
| `CR_APP_DEBOUNCE_S` | `20` | Collapse a burst of pushes into one review |
| `CR_APP_MAX_CONCURRENT_REVIEWS` | `2` | Reviews in flight across all repos |
| `CR_APP_REVIEW_DRAFTS` | `false` | Draft PRs are reviewed on `ready_for_review` |
| `CR_APP_REVIEW_FORKS` | `true` | Read, clone and lint fork branches |
| `CR_APP_REPLY_TO_COMMENTS` | `true` | Answer replies in our own threads |
| `CR_APP_COMMAND_PREFIX` | `@pullagent` | What addresses the bot |
| `CR_APP_REPLY_MODEL` | `CR_MODEL_STANDARD` | Model used for conversation |
| `CR_APP_INDEX_ON_INSTALL` | `true` | Warm the symbol index on install |
| `CR_APP_ALLOW_SETUP` | `true` | Serve `/app/setup` |
| `CR_APP_CREDENTIALS_PATH` | `~/.cache/cr/github-app.json` | Where setup writes |

`--public-url` and `--webhook-url` are start-up flags, not settings. The first
is the base URL your *browser* uses during setup (localhost is fine). The
second is where *GitHub* delivers events, and only differs from
`<public-url>/webhook` when a relay sits in between.

Everything in `README.md` — provider, models, tiers, thresholds — applies
unchanged.

### Permissions the App requests

| Permission | Why |
|---|---|
| `contents: read` | Read the code, clone it, build the symbol index |
| `pull_requests: write` | Read the PR, post the review and its inline threads |
| `issues: write` | PR-level comments: `@pullagent` commands and their answers |
| `checks: write` | Report reviewing / done / failed as a check run |
| `metadata: read` | Implicit for every App |

Events: `pull_request`, `pull_request_review_comment`, `issue_comment`,
`installation`, `installation_repositories`.

---

## Operating it

```bash
cr app status                       # config, installations, recent jobs
cr app replay --pr owner/repo#42    # run the App's review path by hand
cr app replay --pr owner/repo#42 --full
```

`cr app replay` is how you test incremental behaviour without pushing
commits. It is the same code the queue runs.

The dashboard is served from the same process (`/`), so a webhook-triggered
review appears live next to one started from a terminal.

- `GET /api/app/status` — configuration, installations, queue depth
- `GET /api/app/jobs` — recent jobs and why they failed

### The queue

In-process, with the record in SQLite. A queue earns its infrastructure when
workers live on other machines; until then Redis buys one property — survive a
restart — that a table already gives us. On start-up, `pending_jobs()` replays
anything unfinished, because GitHub does not redeliver a webhook you already
answered 202.

Three behaviours matter:

- **Debounce** — four fixup commits in ninety seconds are one review.
- **Singleflight** — one review per PR at a time, or two races produce doubled
  comments.
- **Supersede** — a push during a review cancels it. The stale review never
  posts, the ledger row is closed as `cancelled`, and the check run says
  "superseded" instead of spinning forever.

Swapping in arq is a change to `app/jobs.py` alone: `submit()` and the handler
contract are the whole surface.

### Scaling past one process

The concurrency cap is per process and the queue is in memory, so two
replicas would debounce independently and could double-review a PR. For a
single account or a small org, one process is the right answer. Beyond that,
`BACKLOG.md` CR-02 is the Redis-backed version, and nothing above it changes.

---

## Troubleshooting

**No reviews happen.** Check GitHub's delivery log (App settings → Advanced).
A 401 means the webhook secret does not match; a 503 means the server has no
App credentials loaded.

**"no webhook secret configured".** Deliveries are rejected on purpose. Set
`CR_GITHUB_WEBHOOK_SECRET` to the value on the App's settings page.

**"could not sign the App JWT".** The PEM was mangled. Use
`CR_GITHUB_APP_PRIVATE_KEY_PATH`, which cannot be.

**Reviews are slow the first time on a repo.** It is cloning and indexing.
`cr index owner/repo` up front, or let `CR_APP_INDEX_ON_INSTALL` do it.

**A finding keeps coming back.** Resolve the thread, react 👎, or reply
`@pullagent ignore` under it. All three write a suppression. `cr stats` shows how
often they fire.
