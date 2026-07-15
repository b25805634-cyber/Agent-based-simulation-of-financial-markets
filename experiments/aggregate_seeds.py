"""Aggregate the per-seed result JSONs for a gain into a distribution:
mean +/- std for every metric, and the key envelope-band plot (all sim paths as
a band, real Meta overlaid) so you can see whether Meta falls inside the sim
distribution.

Usage:
  python -m experiments.aggregate_seeds --gain 1.5 --results results
  python -m experiments.aggregate_seeds --gain 1.0 --results results --out results
"""
from __future__ import annotations
import os
import glob
import json
import math
import sys
from pathlib import Path
from statistics import mean, pstdev

from nmsim import validation as V
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from nmsim.run_context import ManagedRunContext
from nmsim.result_reuse import inspect_legacy_analysis_inputs

META = "nmsim/meta_feb2022_reference.csv"


def _load(gain, results_dir):
    runs = []
    for path in sorted(glob.glob(os.path.join(results_dir, f"g{gain}_s*.json"))):
        with open(path) as fh:
            runs.append(json.load(fh))
    return runs


def _stat(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not xs:
        return {"mean": None, "std": None, "min": None, "max": None, "n": 0}
    return {"mean": round(mean(xs), 4), "std": round(pstdev(xs), 4) if len(xs) > 1 else 0.0,
            "min": round(min(xs), 4), "max": round(max(xs), 4), "n": len(xs)}


def _meta_norm_price(L):
    ref, shock = V.load_reference(META)
    base = ref[shock]
    return [ref[shock + t] / base for t in range(min(L, len(ref) - shock))]


def aggregate(gain, results_dir, out_dir):
    runs = _load(gain, results_dir)
    if not runs:
        raise SystemExit(f"no result JSONs for gain {gain} in {results_dir}")
    keys = ["rmse_logprice", "dtw_norm", "drop_depth", "speed", "recovery", "peak_cascade"]
    dist = {k: _stat([r["metrics"][k] for r in runs]) for k in keys}
    seeds = [r["seed"] for r in runs]
    rmse_per_seed = {r["seed"]: r["metrics"]["rmse_logprice"] for r in runs}
    bad = _stat([r["health"]["bad_frac"] for r in runs])

    # ---- envelope coverage: does Meta sit inside the sim band? ----
    paths = [r["norm_price_path"] for r in runs]
    L = min(len(p) for p in paths)
    meta = _meta_norm_price(L)
    L = min(L, len(meta))
    cols = [[p[t] for p in paths] for t in range(L)]
    band_mean = [mean(c) for c in cols]
    band_std = [pstdev(c) if len(c) > 1 else 0.0 for c in cols]
    band_min = [min(c) for c in cols]
    band_max = [max(c) for c in cols]
    in_minmax = sum(band_min[t] <= meta[t] <= band_max[t] for t in range(L))
    in_1std = sum(band_mean[t] - band_std[t] <= meta[t] <= band_mean[t] + band_std[t]
                  for t in range(L))

    summary = {
        "gain": gain, "n_seeds": len(runs), "seeds": seeds,
        "distributions": dist,
        "rmse_logprice_per_seed": rmse_per_seed,
        "health_bad_frac": bad,
        "meta_in_band": {
            "horizon": L,
            "within_minmax_frac": round(in_minmax / L, 4),
            "within_1std_frac": round(in_1std / L, 4),
        },
    }
    os.makedirs(out_dir, exist_ok=True)
    spath = os.path.join(out_dir, f"summary_g{gain}.json")
    with open(spath, "w") as fh:
        json.dump(summary, fh, indent=2)

    _plot(gain, paths, band_mean, band_std, band_min, band_max, meta, L, dist,
          os.path.join(out_dir, f"envelope_g{gain}.png"))
    _print(summary)
    print(f"wrote {spath} and envelope_g{gain}.png")
    return summary


def _plot(gain, paths, mean_, std_, min_, max_, meta, L, dist, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xs = list(range(L))
    fig, ax = plt.subplots(figsize=(10.5, 6))
    # min-max envelope (light) + 1-std band (darker)
    ax.fill_between(xs, min_[:L], max_[:L], color="#2C6FBB", alpha=0.12,
                    label="sim min-max")
    ax.fill_between(xs, [mean_[t] - std_[t] for t in xs], [mean_[t] + std_[t] for t in xs],
                    color="#2C6FBB", alpha=0.25, label="sim mean ±1 std")
    for p in paths:                       # thin individual paths
        ax.plot(xs, p[:L], color="#2C6FBB", alpha=0.18, lw=0.8)
    ax.plot(xs, mean_[:L], color="#1B4F8A", lw=2.2, label="sim mean")
    ax.plot(xs, meta[:L], color="#D81B1B", lw=2.4, ls="--", marker="s", ms=4,
            label="REAL Meta Feb-2022")
    ax.axhline(1.0, color="#999", lw=0.8)
    ax.axvline(0, color="#ccc", ls=":", lw=1)
    rm = dist["rmse_logprice"]; rc = dist["recovery"]
    ax.set_title(f"gain={gain}: {len(paths)} sim paths vs real Meta "
                 f"(RMSE log-price {rm['mean']}±{rm['std']}, recovery {rc['mean']}±{rc['std']})")
    ax.set_xlabel("rounds / trading days since shock (t=0)")
    ax.set_ylabel("price / pre-shock price")
    ax.grid(alpha=0.3)
    ax.legend(loc="lower left", fontsize=9)
    plt.tight_layout()
    plt.savefig(out_png, dpi=135)
    plt.close(fig)


def _print(s):
    print(f"\n===== gain {s['gain']}  (N={s['n_seeds']} seeds: {s['seeds']}) =====")
    for k, d in s["distributions"].items():
        print(f"  {k:14s}: {d['mean']} ± {d['std']}   [min {d['min']}, max {d['max']}]")
    print(f"  health bad_frac: {s['health_bad_frac']['mean']} (max {s['health_bad_frac']['max']})")
    mb = s["meta_in_band"]
    print(f"  Meta inside sim band: min-max {mb['within_minmax_frac']:.0%}, "
          f"±1std {mb['within_1std_frac']:.0%}  (over {mb['horizon']} steps)")
    print(f"  RMSE per seed: {s['rmse_logprice_per_seed']}")


def _input_paths(gain, results_dir):
    return [Path(META), *sorted(Path(results_dir).glob(f"g{gain}_s*.json"))]


def build_argparser():
    p = RaisingArgumentParser(allow_abbrev=False)
    p.add_argument("--gain", type=float, required=True)
    p.add_argument("--results", default="results")
    p.add_argument("--out", default="results")
    p.add_argument("--run-id", default=None)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results",
            command_identity="python -m experiments.aggregate_seeds",
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
    inputs = _input_paths(args.gain, args.results)
    managed = ManagedRunContext.create_driver(
        out_root=Path(args.out),
        command_identity="experiments.aggregate_seeds",
        planned_runs=0,
        run_id=args.run_id,
        input_paths=inputs,
    )
    managed.manifest["analysis_input_provenance"] = (
        inspect_legacy_analysis_inputs(inputs[1:]).as_manifest_payload()
    )
    for index, descriptor in enumerate(managed.manifest.get("inputs", [])):
        descriptor["provenance_class"] = (
            "scientific_reference_input"
            if index == 0
            else "legacy_unverified_input"
        )
    managed.manifest.write_atomic()
    names = (f"summary_g{args.gain}.json", f"envelope_g{args.gain}.png")
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
            aggregate(args.gain, args.results, str(managed.run_dir))
        except BaseException:
            analysis["failed"] = 1
            managed.manifest.write_atomic()
            raise
        analysis["completed"] = 1
        managed.manifest.write_atomic()
        managed.finish(legacy_filenames=names)


if __name__ == "__main__":
    main()
