"""Raster correctness: reference grid, windowed reads, scaling, masking, NDVI, GeoTIFF I/O."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import rasterio
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from rasterio.windows import transform as window_transform

from .errors import GridError, ScaleOffsetError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Grid:
    """Reference projected grid: CRS + affine transform + (rows, cols)."""

    crs: str
    transform: rasterio.Affine
    shape: tuple[int, int]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return array_bounds(*self.shape, self.transform)

    @property
    def res(self) -> tuple[float, float]:
        return abs(self.transform.a), abs(self.transform.e)

    def to_dict(self) -> dict:
        return {
            "crs": self.crs,
            "transform": list(self.transform)[:6],
            "shape": list(self.shape),
            "bounds": list(self.bounds),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Grid:
        return cls(d["crs"], rasterio.Affine(*d["transform"][:6]), tuple(int(v) for v in d["shape"]))


def grids_equal(a: Grid, b: Grid, tol: float = 1e-6) -> bool:
    """Same CRS, same transform (origin + pixel size) and same shape. Shape alone is not alignment."""
    return a.crs == b.crs and a.shape == b.shape and a.transform.almost_equals(b.transform, precision=tol)


def raster_header(href: str) -> dict:
    """Header-only read: no pixel data is fetched."""
    with rasterio.open(href) as ds:
        return {
            "crs": ds.crs.to_string(),
            "transform": list(ds.transform)[:6],
            "res": ds.res,
            "shape": (ds.height, ds.width),
            "dtype": ds.dtypes[0],
            "nodata": ds.nodata,
            "scales": ds.scales[0],
            "offsets": ds.offsets[0],
            "block_shape": ds.block_shapes[0],
            "bounds": tuple(ds.bounds),
        }


def resolve_scale_offset(role: str, stac_scale, stac_offset, raster_scale, raster_offset) -> dict:
    """Decide scale/offset once. A raster value of (1, 0) means 'not stored in the file'.

    Never falls back to an assumed offset; conflicting sources raise.
    """
    if role == "scl":
        return {"scale": None, "offset": None, "source": "categorical: no scaling"}
    raster_has = not (raster_scale == 1.0 and raster_offset == 0.0)
    if stac_scale is None or stac_offset is None:
        if not raster_has:
            raise ScaleOffsetError(f"{role}: no scale/offset in STAC or raster; refusing to assume one")
        return {"scale": raster_scale, "offset": raster_offset, "source": "raster"}
    if raster_has and (not np.isclose(raster_scale, stac_scale) or not np.isclose(raster_offset, stac_offset)):
        raise ScaleOffsetError(
            f"{role}: scale/offset conflict STAC=({stac_scale},{stac_offset}) raster=({raster_scale},{raster_offset})"
        )
    return {
        "scale": stac_scale,
        "offset": stac_offset,
        "source": "stac+raster agree" if raster_has else "stac (raster stores 1/0)",
    }


def snapped_grid(aoi_bbox_4326, ds_crs: str, ds_transform: rasterio.Affine, ds_shape) -> Grid:
    """AOI (WGS84) -> reference grid snapped outward to the dataset's pixel grid.

    Raises GridError if the AOI is not fully inside the dataset (no silent mosaic).
    """
    b = transform_bounds("EPSG:4326", ds_crs, *aoi_bbox_4326, densify_pts=21)
    w = from_bounds(*b, transform=ds_transform)
    col0, row0 = int(np.floor(w.col_off + 1e-9)), int(np.floor(w.row_off + 1e-9))
    col1, row1 = int(np.ceil(w.col_off + w.width - 1e-9)), int(np.ceil(w.row_off + w.height - 1e-9))
    rows, cols = ds_shape
    if col0 < 0 or row0 < 0 or col1 > cols or row1 > rows:
        raise GridError(
            f"AOI not fully inside tile: cols {col0}:{col1}, rows {row0}:{row1}, "
            f"tile {cols}x{rows}. Shrink the AOI or pick another tile."
        )
    win = Window(col0, row0, col1 - col0, row1 - row0)
    return Grid(ds_crs, window_transform(win, ds_transform), (row1 - row0, col1 - col0))


def read_on_grid(href: str, grid: Grid, categorical: bool) -> tuple[np.ndarray, float | None, str]:
    """Read only the grid window. Same-resolution bands: direct aligned window read (verified).

    Other resolutions: padded native window resampled onto the grid; nearest-neighbour for
    categories (never interpolate class labels), bilinear for continuous values.
    """
    with rasterio.open(href) as ds:
        if ds.crs.to_string() != grid.crs:
            raise GridError(f"CRS mismatch {ds.crs} vs {grid.crs}")
        win = from_bounds(*grid.bounds, transform=ds.transform).round_offsets().round_lengths()
        if ds.res == grid.res:
            arr = ds.read(1, window=win)
            got = Grid(grid.crs, window_transform(win, ds.transform), arr.shape)
            if not grids_equal(got, grid):
                raise GridError(f"Grid misalignment: {got} vs {grid}")
            return arr, ds.nodata, "direct window read"
        pad = Window(max(win.col_off - 1, 0), max(win.row_off - 1, 0), win.width + 2, win.height + 2)
        src = ds.read(1, window=pad, boundless=True, fill_value=ds.nodata or 0)
        fill = ds.nodata if ds.nodata is not None else 0
        dst = np.full(grid.shape, fill, dtype=src.dtype)
        method = Resampling.nearest if categorical else Resampling.bilinear
        reproject(
            src,
            dst,
            src_transform=window_transform(pad, ds.transform),
            src_crs=ds.crs,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            src_nodata=ds.nodata,
            dst_nodata=ds.nodata,
            resampling=method,
        )
        return dst, ds.nodata, f"native {ds.res} -> grid via {method.name}"


def dn_to_reflectance(dn: np.ndarray, nodata, scale: float, offset: float) -> np.ndarray:
    """reflectance = DN * scale + offset, applied once, in float32; source nodata -> NaN first."""
    refl = dn.astype(np.float32) * np.float32(scale) + np.float32(offset)
    if nodata is not None:
        refl[dn == nodata] = np.nan
    return refl


def compute_ndvi(
    red: np.ndarray, nir: np.ndarray, scl: np.ndarray, usable_classes, den_min: float, require_positive: bool = True
) -> dict:
    """NDVI plus the masks that explain every invalid pixel. Nothing is clipped."""
    den = nir + red
    den_ok = np.isfinite(den) & (den > den_min)
    with np.errstate(invalid="ignore", divide="ignore"):
        ndvi = np.where(den_ok, (nir - red) / den, np.nan).astype(np.float32)
    finite = np.isfinite(red) & np.isfinite(nir)
    refl_pos = (red > 0) & (nir > 0)
    valid = np.isin(scl, list(usable_classes)) & finite & den_ok & np.isfinite(ndvi)
    if require_positive:
        valid &= refl_pos
    out_of_range = np.isfinite(ndvi) & ((ndvi < -1) | (ndvi > 1))
    return {
        "ndvi": ndvi,
        "valid": valid,
        "den_ok": den_ok,
        "refl_pos": refl_pos,
        "finite": finite,
        "out_of_range": out_of_range,
    }


def dilate(mask: np.ndarray, radius_px: int) -> np.ndarray:
    """Square binary dilation in pure NumPy (keeps the dependency stack small)."""
    if radius_px <= 0:
        return mask.copy()
    out = mask.copy()
    rows, cols = mask.shape
    for dy in range(-radius_px, radius_px + 1):
        for dx in range(-radius_px, radius_px + 1):
            src_r = slice(max(dy, 0), rows + min(dy, 0))
            dst_r = slice(max(-dy, 0), rows + min(-dy, 0))
            src_c = slice(max(dx, 0), cols + min(dx, 0))
            dst_c = slice(max(-dx, 0), cols + min(-dx, 0))
            out[dst_r, dst_c] |= mask[src_r, src_c]
    return out


def write_geotiff(path, arr: np.ndarray, grid: Grid, nodata, tags: dict) -> None:
    profile = {
        "driver": "GTiff",
        "height": arr.shape[0],
        "width": arr.shape[1],
        "count": 1,
        "dtype": arr.dtype.name,
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": nodata,
        "compress": "deflate",
        "predictor": 3 if arr.dtype.kind == "f" else 2,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)
        dst.update_tags(**{k: str(v) for k, v in tags.items()})


def verify_geotiff(path, arr: np.ndarray, grid: Grid, nodata) -> dict:
    """Reopen a saved file and compare georeferencing, nodata and values with what we meant to write."""
    with rasterio.open(path) as ds:
        back = ds.read(1)
        if arr.dtype.kind == "f":
            values_ok = np.array_equal(back, arr, equal_nan=True)
        else:
            values_ok = np.array_equal(back, arr)
        if nodata is not None and isinstance(nodata, float) and np.isnan(nodata):
            nodata_ok = ds.nodata is not None and np.isnan(ds.nodata)
        else:
            nodata_ok = ds.nodata == nodata
        got = Grid(ds.crs.to_string(), ds.transform, (ds.height, ds.width))
        return {
            "grid_ok": grids_equal(got, grid),
            "nodata_ok": bool(nodata_ok),
            "values_ok": bool(values_ok),
            "dtype": ds.dtypes[0],
            "tags": ds.tags(),
        }
