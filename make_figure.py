"""Figure for the new-AOI evaluation, drawn only from the saved outputs (no recomputation)."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).parent
A, B, INK, MUTED = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e"

ctx = json.loads((HERE / "context_description.json").read_text())["per_date"]
s = pd.read_csv(HERE / "evaluation_summary_per_target.csv")
dates = pd.to_datetime([r["date"] for r in ctx])
p50 = [r["ndvi_p5_p25_p50_p75_p95"][2] for r in ctx]
p25 = [r["ndvi_p5_p25_p50_p75_p95"][1] for r in ctx]
p75 = [r["ndvi_p5_p25_p50_p75_p95"][3] for r in ctx]
t = pd.to_datetime(s["target"])

plt.rcParams.update(
    {
        "font.size": 9,
        "axes.edgecolor": MUTED,
        "axes.labelcolor": INK,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.spines.top": False,
        "axes.spines.right": False,
    }
)
fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(8, 6), sharex=True, gridspec_kw={"height_ratios": [1, 1.4]})
fig.patch.set_facecolor("#fcfcfb")
ax0.fill_between(dates, p25, p75, color=MUTED, alpha=0.15, lw=0, label="IQR")
ax0.plot(dates, p50, color=MUTED, lw=2, marker="o", ms=4, label="median")
ax0.set_ylabel("NDVI of valid pixels")
ax0.set_title("Context: surface NDVI per frozen date (median and IQR)", loc="left", color=INK)
ax0.legend(frameon=False, loc="upper left")
for col, name, off in (("A", "A: latest", -1.2), ("B", "B: median3", 1.2)):
    x = t + pd.to_timedelta(off, "D")
    m, lo, hi = s[f"{col}_mae_mean"], s[f"{col}_mae_min"], s[f"{col}_mae_max"]
    ax1.errorbar(x, m, yerr=[m - lo, hi - m], fmt="o", ms=6, lw=2, capsize=0, color=A if col == "A" else B, label=name)
ax1.set_ylabel("MAE (NDVI units)")
ax1.set_ylim(0, None)
ax1.set_title("Hidden-block MAE per target: mean of 5 mask layouts, bar = min–max", loc="left", color=INK)
ax1.legend(frameon=False, loc="upper right")
ax1.grid(axis="y", color=MUTED, alpha=0.15)
ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
fig.text(
    0.01,
    0.005,
    "Tile 36RUU, AOI [31.60, 30.60, 31.65, 30.65], 2026. Synthetic hidden blocks on SCL-eligible "
    "pixels; seeds are mask layouts, not independent data.",
    fontsize=7,
    color=MUTED,
)
fig.tight_layout(rect=(0, 0.02, 1, 1))
fig.savefig(HERE / "evaluation_per_target.png", dpi=150, facecolor=fig.get_facecolor())
