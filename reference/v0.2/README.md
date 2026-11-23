# v0.2 reference outputs (frozen, do not overwrite)

These files are the reference that `deltawatch compare` checks new runs against. Nothing in the
package writes here.

| File | What it is | Where it was produced |
|---|---|---|
| `deltawatch_results_v0.2.zip` → `colab_run/` | QA table, QA panel, run metadata and benchmark results/panel | Final v0.2 run in a fresh Google Colab CPU session, 2026-11-23 |
| `deltawatch_results_v0.2.zip` → `geotiffs/` | 16 GeoTIFFs: NDVI (float32, NaN nodata) + valid mask (uint8, nodata 255) for the 8 frozen dates | Generated in a second Linux environment from the same frozen manifest with v0.2 code, because the Colab bundle excluded `.tif` files. Their QA values were checked to be identical to the Colab run |
| `deltawatch_results_v0.2.zip` → `discovery/` | Frozen manifest, screening table for all 70 tile items, STAC search results, milestone-1 preview | Colab discovery run, 2026-11-23 |
| `benchmark_test_panel_published_root.png` | The benchmark figure that sat at the repository root in commit 0cb858f (an earlier render with a longer panel title; same data as `examples/benchmark_test_panel.png`) | v0.2 |

Provenance of the GeoTIFFs can be checked from their tags:
`manifest_sha256 = a48a72d3…eb603` (the fingerprint of `examples/manifest.json`) and
`processing_sha256 = 5dcd867c…f62a0` (the fingerprint of the `quality` and `assets` config sections).

This archive was published at the repository root in commit `0cb858f` as `deltawatch_results (2).zip`
and was moved here unchanged (SHA-256 `2727cf89…fd2780`).
