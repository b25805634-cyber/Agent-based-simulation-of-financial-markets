"""Analyze the 2x2 contagion grid: per-cell distributions, the contagion EFFECT
SIZE (ON-OFF) vs the within-cell seed band, the news effect, and a 4-cell price
envelope overlay vs real Meta.

Answers: does turning the SOCIAL channel on (everything else held fixed) change
the price dynamics by MORE than the seed-to-seed scatter? If yes -> contagion is
a real effect. If the ON-OFF gap is inside the band -> it is not.

Usage: python -m experiments.aggregate_grid --results results_2x2 [--out results_2x2]
"""
from __future__ import annotations
import os
import glob
import json
import math
import argparse
from statistics import mean, pstdev, stdev

from nmsim import validation as V

META = "nmsim/meta_feb2022_reference.csv"
CELLS = ["real_on", "real_off", "plac_on", "plac_off"]
CELL_STYLE = {
    "real_on":  ("#C0392B", "real news · social ON"),
    "real_off": ("#E59866", "real news · social OFF"),
    "plac_on":  ("#2C6FBB", "placebo · social ON"),
    "plac_off": ("#7F8C8D", "placebo · social OFF"),
}
METRICS = ["drop_depth", "recovery", "peak_cascade", "rmse_logprice", "dtw_norm"]


def _load(results_dir, label):
    runs = []
    for path in sorted(glob.glob(os.path.join(results_dir, f"{label}_s*.json"))):
        with open(path) as fh:
            runs.append(json.load(fh))
    return runs


def _clean(xs):
    return [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]


def _stat(xs):
    xs = _clean(xs)
    if not xs:
        return {"mean": None, "std": 0.0, "n": 0}
    return {"mean": mean(xs), "std": pstdev(xs) if len(xs) > 1 else 0.0, "n": len(xs)}


def _cohen_d(a, b):
    """Standardized mean difference (mean_a - mean_b) / pooled_sd.

    Uses the n-weighted pooled SAMPLE sd. Returns d=None (not a fabricated huge
    number) when the pooled sd is ~0 — a constant metric (e.g. peak_cascade all 0)
    with different means has an UNDEFINED standardized effect, not a 'VERY LARGE' one.
    """
    a, b = _clean(a), _clean(b)            # drop None AND NaN
    if len(a) < 1 or len(b) < 1:
        return None, None
    ma, mb = mean(a), mean(b)
    na, nb = len(a), len(b)
    va = stdev(a) ** 2 if na > 1 else 0.0  # sample variance (n-1)
    vb = stdev(b) ** 2 if nb > 1 else 0.0
    denom = (na - 1) + (nb - 1)
    pooled = math.sqrt(((na - 1) * va + (nb - 1) * vb) / denom) if denom > 0 else 0.0
    if pooled < 1e-9:
        return round(ma - mb, 4), None     # undefined, not 1e8
    return round(ma - mb, 4), round((ma - mb) / pooled, 3)


def _d_label(d):
    if d is None:
        return "n/a (no variance)"
    a = abs(d)
    return ("negligible (<0.2)" if a < 0.2 else "small" if a < 0.5 else
            "medium" if a < 0.8 else "large" if a < 1.5 else "VERY LARGE (>1.5)")


def _meta_norm_price(L):
    ref, shock = V.load_reference(META)
    base = ref[shock]
    return [ref[shock + t] / base for t in range(min(L, len(ref) - shock))]


