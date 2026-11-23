# DeltaWatch v0.3.0: change summary and verification

Baseline: `ayatfakhry/deltawatch` at `0cb858f54d96bf4b3e71db2f332eeb31da0a6744`, cloned read-only.
Nothing was pushed, no PR was opened, and no history was rewritten.

## What I found at the baseline
- The repository root held `README (1).md`, `deltawatch_repo_v0.2.0.zip`, `deltawatch_results (2).zip`, `benchmark_test_panel.png` and `LICENSE` (MIT).
- Both zips are byte-identical (SHA-256) to the files delivered on 2026-11-23, and `README (1).md` is identical to the README inside the source zip.
- The root `benchmark_test_panel.png` is an earlier render of the benchmark figure (a longer panel title, same data). It is kept under `reference/v0.2/`.
- The source zip also contained a stray `.ruff_cache/`, a generated artifact that was not carried over.
- Baseline checks on the unmodified extracted source (local, Python 3.13): **62 passed, 1 skipped, 85 % coverage, ruff clean.** These match the review figures.
- The reference GeoTIFFs already carry `manifest_sha256`, `processing_sha256` and `source_item` tags, and their values verify against `examples/manifest.json` and the current config. **No legacy or migration path was needed**, so none was added.

## Files changed (v0.2 source → v0.3)
| File | Change |
|---|---|
| *(repo root)* | Source moved from the zip into the tree. `README (1).md` and the source zip were removed from the tree; both are still in git history at `0cb858f` |
| `reference/v0.2/deltawatch_results_v0.2.zip` | `deltawatch_results (2).zip`, moved **unchanged** |
| `reference/v0.2/benchmark_test_panel_published_root.png` | The root panel, moved unchanged |
| `reference/v0.2/README.md` | New: what each reference file is and where it was produced |
| `src/deltawatch/benchmark.py` | `validate_run()` + new `load_run()` contract, `input_validation` in results, `declared_protocol` replaces `tuning` |
| `src/deltawatch/reference.py` | New: results-preservation comparison |
| `src/deltawatch/pipeline.py` | New `compare` CLI subcommand |
| `src/deltawatch/__init__.py`, `pyproject.toml` | Version 0.3.0; `ruff==0.16.9` pinned in `[dev]` |
| `.github/workflows/ci.yml` | Python 3.11–3.13, `permissions: contents: read`, `deltawatch --help` smoke step |
| `.gitignore` | Keeps `reference/**/*.zip` despite the `*.zip` rule |
| `README.md` | Fresh-clone path (offline vs online steps), source/reference/generated table, declared-protocol wording, input validation, v0.3 measurements, honest CI status (no badge) |
| `notebooks/demo_colab.ipynb` | Clones the public repo, pins ruff, adds a `compare` cell |
| `CHANGELOG.md` | 0.3.0 entry |
| `tests/conftest.py`, `tests/test_pipeline_offline.py` | Synthetic scene fixture moved to conftest (no assertion changed) |
| `tests/test_benchmark_input_validation.py` | New, 17 tests |
| `tests/test_reference_compare.py` | New, 3 tests |

The full source diff is in `v02_to_v03_source.diff`, the git patch in `v0.3.0.patch`, and the git bundle in `deltawatch_v0.3.0.bundle` (commits after `0cb858f`).

## Phase 2: benchmark input validation
`run_benchmark` now calls `validate_run()` before computing anything. For **every** manifest date it checks:
- both files exist;
- 1 band; NDVI float32 with NaN nodata; mask uint8 with nodata 255;
- CRS, transform and shape equal the manifest grid;
- the tags `manifest_sha256` = the manifest's own fingerprint, `processing_sha256` = `processing_fingerprint(cfg)` (existing canonicalization, unchanged), and `source_item` = the manifest item for that date;
- mask values only {0, 1}; NDVI finite and in [-1, 1] where mask = 1, NaN where mask = 0.

The manifest dict itself is re-fingerprinted. All problems are reported in one error, each with the file and the reason, and **no results file is written**. Nothing is reprojected or repaired.

**Tests:** 17 cover one positive case and these negatives: another run with the same dates but a different processing policy; a manifest-hash tag mismatch; scoring against a different manifest; an edited manifest dict; a processing-hash tag mismatch; a missing provenance tag; a shifted transform; a wrong CRS; a wrong shape; a missing file; wrong nodata; mask value 2; an NDVI value where mask = 0; NaN or 1.5 where mask = 1; a bad last file (not only the first is checked); and several problems reported together.
**Mutation check:** with validation bypassed (old `load_run`), **16 of the 17 fail**. The one that still passes calls `validate_run` directly.

