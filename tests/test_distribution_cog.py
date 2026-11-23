"""COG conversion: equality proof, validation, and refusal of untrusted inputs. No network.

Needs the distribution extra (pip install -e ".[dev,distribution]"). It fails, not skips, without it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from deltawatch.distribution.cog import (
    CogSettings,
    DistributionError,
    build_dates,
    check_pair_semantics,
    compare_rasters,
    convert_to_cog,
    load_distribution_config,
    validate_cog,
)

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "distribution.yaml"
CRS = "EPSG:32636"
SMALL = CogSettings(
    blocksize=64,
    compress="DEFLATE",
    predictor={"ndvi": 3, "mask": 2},
    overview_levels=2,
    overview_resampling={"ndvi": "average", "mask": "mode"},
)
TAGS = {"source_item": "S2A_TEST", "manifest_sha256": "abc", "units": "NDVI (unitless)"}


def _write(path, arr, nodata, tags=TAGS, tiled=False):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype=arr.dtype,
        crs=CRS,
        transform=from_origin(300000.0, 3426000.0, 10.0, 10.0),
        nodata=nodata,
        compress="DEFLATE",
        tiled=tiled,
    ) as d:
        d.write(arr, 1)
        d.update_tags(**tags)
    return path


@pytest.fixture
def pair(tmp_path):
    rng = np.random.default_rng(7)
    ndvi = rng.uniform(-0.2, 0.9, (150, 201)).astype("float32")  # odd width on purpose
    ndvi[10:40, 20:90] = np.nan
    ndvi[0, 0] = -0.0  # signed zero must survive bit-for-bit
    mask = np.isfinite(ndvi).astype("uint8")
    return _write(tmp_path / "ndvi.tif", ndvi, np.nan), _write(tmp_path / "valid.tif", mask, 255)


def test_synthetic_conversion_is_valid_and_identical(pair, tmp_path):
    ndvi_src, mask_src = pair
    ndvi_cog = convert_to_cog(ndvi_src, tmp_path / "out" / "ndvi.tif", "ndvi", SMALL)
    mask_cog = convert_to_cog(mask_src, tmp_path / "out" / "valid.tif", "mask", SMALL)
    for cog in (ndvi_cog, mask_cog):
        v = validate_cog(cog)
        assert v["valid"], v
    r = compare_rasters(ndvi_src, ndvi_cog, "ndvi")
    assert r["identical"], r["checks"]
    assert r["detail"]["cog_overviews"] == [2, 4]
    assert r["detail"]["cog_overview_resampling_tag"] == "AVERAGE"
    m = compare_rasters(mask_src, mask_cog, "mask")
    assert m["identical"], m["checks"]
    assert m["detail"]["cog_overview_resampling_tag"] == "MODE"
    assert check_pair_semantics(ndvi_cog, mask_cog) == {"same_grid": True, "mask_matches_ndvi_nan": True}


def test_overviews_do_not_spread_nan_or_invent_mask_values(pair, tmp_path):
    ndvi_cog = convert_to_cog(pair[0], tmp_path / "ndvi.tif", "ndvi", SMALL)
    mask_cog = convert_to_cog(pair[1], tmp_path / "valid.tif", "mask", SMALL)
    with rasterio.open(ndvi_cog, overview_level=0) as d:
        ov = d.read(1)
    # The NaN hole is 30 x 70 px; at /2 it may cover at most ~15 x 35 overview pixels plus edges.
    assert 0 < np.isnan(ov).sum() <= 16 * 36
    with rasterio.open(mask_cog, overview_level=1) as d:
        assert set(np.unique(d.read(1)).tolist()) <= {0, 1}


@pytest.mark.parametrize("change", ["pixel", "nan_to_zero", "signed_zero", "nodata", "tag"])
def test_comparison_detects_every_kind_of_change(pair, tmp_path, change):
    src = pair[0]
    with rasterio.open(src) as d:
        arr, tags, nodata = d.read(1), d.tags(), d.nodata
    if change == "pixel":
        arr[100, 100] = np.nextafter(arr[100, 100], np.float32(2))  # one ULP
    elif change == "nan_to_zero":
        arr[15, 30] = 0.0
    elif change == "signed_zero":
        arr[0, 0] = 0.0
    elif change == "nodata":
        nodata = -9999.0
    elif change == "tag":
        tags = {**tags, "source_item": "OTHER"}
    altered = _write(tmp_path / "altered.tif", arr, nodata, tags)
    assert compare_rasters(src, altered, "ndvi")["identical"] is False


def test_mask_comparison_detects_flipped_pixel(pair, tmp_path):
    with rasterio.open(pair[1]) as d:
        arr = d.read(1)
    arr[0, 1] = 1 - arr[0, 1]
    altered = _write(tmp_path / "altered_mask.tif", arr, 255)
    assert compare_rasters(pair[1], altered, "mask")["identical"] is False


def test_validator_rejects_a_plain_geotiff(pair):
    # A striped GeoTIFF without overviews is not a COG; the build must treat that as a failure.
    assert validate_cog(pair[0])["valid"] is False


def test_mask_overviews_must_be_categorical(tmp_path):
    bad = CONFIG.read_text().replace("mask: mode", "mask: average")
    (tmp_path / "d.yaml").write_text(bad)
    with pytest.raises(DistributionError, match="categorical"):
        load_distribution_config(tmp_path / "d.yaml")


def test_build_refuses_an_archive_with_the_wrong_checksum(tmp_path):
    cfg = load_distribution_config(CONFIG)
    cfg["source"]["archive_sha256"] = "0" * 64
    with pytest.raises(DistributionError, match="SHA-256"):
        build_dates(cfg, REPO, tmp_path, ["2025-05-30"])


def test_build_refuses_a_date_outside_the_manifest(tmp_path):
    cfg = load_distribution_config(CONFIG)
    with pytest.raises(DistributionError, match="not in the frozen manifest"):
        build_dates(cfg, REPO, tmp_path, ["2025-01-01"])


def test_frozen_ndvi_and_mask_round_trip_exactly(tmp_path):
    """Real frozen v0.2 output (2025-05-30, the date with the most NaN pixels): valid COG, identical pixels."""
    cfg = load_distribution_config(CONFIG)
    report = build_dates(cfg, REPO, tmp_path, ["2025-05-30"])
    assert report["all_valid"] and report["all_identical"] and report["all_pairs_consistent"]
    ndvi = next(e for e in report["equality"] if e["kind"] == "ndvi")
    assert ndvi["detail"]["nan_count"] == 11062 > 0  # the comparison actually exercised NaN pixels
    assert ndvi["detail"]["crs"] == "EPSG:32636" and ndvi["detail"]["shape"] == [564, 489]
    # Deterministic: a second build gives byte-identical files.
    again = build_dates(cfg, REPO, tmp_path / "again", ["2025-05-30"])
    assert [f["sha256"] for f in report["files"]] == [f["sha256"] for f in again["files"]]
    shutil.rmtree(tmp_path / "again")
