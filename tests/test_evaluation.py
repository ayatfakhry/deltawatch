"""Part 3: separate multi-target / multi-seed evaluation (synthetic data, no network)."""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from deltawatch.benchmark import Observation, block_mask, run_split
from deltawatch.evaluation import (
    block_bootstrap,
    block_index,
    eligible_targets,
    evaluate_target_seed,
    joint_support,
)

DATES = ["2026-06-01", "2026-06-11", "2026-06-21", "2026-07-01", "2026-08-15"]
BCFG = {"max_age_days": 30, "median_k": 3, "hidden_fraction": 0.10, "block_px": 10, "max_block_attempts": 20000}
ECFG = {"bootstrap": {"n_resamples": 200, "seed": 7}, "seeds": [301, 302], "min_earlier_dates_within_age": 2}


def _obs(seed=0, shape=(60, 60)):
    rng = np.random.default_rng(seed)
    out = {}
    for i, d in enumerate(DATES):
        ndvi = (0.2 + 0.1 * i + 0.05 * rng.standard_normal(shape)).astype("float32")
        valid = rng.random(shape) > 0.05
        out[d] = Observation(d, np.where(valid, ndvi, np.nan).astype("float32"), valid)
    return out


def _cfg():
    return {"benchmark": BCFG, "evaluation": ECFG}


def test_hidden_truth_does_not_change_predictions():
    obs = _obs()
    t = "2026-06-21"
    r1 = run_split(obs, {"date": t, "seed": 301}, BCFG)
    tampered = dict(obs)
    nd = obs[t].ndvi.copy()
    nd[r1["_hidden"]] = 0.999  # change only the hidden truth
    tampered[t] = Observation(t, nd, obs[t].valid)
    r2 = run_split(tampered, {"date": t, "seed": 301}, BCFG)
    for m in ("latest", "median3"):
        np.testing.assert_array_equal(r1["_preds"][m], r2["_preds"][m])
    assert r1["scores"]["latest"]["mae"] != r2["scores"]["latest"]["mae"]  # truth only reaches scoring


def test_no_future_or_same_day_context():
    obs = _obs()
    t = "2026-06-21"
    base = evaluate_target_seed(obs, t, 301, _cfg())
    changed = dict(obs)
    for d in ("2026-07-01", "2026-08-15"):  # future dates replaced with garbage
        changed[d] = Observation(d, np.full_like(obs[d].ndvi, -0.9), obs[d].valid)
    r = evaluate_target_seed(changed, t, 301, _cfg())
    assert r["A"] == base["A"] and r["B"] == base["B"]
    assert base["context"]["earlier_dates_within_age"] == ["2026-06-11", "2026-06-01"]
    assert all(d < t for d in base["context"]["earlier_dates_within_age"])


def test_target_rule_and_seeds():
    tg = {x["date"]: x for x in eligible_targets(DATES, 30, 2)}
    assert [d for d, x in tg.items() if x["is_target"]] == ["2026-06-21", "2026-07-01"]
    assert not tg["2026-06-11"]["is_target"]  # only one earlier date
    assert not tg["2026-08-15"]["is_target"]  # 2026-07-01 is 45 days earlier
    cfg = yaml.safe_load(open("config/eval_new_aoi_2026.yaml"))
    assert cfg["evaluation"]["seeds"] == [301, 302, 303, 304, 305]
    assert cfg["evaluation"]["bootstrap"]["n_resamples"] == 1000
    assert cfg["evaluation"]["min_earlier_dates_within_age"] == 2


def test_joint_support_on_synthetic_case():
    hidden = np.zeros((2, 4), bool)
    hidden[0] = True
    hidden[1, :2] = True  # 6 hidden pixels
    truth = np.zeros((2, 4))
    pa = np.array([[0.1, 0.1, np.nan, 0.1], [0.2, np.nan, 9, 9]])
    pb = np.array([[0.3, np.nan, 0.3, 0.3], [np.nan, np.nan, 9, 9]])
    js = joint_support(pa, pb, truth, hidden)
    assert js["n"] == 2  # (0,0) and (0,3)
    assert js["only_A"] == 2  # (0,1), (1,0)
    assert js["only_B"] == 1  # (0,2)
    assert js["A"]["mae"] == pytest.approx(0.1) and js["B"]["mae"] == pytest.approx(0.3)
    assert js["mae_A_minus_B"] == pytest.approx(-0.2)


