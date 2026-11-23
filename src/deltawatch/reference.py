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


def compare_qa(run_csv: Path, ref_csv: Path) -> dict:
    a, b = pd.read_csv(run_csv), pd.read_csv(ref_csv)
    cols = [c for c in b.columns if c not in QA_IGNORED]
    missing = [c for c in cols if c not in a.columns]
    equal = not missing and a[cols].equals(b[cols])
    return {
        "equal": bool(equal),
        "columns_compared": len(cols),
        "missing_columns": missing,
        "ignored_runtime_columns": list(QA_IGNORED),
    }


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
