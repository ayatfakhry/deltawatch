"""Separate multi-target, multi-seed evaluation of the existing baselines on a new AOI/period.

Reuses the benchmark contract unchanged: block_mask, Evaluator, predict and run_split. The
predictor sees only earlier dates and the hidden locations; the truth stays with the evaluator.
This module only generalises the list of targets and seeds, adds joint-support comparisons and a
block bootstrap over the actual hidden blocks. It is a synthetic hidden-block task on SCL-usable
pixels of a mixed surface, not recovery under real clouds.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio

from .benchmark import BenchmarkError, load_run, metrics, run_split
from .catalog import dedup_per_date, describe_asset, require_min_dates, search_items, select_dates
from .config import gdal_env
from .errors import GridError, SelectionError
from .manifest import save_manifest
from .pipeline import screen_item
from .raster import raster_header, snapped_grid

log = logging.getLogger("deltawatch.evaluation")
ROLES = ("red", "nir", "scl")
BUCKET = "https://e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com"


# ----------------------------------------------------------------------------- item sources
def list_bucket_items(tiles: list[str], start: str, end: str, cfg: dict) -> list:
    """Fallback item source when the STAC API is unreachable: list the public Collection 1 bucket.

    Loads each item's own STAC JSON and applies the same filters as the STAC search (collection,
    datetime window, scene cloud). Spatial filtering happens later via the tile grid + SCL screening.
    """
    import pystac

    y0, y1 = int(start[:4]), int(end[:4])
    items = []
    for tile in tiles:
        zone, band, sq = tile[:2], tile[2], tile[3:]
        for y in range(y0, y1 + 1):
            for m in range(1, 13):
                prefix = f"sentinel-2-c1-l2a/{zone}/{band}/{sq}/{y}/{m}/"
                if f"{y}-{m:02d}-31" < start or f"{y}-{m:02d}-01" > end:
                    continue
                for p in _list_prefixes(prefix):
                    item_id = p.rstrip("/").split("/")[-1]
                    items.append(pystac.Item.from_file(f"{BUCKET}/{p}{item_id}.json"))
    keep = [
        it
        for it in items
        if it.collection_id == cfg["stac"]["collection"]
        and start <= it.properties["datetime"][:10] <= end
        and (it.properties.get("eo:cloud_cover") or 0) < cfg["stac"]["discovery_max_scene_cloud_pct"]
    ]
    return keep


def zone_band(bbox) -> tuple[str, str]:
    """UTM zone and MGRS latitude band of the AOI centre (no special zones in Egypt)."""
    lon, lat = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return f"{int((lon + 180) // 6) + 1:02d}", "CDEFGHJKLMNPQRSTUVWXX"[int((lat + 80) // 8)]


def _list_prefixes(prefix: str) -> list[str]:
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    url = f"{BUCKET}/?list-type=2&delimiter=/&prefix={prefix}"
    root = ET.fromstring(urllib.request.urlopen(url, timeout=60).read())
    return [p.text for p in root.findall("s3:CommonPrefixes/s3:Prefix", ns)]


def bucket_candidate_tiles(bbox, start: str, red_key: str) -> dict:
    """Every tile in the AOI's zone/band; one item of the window's first month gives its grid."""
    import pystac

    zone, band = zone_band(bbox)
    y, m = int(start[:4]), int(start[5:7])
    out = {}
    for tp in _list_prefixes(f"sentinel-2-c1-l2a/{zone}/{band}/"):
        sq = tp.rstrip("/").split("/")[-1]
        items = _list_prefixes(f"{tp}{y}/{m}/")
        if not items:
            out[f"{zone}{band}{sq}"] = "no items in first month"
            continue
        iid = items[0].rstrip("/").split("/")[-1]
        it = pystac.Item.from_file(f"{BUCKET}/{items[0]}{iid}.json")
        out[f"{zone}{band}{sq}"] = "covers AOI" if tile_grids([it], bbox, red_key)["grids"] else "does not cover AOI"
    return out


# ----------------------------------------------------------------------------- discovery
def tile_grids(items, aoi_bbox, red_key: str) -> dict:
    """Tile -> reference grid for the AOI, only for tiles whose grid contains the whole AOI."""
    out, seen = {}, {}
    for it in items:
        tile = (it.properties.get("grid:code") or "").replace("MGRS-", "")
        if tile in seen:
            continue
        a = it.assets[red_key].extra_fields
        epsg = it.properties.get("proj:epsg") or str(it.properties.get("proj:code", "")).replace("EPSG:", "")
        try:
            g = snapped_grid(
                aoi_bbox, f"EPSG:{epsg}", rasterio.Affine(*a["proj:transform"][:6]), tuple(a["proj:shape"])
            )
            out[tile] = g
            seen[tile] = "covers AOI"
        except GridError as exc:
            seen[tile] = f"does not cover AOI: {exc}"
    return {"grids": out, "coverage": seen}


def choose_tile(eligible_counts: dict[str, int]) -> str:
    """Written rule: the only covering tile; else most eligible dates; tie -> alphabetical."""
    if not eligible_counts:
        raise SelectionError("No covering tile")
    return sorted(eligible_counts, key=lambda t: (-eligible_counts[t], t))[0]


def discover_aoi(cfg: dict, bbox: list, items: list) -> dict:
    """Apply the written tile rule and the unchanged selection logic to one AOI."""
    sel = cfg["selection"]
    tg = tile_grids(items, bbox, cfg["assets"]["red"])
    covering = sorted(tg["grids"])
    if not covering:
        raise SelectionError(f"No tile grid contains AOI {bbox}")
    per_tile = {}
    with rasterio.Env(**gdal_env(cfg)):
        for tile in covering:
            its = sorted((i for i in items if i.properties.get("grid:code") == f"MGRS-{tile}"), key=lambda i: i.id)
            # same as pipeline.discover_and_freeze: grid snapped to the actual red COG header
            h = raster_header(describe_asset(its[0], cfg["assets"]["red"])["href"])
            grid = snapped_grid(bbox, h["crs"], rasterio.Affine(*h["transform"]), h["shape"])
            scr = pd.DataFrame([screen_item(it, grid, cfg) for it in its])
            per_date, dupes = dedup_per_date(scr)
            win = per_date[(per_date["date"] >= sel["window_start"]) & (per_date["date"] <= sel["window_end"])]
            elig = win[win["usable_fraction"] >= sel["min_usable_fraction"]]
            per_tile[tile] = {"items": its, "grid": grid, "screen": scr, "dupes": dupes}
            per_tile[tile].update(per_date=win, eligible=elig)
    counts = {t: len(v["eligible"]) for t, v in per_tile.items()}
    chosen = choose_tile(counts)
    pt = per_tile[chosen]
    selected = select_dates(
        pt["eligible"], sel["window_start"], sel["window_end"], sel["target_dates"], sel["min_gap_days"]
    )
    require_min_dates(selected, sel["min_dates"])
    return {
        "bbox": bbox,
        "tile": chosen,
        "coverage": tg["coverage"],
        "eligible_counts_per_covering_tile": counts,
        "selected": selected,
        **pt,
    }


def build_manifest(cfg: dict, d: dict, item_source: str, attempts: list, n_items: int) -> dict:
    by_id = {it.id: it for it in d["items"]}
    return {
        "manifest_version": 1,
        "frozen_at_utc": pd.Timestamp.now("UTC").isoformat(),
        "stac": {
            "api_url": cfg["stac"]["api_url"],
            "collection": cfg["stac"]["collection"],
            "search": {
                "bbox": d["bbox"],
                "datetime": f"{cfg['time']['start']}/{cfg['time']['end']}",
                "discovery_max_scene_cloud_pct": cfg["stac"]["discovery_max_scene_cloud_pct"],
                "n_items_considered": n_items,
                "n_items_on_tile": len(d["items"]),
                "item_source": item_source,
            },
        },
        "aoi": {"crs": "EPSG:4326", "bbox": d["bbox"]},
        "tile": d["tile"],
        "grid": {**d["grid"].to_dict(), "snapped_to": "red 10 m tile grid"},
        "quality_policy": {
            "scl_usable_classes": cfg["quality"]["scl_usable_classes"],
            "ndvi_denominator_min": cfg["quality"]["ndvi_denominator_min"],
            "scaling": "reflectance = DN * scale + offset, once; STAC and raster must agree",
        },
        "selection_policy": cfg["selection"],
        "discovery": {
            "tile_rule": cfg["evaluation"]["tile_rule"],
            "tile_coverage": d["coverage"],
            "eligible_counts_per_covering_tile": d["eligible_counts_per_covering_tile"],
            "aoi_attempts": attempts,
        },
        "items": [
            {
                "id": r.id,
                "datetime": r.datetime,
                "date": r.date,
                "platform": r.platform,
                "relative_orbit": r.relative_orbit,
                "datatake_id": r.datatake_id,
                "view_incidence_angle": r.view_incidence_angle,
                "usable_fraction_at_screening": r.usable_fraction,
                "assets": {
                    role: {
                        k: v
                        for k, v in describe_asset(by_id[r.id], cfg["assets"][role]).items()
                        if k != "proj_transform"
                    }
                    for role in ROLES
                },
            }
            for r in d["selected"].itertuples()
        ],
    }


def discover_and_freeze(cfg: dict, out_dir: Path, item_source: str = "stac") -> dict:
    """Candidate AOI first; mechanical-only fallbacks in the written order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    aois = [{"name": "candidate", "bbox": cfg["aoi"]["bbox"]}] + cfg["evaluation"]["fallback_aois"]
    attempts = []
    for a in aois:
        try:
            if item_source == "stac":
                items = search_items({**cfg, "aoi": {**cfg["aoi"], "bbox": a["bbox"]}})
            else:
                cand = bucket_candidate_tiles(a["bbox"], cfg["time"]["start"], cfg["assets"]["red"])
                a = {**a, "bucket_tile_scan": {k: v for k, v in cand.items() if v != "does not cover AOI"}}
                tiles = [t for t, v in cand.items() if v == "covers AOI"]
                items = list_bucket_items(tiles, cfg["time"]["start"], cfg["time"]["end"], cfg)
            d = discover_aoi(cfg, a["bbox"], items)
        except (SelectionError, GridError, rasterio.errors.RasterioIOError) as exc:
            attempts.append({**a, "result": "failed (mechanical)", "reason": f"{type(exc).__name__}: {exc}"})
            log.warning("AOI %s failed mechanically: %s", a["name"], exc)
            continue
        attempts.append(
            {
                **a,
                "result": "used",
                "tile": d["tile"],
                "eligible_dates": len(d["eligible"]),
                "selected_dates": len(d["selected"]),
            }
        )
        man = save_manifest(build_manifest(cfg, d, item_source, attempts, len(items)), out_dir / "manifest.json")
        d["screen"].assign(dropped_duplicate=d["screen"]["id"].isin(d["dupes"]["id"])).to_csv(
            out_dir / "screening_all_items.csv", index=False
        )
        return {"manifest": man, "discovery": d, "attempts": attempts}
    raise SelectionError(f"All AOIs failed mechanically: {attempts}")


