"""compare_to_reference must detect real changes and only tolerate the documented v0.3 fields."""

import json
import shutil

import numpy as np
import pandas as pd
import pytest
import rasterio

from deltawatch.benchmark import run_benchmark
from deltawatch.config import load_config
from deltawatch.manifest import load_manifest
from deltawatch.pipeline import run_core
from deltawatch.reference import QA_FLOAT_ATOL, QA_FLOAT_RTOL, compare_benchmark, compare_qa, compare_to_reference


def _make_reference(scene):
    cfg = load_config("config/default.yaml")
    cfg["benchmark"].update(
        block_px=3, validation={"date": "2025-04-05", "seed": 1}, test={"date": "2025-04-17", "seed": 2}
    )
    run_core(cfg, scene / "manifest.json", scene / "run")
    run_benchmark(cfg, load_manifest(scene / "manifest.json"), scene / "run", scene / "run" / "benchmark")
    ref = scene / "ref"
    (ref / "colab_run" / "benchmark").mkdir(parents=True)
    shutil.copytree(scene / "run" / "ndvi", ref / "geotiffs" / "ndvi")
    shutil.copytree(scene / "run" / "mask", ref / "geotiffs" / "mask")
    shutil.copy(scene / "run" / "qa_per_date.csv", ref / "colab_run")
    shutil.copy(scene / "run" / "benchmark" / "benchmark_results.json", ref / "colab_run" / "benchmark")
    return ref


def test_identical_run_is_preserved(scene):
    ref = _make_reference(scene)
    rep = compare_to_reference(scene / "run", ref, load_manifest(scene / "manifest.json"))
    assert rep["all_preserved"] and rep["rasters"]["files"] == 4


def test_changed_pixel_is_detected(scene):
    ref = _make_reference(scene)
    p = ref / "geotiffs" / "ndvi" / "ndvi_2025-04-05.tif"
    with rasterio.open(p, "r+") as ds:
        a = ds.read(1)
        a[3, 3] += 0.001
        ds.write(a, 1)
    rep = compare_to_reference(scene / "run", ref, load_manifest(scene / "manifest.json"))
    assert not rep["all_preserved"] and not rep["rasters"]["all_equal"]


def test_benchmark_score_change_detected_but_documented_fields_tolerated(scene, tmp_path):
    _make_reference(scene)
    run_json = scene / "run" / "benchmark" / "benchmark_results.json"
    old = json.loads(run_json.read_text())
    old["protocol"].pop("declared_protocol")
    old.pop("input_validation")
    old["protocol"]["tuning"] = "v0.2 wording"
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(old))
    r = compare_benchmark(run_json, legacy)
    assert r["equal_except_documented"] and "protocol.tuning" in r["documented_changes_present"]
    old["test"]["scores"]["latest"]["mae"] = 0.5
    legacy.write_text(json.dumps(old))
    r = compare_benchmark(run_json, legacy)
    assert not r["equal_except_documented"] and r["undocumented_differences"] == ["test.scores.latest.mae"]


# ---------------------------------------------------------------- v0.4: QA float tolerance
def _qa_pair(scene, tmp_path):
    ref = _make_reference(scene)
    ref_csv = ref / "colab_run" / "qa_per_date.csv"
    run_csv = tmp_path / "qa_run.csv"
    return pd.read_csv(ref_csv), ref_csv, run_csv


def test_tiny_float_difference_passes(scene, tmp_path):
    df, ref_csv, run_csv = _qa_pair(scene, tmp_path)
    df.loc[1, "ndvi_p2"] += 1e-8
    df.loc[0, "ndvi_p98"] += 1e-8
    df.to_csv(run_csv, index=False)
    r = compare_qa(run_csv, ref_csv)
    assert r["equal"] and r["failures"] == []
    assert r["float_tolerance"] == {"rtol": QA_FLOAT_RTOL, "atol": QA_FLOAT_ATOL, "nan_equals_nan": True}
    assert 0 < r["float_columns"]["ndvi_p2"]["max_abs_diff"] < 1e-7


def test_meaningful_float_difference_fails_with_column_and_date(scene, tmp_path):
    df, ref_csv, run_csv = _qa_pair(scene, tmp_path)
    df.loc[1, "ndvi_p50"] += 1e-3
    df.to_csv(run_csv, index=False)
    r = compare_qa(run_csv, ref_csv)
    assert not r["equal"]
    f = r["failures"][0]
    assert f["column"] == "ndvi_p50" and f["dates"] == [df.loc[1, "date"]]
    assert r["float_columns"]["ndvi_p50"]["max_abs_diff"] == pytest.approx(1e-3, rel=1e-3)


@pytest.mark.parametrize(
    "col,new", [("scl_9", None), ("nodata_red_px", None), ("scale_offset_source", "x"), ("id", "y")]
)
def test_integer_or_text_change_fails(scene, tmp_path, col, new):
    df, ref_csv, run_csv = _qa_pair(scene, tmp_path)
    if new is None:
        df.loc[0, col] = df.loc[0, col] + 1  # smallest possible integer change
    else:
        df[col] = df[col].astype(object)
        df.loc[0, col] = str(df.loc[0, col]) + new
    df.to_csv(run_csv, index=False)
    r = compare_qa(run_csv, ref_csv)
    assert not r["equal"] and r["failures"][0]["column"] == col
    assert col in r["exact_columns"]


def test_nan_versus_value_fails(scene, tmp_path):
    df, ref_csv, run_csv = _qa_pair(scene, tmp_path)
    df.loc[0, "ndvi_p2"] = np.nan
    df.to_csv(run_csv, index=False)
    r = compare_qa(run_csv, ref_csv)
    assert not r["equal"] and r["failures"][0]["column"] == "ndvi_p2"


def test_end_to_end_report_contains_tolerance(scene):
    ref = _make_reference(scene)
    rep = compare_to_reference(scene / "run", ref, load_manifest(scene / "manifest.json"))
    assert rep["all_preserved"] and rep["qa"]["float_tolerance"]["rtol"] == QA_FLOAT_RTOL
