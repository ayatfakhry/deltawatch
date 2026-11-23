"""compare_to_reference must detect real changes and only tolerate the documented v0.3 fields."""

import json
import shutil

import rasterio

from deltawatch.benchmark import run_benchmark
from deltawatch.config import load_config
from deltawatch.manifest import load_manifest
from deltawatch.pipeline import run_core
from deltawatch.reference import compare_benchmark, compare_to_reference


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
