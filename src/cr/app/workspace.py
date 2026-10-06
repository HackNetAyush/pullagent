"""Workspaces: an account's provider connections, custom tiers, and routing.

A workspace is a GitHub account — a person or an organisation — the same unit
the allowlist approves and the webhook routes by. A signed-in user may manage
their own plus the organisations they belonged to at their last sign-in
(administrators may manage any).

A **connection** is one provider account the customer brings: an API key, the
Azure resource name where the provider needs one, and the models they want to
use through it. Model names are theirs to choose — a catalog model, an Azure
deployment name, a fine-tune — so capabilities are learned by testing, not
assumed from a list.

Rules:

1. **A key goes in and never comes out.** Encrypted on save, decrypted only
   to build a provider client; the dashboard sees its last four characters.
   Errors that might echo it are scrubbed before they are shown or stored.
2. **Nothing is saved untested.** A connection is stored only with a receipt
   from a connection test that passed for every model on it — issued by the
   server, encrypted, and bound to the exact key, resource and model names
   tested. A client cannot skip the test by calling the API directly.
3. **Custom tiers run only on the customer's own connections.** Until
   per-account spend limits exist, a tier on CR's managed models would be an
   unmetered tap on our budget.
4. **CR's own providers are never described.** Managed presets are shown by
   what they do, never by which model or endpoint serves them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cr import vault
from cr.config import TIERS, LensRoute, Settings, TierConfig
from cr.llm.client import ClientPool, LLMClient
from cr.llm.registry import CATALOG, PROVIDERS, ModelSpec, custom_spec, validate_resource
from cr.llm.transports import anthropic_client, openai_client
from cr.review.prompts import SPECIALISTS, VERIFIER_LENSES
from cr.store import db as store

log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high", "xhigh", "max"]

# The managed presets triage chooses between, described to customers by what
# they do. T4 is an opt-in experiment for operators, never shown.
SLOTS: tuple[str, ...] = ("T1", "T2", "T3")
# Owner and name as GitHub allows them.
_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")

SLOT_INFO: dict[str, dict[str, str]] = {
    "T1": {"label": "Small changes", "when": "A few hunks in a few files."},
    "T2": {"label": "Standard changes", "when": "Most pull requests."},
    "T3": {
        "label": "Deep review",
        "when": "Auth, payments, migrations, concurrency, or very large diffs.",
    },
}

FINDER_INFO: dict[str, str] = {
    "correctness": "Off-by-one errors, null handling, inverted conditions, wrong variables.",
    "security": "Injection, missing authorization, secrets, unsafe deserialisation, SSRF.",
    "concurrency": "Races, non-atomic updates, deadlocks, ordering assumptions.",
    "api_contract": "Changed signatures, return types and formats that break callers.",
    "test_coverage": "Changed behaviour that no test exercises, or tests that assert the bug.",
    "performance": "N+1 queries, unbounded results, accidental quadratic work.",
    "state_and_security": "State transitions, retries, cleanup and authorization boundaries.",
}
VERIFIER_INFO: dict[str, str] = {
    "correctness": "Does the code actually do what the finding claims?",
    "reachability": "Can the failure be reached from a real entry point?",
    "evidence": "Do the cited lines support the claim, or is it assumed?",
}
assert set(FINDER_INFO) == set(SPECIALISTS)
assert set(VERIFIER_INFO) == set(VERIFIER_LENSES)

# What to type in the "models" field, per provider.
MODEL_HINTS: dict[str, str] = {
    "anthropic": "Claude model IDs, e.g. claude-sonnet-5-5",
    "foundry": "Your Foundry deployment names",
    "openai": "OpenAI model IDs, e.g. gpt-6-sol",
    "azure_openai": "Your Azure OpenAI deployment names",
    "openrouter": "OpenRouter model IDs, e.g. google/gemini-3.8-flash",
    "groq": "Groq model IDs, e.g. openai/gpt-oss-120b",
    "nvidia": "NVIDIA model IDs, e.g. openai/gpt-oss-20b",
}

MAX_COMMENTS = 25
MAX_MODELS = 10
KEY_MIN, KEY_MAX = 8, 512
# A receipt outlives a slow form, not a coffee break.
RECEIPT_TTL_S = 30 * 60
# Model and deployment IDs: letters, digits and the separators providers use.
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


class WorkspaceError(ValueError):
    """A request that cannot be honoured, with a message fit to show the user."""


class Forbidden(PermissionError):
    pass


class RateLimited(WorkspaceError):
    """Too many connection tests for this account in the last hour."""


# --- who may manage what --------------------------------------------------------


class Actor(BaseModel):
    login: str
    # A CR administrator: may view and manage every workspace.
    is_admin: bool = False
    orgs: list[str] = Field(default_factory=list)
    admin_orgs: list[str] = Field(default_factory=list)


def role_in(actor: Actor, account: str) -> str | None:
    """ "owner" (their own account), "admin", "member", or None for no access.
    CR administrators are "admin" everywhere."""
    a = account.lower()
    if a == actor.login.lower():
        return "owner"
    if a in {o.lower() for o in actor.admin_orgs}:
        return "admin"
    if a in {o.lower() for o in actor.orgs}:
        return "member"
    return "admin" if actor.is_admin else None


def can_view(actor: Actor, account: str) -> bool:
    return role_in(actor, account) is not None


def can_manage(actor: Actor, account: str) -> bool:
    """Keys, tiers, routing and guidelines change what an account pays and how
    its code is reviewed: the owner and org admins only."""
    return role_in(actor, account) in ("owner", "admin")


def require_view(actor: Actor, account: str) -> None:
    if not can_view(actor, account):
        raise Forbidden(f"you are not a member of {account}")


def require_manage(actor: Actor, account: str) -> None:
    if not can_manage(actor, account):
        raise Forbidden(f"only {account}'s owners and admins can change this")


def workspaces_for(actor: Actor) -> list[dict[str, Any]]:
    """The accounts this actor may see, own account first, each with the
    actor's role in it and whether the App is installed there."""
    installed = {i.account.lower() for i in store.installations() if not i.removed}
    names = [actor.login] if actor.login != "local" else []
    names += sorted(actor.orgs, key=str.lower)
    if actor.is_admin:
        names += store.known_accounts()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        role = role_in(actor, name) or "member"
        out.append(
            {
                "login": name,
                "kind": "personal" if name.lower() == actor.login.lower() else "organization",
                "role": role,
                "can_manage": role in ("owner", "admin"),
                "installed": name.lower() in installed,
            }
        )
    return out


