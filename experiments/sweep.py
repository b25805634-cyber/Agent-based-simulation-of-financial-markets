"""Driver for the population-composition sweep + its step-0 noise calibration.

Modes:
  calibrate  same (m, seed), social on/off x R repeated draws (temp>0 sampling)
             -> measures within-config drop spread, to size N pairs per m-level
  sweep      m-levels x paired seeds, social on/off per pair, real Meta news

Sequential by default (each 30-agent run already fires 30 concurrent calls per
round; the vLLM endpoint hangs above ~70 sustained concurrent). Resume uses
the central managed-child identity and artifact gate rather than file
existence; the existing VPN wait, health gate, and honest failures.log remain.

Usage:
  python -m experiments.sweep calibrate --m 0.5 --seed 1 --reps 5 --out results_sweep
  python -m experiments.sweep sweep --m-levels 0.3 0.5 0.7 --seeds 1 2 3 4 5 6 7 8 \
      --out results_sweep
"""
from __future__ import annotations
import os
import sys
import json
import subprocess

from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from experiments.drive import _wait_for_endpoint, BAD_THRESHOLD, MAX_RETRIES
from experiments.driver_utils import (
    assess_run_seed_reuse,
    run_managed_driver_jobs,
    set_driver_provenance,
)


def _bad_frac(path):
    try:
        with open(path) as fh:
            return json.load(fh)["health"]["bad_frac"]
    except Exception:
        return 1.0


def _fname(out, m, social, seed, rep):
    sfx = f"_r{rep}" if rep is not None else ""
    return os.path.join(out, f"m{m:g}_real_{social}_s{seed}{sfx}.json")


def _command(out, m, social, seed, rep, provider, temp, total, rounds, model=None):
    cmd = [sys.executable, "-m", "experiments.run_seed",
           "--seed", str(seed), "--provider", provider, "--out", out,
           "--news", "real", "--social", social,
           "--m", str(m), "--total", str(total), "--temp", str(temp)]
    if model:
        cmd += ["--model", model]
    if rep is not None:
        cmd += ["--rep", str(rep)]
    if rounds is not None:
        cmd += ["--rounds", str(rounds)]
    return cmd


def _run_one(
    out, m, social, seed, rep, provider, temp, total, rounds, model=None,
    on_child_launch=None,
):
    path = _fname(out, m, social, seed, rep)
    tag = os.path.basename(path)[:-5]
    cmd = _command(out, m, social, seed, rep, provider, temp, total, rounds, model)
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    last = ""
    for attempt in range(1, MAX_RETRIES + 1):
        if provider == "openai" and not _wait_for_endpoint():
            last = "endpoint unreachable (VPN down >180s)"
            continue
        if on_child_launch is not None:
            on_child_launch()
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            last = ((p.stderr or p.stdout).strip().splitlines() or ["error"])[-1]
            continue
        bad = _bad_frac(path)
        if bad <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac={bad} > {BAD_THRESHOLD}, retrying"
    return (tag, False, f"gave up after {MAX_RETRIES} attempts: {last}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_sweep",
            command_identity="experiments.sweep",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--version", action="version", version="experiments.sweep phase-1.2a")
    ap.add_argument("mode", choices=["calibrate", "sweep"])
    ap.add_argument("--m", type=float, default=0.5, help="calibrate: fuel ratio")
    ap.add_argument("--seed", type=int, default=1, help="calibrate: the fixed seed")
    ap.add_argument("--reps", type=int, default=5, help="calibrate: draws per condition")
    ap.add_argument("--m-levels", type=float, nargs="+", default=[0.3, 0.5, 0.7])
    ap.add_argument("--seeds", type=int, nargs="+", default=list(range(1, 9)))
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--model", default=None, help="override served model id")
    ap.add_argument("--temp", type=float, default=0.3)
    ap.add_argument("--total", type=int, default=30)
    ap.add_argument("--rounds", type=int, default=None)
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel runs; 2 = ~60 peak concurrent calls (probe-verified "
                         "safe; fills the per-round straggler idle). Do NOT exceed 2.")
    ap.add_argument("--out", default="results_sweep")
    try:
        args = ap.parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    set_driver_provenance(args.workers, "experiments.sweep")
    if args.mode == "calibrate":
        jobs = [(args.m, soc, args.seed, rep)
                for rep in range(1, args.reps + 1) for soc in ("on", "off")]
    else:
        # pair-major order: finish (m, seed) on+off together so partial progress
        # still yields complete PAIRS (the unit of analysis)
        jobs = [(m, soc, s, None)
                for m in args.m_levels for s in args.seeds for soc in ("on", "off")]

    reuse_checks = {}
    def assess(job):
        if job not in reuse_checks:
            reuse_checks[job] = assess_run_seed_reuse(
                candidate_path=_fname(args.out, job[0], job[1], job[2], job[3]),
                allowed_result_root=args.out,
                child_command=_command(
                    args.out, job[0], job[1], job[2], job[3], args.provider,
                    args.temp, args.total, args.rounds, args.model,
                ),
                max_bad_frac=BAD_THRESHOLD,
            )
        return reuse_checks[job]
    todo = [j for j in jobs if assess(j) is None or not assess(j).reusable]
    print(f"{args.mode}: {len(jobs)} runs total, {len(todo)} to do "
          f"(total={args.total} agents, temp={args.temp}, rounds={args.rounds or 24})",
          flush=True)

    failures, summary = run_managed_driver_jobs(
        out_root=args.out,
        command_identity="experiments.sweep",
        jobs=jobs,
        workers=args.workers,
        cell_name=lambda job: f"m{job[0]:g}_{job[1]}",
        seed_identity=lambda job: job[2],
        assess_reuse=assess,
        run_job=lambda job, on_child_launch: _run_one(
            args.out,
            job[0],
            job[1],
            job[2],
            job[3],
            args.provider,
            args.temp,
            args.total,
            args.rounds,
            args.model,
            on_child_launch,
        ),
    )
    print(f"DONE. failures: {len(failures)}; driver summary: {summary}", flush=True)


if __name__ == "__main__":
    main()
