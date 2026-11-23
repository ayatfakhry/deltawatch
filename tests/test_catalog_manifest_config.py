import json

import pandas as pd
import pystac
import pytest
import yaml

from deltawatch.catalog import (
    dedup_per_date,
    describe_asset,
    relative_orbit,
    require_min_dates,
    select_dates,
    tile_items,
)
from deltawatch.config import load_config, processing_fingerprint, validate_config
from deltawatch.errors import AssetError, ConfigError, ManifestError, SelectionError
from deltawatch.manifest import fingerprint, load_manifest, save_manifest

HREF = "https://example.invalid/B04.tif"


def make_item(item_id, tile="36RTV", dt="2025-04-17T08:41:44Z", assets=None, requester_pays=False):
    it = pystac.Item(
        item_id,
        geometry=None,
        bbox=None,
        datetime=pd.Timestamp(dt).to_pydatetime(),
        properties={"grid:code": f"MGRS-{tile}", "storage:requester_pays": requester_pays},
    )
    it.collection_id = "sentinel-2-c1-l2a"
    for k, href in (assets or {"red": HREF, "nir": HREF, "scl": HREF}).items():
        it.add_asset(
            k, pystac.Asset(href, extra_fields={"raster:bands": [{"nodata": 0, "scale": 0.0001, "offset": -0.1}]})
        )
    return it


# ---------------------------------------------------------------- assets
def test_describe_asset_reads_stac_scale():
    a = describe_asset(make_item("a"), "red")
    assert (a["scale"], a["offset"], a["nodata"]) == (0.0001, -0.1, 0)


def test_missing_asset_error_lists_available():
    with pytest.raises(AssetError, match="available"):
        describe_asset(make_item("a", assets={"red": HREF}), "scl")


def test_non_https_asset_refused():
    with pytest.raises(AssetError, match="non-HTTPS"):
        describe_asset(make_item("a", assets={"red": "s3://bucket/B04.tif"}), "red")


def test_requester_pays_refused():
    with pytest.raises(AssetError, match="requester-pays"):
        describe_asset(make_item("a", requester_pays=True), "red")


def test_relative_orbit_parsing():
    assert relative_orbit("S2B_MSIL2A_20250331T083559_N0511_R064_T36RTV_20250331T124031.SAFE") == "064"
    assert relative_orbit(None) is None


# ---------------------------------------------------------------- tile + date dedup
def test_tile_filter_drops_adjacent_tile_views():
    items = [make_item("a", "36RTV"), make_item("b", "36RUV"), make_item("c", "36RTV")]
    assert [i.id for i in tile_items(items, "36RTV")] == ["a", "c"]