def vault_ready(settings: Settings) -> bool:
    try:
        vault.encrypt(settings, "probe")
    except vault.VaultUnavailable:
        return False
    return True


# --- connections ------------------------------------------------------------------


def conn_ref(conn_id: int, model: str) -> str:
    """How a tier names a model on a connection."""
    return f"conn:{conn_id}:{model}"


def parse_ref(ref: str) -> tuple[int, str] | None:
    head, _, rest = ref.partition(":")
    if head != "conn" or ":" not in rest:
        return None
    conn_id, _, model = rest.partition(":")
    return (int(conn_id), model) if conn_id.isdigit() and model else None


class ModelPrice(BaseModel):
    """A customer's own price for a model, USD per million tokens."""

    input: float = Field(ge=0, le=1000)
    output: float = Field(ge=0, le=1000)


class ConnectionDraft(BaseModel):
    """A connection as the dialog submits it, for testing or saving."""

    provider: str
    # Blank when editing a saved connection: keep the stored key.
    api_key: str = Field(default="", max_length=KEY_MAX)
    resource: str = ""
    label: str = Field(default="", max_length=64)
    models: list[str] = Field(min_length=1, max_length=MAX_MODELS)
    # Optional per-model prices, keyed by model name. Only tracked spend
    # depends on these, so changing them never needs a new connection test.
    prices: dict[str, ModelPrice | None] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if v not in PROVIDERS:
            raise ValueError(f"unknown provider {v!r}")
        return v

    @field_validator("models")
    @classmethod
    def _models(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            name = raw.strip()
            if not _MODEL_NAME.fullmatch(name):
                raise ValueError(f"{raw!r} is not a valid model name")
            if name not in out:
                out.append(name)
        return out

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return " ".join(v.split())


def _key_fp(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _clean_key(key: str) -> str:
    k = (key or "").strip()
    if not KEY_MIN <= len(k) <= KEY_MAX or any(ch.isspace() for ch in k):
        raise WorkspaceError("that does not look like an API key")
    return k


def _effective(
    settings: Settings, account: str, draft: ConnectionDraft, conn_id: int | None
) -> tuple[str, str]:
    """The key and resource a draft means: what it carries, falling back to the
    saved connection's when editing without re-entering them."""
    saved = None
    if conn_id is not None:
        saved = store.connection(account, conn_id)
        if saved is None:
            raise WorkspaceError("that connection does not exist")
        if saved.provider != draft.provider:
            raise WorkspaceError("a connection's provider cannot be changed; add a new one")
    if draft.api_key.strip():
        key = _clean_key(draft.api_key)
    elif saved is not None:
        key = vault.decrypt(settings, saved.ciphertext)
    else:
        raise WorkspaceError("enter the API key")

    resource = ""
    if PROVIDERS[draft.provider].needs_resource:
        try:
            resource = validate_resource(draft.resource or (saved.resource if saved else ""))
        except ValueError as e:
            raise WorkspaceError(str(e)) from e
    return key, resource


def _provider_client(provider: str, key: str, resource: str) -> Any:
    if provider == "anthropic":
        return anthropic_client("anthropic", api_key=key)
    if provider == "foundry":
        return anthropic_client("foundry", api_key=key, resource=resource)
    return openai_client(provider, api_key=key, base_url=PROVIDERS[provider].url(resource or None))


# SDK error class names and status codes, mapped to something a person can act
# on. Anything unmapped falls through with the provider's own message, trimmed.
_EXPLANATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("AuthenticationError", "Error code: 401"),
        "The provider rejected this key. Check it was copied in full and is still active.",
    ),
    (("PermissionDeniedError", "Error code: 403"), "This key has no access to {model}."),
    (("NotFoundError", "Error code: 404", "DeploymentNotFound"), "{missing}"),
    (
        ("RateLimitError", "Error code: 429"),
        "The provider is rate-limiting this key, or it has run out of credit.",
    ),
    (("APIConnectionError", "APITimeoutError"), "Could not reach the provider."),
    (
        ("response_format", "json_schema", "output_format", "structured output"),
        "{model} does not support schema-checked JSON output, which reviews need.",
    ),
)


