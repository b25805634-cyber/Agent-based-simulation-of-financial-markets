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
import sys
from pathlib import Path
from statistics import mean, stdev

from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.run_context import ManagedRunContext
from nmsim.result_reuse import inspect_legacy_analysis_inputs

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


def analyze(input_dir, out_png):
    data = load(input_dir)

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
        plt.tight_layout(); plt.savefig(out_png, dpi=135); plt.close()
        print(f"\nwrote {out_png}")


def _input_paths(out_dir):
    paths = []
    for path in sorted(Path(out_dir).glob("*.json")):
        match = FNAME.search(path.name)
        if match and not match.group("rep"):
            paths.append(path)
    return paths


def build_argparser():
    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--out", default="results_sweep")
    ap.add_argument("--plot", default=None, help="output PNG (default <out>/sweep_main.png)")
    ap.add_argument("--run-id", default=None)
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_sweep",
            command_identity="python -m experiments.aggregate_sweep",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        args = build_argparser().parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")
    input_dir = Path(args.out)
    legacy_plot = Path(args.plot) if args.plot else input_dir / "sweep_main.png"
    out_root = legacy_plot.parent
    inputs = _input_paths(input_dir)
    managed = ManagedRunContext.create_driver(
        out_root=out_root,
        command_identity="experiments.aggregate_sweep",
        planned_runs=0,
        run_id=args.run_id,
        input_paths=inputs,
    )
    managed.manifest["analysis_input_provenance"] = (
        inspect_legacy_analysis_inputs(inputs).as_manifest_payload()
    )
    for descriptor in managed.manifest.get("inputs", []):
        descriptor["provenance_class"] = "legacy_unverified_input"
    managed.manifest.write_atomic()
    with managed:
        managed.set_stage("result_export")
        analysis = {
            "schema_version": "1.0",
            "unit": "analysis_attempts",
            "planned": 1,
            "started": 1,
            "completed": 0,
            "failed": 0,
            "input_files": len(inputs),
        }
        managed.manifest["managed_context"]["run_kind"] = "analysis"
        managed.manifest["analysis_completion"] = analysis
        managed.manifest.write_atomic()
        try:
            analyze(str(input_dir), str(managed.run_dir / legacy_plot.name))
        except BaseException:
            analysis["failed"] = 1
            managed.manifest.write_atomic()
            raise
        analysis["completed"] = 1
        managed.manifest.write_atomic()
        managed.finish(legacy_filenames=(legacy_plot.name,))


if __name__ == "__main__":
    main()
