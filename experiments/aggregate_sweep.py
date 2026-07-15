"""Analyze the population-composition sweep.

Per m-level: PAIRED (same-seed) drop difference, social ON - OFF. Drops are
negative, so diff > 0 means ON is SHALLOWER = social channel dampens; diff < 0
means social amplifies. Main figure: x = m (fuel ratio), y = mean paired diff
with a 95% t-CI -- where does it cross 0?

Also reports the mediator check (fuel mean post-news sentiment vs drop) for
runs that carry the instrumentation, and honest realized-N per level.

Usage: python -m experiments.aggregate_sweep --out results_sweep
"""
from __future__ import annotations
import os
import re
import glob
import json
import argparse
from statistics import mean, stdev

# two-sided 95% t critical values by df
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179}

FNAME = re.compile(r"m(?P<m>[\d.]+)_real_(?P<soc>on|off)_s(?P<seed>\d+)"
                   r"(?:_r(?P<rep>\d+))?\.json$")


def load(out_dir):
    """-> {m: {seed: {'on': metrics, 'off': metrics}}} (sweep runs only, clean only)."""
    data = {}
    for f in glob.glob(os.path.join(out_dir, "*.json")):
        g = FNAME.search(os.path.basename(f))
        if not g or g.group("rep"):           # skip calibration reps
            continue
        d = json.load(open(f))
        if d["health"]["bad_frac"] > 0.15:
            continue
        m, soc, seed = float(g.group("m")), g.group("soc"), int(g.group("seed"))
        data.setdefault(m, {}).setdefault(seed, {})[soc] = d["metrics"]
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results_sweep")
    ap.add_argument("--plot", default=None, help="output PNG (default <out>/sweep_main.png)")
    args = ap.parse_args()
    data = load(args.out)

    rows = []
    print("===== POPULATION-COMPOSITION SWEEP =====")
    print("paired diff = drop(ON) - drop(OFF);  >0 = social DAMPENS, <0 = AMPLIFIES\n")
    for m in sorted(data):
        seeds = data[m]
        pairs = {s: v for s, v in seeds.items() if "on" in v and "off" in v}
        diffs = [v["on"]["drop_depth"] - v["off"]["drop_depth"] for v in pairs.values()]
        n = len(diffs)
        line = f"m={m:g}: 完整配对 {n}"
        if n >= 2:
            mu, sd = mean(diffs), stdev(diffs)
            half = T975.get(n - 1, 1.96) * sd / (n ** 0.5)
            lo, hi = mu - half, mu + half
            verdict = ("减震 (CI>0)" if lo > 0 else
                       "放大 (CI<0)" if hi < 0 else "跨 0 (不显著)")
            line += (f"  diff={mu:+.3f} ±{half:.3f} (95%CI [{lo:+.3f},{hi:+.3f}])"
                     f"  -> {verdict}")
            rows.append((m, mu, half, n))
        elif n == 1:
            mu = diffs[0]
            line += f"  diff={mu:+.3f} (单对, 无CI)"
            rows.append((m, mu, None, n))
        print(line)
        # honest per-seed detail
        for s in sorted(pairs):
            v = pairs[s]
            print(f"    seed {s}: on {v['on']['drop_depth']:+.3f} / "
                  f"off {v['off']['drop_depth']:+.3f}  diff "
                  f"{v['on']['drop_depth']-v['off']['drop_depth']:+.3f}")

    # ---- mediator: fuel panic vs drop (instrumented runs only) ----
    print("\n--- 中介检验: 燃料盘平均恐慌 (post-news) vs 跌幅 ---")
    pts = []
    for m, seeds in sorted(data.items()):
        for s, v in seeds.items():
            for soc in ("on", "off"):
                mt = v.get(soc)
                if mt and mt.get("fuel_sent_postnews") is not None:
                    pts.append((m, soc, mt["fuel_sent_postnews"], mt["drop_depth"]))
    if pts:
        for m in sorted({p[0] for p in pts}):
            sub = [p for p in pts if p[0] == m]
            fs = [p[2] for p in sub]; dr = [p[3] for p in sub]
            print(f"  m={m:g}: n={len(sub)}  fuel_sent均值={mean(fs):+.3f}  drop均值={mean(dr):+.3f}")
        if len(pts) >= 3:
            fs = [p[2] for p in pts]; dr = [p[3] for p in pts]
            mf, md = mean(fs), mean(dr)
            cov = sum((a - mf) * (b - md) for a, b in zip(fs, dr))
            vf = sum((a - mf) ** 2 for a in fs); vd = sum((b - md) ** 2 for b in dr)
            r = cov / (vf * vd) ** 0.5 if vf > 0 and vd > 0 else float("nan")
            print(f"  全部 n={len(pts)}: corr(燃料恐慌, 跌幅) = {r:+.3f} "
                  f"(预期为正: 恐慌越深(越负) 跌幅越深(越负))")
    else:
        print("  (现有 run 未带 fuel_sent 字段 — 该度量是后补的, 续跑数据会有)")

    # ---- main figure ----
    if rows:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8.5, 5.5))
        xs = [r[0] for r in rows]; ys = [r[1] for r in rows]
        errs = [r[2] if r[2] is not None else 0 for r in rows]
        ax.errorbar(xs, ys, yerr=errs, fmt="o-", lw=2, ms=8, capsize=6,
                    color="#C0392B", ecolor="#C0392B")
        for m, mu, half, n in rows:
            ax.annotate(f"N={n}", (m, mu), textcoords="offset points",
                        xytext=(10, 8), fontsize=9)
        ax.axhline(0, color="#555", lw=1.2, ls="--")
        ax.set_xlabel("fuel ratio m  (retail+fomo) / rest")
        ax.set_ylabel("paired drop diff: ON − OFF   (>0 = dampening)")
        ax.set_title("Where does social dampening cross zero?\n"
                     "(paired same-seed, 95% t-CI, real Meta news, 30 agents)")
        ax.grid(alpha=0.3)
        out_png = args.plot or os.path.join(args.out, "sweep_main.png")
        plt.tight_layout(); plt.savefig(out_png, dpi=135); plt.close()
        print(f"\nwrote {out_png}")


if __name__ == "__main__":
    main()
