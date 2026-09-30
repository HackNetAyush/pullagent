"""The GitHub App: webhook ingress, a durable queue, and the review runner.

`cr review-pr` (the Action) and `cr app serve` (this package) are two deliveries
of the same engine. Nothing in `cr.review` knows which one called it.

    webhook -> verify -> dedup -> enqueue -> 202
                                     |
                             worker: fetch -> review -> post -> record

Layout:

    auth.py          App JWT -> short-lived, repo-scoped installation tokens
    api.py           async GitHub client, installation-scoped
    events.py        signature verification and routing decisions (pure)
    jobs.py          in-process queue: singleflight, debounce, supersede
    runner.py        the review job, incremental on push
    conversation.py  replies to humans on our own review threads
    manifest.py      one-click App creation
    service.py       FastAPI wiring
"""

from __future__ import annotations
