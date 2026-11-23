"""Hosting check logic. Uses a local stdlib HTTP server (which ignores Range) and canned responses."""

from __future__ import annotations

import http.server
import threading
from functools import partial

import pytest

from deltawatch.distribution import hosting

ORIGIN = "https://radiantearth.github.io"


@pytest.fixture
def plain_server(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    (tmp_path / "a.tif").write_bytes(b"II*\x00" + b"\x00" * 5000)
    (tmp_path / "catalog.json").write_text("{}")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_host_without_range_or_cors_fails(plain_server):
    r = hosting.check_host(f"{plain_server}/a.tif", f"{plain_server}/catalog.json", ORIGIN)
    assert r["passed"] is False
    assert r["checks"]["cog_range_status_206"] is False  # the stdlib server answers 200 with the whole file
    assert r["checks"]["cog_cors_allows_origin"] is False
    assert r["cog_range_request"]["request"]["headers"]["Range"] == "bytes=0-1023"


def test_host_with_range_and_cors_passes(monkeypatch):
    def fake(url, headers, method="GET"):
        if url.endswith(".tif"):
            h = {"content-range": "bytes 0-1023/9999", "access-control-allow-origin": "*"}
            return {"request": {"url": url, "headers": headers}, "status": 206, "headers": h, "body_bytes": 1024}
        h = {"access-control-allow-origin": "*"}
        return {"request": {"url": url, "headers": headers}, "status": 200, "headers": h, "body_bytes": 10}

    monkeypatch.setattr(hosting, "_request", fake)
    assert hosting.check_host("https://x/a.tif", "https://x/catalog.json", ORIGIN)["passed"] is True


def test_wrong_cors_origin_fails(monkeypatch):
    def fake(url, headers, method="GET"):
        h = {"content-range": "bytes 0-1023/9999", "access-control-allow-origin": "https://other.example"}
        return {"request": {}, "status": 206 if url.endswith(".tif") else 200, "headers": h, "body_bytes": 1024}

    monkeypatch.setattr(hosting, "_request", fake)
    r = hosting.check_host("https://x/a.tif", "https://x/catalog.json", ORIGIN)
    assert r["passed"] is False and r["checks"]["cog_cors_allows_origin"] is False
