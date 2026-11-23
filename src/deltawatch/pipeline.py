"""End-to-end stages: discover -> freeze manifest -> process dates -> QA exports.

Usage:
    python -m deltawatch.pipeline run --config config/default.yaml --manifest examples/manifest.json
    python -m deltawatch.pipeline discover --config config/default.yaml --out outputs/manifest.json
    python -m deltawatch.pipeline benchmark --config config/default.yaml --run-dir outputs
    python -m deltawatch.pipeline compare --run-dir outputs
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio

from . import __version__
from .catalog import (
    dedup_per_date,
    describe_asset,
    item_record,
    require_min_dates,
    search_items,
    select_dates,
    tile_items,
)
from .config import gdal_env, load_config, processing_fingerprint
from .errors import GridError
from .manifest import load_manifest, save_manifest
from .raster import (
    Grid,
    compute_ndvi,
    dilate,
    dn_to_reflectance,
    raster_header,
    read_on_grid,
    resolve_scale_offset,
    snapped_grid,
    verify_geotiff,
    write_geotiff,
)

log = logging.getLogger("deltawatch")
ROLES = ("red", "nir", "scl")


def env_versions() -> dict:
    import matplotlib
    import pystac_client
    import yaml

    return {
        "deltawatch": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "rasterio": rasterio.__version__,
        "gdal": rasterio.__gdal_version__,
        "pystac_client": pystac_client.__version__,
        "pyyaml": yaml.__version__,
        "matplotlib": matplotlib.__version__,
    }


# ----------------------------------------------------------------------------- discovery
def screen_item(item, grid: Grid, cfg: dict) -> dict:
    """SCL-only AOI screening for one item; never raises (errors are recorded)."""
    row = {**item_record(item), "error": None}
    try:
        for role in ROLES:
            describe_asset(item, cfg["assets"][role])
        scl, nod, _ = read_on_grid(item.assets[cfg["assets"]["scl"]].href, grid, categorical=True)
        row["aoi_data_fraction"] = round(float((scl != nod).mean()), 4)
        row["usable_fraction"] = round(float(np.isin(scl, cfg["quality"]["scl_usable_classes"]).mean()), 4)
    except Exception as exc:  # recorded per item, reported in the screening table
        row["error"] = f"{type(exc).__name__}: {exc}"
        log.error("screen failed %s: %s", item.id, row["error"])
    return row


def discover_and_freeze(cfg: dict, out_path: str | Path) -> tuple[dict, pd.DataFrame]:
    sel = cfg["selection"]
    with rasterio.Env(**gdal_env(cfg)):
        items = search_items(cfg)
        cands = tile_items(items, cfg["tile"]["preferred_mgrs"])
        if not cands:
            raise GridError("No item on the preferred tile")
        ref = describe_asset(cands[0], cfg["assets"]["red"])
        h = raster_header(ref["href"])
        grid = snapped_grid(cfg["aoi"]["bbox"], h["crs"], rasterio.Affine(*h["transform"]), h["shape"])
        screen = pd.DataFrame([screen_item(it, grid, cfg) for it in cands])
    per_date, dupes = dedup_per_date(screen)
    eligible = per_date[per_date["usable_fraction"] >= sel["min_usable_fraction"]]
    selected = select_dates(eligible, sel["window_start"], sel["window_end"], sel["target_dates"], sel["min_gap_days"])
    require_min_dates(selected, sel["min_dates"])
    by_id = {it.id: it for it in cands}
    manifest = {
        "manifest_version": 1,
        "frozen_at_utc": pd.Timestamp.now("UTC").isoformat(),
        "stac": {
            "api_url": cfg["stac"]["api_url"],
            "collection": cfg["stac"]["collection"],
            "search": {
                "bbox": cfg["aoi"]["bbox"],
                "datetime": f"{cfg['time']['start']}/{cfg['time']['end']}",
                "discovery_max_scene_cloud_pct": cfg["stac"]["discovery_max_scene_cloud_pct"],
                "n_items_returned": len(items),
                "n_items_on_tile": len(cands),
            },
        },
        "aoi": cfg["aoi"],
        "tile": cfg["tile"]["preferred_mgrs"],
        "grid": {**grid.to_dict(), "snapped_to": "red 10 m tile grid"},
        "quality_policy": {
            "scl_usable_classes": cfg["quality"]["scl_usable_classes"],
            "ndvi_denominator_min": cfg["quality"]["ndvi_denominator_min"],
            "scaling": "reflectance = DN * scale + offset, once; STAC and raster must agree",
        },
        "selection_policy": sel,
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
            for r in selected.itertuples()
        ],
    }
    return save_manifest(manifest, out_path), screen.assign(dropped_duplicate=screen["id"].isin(dupes["id"]))


# ----------------------------------------------------------------------------- processing
def _reusable(paths: dict, tags_expected: dict) -> bool:
    """Accept cached outputs only if both exist and carry the same input/config fingerprints."""
    if not all(Path(p).exists() for p in paths.values()):
        return False
    try:
        for p in paths.values():
            with rasterio.open(p) as ds:
                t = ds.tags()
                if any(t.get(k) != str(v) for k, v in tags_expected.items()):
                    return False
        return True
    except rasterio.errors.RasterioIOError:
        return False


def process_item(rec: dict, grid: Grid, cfg: dict, man_fp: str, out_dir: Path, force: bool = False) -> dict:
    q = cfg["quality"]
    t0 = time.perf_counter()
    paths = {
        "ndvi": out_dir / "ndvi" / f"ndvi_{rec['date']}.tif",
        "mask": out_dir / "mask" / f"valid_{rec['date']}.tif",
    }
    fp_tags = {"manifest_sha256": man_fp, "processing_sha256": processing_fingerprint(cfg), "source_item": rec["id"]}

    hdr, dn = {}, {}
    for role in ROLES:
        href = rec["assets"][role]["href"]
        hdr[role] = raster_header(href)
        dn[role], _, _ = read_on_grid(href, grid, categorical=(role == "scl"))
    scaling = {
        r: resolve_scale_offset(
            r, rec["assets"][r]["scale"], rec["assets"][r]["offset"], hdr[r]["scales"], hdr[r]["offsets"]
        )
        for r in ("red", "nir")
    }
    red = dn_to_reflectance(dn["red"], hdr["red"]["nodata"], scaling["red"]["scale"], scaling["red"]["offset"])
    nir = dn_to_reflectance(dn["nir"], hdr["nir"]["nodata"], scaling["nir"]["scale"], scaling["nir"]["offset"])
    scl = dn["scl"]
    r = compute_ndvi(
        red, nir, scl, q["scl_usable_classes"], q["ndvi_denominator_min"], q["require_positive_reflectance"]
    )
    valid = r["valid"]
    ndvi_out = np.where(valid, r["ndvi"], np.nan).astype(np.float32)
    mask_out = valid.astype(np.uint8)
    dil_valid = valid & ~dilate(np.isin(scl, q["dilation_classes"]), q["dilation_radius_px"])

    reused = (not force) and _reusable(paths, fp_tags)
    if not reused:
        tags = {
            **fp_tags,
            "acquired": rec["datetime"],
            "collection": "sentinel-2-c1-l2a",
            "scale_offset_red": (scaling["red"]["scale"], scaling["red"]["offset"]),
            "scale_offset_nir": (scaling["nir"]["scale"], scaling["nir"]["offset"]),
            "scl_usable_classes": q["scl_usable_classes"],
            "attribution": f"Contains modified Copernicus Sentinel data {rec['date'][:4]}",
        }
        for p in paths.values():
            p.parent.mkdir(parents=True, exist_ok=True)
        write_geotiff(
            paths["ndvi"], ndvi_out, grid, float("nan"), {**tags, "units": "NDVI (unitless)", "nodata": "NaN"}
        )
        write_geotiff(
            paths["mask"],
            mask_out,
            grid,
            255,
            {**tags, "values": "1=valid observation, 0=not valid, 255=nodata (unused)"},
        )
    rt = {
        "ndvi": verify_geotiff(paths["ndvi"], ndvi_out, grid, float("nan")),
        "mask": verify_geotiff(paths["mask"], mask_out, grid, 255),
    }

    vals, cnts = np.unique(scl, return_counts=True)
    counts = dict(zip(vals.tolist(), cnts.tolist(), strict=True))
    row = {
        "date": rec["date"],
        "id": rec["id"],
        "platform": rec.get("platform"),
        "relative_orbit": rec.get("relative_orbit"),
        "valid_fraction": round(float(valid.mean()), 4),
        "valid_fraction_if_dilated": round(float(dil_valid.mean()), 4),
        "nodata_red_px": int(np.isnan(red).sum()),
        "nodata_nir_px": int(np.isnan(nir).sum()),
        "nodata_marked_valid_px": int((valid & ~r["finite"]).sum()),
        "denominator_rejected_px": int((~r["den_ok"] & r["finite"]).sum()),
        "nonpositive_reflectance_px": int((r["finite"] & ~r["refl_pos"]).sum()),
        "ndvi_out_of_range_px": int(r["out_of_range"].sum()),
        "ndvi_out_of_range_with_positive_refl_px": int((r["out_of_range"] & r["refl_pos"]).sum()),
        "red_refl_lt0_px": int(np.sum(red < 0)),
        "nir_refl_gt1_px": int(np.sum(nir > 1)),
        "valid_ndvi_max_abs": float(np.nanmax(np.abs(ndvi_out))) if valid.any() else None,
        **{f"ndvi_p{p}": (float(np.nanpercentile(ndvi_out, p)) if valid.any() else None) for p in (2, 50, 98)},
        "roundtrip_ok": all(v["grid_ok"] and v["nodata_ok"] and v["values_ok"] for v in rt.values()),
        "reused_cached_output": reused,
        "scale_offset_source": scaling["red"]["source"],
        "seconds": round(time.perf_counter() - t0, 2),
        **{f"scl_{c}": int(counts.get(c, 0)) for c in range(12)},
    }
    return row


def qa_panel(out_dir: Path, grid: Grid, qa: pd.DataFrame, path: Path) -> None:
    import matplotlib
    import matplotlib.pyplot as plt

    if "ipykernel" not in sys.modules:
        matplotlib.use("Agg")
    n = len(qa)
    ncol = min(4, n)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.2 * ncol, 4.4 * nrow), constrained_layout=True, squeeze=False)
    im = None
    for ax, row in zip(axes.ravel(), qa.itertuples(), strict=False):
        with rasterio.open(out_dir / "ndvi" / f"ndvi_{row.date}.tif") as ds:
            im = ax.imshow(ds.read(1), cmap="RdYlGn", vmin=-0.2, vmax=0.9)
        ax.set_title(f"{row.date}  valid {row.valid_fraction:.1%}", fontsize=10)
        ax.set_xticks([])
        ax.set_yticks([])
    for ax in axes.ravel()[n:]:
        ax.axis("off")
    fig.colorbar(im, ax=axes, shrink=0.6, label="NDVI (unitless); blank = not a valid observation")
    years = ", ".join(sorted({d[:4] for d in qa["date"]}))
    fig.suptitle(
        f"DeltaWatch NDVI | {grid.crs} 10 m | Contains modified Copernicus Sentinel data {years}; "
        "accessed via Element 84 Earth Search",
        fontsize=10,
    )
    fig.savefig(path, dpi=100)
    plt.close(fig)


def run_core(cfg: dict, manifest_path: str | Path, out_dir: str | Path, force: bool = False) -> dict:
    """Process every frozen manifest date sequentially and write QA + run metadata."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timings = {}
    t = time.perf_counter()
    man = load_manifest(manifest_path)
    grid = Grid.from_dict(man["grid"])
    if man["quality_policy"]["scl_usable_classes"] != cfg["quality"]["scl_usable_classes"]:
        log.warning("Config SCL classes differ from manifest screening policy; outputs follow the config")
    timings["load_manifest_s"] = round(time.perf_counter() - t, 3)

    rows = []
    t = time.perf_counter()
    with rasterio.Env(**gdal_env(cfg)):
        for rec in man["items"]:  # sequential: one request stream at a time
            row = process_item(rec, grid, cfg, man["fingerprint_sha256"], out_dir, force=force)
            log.info(
                "%s valid=%.3f p50=%.3f oor=%d reused=%s %.2fs",
                row["date"],
                row["valid_fraction"],
                row["ndvi_p50"] or float("nan"),
                row["ndvi_out_of_range_px"],
                row["reused_cached_output"],
                row["seconds"],
            )
            rows.append(row)
    timings["process_dates_s"] = round(time.perf_counter() - t, 2)
    qa = pd.DataFrame(rows)
    qa.to_csv(out_dir / "qa_per_date.csv", index=False)

    t = time.perf_counter()
    qa_panel(out_dir, grid, qa, out_dir / "qa_ndvi_panel.png")
    timings["qa_panel_s"] = round(time.perf_counter() - t, 2)

    peak_rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)  # Linux: KiB
    local_mb = round(sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file()) / 1e6, 2)
    meta = {
        "manifest_sha256": man["fingerprint_sha256"],
        "processing_sha256": processing_fingerprint(cfg),
        "n_dates": len(rows),
        "grid": grid.to_dict(),
        "env": env_versions(),
        "timings_s_measured": timings,
        "memory": {
            "estimate_MB_per_date_float32_x6": round(grid.shape[0] * grid.shape[1] * 4 * 6 / 1e6, 2),
            "measured_peak_process_rss_MB": peak_rss_mb,
            "note": "estimate = shape x dtype x arrays (not measured); peak RSS = whole process",
        },
        "measured_local_outputs_MB": local_mb,
        "quality_policy": cfg["quality"],
    }
    (out_dir / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    return {"qa": qa, "meta": meta}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deltawatch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="search STAC, screen, select, freeze a manifest")
    d.add_argument("--config", default="config/default.yaml")
    d.add_argument("--out", default="outputs/manifest.json")
    r = sub.add_parser("run", help="process a frozen manifest")
    r.add_argument("--config", default="config/default.yaml")
    r.add_argument("--manifest", default="examples/manifest.json")
    r.add_argument("--out", default=None)
    r.add_argument("--force", action="store_true")
    b = sub.add_parser("benchmark", help="locked past-only gap-filling benchmark on a finished run")
    b.add_argument("--config", default="config/default.yaml")
    b.add_argument("--manifest", default="examples/manifest.json")
    b.add_argument("--run-dir", default="outputs")
    b.add_argument("--out", default=None)
    c = sub.add_parser("compare", help="compare a finished run with the frozen reference outputs")
    c.add_argument("--manifest", default="examples/manifest.json")
    c.add_argument("--run-dir", default="outputs")
    c.add_argument("--reference", default="reference/v0.2/deltawatch_results_v0.2.zip")
    c.add_argument("--report", default=None, help="write the JSON report here")
    e = sub.add_parser("evaluate", help="separate multi-target / multi-seed evaluation (new AOI)")
    e.add_argument("stage", choices=["discover", "describe", "score"])
    e.add_argument("--config", default="config/eval_new_aoi_2026.yaml")
    e.add_argument("--out", default="evaluations/new_aoi_2026")
    e.add_argument("--manifest", default="evaluations/new_aoi_2026/manifest.json")
    e.add_argument("--run-dir", default="evaluations/new_aoi_2026/run")
    e.add_argument(
        "--item-source",
        choices=["stac", "bucket"],
        default="stac",
        help="bucket: list the public COG bucket when the STAC API is unreachable",
    )
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd == "compare":
        from .reference import compare_to_reference

        rep = compare_to_reference(a.run_dir, a.reference, load_manifest(a.manifest))
        if a.report:
            Path(a.report).write_text(json.dumps(rep, indent=2))
        print(
            json.dumps(
                {
                    k: (v if k != "rasters" else {kk: vv for kk, vv in v.items() if kk != "per_file"})
                    for k, v in rep.items()
                },
                indent=2,
            )
        )
        return 0 if rep["all_preserved"] else 1
    cfg = load_config(a.config)
    if a.cmd == "discover":
        m, screen = discover_and_freeze(cfg, a.out)
        screen.to_csv(Path(a.out).with_name("screening_all_items.csv"), index=False)
        print(f"froze {len(m['items'])} dates -> {a.out} (sha256 {m['fingerprint_sha256'][:16]})")
    elif a.cmd == "evaluate":
        from . import evaluation

        if a.stage == "discover":
            r = evaluation.discover_and_freeze(cfg, Path(a.out), item_source=a.item_source)
            m = r["manifest"]
            print(
                f"tile {m['tile']} aoi {m['aoi']['bbox']}: froze {len(m['items'])} dates "
                f"(sha256 {m['fingerprint_sha256']})"
            )
        elif a.stage == "describe":
            desc = evaluation.describe_context(cfg, load_manifest(a.manifest), Path(a.run_dir), Path(a.out))
            for r in desc["per_date"]:
                print(r["date"], r["ndvi_p5_p25_p50_p75_p95"], r["scl_fraction"])
        else:
            res = evaluation.run_evaluation(cfg, load_manifest(a.manifest), Path(a.run_dir), Path(a.out))
            print(res["_summary"].to_string(index=False))
    elif a.cmd == "benchmark":
        from .benchmark import run_benchmark

        res = run_benchmark(cfg, load_manifest(a.manifest), a.run_dir, a.out or Path(a.run_dir) / "benchmark")
        for split in ("validation", "test"):
            s = res[split]["scores"]
            print(split, res[split]["target_date"], {m: s[m] for m in ("latest", "median3")})
    else:
        res = run_core(cfg, a.manifest, a.out or cfg["outputs"]["dir"], force=a.force)
        print(res["qa"][["date", "valid_fraction", "ndvi_p50", "roundtrip_ok"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
