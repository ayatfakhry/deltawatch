# Third-party notices

## Satellite data
Derived products in this repository (NDVI rasters, masks, QA figures and tables) are generated from
Copernicus Sentinel-2 Level-2A data.

**Contains modified Copernicus Sentinel data 2025 and 2026.**

Use of Sentinel data is governed by the
[Legal Notice on the use of Copernicus Sentinel Data and Service Information](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice).
The data terms are separate from, and are not changed by, the license chosen for this repository's code.

## Data access and catalog
Scenes are discovered through the [Element 84 Earth Search](https://github.com/Element84/earth-search)
STAC API (`https://earth-search.aws.element84.com/v1`, collection `sentinel-2-c1-l2a`) and read as
public Cloud-Optimized GeoTIFFs from the
[Sentinel-2 L2A COGs on the Registry of Open Data on AWS](https://registry.opendata.aws/sentinel-2-l2a-cogs/).
No Earth Search branding, screenshots or third-party code are included in this repository.

## Python dependencies
Installed from PyPI, each under its own license: NumPy (BSD-3-Clause), pandas (BSD-3-Clause),
Rasterio (BSD-3-Clause; bundles GDAL, MIT/X), pystac-client (Apache-2.0), PyYAML (MIT),
Matplotlib (Matplotlib License, PSF-based), pytest (MIT), ruff (MIT).