def aggregate(results_dir, out_dir):
    cells = {lbl: _load(results_dir, lbl) for lbl in CELLS}
    # per-cell metric distributions
    dist = {lbl: {m: _stat([r["metrics"][m] for r in runs]) for m in METRICS}
            for lbl, runs in cells.items()}
    realized_n = {lbl: len(runs) for lbl, runs in cells.items()}

    # key contrasts: contagion effect (ON-OFF) and news effect (real-placebo)
    def vals(lbl, m):
        return [r["metrics"][m] for r in cells[lbl]]

    contrasts = {}
    for m in METRICS:
        d_real, dd_real = _cohen_d(vals("real_on", m), vals("real_off", m))
        d_plac, dd_plac = _cohen_d(vals("plac_on", m), vals("plac_off", m))
        n_off, ndd_off = _cohen_d(vals("real_off", m), vals("plac_off", m))
        n_on, ndd_on = _cohen_d(vals("real_on", m), vals("plac_on", m))
        contrasts[m] = {
            "contagion_effect_realnews": {"delta": d_real, "cohen_d": dd_real, "size": _d_label(dd_real)},
            "contagion_effect_placebo":  {"delta": d_plac, "cohen_d": dd_plac, "size": _d_label(dd_plac)},
            "news_effect_socialoff":     {"delta": n_off, "cohen_d": ndd_off, "size": _d_label(ndd_off)},
            "news_effect_socialon":      {"delta": n_on, "cohen_d": ndd_on, "size": _d_label(ndd_on)},
        }

    summary = {"results_dir": results_dir, "realized_n": realized_n,
               "distributions": dist, "contrasts": contrasts}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "grid_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)

    _plot(cells, out_dir)
    _print(summary)
    print(f"\nwrote {out_dir}/grid_summary.json and envelope_2x2.png")
    return summary


def _band(runs):
    paths = [r["norm_price_path"] for r in runs if r.get("norm_price_path")]
    if not paths:
        return [], [], [], 0
    L = min(len(p) for p in paths)
    cols = [[p[t] for p in paths] for t in range(L)]
    return ([mean(c) for c in cols],
            [pstdev(c) if len(c) > 1 else 0.0 for c in cols], paths, L)


def _plot(cells, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 6.5))
    Lmin = 999
    for lbl in CELLS:
        if cells[lbl]:
            m, s, paths, L = _band(cells[lbl])
            Lmin = min(Lmin, L)
    for lbl in CELLS:
        runs = cells[lbl]
        if not runs:
            continue
        color, legend = CELL_STYLE[lbl]
        m, s, paths, L = _band(runs)
        L = min(L, Lmin)
        xs = list(range(L))
        ax.fill_between(xs, [m[t] - s[t] for t in xs], [m[t] + s[t] for t in xs],
                        color=color, alpha=0.13)
        ax.plot(xs, m[:L], color=color, lw=2.2, marker="o", ms=3,
                label=f"{legend}  (N={len(runs)})")
    # real Meta overlay
    meta = _meta_norm_price(Lmin)
    ax.plot(range(len(meta)), meta, color="#111", lw=2.4, ls="--", marker="s", ms=4,
            label="REAL Meta Feb-2022")
    ax.axhline(1.0, color="#999", lw=0.8)
    ax.axvline(0, color="#ccc", ls=":", lw=1)
    ax.set_title("2x2 contagion grid: price trajectory (mean ±1 std) per cell vs real Meta\n"
                 "does turning the SOCIAL channel ON deepen the crash / kill the rebound?")
    ax.set_xlabel("rounds / trading days since shock (t=0)")
    ax.set_ylabel("price / pre-shock price")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower left", fontsize=9)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "envelope_2x2.png"), dpi=135)
    plt.close(fig)


def _print(s):
    print("\n===== 2x2 CONTAGION GRID =====")
    print("realized N per cell:", s["realized_n"])
    print("\n-- per-cell distributions (mean ± std) --")
    hdr = "cell".ljust(10) + "".join(m[:9].rjust(11) for m in METRICS)
    print(hdr)
    for lbl in CELLS:
        d = s["distributions"][lbl]
        row = lbl.ljust(10)
        for m in METRICS:
            st = d[m]
            row += (f"{st['mean']:+.3f}±{st['std']:.2f}" if st["mean"] is not None else "  --  ").rjust(11)
        print(row)
    print("\n-- KEY CONTRASTS (delta + Cohen's d; |d|>0.8 = effect exceeds the seed band) --")
    for m in ["drop_depth", "recovery", "peak_cascade", "rmse_logprice"]:
        c = s["contrasts"][m]
        ce = c["contagion_effect_realnews"]
        ne = c["news_effect_socialoff"]
        print(f"  {m}:")
        print(f"     contagion (real_on - real_off): Δ={ce['delta']}  d={ce['cohen_d']}  [{ce['size']}]")
        print(f"     news (real_off - plac_off):     Δ={ne['delta']}  d={ne['cohen_d']}  [{ne['size']}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results_2x2")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    aggregate(args.results, args.out or args.results)


if __name__ == "__main__":
    main()