def _explain(errors: list[str], model: str, provider: str) -> str:
    raw = "; ".join(errors) or "No structured answer came back."
    missing = (
        f"There is no deployment named {model} on this resource."
        if PROVIDERS[provider].needs_resource
        else f"{model} was not found. Check the model ID."
    )
    for markers, message in _EXPLANATIONS:
        if any(m in raw for m in markers):
            return message.format(model=model, missing=missing)
    return f"The test call failed: {raw[:240]}"


def _effort_rejected(errors: list[str]) -> bool:
    raw = " ".join(errors).lower()
    return ("400" in raw or "badrequest" in raw) and ("reasoning" in raw or "effort" in raw)


async def _probe_model(provider: str, key: str, resource: str, model: str) -> dict[str, Any]:
    """One structured call on one model. If the model rejects the effort
    parameter, retry without it and remember that it takes none."""
    from cr.doctor import probe  # noqa: PLC0415 - doctor imports config at module load

    async def attempt(effort: bool | None) -> tuple[Any, ModelSpec, float]:
        spec = custom_spec(provider, model, endpoint="probe", effort=effort)
        client = LLMClient(
            pool=ClientPool(None, factory=lambda _key: _provider_client(provider, key, resource)),
            resolver=lambda _ref: spec,
            max_concurrency=1,
        )
        started = time.monotonic()
        report = await probe(client, model, check_cache=False)
        return report, spec, time.monotonic() - started

    report, spec, elapsed = await attempt(None)
    if not report.healthy and spec.supports_effort and _effort_rejected(report.errors):
        report, spec, elapsed = await attempt(False)

    ok = report.healthy
    return {
        "model": model,
        "label": spec.label,
        "listed": spec.known,
        "ok": ok,
        "effort": spec.supports_effort,
        "latency_ms": round(elapsed * 1000),
        "cost_usd": round(report.cost_usd, 6),
        "detail": "Connected. Returns schema-checked JSON."
        if ok
        else vault.scrub(_explain(report.errors, model, provider), [key]),
    }


async def test_connection(
    settings: Settings, account: str, draft: ConnectionDraft, *, conn_id: int | None = None
) -> dict[str, Any]:
    """Test every model on a draft connection with one small structured call
    each, on the customer's own credit. Returns per-model results and, when
    every model passed, a receipt the save call must present."""
    since = datetime.now(UTC) - timedelta(hours=1)
    if store.key_tests_since(account, since) >= settings.key_test_limit_per_hour:
        raise RateLimited(
            f"at most {settings.key_test_limit_per_hour} connection tests per hour; try again later"
        )
    key, resource = _effective(settings, account, draft, conn_id)
    store.note_key_test(account)

    gate = asyncio.Semaphore(4)

    async def one(model: str) -> dict[str, Any]:
        async with gate:
            return await _probe_model(draft.provider, key, resource, model)

    results = list(await asyncio.gather(*(one(m) for m in draft.models)))
    all_ok = all(r["ok"] for r in results)
    receipt = None
    if all_ok:
        receipt = vault.encrypt(
            settings,
            json.dumps(
                {
                    "kind": "connection-test",
                    "account": account.lower(),
                    "provider": draft.provider,
                    "resource": resource,
                    "key": _key_fp(key),
                    "models": {r["model"]: {"effort": r["effort"]} for r in results},
                },
                sort_keys=True,
            ),
        )
    return {"ok": all_ok, "results": results, "receipt": receipt}


def _needs_test(account: str, draft: ConnectionDraft, conn_id: int | None) -> bool:
    """A new connection, a new key, a new resource or a model that was never
    tested needs a passing test. Renaming, re-pricing or dropping models does
    not: nothing about what gets called changed."""
    if conn_id is None or draft.api_key.strip():
        return True
    saved = store.connection(account, conn_id)
    if saved is None:
        return True
    tested = {m["name"] for m in saved.models or []}
    same_resource = not PROVIDERS[draft.provider].needs_resource or (
        (draft.resource or saved.resource).strip().lower() == saved.resource
    )
    return not same_resource or any(m not in tested for m in draft.models)