## Phase 3: wording and results preservation
- `protocol.tuning` ("none … test scored once") was replaced by `protocol.declared_protocol`. It says the settings come from config, that the documentation declares they were fixed before the first scoring, and that the file **cannot verify the history of earlier runs**.
- No change to the frozen manifest, dates, splits, seeds, block layout, window, masking/scaling, metrics or baselines.

### Measured checks (all local; none of this is GitHub Actions)
| Check | Result |
|---|---|
| Fresh clone of the v0.3 commit, clean venv, `pip install -e ".[dev]"` on Python 3.11, 3.12, 3.13 | installs; `deltawatch --help` works |
| `pytest --cov=deltawatch` (each Python) | **82 passed, 1 skipped (live test), 85 % line coverage** |
| `ruff check .` / `ruff format --check .` | clean (22 files) |
| Warnings | Python 3.11 env resolved rasterio 1.4.4, which emits ~600 `PendingDeprecationWarning`s from inside rasterio/affine (not our code); none on 3.12/3.13 |
| `deltawatch run` on `examples/manifest.json` (online COG reads) | 8 dates, 9.8 s processing, peak RSS 229 MB, 9.7 MB outputs (Python 3.13) |
| `deltawatch benchmark` | completed after validation passed |
| `deltawatch compare` vs `reference/v0.2/deltawatch_results_v0.2.zip` | **exit 0: 16/16 rasters identical** (values including NaN, CRS, transform, shape, dtype, nodata, provenance tags); **QA table identical** (33 columns, runtime columns excluded); **benchmark identical** in all settings, counts, coverage, MAE, RMSE, bias, block coordinates and provenance. Only the documented fields differ: `protocol.tuning` → `protocol.declared_protocol`, and the new `input_validation` |

Benchmark values reproduced: validation 2025-05-17 A MAE 0.08158 / RMSE 0.11151, B 0.13039 / 0.16531; test 2025-06-23 A 0.10038 / 0.1324, B 0.12121 / 0.15954; coverage 0.9989 / 0.9976.

**Reference caveat:** the reference GeoTIFFs were generated in a second environment from the same manifest (the Colab bundle excluded `.tif`). The reference QA table and benchmark JSON come from Ayat's Colab run. This is documented in `reference/v0.2/README.md`.

### Not done / limits
- **No GitHub Actions run exists for v0.3**, so CI success is not claimed and no badge was added. The CI file was reviewed, and its exact commands were run locally from a fresh clone.
- Coverage of `pipeline.py` (62 %) and `catalog.py` (76 %) is mostly the online discovery path, which the offline tests don't reach.
- `discover` (live STAC search) was not re-run. The STAC API is not reachable from my environment, and it isn't needed to reproduce v0.2.

## What Ayat needs to do
1. Replace the repository contents with the tree in `deltawatch_v0.3.0_repo.zip`. That means deleting the 4 old root files (`README (1).md`, `deltawatch_repo_v0.2.0.zip`, `deltawatch_results (2).zip`, `benchmark_test_panel.png`) and uploading the new files, including `.github/` and `reference/`. With git, `git pull deltawatch_v0.3.0.bundle v0.3-hardening` applies the exact commit on top of `0cb858f`.
2. Open the **Actions** tab and check the `ci` run on Python 3.11/3.12/3.13. Only a green run there counts as CI success.
3. Optionally run `notebooks/demo_colab.ipynb` in Colab. It now clones the public repo, so it works only after step 1.

## Phase 4: follow-up design (not implemented)
**External, pre-registered evaluation.** Before looking at any new scores, pick one AOI or season not used so far: either a second Delta AOI fully inside a single tile with the same size, or the 2025/26 winter season on the current AOI. Freeze a new manifest with the same discovery and selection code, then write a protocol file. It names validation targets (allowed for any choices, e.g. the age limit) and later test targets (scored once). It uses at least 5 targets per split, each with ≥ 2 earlier valid dates within 30 days, and 10 block seeds per target. Results are reported **per target date**, with mean ± SD and the range across seeds, coverage and abstention counts, plus intersection metrics. A/B are compared per date, not only on the pooled mean. Past-only context stays enforced. v0.2/v0.3 results stay frozen as they are, and the AOI, season and protocol aren't changed after test scores are seen. No deep learning, dashboards or paid services.
