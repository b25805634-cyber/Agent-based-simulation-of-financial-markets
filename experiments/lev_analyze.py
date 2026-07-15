"""Analyzer for the leverage 2x2: {social on/off} x {leverage on/off}, m fixed.

Cells (filenames in --out):
  soc_off_levoff = m{m}_real_off          soc_on_levoff = m{m}_real_on
  soc_off_levon  = m{m}_real_off_lev      soc_on_levon  = m{m}_real_on_lev

Outputs:
  (1) four-cell mean drop + same-seed paired contrasts:
        leverage effect   = drop(lev_on) - drop(lev_off)   per social arm  (<0 = leverage AMPLIFIES the crash)
        social effect     = drop(soc_on) - drop(soc_off)    per leverage arm (>0 = social DAMPENS)
        => does leverage unlock self-reinforcing deep crashes? does the social
           dampener still win once the leverage amplifier is switched on?
  (2) forced-liquidation scale per cell (events + shares) as mechanism evidence.
  (3) a 2x2 figure (mean drop per cell + paired effects).

Usage:  python -m experiments.lev_analyze --out results_sweep --m 0.7
"""
from __future__ import annotations
import os
import re
import glob
import json
import math
import argparse
from statistics import mean, stdev

CELLS = {                       # cell key -> filename label
    "soc_off_levoff": "off",
    "soc_on_levoff":  "on",
    "soc_off_levon":  "off_lev",
    "soc_on_levon":   "on_lev",
}
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
        15: 2.131, 16: 2.120}


def _ci(xs):
    n = len(xs)
    if n == 0:
        return None
    mu = mean(xs)
    if n == 1:
        return (mu, None, None, 1)
    h = _T95.get(n - 1, 1.96) * stdev(xs) / math.sqrt(n)
    return (mu, mu - h, mu + h, n)


def load(out_dir, m):
    """-> {cell_key: {seed: record}} for clean runs only."""
    data = {k: {} for k in CELLS}
    for key, lab in CELLS.items():
        rx = re.compile(rf"m{re.escape(f'{m:g}')}_real_{re.escape(lab)}_s(\d+)\.json$")
        for f in glob.glob(os.path.join(out_dir, "*.json")):
            g = rx.search(os.path.basename(f))
            if not g:
                continue
            d = json.load(open(f))
            if d.get("health", {}).get("bad_frac", 1.0) > 0.15:
                continue
            data[key][int(g.group(1))] = d
    return data


def _drop(r):
    return r["metrics"]["drop_depth"]


def _liq(r):
    evs = r.get("liquidations", [])
    return len(evs), sum(e[2] for e in evs)        # (#events, shares)


def _liq_detail(r):
    """-> (#events, shares, #pre_news_events, breach_price_min, breach_price_max).
    Event shape: [round, agent, shares, price, threshold, pnl]."""
    evs = r.get("liquidations", [])
    nr = r.get("news_round", 12)
    pre = sum(1 for e in evs if e[0] < nr)
    prices = [e[3] for e in evs]
    return (len(evs), sum(e[2] for e in evs), pre,
            (min(prices) if prices else None), (max(prices) if prices else None))