def save_connection(
    settings: Settings,
    account: str,
    draft: ConnectionDraft,
    receipt: str,
    *,
    conn_id: int | None = None,
    by: str = "",
) -> dict[str, Any]:
    """Store a connection. Anything that changes what gets called — the key,
    the resource, a new model — must come with a receipt from a passing test
    of exactly that; renames, prices and removals need none."""
    key, resource = _effective(settings, account, draft, conn_id)
    unknown = set(draft.prices) - set(draft.models)
    if unknown:
        raise WorkspaceError(f"a price was given for {sorted(unknown)[0]}, which is not listed")

    saved = store.connection(account, conn_id) if conn_id is not None else None
    effort = {m["name"]: bool(m.get("effort")) for m in (saved.models if saved else []) or []}
    tested_at = {m["name"]: m.get("tested_at") for m in (saved.models if saved else []) or []}
    needs_test = _needs_test(account, draft, conn_id)
    if needs_test:
        try:
            payload = json.loads(vault.decrypt(settings, receipt or "", ttl=RECEIPT_TTL_S))
        except (vault.SecretUnreadable, json.JSONDecodeError) as e:
            raise WorkspaceError("test the connection before saving it") from e
        if (
            payload.get("kind") != "connection-test"
            or payload.get("account") != account.lower()
            or payload.get("provider") != draft.provider
            or payload.get("resource") != resource
            or payload.get("key") != _key_fp(key)
            or any(m not in payload.get("models", {}) for m in draft.models)
        ):
            raise WorkspaceError("the connection changed since it was tested; test it again")
        now = datetime.now(UTC).isoformat()
        effort = {m: bool(payload["models"][m].get("effort")) for m in draft.models}
        tested_at = dict.fromkeys(draft.models, now)

    models = []
    for m in draft.models:
        entry: dict[str, Any] = {
            "name": m,
            "effort": effort.get(m, False),
            "tested_at": tested_at.get(m),
        }
        price = draft.prices.get(m)
        if price is not None:
            entry["price"] = {"input": price.input, "output": price.output}
        models.append(entry)
    p = PROVIDERS[draft.provider]
    label = (
        draft.label
        or (saved.label if saved else "")
        or (f"{p.label} · {resource}" if resource else p.label)
    )[:64]
    # Re-encrypt only a key that was entered; an edit without one keeps it.
    new_key = bool(draft.api_key.strip()) or conn_id is None
    try:
        row = store.save_connection(
            account,
            provider=draft.provider,
            label=label,
            models=models,
            ciphertext=vault.encrypt(settings, key) if new_key else None,
            hint=vault.hint(key),
            resource=resource,
            conn_id=conn_id,
            created_by=by,
            tested=needs_test,
        )
    except LookupError as e:
        raise WorkspaceError("that connection does not exist") from e
    log.info("connection %s saved for %s (%s) by %s", row.id, account, draft.provider, by)
    return _connection_json(row, {}, {})


def _routed_tier_ids(account: str) -> set[int]:
    rules = store.routing_rules(account)
    return {t for t in [rules["all"], *rules["repos"].values()] if t is not None}


def delete_connection(account: str, conn_id: int) -> None:
    """Refuses while a routed tier still uses the connection: removing it
    would turn every review in those repositories into a failure."""
    assigned = _routed_tier_ids(account)
    blocking = [
        t.name
        for t in store.custom_tiers(account)
        if t.id in assigned
        and any(
            (parsed := parse_ref(ref)) and parsed[0] == conn_id
            for ref in TierSpec.model_validate(t.config).all_models()
        )
    ]
    if blocking:
        raise WorkspaceError(
            f"{', '.join(blocking)} uses this connection and is reviewing repositories. "
            "Route them elsewhere first."
        )
    if not store.delete_connection(account, conn_id):
        raise WorkspaceError("that connection does not exist")


def _entry_spec(provider: str, entry: dict[str, Any], *, endpoint: str = "") -> ModelSpec:
    price = entry.get("price")
    return custom_spec(
        provider,
        entry["name"],
        endpoint=endpoint,
        effort=bool(entry.get("effort")),
        price=(price["input"], price["output"]) if price else None,
    )


class _Connections:
    """An account's connections, decrypted lazily, as a resolver and client
    factory for one review run."""

    def __init__(self, settings: Settings, account: str) -> None:
        self.settings = settings
        self.rows = {c.id: c for c in store.connections(account)}

    def spec(self, ref: str) -> ModelSpec:
        parsed = parse_ref(ref)
        if parsed is None:
            raise ValueError(f"{ref} is not a model on one of your connections")
        conn_id, model = parsed
        row = self.rows.get(conn_id)
        entry = next((m for m in (row.models or []) if m["name"] == model), None) if row else None
        if row is None or entry is None:
            raise ValueError(f"{model} is not on any of your connections")
        return _entry_spec(row.provider, entry, endpoint=f"conn:{conn_id}")

    def client(self, endpoint: str) -> Any:
        conn_id = int(endpoint.split(":", 1)[1])
        row = self.rows[conn_id]
        return _provider_client(
            row.provider, vault.decrypt(self.settings, row.ciphertext), row.resource
        )


def secrets_for(settings: Settings, account: str) -> list[str]:
    """Every key a review for `account` could have used: its own connections'
    and this server's. What an error shown on a pull request must not repeat."""
    out = [
        v
        for k, v in settings.model_dump().items()
        if k.endswith("_api_key") and isinstance(v, str) and v
    ]
    for row in store.connections(account):
        try:
            out.append(vault.decrypt(settings, row.ciphertext))
        except (vault.VaultUnavailable, vault.SecretUnreadable):
            continue
    return out


