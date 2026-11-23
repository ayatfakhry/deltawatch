"""Convert frozen NDVI and mask GeoTIFFs to Cloud Optimized GeoTIFFs and prove nothing changed.

Three steps, each usable on its own:
- `convert_to_cog`: rio-cogeo translation with the settings from config/distribution.yaml.
- `validate_cog`: rio-cogeo's validator. Any error is a failure, never a warning.
- `compare_rasters`: decodes source and COG at full resolution and checks pixel values
  (NaN equals NaN), mask semantics, CRS, transform, shape, dtype, nodata and provenance tags.
"""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import yaml

from ..errors import DeltaWatchError
from ..manifest import load_manifest

KINDS = ("ndvi", "mask")


class DistributionError(DeltaWatchError):
    """Raised when the distribution build cannot be trusted (bad input, invalid COG, mismatch)."""


@dataclass(frozen=True)
class CogSettings:
    blocksize: int
    compress: str
    predictor: dict
    overview_levels: int
    overview_resampling: dict

    def profile(self, kind: str) -> dict:
        return {
            "driver": "GTiff",
            "interleave": "pixel",
            "tiled": True,
            "blockxsize": self.blocksize,
            "blockysize": self.blocksize,
            "compress": self.compress,
            "predictor": int(self.predictor[kind]),
        }


def load_distribution_config(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    for key in ("source", "cog"):
        if key not in cfg:
            raise DistributionError(f"{path}: missing section '{key}'")
    cog = cfg["cog"]
    for kind in KINDS:
        if kind not in cog["predictor"] or kind not in cog["overview_resampling"]:
            raise DistributionError(f"{path}: cog settings missing for '{kind}'")
    if cog["overview_resampling"]["mask"] not in ("nearest", "mode"):
        raise DistributionError("mask overviews must use 'nearest' or 'mode' (categorical data)")
    return cfg


def cog_settings(cfg: dict) -> CogSettings:
    c = cfg["cog"]
    return CogSettings(
        blocksize=int(c["blocksize"]),
        compress=str(c["compress"]),
        predictor=dict(c["predictor"]),
        overview_levels=int(c["overview_levels"]),
        overview_resampling=dict(c["overview_resampling"]),
    )


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def frozen_sources(cfg: dict, repo_root: str | Path) -> dict[str, dict[str, str]]:
    """Return {date: {"ndvi": vsizip path, "mask": vsizip path}} after checking archive and manifest."""
    root = Path(repo_root)
    src = cfg["source"]
    archive = (root / src["archive"]).resolve()
    digest = sha256_file(archive)
    if digest != src["archive_sha256"]:
        raise DistributionError(f"{archive.name}: SHA-256 {digest[:12]} != frozen {src['archive_sha256'][:12]}")
    manifest = load_manifest(root / src["manifest"])  # refuses an edited manifest
    dates = [it["date"] for it in manifest["items"]]
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
    out = {}
    for d in dates:
        members = {"ndvi": src["ndvi_member"].format(date=d), "mask": src["mask_member"].format(date=d)}
        for m in members.values():
            if m not in names:
                raise DistributionError(f"{archive.name}: missing {m}")
        out[d] = {k: f"/vsizip/{archive}/{m}" for k, m in members.items()}
    return out


def convert_to_cog(src: str | Path, dst: str | Path, kind: str, settings: CogSettings) -> Path:
    from rio_cogeo.cogeo import cog_translate

    if kind not in KINDS:
        raise DistributionError(f"unknown raster kind '{kind}'")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src) as s:
        if s.count != 1:
            raise DistributionError(f"{src}: expected 1 band, found {s.count}")
        cog_translate(
            s,
            str(dst),
            settings.profile(kind),
            nodata=s.nodata,
            overview_level=settings.overview_levels,
            overview_resampling=settings.overview_resampling[kind],
            in_memory=True,
            quiet=True,
        )
    return dst


def validate_cog(path: str | Path) -> dict:
    from rio_cogeo.cogeo import cog_validate

    valid, errors, warnings = cog_validate(str(path), strict=True, quiet=True)  # strict: warnings fail too
    return {"file": Path(path).name, "valid": bool(valid) and not errors, "errors": errors, "warnings": warnings}


def _nodata_equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return a == b


