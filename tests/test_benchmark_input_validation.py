"""validate_run / run_benchmark must refuse run directories that do not belong to the manifest + config.

All tests are offline: they build a real run with run_core on the synthetic two-date scene, then
tamper with copies of it. Nothing is repaired or reprojected by the code under test.
"""

import json
import shutil

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from deltawatch.benchmark import BenchmarkError, run_benchmark, validate_run
from deltawatch.config import load_config
from deltawatch.manifest import load_manifest, save_manifest
from deltawatch.pipeline import run_core

DATES = ["2025-04-05", "2025-04-17"]


@pytest.fixture
def good_run(scene):
    cfg = load_config("config/default.yaml")
    cfg["benchmark"].update(block_px=3, validation={"date": DATES[0], "seed": 1}, test={"date": DATES[1], "seed": 2})
    run_core(cfg, scene / "manifest.json", scene / "out")
    return cfg, load_manifest(scene / "manifest.json"), scene / "out", scene


def _copy(run, scene, name):
    dst = scene / name
    shutil.copytree(run, dst)
    return dst


def _rewrite(path, arr=None, **profile_changes):
    """Rewrite a GeoTIFF keeping its tags, optionally with new pixels / profile."""
    with rasterio.open(path) as ds:
        prof, tags, data = ds.profile, ds.tags(), ds.read(1)
    prof.update(profile_changes)
    data = data if arr is None else arr
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(data.astype(prof["dtype"]), 1)
        dst.update_tags(**tags)


def _set_tag(path, key, value):
    with rasterio.open(path, "r+") as ds:
        ds.update_tags(**{key: value})


# ---------------------------------------------------------------- positive
def test_valid_run_is_accepted_and_reported(good_run):
    cfg, man, run, scene = good_run
    report = validate_run(run, man, cfg)["report"]
    assert report["verified"] and report["run_dir_files_checked"] == 4
    assert report["manifest_sha256_from_tags"] == man["fingerprint_sha256"]
    res = run_benchmark(cfg, man, run, scene / "bench")
    saved = json.loads((scene / "bench" / "benchmark_results.json").read_text())
    assert saved["input_validation"]["verified"] is True
    assert "cannot verify the history" in saved["protocol"]["declared_protocol"]
    assert res["test"]["scores"]["latest"]["mae"] == 0.0


# ---------------------------------------------------------------- negatives
def _assert_rejected(cfg, man, run, scene, match):
    with pytest.raises(BenchmarkError, match=match):
        run_benchmark(cfg, man, run, scene / "bench_rejected")
    assert not (scene / "bench_rejected" / "benchmark_results.json").exists()  # nothing written


def test_other_run_with_same_dates_but_different_provenance(good_run):
    cfg, man, run, scene = good_run
    other_cfg = load_config("config/default.yaml")
    other_cfg["quality"]["scl_usable_classes"] = [4, 5]  # different processing policy
    run_core(other_cfg, scene / "manifest.json", scene / "other")
    _assert_rejected(cfg, man, scene / "other", scene, "processing_sha256")


def test_manifest_hash_mismatch_in_tags(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "bad_manifest_tag")
    _set_tag(bad / "ndvi" / f"ndvi_{DATES[1]}.tif", "manifest_sha256", "0" * 64)
    _assert_rejected(cfg, man, bad, scene, r"ndvi_2025-04-17\.tif: tag manifest_sha256")


def test_scoring_against_a_different_manifest(good_run):
    cfg, man, run, scene = good_run
    other = dict(man)
    other["items"] = [dict(i) for i in man["items"]]
    other["items"][0]["id"] = "S2X_SOME_OTHER_ITEM"
    other = save_manifest(other, scene / "other_manifest.json")
    _assert_rejected(cfg, other, run, scene, "manifest_sha256")


def test_tampered_manifest_dict_rejected(good_run):
    cfg, man, run, scene = good_run
    edited = {**man, "tile": "36RUV"}  # content changed, fingerprint not
    _assert_rejected(cfg, edited, run, scene, "fingerprint")


def test_processing_hash_tag_mismatch(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "bad_proc")
    _set_tag(bad / "mask" / f"valid_{DATES[0]}.tif", "processing_sha256", "f" * 64)
    _assert_rejected(cfg, man, bad, scene, r"valid_2025-04-05\.tif: tag processing_sha256")