def byok_client(settings: Settings, account: str, *, max_concurrency: int = 2) -> LLMClient:
    """An LLMClient that can only reach models on this account's connections.
    There is no fallback to the deployment's credentials."""
    conns = _Connections(settings, account)
    pool = ClientPool(None, factory=conns.client)
    return LLMClient(pool=pool, resolver=conns.spec, max_concurrency=max_concurrency)


# --- custom tiers -------------------------------------------------------------------


class StageChoice(BaseModel):
    model: str
    effort: Effort


class LensChoice(BaseModel):
    lens: str
    # None: use the stage's model / effort.
    model: str | None = None
    effort: Effort | None = None


MAX_CUSTOM_FINDERS = 3
MAX_CUSTOM_VERIFIERS = 2

# Wording that asks a finder to filter rather than to find. Current models
# follow it literally: they investigate as hard, find the bug, then stay
# silent about it (see prompts.py, rule 1). Flagged, not blocked - the
# instruction is the owner's - and the finder prompt restates the rule.
_FILTER_WORDING: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bonly\s+(report|flag|mention|comment|raise|surface)\b", re.I),
        '"Only report..." makes the model stay silent about real bugs it found.',
    ),
    (
        re.compile(
            r"\b(don'?t|do not|never|ignore|skip)\b[^.\n]{0,40}"
            r"\b(minor|small|low|nit|nits|trivial|style)\b",
            re.I,
        ),
        "Asking it to skip small issues drops findings; severity already ranks them.",
    ),
    (
        re.compile(r"\b(high|critical)[- ]severity\s+only\b|\bjust\s+(report|flag)\b", re.I),
        "Filtering by severity belongs to the verifier and the comment limit, not the finder.",
    ),
)


def filter_warnings(text: str) -> list[str]:
    return [why for pattern, why in _FILTER_WORDING if pattern.search(text or "")]


class CustomLens(BaseModel):
    """An owner-written lens: what to look for, in their own words."""

    id: str = Field(pattern=r"^custom_[a-z0-9_]{1,30}$")
    name: str = Field(min_length=1, max_length=40)
    instruction: str = Field(min_length=20, max_length=2000)
    # None: use the stage's model / effort.
    model: str | None = None
    effort: Effort | None = None

    @field_validator("name", "instruction")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class TierSpec(BaseModel):
    """A custom tier as the customer builds it. Stored verbatim; turned into a
    `TierConfig` only when a review runs, so a later fix to the conversion
    applies to tiers saved before it."""

    name: str = Field(min_length=1, max_length=40)
    finder: StageChoice
    verifier: StageChoice
    finders: list[LensChoice] = Field(default_factory=list, max_length=len(SPECIALISTS))
    verifiers: list[LensChoice] = Field(default_factory=list, max_length=len(VERIFIER_LENSES))
    custom_finders: list[CustomLens] = Field(default_factory=list, max_length=MAX_CUSTOM_FINDERS)
    custom_verifiers: list[CustomLens] = Field(
        default_factory=list, max_length=MAX_CUSTOM_VERIFIERS
    )
    max_comments: int = Field(default=8, ge=1, le=MAX_COMMENTS)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name is required")
        return v

    @model_validator(mode="after")
    def _lenses(self) -> TierSpec:
        for kind, chosen, allowed in (
            ("finder", self.finders, SPECIALISTS),
            ("verifier", self.verifiers, VERIFIER_LENSES),
        ):
            names = [c.lens for c in chosen]
            unknown = sorted(set(names) - set(allowed))
            if unknown:
                raise ValueError(f"unknown {kind} lens: {', '.join(unknown)}")
            if len(set(names)) != len(names):
                raise ValueError(f"each {kind} lens may appear once")
        if not self.finders and not self.custom_finders:
            raise ValueError("turn on at least one finder lens")
        if not self.verifiers and not self.custom_verifiers:
            raise ValueError("turn on at least one verifier lens")
        ids = [c.id for c in self.custom_finders + self.custom_verifiers]
        if len(set(ids)) != len(ids):
            raise ValueError("each custom lens needs its own id")
        return self

    def all_models(self) -> set[str]:
        out = {self.finder.model, self.verifier.model}
        everyone: list[Any] = [
            *self.finders,
            *self.verifiers,
            *self.custom_finders,
            *self.custom_verifiers,
        ]
        out |= {c.model for c in everyone if c.model}
        return out

    def warnings(self) -> dict[str, list[str]]:
        """Per custom finder lens, wording likely to cost recall."""
        out = {c.id: filter_warnings(c.instruction) for c in self.custom_finders}
        return {k: v for k, v in out.items() if v}


def tier_problems(spec: TierSpec, account: str) -> list[str]:
    """Why this tier cannot run right now, in words for the dashboard."""
    rows = {c.id: c for c in store.connections(account)}
    problems: list[str] = []
    for ref in sorted(spec.all_models()):
        parsed = parse_ref(ref)
        if parsed is None:
            problems.append(f"{ref}: choose a model from one of your connections")
            continue
        conn_id, model = parsed
        row = rows.get(conn_id)
        if row is None:
            problems.append(f"{model}: its connection was removed")
        elif model not in {m["name"] for m in row.models or []}:
            problems.append(f"{model} is no longer on {row.label}")
    return problems


