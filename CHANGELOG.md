# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-11-23
### Changed
- CI: the workflow now lives at `.github/workflows/ci.yml` and starts with `name: ci`. The copy at the
  repository root (`ci.yml`, whose `name:` line held the path) is removed; GitHub only runs workflows
  from `.github/workflows/`. `.pre-commit-config.yaml`, missing from the published tree, is restored.
- `deltawatch compare`: float columns of the QA table are compared with `numpy.isclose`
  (`QA_FLOAT_RTOL = 1e-6`, `QA_FLOAT_ATOL = 1e-9`, NaN equals NaN) instead of exact equality. Integer,
  text, date, hash and ID columns stay exact; `seconds` and `reused_cached_output` stay ignored. The
  report records the tolerance, the largest absolute and relative difference per float column, and
  every failing column with its dates. Rasters and benchmark values are still compared exactly.

### Added
- Separate evaluation on a new AOI and season (`config/eval_new_aoi_2026.yaml`,
  `evaluations/new_aoi_2026/`, `deltawatch.evaluation`, `deltawatch evaluate discover|describe|score`):
  tile rule and fallbacks written before discovery, frozen manifest, `validate_run`, a context record
  written before scoring, every eligible target × seeds 301–305, full-support and joint-support
  scores, A−B on the joint support only, and a block bootstrap over the actual hidden blocks.
- `--item-source bucket` for that evaluation: lists the public Collection 1 COG bucket when the STAC
  API is unreachable and records this in the manifest.
- 18 new offline tests: QA float tolerance (8) and the evaluation (10).

### Unchanged on purpose
- Pipeline algorithm, baselines A/B and their settings, `config/default.yaml`, `examples/`,
  `reference/`, the validation and test splits and seeds. A fresh run still reproduces all v0.2
  rasters and scores (`all_preserved: true`).

## [0.3.0] - 2026-11-23
### Changed
- Repository layout: the source moved out of `deltawatch_repo_v0.2.0.zip` into the repository root, so
  `README.md`, `.github/workflows/ci.yml` and the package are now real files in the tree. The duplicate
  `README (1).md` and the source zip were removed (both remain in git history at `0cb858f`).
  `deltawatch_results (2).zip` was moved unchanged to `reference/v0.2/deltawatch_results_v0.2.zip`.
- Benchmark results now carry `protocol.declared_protocol`, which describes the declared protocol,
  in place of `protocol.tuning`, which asserted a history the program cannot verify.
- CI: added Python 3.13 to the matrix, read-only permissions, and a check that the CLI entry point
  installs. `ruff` is pinned to 0.16.9 so formatting checks don't drift.
- The Colab notebook clones the public repository instead of asking for an uploaded zip.

### Added
- `benchmark.validate_run`: before any score is written, every required NDVI and mask file is checked
  for presence, band count, dtype, nodata, the manifest grid (CRS, transform, shape), provenance tags
  (`manifest_sha256`, `processing_sha256`, `source_item`), mask values {0, 1}, and NDVI/mask consistency.
  All problems are reported together with file names. Nothing is reprojected or repaired.
  The results file records the verified fingerprints under `input_validation`.
- `deltawatch compare` (`deltawatch.reference`): a results-preservation check of a run against the
  frozen v0.2 reference. It compares values, grids and meaningful metadata (not bytes) and lists the
  documented v0.3 JSON changes separately.
- 20 new offline tests: positive and negative input validation (17) and reference comparison (3).

### Unchanged on purpose
- Frozen manifest, dates, splits, seeds, block layout, masking/scaling policy, metric definitions and
  baselines A/B. A fresh run reproduces all v0.2 rasters and scores (see README).

## [0.2.0] - 2026-11-23
### Added
- Optional locked benchmark (`deltawatch benchmark`): past-only baselines A (latest within 30 days) and
  B (median of up to 3 within 30 days) on non-overlapping hidden 20 px blocks, with validation/test
  targets and seeds locked in config before scoring, an evaluator isolated from predictors, and
  intersection metrics. Tests cover past-only context, truth isolation, deterministic masks,
  hand-calculated MAE/RMSE, empty support and coverage arithmetic.

### Fixed
- `pytest` now finds the package without installation (`pythonpath = ["src"]`); the first fresh
  Colab run failed test collection with `ModuleNotFoundError: deltawatch`.

## [0.1.0] - 2026-11-23
### Added
- Config-driven STAC discovery on Earth Search `sentinel-2-c1-l2a`, single MGRS tile (36RTV).
- SCL-only AOI screening, one item per acquisition date with a documented tie-break, deterministic
  date selection spread over a configurable window, frozen manifest with SHA-256 fingerprint.
- Windowed COG reads on a reference grid snapped to the red 10 m grid; nearest-neighbour SCL
  resampling; scale/offset reconciliation between STAC and raster metadata (fails on conflict).
- Per-date NDVI (float32, NaN nodata) and valid-mask (uint8) GeoTIFFs with provenance tags and
  read-back verification; QA CSV, QA panel with attribution, run metadata with measured timings/memory.
- Fingerprint-checked reuse of cached outputs.
- CLI (`deltawatch discover`, `deltawatch run`), offline pytest suite, optional live smoke test,
  ruff, pre-commit and GitHub Actions CI configuration.

### Decisions
- Selection window narrowed to 2025-03-20..2025-06-29 so later benchmark targets have earlier
  context within 30 days (full-season spread left gaps up to 37 days).
- NDVI is a valid observation only where red > 0 and nir > 0; slightly negative reflectances are
  kept and counted, never clipped (found on 2025-04-05 and 2025-04-17).
