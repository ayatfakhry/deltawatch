# Separate evaluation: new AOI, 2026 season

This folder is a **separate evaluation** of the two existing baselines (A `latest`, B `median3`) on
an area and period the project had not used. It does not replace or edit the v0.2 benchmark
(`examples/`, `reference/`, `config/default.yaml` are untouched). The pipeline, the baselines, their
settings (`max_age_days=30`, `median_k=3`, `hidden_fraction=0.10`, `block_px=20`) and the masking
code are the same as v0.2.

## What was fixed before anything was scored

| Item | Value | Where it is recorded |
|---|---|---|
| Config | `config/eval_new_aoi_2026.yaml` | written before discovery |
| Period | 2026-05-20 .. 2026-09-30 | config |
| AOI | candidate `[31.60, 30.60, 31.65, 30.65]` (eastern Delta); fallbacks lake `[30.50, 30.60, 30.55, 30.65]`, then Minya `[30.80, 28.10, 30.85, 28.15]`, mechanical failures only | config |
| Tile rule | one covering tile -> use it; several -> most dates with usable ≥ 0.70; tie -> alphabetical | config, `manifest.json` → `discovery` |
| Selection | target 12, min 8, min gap 10 days, min usable 0.70 (unchanged `select_dates`) | config |
| Targets | every frozen date with ≥ 2 earlier frozen dates within 30 days | `plan.json` |
| Seeds | 301, 302, 303, 304, 305 (mask layouts) | config, `plan.json` |
| Bootstrap | 1000 resamples of the actual hidden 20×20 blocks, seed `[20260520, target ordinal, mask seed]` | config |

## What happened

* **AOI used:** the candidate, no fallback was needed. **Tile:** `36RUU` (the only tile in zone 36,
  band R whose grid contains the whole AOI). 80 items on the tile in the window, 35 dates with
  usable ≥ 0.70 after one-per-date deduplication, **11 dates selected** (`select_dates` filled 11 of
  12 slots while keeping the 10-day gap; 11 ≥ min 8).
* **Manifest:** fingerprint `9da69a09541dc51deee59402bff892cc3d0f215d23d82166ba8c03ba0f5faabb`
  (file SHA-256 `471d90e1…cbe06d`), frozen and recorded in `manifest_freeze_record.json` before
  processing.
* **Item source:** the STAC API host was not reachable from the build sandbox, so discovery listed
  the public Collection 1 COG bucket (`--item-source bucket`) and read each item's own STAC JSON,
  then applied the same filters (collection, datetime, scene cloud < 60). The manifest records
  `item_source: bucket`. Re-running discovery with `--item-source stac` in Colab should return the
  same item IDs; that check has **not been run**.
* **Processing and input validation:** `validate_run` accepted all 11 dates (22 files, grid,
  dtypes, nodata, provenance tags; processing fingerprint `5dcd867c…f62a0`, same as v0.2).
* **Context (recorded before scoring, `context_description.json`):** a summer crop cycle. Median
  NDVI goes 0.23 (22 May) → 0.75 (31 Jul) → 0.67 (16 Sep). SCL is vegetation (4) and bare/built (5)
  only, with a small dark-area class (2) and ~1 % unclassified (7). Water (6) is ~0.
* **Targets:** 9 of 11 dates (22 May and 6 Jun have fewer than 2 earlier dates within 30 days).
  9 × 5 = 45 runs, 0 failures, nothing dropped or reseeded.

## Results (mean and [min, max] over the 5 mask layouts)