def to_tier_config(spec: TierSpec, name: str = "custom") -> TierConfig:
    from cr.review import prompts  # noqa: PLC0415 - keeps this module import-light

    deep = {"xhigh", "max"}
    finders: list[Any] = [*spec.finders, *spec.custom_finders]
    verifiers: list[Any] = [*spec.verifiers, *spec.custom_verifiers]
    efforts = {spec.finder.effort} | {c.effort for c in finders if c.effort}

    def lens_id(c: Any) -> str:
        return c.id if isinstance(c, CustomLens) else c.lens

    return TierConfig(
        name=name,
        model=spec.finder.model,
        effort=spec.finder.effort,
        finders=[lens_id(c) for c in finders],
        verifier_lenses=[lens_id(c) for c in verifiers],
        finder_prompts={
            c.id: prompts.custom_finder_instruction(c.name, c.instruction)
            for c in spec.custom_finders
        },
        verifier_prompts={c.id: f"{c.name}: {c.instruction}" for c in spec.custom_verifiers},
        max_comments=spec.max_comments,
        verifier_model=spec.verifier.model,
        verifier_effort=spec.verifier.effort,
        # Deeper reasoning spends from the same budget as the answer; the
        # managed deep tier needed 64K for the same reason.
        finder_max_tokens=64_000 if efforts & deep else 32_000,
        finder_routes={
            lens_id(c): LensRoute(
                model=c.model or spec.finder.model, effort=c.effort or spec.finder.effort
            )
            for c in finders
            if c.model or c.effort
        },
        verifier_routes={
            lens_id(c): LensRoute(
                model=c.model or spec.verifier.model, effort=c.effort or spec.verifier.effort
            )
            for c in verifiers
            if c.model or c.effort
        },
        custom=True,
        label=spec.name,
    )


def attribution(account: str, cfg: TierConfig) -> str:
    """The line a review on the customer's own keys carries: which tier, which
    of their models found and which verified, and through which connection."""
    rows = {c.id: c for c in store.connections(account)}

    def describe(ref: str) -> str:
        parsed = parse_ref(ref)
        if parsed is None:
            return ref
        conn_id, model = parsed
        row = rows.get(conn_id)
        return f"`{model}` via {row.label}" if row else f"`{model}`"

    finders = sorted({describe(cfg.finder_route(lens)[0]) for lens in cfg.finders})
    verifiers = sorted({describe(cfg.verifier_route(lens)[0]) for lens in cfg.verifier_lenses})
    return (
        f"Reviewed with your tier **{cfg.label or cfg.name}** on your own API keys. "
        f"Found by {', '.join(finders)}; verified by {', '.join(verifiers)}."
    )


def save_tier(
    account: str, spec: TierSpec, *, tier_id: int | None = None, by: str = ""
) -> dict[str, Any]:
    problems = tier_problems(spec, account)
    if problems:
        raise WorkspaceError("; ".join(problems))
    try:
        row = store.save_custom_tier(
            account,
            name=spec.name,
            config=spec.model_dump(mode="json"),
            tier_id=tier_id,
            created_by=by,
        )
    except LookupError as e:
        raise WorkspaceError("that tier does not exist") from e
    except ValueError as e:
        raise WorkspaceError(str(e)) from e
    return _tier_json(row.id, row.name, spec, [], _iso(row.updated_at))


def save_routing(
    account: str, all_repos: int | None, repos: dict[str, int], *, by: str = ""
) -> dict[str, Any]:
    """Replace this account's routing: one tier for every repository and/or a
    tier per repository. Only the account's own repositories, only tiers
    that can run."""
    tiers = {t.id: t for t in store.custom_tiers(account)}

    def check(tier_id: int, where: str) -> None:
        row = tiers.get(tier_id)
        if row is None:
            raise WorkspaceError(f"{where}: that tier does not exist")
        problems = tier_problems(TierSpec.model_validate(row.config), account)
        if problems:
            raise WorkspaceError(f"{row.name} cannot run yet: {problems[0]}")

    if all_repos is not None:
        check(all_repos, "All repositories")
    cleaned: dict[str, int] = {}
    for repo, tier_id in repos.items():
        name = repo.strip()
        if not _REPO.fullmatch(name):
            raise WorkspaceError(f"{repo!r} is not an owner/name repository")
        if name.split("/", 1)[0].lower() != account.lower():
            raise WorkspaceError(f"{name} does not belong to {account}")
        check(tier_id, name)
        cleaned[name] = tier_id
    rules = {"all": all_repos, "repos": cleaned}
    store.save_routing_rules(account, rules, updated_by=by)
    return rules


class TierUnavailable(RuntimeError):
    """A repository is routed to a custom tier that cannot run. Raised at
    review time so the check says why, instead of silently spending CR's
    credits on a repository the customer chose to run on their own keys."""


def tier_for_repo(account: str, repo: str) -> int | None:
    """The tier this repository is forced onto, if any. A repository rule
    beats the account-wide one."""
    rules = store.routing_rules(account)
    by_repo = {r.lower(): t for r, t in rules["repos"].items()}
    return by_repo.get(repo.lower(), rules["all"])