def test_bootstrap_uses_actual_blocks_and_is_deterministic():
    obs = _obs()
    t = "2026-06-21"
    mask = block_mask(obs[t].valid, 0.10, 10, 301, 20000)
    idx = block_index(mask["hidden"], mask["blocks"])
    assert len(idx) == len(mask["blocks"])
    flat = np.concatenate(idx)
    assert len(flat) == len(set(flat)) == int(mask["hidden"].sum())  # exactly the hidden pixels, no overlap
    np.testing.assert_array_equal(np.sort(flat), np.flatnonzero(mask["hidden"]))
    for (r0, c0, b), ix in zip(mask["blocks"], idx, strict=True):  # each group lies inside its own square
        rr, cc = np.divmod(ix, mask["hidden"].shape[1])
        assert rr.min() >= r0 and rr.max() < r0 + b and cc.min() >= c0 and cc.max() < c0 + b
    r = run_split(obs, {"date": t, "seed": 301}, BCFG)
    truth = np.where(r["_hidden"], obs[t].ndvi, np.nan)
    args = (r["_preds"]["latest"], r["_preds"]["median3"], truth, r["_hidden"], r["mask"]["blocks"], 300)
    a, b, c = block_bootstrap(*args, seed=11), block_bootstrap(*args, seed=11), block_bootstrap(*args, seed=12)
    assert a == b and a != c
    assert a["n_blocks"] == len(mask["blocks"])
    lo, hi = a["A_mae"]
    assert lo <= r["scores"]["latest"]["mae"] <= hi


def test_bootstrap_resamples_whole_blocks():
    """With error confined to one block, every resample's MAE is a multiple of that block's share."""
    hidden = np.zeros((20, 20), bool)
    hidden[:10, :10] = hidden[10:, 10:] = True
    blocks = [[0, 0, 10], [10, 10, 10]]
    truth = np.where(hidden, 0.0, np.nan)
    pa = np.where(hidden, 0.0, np.nan)
    pa[:10, :10] = 1.0  # all error in block 0
    out = block_bootstrap(pa, pa, truth, hidden, blocks, 500, seed=3)
    assert out["n_blocks"] == 2
    assert out["A_mae"] == [0.0, 1.0]  # pixel-level resampling would give a narrow interval around 0.5


def test_tile_rule():
    from deltawatch.evaluation import choose_tile

    assert choose_tile({"36RUU": 3}) == "36RUU"
    assert choose_tile({"36RUU": 30, "36RUV": 35}) == "36RUV"
    assert choose_tile({"36RUV": 35, "36RUU": 35}) == "36RUU"  # tie -> alphabetical


def test_zone_band_and_bucket_listing_offline():
    from deltawatch.evaluation import zone_band

    assert zone_band([31.60, 30.60, 31.65, 30.65]) == ("36", "R")
    assert zone_band([30.80, 28.10, 30.85, 28.15]) == ("36", "R")


