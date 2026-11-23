"""Static STAC catalog: schema validity, honest metadata, link integrity, asset integrity. No network."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import numpy as np
import pystac
import pytest
import rasterio

from deltawatch.distribution.cog import build_dates, load_distribution_config
from deltawatch.distribution.stac import (
    COLLECTION_ID,
    build_catalog,
    validate_catalog,
    verify_assets,
    walk_links,
)

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "distribution.yaml"
DATES = [
    "2025-03-25",
    "2025-04-05",
    "2025-04-17",
    "2025-05-02",
    "2025-05-17",
    "2025-05-30",
    "2025-06-13",
    "2025-06-23",
]


@pytest.fixture(scope="module")
def catalog_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("catalog")
    cfg = load_distribution_config(CONFIG)
    build_dates(cfg, REPO, out / COLLECTION_ID)
    build_catalog(cfg, REPO, out)
    return out


@pytest.fixture
def copy_of(catalog_dir, tmp_path):
    dst = tmp_path / "copy"
    shutil.copytree(catalog_dir, dst)
    return dst


def _item(cat: Path, date: str) -> dict:
    return json.loads((cat / COLLECTION_ID / date / f"{COLLECTION_ID}-{date}.json").read_text())


def test_layout_is_one_folder_per_date(catalog_dir):
    assert (catalog_dir / "catalog.json").exists()
    assert (catalog_dir / COLLECTION_ID / "collection.json").exists()
    for d in DATES:
        files = sorted(p.name for p in (catalog_dir / COLLECTION_ID / d).iterdir())
        assert files == [f"{COLLECTION_ID}-{d}.json", f"ndvi_{d}.tif", f"thumbnail_{d}.png", f"valid_{d}.tif"]


def test_catalog_validates_offline(catalog_dir, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("validation tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    r = validate_catalog(catalog_dir)
    assert r["documents_validated"] == 10 and r["items"] == 8
    assert r["broken_links"] == [] and r["links_checked"] > 0


def test_invalid_item_fails_validation(copy_of):
    path = copy_of / COLLECTION_ID / DATES[0] / f"{COLLECTION_ID}-{DATES[0]}.json"
    item = json.loads(path.read_text())
    item["properties"]["proj:shape"] = "564x489"  # wrong type for the projection extension
    path.write_text(json.dumps(item))
    with pytest.raises(pystac.STACValidationError):
        validate_catalog(copy_of)


def test_link_walk_reports_a_missing_asset_and_item(copy_of):
    (copy_of / COLLECTION_ID / DATES[1] / f"valid_{DATES[1]}.tif").unlink()
    (copy_of / COLLECTION_ID / DATES[2] / f"{COLLECTION_ID}-{DATES[2]}.json").unlink()
    broken = walk_links(copy_of / "catalog.json")["broken_links"]
    hrefs = {b["href"] for b in broken}
    assert f"./valid_{DATES[1]}.tif" in hrefs
    assert f"./{DATES[2]}/{COLLECTION_ID}-{DATES[2]}.json" in hrefs


def test_all_hrefs_are_relative(catalog_dir):
    for p in catalog_dir.rglob("*.json"):
        doc = json.loads(p.read_text())
        hrefs = [lk["href"] for lk in doc["links"]] + [a["href"] for a in doc.get("assets", {}).values()]
        for h in hrefs:
            assert h.startswith(("./", "../", "https://")), (p.name, h)
        assert not any(lk["rel"] == "self" for lk in doc["links"])  # self-contained catalog


@pytest.mark.parametrize("date", DATES)
def test_item_metadata_is_read_from_the_files(catalog_dir, date):
    item = _item(catalog_dir, date)
    folder = catalog_dir / COLLECTION_ID / date
    with rasterio.open(folder / f"ndvi_{date}.tif") as n, rasterio.open(folder / f"valid_{date}.tif") as m:
        p = item["properties"]
        assert p["datetime"].startswith(n.tags()["acquired"][:19])
        assert p["deltawatch:source_item"] == n.tags()["source_item"]
        assert p["proj:code"] == n.crs.to_string() == "EPSG:32636"
        assert p["proj:shape"] == [n.height, n.width]
        assert p["proj:transform"] == list(n.transform)[:6]
        nb = item["assets"]["ndvi"]["raster:bands"][0]
        assert nb["nodata"] == "nan" and np.isnan(n.nodata)
        assert nb["data_type"] == n.dtypes[0] == "float32"
        assert "unit" not in nb  # the files carry no band unit, so none is claimed
        a = n.read(1)
        assert nb["statistics"]["valid_percent"] == round(100 * np.isfinite(a).sum() / a.size, 4)
        assert nb["statistics"]["minimum"] == pytest.approx(float(np.nanmin(a)))
        mb = item["assets"]["valid"]["raster:bands"][0]
        assert mb["nodata"] == m.nodata == 255 and mb["data_type"] == "uint8"
        classes = {c["value"] for c in item["assets"]["valid"]["classification:classes"]}
        assert classes == {0, 1, 255}
    # bbox and geometry agree, and the footprint contains the AOI centre from the manifest
    xs = [c[0] for c in item["geometry"]["coordinates"][0]]
    ys = [c[1] for c in item["geometry"]["coordinates"][0]]
    assert item["bbox"] == [min(xs), min(ys), max(xs), max(ys)]
    assert item["bbox"][0] < 30.925 < item["bbox"][2] and item["bbox"][1] < 30.925 < item["bbox"][3]


def test_unknown_fields_are_not_invented(catalog_dir):
    item = _item(catalog_dir, DATES[0])
    for absent in ("gsd", "instruments", "constellation", "view:off_nadir", "sat:orbit_state", "created"):
        assert absent not in item["properties"]


def test_collection_extent_and_item_assets(catalog_dir):
    col = json.loads((catalog_dir / COLLECTION_ID / "collection.json").read_text())
    assert col["extent"]["temporal"]["interval"][0][0].startswith("2025-03-25")
    assert col["extent"]["temporal"]["interval"][0][1].startswith("2025-06-23")
    assert set(col["item_assets"]) == {"ndvi", "valid", "thumbnail"}
    assert col["license"] == "other" and any(lk["rel"] == "license" for lk in col["links"])


def test_asset_checksums_match_and_tampering_is_detected(catalog_dir, copy_of):
    assert verify_assets(catalog_dir) == {"assets_checked": 24, "mismatches": [], "ok": True}
    f = copy_of / COLLECTION_ID / DATES[3] / f"thumbnail_{DATES[3]}.png"
    f.write_bytes(f.read_bytes()[:-1] + b"\x00")
    r = verify_assets(copy_of)
    assert not r["ok"] and r["mismatches"] == [
        {"item": f"{COLLECTION_ID}-{DATES[3]}", "asset": "thumbnail", "why": "checksum"}
    ]


def test_catalog_build_is_deterministic(catalog_dir, tmp_path):
    cfg = load_distribution_config(CONFIG)
    build_dates(cfg, REPO, tmp_path / COLLECTION_ID)
    build_catalog(cfg, REPO, tmp_path)
    for p in catalog_dir.rglob("*"):
        if p.is_file():
            assert (tmp_path / p.relative_to(catalog_dir)).read_bytes() == p.read_bytes(), p.name