def resolve_repo(
    settings: Settings, account: str, repo: str
) -> tuple[TierConfig, LLMClient] | None:
    """The custom tier and BYOK client every pull request in `repo` must use,
    or None when the repository runs CR's managed review."""
    tier_id = tier_for_repo(account, repo)
    if tier_id is None:
        return None
    row = store.custom_tier(account, tier_id)
    if row is None:
        return None
    spec = TierSpec.model_validate(row.config)
    problems = tier_problems(spec, account)
    if problems:
        raise TierUnavailable(f"custom tier {row.name!r} cannot run: {problems[0]}")
    client = byok_client(settings, account, max_concurrency=settings.max_concurrency)
    return to_tier_config(spec), client


# --- the dashboard view ---------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=UTC).isoformat() if dt else None


def _tier_json(
    tier_id: int, name: str, spec: TierSpec, problems: list[str], updated_at: str | None
) -> dict[str, Any]:
    return {
        "id": tier_id,
        "name": name,
        "spec": spec.model_dump(mode="json"),
        "problems": problems,
        "warnings": spec.warnings(),
        "updated_at": updated_at,
    }


def _pricing(m: ModelSpec) -> dict[str, float] | None:
    p = m.pricing
    return None if p is None else {"input": p.input, "output": p.output}


def _host(provider: str, resource: str = "") -> str:
    host = PROVIDERS[provider].base_url.split("://", 1)[-1].split("/", 1)[0]
    return host.replace("{resource}", resource) if resource else host


def _connection_json(
    row: Any, used_by: dict[int, list[str]], spend: dict[str, float]
) -> dict[str, Any]:
    models = []
    for entry in row.models or []:
        spec = _entry_spec(row.provider, entry)
        ref = conn_ref(row.id, entry["name"])
        models.append(
            {
                "ref": ref,
                "name": entry["name"],
                "label": spec.label,
                "listed": spec.known,
                "effort_levels": list(spec.effort_levels) if spec.supports_effort else [],
                "pricing": _pricing(spec),
                # "custom" when the customer set it, "catalog" when CR knows the
                # list price, None when spend on this model is untracked.
                "price_source": "custom"
                if entry.get("price")
                else ("catalog" if spec.pricing else None),
                "catalog_pricing": _pricing(custom_spec(row.provider, entry["name"])),
                "spend_30d": round(spend.get(ref, 0.0), 6),
                "tested_at": entry.get("tested_at"),
            }
        )
    return {
        "id": row.id,
        "provider": row.provider,
        "provider_label": PROVIDERS[row.provider].label,
        "label": row.label,
        "host": _host(row.provider, row.resource),
        "resource": row.resource,
        "hint": row.hint,
        "models": models,
        "tested_at": _iso(row.tested_at),
        "used_by": used_by.get(row.id, []),
        "spend_30d": round(sum(m["spend_30d"] for m in models), 6),
    }


def overview(settings: Settings, account: str) -> dict[str, Any]:
    routing = store.routing_rules(account)
    spend = store.byok_spend(account, datetime.now(UTC) - timedelta(days=30))
    tiers = []
    used_by: dict[int, list[str]] = {}
    for row in store.custom_tiers(account):
        spec = TierSpec.model_validate(row.config)
        tiers.append(
            _tier_json(row.id, row.name, spec, tier_problems(spec, account), _iso(row.updated_at))
        )
        for ref in spec.all_models():
            parsed = parse_ref(ref)
            if parsed and row.name not in used_by.setdefault(parsed[0], []):
                used_by[parsed[0]].append(row.name)

    return {
        "account": account,
        "vault_ready": vault_ready(settings),
        "providers": [
            {
                "id": pid,
                "label": p.label,
                "host": _host(pid),
                "needs_resource": p.needs_resource,
                "docs_url": p.docs_url,
                "model_hint": MODEL_HINTS.get(pid, "Model IDs"),
                # Catalog names offered as one-click suggestions. Any other
                # name is accepted too; its connection test decides.
                "suggestions": [
                    {"name": m.model, "label": m.label, "pricing": _pricing(m)}
                    for m in CATALOG
                    if m.provider == pid and m.verified
                ],
            }
            for pid, p in PROVIDERS.items()
        ],
        "connections": [_connection_json(c, used_by, spend) for c in store.connections(account)],
        # The managed presets, described by behaviour only.
        "presets": [
            {
                "slot": slot,
                **SLOT_INFO[slot],
                "finders": TIERS[slot].finders,
                "verifiers": TIERS[slot].verifier_lenses,
                "effort": TIERS[slot].effort,
                "max_comments": TIERS[slot].max_comments,
            }
            for slot in SLOTS
        ],
        "lenses": {
            "finders": [{"id": k, "description": v} for k, v in FINDER_INFO.items()],
            "verifiers": [{"id": k, "description": v} for k, v in VERIFIER_INFO.items()],
        },
        "tiers": tiers,
        "routing": routing,
        # Offered in the repository picker. Any of the account's repositories
        # can be typed in too; one granted later need not appear here first.
        "repos": store.account_repos(account),
        "guidelines": store.account_guidelines(account),
        "limits": {
            "max_guidelines": MAX_GUIDELINES,
            "max_comments": MAX_COMMENTS,
            "max_models": MAX_MODELS,
            "max_custom_finders": MAX_CUSTOM_FINDERS,
            "max_custom_verifiers": MAX_CUSTOM_VERIFIERS,
        },
    }


