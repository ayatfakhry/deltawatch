"""Results-preservation check: compare a fresh run against the frozen v0.2 reference outputs.

Compares values, grids and meaningful metadata (not bytes): NDVI and mask arrays (NaN-aware),
CRS/transform/shape, dtype, nodata, provenance tags, QA numbers and benchmark settings/scores.
Fields that changed on purpose in v0.3 are listed in DOCUMENTED_BENCHMARK_CHANGES and reported
separately instead of being silently ignored.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio

TAGS_COMPARED = (
    "manifest_sha256",
    "processing_sha256",
    "source_item",
    "acquired",
    "collection",
    "scale_offset_red",
    "scale_offset_nir",
    "scl_usable_classes",
)
QA_IGNORED = ("seconds", "reused_cached_output")  # runtime-dependent, not results
# v0.3 replaced protocol.tuning with protocol.declared_protocol and added input_validation.
DOCUMENTED_BENCHMARK_CHANGES = ("protocol.tuning", "protocol.declared_protocol", "input_validation")


def _resolve(ref: Path) -> Path:
    """Accept the reference folder or the reference zip (extracted to a temp dir)."""
    if ref.suffix == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="dw_ref_"))
        with zipfile.ZipFile(ref) as z:
            z.extractall(tmp)
        hits = list(tmp.rglob("geotiffs"))
        return hits[0].parent if hits else tmp
    return ref


def compare_rasters(run_dir: Path, ref_geotiffs: Path, dates: list[str]) -> list[dict]:
    rows = []
    for d in dates:
        for kind, name in (("ndvi", f"ndvi_{d}.tif"), ("mask", f"valid_{d}.tif")):
            with rasterio.open(run_dir / kind / name) as a, rasterio.open(ref_geotiffs / kind / name) as b:
                x, y = a.read(1), b.read(1)
                same_vals = np.array_equal(x, y, equal_nan=(kind == "ndvi"))
                ta, tb = a.tags(), b.tags()
                rows.append(
                    {
                        "file": f"{kind}/{name}",
                        "values_equal": bool(same_vals),
                        "nan_count_equal": bool(int(np.isnan(x).sum()) == int(np.isnan(y).sum()))
                        if kind == "ndvi"
                        else True,
                        "grid_equal": bool(a.crs == b.crs and a.transform == b.transform and a.shape == b.shape),
                        "dtype_nodata_equal": bool(
                            a.dtypes == b.dtypes
                            and (
                                (np.isnan(a.nodata) and np.isnan(b.nodata)) if kind == "ndvi" else a.nodata == b.nodata
                            )
                        ),
                        "tags_equal": all(ta.get(k) == tb.get(k) for k in TAGS_COMPARED),
                    }
                )
    return rows


# Float QA columns (fractions, NDVI percentiles, max |NDVI|) are compared with a tolerance because
# percentile interpolation can differ in the last bits across numpy/Python builds (measured: ~1e-8 on
# ndvi_p2/ndvi_p98 under Python 3.10). The tolerance is far below any meaningful NDVI or fraction
# change (QA values are reported to 4 decimals). Integer, text, date, hash and ID columns stay exact.
QA_FLOAT_RTOL = 1e-6
QA_FLOAT_ATOL = 1e-9


def compare_qa(run_csv: Path, ref_csv: Path) -> dict:
    a, b = pd.read_csv(run_csv), pd.read_csv(ref_csv)
    cols = [c for c in b.columns if c not in QA_IGNORED]
    missing = [c for c in cols if c not in a.columns]
    report = {
        "equal": False,
        "columns_compared": len(cols),
        "missing_columns": missing,
        "ignored_runtime_columns": list(QA_IGNORED),
        "float_tolerance": {"rtol": QA_FLOAT_RTOL, "atol": QA_FLOAT_ATOL, "nan_equals_nan": True},
        "float_columns": {},
        "exact_columns": [],
        "failures": [],
    }
    if missing:
        return report
    if len(a) != len(b) or "date" not in b.columns or a["date"].tolist() != b["date"].tolist():
        report["failures"].append(
            {
                "column": "date",
                "reason": "rows/dates differ",
                "run": a.get("date", pd.Series()).tolist(),
                "reference": b["date"].tolist(),
            }
        )
        return report
    dates = b["date"].astype(str).tolist()
    for c in cols:
        x, y = a[c], b[c]
        if pd.api.types.is_float_dtype(x) or pd.api.types.is_float_dtype(y):
            if not (pd.api.types.is_numeric_dtype(x) and pd.api.types.is_numeric_dtype(y)):
                report["failures"].append({"column": c, "reason": "numeric vs non-numeric"})
                continue
            xv, yv = x.to_numpy(dtype=float), y.to_numpy(dtype=float)
            ok = np.isclose(xv, yv, rtol=QA_FLOAT_RTOL, atol=QA_FLOAT_ATOL, equal_nan=True)
            both = np.isfinite(xv) & np.isfinite(yv)
            d = np.abs(xv - yv)
            with np.errstate(divide="ignore", invalid="ignore"):
                rel = np.where(np.abs(yv) > 0, d / np.abs(yv), np.where(d > 0, np.inf, 0.0))
            report["float_columns"][c] = {
                "max_abs_diff": float(d[both].max()) if both.any() else 0.0,
                "max_rel_diff": float(rel[both].max()) if both.any() else 0.0,
                "within_tolerance": bool(ok.all()),
            }
            if not ok.all():
                report["failures"].append(
                    {
                        "column": c,
                        "reason": "outside float tolerance",
                        "dates": [dates[i] for i in np.flatnonzero(~ok)],
                        "run": [None if np.isnan(v) else float(v) for v in xv[~ok]],
                        "reference": [None if np.isnan(v) else float(v) for v in yv[~ok]],
                    }
                )
        else:
            report["exact_columns"].append(c)
            xs, ys = x.astype(str).tolist(), y.astype(str).tolist()
            bad = [i for i, (u, v) in enumerate(zip(xs, ys, strict=True)) if u != v]
            if bad:
                report["failures"].append(
                    {
                        "column": c,
                        "reason": "exact column differs",
                        "dates": [dates[i] for i in bad],
                        "run": [xs[i] for i in bad],
                        "reference": [ys[i] for i in bad],
                    }
                )
    report["equal"] = not report["failures"]
    return report


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def compare_benchmark(run_json: Path, ref_json: Path) -> dict:
    a, b = _flatten(json.loads(run_json.read_text())), _flatten(json.loads(ref_json.read_text()))
    documented = lambda k: any(k == p or k.startswith(p + ".") for p in DOCUMENTED_BENCHMARK_CHANGES)  # noqa: E731
    keys = sorted(set(a) | set(b))
    diffs = [k for k in keys if not documented(k) and a.get(k) != b.get(k)]
    changed_documented = [k for k in keys if documented(k) and a.get(k) != b.get(k)]
    return {
        "equal_except_documented": not diffs,
        "undocumented_differences": diffs,
        "documented_changes_present": changed_documented,
        "fields_compared": len(keys),
    }


def compare_to_reference(
    run_dir: str | Path, reference: str | Path, manifest: dict, benchmark_dir: str | Path | None = None
) -> dict:
    run_dir, ref = Path(run_dir), _resolve(Path(reference))
    dates = [i["date"] for i in manifest["items"]]
    geo = ref / "geotiffs"
    rasters = compare_rasters(run_dir, geo, dates)
    qa_ref = ref / "colab_run" / "qa_per_date.csv"
    bench_ref = ref / "colab_run" / "benchmark" / "benchmark_results.json"
    report = {
        "rasters": {
            "files": len(rasters),
            "all_equal": all(all(v for k, v in r.items() if k != "file") for r in rasters),
            "per_file": rasters,
        },
        "qa": compare_qa(run_dir / "qa_per_date.csv", qa_ref),
    }
    bdir = Path(benchmark_dir) if benchmark_dir else run_dir / "benchmark"
    if (bdir / "benchmark_results.json").exists():
        report["benchmark"] = compare_benchmark(bdir / "benchmark_results.json", bench_ref)
    report["all_preserved"] = (
        report["rasters"]["all_equal"]
        and report["qa"]["equal"]
        and (report.get("benchmark", {"equal_except_documented": True})["equal_except_documented"])
    )
    return report
