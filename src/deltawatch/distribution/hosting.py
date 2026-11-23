"""Check whether a live host can serve COGs to a browser: HTTP Range (206) and CORS.

Run it against the DEPLOYED site after publishing. It records the exact requests and responses so
the README can quote them. Nothing here assumes a host supports anything until this check says so.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timezone

RANGE_BYTES = 1024
HEADERS_KEPT = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "access-control-allow-origin",
    "access-control-expose-headers",
    "cache-control",
    "server",
)


def _request(url: str, headers: dict, method: str = "GET") -> dict:
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read() if method == "GET" else b""
            status, hdrs = resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        body, status, hdrs = exc.read(), exc.code, exc.headers
    return {
        "request": {"method": method, "url": url, "headers": headers},
        "status": status,
        "headers": {k: hdrs.get(k) for k in HEADERS_KEPT if hdrs.get(k) is not None},
        "body_bytes": len(body),
    }


def _cors_ok(resp: dict, origin: str) -> bool:
    return resp["headers"].get("access-control-allow-origin") in ("*", origin)


def check_host(cog_url: str, catalog_url: str, origin: str) -> dict:
    """Range + CORS on one COG, CORS on the catalog JSON. `passed` needs every check True."""
    rng = _request(cog_url, {"Range": f"bytes=0-{RANGE_BYTES - 1}", "Origin": origin})
    cat = _request(catalog_url, {"Origin": origin})
    checks = {
        "cog_range_status_206": rng["status"] == 206,
        "cog_content_range_present": rng["headers"].get("content-range", "").startswith(f"bytes 0-{RANGE_BYTES - 1}/"),
        "cog_range_body_length": rng["body_bytes"] == RANGE_BYTES,
        "cog_cors_allows_origin": _cors_ok(rng, origin),
        "catalog_status_200": cat["status"] == 200,
        "catalog_cors_allows_origin": _cors_ok(cat, origin),
    }
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "checks": checks,
        "passed": all(checks.values()),
        "cog_range_request": rng,
        "catalog_request": cat,
        "note": (
            "Range is a CORS-safelisted header only for a single simple range (bytes=N-M), so this GET "
            "matches what a browser sends without a preflight. Re-run after every deployment."
        ),
               }
