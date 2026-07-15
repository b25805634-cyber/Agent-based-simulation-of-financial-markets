"""Step-0 analysis: within-config drop spread -> pairs-per-level N for the sweep.

Reads the calibration reps (same m, same seed, social on/off, temp>0 resampled),
reports the drop distribution per arm, and sizes N for a paired test to detect
the 2x2 baseline contagion effect (|delta| ~ 0.18 drop) at alpha=.05, power=.80:
    N = ceil(((1.96 + 0.84) * sigma_d / delta)^2),  sigma_d = sqrt(s_on^2 + s_off^2)
Clamped to [8, 12] per the experiment plan. Writes N to <out>/calib_N.txt.
"""
from __future__ import annotations
import glob
import json
import math
import argparse
from statistics import mean, stdev

DELTA = 0.18          # 2x2 baseline ON-OFF drop effect, the size we must resolve
N_MIN, N_MAX = 8, 12


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results_sweep")
    ap.add_argument("--m", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    arms = {}
    for soc in ("on", "off"):
        files = sorted(glob.glob(
            f"{args.out}/m{args.m:g}_real_{soc}_s{args.seed}_r*.json"))
        drops = []
        for f in files:
            d = json.load(open(f))
            if d["health"]["bad_frac"] <= 0.15:
                drops.append(d["metrics"]["drop_depth"])
        arms[soc] = drops
        print(f"[calib] social {soc}: n={len(drops)} drops="
              f"{[round(x,3) for x in drops]}"
              + (f"  mean={mean(drops):+.3f} sd={stdev(drops):.3f}"
                 if len(drops) > 1 else ""))

    if len(arms["on"]) >= 3 and len(arms["off"]) >= 3:
        s_on, s_off = stdev(arms["on"]), stdev(arms["off"])
        sigma_d = math.sqrt(s_on ** 2 + s_off ** 2)
        n_raw = math.ceil(((1.96 + 0.84) * sigma_d / DELTA) ** 2)
        n = max(N_MIN, min(N_MAX, n_raw))
        print(f"[calib] sigma_d={sigma_d:.3f}  raw N={n_raw}  -> clamped N={n}")
        print(f"[calib] 30-agent dampening preview: ON {mean(arms['on']):+.3f} "
              f"vs OFF {mean(arms['off']):+.3f} (diff {mean(arms['on'])-mean(arms['off']):+.3f})")
    else:
        n = 10
        print(f"[calib] insufficient reps; defaulting N={n}")

    with open(f"{args.out}/calib_N.txt", "w") as fh:
        fh.write(str(n))
    print(f"[calib] wrote {args.out}/calib_N.txt = {n}")


if __name__ == "__main__":
    main()
