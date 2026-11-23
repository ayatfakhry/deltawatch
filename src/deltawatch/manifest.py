"""Frozen source manifest: what was chosen, from where, under which policies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import ManifestError

VOLATILE_KEYS = ("frozen_at_utc", "fingerprint_sha256")


def fingerprint(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k not in VOLATILE_KEYS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def save_manifest(manifest: dict, path: str | Path) -> dict:
    m = dict(manifest)
    m["fingerprint_sha256"] = fingerprint(m)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(m, indent=2, default=str))
    return m


def load_manifest(path: str | Path) -> dict:
    m = json.loads(Path(path).read_text())
    if "fingerprint_sha256" not in m:
        raise ManifestError("Manifest has no fingerprint; it was not frozen by DeltaWatch")
    fp = fingerprint(m)
    if fp != m["fingerprint_sha256"]:
        raise ManifestError(f"Manifest edited after freezing ({fp[:12]} != {m['fingerprint_sha256'][:12]})")
    if m["stac"]["collection"] != "sentinel-2-c1-l2a":
        raise ManifestError("Manifest collection is not sentinel-2-c1-l2a")
    dates = [i["date"] for i in m["items"]]
    if len(set(dates)) != len(dates):
        raise ManifestError("Manifest has more than one item for a date")
    for it in m["items"]:
        for role in ("red", "nir", "scl"):
            if role not in it["assets"]:
                raise ManifestError(f"{it['id']}: asset '{role}' missing from manifest")
    return m
