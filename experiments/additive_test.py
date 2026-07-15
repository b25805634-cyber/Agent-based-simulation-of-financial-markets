"""Task 2: is the social channel an ADDITIVE stabilizer or a PROPORTIONAL one?

Two complementary regressions over the paired sweep data:

  (1) diff ~ |drop_off|      (the user's framing)
      additive    -> intercept > 0, slope ~ 0
      proportional-> intercept ~ 0, slope > 0 (= absorbed fraction k)
      CAVEAT: |drop_off| appears inside diff (= drop_on + |drop_off|), so
      seed-level noise in OFF mechanically inflates the slope. Read alongside (2).

  (2) drop_on ~ drop_off     (cleaner: OFF only on the x side)
      additive    -> slope ~ 1, intercept = +c > 0
      proportional-> slope = (1-k) < 1, intercept ~ 0

Plus the noise-robust group-level view (m-level means, 3 points).

Usage: python -m experiments.additive_test [--out results_sweep]
"""
from __future__ import annotations
import os
import re
import glob
import json
import argparse
from statistics import mean

T975 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31,
        9: 2.26, 10: 2.23, 12: 2.18, 14: 2.14, 16: 2.12, 18: 2.10, 20: 2.09,
        22: 2.07, 25: 2.06, 30: 2.04, 40: 2.02}


def tcrit(df):
    return T975[min(T975, key=lambda k: abs(k - df))]


def ols(xs, ys):
    n = len(xs)
    xb, yb = mean(xs), mean(ys)
    sxx = sum((x - xb) ** 2 for x in xs)
    b = sum((x - xb) * (y - yb) for x, y in zip(xs, ys)) / sxx
    a = yb - b * xb
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    s2 = sum(e * e for e in resid) / (n - 2)
    se_b = (s2 / sxx) ** 0.5
    se_a = (s2 * (1 / n + xb * xb / sxx)) ** 0.5
    t = tcrit(n - 2)
    return a, b, (a - t * se_a, a + t * se_a), (b - t * se_b, b + t * se_b), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results_sweep")
    args = ap.parse_args()
    pat = re.compile(r"m([\d.]+)_real_(on|off)_s(\d+)\.json$")
    by_key = {}
    for f in glob.glob(os.path.join(args.out, "*.json")):
        g = pat.search(os.path.basename(f))
        if not g:
            continue
        d = json.load(open(f))
        if d["health"]["bad_frac"] > 0.15:
            continue
        by_key.setdefault((float(g.group(1)), int(g.group(3))), {})[g.group(2)] = \
            d["metrics"]["drop_depth"]

    pairs = [(m, s, v["on"], v["off"]) for (m, s), v in sorted(by_key.items())
             if "on" in v and "off" in v]
    print(f"配对总数: {len(pairs)}  (按 m: " + ", ".join(
        f"{m:g}:{sum(1 for p in pairs if p[0]==m)}" for m in sorted({p[0] for p in pairs})) + ")")

    xs1 = [abs(off) for _, _, on, off in pairs]
    ys1 = [on - off for _, _, on, off in pairs]
    a, b, ca, cb, n = ols(xs1, ys1)
    print(f"\n(1) diff ~ |drop_off|   [用户口径; 注意耦合偏置]")
    print(f"    截距 a = {a:+.4f}  95%CI [{ca[0]:+.4f}, {ca[1]:+.4f}]"
          f"  {'>0 ✅' if ca[0] > 0 else '含0'}")
    print(f"    斜率 b = {b:+.4f}  95%CI [{cb[0]:+.4f}, {cb[1]:+.4f}]"
          f"  {'>0 ✅' if cb[0] > 0 else ('<0' if cb[1] < 0 else '含0')}")

    xs2 = [off for _, _, on, off in pairs]
    ys2 = [on for _, _, on, off in pairs]
    a2, b2, ca2, cb2, _ = ols(xs2, ys2)
    print(f"\n(2) drop_on ~ drop_off  [更干净的口径]")
    print(f"    截距 a = {a2:+.4f}  95%CI [{ca2[0]:+.4f}, {ca2[1]:+.4f}]"
          f"  (纯加性预测 a>0; 纯比例预测 a≈0)")
    print(f"    斜率 b = {b2:+.4f}  95%CI [{cb2[0]:+.4f}, {cb2[1]:+.4f}]"
          f"  (纯加性预测 b≈1; 纯比例预测 b=1-k<1)")
    if cb2[1] < 1 and ca2[0] > 0:
        verdict = "混合: 既有加性成分(截距>0) 也有比例成分(斜率显著<1)"
    elif cb2[1] < 1:
        verdict = "偏比例: 斜率显著<1, 截距不显著"
    elif ca2[0] > 0:
        verdict = "偏加性: 截距显著>0, 斜率与1无差异"
    else:
        verdict = "无法区分(CI都太宽)"
    print(f"    -> 判定: {verdict}")
    if 0 < b2 < 1:
        print(f"    隐含吸收比例 k = 1 - b = {1-b2:.2f}"
              f"  (每多跌1单位, 社交垫掉 {1-b2:.0%})")

    print("\n(3) 组水平(m 档均值, 抗噪)")
    for m in sorted({p[0] for p in pairs}):
        sub = [p for p in pairs if p[0] == m]
        print(f"    m={m:g}: |OFF|均值={mean(abs(p[3]) for p in sub):.3f}"
              f"  diff均值={mean(p[2]-p[3] for p in sub):+.3f}  (N={len(sub)})")


if __name__ == "__main__":
    main()
