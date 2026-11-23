"""Optional locked benchmark: past-only temporal baselines on artificially hidden NDVI pixels.

This is a synthetic missing-data task on pixels SCL marks usable. It is NOT recovery of ground
truth under real clouds (SCL itself can be wrong). Prediction code never receives the target
date's NDVI, reflectances or any statistic of the hidden truth; the Evaluator holds the truth.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import rasterio

from .errors import DeltaWatchError


class BenchmarkError(DeltaWatchError):
    """The locked protocol cannot be satisfied (reported, never silently weakened)."""


@dataclass(frozen=True)
class Observation:
    """One earlier date given to a predictor: NDVI (NaN where invalid) + its valid mask."""

    date: str
    ndvi: np.ndarray
    valid: np.ndarray


# ----------------------------------------------------------------------------- masks
def block_mask(eligible: np.ndarray, fraction: float, block_px: int, seed: int, max_attempts: int) -> dict:
    """Non-overlapping square blocks placed inside eligible pixels until >= fraction is hidden.

    Hidden pixels = union of blocks intersected with eligible pixels. Blocks must be at least
    half eligible so they sit inside observed areas. Deterministic for a given seed.
    """
    n_eligible = int(eligible.sum())
    target = int(round(fraction * n_eligible))
    if target == 0:
        raise BenchmarkError("No eligible pixels to hide")
    rng = np.random.default_rng(seed)
    rows, cols = eligible.shape
    taken = np.zeros_like(eligible, dtype=bool)
    hidden = np.zeros_like(eligible, dtype=bool)
    blocks = []
    for _ in range(max_attempts):
        if hidden.sum() >= target:
            break
        r = int(rng.integers(0, rows - block_px + 1))
        c = int(rng.integers(0, cols - block_px + 1))
        win = (slice(r, r + block_px), slice(c, c + block_px))
        if taken[win].any() or eligible[win].mean() < 0.5:
            continue
        taken[win] = True
        hidden[win] |= eligible[win]
        blocks.append([r, c, block_px])
    if hidden.sum() < target:
        raise BenchmarkError(
            f"Could hide only {int(hidden.sum())} of {target} target pixels with "
            f"{block_px}px blocks; report this instead of weakening the design"
        )
    return {
        "hidden": hidden,
        "blocks": blocks,
        "n_eligible": n_eligible,
        "target_count": target,
        "n_hidden": int(hidden.sum()),
        "seed": seed,
        "block_px": block_px,
    }


# ----------------------------------------------------------------------------- predictors
def _usable_context(context: list[Observation], target_date: str, max_age_days: int) -> list[tuple[int, Observation]]:
    t = date.fromisoformat(target_date)
    out = []
    for obs in context:
        d = date.fromisoformat(obs.date)
        if d >= t:
            raise BenchmarkError(f"Future or same-day context {obs.date} for target {target_date}")
        age = (t - d).days
        if age <= max_age_days:
            out.append((age, obs))
    return sorted(out, key=lambda x: x[0])  # most recent first


def predict(
    context: list[Observation], target_date: str, missing: np.ndarray, method: str, max_age_days: int = 30, k: int = 3
) -> dict:
    """Estimate NDVI at `missing` pixels from earlier observations only.

    method 'latest' (A): most recent earlier valid value within max_age_days.
    method 'median3' (B): median of up to k most recent earlier valid values within max_age_days.
    Pixels with no qualifying observation abstain (NaN). Returns prediction plus provenance.
    """
    if method not in ("latest", "median3"):
        raise ValueError(f"unknown method {method}")
    ctx = _usable_context(context, target_date, max_age_days)
    shape = missing.shape
    pred = np.full(shape, np.nan, np.float32)
    n_used = np.zeros(shape, np.uint8)
    age_latest = np.full(shape, -1, np.int16)
    if not ctx:
        return {"pred": pred, "n_used": n_used, "age_latest": age_latest, "context_dates": []}
    stack = np.stack([np.where(o.valid, o.ndvi, np.nan) for _, o in ctx])  # (n, rows, cols), recent first
    ages = np.array([a for a, _ in ctx])
    have = np.isfinite(stack)
    rank = np.cumsum(have, axis=0)  # 1 = most recent valid obs
    if method == "latest":
        sel = have & (rank == 1)
    else:
        sel = have & (rank <= k)
    picked = np.where(sel, stack, np.nan)
    n = sel.sum(axis=0)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels simply abstain
        # 'latest' selects exactly one value per pixel, so nansum returns it unchanged.
        est = np.nanmedian(picked, axis=0) if method == "median3" else np.nansum(picked, axis=0)
    ok = missing & (n > 0)
    pred[ok] = est[ok].astype(np.float32)
    n_used[ok] = n[ok]
    first = np.argmax(have, axis=0)
    age_latest[ok] = ages[first][ok]
    return {"pred": pred, "n_used": n_used, "age_latest": age_latest, "context_dates": [o.date for _, o in ctx]}


# ----------------------------------------------------------------------------- evaluation
def metrics(pred: np.ndarray, truth: np.ndarray, hidden: np.ndarray, support: np.ndarray | None = None) -> dict:
    """MAE/RMSE in NDVI units on hidden pixels that have a prediction (and optional support mask)."""
    scored = hidden & np.isfinite(pred) & np.isfinite(truth)
    if support is not None:
        scored &= support
    n_hidden = int(hidden.sum())
    n = int(scored.sum())
    if n == 0:
        return {"n_hidden": n_hidden, "n_predicted": 0, "coverage": 0.0, "mae": None, "rmse": None, "bias": None}
    err = (pred[scored] - truth[scored]).astype(np.float64)
    return {
        "n_hidden": n_hidden,
        "n_predicted": n,
        "coverage": round(n / n_hidden, 4),
        "mae": round(float(np.abs(err).mean()), 5),
        "rmse": round(float(np.sqrt((err**2).mean())), 5),
        "bias": round(float(err.mean()), 5),
    }


class Evaluator:
    """Holds the target truth. Predictors only ever see `public_inputs()`."""

    def __init__(self, target_date: str, ndvi: np.ndarray, valid: np.ndarray, mask: dict):
        self.target_date = target_date
        self._truth = np.where(mask["hidden"], ndvi, np.nan).astype(np.float32)
        self.hidden = mask["hidden"]
        self.mask = mask

    def public_inputs(self) -> dict:
        return {"target_date": self.target_date, "missing": self.hidden.copy()}

    def score(self, preds: dict[str, np.ndarray]) -> dict:
        out = {m: metrics(p, self._truth, self.hidden) for m, p in preds.items()}
        both = np.logical_and.reduce([np.isfinite(p) for p in preds.values()])
        out["intersection"] = {m: metrics(p, self._truth, self.hidden, support=both) for m, p in preds.items()}
        return out


# ----------------------------------------------------------------------------- run
# Data contract written by pipeline.process_item (v0.2): one band each; NDVI float32 with NaN
# nodata; mask uint8 with nodata 255 and only values {0, 1}; both on the manifest grid and tagged
# with manifest_sha256, processing_sha256 and source_item.
NDVI_DTYPE, MASK_DTYPE, MASK_NODATA, MASK_VALUES = "float32", "uint8", 255, {0, 1}
REQUIRED_TAGS = ("manifest_sha256", "processing_sha256", "source_item")


def _file_problems(path: Path, kind: str, grid, expected_tags: dict) -> tuple[list[str], np.ndarray | None]:
    """Check one GeoTIFF against the data contract. Returns (problems, array or None)."""
    from .raster import Grid, grids_equal

    if not path.exists():
        return [f"{path}: missing file"], None
    probs = []
    with rasterio.open(path) as ds:
        if ds.count != 1:
            probs.append(f"{path}: {ds.count} bands, expected 1")
        dtype = NDVI_DTYPE if kind == "ndvi" else MASK_DTYPE
        if ds.dtypes[0] != dtype:
            probs.append(f"{path}: dtype {ds.dtypes[0]}, expected {dtype}")
        if kind == "ndvi":
            if ds.nodata is None or not np.isnan(ds.nodata):
                probs.append(f"{path}: nodata {ds.nodata}, expected NaN")
        elif ds.nodata != MASK_NODATA:
            probs.append(f"{path}: nodata {ds.nodata}, expected {MASK_NODATA}")
        got = Grid(ds.crs.to_string() if ds.crs else "None", ds.transform, (ds.height, ds.width))
        if not grids_equal(got, grid):
            probs.append(
                f"{path}: grid {got.crs} {tuple(got.transform)[:6]} {got.shape} does not match manifest "
                f"grid {grid.crs} {tuple(grid.transform)[:6]} {grid.shape}"
            )
        tags = ds.tags()
        for k, v in expected_tags.items():
            if k not in tags:
                probs.append(f"{path}: tag '{k}' missing (provenance cannot be verified)")
            elif tags[k] != v:
                probs.append(f"{path}: tag {k}={tags[k][:16]}... does not match expected {v[:16]}...")
        arr = ds.read(1) if not probs else None
    return probs, arr


def validate_run(run_dir: str | Path, manifest: dict, cfg: dict, dates: list[str] | None = None) -> dict:
    """Refuse to score a run directory unless every required file belongs to this manifest + config.

    Checks, for every requested date (default: all manifest dates): both files exist; band count,
    dtype and nodata follow the data contract; CRS, transform and shape equal the manifest grid;
    provenance tags match the manifest fingerprint, the current processing fingerprint and the
    manifest item for that date; mask values are only {0, 1}; NDVI is finite and within [-1, 1]
    where the mask is 1 and NaN where it is 0. Nothing is reprojected or repaired.
    """
    from .config import processing_fingerprint
    from .manifest import fingerprint
    from .raster import Grid

    run_dir = Path(run_dir)
    man_fp = manifest.get("fingerprint_sha256")
    if not man_fp or fingerprint(manifest) != man_fp:
        raise BenchmarkError("Manifest fingerprint missing or does not match its content")
    proc_fp = processing_fingerprint(cfg)
    grid = Grid.from_dict(manifest["grid"])
    items = {i["date"]: i for i in manifest["items"]}
    dates = dates or list(items)
    unknown = [d for d in dates if d not in items]
    if unknown:
        raise BenchmarkError(f"Dates not in manifest: {unknown}")
    problems, arrays = [], {}
    for d in dates:
        expected = {"manifest_sha256": man_fp, "processing_sha256": proc_fp, "source_item": items[d]["id"]}
        pn, ndvi = _file_problems(run_dir / "ndvi" / f"ndvi_{d}.tif", "ndvi", grid, expected)
        pm, mask = _file_problems(run_dir / "mask" / f"valid_{d}.tif", "mask", grid, expected)
        problems += pn + pm
        if ndvi is None or mask is None:
            continue
        bad_vals = set(np.unique(mask).tolist()) - MASK_VALUES
        if bad_vals:
            problems.append(
                f"{run_dir / 'mask' / f'valid_{d}.tif'}: values {sorted(bad_vals)} outside {sorted(MASK_VALUES)}"
            )
            continue
        v = mask == 1
        if not (np.isfinite(ndvi[v]).all() and (np.abs(ndvi[v]) <= 1).all()):
            problems.append(f"{d}: NDVI not finite or outside [-1, 1] where mask = 1")
        if not np.isnan(ndvi[~v]).all():
            problems.append(f"{d}: NDVI has values where mask = 0 (expected NaN nodata)")
        arrays[d] = (ndvi, v)
    if problems:
        raise BenchmarkError("Run directory rejected before scoring:\n  - " + "\n  - ".join(problems))
    return {
        "arrays": arrays,
        "report": {
            "verified": True,
            "run_dir_files_checked": 2 * len(dates),
            "dates": dates,
            "manifest_sha256_from_tags": man_fp,
            "processing_sha256_from_tags": proc_fp,
            "checks": [
                "files present",
                "band count/dtype/nodata",
                "CRS/transform/shape = manifest grid",
                "manifest_sha256/processing_sha256/source_item tags",
                "mask values {0,1}",
                "NDVI finite in [-1,1] where mask=1, NaN where mask=0",
            ],
        },
    }


def load_run(run_dir: str | Path, manifest: dict, cfg: dict) -> tuple[dict[str, Observation], dict]:
    """Load NDVI + masks for every manifest date, only after validate_run accepts them."""
    v = validate_run(run_dir, manifest, cfg)
    obs = {d: Observation(d, ndvi, valid) for d, (ndvi, valid) in v["arrays"].items()}
    return obs, v["report"]


def run_split(obs: dict[str, Observation], split: dict, bcfg: dict) -> dict:
    tgt = split["date"]
    if tgt not in obs:
        raise BenchmarkError(f"Target {tgt} not among processed dates")
    target = obs[tgt]
    mask = block_mask(
        target.valid, bcfg["hidden_fraction"], bcfg["block_px"], split["seed"], bcfg["max_block_attempts"]
    )
    ev = Evaluator(tgt, target.ndvi, target.valid, mask)
    pub = ev.public_inputs()
    context = [o for d, o in obs.items() if d < tgt]  # past only; target never passed in
    preds, prov = {}, {}
    for method in ("latest", "median3"):
        r = predict(context, pub["target_date"], pub["missing"], method, bcfg["max_age_days"], bcfg["median_k"])
        preds[method] = r["pred"]
        h = pub["missing"] & np.isfinite(r["pred"])
        prov[method] = {
            "context_dates_within_age": r["context_dates"],
            "n_used_hist": {
                int(v): int(c) for v, c in zip(*np.unique(r["n_used"][h], return_counts=True), strict=True)
            },
            "mean_age_latest_days": round(float(r["age_latest"][h].mean()), 2) if h.any() else None,
        }
    scores = ev.score(preds)
    return {
        "target_date": tgt,
        "seed": split["seed"],
        "mask": {k: v for k, v in mask.items() if k != "hidden"},
        "scores": scores,
        "provenance": prov,
        "_preds": preds,
        "_hidden": mask["hidden"],
    }


def run_benchmark(cfg: dict, manifest: dict, run_dir: str | Path, out_dir: str | Path) -> dict:
    """Score the validation split, then the test split, on a run directory that passed validate_run."""
    keys = ["max_age_days", "median_k", "hidden_fraction", "block_px", "max_block_attempts", "validation", "test"]
    missing = [k for k in keys if k not in cfg.get("benchmark", {})]
    if missing:
        raise BenchmarkError(f"Config benchmark section is missing: {', '.join(missing)}")
    bcfg = cfg["benchmark"]
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not bcfg["validation"]["date"] < bcfg["test"]["date"]:
        raise BenchmarkError("Test date must be later than the validation date")
    if bcfg["validation"]["seed"] == bcfg["test"]["seed"]:
        raise BenchmarkError("Validation and test must use different seeds / mask layouts")
    obs, input_report = load_run(run_dir, manifest, cfg)  # raises before any score is written
    val = run_split(obs, bcfg["validation"], bcfg)
    test = run_split(obs, bcfg["test"], bcfg)
    result = {
        "task": "synthetic hidden-block NDVI estimation on SCL-usable pixels (not real-cloud ground truth)",
        "protocol": {
            **{k: v for k, v in bcfg.items()},
            "manifest_sha256": manifest["fingerprint_sha256"],
            "declared_protocol": (
                "Targets, seeds, block size, hidden fraction, age limit and both baselines are read from "
                "config. The project documentation declares these were fixed before the first scoring "
                "(2026-11-23) and that no setting was tuned on the test split. This file records the "
                "settings used for this run; it cannot verify the history of earlier runs."
            ),
        },
        "input_validation": input_report,
        "validation": {k: v for k, v in val.items() if not k.startswith("_")},
        "test": {k: v for k, v in test.items() if not k.startswith("_")},
    }
    (out_dir / "benchmark_results.json").write_text(json.dumps(result, indent=2, default=str))
    _panel(obs, test, out_dir / "benchmark_test_panel.png")
    return {**result, "_val": val, "_test": test}


def _panel(obs, split, path):
    import sys

    import matplotlib

    if "ipykernel" not in sys.modules:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    t = obs[split["target_date"]]
    hidden = split["_hidden"]
    pa = split["_preds"]["latest"]
    state = np.zeros(hidden.shape, np.uint8)  # 0 not observed, 1 observed, 2 hidden+estimated, 3 hidden+abstained
    state[t.valid] = 1
    state[hidden & np.isfinite(pa)] = 2
    state[hidden & ~np.isfinite(pa)] = 3
    fig, ax = plt.subplots(1, 3, figsize=(15, 5.2), constrained_layout=True)
    ax[0].imshow(
        state,
        cmap=ListedColormap(["#000000", "#bdbdbd", "#1f77b4", "#d62728"]),
        vmin=0,
        vmax=3,
        interpolation="nearest",
    )
    ax[0].set_title(
        "grey: observed   blue: hidden + estimated\nred: hidden + abstained   black: not observed", fontsize=9
    )
    show = np.where(hidden, pa, np.nan)
    im = ax[1].imshow(show, cmap="RdYlGn", vmin=-0.2, vmax=0.9)
    ax[1].set_title("Baseline A estimates (ESTIMATED, not observed)")
    fig.colorbar(im, ax=ax[1], shrink=0.8, label="NDVI")
    err = np.abs(np.where(hidden, pa - t.ndvi, np.nan))
    im2 = ax[2].imshow(err, cmap="magma", vmin=0, vmax=0.4)
    ax[2].set_title("|A − held-out NDVI| on hidden pixels")
    fig.colorbar(im2, ax=ax[2], shrink=0.8, label="NDVI units")
    for a in ax:
        a.set_xticks([])
        a.set_yticks([])
    fig.suptitle(
        f"Locked test {split['target_date']} (synthetic hidden blocks) | Contains modified Copernicus Sentinel data "
        f"{split['target_date'][:4]}",
        fontsize=10,
    )
    fig.savefig(path, dpi=100)
    plt.close(fig)