| Target | earlier ≤30 d | A coverage | A MAE | B MAE | joint MAE A−B | true NDVI SD (hidden) |
|---|---|---|---|---|---|---|
| 2026-06-16 | 2 | 0.9993 | 0.0848 [0.0826, 0.0892] | 0.1037 [0.1008, 0.1100] | -0.0188 [-0.0208, -0.0164] | 0.170 |
| 2026-06-26 | 2 | 0.9999 | 0.1215 [0.1177, 0.1284] | 0.1471 [0.1411, 0.1566] | -0.0256 [-0.0282, -0.0234] | 0.225 |
| 2026-07-06 | 3 | 0.9999 | 0.1138 [0.1062, 0.1189] | 0.1823 [0.1732, 0.1953] | -0.0685 [-0.0764, -0.0636] | 0.228 |
| 2026-07-16 | 3 | 0.9997 | 0.1384 [0.1344, 0.1437] | 0.2150 [0.2036, 0.2220] | -0.0766 [-0.0816, -0.0692] | 0.265 |
| 2026-07-31 | 2 | 0.9997 | 0.0920 [0.0874, 0.0988] | 0.1419 [0.1355, 0.1524] | -0.0500 [-0.0537, -0.0481] | 0.271 |
| 2026-08-10 | 2 | 0.9997 | 0.0362 [0.0350, 0.0369] | 0.0652 [0.0617, 0.0691] | -0.0290 [-0.0325, -0.0267] | 0.262 |
| 2026-08-27 | 2 | 0.9987 | 0.0483 [0.0467, 0.0495] | 0.0543 [0.0523, 0.0560] | -0.0060 [-0.0064, -0.0056] | 0.248 |
| 2026-09-06 | 2 | 0.9994 | 0.0468 [0.0452, 0.0484] | 0.0540 [0.0526, 0.0551] | -0.0072 [-0.0096, -0.0059] | 0.268 |
| 2026-09-16 | 2 | 0.9991 | 0.0587 [0.0557, 0.0623] | 0.0663 [0.0624, 0.0706] | -0.0076 [-0.0100, -0.0053] | 0.264 |

A and B predict on exactly the same pixels here (only-A = only-B = 0 in all 45 runs), so the
joint support equals each baseline's full support. In all 45 (target, seed) runs, A had the lower
MAE on the joint support and the block-bootstrap 95 % interval of A−B was below zero. Errors are
largest during green-up (late June to July), where both baselines under-predict (negative bias)
because they carry older, lower NDVI forward; B, which averages up to 3 older dates, lags more.

![per-target results](evaluation_per_target.png)

## Limitations

* One AOI, one tile, one season. The 9 targets share earlier dates and the same fields, so they are
  not independent; the 5 seeds are mask layouts on the same image, not new data. That is why there
  is no pooled number, no confidence interval across targets and no p-value.
* The bootstrap resamples the hidden blocks of one (target, seed). It describes how much the score
  depends on which blocks were hidden, nothing else: not other dates, places, years or sensors.
* The task hides synthetic blocks on SCL-usable pixels. It is not reconstruction under real clouds
  (cloud edges, haze and shadows behave differently), and SCL itself can be wrong.
* The surface is mixed (crops, bare and built land) and no land-cover mask is applied.
* Coverage is near 1 because consecutive dates are 10–17 days apart; longer gaps would cause more
  abstentions.
* Discovery used the bucket listing, not the STAC API (see above).

## Reproduce

```bash
deltawatch evaluate discover --item-source stac   # or bucket; re-freezes a manifest (new timestamp)
deltawatch run --config config/eval_new_aoi_2026.yaml \
  --manifest evaluations/new_aoi_2026/manifest.json --out evaluations/new_aoi_2026/run
deltawatch evaluate describe                       # validate_run + context, before any score
deltawatch evaluate score                          # 9 targets x 5 seeds, bootstrap, JSON/CSV/MD
python evaluations/new_aoi_2026/make_figure.py
```

To reuse the frozen manifest, skip `discover`. The GeoTIFFs under `run/` are git-ignored; the
small CSV/JSON files are tracked.

## Files

`manifest.json`, `manifest_freeze_record.json`, `screening_all_items.csv`, `context_description.json`,
`plan.json`, `evaluation_results.json` (everything, with bootstrap intervals), `evaluation_per_target_seed.csv`,
`evaluation_summary_per_target.csv`, `evaluation_summary.md`, `evaluation_per_target.png`,
`run/qa_per_date.csv`, `run/run_metadata.json`, `run/qa_ndvi_panel.png`.
