"""Static STAC catalog for the DeltaWatch COGs: one Collection, one Item per frozen date.

Every metadata value is read from the COG files, the frozen manifest or the frozen QA table.
A field whose value is not known from those sources is left out, never guessed.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pystac
import rasterio
from pystac.extensions.classification import Classification, ClassificationExtension
from pystac.extensions.eo import EOExtension
from pystac.extensions.file import FileExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.raster import DataType, RasterBand, RasterExtension, Sampling, Statistics
from rasterio.warp import transform_geom

from ..manifest import load_manifest
from .cog import DistributionError, frozen_sources, sha256_file

COLLECTION_ID = "deltawatch-ndvi"
COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
SCL_CLOUD_CLASSES = (8, 9, 10)  # cloud medium probability, cloud high probability, thin cirrus
REPO_URL = "https://github.com/ayatfakhry/deltawatch"
LEGAL_NOTICE_URL = "https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice"


def item_id(date: str) -> str:
    return f"{COLLECTION_ID}-{date}"


def _multihash_sha256(path: Path) -> str:
    # file:checksum is a multihash: 0x12 = sha2-256, 0x20 = 32-byte digest, then the hex digest.
    return "1220" + sha256_file(path)


def _footprint(src: rasterio.DatasetReader) -> dict:
    """Raster outline in EPSG:4326 from its four UTM corners (edge curvature over 5 km is negligible)."""
    left, bottom, right, top = src.bounds
    ring = [[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]
    geom = {"type": "Polygon", "coordinates": [ring]}
    out = transform_geom(src.crs, "EPSG:4326", geom, precision=7)
    out["coordinates"] = [[list(p) for p in r] for r in out["coordinates"]]
    return out


def _bbox(geom: dict) -> list[float]:
    xs = [p[0] for p in geom["coordinates"][0]]
    ys = [p[1] for p in geom["coordinates"][0]]
    return [min(xs), min(ys), max(xs), max(ys)]


def _band(src: rasterio.DatasetReader, with_stats: bool) -> RasterBand:
    nodata = src.nodata
    if nodata is not None and np.isnan(nodata):
        nodata = "nan"  # the raster extension's spelling for a NaN nodata value
    band = RasterBand.create(
        nodata=nodata,
        data_type=DataType(src.dtypes[0]),
        spatial_resolution=float(src.res[0]) if src.res[0] == src.res[1] else None,
        sampling=Sampling.AREA if src.tags().get("AREA_OR_POINT") == "Area" else None,
        unit=src.units[0] or None,  # NDVI GeoTIFFs carry no band unit, so the field is omitted
    )
    if with_stats:
        a = src.read(1, masked=True).astype("float64")
        a = np.ma.masked_invalid(a)
        n = int(a.count())
        band.statistics = Statistics.create(
            minimum=float(a.min()),
            maximum=float(a.max()),
            mean=float(a.mean()),
            stddev=float(a.std()),
            valid_percent=round(100.0 * n / a.size, 4),
        )
    return band


def _mask_classes(tag: str) -> list[Classification]:
    """Parse the mask's own 'values' tag, e.g. '1=valid observation, 0=not valid, 255=nodata (unused)'."""
    classes = []
    for part in tag.split(","):
        value, _, desc = part.strip().partition("=")
        v, desc = int(value), desc.strip()
        name = re.sub(r"[^0-9A-Za-z]+", "_", desc.split("(")[0].strip()).strip("_").lower()
        classes.append(Classification.create(value=v, name=name, description=desc, nodata=True if v == 255 else None))
    return classes


def _qa_rows(cfg: dict, repo_root: Path) -> dict[str, dict]:
    """The frozen QA table from the same archive as the rasters (colab_run/qa_per_date.csv)."""
    archive = repo_root / cfg["source"]["archive"]
    with zipfile.ZipFile(archive) as z:
        text = z.read(cfg["source"]["qa_member"]).decode()
    return {r["date"]: r for r in csv.DictReader(io.StringIO(text))}


