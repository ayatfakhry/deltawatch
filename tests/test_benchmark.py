import numpy as np
import pytest

from deltawatch.benchmark import BenchmarkError, Evaluator, Observation, block_mask, metrics, predict, run_split

SHAPE = (60, 60)


def obs(d, value, valid=None):
    v = np.ones(SHAPE, bool) if valid is None else valid
    return Observation(d, np.where(v, np.float32(value), np.nan).astype(np.float32), v)


def test_baseline_a_latest_and_b_median_by_hand():
    ctx = [obs("2025-05-01", 0.2), obs("2025-05-10", 0.6), obs("2025-05-15", 0.4)]
    miss = np.ones(SHAPE, bool)
    a = predict(ctx, "2025-05-20", miss, "latest")
    b = predict(ctx, "2025-05-20", miss, "median3")
    assert np.allclose(a["pred"], 0.4) and (a["age_latest"] == 5).all() and (a["n_used"] == 1).all()
    assert np.allclose(b["pred"], 0.4) and (b["n_used"] == 3).all()  # median(0.4, 0.6, 0.2)


def test_max_age_and_invalid_context_are_respected():
    v = np.ones(SHAPE, bool)
    v[0, 0] = False
    ctx = [obs("2025-04-01", 0.9), obs("2025-05-10", 0.5, v)]  # 2025-04-01 is 49 days old
    p = predict(ctx, "2025-05-20", np.ones(SHAPE, bool), "median3")
    assert np.isnan(p["pred"][0, 0])  # abstain: only old/invalid context
    assert np.allclose(p["pred"][1:, :], 0.5) and p["context_dates"] == ["2025-05-10"]


def test_latest_skips_invalid_recent_pixel():
    v = np.ones(SHAPE, bool)
    v[3, 3] = False
    ctx = [obs("2025-05-05", 0.3), obs("2025-05-15", 0.7, v)]
    p = predict(ctx, "2025-05-20", np.ones(SHAPE, bool), "latest")
    assert p["pred"][3, 3] == pytest.approx(0.3) and p["age_latest"][3, 3] == 15


def test_future_context_is_rejected():
    with pytest.raises(BenchmarkError, match="Future"):
        predict([obs("2025-05-25", 0.5)], "2025-05-20", np.ones(SHAPE, bool), "latest")
    with pytest.raises(BenchmarkError, match="same-day"):
        predict([obs("2025-05-20", 0.5)], "2025-05-20", np.ones(SHAPE, bool), "latest")


def test_predictions_only_at_missing_pixels():
    miss = np.zeros(SHAPE, bool)
    miss[10:20, 10:20] = True
    p = predict([obs("2025-05-10", 0.5)], "2025-05-20", miss, "latest")
    assert np.isfinite(p["pred"]).sum() == 100 and np.isnan(p["pred"][~miss]).all()


def test_masks_deterministic_non_overlapping_inside_eligible():
    elig = np.ones(SHAPE, bool)
    elig[:, :15] = False
    m1 = block_mask(elig, 0.10, 6, seed=1, max_attempts=5000)
    m2 = block_mask(elig, 0.10, 6, seed=1, max_attempts=5000)
    m3 = block_mask(elig, 0.10, 6, seed=2, max_attempts=5000)
    assert np.array_equal(m1["hidden"], m2["hidden"]) and m1["blocks"] == m2["blocks"]
    assert not np.array_equal(m1["hidden"], m3["hidden"])
    assert not (m1["hidden"] & ~elig).any()
    assert m1["n_hidden"] >= m1["target_count"] == round(0.10 * elig.sum())
    cover = np.zeros(SHAPE, int)
    for r, c, b in m1["blocks"]:
        cover[r : r + b, c : c + b] += 1
    assert cover.max() == 1


def test_infeasible_design_fails_instead_of_weakening():
    elig = np.zeros(SHAPE, bool)
    elig[::7, ::7] = True  # scattered pixels only
    with pytest.raises(BenchmarkError, match="report this"):
        block_mask(elig, 0.10, 6, seed=1, max_attempts=2000)


def test_metrics_hand_calculated():
    truth = np.array([0.5, 0.5, 0.5, 0.5], np.float32)
    pred = np.array([0.6, 0.3, np.nan, 0.5], np.float32)
    hidden = np.array([True, True, True, False])
    m = metrics(pred, truth, hidden)
    assert m["n_hidden"] == 3 and m["n_predicted"] == 2 and m["coverage"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["mae"] == pytest.approx(0.15, abs=1e-5)
    assert m["rmse"] == pytest.approx(np.sqrt((0.01 + 0.04) / 2), abs=1e-5)
    assert m["bias"] == pytest.approx(-0.05, abs=1e-5)


def test_empty_support():
    m = metrics(np.full(3, np.nan, np.float32), np.ones(3, np.float32), np.ones(3, bool))
    assert m == {"n_hidden": 3, "n_predicted": 0, "coverage": 0.0, "mae": None, "rmse": None, "bias": None}


def test_intersection_scores_use_common_pixels():
    truth = np.full(4, 0.5, np.float32)
    hidden = np.ones(4, bool)
    mask = {"hidden": hidden}
    ev = Evaluator("2025-05-20", truth, hidden, mask)
    s = ev.score(
        {"latest": np.array([0.5, 0.5, 0.5, 0.9], np.float32), "median3": np.array([0.5, 0.5, 0.5, np.nan], np.float32)}
    )
    assert s["latest"]["n_predicted"] == 4 and s["median3"]["n_predicted"] == 3
    assert s["intersection"]["latest"]["n_predicted"] == 3 and s["intersection"]["latest"]["mae"] == 0.0


def _series(target_value):
    rng = np.random.default_rng(0)
    d = {
        f"2025-05-{k:02d}": Observation(
            f"2025-05-{k:02d}", rng.uniform(0, 0.8, SHAPE).astype(np.float32), np.ones(SHAPE, bool)
        )
        for k in (1, 8, 15)
    }
    tv = np.ones(SHAPE, bool)
    d["2025-05-20"] = Observation("2025-05-20", np.full(SHAPE, target_value, np.float32), tv)
    return d


CFG = {"hidden_fraction": 0.10, "block_px": 6, "max_block_attempts": 5000, "max_age_days": 30, "median_k": 3}


def test_predictions_do_not_depend_on_hidden_truth():
    a = run_split(_series(0.1), {"date": "2025-05-20", "seed": 7}, CFG)
    b = run_split(_series(0.9), {"date": "2025-05-20", "seed": 7}, CFG)
    for m in ("latest", "median3"):
        np.testing.assert_array_equal(a["_preds"][m], b["_preds"][m])
    assert a["scores"]["latest"]["mae"] != b["scores"]["latest"]["mae"]  # only the scores change


def test_same_input_repeatability_and_context_precedes_target():
    a = run_split(_series(0.5), {"date": "2025-05-20", "seed": 7}, CFG)
    b = run_split(_series(0.5), {"date": "2025-05-20", "seed": 7}, CFG)
    assert a["scores"] == b["scores"] and a["mask"]["blocks"] == b["mask"]["blocks"]
    assert all(d < "2025-05-20" for d in a["provenance"]["median3"]["context_dates_within_age"])
