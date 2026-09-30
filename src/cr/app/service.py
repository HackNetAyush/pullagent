"""The HTTP service: webhook ingress, setup flow, and the dashboard.

The ingress does four things and nothing else — verify, dedup, enqueue, 202 —
because GitHub gives a webhook ten seconds and a review takes minutes. Every
decision that can be made without I/O is made in `events.decide()`, and
everything that costs money happens on the queue.

Mounted alongside the existing read-only dashboard API, so one process serves
both and a review triggered by a webhook shows up live on the same page as one
started from a terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from cr.app import accounts, conversation, events, jobs, manifest
from cr.app.auth import AppAuth, AuthError
from cr.app.authroutes import build_auth_router
from cr.app.jobs import JobQueue, QueuedJob, index_key, reply_key, review_key
from cr.app.runner import run_index_job, run_review_job
from cr.config import Settings
from cr.config import settings as default_settings
from cr.server import guard as dashboard_guard
from cr.store import db as store

log = logging.getLogger(__name__)

# Repos to index up front when an App is installed. Indexing is minutes per
# large repo; an org install granting 300 repos must not become a 6-hour
# stampede. The rest index lazily on their first review.
MAX_EAGER_INDEX = 20


class AppService:
    """Everything the ingress needs, in one object with a lifecycle.

    Holding this on the FastAPI app rather than in module globals is what lets
    tests build a service with a fake `AppAuth` and no network at all.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings
        self.setup = manifest.SetupFlow()
        self.queue = self._build_queue()
        self._auth: AppAuth | None = None
        self.public_url: str = ""
        # Where GitHub POSTs events, when that is not this server's own
        # host (a smee.io-style relay). Blank means "<public_url>/webhook".
        self.webhook_url: str = ""
        self.started = False
        # asyncio holds only a weak reference to a running task, so a
        # fire-and-forget background job can be garbage-collected mid-flight.
        self._background: set[asyncio.Task] = set()

    def _build_queue(self):
        """In-process by default; Service Bus when one is configured.

        The role matters as much as the backend: a `web` replica must produce
        without consuming, or ingress containers would compete with workers for
        the same messages and a review would land on a box with no git mirror.
        """
        s = self.settings
        if not s.servicebus_connection:
            if s.app_role == "worker":
                log.warning(
                    "CR_APP_ROLE=worker without CR_SERVICEBUS_CONNECTION — there is no "
                    "shared queue to consume from, so this process will do nothing"
                )
            return JobQueue(self._handle, concurrency=s.app_max_concurrent_reviews)

        from cr.app.busqueue import ServiceBusQueue

        return ServiceBusQueue(
            self._handle,
            connection_string=s.servicebus_connection,
            queue_name=s.servicebus_queue,
            concurrency=s.app_max_concurrent_reviews,
            consume=s.app_role in ("worker", "all"),
        )

    # --- credentials --------------------------------------------------------

    def auth(self) -> AppAuth:
        """The App credentials, built on first use.

        Lazily, because the setup flow can configure the App while this
        process is already running — and then this returns a working AppAuth
        without a restart.
        """
        if self._auth is None:
            self._auth = AppAuth.from_settings(self.settings)
        return self._auth

    def _forget(self, installation_id: int) -> None:
        """Drop cached tokens for an installation that lost access.

        Guarded: an uninstall webhook can arrive on a server that has no
        credentials loaded, and failing to clear a cache must not 500 the
        ingress.
        """
        with contextlib.suppress(AuthError):
            self.auth().forget(installation_id)

    def load_saved_credentials(self) -> bool:
        creds = manifest.load_credentials(self.settings.credentials_path())
        if not creds:
            return False
        manifest.apply_credentials(creds, self.settings)
        self._auth = None
        return True

    @property
    def configured(self) -> bool:
        return self.settings.app_configured()

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self.started:
            return
        store.init()
        if not self.configured:
            self.load_saved_credentials()
        await self.queue.start()
        store.prune_deliveries()
        self.started = True

        if self.configured:
            self.spawn(self._sync_installations())
        else:
            log.warning(
                "no GitHub App configured — open /app/setup to create one, "
                "or set CR_GITHUB_APP_ID and CR_GITHUB_APP_PRIVATE_KEY"
            )

    def spawn(self, coro) -> None:
        """Run a background coroutine, holding a strong reference to it."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def stop(self) -> None:
        await self.queue.stop()
        if self._auth is not None:
            await self._auth.aclose()
        self.started = False

    async def _sync_installations(self) -> None:
        """Reconcile with GitHub at start-up.

        Installation webhooks that arrived while this server was down are
        gone; without this the App would not know which repos it owns until
        someone opened a PR.
        """
        try:
            for inst in await self.auth().list_installations():
                iid = inst["id"]
                account = (inst.get("account") or {}).get("login", "")
                repos = await self.auth().list_installation_repos(iid)
                store.upsert_installation(
                    iid,
                    account=account,
                    account_type=(inst.get("account") or {}).get("type", ""),
                    repos=repos,
                    repo_selection=inst.get("repository_selection", ""),
                    suspended=bool(inst.get("suspended_at")),
                    removed=False,
                )
                # Make the account visible to an admin without approving it.
                # An install we never recorded is one nobody can act on: the
                # owner sees silence and the approval queue stays empty.
                if account:
                    store.record_account(
                        account, account_type=(inst.get("account") or {}).get("type", "")
                    )
                log.info(
                    "installation %s (%s): %d repo(s) [%s]",
                    iid,
                    account,
                    len(repos),
                    store.account_status(account) if account else "no account",
                )
        except Exception as e:  # noqa: BLE001 - start-up reconciliation is best-effort
            log.warning("could not sync installations: %s", e)

    # --- the worker ---------------------------------------------------------

    async def _handle(self, job: QueuedJob) -> None:
        """Dispatch one queued job. Runs on the queue's worker, not the ingress."""
        auth = self.auth()
        match job.kind:
            case "review":
                # The row id travels with the payload so the runner can check,
                # right before it posts, that this review is still wanted.
                outcome = await run_review_job(
                    {**job.payload, "row_id": job.row_id}, auth, settings=self.settings
                )
                if outcome.skipped:
                    log.info(
                        "skipped %s#%s: %s",
                        outcome.repo,
                        outcome.pr_number,
                        outcome.skipped,
                    )
                elif outcome.error:
                    # Surfaced on the PR's check run already; raise so the job
                    # row records the failure too.
                    raise RuntimeError(outcome.error)
            case "reply":
                result = await conversation.handle_reply(job.payload, auth, settings=self.settings)
                log.info("reply on %s: %s", job.key, result.verdict or result.skipped)
            case "command":
                result = await conversation.handle_command(
                    job.payload, auth, settings=self.settings
                )
                log.info("command %s: %s", job.payload.get("command"), result.verdict)
            case "index":
                await run_index_job(job.payload, auth)
            case _:
                log.warning("unknown job kind %r", job.kind)

    # --- routing ------------------------------------------------------------

    def dispatch(self, decision: events.Decision) -> list[str]:
        """Turn a routing decision into queued work. Returns what was queued."""
        queued: list[str] = []
        s = self.settings

        match decision.action:
            case "review":
                key = review_key(decision.repo, decision.pr_number or 0)
                self.queue.submit(
                    "review",
                    key,
                    payload=_review_payload(decision),
                    repo=decision.repo,
                    installation_id=decision.installation_id,
                    # A push is debounced; opening a PR is not — nobody opens
                    # the same PR four times in a minute.
                    delay_s=s.app_debounce_s if not decision.full_review else 0.0,
                )
                queued.append(key)

            case "reply":
                key = reply_key(decision.repo, decision.comment_id or 0)
                self.queue.submit(
                    "reply",
                    key,
                    payload={
                        "repo": decision.repo,
                        "pr_number": decision.pr_number,
                        "installation_id": decision.installation_id,
                        "comment_id": decision.comment_id,
                        "sender": decision.sender,
                    },
                    repo=decision.repo,
                    installation_id=decision.installation_id,
                )
                queued.append(key)

            case "command":
                queued += self._dispatch_command(decision)

            case "sync_install":
                queued += self._dispatch_install(decision)

        return queued

    def _dispatch_command(self, d: events.Decision) -> list[str]:
        queued: list[str] = []
        # `review` and `incremental` both mean "look again now" — no debounce,
        # because a human is watching, and full_review distinguishes the scope.
        if d.command in ("review", "incremental"):
            key = review_key(d.repo, d.pr_number or 0)
            self.queue.submit(
                "review",
                key,
                payload={**_review_payload(d), "full_review": d.command == "review"},
                repo=d.repo,
                installation_id=d.installation_id,
            )
            queued.append(key)

        key = reply_key(d.repo, d.comment_id or 0)
        self.queue.submit(
            "command",
            key,
            payload={
                "repo": d.repo,
                "pr_number": d.pr_number,
                "installation_id": d.installation_id,
                "comment_id": d.comment_id,
                "command": d.command,
                "command_args": d.command_args,
                "sender": d.sender,
                "in_thread": d.in_thread,
            },
            repo=d.repo,
            installation_id=d.installation_id,
            # Never cancel an in-flight command for the same comment: two
            # different commands can legitimately arrive together.
            cancel_running=False,
        )
        queued.append(key)
        return queued

    def _dispatch_install(self, d: events.Decision) -> list[str]:
        if d.installation_id is None:
            return []

        if d.access_removed:
            # Deselecting some repos names them; uninstalling names none.
            if d.repos:
                store.drop_installation_repos(d.installation_id, list(d.repos))
            else:
                store.upsert_installation(d.installation_id, removed=True)
            self._forget(d.installation_id)
            return []

        store.upsert_installation(
            d.installation_id,
            account=d.sender,
            repos=list(d.repos),
            suspended=d.suspended,
        )
        if d.suspended:
            # A suspended App must not keep using a token it already holds.
            self._forget(d.installation_id)
            return []

        if not self.settings.app_index_on_install:
            return []

        queued = []
        for repo in list(d.repos)[:MAX_EAGER_INDEX]:
            key = index_key(repo)
            self.queue.submit(
                "index",
                key,
                payload={"repo": repo, "installation_id": d.installation_id},
                repo=repo,
                installation_id=d.installation_id,
            )
            queued.append(key)
        if len(d.repos) > MAX_EAGER_INDEX:
            log.info(
                "indexing the first %d of %d repo(s) now; the rest index on their first review",
                MAX_EAGER_INDEX,
                len(d.repos),
            )
        return queued