# ---------------------------------------------------------------- end-to-end, offline
def _scene_n(tmp_path, dates, shape=(60, 60)):
    """N-date synthetic scene (10 m red/nir, 20 m SCL) with NDVI rising over time + noise."""
    import rasterio
    from rasterio.transform import from_origin

    from deltawatch.manifest import save_manifest

    rng = np.random.default_rng(5)
    items = []
    for k, d in enumerate(dates):
        nir = (3000 + 400 * k + rng.integers(-300, 300, shape)).astype(np.uint16)
        red = np.full(shape, 1800, np.uint16)
        scl = np.full((shape[0] // 2, shape[1] // 2), 4, np.uint8)
        if k == 3:
            scl[:3, :3] = 9
        paths = {}
        for role, arr, res in (("red", red, 10), ("nir", nir, 10), ("scl", scl, 20)):
            p = tmp_path / f"{role}_{d}.tif"
            with rasterio.open(
                p,
                "w",
                driver="GTiff",
                height=arr.shape[0],
                width=arr.shape[1],
                count=1,
                dtype=arr.dtype.name,
                crs="EPSG:32636",
                nodata=0,
                transform=from_origin(300000.0, 3426000.0, res, res),
            ) as dst:
                dst.write(arr, 1)
                if role != "scl":
                    dst.scales, dst.offsets = (0.0001,), (-0.1,)
            paths[role] = p
        items.append(
            {
                "id": f"S2X_T36RUU_{d}",
                "date": d,
                "datetime": f"{d}T08:33:00Z",
                "assets": {
                    r: {"href": str(p), "scale": None if r == "scl" else 0.0001, "offset": None if r == "scl" else -0.1}
                    for r, p in paths.items()
                },
            }
        )
    m = {
        "manifest_version": 1,
        "frozen_at_utc": "x",
        "stac": {"collection": "sentinel-2-c1-l2a"},
        "grid": {"crs": "EPSG:32636", "transform": [10.0, 0.0, 300000.0, 0.0, -10.0, 3426000.0], "shape": list(shape)},
        "quality_policy": {"scl_usable_classes": [4, 5, 6]},
        "items": items,
    }
    return save_manifest(m, tmp_path / "manifest.json")


def test_run_evaluation_end_to_end_offline(tmp_path):
    import json

    from deltawatch.config import load_config
    from deltawatch.evaluation import describe_context, run_evaluation
    from deltawatch.pipeline import run_core

    man = _scene_n(tmp_path, DATES)
    cfg = load_config("config/eval_new_aoi_2026.yaml")
    cfg["benchmark"]["block_px"] = 10  # the 60x60 synthetic grid is too small for 20 px blocks
    cfg["evaluation"]["bootstrap"]["n_resamples"] = 100
    run_core(cfg, tmp_path / "manifest.json", tmp_path / "run")
    out = tmp_path / "eval"
    describe_context(cfg, man, tmp_path / "run", out)
    before = (out / "context_description.json").read_text()
    res = run_evaluation(cfg, man, tmp_path / "run", out)
    assert (out / "context_description.json").read_text() == before  # pre-score record kept
    assert res["plan"]["targets"] == ["2026-06-21", "2026-07-01"]
    assert len(res["results"]) == 2 * 5 and res["failures"] == []
    assert sorted({(r["target"], r["seed"]) for r in res["results"]}) == [
        (t, s) for t in ("2026-06-21", "2026-07-01") for s in (301, 302, 303, 304, 305)
    ]
    for f in (
        "evaluation_results.json",
        "evaluation_per_target_seed.csv",
        "evaluation_summary_per_target.csv",
        "evaluation_summary.md",
        "plan.json",
    ):
        assert (out / f).exists()
    saved = json.loads((out / "evaluation_results.json").read_text())
    assert saved["manifest_sha256"] == man["fingerprint_sha256"]
    assert res["_summary"]["n_seeds"].tolist() == [5, 5]


def test_fallback_order_is_mechanical_only(tmp_path, monkeypatch):
    """Candidate fails mechanically -> first fallback is tried next; the attempt log keeps both."""
    import pandas as pd

    from deltawatch import evaluation as ev
    from deltawatch.config import load_config
    from deltawatch.errors import SelectionError

    cfg = load_config("config/eval_new_aoi_2026.yaml")
    seen = []

    def fake_discover(cfg, bbox, items):
        seen.append(bbox)
        if bbox == [31.60, 30.60, 31.65, 30.65]:
            raise SelectionError("Only 5 dates pass (need 8)")
        empty = pd.DataFrame({"id": [], "error": []})
        return {"tile": "36RUV", "eligible": [1] * 9, "selected": [1] * 8, "screen": empty, "dupes": empty}

    monkeypatch.setattr(ev, "search_items", lambda c: [])
    monkeypatch.setattr(ev, "discover_aoi", fake_discover)
    monkeypatch.setattr(ev, "build_manifest", lambda cfg, d, src, attempts, n: {"attempts": attempts})
    monkeypatch.setattr(ev, "save_manifest", lambda m, p: m)
    r = ev.discover_and_freeze(cfg, tmp_path)
    assert seen == [[31.60, 30.60, 31.65, 30.65], [30.50, 30.60, 30.55, 30.65]]
    assert [a["result"] for a in r["attempts"]] == ["failed (mechanical)", "used"]
    assert "need 8" in r["attempts"][0]["reason"]
