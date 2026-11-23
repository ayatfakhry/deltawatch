"""A minimal local query service over the static catalog (datetime range + bbox).

This is NOT a STAC API implementation and does not claim conformance to any STAC API spec.
It reads the static catalog files at startup, answers simple queries, and serves the asset files
(with HTTP Range support) so a local client can stream a COG. Local use only: no deployment.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

LABEL = "minimal local query service over the static DeltaWatch catalog (not a STAC API)"


def _parse_instant(text: str, end: bool) -> datetime:
    """RFC 3339 datetime, or a plain date (start or end of that UTC day, so date ranges are inclusive)."""
    try:
        if "T" not in text:
            d = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(d, time.max if end else time.min, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"invalid datetime '{text}': use RFC 3339 or YYYY-MM-DD") from exc
    if dt.tzinfo is None:
        raise HTTPException(400, f"datetime '{text}' needs a timezone (e.g. Z)")
    return dt


def parse_datetime(value: str | None) -> tuple[datetime | None, datetime | None]:
    """'A/B', '../B', 'A/..' or a single instant. Bounds are inclusive."""
    if not value:
        return None, None
    if "/" in value:
        a, _, b = value.partition("/")
        lo = None if a in ("", "..") else _parse_instant(a, end=False)
        hi = None if b in ("", "..") else _parse_instant(b, end=True)
        if lo is None and hi is None:
            raise HTTPException(400, "datetime interval must have at least one bound")
    else:
        lo, hi = _parse_instant(value, end=False), _parse_instant(value, end=True)
    if lo and hi and lo > hi:
        raise HTTPException(400, "datetime interval start is after its end")
    return lo, hi


def parse_bbox(value: str | None) -> list[float] | None:
    if not value:
        return None
    try:
        b = [float(v) for v in value.split(",")]
    except ValueError as exc:
        raise HTTPException(400, "bbox must be four numbers: minlon,minlat,maxlon,maxlat") from exc
    if len(b) != 4:
        raise HTTPException(400, "bbox must be four numbers: minlon,minlat,maxlon,maxlat")
    if not (-180 <= b[0] <= b[2] <= 180 and -90 <= b[1] <= b[3] <= 90):
        raise HTTPException(400, "bbox must satisfy -180<=minlon<=maxlon<=180 and -90<=minlat<=maxlat<=90")
    return b


def _intersects(a: list[float], b: list[float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def load_items(catalog_dir: Path) -> tuple[dict, list[dict]]:
    """Read the collection and its items straight from the static JSON (no pystac needed at runtime)."""
    root = json.loads((catalog_dir / "catalog.json").read_text())
    child = next(lk for lk in root["links"] if lk["rel"] == "child")
    col_path = (catalog_dir / child["href"]).resolve()
    collection = json.loads(col_path.read_text())
    items = []
    for lk in collection["links"]:
        if lk["rel"] != "item":
            continue
        path = (col_path.parent / lk["href"]).resolve()
        item = json.loads(path.read_text())
        item["_dir"] = path.parent.relative_to(catalog_dir.resolve()).as_posix()
        items.append(item)
    items.sort(key=lambda i: i["properties"]["datetime"])
    return collection, items


def create_app(catalog_dir: str | Path) -> FastAPI:
    catalog_dir = Path(catalog_dir).resolve()
    if not (catalog_dir / "catalog.json").exists():
        raise FileNotFoundError(f"{catalog_dir}/catalog.json not found; build the catalog first")
    collection, items = load_items(catalog_dir)
    app = FastAPI(title="DeltaWatch local query", description=LABEL, version="0.1.0")
    # Static files: catalog JSON and COGs. Starlette serves Range requests with 206 Partial Content.
    app.mount("/files", StaticFiles(directory=catalog_dir), name="files")

    def public(item: dict, request: Request) -> dict:
        out = {k: v for k, v in item.items() if k != "_dir"}
        base = str(request.base_url).rstrip("/")
        out["assets"] = {
            k: {**a, "href": f"{base}/files/{item['_dir']}/{a['href'].removeprefix('./')}"}
            for k, a in item["assets"].items()
        }
        out["links"] = [{"rel": "self", "href": f"{base}/items/{item['id']}", "type": "application/geo+json"}]
        return out

    @app.get("/")
    def landing() -> dict:
        return {
            "title": "DeltaWatch local query",
            "description": LABEL,
            "collection": collection["id"],
            "items": len(items),
            "endpoints": ["/collection", "/items/{item_id}", "/search?datetime=&bbox=&limit=", "/files/..."],
        }

    @app.get("/collection")
    def get_collection() -> dict:
        return {k: v for k, v in collection.items() if k != "links"}

    @app.get("/items/{item_id}")
    def get_item(item_id: str, request: Request) -> dict:
        for it in items:
            if it["id"] == item_id:
                return public(it, request)
        raise HTTPException(404, f"no item '{item_id}'")

    @app.get("/search")
    def search(
        request: Request,
        datetime_: str | None = Query(None, alias="datetime", description="A/B, ../B, A/.. or one instant"),
        bbox: str | None = Query(None, description="minlon,minlat,maxlon,maxlat (EPSG:4326)"),
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict:
        lo, hi = parse_datetime(datetime_)
        box = parse_bbox(bbox)
        hits = []
        for it in items:
            t = datetime.fromisoformat(it["properties"]["datetime"].replace("Z", "+00:00"))
            if (lo and t < lo) or (hi and t > hi):
                continue
            if box and not _intersects(it["bbox"], box):
                continue
            hits.append(it)
        return {
            "type": "FeatureCollection",
            "features": [public(it, request) for it in hits[:limit]],
            "numberMatched": len(hits),
            "numberReturned": min(len(hits), limit),
        }

    return app
