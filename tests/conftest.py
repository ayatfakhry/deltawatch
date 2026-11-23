"""Shared synthetic fixtures. No network is used by any test outside tests/live/."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from deltawatch.manifest import save_manifest
from deltawatch.raster import Grid

CRS = "EPSG:32636"


@pytest.fixture
def grid10() -> Grid:
    # 6 x 8 pixels of 10 m, origin on the 10 m tile grid (and on the 20 m grid).
    return Grid(CRS, from_origin(300000.0, 3426000.0, 10.0, 10.0), (6, 8))


@pytest.fixture
def scl20_path(tmp_path):
    """A 20 m categorical raster covering grid10 with a 1-pixel margin, distinct class per pixel."""
    arr = np.array(
        [[0, 1, 2, 3, 4, 5], [6, 7, 8, 9, 10, 11], [4, 5, 6, 4, 5, 6], [3, 8, 9, 10, 4, 4], [5, 5, 5, 5, 5, 5]],
        dtype=np.uint8,
    )
    path = tmp_path / "scl20.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype="uint8",
        crs=CRS,
        transform=from_origin(299980.0, 3426020.0, 20.0, 20.0),
        nodata=0,
    ) as dst:
        dst.write(arr, 1)
    return path, arr


@pytest.fixture
def dn10_path(tmp_path):
    """A 10 m uint16 DN raster larger than grid10, with stored scale/offset like Collection 1."""
    rng = np.random.default_rng(0)
    arr = rng.integers(1000, 5000, size=(20, 20), dtype=np.uint16)
    arr[5, 5] = 0  # nodata
    path = tmp_path / "dn10.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="uint16",
        crs=CRS,
        transform=from_origin(299950.0, 3426050.0, 10.0, 10.0),
        nodata=0,
    ) as dst:
        dst.write(arr, 1)
        dst.scales = (0.0001,)
        dst.offsets = (-0.1,)
    return path, arr


# ---------------------------------------------------------------- synthetic two-date scene


def _write_scene_raster(path, arr, res, origin, nodata, scale=None):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype=arr.dtype.name,
        crs=CRS,
        transform=from_origin(*origin, res, res),
        nodata=nodata,
    ) as d:
        d.write(arr, 1)
        if scale:
            d.scales, d.offsets = (scale[0],), (scale[1],)


@pytest.fixture
def scene(tmp_path):
    """Two dates; date 2 has a cloud patch, a nodata pixel and one negative-NIR pixel."""
    items = []
    for k, date in enumerate(["2025-04-05", "2025-04-17"]):
        red = np.full((12, 12), 1800, np.uint16)  # 0.08 reflectance
        nir = np.full((12, 12), 5100, np.uint16)  # 0.41 reflectance
        scl = np.full((6, 6), 4, np.uint8)
        if k == 1:
            scl[0:2, 0:2] = 9  # cloud -> 4x4 invalid at 10 m
            red[10, 10] = 0  # nodata
            red[11, 11], nir[11, 11] = 1066, 958  # negative NIR -> out-of-range NDVI
        paths = {}
        for role, arr, res, nod, sc in [
            ("red", red, 10, 0, (0.0001, -0.1)),
            ("nir", nir, 10, 0, (0.0001, -0.1)),
            ("scl", scl, 20, 0, None),
        ]:
            p = tmp_path / f"{role}_{date}.tif"
            _write_scene_raster(p, arr, res, (300000.0, 3426000.0), nod, sc)
            paths[role] = p
        items.append(
            {
                "id": f"S2X_T36RTV_{date}",
                "date": date,
                "datetime": f"{date}T08:41:00Z",
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
        "grid": {"crs": CRS, "transform": [10.0, 0.0, 300000.0, 0.0, -10.0, 3426000.0], "shape": [12, 12]},
        "quality_policy": {"scl_usable_classes": [4, 5, 6]},
        "items": items,
    }
    save_manifest(m, tmp_path / "manifest.json")
    return tmp_path
