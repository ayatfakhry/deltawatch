import numpy as np
import pytest

from deltawatch.errors import ScaleOffsetError
from deltawatch.raster import compute_ndvi, dn_to_reflectance, resolve_scale_offset


def test_known_reflectance_scaling_values():
    dn = np.array([1000, 2000, 11000, 1], dtype=np.uint16)
    refl = dn_to_reflectance(dn, nodata=0, scale=0.0001, offset=-0.1)
    np.testing.assert_allclose(refl, [0.0, 0.1, 1.0, -0.0999], atol=1e-6)
    assert refl.dtype == np.float32


def test_nodata_becomes_nan_before_use():
    dn = np.array([0, 1500, 0], dtype=np.uint16)
    refl = dn_to_reflectance(dn, nodata=0, scale=0.0001, offset=-0.1)
    assert np.isnan(refl[0]) and np.isnan(refl[2])
    assert refl[1] == pytest.approx(0.05, abs=1e-6)


def test_scaling_applied_exactly_once():
    dn = np.full(3, 3000, np.uint16)
    once = dn_to_reflectance(dn, 0, 0.0001, -0.1)
    assert np.allclose(once, 0.2, atol=1e-6)  # applying twice would give ~ -0.09998


@pytest.mark.parametrize(
    "raster,expected_source", [((0.0001, -0.1), "stac+raster agree"), ((1.0, 0.0), "stac (raster stores 1/0)")]
)
def test_scale_offset_resolution(raster, expected_source):
    r = resolve_scale_offset("red", 0.0001, -0.1, *raster)
    assert (r["scale"], r["offset"], r["source"]) == (0.0001, -0.1, expected_source)


def test_scale_offset_conflict_fails_clearly():
    with pytest.raises(ScaleOffsetError, match="conflict"):
        resolve_scale_offset("nir", 0.0001, -0.1, 0.0001, 0.0)


def test_no_scale_anywhere_refuses_to_assume():
    with pytest.raises(ScaleOffsetError, match="refusing"):
        resolve_scale_offset("red", None, None, 1.0, 0.0)


def test_scl_is_never_scaled():
    assert resolve_scale_offset("scl", 0.0001, -0.1, 1.0, 0.0)["scale"] is None


def _scl(shape, cls=4):
    return np.full(shape, cls, np.uint8)


def test_constant_synthetic_ndvi():
    red = np.full((3, 3), 0.1, np.float32)
    nir = np.full((3, 3), 0.5, np.float32)
    r = compute_ndvi(red, nir, _scl(red.shape), [4, 5, 6], 1e-4)
    np.testing.assert_allclose(r["ndvi"], (0.5 - 0.1) / 0.6, rtol=1e-6)
    assert r["valid"].all()


@pytest.mark.parametrize("red,nir", [(0.0, 0.0), (0.00004, 0.00004), (-0.05, 0.02), (-0.03, -0.02)])
def test_denominator_edge_cases_are_invalid(red, nir):
    r = compute_ndvi(np.array([red], np.float32), np.array([nir], np.float32), _scl(1), [4], 1e-4)
    assert not r["valid"][0]
    assert np.isnan(r["ndvi"][0]) or not r["refl_pos"][0]


def test_negative_reflectance_is_counted_not_clipped():
    # nir slightly negative: denominator positive but NDVI explodes. Must be flagged, not clipped.
    red = np.array([0.0066], np.float32)
    nir = np.array([-0.0042], np.float32)
    r = compute_ndvi(red, nir, _scl(1), [4], 1e-4)
    assert r["out_of_range"][0] and not r["refl_pos"][0] and not r["valid"][0]
    assert r["ndvi"][0] < -1  # value preserved for reporting


def test_positive_reflectances_never_leave_unit_range():
    rng = np.random.default_rng(1)
    red = rng.uniform(1e-4, 1.5, 1000).astype(np.float32)
    nir = rng.uniform(1e-4, 1.5, 1000).astype(np.float32)
    r = compute_ndvi(red, nir, _scl(1000), [4], 1e-4)
    assert not (r["out_of_range"] & r["refl_pos"]).any()


def test_scl_policy_and_nan_inputs():
    red = np.array([0.1, 0.1, np.nan, 0.1], np.float32)
    nir = np.array([0.4, 0.4, 0.4, 0.4], np.float32)
    scl = np.array([4, 9, 4, 0], np.uint8)
    r = compute_ndvi(red, nir, scl, [4, 5, 6], 1e-4)
    assert r["valid"].tolist() == [True, False, False, False]


def test_missing_data_is_never_zero_vegetation():
    red = np.array([np.nan], np.float32)
    nir = np.array([0.3], np.float32)
    r = compute_ndvi(red, nir, _scl(1), [4], 1e-4)
    assert np.isnan(r["ndvi"][0]) and not r["valid"][0]
