"""Encryption for customer API keys, and scrubbing for anything that echoes them.

A key a customer pastes into the dashboard is stored only as a Fernet token
(AES-128-CBC + HMAC-SHA256) under a master key that lives in the environment
(Key Vault in production), never in the database. A database dump is useless
without it. The browser never gets a key back: once saved, a key is shown as
its last four characters and nothing else.

Key rotation: CR_SECRETS_KEY may hold several comma-separated keys. The first
encrypts; every one of them decrypts. Add the new key in front, have keys
re-saved, then drop the old one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger(__name__)


class VaultUnavailable(RuntimeError):
    """No master key is configured, so customer keys cannot be stored or read."""


class SecretUnreadable(RuntimeError):
    """A stored key could not be decrypted — the master key changed."""


def _local_key_path() -> Path:
    from cr.repo import default_cache_dir  # noqa: PLC0415 - avoids an import cycle

    return default_cache_dir() / "secrets.key"


def _keys(settings: Any) -> list[bytes]:
    raw = (getattr(settings, "secrets_key", None) or "").strip()
    if raw:
        return [k.strip().encode() for k in raw.split(",") if k.strip()]
    if getattr(settings, "db_url", None):
        # A shared database means a deployment. Generating a key on one
        # replica's disk would make every other replica unable to read it.
        raise VaultUnavailable(
            "CR_SECRETS_KEY is not set. Customer API keys cannot be stored without it."
        )
    # Laptop install: SQLite on local disk, so a key beside it is no weaker.
    path = _local_key_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
        # Windows ignores POSIX modes; the file still sits under the user's home.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        log.info("generated a local secrets key at %s", path)
    return [path.read_bytes().strip()]


def _fernet(settings: Any) -> MultiFernet:
    try:
        return MultiFernet([Fernet(k) for k in _keys(settings)])
    except ValueError as e:
        raise VaultUnavailable(f"CR_SECRETS_KEY is not a valid Fernet key: {e}") from e


def encrypt(settings: Any, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt(settings: Any, token: str, *, ttl: int | None = None) -> str:
    """`ttl` (seconds) rejects tokens older than that — for short-lived
    tokens such as connection-test receipts, never for stored keys."""
    try:
        return _fernet(settings).decrypt(token.encode(), ttl=ttl).decode()
    except InvalidToken as e:
        raise SecretUnreadable(
            "this value could not be decrypted: it expired, was altered, or CR_SECRETS_KEY changed"
        ) from e


def hint(secret: str) -> str:
    """What the dashboard may show of a key: its last four characters."""
    tail = secret.strip()[-4:]
    return f"…{tail}" if len(secret.strip()) >= 12 else "…"


# Shapes providers use for keys, including the partly-masked forms some of them
# echo back in authentication errors ("Incorrect API key provided: sk-ab***yz").
_KEY_SHAPES = re.compile(
    r"(?:sk-(?:ant-|or-|proj-)?|gsk_|nvapi-|xai-|AIza)[A-Za-z0-9_\-*.]{3,}",
)


def scrub(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Remove every key, and anything shaped like one, from a message that is
    about to be shown, logged or stored."""
    out = text or ""
    for s in secrets:
        s = (s or "").strip()
        if len(s) >= 6:
            out = out.replace(s, "[redacted]")
            # Some providers echo a prefix and suffix around a mask.
            for part in (s[:8], s[-8:]):
                if len(part) >= 8:
                    out = out.replace(part, "[redacted]")
    return _KEY_SHAPES.sub("[redacted]", out)