def _paired(a, b):
    return [_drop(a[s]) - _drop(b[s]) for s in sorted(set(a) & set(b))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results_sweep")
    ap.add_argument("--m", type=float, default=0.7)
    ap.add_argument("--plot", default=None)
    args = ap.parse_args()
    data = load(args.out, args.m)

    print(f"===== LEVERAGE 2x2  (m={args.m:g}, real news) =====")
    print("cells:", ", ".join(f"{k}(n={len(data[k])})" for k in CELLS), "\n")

    # ---- (1) four-cell mean drops ----
    print("--- (1) mean drop per cell  [drop = trough/pre - 1] ---")
    print(f"{'':14s}  {'lev OFF':>14s}   {'lev ON':>14s}")
    for soc in ("off", "on"):
        off = [_drop(r) for r in data[f"soc_{soc}_levoff"].values()]
        on = [_drop(r) for r in data[f"soc_{soc}_levon"].values()]
        mo = f"{mean(off):+.3f} (n={len(off)})" if off else "—"
        mn = f"{mean(on):+.3f} (n={len(on)})" if on else "—"
        print(f"social {soc:<3s}    {mo:>14s}   {mn:>14s}")

    # ---- paired effects ----
    print("\n--- paired contrasts (same seed, 95% CI) ---")
    print("LEVERAGE effect = drop(lev_on) - drop(lev_off)   (<0 => leverage AMPLIFIES)")
    for soc in ("off", "on"):
        d = _paired(data[f"soc_{soc}_levon"], data[f"soc_{soc}_levoff"])
        c = _ci(d)
        s = (f"{c[0]:+.3f} [{c[1]:+.3f},{c[2]:+.3f}] n={c[3]}" if c and c[3] >= 2
             else (f"{c[0]:+.3f} n=1" if c else "—"))
        print(f"  social {soc:<3s}: {s}")
    print("SOCIAL effect = drop(soc_on) - drop(soc_off)     (>0 => social DAMPENS)")
    for lev in ("levoff", "levon"):
        d = _paired(data[f"soc_on_{lev}"], data[f"soc_off_{lev}"])
        c = _ci(d)
        s = (f"{c[0]:+.3f} [{c[1]:+.3f},{c[2]:+.3f}] n={c[3]}" if c and c[3] >= 2
             else (f"{c[0]:+.3f} n=1" if c else "—"))
        tag = "leverage OFF" if lev == "levoff" else "leverage ON "
        print(f"  {tag}: {s}")

    # ---- (2) liquidation scale + price-driven check (M4) ----
    print("\n--- (2) forced-liquidation scale (lev-ON cells) ---")
    print(f"{'cell':16s} {'runs':>5s} {'mean #ev':>9s} {'mean sh':>9s} "
          f"{'breach px range':>16s} {'pre-news ev':>11s}")
    warn = []
    for key in ("soc_off_levon", "soc_on_levon"):
        recs = list(data[key].values())
        if not recs:
            print(f"{key:16s} {0:5d}"); continue
        det = [_liq_detail(r) for r in recs]
        ev = mean(d[0] for d in det); sh = mean(d[1] for d in det)
        prenews = sum(d[2] for d in det)
        lows = [d[3] for d in det if d[3] is not None]
        highs = [d[4] for d in det if d[4] is not None]
        rng = f"{min(lows):.1f}–{max(highs):.1f}" if lows else "—"
        print(f"{key:16s} {len(recs):5d} {ev:9.1f} {sh:9.1f} {rng:>16s} {prenews:11d}")
        if prenews:
            warn.append(f"{key}: {prenews} PRE-NEWS liquidation events — drop_depth "
                        f"denominator may be rebased; inspect (M4).")
        if highs and max(highs) > 100:
            warn.append(f"{key}: a breach price >100 — should be impossible under "
                        f"price-only margin; investigate.")
    print("\n  price-only margin => all breach prices are <100 and driven by the "
          "price path, not by selling (the V2 redesign).")

    # ---- model provenance (M7) ----
    lev_models = set()
    for soc in ("off", "on"):
        for r in data[f"soc_{soc}_levon"].values():
            if r.get("model"):
                lev_models.add(r["model"])
    base_has_model = any(r.get("model") for soc in ("off", "on")
                         for r in data[f"soc_{soc}_levoff"].values())
    if len(lev_models) > 1:
        warn.append(f"lev-ON runs span MULTIPLE models {sorted(lev_models)} — the "
                    f"endpoint swapped mid-run; pairing invalid.")
    elif lev_models:
        print(f"\n  provenance: lev-ON runs served by {sorted(lev_models)[0]}.")
    if not base_has_model:
        print("  provenance NOTE: lev-OFF baseline JSONs predate model-id logging "
              "(no 'model' key) — assumed MiniMax-M2.7 per project records; cross-"
              "model swap vs baseline cannot be auto-verified, only the lev-ON set is.")
    # per-seed cross-check where BOTH sides recorded a model
    for soc in ("off", "on"):
        on, off = data[f"soc_{soc}_levon"], data[f"soc_{soc}_levoff"]
        for s in sorted(set(on) & set(off)):
            mon, moff = on[s].get("model"), off[s].get("model")
            if mon and moff and mon != moff:
                warn.append(f"MODEL MISMATCH seed {s} social {soc}: lev_on={mon} "
                            f"vs lev_off={moff} — pairing invalid.")
    if warn:
        print("\n  ⚠ WARNINGS:")
        for w in warn:
            print(f"    - {w}")

    # ---- (3) figure ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cats = ["soc_off\nlevoff", "soc_on\nlevoff", "soc_off\nlevon", "soc_on\nlevon"]
        keys = ["soc_off_levoff", "soc_on_levoff", "soc_off_levon", "soc_on_levon"]
        # restrict to the seed set present in ALL four cells so the headline 2x2
        # is a clean paired comparison (not mixed-N across cells).
        common = set.intersection(*[set(data[k]) for k in keys]) if all(data[k] for k in keys) else set()
        means = [mean([_drop(data[k][s]) for s in common]) if common else 0 for k in keys]
        cols = ["#888", "#2a7", "#c33", "#e80"]
        fig, ax = plt.subplots(figsize=(7, 5))
        ax.bar(cats, means, color=cols, alpha=.85)
        ax.axhline(0, color="k", lw=.8)
        ax.set_ylabel("mean drop (trough/pre - 1)")
        ax.set_title(f"Leverage 2x2 — mean crash depth (m={args.m:g})\n"
                     "stabiliser (social) vs amplifier (leverage)")
        for i, v in enumerate(means):
            ax.text(i, v - 0.01, f"{v:+.3f}", ha="center", va="top", fontsize=9)
        out = args.plot or os.path.join(args.out, f"leverage_2x2_m{args.m:g}.png")
        plt.tight_layout(); plt.savefig(out, dpi=130)
        print(f"\nfigure -> {out}")
    except Exception as e:
        print(f"\nplot skipped: {e}")


if __name__ == "__main__":
    main()