def _review_payload(d: events.Decision) -> dict[str, Any]:
    return {
        "repo": d.repo,
        "pr_number": d.pr_number,
        "installation_id": d.installation_id,
        "head_sha": d.head_sha,
        "base_sha": d.base_sha,
        "before_sha": d.before_sha,
        "full_review": d.full_review,
        "is_fork": d.is_fork,
        "trigger": d.reason,
    }


# --- routes ------------------------------------------------------------------


def build_router(service: AppService) -> APIRouter:
    router = APIRouter()
    s = service.settings

    @router.post("/webhook")
    async def webhook(request: Request) -> Response:
        """Verify, dedup, enqueue, 202. Nothing here may block."""
        body = await request.body()
        try:
            events.verify_signature(
                s.github_webhook_secret, body, request.headers.get(events.SIGNATURE_HEADER)
            )
        except events.SignatureError as e:
            # 401, not 400: this is an authentication failure, and GitHub's
            # delivery log should say so plainly.
            log.warning("rejected a webhook delivery: %s", e)
            return JSONResponse({"error": str(e)}, status_code=401)

        event = request.headers.get(events.EVENT_HEADER, "")
        delivery = request.headers.get(events.DELIVERY_HEADER, "")
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, status_code=400)

        decision = events.decide(event, payload, events.Policy.from_settings(s))
        if not decision.acts:
            return JSONResponse({"ok": True, "ignored": decision.reason}, status_code=200)

        # Before claiming the delivery: a 503 here consumes the delivery id,
        # and the operator's manual redelivery after fixing the config would
        # then be rejected as a duplicate.
        if not service.configured:
            return JSONResponse(
                {"error": "the App is not configured on this server"}, status_code=503
            )

        # The allowlist gate. This is the last point before work is queued, and
        # everything past it costs money, so a public App has to answer here.
        # Install events are exempt: they are how an account first appears, and
        # refusing them would mean nobody could ever show up to be approved.
        login, account_type = accounts.account_for(payload)
        if (
            s.app_require_approval
            and decision.action != "sync_install"
            and not accounts.allowed(login)
        ):
            store.note_blocked_event(login, account_type)
            log.info("blocked %s for %s: account not approved", event, login or "?")
            return JSONResponse({"ok": True, "blocked": "account is not approved"}, status_code=200)

        if delivery and not store.mark_delivery(
            delivery, event=event, action=payload.get("action", ""), repo=decision.repo
        ):
            log.info("duplicate delivery %s (%s); already handled", delivery, event)
            return JSONResponse({"ok": True, "duplicate": True}, status_code=200)

        queued = service.dispatch(decision)
        log.info(
            "%s %s -> %s%s",
            event,
            decision.repo or decision.installation_id,
            decision.action,
            f" ({', '.join(queued)})" if queued else "",
        )
        return JSONResponse(
            {"ok": True, "action": decision.action, "queued": queued}, status_code=202
        )

    # The two reads below leak repository names, installation accounts and job
    # keys, so they sit behind the same guard as the rest of the dashboard.
    # /webhook and the setup routes stay open: GitHub has no session, and
    # setup runs before any user exists.
    @router.get("/api/app/status", dependencies=[Depends(dashboard_guard)])
    def status() -> dict[str, Any]:
        insts = store.installations()
        return {
            "configured": service.configured,
            "app_id": s.github_app_id or "",
            "slug": s.github_app_slug or "",
            "webhook_secret_set": bool(s.github_webhook_secret),
            "public_url": service.public_url,
            "webhook_url": service.webhook_url or f"{service.public_url}/webhook",
            "incremental": s.app_incremental,
            "review_forks": s.app_review_forks,
            "review_drafts": s.app_review_drafts,
            "debounce_s": s.app_debounce_s,
            "installations": [
                {
                    "id": i.id,
                    "account": i.account,
                    "repos": i.repos,
                    "suspended": i.suspended,
                }
                for i in insts
            ],
            "queue": service.queue.snapshot(),
        }

    @router.get("/api/app/jobs", dependencies=[Depends(dashboard_guard)])
    def recent_jobs(
        limit: int = 25,
        offset: int = 0,
        kind: str | None = None,
        status: str | None = None,
        repo: str | None = None,
    ) -> dict[str, Any]:
        rows, total = store.jobs_page(
            limit=limit, offset=offset, kind=kind, status=status, repo=repo
        )
        return {
            "items": [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "key": j.key,
                    "repo": j.repo,
                    "status": j.status,
                    "attempts": j.attempts,
                    "error": j.error,
                    "created_at": j.created_at.isoformat() if j.created_at else None,
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                    "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                }
                for j in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    # --- setup ---------------------------------------------------------------

    def setup_blocked() -> Response | None:
        if not s.app_allow_setup:
            return HTMLResponse("<h1>Setup is disabled</h1>", status_code=403)
        if service.configured:
            return HTMLResponse(
                "<h1>Already configured</h1><p>This server already has App credentials. "
                "Delete them, or set <code>CR_APP_ALLOW_SETUP=false</code>.</p>",
                status_code=409,
            )
        return None

    @router.get("/app/setup", response_class=HTMLResponse)
    def setup(request: Request, org: str = "", name: str = "CR code review") -> Response:
        if (blocked := setup_blocked()) is not None:
            return blocked
        public = service.public_url or str(request.base_url).rstrip("/")
        page = manifest.setup_page(
            public,
            service.setup.issue_state(),
            webhook_url=service.webhook_url,
            name=name,
        )
        if org:
            page = page.replace(
                "https://github.com/settings/apps/new?",
                f"https://github.com/organizations/{org}/settings/apps/new?",
            )
        return HTMLResponse(page)

    @router.get("/app/setup/callback", response_class=HTMLResponse)
    async def setup_callback(code: str = "", state: str = "") -> Response:
        if (blocked := setup_blocked()) is not None:
            return blocked
        try:
            service.setup.consume_state(state)
            if not code:
                raise manifest.SetupError("GitHub did not send a code")
            data = await manifest.exchange_code(code)
        except manifest.SetupError as e:
            return HTMLResponse(f"<h1>Setup failed</h1><p>{e}</p>", status_code=400)

        path = manifest.save_credentials(data, s.credentials_path())
        service.load_saved_credentials()
        creds = manifest.load_credentials(path) or {}
        # Pick up the installations this App already has, if any.
        service.spawn(service._sync_installations())
        return HTMLResponse(manifest.done_page(creds, path))

    @router.get("/app/install")
    def install() -> Response:
        """Shortcut to the install page, once the App exists."""
        if s.github_app_slug:
            return RedirectResponse(
                f"https://github.com/apps/{s.github_app_slug}/installations/new"
            )
        return RedirectResponse("/app/setup")

    return router


# --- assembly ----------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    public_url: str = "",
    webhook_url: str = "",
    with_dashboard: bool = True,
    service: AppService | None = None,
) -> FastAPI:
    """Build the whole service. `cr app serve` and the tests both use this.

    Passing `service` in is what lets a test drive the real ingress, queue and
    runner with a fake `AppAuth` — the alternative is mocking the queue, which
    would stop testing the parts that actually break.
    """
    service = service or AppService(settings)
    service.public_url = public_url.rstrip("/")
    service.webhook_url = webhook_url.rstrip("/")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    api = FastAPI(title="CR GitHub App", docs_url="/api/docs", lifespan=lifespan)
    api.state.service = service
    api.include_router(build_router(service))
    api.include_router(build_auth_router(lambda: service.settings, lambda: service.public_url))

    if with_dashboard:
        # The dashboard's routes are appended *after* ours on purpose: its SPA
        # catch-all is `/{path:path}`, which would otherwise swallow /webhook.
        # Its own docs routes are dropped — this app already has a set, and a
        # duplicate path shadows rather than merges.
        from fastapi.middleware.cors import CORSMiddleware

        from cr.server import app as dashboard

        own = {"/openapi.json", "/api/docs", "/api/redoc", "/redoc", "/docs/oauth2-redirect"}
        for route in dashboard.routes:
            if getattr(route, "path", "") not in own:
                api.router.routes.append(route)

        api.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    return api


def check_ready(settings: Settings | None = None) -> list[str]:
    """Problems that would make `cr app serve` useless. Reported before binding."""
    s = settings or default_settings
    problems: list[str] = []
    if not s.app_configured():
        creds = manifest.load_credentials(s.credentials_path())
        if creds:
            manifest.apply_credentials(creds, s)
    if not s.app_configured():
        problems.append(
            "no App credentials — open /app/setup once the server is running, or set "
            "CR_GITHUB_APP_ID and CR_GITHUB_APP_PRIVATE_KEY"
        )
    elif not s.github_webhook_secret:
        problems.append(
            "CR_GITHUB_WEBHOOK_SECRET is not set — unsigned deliveries are rejected, so "
            "no webhook will ever be processed"
        )
    if s.app_require_approval:
        # Failing closed is correct, but failing closed *silently* is not: the
        # symptom is "the bot stopped working" with nothing anywhere saying why.
        try:
            if not store.accounts("approved") and store.installations():
                problems.append(
                    "CR_APP_REQUIRE_APPROVAL is on and no account is approved yet — every "
                    "webhook will be dropped. Run `cr app approve --all-installed`, or "
                    "approve from the dashboard once signed in"
                )
            if accounts.configured(s) and not store.admin_count():
                problems.append(
                    "no administrator exists yet — set CR_ADMIN_LOGINS to your GitHub "
                    "login and sign in once, or nobody can approve anything"
                )
        except Exception as e:  # noqa: BLE001 - a readiness check must not crash start-up
            log.debug("approval readiness check skipped: %s", e)
    if s.provider == "foundry" and not s.azure_api_key:
        problems.append("CR_PROVIDER=foundry but CR_AZURE_API_KEY is not set")
    if s.provider == "anthropic" and not s.anthropic_api_key:
        problems.append("CR_PROVIDER=anthropic but CR_ANTHROPIC_API_KEY is not set")
    return problems


def reloadable_app() -> FastAPI:
    """Factory for `uvicorn --reload`, which needs an import string rather than
    an object. The public URL travels by env var because there is nowhere else
    to put it when uvicorn re-imports this module in a fresh process."""
    import os

    return create_app(
        public_url=os.environ.get("CR_APP_PUBLIC_URL", ""),
        webhook_url=os.environ.get("CR_APP_WEBHOOK_URL", ""),
    )


__all__ = [
    "AppService",
    "AuthError",
    "check_ready",
    "create_app",
    "jobs",
    "reloadable_app",
]
