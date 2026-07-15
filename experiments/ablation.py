"""Analyzer for the influencer-megaphone ablation (m=0.7, real news).

Five arms share one population and same-seed pairing:
  on      = full social, influencer has the mic   (baseline dampener)
  off     = social closed                          (reference crash)
  muted   = social on, influencer's take reaches NO ONE (+told it's unheard)
  solo    = ONLY influencer's take circulates       (+told it has the mic)
  demoted = influencer is an ordinary node (no hub, no auto-seed)

Two outputs (exactly what was asked for):
  (1) four/five-arm PAIRED drop diffs vs OFF (same seed), mean ± 95% t-CI.
      diff = drop(arm) - drop(off);  drop_depth = trough/pre - 1 (negative = crash),
      so diff > 0 means the arm DAMPENS relative to no-social. Also diff vs ON,
      to see whether an arm restores the baseline dampening.
  (2) the influencer's OWN post-news sell volume per arm — the mechanism test:
      mic present -> influencer sells less (broadcasts instead);
      mic removed -> it dumps.

Usage:  python -m experiments.ablation --out results_sweep --m 0.7
"""
from __future__ import annotations
import os
import re
import glob
import json
import math
import argparse
from statistics import mean, stdev

ARMS = ["off", "on", "muted", "solo", "demoted"]
INFLUENCER = "influencer_amplifier"


def _fname_re(m):
    arms = "|".join(ARMS)
    return re.compile(rf"m{re.escape(f'{m:g}')}_real_(?P<arm>{arms})_s(?P<seed>\d+)\.json$")


def load(out_dir, m):
    """-> {arm: {seed: record}} for clean runs only."""
    rx = _fname_re(m)
    data = {}
    for f in glob.glob(os.path.join(out_dir, "*.json")):
        g = rx.search(os.path.basename(f))
        if not g:
            continue
        d = json.load(open(f))
        if d.get("health", {}).get("bad_frac", 1.0) > 0.15:
            continue
        data.setdefault(g.group("arm"), {})[int(g.group("seed"))] = d
    return data


def _t95(n):
    # two-sided 95% t critical values (df = n-1); fall back to z for large n.
    tbl = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
           7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
           13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
           19: 2.093, 20: 2.086}
    return tbl.get(n - 1, 1.96)


def _ci(xs):
    n = len(xs)
    if n == 0:
        return (None, None, None, 0)
    mu = mean(xs)
    if n == 1:
        return (mu, None, None, 1)
    sd = stdev(xs)
    half = _t95(n) * sd / math.sqrt(n)
    return (mu, mu - half, mu + half, n)


def _drop(rec):
    return rec["metrics"]["drop_depth"]


def _inf_postnews_sell(rec):
    """Total shares the influencer SOLD after the news round (qty summed)."""
    nr = rec.get("news_round", 12)
    tot = 0
    for row in rec.get("orders", []):
        r, a, s, q = row[0], row[1], row[2], row[3]
        if a == INFLUENCER and s == "sell" and r >= nr:
            tot += int(q)
    return tot


def _paired(data, arm, ref):
    """Same-seed (drop(arm) - drop(ref)) list over seeds present in both."""
    a, b = data.get(arm, {}), data.get(ref, {})
    return [(_drop(a[s]) - _drop(b[s])) for s in sorted(set(a) & set(b))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results_sweep")
    ap.add_argument("--m", type=float, default=0.7)
    args = ap.parse_args()

    data = load(args.out, args.m)
    present = [arm for arm in ARMS if arm in data]
    print(f"===== INFLUENCER-MEGAPHONE ABLATION  (m={args.m:g}, real news) =====")
    print("arms present:", ", ".join(f"{a}(n={len(data[a])})" for a in present), "\n")
    if "off" not in data:
        print("WARNING: no OFF arm in this dir — diffs-vs-OFF will be empty.\n")

    # ---- (1) paired drop diffs ----
    print("--- (1) PAIRED drop diffs  [drop = trough/pre - 1; diff>0 => DAMPENS] ---")
    print(f"{'arm':9s} {'n':>3s}  {'mean drop':>10s}   {'diff vs OFF (95% CI)':>30s}   {'diff vs ON (95% CI)':>28s}")
    for arm in present:
        drops = [_drop(r) for r in data[arm].values()]
        md = mean(drops) if drops else float('nan')
        do = _paired(data, arm, "off")
        dn = _paired(data, arm, "on")
        mo, lo, hi, no = _ci(do)
        mn, lnv, hnv, nn = _ci(dn)
        so = f"{mo:+.3f} [{lo:+.3f},{hi:+.3f}] n={no}" if no >= 2 else (f"{mo:+.3f} n={no}" if no == 1 else "—")
        sn = f"{mn:+.3f} [{lnv:+.3f},{hnv:+.3f}] n={nn}" if nn >= 2 else (f"{mn:+.3f} n={nn}" if nn == 1 else "—")
        print(f"{arm:9s} {len(drops):3d}  {md:+10.3f}   {so:>30s}   {sn:>28s}")

    # per-seed table for the two decisive arms
    for arm in ("muted", "solo", "demoted"):
        if arm not in data:
            continue
        seeds = sorted(set(data[arm]) & set(data.get("off", {})) & set(data.get("on", {})))
        if not seeds:
            continue
        print(f"\n  per-seed  off / on / {arm}   (drop_depth):")
        for s in seeds:
            do, dn, da = _drop(data['off'][s]), _drop(data['on'][s]), _drop(data[arm][s])
            print(f"    seed {s:2d}: off {do:+.3f}  on {dn:+.3f}  {arm} {da:+.3f}"
                  f"   | {arm}-off {da-do:+.3f}  {arm}-on {da-dn:+.3f}")

    # ---- (2) influencer's own post-news sell volume ----
    print("\n--- (2) INFLUENCER own post-news SELL volume (shares) ---")
    print("    prediction: mic present (on/solo) -> sells LESS;  mic gone (muted) -> DUMPS\n")
    print(f"{'arm':9s} {'n':>3s}  {'mean sell':>10s}  {'sd':>8s}   per-seed")
    for arm in present:
        vals = {s: _inf_postnews_sell(r) for s, r in sorted(data[arm].items())}
        xs = list(vals.values())
        mu = mean(xs) if xs else float('nan')
        sd = stdev(xs) if len(xs) > 1 else 0.0
        seedstr = " ".join(f"s{s}:{v}" for s, v in vals.items())
        print(f"{arm:9s} {len(xs):3d}  {mu:10.1f}  {sd:8.1f}   {seedstr}")

    print("\n(diff vs OFF ~0 => arm lost the dampening; ~+ON => arm restored it.)")


if __name__ == "__main__":
    main()
