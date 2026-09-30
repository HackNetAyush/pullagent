"""Pin every Martian gold PR that has a CodeRabbit fork, for the v2 pilot runner.

Public read-only downloads only: no model calls, no GitHub writes, no LLM spend.
Output files match the schema `coderabbit_bench_v2.py --run` already consumes, so
the runner, judge and scoring are unchanged.

The CodeRabbit baseline here is parsed from the review CodeRabbit actually
published on the pinned head commit. That is a DIFFERENT construction from the
four rows pinned by `coderabbit_bench_v2.py --prepare`, which reuse the previous
evaluation's model-atomized true/false-positive summaries. Compound CodeRabbit
comments are left whole here rather than split into atomic claims, so candidate
COUNTS are not comparable across the two constructions. Gold recall still is:
the judge matches each gold label against every candidate, and a compound
candidate that contains the claim still matches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rabbit_baseline  # noqa: E402

from cr.config import settings  # noqa: E402
from cr.diff import parse  # noqa: E402

INPUTS = ROOT / ".cache" / "coderabbit-pilot"
ORG = "code-review-benchmark"
DATASET = "withmartian/code-review-benchmark"
GOLD_KEYS = ("cal_dot_com", "discourse", "grafana", "keycloak", "sentry")
FORK_NAME = re.compile(r"(?P<key>.+?)__(?P<repo>.+?)__coderabbit__PR(?P<pr>\d+)__(?P<date>\d+)$")


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def input_path(pr: str) -> Path:
    return INPUTS / (pr.replace("/", "__").replace("#", "__") + ".json")


def paged(client: httpx.Client, url: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        response = client.get(url, params={"per_page": 100, "page": page})
        response.raise_for_status()
        batch = response.json()
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def benchmark_pull(client: httpx.Client, fork: str) -> dict:
    """The seeded benchmark PR, not the dependabot noise these forks accumulate."""
    pulls = paged(client, f"https://api.github.com/repos/{ORG}/{fork}/pulls?state=all")
    seeded = sorted(
        (p for p in pulls if p["user"]["login"] != "dependabot[bot]"), key=lambda p: p["number"]
    )
    if not seeded:
        raise ValueError(f"No seeded PR on {fork}")
    return seeded[0]


def discover_forks(client: httpx.Client) -> dict[tuple[str, int], str]:
    forks = {}
    for repo in paged(client, f"https://api.github.com/orgs/{ORG}/repos"):
        match = FORK_NAME.match(repo["name"])
        if match:
            forks[(match.group("key"), int(match.group("pr")))] = repo["name"]
    return forks


def pin(client: httpx.Client, key: str, entry: dict, fork: str, dataset_sha: str) -> dict:
    number = int(entry["url"].rstrip("/").split("/")[-1])
    slug = "/".join(entry["url"].split("/")[3:5])
    pr = f"{slug}#{number}"
    pull = benchmark_pull(client, fork)
    head, base = pull["head"]["sha"], pull["base"]["sha"]
    api = f"https://api.github.com/repos/{ORG}/{fork}/pulls/{pull['number']}"
    comments = paged(client, f"{api}/comments")
    reviews = paged(client, f"{api}/reviews")

    # Every CodeRabbit inline comment must belong to the commit we pin, or the
    # baseline and the reviewed diff describe different code.
    stale = sorted(
        {
            c["original_commit_id"]
            for c in comments
            if rabbit_baseline.is_rabbit(c)
            and c.get("original_commit_id")
            and c["original_commit_id"] != head
        }
    )
    if stale:
        raise ValueError(f"CodeRabbit comments predate pinned head on {pr}: {stale}")

    extracted = rabbit_baseline.extract(comments, reviews, head)
    # CodeRabbit acknowledged the review request on a few forks but never posted
    # a review. That is not evidence it found nothing, so it is flagged rather
    # than scored as a zero-recall CodeRabbit run.
    status = "ok" if extracted["texts"] else "no_review_posted"

    response = client.get(
        f"https://api.github.com/repos/{ORG}/{fork}/compare/{base}...{head}",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )
    response.raise_for_status()
    diff = response.text
    files = parse(diff)
    if not files:
        raise ValueError(f"Missing/invalid immutable diff for {pr}")

    value = dict(
        pr=pr,
        gold_key=key,
        fork_url=f"https://github.com/{ORG}/{fork}/pull/{pull['number']}",
        fork_slug=f"{ORG}/{fork}",
        base=base,
        head=head,
        dataset_sha=dataset_sha,
        gold=entry["comments"],
        title=pull.get("title", ""),
        description=pull.get("body") or "",
        diff=diff,
        diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
        rabbit_review_commits=[head] if extracted["texts"] else [],
        rabbit_candidates=extracted["texts"],
        rabbit_candidate_detail=extracted["candidates"],
        rabbit_published_candidate_count=len(extracted["texts"]),
        rabbit_status=status,
        rabbit_actionable_posted=extracted["actionable_posted"],
        rabbit_praise_excluded=extracted["praise_excluded"],
        baseline_source="parsed_published_review",
        baseline_note=(
            "CodeRabbit candidates parsed from its published review at the pinned head "
            "(inline comments plus the collapsed nitpick / outside-diff / duplicate "
            "sections); the LGTM 'Additional comments' section is excluded. Compound "
            "comments are not split into atomic claims, unlike the four rows pinned by "
            "coderabbit_bench_v2.py --prepare, so candidate counts are not comparable "
            "across the two constructions."
        ),
        corpus_note=(
            f"Martian offline gold set, dataset pinned at {dataset_sha[:12]}. "
            "Development data, not a held-out test set."
        ),
    )
    dump(input_path(pr), value)
    print(
        f"pinned {pr}: {len(files)} files; {head[:12]}; "
        f"rabbit={len(extracted['texts'])} ({status}); gold={len(entry['comments'])}",
        flush=True,
    )
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", action="append", default=[], help="owner/repo#number; repeatable")
    parser.add_argument("--key", action="append", default=[], choices=list(GOLD_KEYS))
    parser.add_argument(
        "--refresh", action="store_true", help="Re-pin PRs whose input file already exists"
    )
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {settings.github_token}"} if settings.github_token else {}
    pinned, skipped = [], []
    with httpx.Client(headers=headers, timeout=90, follow_redirects=True) as client:
        response = client.get(f"https://api.github.com/repos/{DATASET}/commits/main")
        response.raise_for_status()
        dataset_sha = response.json()["sha"]
        print(f"gold dataset pinned at {dataset_sha}", flush=True)
        forks = discover_forks(client)
        print(f"discovered {len(forks)} CodeRabbit forks in {ORG}", flush=True)

        for key in args.key or GOLD_KEYS:
            gold_url = (
                f"https://raw.githubusercontent.com/{DATASET}/{dataset_sha}/"
                f"offline/golden_comments/{key}.json"
            )
            response = client.get(gold_url)
            response.raise_for_status()
            for entry in response.json():
                number = int(entry["url"].rstrip("/").split("/")[-1])
                slug = "/".join(entry["url"].split("/")[3:5])
                pr = f"{slug}#{number}"
                if args.pr and pr not in args.pr:
                    continue
                existing = input_path(pr)
                if existing.exists():
                    prior = json.loads(existing.read_text(encoding="utf-8"))
                    # Never overwrite a row pinned by coderabbit_bench_v2.py
                    # --prepare: its baseline is the previous evaluation's
                    # summaries, and rewriting it would silently change the
                    # construction behind already-scored results.
                    if prior.get("baseline_source") != "parsed_published_review":
                        print(f"keeping reconstructed baseline: {pr}", flush=True)
                        continue
                    if not args.refresh:
                        print(f"inputs already pinned: {pr}", flush=True)
                        continue
                fork = forks.get((key, number))
                if not fork:
                    skipped.append((pr, "no CodeRabbit fork"))
                    print(f"SKIP {pr}: no CodeRabbit fork in {ORG}", flush=True)
                    continue
                pinned.append(pin(client, key, entry, fork, dataset_sha))

    gold = sum(len(v["gold"]) for v in pinned)
    flagged = [v["pr"] for v in pinned if v["rabbit_status"] != "ok"]
    print(f"\npinned {len(pinned)} PRs carrying {gold} gold labels", flush=True)
    if flagged:
        print(f"CodeRabbit posted no review on {len(flagged)}: {', '.join(flagged)}", flush=True)
    for pr, why in skipped:
        print(f"skipped {pr}: {why}", flush=True)


if __name__ == "__main__":
    main()