def test_missing_provenance_tag_is_not_guessed(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "no_tag")
    p = bad / "ndvi" / f"ndvi_{DATES[0]}.tif"
    with rasterio.open(p) as ds:
        prof, data, tags = ds.profile, ds.read(1), ds.tags()
    tags.pop("source_item")
    with rasterio.open(p, "w", **prof) as dst:
        dst.write(data, 1)
        dst.update_tags(**tags)
    _assert_rejected(cfg, man, bad, scene, "tag 'source_item' missing")


def test_shifted_transform(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "shifted")
    _rewrite(bad / "mask" / f"valid_{DATES[1]}.tif", transform=from_origin(300010.0, 3426000.0, 10.0, 10.0))
    _assert_rejected(cfg, man, bad, scene, "does not match manifest grid")


def test_wrong_crs(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "crs")
    _rewrite(bad / "ndvi" / f"ndvi_{DATES[0]}.tif", crs="EPSG:32635")
    _assert_rejected(cfg, man, bad, scene, "does not match manifest grid")


def test_wrong_shape(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "shape")
    p = bad / "ndvi" / f"ndvi_{DATES[0]}.tif"
    with rasterio.open(p) as ds:
        arr = ds.read(1)
    _rewrite(p, arr[:-1, :], height=arr.shape[0] - 1)
    _assert_rejected(cfg, man, bad, scene, "does not match manifest grid")


def test_missing_file(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "missing")
    (bad / "mask" / f"valid_{DATES[0]}.tif").unlink()
    _assert_rejected(cfg, man, bad, scene, r"valid_2025-04-05\.tif: missing file")


def test_wrong_dtype_and_nodata(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "dtype")
    _rewrite(bad / "mask" / f"valid_{DATES[0]}.tif", nodata=0)
    _assert_rejected(cfg, man, bad, scene, "nodata 0.0, expected 255")


def test_mask_value_outside_contract(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "maskval")
    p = bad / "mask" / f"valid_{DATES[0]}.tif"
    with rasterio.open(p) as ds:
        m = ds.read(1)
    m[0, 0] = 2
    _rewrite(p, m)
    _assert_rejected(cfg, man, bad, scene, r"values \[2\]")


def test_ndvi_value_where_mask_is_zero(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "inconsistent_invalid")
    p = bad / "ndvi" / f"ndvi_{DATES[1]}.tif"  # date 2 has cloud pixels with mask 0
    with rasterio.open(p) as ds:
        a = ds.read(1)
    a[0, 0] = 0.5
    _rewrite(p, a)
    _assert_rejected(cfg, man, bad, scene, "values where mask = 0")


def test_nan_or_out_of_range_where_mask_is_one(good_run):
    cfg, man, run, scene = good_run
    for name, value in (("nan_valid", np.nan), ("oor_valid", 1.5)):
        bad = _copy(run, scene, name)
        p = bad / "ndvi" / f"ndvi_{DATES[0]}.tif"
        with rasterio.open(p) as ds:
            a = ds.read(1)
        a[5, 5] = value
        _rewrite(p, a)
        _assert_rejected(cfg, man, bad, scene, r"not finite or outside \[-1, 1\]")


def test_every_file_is_checked_not_just_the_first(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "last_file_bad")
    _set_tag(bad / "mask" / f"valid_{DATES[-1]}.tif", "source_item", "WRONG")
    _assert_rejected(cfg, man, bad, scene, r"valid_2025-04-17\.tif: tag source_item")


def test_all_problems_listed_together(good_run):
    cfg, man, run, scene = good_run
    bad = _copy(run, scene, "many")
    (bad / "ndvi" / f"ndvi_{DATES[0]}.tif").unlink()
    _set_tag(bad / "mask" / f"valid_{DATES[1]}.tif", "processing_sha256", "x")
    with pytest.raises(BenchmarkError) as e:
        validate_run(bad, man, cfg)
    msg = str(e.value)
    assert "ndvi_2025-04-05.tif: missing file" in msg and "valid_2025-04-17.tif: tag processing_sha256" in msg
