"""Task 2 — critical-point coarse sweep: how strong an amplifier flips the market
from self-stabilising to self-destructing.

Fixed: m=0.7, social ON, temp=0, real Meta news, same-seed paired. Sweep the
LEVERAGE STRENGTH (L center → shallower breach trigger = more aggressive) across
several levels; each level × N seeds. Each lev-ON run pairs against the SAME-SEED
social-ON lev-OFF temp=0 baseline (from Phase 2b, in --baseline-dir). Find the
level where crash depth suddenly drops (the self-stabilise → self-destruct point).

Breach trigger by L (maintenance 0.25): L=2.0→-33%, 2.5→-20%, 3.0→-11%, 3.5→-7%.

Labels are distinct per level: m{m}_critL{ratio:g}_s{seed}.json (no collision).
Resumable / VPN-wait / health-gate / honest-N (same self-healing as phase2b).

Usage:
  python -m experiments.critsweep --levels 2.0 2.6 3.0 3.5 --spread 0.3 \
      --seeds 1 2 3 4 5 6 7 8 --out results_critsweep --workers 3
"""
from __future__ import annotations
import os, sys, json, subprocess
from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from experiments.drive import _wait_for_endpoint, BAD_THRESHOLD, MAX_RETRIES
from experiments.driver_utils import (
    archive_rejected_result,
    run_managed_driver_jobs,
    set_driver_provenance,
)


def _label(m, ratio):
    return f"m{m:g}_critL{ratio:g}"          # run_seed appends _s{seed}


def _bad(p):
    try:
        return json.load(open(p))["health"]["bad_frac"]
    except Exception:
        return 1.0


def _run(out, m, ratio, spread, maint, frac, seed, provider, total, model):
    label = _label(m, ratio)
    path = os.path.join(out, f"{label}_s{seed}.json")
    if os.path.exists(path):
        bad = _bad(path)
        if bad <= BAD_THRESHOLD:
            return (f"{label}_s{seed}", True, "cached")
        archive_rejected_result(path, f"pre-existing bad_frac={bad} > {BAD_THRESHOLD}")
    cmd = [sys.executable, "-m", "experiments.run_seed", "--seed", str(seed),
           "--provider", provider, "--out", out, "--label", label,
           "--social", "on", "--leverage", "--m", str(m), "--total", str(total),
           "--temp", "0", "--leverage-ratio", str(ratio), "--leverage-spread", str(spread)]
    if maint is not None: cmd += ["--maint", str(maint)]
    if frac is not None:  cmd += ["--lev-fraction", str(frac)]
    if model:             cmd += ["--model", model]
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    last = ""
    for attempt in range(1, MAX_RETRIES + 1):
        if provider == "openai" and not _wait_for_endpoint():
            last = "endpoint unreachable (VPN down >180s)"; continue
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            last = ((p.stderr or p.stdout).strip().splitlines() or ["error"])[-1]
            archive_rejected_result(
                path,
                f"subprocess exit {p.returncode}; details are in the managed driver private log",
            )
            continue
        if _bad(path) <= BAD_THRESHOLD:
            return (f"{label}_s{seed}", True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac>{BAD_THRESHOLD}"
        archive_rejected_result(path, last)
    return (f"{label}_s{seed}", False, f"gave up: {last}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_critsweep",
            command_identity="experiments.critsweep",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--version", action="version", version="experiments.critsweep phase-1.1b")
    ap.add_argument("--m", type=float, default=0.7)
    ap.add_argument("--levels", type=float, nargs="+", required=True,
                    help="leverage L centers, low→high strength (shallower trigger)")
    ap.add_argument("--spread", type=float, default=0.3)
    ap.add_argument("--maint", type=float, default=None)
    ap.add_argument("--fraction", type=float, default=None)
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--model", default=None)
    ap.add_argument("--total", type=int, default=30)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="results_critsweep")
    try:
        args = ap.parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")
    set_driver_provenance(args.workers, "experiments.critsweep")

    jobs = [(L, s) for L in args.levels for s in args.seeds]
    print(f"critsweep temp=0 m={args.m:g} social=on levels(L)={args.levels} spread={args.spread} "
          f"maint={args.maint} frac={args.fraction}: {len(jobs)} runs (workers={args.workers})", flush=True)

    failures, summary = run_managed_driver_jobs(
        out_root=args.out,
        command_identity="experiments.critsweep",
        jobs=jobs,
        workers=args.workers,
        cell_name=lambda job: _label(args.m, job[0]),
        seed_identity=lambda job: job[1],
        is_healthy=lambda job: (
            os.path.exists(
                os.path.join(args.out, f"{_label(args.m, job[0])}_s{job[1]}.json")
            )
            and _bad(
                os.path.join(args.out, f"{_label(args.m, job[0])}_s{job[1]}.json")
            )
            <= BAD_THRESHOLD
        ),
        run_job=lambda job: _run(
            args.out,
            args.m,
            job[0],
            args.spread,
            args.maint,
            args.fraction,
            job[1],
            args.provider,
            args.total,
            args.model,
        ),
    )
    print(f"DONE. failures: {len(failures)}; driver summary: {summary}", flush=True)


if __name__ == "__main__":
    main()