# --- per-repository guidelines -------------------------------------------------------

MAX_GUIDELINES = 4000


def save_guidelines(account: str, repo: str, text: str, *, by: str = "") -> str:
    """Store a repository's review guidelines; empty text clears them.

    They become part of the cached system prompt for every review of the
    repository — managed or on the customer's own keys — so they must stay
    byte-stable: anything that changes per run (a timestamp, a UUID) would
    silently disable caching and is refused with a reason instead.
    """
    from cr.llm.prefix import CacheInvalidatorError, assert_cacheable  # noqa: PLC0415

    name = repo.strip()
    if not _REPO.fullmatch(name):
        raise WorkspaceError(f"{repo!r} is not an owner/name repository")
    if name.split("/", 1)[0].lower() != account.lower():
        raise WorkspaceError(f"{name} does not belong to {account}")
    body = "\n".join(line.rstrip() for line in (text or "").strip().splitlines())
    if len(body) > MAX_GUIDELINES:
        raise WorkspaceError(f"keep guidelines under {MAX_GUIDELINES:,} characters")
    try:
        assert_cacheable(body, "guidelines")
    except CacheInvalidatorError as e:
        found = str(e).split(":", 1)[0].split(" found")[0]
        raise WorkspaceError(
            f"Guidelines cannot contain a {found}: it changes the prompt on every review "
            "and disables caching. Remove it and save again."
        ) from e
    store.save_repo_guidelines(name, account, body, updated_by=by)
    return body


# --- sample review -------------------------------------------------------------------------

# A small, self-contained change with one planted defect: `end` drops the last
# item of every page. A model worth routing reviews to should catch it.
SAMPLE_DIFF = """diff --git a/app/pagination.py b/app/pagination.py
new file mode 100644
--- /dev/null
+++ b/app/pagination.py
@@ -0,0 +1,7 @@
+def paginate(items, page, size):
+    \"\"\"Return page `page` (zero-indexed) of `items`, `size` items per page.\"\"\"
+    if page < 0 or size <= 0:
+        raise ValueError("page must be >= 0 and size > 0")
+    start = page * size
+    end = start + size - 1
+    return items[start:end]
"""
_BUG_LINES = range(5, 8)
_BUG_WORDS = ("off-by-one", "off by one", "last item", "size - 1", "one item short", "drops")


def _hits_planted_bug(f: Any) -> bool:
    on_lines = any(
        e.file.endswith("pagination.py") and e.start_line <= _BUG_LINES[-1] and e.end_line >= 5
        for e in f.evidence
    )
    text = f"{f.claim} {f.failure_scenario}".lower()
    return on_lines or any(w in text for w in _BUG_WORDS)


async def sample_review(
    settings: Settings, account: str, conn_id: int, model: str
) -> dict[str, Any]:
    """Run one finder and one verifier on a model, over a diff with a planted
    bug, on the customer's own key. Answers "is this model any good at
    reviewing?" before real pull requests depend on it."""
    from cr.llm.prefix import PRContext, RepoContext  # noqa: PLC0415
    from cr.review.engine import review  # noqa: PLC0415 - engine imports this module's deps

    row = store.connection(account, conn_id)
    if row is None or model not in {m["name"] for m in row.models or []}:
        raise WorkspaceError("that model is not on this connection")
    since = datetime.now(UTC) - timedelta(hours=1)
    if store.key_tests_since(account, since) >= settings.key_test_limit_per_hour:
        raise RateLimited(
            f"at most {settings.key_test_limit_per_hour} tests per hour; try again later"
        )
    store.note_key_test(account)

    ref = conn_ref(conn_id, model)
    tier = TierConfig(
        name="sample",
        model=ref,
        effort="medium",
        finders=["correctness"],
        verifier_lenses=["correctness"],
        max_comments=3,
        verifier_model=ref,
        custom=True,
        label="Sample review",
    )
    client = byok_client(settings, account)
    result = await review(
        repo=RepoContext(slug="cr/sample-review"),
        pr=PRContext(
            title="Add a pagination helper",
            description="Adds `paginate`, which returns one page of a list.",
            diff=SAMPLE_DIFF,
        ),
        tier=tier,
        client=client,
        cfg=settings,
        remember=False,
        record=False,
        use_cache=False,
    )
    secret = vault.decrypt(settings, row.ciphertext)
    raw = [f for f in result.raw_findings if _hits_planted_bug(f)]
    posted = [v for v in result.posted if _hits_planted_bug(v.finding)]
    return {
        "model": model,
        "found": bool(raw),
        "verified": bool(posted),
        "claim": posted[0].final_claim if posted else (raw[0].claim if raw else ""),
        "other_findings": len(result.posted) - len(posted),
        "cost_usd": round(result.cost_usd, 6),
        "elapsed_s": round(result.elapsed_s, 1),
        "errors": [vault.scrub(e, [secret])[:300] for e in result.errors],
    }