def _cloud_cover(row: dict) -> float:
    scl = {k: int(v) for k, v in row.items() if k.startswith("scl_")}
    total = sum(scl.values())
    if total == 0:
        raise DistributionError(f"{row['date']}: QA row has no SCL counts")
    return round(100.0 * sum(scl[f"scl_{c}"] for c in SCL_CLOUD_CLASSES) / total, 4)


def render_thumbnail(ndvi_cog: Path, dst: Path, thumb_cfg: dict) -> Path:
    """PNG preview from the /2 overview with a fixed colour range; NaN is transparent. Not data."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import colormaps
    from matplotlib import pyplot as plt

    with rasterio.open(ndvi_cog, overview_level=0) as src:
        a = src.read(1)
    cmap = colormaps[thumb_cfg["colormap"]].with_extremes(bad=(0, 0, 0, 0))
    plt.imsave(
        dst,
        np.ma.masked_invalid(a),
        cmap=cmap,
        vmin=float(thumb_cfg["vmin"]),
        vmax=float(thumb_cfg["vmax"]),
        format="png",
        metadata={"Software": None},  # keeps the PNG free of version strings, so rebuilds are byte-stable
    )
    return dst


def build_item(
    date: str, item_dir: Path, manifest_item: dict, qa_row: dict, manifest_sha: str, thumb_cfg: dict | None = None
) -> pystac.Item:
    ndvi_path, mask_path = item_dir / f"ndvi_{date}.tif", item_dir / f"valid_{date}.tif"
    with rasterio.open(ndvi_path) as n, rasterio.open(mask_path) as m:
        if (n.crs, n.transform, n.shape) != (m.crs, m.transform, m.shape):
            raise DistributionError(f"{date}: NDVI and mask are not on the same grid")
        tags = n.tags()
        if tags.get("manifest_sha256") != manifest_sha:
            raise DistributionError(f"{date}: COG was not produced from the frozen manifest")
        if tags.get("source_item") != manifest_item["id"]:
            raise DistributionError(f"{date}: COG source_item does not match the manifest")
        geom = _footprint(n)
        acquired = datetime.fromisoformat(tags["acquired"].replace("Z", "+00:00"))
        item = pystac.Item(
            id=item_id(date),
            geometry=geom,
            bbox=_bbox(geom),
            datetime=acquired,
            properties={
                "title": f"DeltaWatch NDVI {date}",
                "platform": manifest_item["platform"],
                "deltawatch:source_item": tags["source_item"],
                "deltawatch:source_collection": tags["collection"],
                "deltawatch:manifest_sha256": tags["manifest_sha256"],
                "deltawatch:processing_sha256": tags["processing_sha256"],
                "deltawatch:valid_fraction": float(qa_row["valid_fraction"]),
            },
        )
        proj = ProjectionExtension.ext(item, add_if_missing=True)
        proj.apply(
            code=n.crs.to_string(),
            shape=[n.height, n.width],
            transform=list(n.transform)[:6],
            bbox=list(n.bounds),
        )
        EOExtension.ext(item, add_if_missing=True).apply(cloud_cover=_cloud_cover(qa_row))

        ndvi = pystac.Asset(
            href=f"./{ndvi_path.name}",
            media_type=COG_MEDIA_TYPE,
            title="NDVI",
            description=(
                "NDVI = (nir - red) / (nir + red) from Sentinel-2 L2A B08 and B04 reflectance, unitless. "
                "NaN where the observation is not valid."
            ),
            roles=["data"],
        )
        mask = pystac.Asset(
            href=f"./{mask_path.name}",
            media_type=COG_MEDIA_TYPE,
            title="Validity mask",
            description=m.tags().get("values"),
            roles=["data-mask"],
        )
        item.add_asset("ndvi", ndvi)
        item.add_asset("valid", mask)
        thumb_path = None
        if thumb_cfg:
            thumb_path = render_thumbnail(ndvi_path, item_dir / f"thumbnail_{date}.png", thumb_cfg)
            item.add_asset(
                "thumbnail",
                pystac.Asset(
                    href=f"./{thumb_path.name}",
                    media_type=pystac.MediaType.PNG,
                    title="Preview (not data)",
                    description=(
                        f"NDVI /2 overview, colormap {thumb_cfg['colormap']} from {thumb_cfg['vmin']} to "
                        f"{thumb_cfg['vmax']}; transparent = not valid."
                    ),
                    roles=["thumbnail"],
                ),
            )
        RasterExtension.ext(ndvi, add_if_missing=True).apply([_band(n, with_stats=True)])
        RasterExtension.ext(mask, add_if_missing=True).apply([_band(m, with_stats=False)])
        ClassificationExtension.ext(mask, add_if_missing=True).apply(classes=_mask_classes(m.tags()["values"]))
    files = [(ndvi, ndvi_path), (mask, mask_path)]
    if thumb_path is not None:
        files.append((item.assets["thumbnail"], thumb_path))
    for asset, path in files:
        f = FileExtension.ext(asset, add_if_missing=True)
        f.checksum = _multihash_sha256(path)
        f.size = path.stat().st_size
    item.add_link(pystac.Link(rel="license", target=LEGAL_NOTICE_URL, title="Copernicus Sentinel data legal notice"))
    return item


def build_catalog(cfg: dict, repo_root: str | Path, catalog_dir: str | Path) -> pystac.Catalog:
    """Write catalog.json, collection.json and one Item JSON per date next to the existing COGs."""
    repo_root, catalog_dir = Path(repo_root), Path(catalog_dir)
    manifest = load_manifest(repo_root / cfg["source"]["manifest"])
    by_date = {it["date"]: it for it in manifest["items"]}
    dates = sorted(frozen_sources(cfg, repo_root))
    qa = _qa_rows(cfg, repo_root)
    col_dir = catalog_dir / COLLECTION_ID
    items = []
    for d in dates:
        if not (col_dir / d / f"ndvi_{d}.tif").exists():
            raise DistributionError(f"{d}: COG missing in {col_dir / d}; run the COG step first")
        items.append(
            build_item(d, col_dir / d, by_date[d], qa[d], manifest["fingerprint_sha256"], cfg.get("thumbnail"))
        )

    bbox = [
        min(i.bbox[0] for i in items),
        min(i.bbox[1] for i in items),
        max(i.bbox[2] for i in items),
        max(i.bbox[3] for i in items),
    ]
    times = sorted(i.datetime for i in items)
    collection = pystac.Collection(
        id=COLLECTION_ID,
        title="DeltaWatch NDVI (Nile Delta, 5 x 5 km, Mar-Jun 2025)",
        description=(
            "Per-date NDVI and validity masks from the DeltaWatch pipeline for one 5 x 5 km area of the Nile "
            "Delta (Sentinel-2 L2A, tile 36RTV, 8 frozen dates). Packaged as Cloud Optimized GeoTIFFs from the "
            "frozen v0.2 results; pixel values are identical to the pipeline outputs. Contains modified "
            "Copernicus Sentinel data 2025."
        ),
        extent=pystac.Extent(
            pystac.SpatialExtent([bbox]),
            pystac.TemporalExtent([[times[0], times[-1]]]),
        ),
        license="other",
        providers=[
            pystac.Provider(
                name="European Union / ESA / Copernicus",
                roles=[pystac.ProviderRole.PRODUCER, pystac.ProviderRole.LICENSOR],
                url="https://sentinels.copernicus.eu/",
            ),
            pystac.Provider(name="DeltaWatch", roles=[pystac.ProviderRole.PROCESSOR], url=REPO_URL),
        ],
        summaries=pystac.Summaries({"platform": sorted({i.properties["platform"] for i in items})}),
    )
    # Addition: item_assets (core in STAC 1.1) tells clients up front which assets every Item carries.
    collection.item_assets = {
        key: pystac.ItemAssetDefinition.create(title=a.title, description=None, media_type=a.media_type, roles=a.roles)
        for key, a in items[0].assets.items()
    }
    collection.add_link(
        pystac.Link(rel="license", target=LEGAL_NOTICE_URL, title="Copernicus Sentinel data legal notice")
    )
    collection.add_link(pystac.Link(rel="about", target=REPO_URL, media_type="text/html", title="DeltaWatch source"))
    for it in items:
        collection.add_item(it)
    catalog = pystac.Catalog(
        id="deltawatch",
        title="DeltaWatch",
        description="Static STAC catalog of DeltaWatch outputs. No server-side search.",
    )
    catalog.add_child(collection)
    # Item JSON sits in the date folder next to its COGs, so asset hrefs stay "./<file>.tif".
    catalog.normalize_hrefs(str(catalog_dir), strategy=pystac.layout.TemplateLayoutStrategy(item_template="${date}"))
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    return catalog


def validate_catalog(catalog_dir: str | Path) -> dict:
    """Schema-validate every JSON document and walk every internal link. Raises on any invalid object.

    pystac ships the core and extension JSON schemas used here, so validation needs no network.
    """
    from pystac.validation import validate_dict

    catalog_dir = Path(catalog_dir)
    docs = sorted(catalog_dir.rglob("*.json"))
    for path in docs:
        validate_dict(json.loads(path.read_text()), href=str(path))  # raises pystac.STACValidationError
    # Also make sure pystac can load the whole tree from the root.
    n_items = len(list(pystac.Catalog.from_file(str(catalog_dir / "catalog.json")).get_items(recursive=True)))
    return {"documents_validated": len(docs), "items": n_items, **walk_links(catalog_dir / "catalog.json")}


def walk_links(root_json: str | Path) -> dict:
    """Follow every relative link and asset href from the root; every target must exist and parse."""
    root_json = Path(root_json).resolve()
    base = root_json.parent
    seen, stack, checked, broken = set(), [root_json], 0, []
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        doc = json.loads(path.read_text())
        targets = [(lk["rel"], lk["href"]) for lk in doc.get("links", [])]
        targets += [(f"asset:{k}", a["href"]) for k, a in doc.get("assets", {}).items()]
        for rel, href in targets:
            if urlparse(href).scheme in ("http", "https"):
                continue  # external (license, repo) links are not part of the self-contained catalog
            checked += 1
            target = (path.parent / href).resolve()
            if not target.exists() or not target.is_relative_to(base):
                broken.append({"from": str(path.relative_to(base)), "rel": rel, "href": href})
            elif target.suffix == ".json" and rel in ("child", "item", "root", "parent", "collection"):
                stack.append(target)
            if Path(href).is_absolute():
                broken.append({"from": str(path.relative_to(base)), "rel": rel, "href": href, "why": "absolute"})
    return {"documents_walked": len(seen), "links_checked": checked, "broken_links": broken}


def verify_assets(catalog_dir: str | Path) -> dict:
    """Recompute every asset's size and SHA-256 and compare with file:size / file:checksum in its Item."""
    catalog_dir = Path(catalog_dir)
    checked, mismatches = 0, []
    for path in sorted((catalog_dir / COLLECTION_ID).glob("*/*.json")):
        item = json.loads(path.read_text())
        for key, a in item["assets"].items():
            f = path.parent / a["href"]
            checked += 1
            if not f.exists():
                mismatches.append({"item": item["id"], "asset": key, "why": "missing"})
            elif f.stat().st_size != a.get("file:size"):
                mismatches.append({"item": item["id"], "asset": key, "why": "size"})
            elif _multihash_sha256(f) != a.get("file:checksum"):
                mismatches.append({"item": item["id"], "asset": key, "why": "checksum"})
    return {"assets_checked": checked, "mismatches": mismatches, "ok": checked > 0 and not mismatches}