def compare_rasters(src: str | Path, cog: str | Path, kind: str) -> dict:
    """Full-resolution, nodata-aware comparison. Every check must be True for `identical`."""
    with rasterio.open(src) as a, rasterio.open(cog) as b:
        x, y = a.read(1), b.read(1)
        checks = {
            "crs_equal": a.crs == b.crs,
            "transform_equal": a.transform == b.transform,
            "shape_equal": (a.height, a.width, a.count) == (b.height, b.width, b.count),
            "dtype_equal": a.dtypes == b.dtypes,
            "nodata_equal": _nodata_equal(a.nodata, b.nodata),
            # GDAL's per-pixel validity mask (255 valid, 0 nodata) must match exactly.
            "gdal_mask_equal": bool(np.array_equal(a.read_masks(1), b.read_masks(1))),
            "scale_offset_equal": (a.scales, a.offsets) == (b.scales, b.offsets),
            # Every source tag (provenance, units, attribution) must survive unchanged.
            "source_tags_preserved": all(b.tags().get(k) == v for k, v in a.tags().items()),
        }
        if kind == "ndvi":
            checks["values_equal_nan_aware"] = bool(np.array_equal(x, y, equal_nan=True))
            checks["nan_positions_equal"] = bool(np.array_equal(np.isnan(x), np.isnan(y)))
            # Bit-level equality catches anything array_equal could hide (e.g. -0.0 vs 0.0).
            checks["bitwise_equal"] = bool(np.array_equal(x.view(np.uint32), y.view(np.uint32)))
        else:
            checks["values_equal"] = bool(np.array_equal(x, y))
            checks["mask_values_in_0_1"] = bool(set(np.unique(y).tolist()) <= {0, 1})
            checks["valid_count_equal"] = int((x == 1).sum()) == int((y == 1).sum())
        detail = {
            "shape": [a.height, a.width],
            "dtype": a.dtypes[0],
            "nodata": "NaN" if a.nodata is not None and math.isnan(a.nodata) else a.nodata,
            "crs": a.crs.to_string(),
            "nodata_pixel_count": int((a.read_masks(1) == 0).sum()),
            "cog_overviews": b.overviews(1),
            "cog_block_shapes": [list(s) for s in b.block_shapes],
            "cog_compression": b.profile.get("compress"),
            "cog_overview_resampling_tag": b.tags().get("OVERVIEW_RESAMPLING"),
        }
        if kind == "ndvi":
            detail["nan_count"] = int(np.isnan(x).sum())
        else:
            detail["valid_pixel_count"] = int((x == 1).sum())
    return {
        "kind": kind,
        "source": str(src).split("/")[-1],
        "cog": Path(cog).name,
        "checks": {k: bool(v) for k, v in checks.items()},
        "identical": all(checks.values()),
        "detail": detail,
    }


def check_pair_semantics(ndvi_cog: str | Path, mask_cog: str | Path) -> dict:
    """DeltaWatch's data contract: mask == 1 exactly where NDVI is finite, mask == 0 where NDVI is NaN."""
    with rasterio.open(ndvi_cog) as n, rasterio.open(mask_cog) as m:
        x, v = n.read(1), m.read(1)
        same_grid = (n.crs, n.transform, n.shape) == (m.crs, m.transform, m.shape)
    ok = same_grid and bool(np.array_equal(np.isfinite(x), v == 1)) and bool(np.array_equal(np.isnan(x), v == 0))
    return {"same_grid": bool(same_grid), "mask_matches_ndvi_nan": ok}


def tool_versions() -> dict:
    import rio_cogeo

    return {"rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__, "rio_cogeo": rio_cogeo.__version__}


def build_dates(cfg: dict, repo_root: str | Path, out_dir: str | Path, dates: list[str] | None = None) -> dict:
    """Convert, validate and compare the selected dates. Raises if any file fails; returns the report."""
    sources = frozen_sources(cfg, repo_root)
    selected = dates or sorted(sources)
    unknown = sorted(set(selected) - set(sources))
    if unknown:
        raise DistributionError(f"dates not in the frozen manifest: {unknown}")
    settings = cog_settings(cfg)
    out_dir = Path(out_dir)
    files, validation, equality, pairs = [], [], [], []
    for d in selected:
        written = {}
        for kind in KINDS:
            name = f"{'ndvi' if kind == 'ndvi' else 'valid'}_{d}.tif"
            dst = written[kind] = convert_to_cog(sources[d][kind], out_dir / d / name, kind, settings)
            validation.append({"date": d, "kind": kind, **validate_cog(dst)})
            equality.append({"date": d, **compare_rasters(sources[d][kind], dst, kind)})
            files.append(
                {
                    "date": d,
                    "kind": kind,
                    "path": f"{d}/{name}",
                    "sha256": sha256_file(dst),
                    "size_bytes": dst.stat().st_size,
                }
            )
        pairs.append({"date": d, **check_pair_semantics(written["ndvi"], written["mask"])})
    report = {
        "source_archive": cfg["source"]["archive"],
        "source_archive_sha256": cfg["source"]["archive_sha256"],
        "settings": cfg["cog"],
        "tool_versions": tool_versions(),
        "files": files,
        "validation": validation,
        "equality": equality,
        "pair_semantics": pairs,
        "all_valid": all(v["valid"] for v in validation),
        "all_identical": all(e["identical"] for e in equality),
        "all_pairs_consistent": all(p["same_grid"] and p["mask_matches_ndvi_nan"] for p in pairs),
    }
    if not report["all_valid"]:
        bad = [v["file"] for v in validation if not v["valid"]]
        raise DistributionError(f"COG validation failed: {bad}\n{json.dumps(report['validation'], indent=1)}")
    if not report["all_identical"]:
        bad = [e["cog"] for e in equality if not e["identical"]]
        raise DistributionError(f"COG differs from source: {bad}")
    if not report["all_pairs_consistent"]:
        raise DistributionError(f"NDVI/mask pair breaks the data contract: {pairs}")
    return report