# ----------------------------------------------------------------------------- targets
def eligible_targets(dates: list[str], max_age_days: int, min_earlier: int) -> list[dict]:
    """Every frozen date with >= min_earlier earlier frozen dates within max_age_days (dates only)."""
    ds = sorted(dates)
    out = []
    for t in ds:
        td = date.fromisoformat(t)
        earlier = [d for d in ds if d < t and (td - date.fromisoformat(d)).days <= max_age_days]
        out.append({"date": t, "earlier_within_age": earlier, "is_target": len(earlier) >= min_earlier})
    return out


# ----------------------------------------------------------------------------- bootstrap
def block_index(hidden: np.ndarray, blocks: list) -> list[np.ndarray]:
    """Flat pixel indices of hidden pixels in each actual block (blocks never overlap)."""
    cols = hidden.shape[1]
    idx = []
    for r, c, b in blocks:
        rr, cc = np.nonzero(hidden[r : r + b, c : c + b])
        idx.append((rr + r) * cols + (cc + c))
    return idx


def block_bootstrap(pred_a, pred_b, truth, hidden, blocks, n_resamples: int, seed) -> dict:
    """Percentile CIs from resampling the actual hidden blocks with replacement."""
    blocks_idx = block_index(hidden, blocks)
    pa, pb, t = pred_a.ravel(), pred_b.ravel(), truth.ravel()
    stats = []  # per block: [nA, sAbsA, sSqA, nB, sAbsB, sSqB, nJ, sAbsJA, sSqJA, sAbsJB, sSqJB]
    for ix in blocks_idx:
        ea, eb = pa[ix] - t[ix], pb[ix] - t[ix]
        fa, fb = np.isfinite(ea), np.isfinite(eb)
        j = fa & fb
        stats.append(
            [
                fa.sum(),
                np.abs(ea[fa]).sum(),
                (ea[fa] ** 2).sum(),
                fb.sum(),
                np.abs(eb[fb]).sum(),
                (eb[fb] ** 2).sum(),
                j.sum(),
                np.abs(ea[j]).sum(),
                (ea[j] ** 2).sum(),
                np.abs(eb[j]).sum(),
                (eb[j] ** 2).sum(),
            ]
        )
    S = np.asarray(stats, dtype=np.float64)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(S), size=(n_resamples, len(S)))
    agg = S[pick].sum(axis=1)  # (n_resamples, 11)
    with np.errstate(invalid="ignore", divide="ignore"):
        res = {
            "A_mae": agg[:, 1] / agg[:, 0],
            "A_rmse": np.sqrt(agg[:, 2] / agg[:, 0]),
            "B_mae": agg[:, 4] / agg[:, 3],
            "B_rmse": np.sqrt(agg[:, 5] / agg[:, 3]),
            "joint_A_mae": agg[:, 7] / agg[:, 6],
            "joint_B_mae": agg[:, 9] / agg[:, 6],
            "joint_A_rmse": np.sqrt(agg[:, 8] / agg[:, 6]),
            "joint_B_rmse": np.sqrt(agg[:, 10] / agg[:, 6]),
        }
    res["joint_mae_A_minus_B"] = res["joint_A_mae"] - res["joint_B_mae"]
    res["joint_rmse_A_minus_B"] = res["joint_A_rmse"] - res["joint_B_rmse"]
    out = {"n_blocks": int(len(S)), "n_resamples": n_resamples}
    for k, v in res.items():
        v = v[np.isfinite(v)]
        out[k] = [round(float(np.percentile(v, 2.5)), 5), round(float(np.percentile(v, 97.5)), 5)] if len(v) else None
    return out


