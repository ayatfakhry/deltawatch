# DeltaWatch v0.4.0: change summary and verification

Baseline: `ayatfakhry/deltawatch` at `43e156a` (main), cloned read-only. Nothing was pushed, no PR
was opened and no history was rewritten. Every number below comes from a command run on
2026-11-23 in a Linux build sandbox (Python 3.13.18, numpy 2.5.3, unless a row says otherwise).
**GitHub Actions has not run this code; no CI status is claimed.**

## What I found at the baseline
- `ci.yml` was at the repository root, with `name: .github/workflows/ci.yml` as its first line. GitHub
  only runs workflows inside `.github/workflows/`, so this file never ran. There was no `.github/`
  folder in the published tree.
- `.pre-commit-config.yaml` (part of v0.3) was missing from the published tree.
- Leftovers at the root from manual uploads: `README (1).md`, `deltawatch_repo_v0.2.0.zip`,
  `deltawatch_results (2).zip`, `benchmark_test_panel.png`, `compare_report_v0.3_vs_v0.2.json`,
  `CHANGES_v0.3.md`. v0.4 does not touch them; deleting them is optional (see the reply).
- `git check-ignore -v examples/* reference/* reference/v0.2/*` printed nothing (exit 1): no file in
  `examples/` or `reference/` is ignored. All 10 reference files are tracked.

## Part 1: CI
| File | Change |
|---|---|
| `.github/workflows/ci.yml` | New path. Identical to the root `ci.yml` except line 1, which is now `name: ci` |
| `ci.yml` (root) | Deleted (git sees it as a rename) |
| `.pre-commit-config.yaml` | Restored from v0.3 (ruff-pre-commit v0.16.9) |

The workflow: triggers `push` and `pull_request`; `permissions: contents: read`; matrix Python
3.11 / 3.12 / 3.13; steps `pip install -e ".[dev]"`, `ruff check .`, `ruff format --check .`,
`pytest --cov=deltawatch --cov-report=term-missing`, `deltawatch --help`. No other line changed. No
badge was added to the README.

## Part 2: float tolerance in the QA comparison
`src/deltawatch/reference.py`: `QA_FLOAT_RTOL = 1e-6`, `QA_FLOAT_ATOL = 1e-9` (documented in the
code). Float columns use `numpy.isclose(..., equal_nan=True)`. Integer, text, date, hash and ID
columns are compared exactly; `seconds` and `reused_cached_output` stay ignored. Rows must align by
date. The report now has `float_tolerance`, `float_columns` (per column: `max_abs_diff`,
`max_rel_diff`, `within_tolerance`), `exact_columns` and `failures` (column, reason, dates, values).

Tests (`tests/test_reference_compare.py`, 8 new cases): a +1e-8 change passes; a +1e-3 change fails
and names the column and date; changes to `scl_9`, `nodata_red_px`, `scale_offset_source` and `id`
fail (4 cases); NaN vs a value fails; the end-to-end report contains the tolerance.
**Mutation check:** with `compare_qa` reverted to exact `DataFrame.equals`, 7 of the 8 new cases
failed (the identical-input case passes either way, as it should).

## Part 3: separate evaluation on a new AOI and season
See `evaluations/new_aoi_2026/README.md` for the full table and the limitations.
- Rules written in `config/eval_new_aoi_2026.yaml` before discovery: period 2026-05-20..09-30,
  candidate AOI `[31.60, 30.60, 31.65, 30.65]`, the tile rule, two mechanical-only fallbacks,
  selection (12 / min 8 / gap 10 / usable ≥ 0.70), target rule, seeds 301–305, bootstrap.
  `config/default.yaml`, `examples/` and `reference/` are unchanged.
- **Final AOI: the candidate (no fallback used). Final tile: 36RUU** (the only zone-36 band-R tile
  whose grid contains the AOI). 80 items, 35 eligible dates, 11 selected.
- Manifest fingerprint `9da69a09541dc51deee59402bff892cc3d0f215d23d82166ba8c03ba0f5faabb`, written
  to `manifest_freeze_record.json` (20:10:25 UTC) before processing and scoring.
