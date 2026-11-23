"""STAC discovery, asset checks, per-date de-duplication and date selection."""

from __future__ import annotations

import logging
import re
import time
from datetime import date

import numpy as np
import pandas as pd

from .errors import AssetError, SelectionError

log = logging.getLogger(__name__)
COLLECTION = "sentinel-2-c1-l2a"


def open_catalog(url: str, retries: int):
    import pystac_client

    for attempt in range(1, retries + 1):
        try:
            return pystac_client.Client.open(url)
        except Exception as exc:
            log.warning("STAC open attempt %d/%d failed: %r", attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(2**attempt)


def search_items(cfg: dict) -> list:
    cat = open_catalog(cfg["stac"]["api_url"], cfg["network"]["stac_max_retries"])
    search = cat.search(
        collections=[cfg["stac"]["collection"]],
        bbox=cfg["aoi"]["bbox"],
        datetime=f"{cfg['time']['start']}/{cfg['time']['end']}",
        query={"eo:cloud_cover": {"lt": cfg["stac"]["discovery_max_scene_cloud_pct"]}},
        max_items=cfg["stac"]["max_items"],
    )
    items = list(search.items())
    wrong = [it.id for it in items if it.collection_id != cfg["stac"]["collection"]]
    if wrong:
        raise RuntimeError(f"Items from unexpected collection: {wrong[:5]}")
    return items


def relative_orbit(product_uri: str | None) -> str | None:
    m = re.search(r"_R(\d{3})_", product_uri or "")
    return m.group(1) if m else None


def item_record(item) -> dict:
    p = item.properties
    return {
        "id": item.id,
        "datetime": p["datetime"],
        "date": p["datetime"][:10],
        "mgrs": (p.get("grid:code") or "").replace("MGRS-", ""),
        "scene_cloud_pct": p.get("eo:cloud_cover"),
        "platform": p.get("platform"),
        "relative_orbit": relative_orbit(p.get("s2:product_uri")),
        "datatake_id": p.get("s2:datatake_id"),
        "view_incidence_angle": p.get("view:incidence_angle"),
        "processing_baseline": p.get("s2:processing_baseline"),
    }


def describe_asset(item, key: str) -> dict:
    """Asset metadata from STAC. Refuses missing, non-HTTPS or requester-pays assets."""
    if key not in item.assets:
        raise AssetError(f"Asset '{key}' missing from {item.id}; available: {sorted(item.assets)}")
    a = item.assets[key]
    d = a.extra_fields
    if not a.href.startswith("https://"):
        raise AssetError(f"{item.id}/{key}: non-HTTPS href {a.href!r}; refusing credentialed access")
    if item.properties.get("storage:requester_pays") or d.get("storage:requester_pays"):
        raise AssetError(f"{item.id}/{key}: requester-pays asset; refusing")
    rb = (d.get("raster:bands") or [{}])[0]
    return {
        "key": key,
        "href": a.href,
        "nodata": rb.get("nodata"),
        "scale": rb.get("scale"),
        "offset": rb.get("offset"),
        "resolution_m": rb.get("spatial_resolution"),
        "file_checksum": d.get("file:checksum"),
        "proj_transform": d.get("proj:transform"),
    }


def tile_items(items, mgrs: str) -> list:
    """Keep one MGRS tile; overlapping views from adjacent tiles are not independent dates."""
    keep = [it for it in items if it.properties.get("grid:code") == f"MGRS-{mgrs}"]
    others = sorted({it.properties.get("grid:code") for it in items} - {f"MGRS-{mgrs}"})
    log.info("Tile %s: %d items kept; ignored tiles: %s", mgrs, len(keep), others)
    return keep


def dedup_per_date(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """At most one item per acquisition date.

    Same-date items on one tile can be different acquisitions (e.g. S2A and S2B on different
    relative orbits, minutes apart). Tie-break: higher AOI usable fraction, then view closer
    to nadir, then id (determinism). Rows with a screening error are never kept.
    """
    ok = df[df["error"].isna()] if "error" in df else df
    ok = ok.sort_values(["date", "usable_fraction", "view_incidence_angle", "id"], ascending=[True, False, True, True])
    kept = ok.drop_duplicates("date", keep="first")
    dropped = ok[~ok["id"].isin(kept["id"])]
    return kept.reset_index(drop=True), dropped.reset_index(drop=True)


def select_dates(eligible: pd.DataFrame, start: str, end: str, target: int, min_gap: int) -> pd.DataFrame:
    """Deterministic selection spread over [start, end].

    Split the window into `target` equal bins and take the most usable date per bin (tie ->
    closest to bin centre), then fill remaining slots with the best dates that keep `min_gap`.
    """
    t0, t1 = date.fromisoformat(start).toordinal(), date.fromisoformat(end).toordinal()
    e = eligible[(eligible["date"] >= start) & (eligible["date"] <= end)].copy()
    e["ord"] = e["date"].map(lambda d: date.fromisoformat(d).toordinal())
    edges = np.linspace(t0, t1 + 1, target + 1)
    chosen: list = []

    def gap_ok(o):
        return all(abs(o - c["ord"]) >= min_gap for c in chosen)

    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        b = e[(e["ord"] >= lo) & (e["ord"] < hi)].copy()
        b["dist"] = (b["ord"] - (lo + hi) / 2).abs()
        for _, r in b.sort_values(["usable_fraction", "dist", "id"], ascending=[False, True, True]).iterrows():
            if gap_ok(r["ord"]):
                chosen.append(r)
                break
    for _, r in e.sort_values(["usable_fraction", "ord", "id"], ascending=[False, True, True]).iterrows():
        if len(chosen) >= target:
            break
        if r["id"] not in {c["id"] for c in chosen} and gap_ok(r["ord"]):
            chosen.append(r)
    if not chosen:
        return e.iloc[0:0].drop(columns=["ord"])
    out = pd.DataFrame(chosen).sort_values("date").reset_index(drop=True)
    return out.drop(columns=["ord", "dist"], errors="ignore")


def require_min_dates(selected: pd.DataFrame, min_dates: int) -> None:
    if len(selected) < min_dates:
        raise SelectionError(
            f"Only {len(selected)} dates pass (need {min_dates}). Report counts and agree "
            "a wider date range before changing the config."
        )
