"""Load and validate the YAML config; build the anonymous GDAL HTTP environment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from .errors import ConfigError

REQUIRED_KEYS = [
    ("stac", "api_url"),
    ("stac", "collection"),
    ("stac", "max_items"),
    ("stac", "discovery_max_scene_cloud_pct"),
    ("aoi", "bbox"),
    ("time", "start"),
    ("time", "end"),
    ("tile", "preferred_mgrs"),
    ("assets", "red"),
    ("assets", "nir"),
    ("assets", "scl"),
    ("quality", "scl_usable_classes"),
    ("quality", "ndvi_denominator_min"),
    ("quality", "require_positive_reflectance"),
    ("quality", "dilation_classes"),
    ("quality", "dilation_radius_px"),
    ("selection", "min_usable_fraction"),
    ("selection", "target_dates"),
    ("selection", "min_dates"),
    ("selection", "min_gap_days"),
    ("selection", "window_start"),
    ("selection", "window_end"),
    ("network", "gdal_http_timeout_s"),
    ("network", "gdal_http_max_retry"),
    ("network", "gdal_http_retry_delay_s"),
    ("network", "stac_max_retries"),
    ("outputs", "dir"),
]


def validate_config(cfg: dict) -> dict:
    """Fail early, naming every missing key, instead of a KeyError deep inside a stage."""
    missing = []
    for section, key in REQUIRED_KEYS:
        if not isinstance(cfg.get(section), dict) or key not in cfg[section]:
            missing.append(f"{section}.{key}")
    if missing:
        raise ConfigError(f"Config is missing keys: {', '.join(missing)}")
    if cfg["stac"]["collection"] != "sentinel-2-c1-l2a":
        raise ConfigError("stac.collection must be sentinel-2-c1-l2a (no silent substitution)")
    w, s, e, n = cfg["aoi"]["bbox"]
    if not (w < e and s < n):
        raise ConfigError(f"aoi.bbox must be [west, south, east, north], got {cfg['aoi']['bbox']}")
    return cfg


def load_config(path: str | Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    return validate_config(cfg)


def processing_fingerprint(cfg: dict) -> str:
    """Hash of every config value that changes per-date outputs (used to accept cached outputs)."""
    relevant = {"quality": cfg["quality"], "assets": cfg["assets"]}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def gdal_env(cfg: dict) -> dict:
    """Anonymous, bounded HTTP access for GDAL. No AWS credentials, no requester-pays."""
    net = cfg["network"]
    return dict(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_TIMEOUT=str(net["gdal_http_timeout_s"]),
        GDAL_HTTP_MAX_RETRY=str(net["gdal_http_max_retry"]),
        GDAL_HTTP_RETRY_DELAY=str(net["gdal_http_retry_delay_s"]),
        GDAL_HTTP_MULTIPLEX="NO",
        VSI_CACHE="TRUE",
    )
