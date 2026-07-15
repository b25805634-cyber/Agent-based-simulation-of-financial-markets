"""Driver for the influencer-megaphone ablation arms (muted / solo / demoted)
at a fixed m, paired by seed against the existing ON/OFF baseline.

Resumable, health-gated, VPN-wait, workers<=2. Writes m{m}_real_{arm}_s{seed}.json.

Usage:
  python -m experiments.ablate --m 0.7 --arms muted solo \
      --seeds 9 10 11 12 13 14 15 16 1 2 3 4 --workers 2 --out results_sweep
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


def _path(out, m, arm, seed):
    return os.path.join(out, f"m{m:g}_real_{arm}_s{seed}.json")


def _bad(path):
    try:
        return json.load(open(path))["health"]["bad_frac"]
    except Exception:
        return 1.0


def _command(out, m, arm, seed, provider, temp, total, model):
    cmd = [sys.executable, "-m", "experiments.run_seed", "--seed", str(seed),
           "--provider", provider, "--out", out, "--arm", arm,
           "--m", str(m), "--total", str(total), "--temp", str(temp)]
    if model:
        cmd += ["--model", model]
    return cmd


def _run(out, m, arm, seed, provider, temp, total, model, on_child_launch=None):
    path = _path(out, m, arm, seed)
    tag = os.path.basename(path)[:-5]
    cmd = _command(out, m, arm, seed, provider, temp, total, model)
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
        if _bad(path) <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac>{BAD_THRESHOLD}, retrying"
    return (tag, False, f"gave up after {MAX_RETRIES}: {last}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_sweep",
            command_identity="experiments.ablate",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--version", action="version", version="experiments.ablate phase-1.2a")
    ap.add_argument("--m", type=float, default=0.7)
    ap.add_argument("--arms", nargs="+", default=["muted", "solo"])
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--model", default=None)
    ap.add_argument("--temp", type=float, default=0.3)
    ap.add_argument("--total", type=int, default=30)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default="results_sweep")
    try:
        args = ap.parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")
    set_driver_provenance(args.workers, "experiments.ablate")

    # arm-major within seed-pairing: finish each arm's full seed set so any
    # interruption still leaves a complete, analyzable arm.
    jobs = [(args.m, arm, s) for arm in args.arms for s in args.seeds]
    print(f"ablation m={args.m:g} arms={args.arms}: {len(jobs)} runs "
          f"(workers={args.workers}, model={args.model or 'MiniMax-M2.7'})", flush=True)

    reuse_checks = {}
    def assess(job):
        if job not in reuse_checks:
            reuse_checks[job] = assess_run_seed_reuse(
                candidate_path=_path(args.out, job[0], job[1], job[2]),
                allowed_result_root=args.out,
                child_command=_command(
                    args.out, job[0], job[1], job[2], args.provider,
                    args.temp, args.total, args.model,
                ),
                max_bad_frac=BAD_THRESHOLD,
            )
        return reuse_checks[job]

    failures, summary = run_managed_driver_jobs(
        out_root=args.out,
        command_identity="experiments.ablate",
        jobs=jobs,
        workers=args.workers,
        cell_name=lambda job: str(job[1]),
        seed_identity=lambda job: job[2],
        assess_reuse=assess,
        run_job=lambda job, on_child_launch: _run(
            args.out,
            job[0],
            job[1],
            job[2],
            args.provider,
            args.temp,
            args.total,
            args.model,
            on_child_launch,
        ),
    )
    print(f"DONE. failures: {len(failures)}; driver summary: {summary}", flush=True)


if __name__ == "__main__":
    main()