def _screen(rows):
    base = {"usable_fraction": 1.0, "view_incidence_angle": 5.0, "error": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_dedup_one_item_per_date_with_documented_tiebreak():
    df = _screen(
        [
            {"id": "S2A_x", "date": "2025-04-17", "usable_fraction": 0.99},
            {"id": "S2B_x", "date": "2025-04-17", "usable_fraction": 0.997},  # higher usable wins
            {"id": "S2A_y", "date": "2025-05-17", "view_incidence_angle": 10.7},
            {"id": "S2B_y", "date": "2025-05-17", "view_incidence_angle": 5.1},  # tie -> nearer nadir
            {"id": "S2C_z", "date": "2025-06-01", "error": "IOError: boom"},  # errors never kept
        ]
    )
    kept, dropped = dedup_per_date(df)
    assert kept["id"].tolist() == ["S2B_x", "S2B_y"]
    assert sorted(dropped["id"]) == ["S2A_x", "S2A_y"]
    assert kept["date"].is_unique


def test_dedup_is_order_independent():
    df = _screen([{"id": "b", "date": "2025-01-01"}, {"id": "a", "date": "2025-01-01"}])
    assert dedup_per_date(df)[0]["id"].tolist() == dedup_per_date(df.iloc[::-1])[0]["id"].tolist() == ["a"]


# ---------------------------------------------------------------- selection
def _eligible(dates, usable=1.0):
    return pd.DataFrame({"id": [f"i{d}" for d in dates], "date": dates, "usable_fraction": usable})


def test_selection_spread_gap_and_determinism():
    dates = pd.date_range("2025-03-20", "2025-06-29", freq="2D").strftime("%Y-%m-%d").tolist()
    e = _eligible(dates)
    a = select_dates(e, "2025-03-20", "2025-06-29", 8, 10)
    b = select_dates(e.sample(frac=1, random_state=3), "2025-03-20", "2025-06-29", 8, 10)
    assert a["id"].tolist() == b["id"].tolist() and len(a) == 8
    days = pd.to_datetime(a["date"]).diff().dt.days.dropna()
    assert days.min() >= 10
    assert a["date"].min() < "2025-04-05" and a["date"].max() > "2025-06-10"  # spread, not clustered


def test_selection_respects_window():
    e = _eligible(["2025-01-05", "2025-03-25", "2025-07-15"])
    assert select_dates(e, "2025-03-20", "2025-06-29", 8, 10)["date"].tolist() == ["2025-03-25"]


def test_too_few_dates_fails_instead_of_weakening():
    with pytest.raises(SelectionError, match="wider date range"):
        require_min_dates(_eligible(["2025-04-01"]), 6)


# ---------------------------------------------------------------- manifest
def _manifest():
    return {
        "manifest_version": 1,
        "frozen_at_utc": "2026-11-23T00:00:00Z",
        "stac": {"collection": "sentinel-2-c1-l2a"},
        "items": [{"id": "a", "date": "2025-04-17", "assets": {"red": {}, "nir": {}, "scl": {}}}],
    }


def test_manifest_fingerprint_ignores_timestamp_only(tmp_path):
    m = _manifest()
    fp = fingerprint(m)
    assert fingerprint({**m, "frozen_at_utc": "later"}) == fp
    assert fingerprint({**m, "items": []}) != fp
    saved = save_manifest(m, tmp_path / "m.json")
    assert load_manifest(tmp_path / "m.json")["fingerprint_sha256"] == saved["fingerprint_sha256"]


def test_tampered_manifest_rejected(tmp_path):
    save_manifest(_manifest(), tmp_path / "m.json")
    d = json.loads((tmp_path / "m.json").read_text())
    d["items"][0]["id"] = "edited"
    (tmp_path / "m.json").write_text(json.dumps(d))
    with pytest.raises(ManifestError, match="edited after freezing"):
        load_manifest(tmp_path / "m.json")


def test_manifest_duplicate_dates_rejected(tmp_path):
    m = _manifest()
    m["items"].append({**m["items"][0], "id": "b"})
    save_manifest(m, tmp_path / "m.json")
    with pytest.raises(ManifestError, match="more than one item"):
        load_manifest(tmp_path / "m.json")


def test_example_manifest_from_colab_run_is_valid():
    m = load_manifest("examples/manifest.json")
    assert len(m["items"]) == 8 and m["tile"] == "36RTV"


# ---------------------------------------------------------------- config
def test_default_config_loads():
    cfg = load_config("config/default.yaml")
    assert cfg["stac"]["collection"] == "sentinel-2-c1-l2a"


def test_missing_key_named_clearly():
    cfg = yaml.safe_load(open("config/default.yaml"))
    del cfg["network"]["stac_max_retries"]
    with pytest.raises(ConfigError, match="network.stac_max_retries"):
        validate_config(cfg)


def test_legacy_collection_rejected():
    cfg = yaml.safe_load(open("config/default.yaml"))
    cfg["stac"]["collection"] = "sentinel-2-l2a"
    with pytest.raises(ConfigError, match="no silent substitution"):
        validate_config(cfg)


def test_processing_fingerprint_changes_with_policy():
    cfg = load_config("config/default.yaml")
    fp = processing_fingerprint(cfg)
    cfg["quality"]["scl_usable_classes"] = [4, 5]
    assert processing_fingerprint(cfg) != fp
