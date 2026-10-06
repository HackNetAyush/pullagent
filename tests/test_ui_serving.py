"""Serving the built dashboard: the SPA fallback, and nothing outside it.

Requests go in as raw ASGI scopes, because an HTTP client normalises `..` out
of a URL before sending it - and the attack is a client that does not.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI

from cr.server import mount_ui


def _get(app: FastAPI, path: str) -> tuple[int, bytes]:
    sent: list[dict] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("test", 1),
        "server": ("test", 80),
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def _site(tmp_path, *, with_index: bool = True) -> FastAPI:
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    if with_index:
        (dist / "index.html").write_text("<!doctype html>shell")
    (tmp_path / "secret.env").write_text("CR_GITHUB_APP_PRIVATE_KEY=nope")
    app = FastAPI()
    mount_ui(app, dist)
    return app


def test_client_routes_get_the_shell_and_files_are_served(tmp_path):
    app = _site(tmp_path)
    assert _get(app, "/runs/12") == (200, b"<!doctype html>shell")
    assert _get(app, "/favicon.svg") == (200, b"<svg/>")
    assert _get(app, "/assets/app.js") == (200, b"console.log(1)")


def test_nothing_outside_the_build_directory_is_served(tmp_path):
    app = _site(tmp_path)
    for path in ("/../../secret.env", "/../../../secret.env", "/assets/../../../secret.env"):
        status, body = _get(app, path)
        assert b"PRIVATE_KEY" not in body, path
        assert status in (200, 404), path


def test_a_missing_build_is_a_clear_503_not_a_crash(tmp_path):
    app = _site(tmp_path, with_index=False)
    status, body = _get(app, "/")
    assert status == 503
    assert b"being rebuilt" in body


def test_unknown_api_routes_are_404s_not_the_shell(tmp_path):
    status, body = _get(_site(tmp_path), "/api/nope")
    assert status == 404 and b"no such endpoint" in body