- **Item source:** the Earth Search STAC API host was blocked from the sandbox, so discovery used
  `--item-source bucket`: it lists the public COG bucket and reads each item's STAC JSON, with the
  same filters. Recorded in the manifest. A STAC cross-check in Colab has **not been run**.
- `validate_run`: verified, 22 files, processing fingerprint `5dcd867c…f62a0` (same as v0.2).
- Context recorded before scoring (`context_description.json`): NDVI percentiles and SCL fractions
  per date; a summer crop cycle (median NDVI 0.21 → 0.75 → 0.67).
- 9 targets × 5 seeds = 45 runs, 0 failures. In all 45, A (`latest`) had lower MAE than B
  (`median3`) on the joint support, and the 95 % block-bootstrap interval of A−B was below zero.
  Joint support equalled full support (only-A = only-B = 0). No pooled number, no p-values.
- Reuse: `run_split` (which calls `block_mask`, `Evaluator`, `predict`), `metrics`, `load_run` /
  `validate_run`, `screen_item`, `dedup_per_date`, `select_dates`, `snapped_grid`, `save_manifest`.

Tests (`tests/test_evaluation.py`, 10): hidden truth does not change predictions; no future or
same-day context; target rule and seeds; joint support on a synthetic case; bootstrap uses the
actual blocks and is deterministic; bootstrap resamples whole blocks; tile rule; zone/band; fallback
order; offline end-to-end run of `run_evaluation`. **Mutation check:** unseeded bootstrap, joint as
union, target rule off by one, same-day leak, future leak, and pixel-level instead of block-level
grouping each made at least one test fail.

## Extras (beyond the plan, low risk, no frozen result touched)
- `deltawatch evaluate discover|describe|score` CLI, so the evaluation is reproducible in three
  commands; `describe` writes the context record before any score and `score` never overwrites it.
- `evaluations/new_aoi_2026/evaluation_per_target.png` and `make_figure.py`, drawn only from the
  saved outputs.
- The offline end-to-end and fallback-order tests above (coverage of `evaluation.py` 37 % → 67 %).
- Version bumped to 0.4.0; README, CHANGELOG and THIRD_PARTY_NOTICES (2026 data) updated.

## Verification (commands and results)
| Check | Command | Result |
|---|---|---|
| Lint | `ruff check . && ruff format --check .` | clean (30 files) |
| Tests, sandbox | `python -m pytest -p no:cacheprovider` | **100 passed, 1 skipped** |
| CI emulation, fresh clone of the v0.4 branch, uv venv per version | `pip install -e ".[dev]"; ruff check .; ruff format --check .; pytest --cov=deltawatch; deltawatch --help` | Python 3.11, 3.12, 3.13: lint clean, **100 passed, 1 skipped, 79 % coverage**, CLI ok (3.11 shows rasterio `PendingDeprecationWarning`s from a dependency, not from DeltaWatch) |
| v0.2 pipeline rerun | `deltawatch run --config config/default.yaml --manifest examples/manifest.json --out $O` | exit 0 |
| Benchmark | `deltawatch benchmark --config config/default.yaml --manifest examples/manifest.json --run-dir $O` | test 2025-06-23: A MAE 0.10038 / RMSE 0.1324, B MAE 0.12121 / RMSE 0.15954 (= v0.2) |
| Preservation | `deltawatch compare --manifest examples/manifest.json --run-dir $O --reference reference/v0.2/deltawatch_results_v0.2.zip --report $O/compare_report.json` | exit 0, **`all_preserved: true`**, 16/16 rasters equal, QA equal (6 float columns, max diff 0), no undocumented benchmark differences |
| Evaluation | `deltawatch evaluate discover --item-source bucket`; `deltawatch run --config config/eval_new_aoi_2026.yaml --manifest evaluations/new_aoi_2026/manifest.json --out evaluations/new_aoi_2026/run`; `deltawatch evaluate describe`; `deltawatch evaluate score` | as in Part 3; re-scoring after the last code change gave identical results |

Coverage is 79 % (85 % in v0.3) because the network discovery code in `evaluation.py` is not
exercised offline.

## Not run
- GitHub Actions (only a local emulation, above).
- Discovery through the STAC API for the new AOI (blocked in the sandbox; bucket listing used).
- The Colab notebook with v0.4.
- `DELTAWATCH_LIVE=1 pytest tests/live`.
