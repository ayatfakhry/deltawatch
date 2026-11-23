"""Local query service (not a STAC API): datetime/bbox filtering, errors, Range streaming. No network."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deltawatch.distribution.cog import build_dates, load_distribution_config
from deltawatch.distribution.query import create_app
from deltawatch.distribution.stac import COLLECTION_ID, build_catalog

REPO = Path(__file__).resolve().parents[1]
INSIDE = "30.92,30.92,30.93,30.93"  # inside the AOI
OUTSIDE = "31.5,30.5,31.6,30.6"  # eastern Delta, no overlap


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    out = tmp_path_factory.mktemp("catalog")
    cfg = load_distribution_config(REPO / "config" / "distribution.yaml")
    build_dates(cfg, REPO, out / COLLECTION_ID)
    build_catalog(cfg, REPO, out)
    return TestClient(create_app(out))


def _dates(r):
    return [f["id"].removeprefix(f"{COLLECTION_ID}-") for f in r.json()["features"]]


def test_landing_states_it_is_not_a_stac_api(client):
    body = client.get("/").json()
    assert "not a STAC API" in body["description"] and body["items"] == 8


def test_search_without_filters_returns_all_dates_in_order(client):
    r = client.get("/search")
    assert r.status_code == 200 and r.json()["numberMatched"] == 8
    assert _dates(r) == sorted(_dates(r))


@pytest.mark.parametrize(
    ("dt", "expected"),
    [
        ("2025-05-01/2025-05-31", ["2025-05-02", "2025-05-17", "2025-05-30"]),
        ("2025-05-30/2025-05-30", ["2025-05-30"]),  # a date range includes the whole end day
        ("../2025-04-05", ["2025-03-25", "2025-04-05"]),
        ("2025-06-14/..", ["2025-06-23"]),
        ("2025-05-17T08:51:52.943Z", ["2025-05-17"]),  # exact instant
        ("2025-05-17T00:00:00Z/2025-05-17T08:00:00Z", []),  # before the overpass that day
        ("2024-01-01/2024-12-31", []),
    ],
)
def test_datetime_filter(client, dt, expected):
    assert _dates(client.get("/search", params={"datetime": dt})) == expected


def test_bbox_filter(client):
    assert client.get("/search", params={"bbox": INSIDE}).json()["numberMatched"] == 8
    assert client.get("/search", params={"bbox": OUTSIDE}).json()["numberMatched"] == 0


def test_combined_filter_and_limit(client):
    r = client.get("/search", params={"bbox": INSIDE, "datetime": "2025-04-01/2025-06-30", "limit": 2})
    body = r.json()
    assert body["numberMatched"] == 7 and body["numberReturned"] == 2 and len(body["features"]) == 2


@pytest.mark.parametrize(
    "params",
    [
        {"bbox": "1,2,3"},
        {"bbox": "a,b,c,d"},
        {"bbox": "31,30,30,31"},  # min > max
        {"bbox": "0,-91,1,0"},
        {"datetime": "2025-13-01"},
        {"datetime": "2025-05-01T00:00:00"},  # no timezone
        {"datetime": "2025-06-01/2025-05-01"},
        {"datetime": "../.."},
        {"limit": 0},
    ],
)
def test_bad_input_is_a_client_error(client, params):
    assert client.get("/search", params=params).status_code in (400, 422)


def test_item_endpoint_and_absolute_asset_urls(client):
    r = client.get(f"/items/{COLLECTION_ID}-2025-05-30")
    assert r.status_code == 200
    href = r.json()["assets"]["ndvi"]["href"]
    assert href.endswith("/files/deltawatch-ndvi/2025-05-30/ndvi_2025-05-30.tif")
    assert client.get(f"/items/{COLLECTION_ID}-1999-01-01").status_code == 404


def test_cog_is_streamable_locally_with_range_requests(client):
    url = client.get(f"/items/{COLLECTION_ID}-2025-05-30").json()["assets"]["ndvi"]["href"]
    path = url.split("://", 1)[1].split("/", 1)[1]
    full = client.get("/" + path)
    part = client.get("/" + path, headers={"Range": "bytes=0-1023"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-1023/{len(full.content)}"
    assert part.content == full.content[:1024]
    assert part.content[:4] in (b"II*\x00", b"MM\x00*")  # TIFF header


def test_path_traversal_is_refused(client):
    assert client.get("/files/../config/distribution.yaml").status_code == 404
