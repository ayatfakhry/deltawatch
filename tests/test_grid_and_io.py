import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from deltawatch.errors import GridError
from deltawatch.raster import Grid, dilate, grids_equal, read_on_grid, snapped_grid, verify_geotiff, write_geotiff


def test_grid_equality_requires_transform_not_just_shape(grid10):
    shifted = Grid(grid10.crs, from_origin(300010.0, 3426000.0, 10.0, 10.0), grid10.shape)
    assert grid10.shape == shifted.shape
    assert not grids_equal(grid10, shifted)
    assert grids_equal(grid10, Grid.from_dict(grid10.to_dict()))
    assert not grids_equal(grid10, Grid("EPSG:32635", grid10.transform, grid10.shape))


def test_snapped_grid_is_on_tile_pixel_grid():
    tile_tf = from_origin(199980.0, 3500040.0, 10.0, 10.0)
    g = snapped_grid([30.90, 30.90, 30.95, 30.95], "EPSG:32636", tile_tf, (10980, 10980))
    assert (g.transform.c - tile_tf.c) % 10 == 0 and (tile_tf.f - g.transform.f) % 10 == 0
    assert g.shape == (564, 489)  # matches the live Colab and local runs


def test_aoi_outside_tile_raises_instead_of_mosaicking():
    tile_tf = from_origin(199980.0, 3500040.0, 10.0, 10.0)
    with pytest.raises(GridError, match="not fully inside"):
        snapped_grid([31.5, 30.90, 31.55, 30.95], "EPSG:32636", tile_tf, (10980, 10980))


def test_same_resolution_read_is_aligned(dn10_path, grid10):
    path, full = dn10_path
    arr, nod, note = read_on_grid(str(path), grid10, categorical=False)
    assert note == "direct window read" and arr.shape == grid10.shape and nod == 0
    np.testing.assert_array_equal(arr, full[5:11, 5:13])  # origin offset 50 m = 5 px


def test_scl_nearest_resampling_preserves_categories(scl20_path, grid10):
    path, src = scl20_path
    arr, nod, note = read_on_grid(str(path), grid10, categorical=True)
    assert "nearest" in note and arr.shape == grid10.shape
    assert set(np.unique(arr)) <= set(np.unique(src))  # no invented classes
    # Each 10 m pixel equals its parent 20 m pixel (grid10 starts 1 px inside the 20 m raster).
    for r in range(grid10.shape[0]):
        for c in range(grid10.shape[1]):
            assert arr[r, c] == src[1 + r // 2, 1 + c // 2]
    # 2x2 blocks are constant: upsampling does not create 10 m mask detail.
    assert (arr[0::2, 0::2] == arr[1::2, 1::2]).all()


def test_crs_mismatch_raises(dn10_path, grid10):
    with pytest.raises(GridError, match="CRS"):
        read_on_grid(str(dn10_path[0]), Grid("EPSG:32635", grid10.transform, grid10.shape), False)


@pytest.mark.parametrize("dtype,nodata", [(np.float32, float("nan")), (np.uint8, 255)])
def test_geotiff_round_trip(tmp_path, grid10, dtype, nodata):
    arr = (np.arange(48).reshape(6, 8) % 2).astype(dtype)
    if dtype == np.float32:
        arr[0, 0] = np.nan
    p = tmp_path / "x.tif"
    write_geotiff(p, arr, grid10, nodata, {"source_item": "abc"})
    v = verify_geotiff(p, arr, grid10, nodata)
    assert v["grid_ok"] and v["nodata_ok"] and v["values_ok"] and v["tags"]["source_item"] == "abc"
    with rasterio.open(p) as ds:
        assert ds.crs.to_string() == grid10.crs and ds.transform == grid10.transform


def test_round_trip_detects_wrong_grid(tmp_path, grid10):
    arr = np.zeros(grid10.shape, np.uint8)
    p = tmp_path / "y.tif"
    write_geotiff(p, arr, grid10, 255, {})
    other = Grid(grid10.crs, from_origin(300010.0, 3426000.0, 10.0, 10.0), grid10.shape)
    assert not verify_geotiff(p, arr, other, 255)["grid_ok"]


def test_dilation():
    m = np.zeros((7, 7), bool)
    m[3, 3] = True
    assert dilate(m, 0).sum() == 1
    assert dilate(m, 1).sum() == 9 and dilate(m, 2).sum() == 25
    edge = np.zeros((4, 4), bool)
    edge[0, 0] = True
    assert dilate(edge, 1).sum() == 4
