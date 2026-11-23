"""End-to-end run_core on synthetic local rasters: no network, exercises the real code path."""

import numpy as np
import pandas as pd
import pytest
import rasterio

from deltawatch.config import load_config
from deltawatch.pipeline import run_core


def test_run_core_end_to_end(scene):
    cfg = load_config("config/default.yaml")
    res = run_core(cfg, scene / "manifest.json", scene / "out")
    qa = res["qa"].set_index("date")
    assert qa.loc["2025-04-05", "valid_fraction"] == 1.0
    assert qa.loc["2025-04-05", "ndvi_p50"] == pytest.approx((0.41 - 0.08) / 0.49, abs=1e-4)
    d2 = qa.loc["2025-04-17"]
    assert d2["valid_fraction"] == pytest.approx((144 - 16 - 2) / 144, abs=1e-4)
    assert d2["nodata_red_px"] == 1 and d2["nodata_marked_valid_px"] == 0
    assert d2["ndvi_out_of_range_px"] == 1 and d2["ndvi_out_of_range_with_positive_refl_px"] == 0
    assert d2["scl_9"] == 16 and d2["valid_fraction_if_dilated"] < d2["valid_fraction"]
    assert qa["roundtrip_ok"].all() and qa["valid_ndvi_max_abs"].max() <= 1
    out = scene / "out"
    assert (out / "qa_per_date.csv").exists() and (out / "qa_ndvi_panel.png").exists()
    assert res["meta"]["memory"]["measured_peak_process_rss_MB"] > 0
    with rasterio.open(out / "ndvi" / "ndvi_2025-04-17.tif") as ds:
        a = ds.read(1)
        assert np.isnan(ds.nodata) and np.isnan(a[0:4, 0:4]).all()  # cloud stays NaN, never 0
        assert ds.tags()["source_item"] == "S2X_T36RTV_2025-04-17"


def test_cached_outputs_reused_only_with_matching_fingerprint(scene):
    cfg = load_config("config/default.yaml")
    run_core(cfg, scene / "manifest.json", scene / "out")
    again = run_core(cfg, scene / "manifest.json", scene / "out")["qa"]
    assert again["reused_cached_output"].all()
    cfg["quality"]["scl_usable_classes"] = [4, 5]  # policy change -> must recompute
    changed = run_core(cfg, scene / "manifest.json", scene / "out")["qa"]
    assert not changed["reused_cached_output"].any()


def test_repeatable_outputs(scene):
    cfg = load_config("config/default.yaml")
    a = run_core(cfg, scene / "manifest.json", scene / "o1", force=True)["qa"].drop(
        columns=["seconds", "reused_cached_output"]
    )
    b = run_core(cfg, scene / "manifest.json", scene / "o2", force=True)["qa"].drop(
        columns=["seconds", "reused_cached_output"]
    )
    pd.testing.assert_frame_equal(a, b)


def test_benchmark_end_to_end_on_synthetic_run(scene):
    from deltawatch.benchmark import BenchmarkError, run_benchmark
    from deltawatch.manifest import load_manifest

    cfg = load_config("config/default.yaml")
    run_core(cfg, scene / "manifest.json", scene / "out")
    cfg["benchmark"].update(
        block_px=3, validation={"date": "2025-04-05", "seed": 1}, test={"date": "2025-04-17", "seed": 2}
    )
    res = run_benchmark(cfg, load_manifest(scene / "manifest.json"), scene / "out", scene / "bench")
    assert res["validation"]["scores"]["latest"]["n_predicted"] == 0  # no earlier context: abstain
    t = res["test"]["scores"]["latest"]
    assert t["coverage"] == 1.0 and t["mae"] == 0.0  # constant scene: exact persistence
    assert (scene / "bench" / "benchmark_results.json").exists() and (
        scene / "bench" / "benchmark_test_panel.png"
    ).exists()
    cfg["benchmark"]["test"]["seed"] = 1
    with pytest.raises(BenchmarkError, match="different seeds"):
        run_benchmark(cfg, load_manifest(scene / "manifest.json"), scene / "out", scene / "bench")
