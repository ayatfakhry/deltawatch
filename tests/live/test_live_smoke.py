"""Optional live access smoke test. Skipped unless DELTAWATCH_LIVE=1 (needs network)."""

import os

import pytest
import rasterio

from deltawatch.config import gdal_env, load_config
from deltawatch.manifest import load_manifest
from deltawatch.raster import Grid, read_on_grid

pytestmark = pytest.mark.skipif(os.environ.get("DELTAWATCH_LIVE") != "1", reason="set DELTAWATCH_LIVE=1")


def test_first_manifest_item_reads_anonymously():
    cfg = load_config("config/default.yaml")
    m = load_manifest("examples/manifest.json")
    grid = Grid.from_dict(m["grid"])
    with rasterio.Env(**gdal_env(cfg)):
        arr, nodata, _ = read_on_grid(m["items"][0]["assets"]["red"]["href"], grid, categorical=False)
    assert arr.shape == grid.shape and nodata == 0
