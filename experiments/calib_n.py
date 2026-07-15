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

DELTA = 0.18          # 2x2 baseline ON-OFF drop effect, the size we must resolve
N_MIN, N_MAX = 8, 12


def calibrate(input_dir, m, seed, out_path):
    arms = {}
    for soc in ("on", "off"):
        files = sorted(glob.glob(
            f"{input_dir}/m{m:g}_real_{soc}_s{seed}_r*.json"))
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

    with open(out_path, "w") as fh:
        fh.write(str(n))
    print(f"[calib] wrote {out_path} = {n}")
    return n


def _input_paths(out_dir, m, seed):
    paths = []
    for soc in ("on", "off"):
        paths.extend(sorted(Path(out_dir).glob(
            f"m{m:g}_real_{soc}_s{seed}_r*.json"
        )))
    return paths


def build_argparser():
    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--out", default="results_sweep")
    ap.add_argument("--m", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--run-id", default=None)
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_sweep",
            command_identity="python -m experiments.calib_n",
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
    inputs = _input_paths(input_dir, args.m, args.seed)
    managed = ManagedRunContext.create_driver(
        out_root=input_dir,
        command_identity="experiments.calib_n",
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
            calibrate(
                str(input_dir), args.m, args.seed,
                str(managed.run_dir / "calib_N.txt"),
            )
        except BaseException:
            analysis["failed"] = 1
            managed.manifest.write_atomic()
            raise
        analysis["completed"] = 1
        managed.manifest.write_atomic()
        managed.finish(legacy_filenames=("calib_N.txt",))


if __name__ == "__main__":
    main()