# ----------------------------------------------------------------------------- evaluation
def joint_support(pred_a, pred_b, truth, hidden) -> dict:
    """Scores of both baselines on the pixels where both predicted; A-B only on that support."""
    fa, fb = hidden & np.isfinite(pred_a), hidden & np.isfinite(pred_b)
    joint = fa & fb
    ma = metrics(pred_a, truth, hidden, support=joint)
    mb = metrics(pred_b, truth, hidden, support=joint)
    diff = {k: None if ma[k] is None else round(ma[k] - mb[k], 5) for k in ("mae", "rmse", "bias")}
    return {
        "n": int(joint.sum()),
        "only_A": int((fa & ~fb).sum()),
        "only_B": int((fb & ~fa).sum()),
        "A": ma,
        "B": mb,
        "mae_A_minus_B": diff["mae"],
        "rmse_A_minus_B": diff["rmse"],
        "bias_A_minus_B": diff["bias"],
    }


def evaluate_target_seed(obs: dict, target: str, seed: int, cfg: dict) -> dict:
    bcfg, ecfg = cfg["benchmark"], cfg["evaluation"]
    r = run_split(obs, {"date": target, "seed": seed}, bcfg)  # unchanged contract
    pa, pb, hidden = r["_preds"]["latest"], r["_preds"]["median3"], r["_hidden"]
    truth = np.where(hidden, obs[target].ndvi, np.nan)  # evaluation layer only; never given to predict
    full = {m: r["scores"][m] for m in ("latest", "median3")}
    js = joint_support(pa, pb, truth, hidden)
    boot_seed = [ecfg["bootstrap"]["seed"], date.fromisoformat(target).toordinal(), seed]
    boot = block_bootstrap(pa, pb, truth, hidden, r["mask"]["blocks"], ecfg["bootstrap"]["n_resamples"], boot_seed)
    td = date.fromisoformat(target)
    return {
        "target": target,
        "seed": seed,
        "n_hidden": int(hidden.sum()),
        "n_blocks": len(r["mask"]["blocks"]),
        "A": {**full["latest"], "n_abstain": int((hidden & ~np.isfinite(pa)).sum())},
        "B": {**full["median3"], "n_abstain": int((hidden & ~np.isfinite(pb)).sum())},
        "abstain_reason": "no earlier valid observation within max_age_days at that pixel",
        "joint_support": js,
        "bootstrap_ci95": boot,
        "context": {
            "earlier_dates_within_age": [d for d in r["provenance"]["latest"]["context_dates_within_age"]],
            "n_earlier_within_age": len(r["provenance"]["latest"]["context_dates_within_age"]),
            "mean_age_latest_obs_days": r["provenance"]["latest"]["mean_age_latest_days"],
            "true_ndvi_sd_hidden": round(float(np.nanstd(truth[hidden])), 5),
            "true_ndvi_mean_hidden": round(float(np.nanmean(truth[hidden])), 5),
            "days_since_previous_frozen_date": min((td - date.fromisoformat(d)).days for d in obs if d < target),
        },
    }


