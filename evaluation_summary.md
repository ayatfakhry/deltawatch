# New-AOI 2026 evaluation: per-target summary

Synthetic hidden-block task on all SCL-eligible surface (mixed surface). Values are the mean and [min, max] across 5 mask layouts (seeds 301-305) on the same image; seeds are not independent datasets and targets are temporally dependent, so no pooled CI or p-values are reported.

| Target | earlier ≤30 d | A coverage | A MAE mean [min, max] | B MAE mean [min, max] | joint MAE A−B mean [min, max] | true NDVI SD (hidden) |
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

Targets evaluated: 9 of 9; failures: 0.
Per-seed values with block-bootstrap 95 % intervals are in evaluation_per_target_seed.csv / evaluation_results.json.