def describe_context(cfg: dict, manifest: dict, run_dir: Path, out_dir: Path) -> dict:
    """Validate the run and write the surface description; touches no prediction or error."""
    obs, _ = load_run(run_dir, manifest, cfg)
    desc = context_description(obs, pd.read_csv(run_dir / "qa_per_date.csv"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "context_description.json").write_text(json.dumps(desc, indent=2))
    return desc


def context_description(obs: dict, run_qa: pd.DataFrame) -> dict:
    """Surface description recorded BEFORE any scoring (no errors involved)."""
    rows = []
    for d, o in obs.items():
        v = o.ndvi[o.valid]
        q = run_qa.set_index("date").loc[d]
        total = sum(int(q[f"scl_{c}"]) for c in range(12))
        rows.append(
            {
                "date": d,
                "valid_fraction": float(o.valid.mean()),
                "ndvi_p5_p25_p50_p75_p95": [round(float(x), 4) for x in np.percentile(v, [5, 25, 50, 75, 95])],
                "scl_fraction": {c: round(int(q[f"scl_{c}"]) / total, 4) for c in range(12) if int(q[f"scl_{c}"])},
            }
        )
    return {
        "recorded_utc": pd.Timestamp.now("UTC").isoformat(),
        "note": "All of SCL 4/5/6 is eligible: vegetation, bare/built and water. No land-cover mask is applied.",
        "per_date": rows,
    }


def run_evaluation(cfg: dict, manifest: dict, run_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    obs, input_report = load_run(run_dir, manifest, cfg)  # validate_run first
    qa = pd.read_csv(run_dir / "qa_per_date.csv")
    ctx_path = out_dir / "context_description.json"
    if not ctx_path.exists():  # keep the record written before any scoring
        ctx_path.write_text(json.dumps(context_description(obs, qa), indent=2))
    tg = eligible_targets(
        list(obs), cfg["benchmark"]["max_age_days"], cfg["evaluation"]["min_earlier_dates_within_age"]
    )
    targets = [t["date"] for t in tg if t["is_target"]]
    seeds = cfg["evaluation"]["seeds"]
    plan = {"targets": targets, "seeds": seeds, "target_rule": tg, "written_utc": pd.Timestamp.now("UTC").isoformat()}
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    rows, failures = [], []
    t0 = time.perf_counter()
    for t in targets:
        for s in seeds:
            try:
                rows.append(evaluate_target_seed(obs, t, s, cfg))
            except BenchmarkError as exc:  # recorded, never retried with another seed
                failures.append({"target": t, "seed": s, "reason": str(exc)})
    result = {
        "task": "synthetic hidden-block NDVI estimation on all SCL-eligible surface in the AOI (mixed surface); "
        "not recovery under real clouds; SCL can be wrong",
        "manifest_sha256": manifest["fingerprint_sha256"],
        "input_validation": input_report,
        "settings": {
            "benchmark": {
                k: cfg["benchmark"][k]
                for k in ("max_age_days", "median_k", "hidden_fraction", "block_px", "max_block_attempts")
            },
            "evaluation": cfg["evaluation"],
        },
        "plan": plan,
        "results": rows,
        "failures": failures,
        "runtime_s": round(time.perf_counter() - t0, 2),
    }
    (out_dir / "evaluation_results.json").write_text(json.dumps(result, indent=2, default=str))
    flat = pd.DataFrame([_flat_row(r) for r in rows])
    flat.to_csv(out_dir / "evaluation_per_target_seed.csv", index=False)
    summary = summarise(flat)
    summary.to_csv(out_dir / "evaluation_summary_per_target.csv", index=False)
    (out_dir / "evaluation_summary.md").write_text(summary_markdown(summary, result))
    return {**result, "_flat": flat, "_summary": summary}


def _flat_row(r: dict) -> dict:
    j = r["joint_support"]
    return {
        "target": r["target"],
        "seed": r["seed"],
        "n_hidden": r["n_hidden"],
        "n_blocks": r["n_blocks"],
        "A_n_pred": r["A"]["n_predicted"],
        "A_coverage": r["A"]["coverage"],
        "A_abstain": r["A"]["n_abstain"],
        "A_mae": r["A"]["mae"],
        "A_rmse": r["A"]["rmse"],
        "A_bias": r["A"]["bias"],
        "B_n_pred": r["B"]["n_predicted"],
        "B_coverage": r["B"]["coverage"],
        "B_abstain": r["B"]["n_abstain"],
        "B_mae": r["B"]["mae"],
        "B_rmse": r["B"]["rmse"],
        "B_bias": r["B"]["bias"],
        "joint_n": j["n"],
        "only_A": j["only_A"],
        "only_B": j["only_B"],
        "joint_A_mae": j["A"]["mae"],
        "joint_B_mae": j["B"]["mae"],
        "joint_mae_A_minus_B": j["mae_A_minus_B"],
        "joint_rmse_A_minus_B": j["rmse_A_minus_B"],
        "ci_joint_mae_A_minus_B": r["bootstrap_ci95"]["joint_mae_A_minus_B"],
        "ci_A_mae": r["bootstrap_ci95"]["A_mae"],
        "ci_B_mae": r["bootstrap_ci95"]["B_mae"],
        "n_earlier_within_30d": r["context"]["n_earlier_within_age"],
        "mean_age_latest_obs_days": r["context"]["mean_age_latest_obs_days"],
        "true_ndvi_sd_hidden": r["context"]["true_ndvi_sd_hidden"],
    }


def summarise(flat: pd.DataFrame) -> pd.DataFrame:
    """Per target: mean and range across the 5 mask layouts (description only; no pooled CI)."""
    if flat.empty:
        return flat
    g = flat.groupby("target")
    rows = []
    for t, d in g:
        row = {
            "target": t,
            "n_seeds": len(d),
            "n_earlier_within_30d": int(d["n_earlier_within_30d"].iloc[0]),
            "true_ndvi_sd_hidden_mean": round(d["true_ndvi_sd_hidden"].mean(), 4),
        }
        for c in ("A_coverage", "B_coverage", "A_mae", "B_mae", "A_rmse", "B_rmse", "joint_mae_A_minus_B"):
            row[f"{c}_mean"] = round(d[c].mean(), 5)
            row[f"{c}_min"] = round(d[c].min(), 5)
            row[f"{c}_max"] = round(d[c].max(), 5)
        row["A_abstain_total"] = int(d["A_abstain"].sum())
        rows.append(row)
    return pd.DataFrame(rows)


def summary_markdown(s: pd.DataFrame, result: dict) -> str:
    lines = [
        "# New-AOI 2026 evaluation: per-target summary",
        "",
        "Synthetic hidden-block task on all SCL-eligible surface (mixed surface). Values are the mean "
        "and [min, max] across 5 mask layouts (seeds 301-305) on the same image; seeds are not independent "
        "datasets and targets are temporally dependent, so no pooled CI or p-values are reported.",
        "",
        "| Target | earlier ≤30 d | A coverage | A MAE mean [min, max] | B MAE mean [min, max] | "
        "joint MAE A−B mean [min, max] | true NDVI SD (hidden) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in s.itertuples():
        lines.append(
            f"| {r.target} | {r.n_earlier_within_30d} | {r.A_coverage_mean:.4f} | "
            f"{r.A_mae_mean:.4f} [{r.A_mae_min:.4f}, {r.A_mae_max:.4f}] | "
            f"{r.B_mae_mean:.4f} [{r.B_mae_min:.4f}, {r.B_mae_max:.4f}] | "
            f"{r.joint_mae_A_minus_B_mean:+.4f} [{r.joint_mae_A_minus_B_min:+.4f}, {r.joint_mae_A_minus_B_max:+.4f}] | "
            f"{r.true_ndvi_sd_hidden_mean:.3f} |"
        )
    lines += [
        "",
        f"Targets evaluated: {s.shape[0]} of {len(result['plan']['targets'])}; failures: {len(result['failures'])}.",
        "Per-seed values with block-bootstrap 95 % intervals are in evaluation_per_target_seed.csv / "
        "evaluation_results.json.",
    ]
    return "\n".join(lines) + "\n"
