"""Drive many (gain, seed) runs with a concurrency cap. Resumable: skips any
(gain, seed) whose result JSON already exists, so a re-launch only fills gaps.

Each run is a separate subprocess (isolated asyncio loop / failure domain).

Usage:
  python -m experiments.drive --gains 1.0 1.5 \
      --seeds 11 13 17 19 23 29 31 37 41 --provider openai --workers 3 --out results
"""
from __future__ import annotations
import os
import sys
import json
import time
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from experiments.driver_utils import (
    archive_rejected_result,
    run_managed_driver_jobs,
    set_driver_provenance,
)

BAD_THRESHOLD = 0.15      # a run with >15% failed orders is endpoint-corrupted
MAX_RETRIES = 5           # VPN drops are expected; give each seed buffer to retry
ENDPOINT = ("10.214.32.152", 8000)   # gated behind a flaky VPN (OnDemand auto-reconnects)


def _endpoint_up(timeout=4):
    s = socket.socket(); s.settimeout(timeout)
    try:
        s.connect(ENDPOINT)          # also kicks VPN OnDemand if tunnel is down
        return True
    except Exception:
        return False
    finally:
        s.close()


def _wait_for_endpoint(max_wait=180):
    """Block until the endpoint is reachable (VPN up). The connect attempts above
    trigger OnDemand to re-establish the tunnel; poll until it's back."""
    waited = 0
    while not _endpoint_up():
        if waited >= max_wait:
            return False
        time.sleep(8)
        waited += 8
    return True


def _exists(out, gain, seed):
    return os.path.exists(os.path.join(out, f"g{gain}_s{seed}.json"))


def _bad_frac(out, gain, seed):
    try:
        with open(os.path.join(out, f"g{gain}_s{seed}.json")) as fh:
            return json.load(fh)["health"]["bad_frac"]
    except Exception:
        return 1.0


def _healthy(out, gain, seed):
    return _exists(out, gain, seed) and _bad_frac(out, gain, seed) <= BAD_THRESHOLD


def _run(gain, seed, provider, out):
    """Run one seed; retry if the endpoint corrupted it (high bad-order rate)."""
    tag = f"g{gain} s{seed}"
    path = os.path.join(out, f"g{gain}_s{seed}.json")
    if os.path.exists(path):
        bad = _bad_frac(out, gain, seed)
        if bad <= BAD_THRESHOLD:
            return (tag, True, "cached")
        archive_rejected_result(path, f"pre-existing bad_frac={bad} > {BAD_THRESHOLD}")
    cmd = [sys.executable, "-m", "experiments.run_seed",
           "--gain", str(gain), "--seed", str(seed),
           "--provider", provider, "--out", out, "--label", f"g{gain}"]
    # Pin PYTHONHASHSEED so every subprocess is identical (defense-in-depth on top
    # of the seed-derived RNGs; the sim is already hash-independent — see repro_check).
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    last = ""
    for attempt in range(1, MAX_RETRIES + 1):
        # only the VPN-gated openai endpoint needs the wait; mock has no endpoint
        if provider == "openai" and not _wait_for_endpoint():
            last = "endpoint unreachable (VPN down >180s)"
            continue
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            last = ((p.stderr or p.stdout).strip().splitlines() or ["error"])[-1]
            archive_rejected_result(
                path,
                f"subprocess exit {p.returncode}; details are in the managed driver private log",
            )
            continue
        bad = _bad_frac(out, gain, seed)
        if bad <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        # corrupted -> drop it and retry
        last = f"bad_frac={bad} > {BAD_THRESHOLD}, retrying"
        archive_rejected_result(path, last)
    return (tag, False, f"gave up after {MAX_RETRIES} attempts: {last}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results",
            command_identity="experiments.drive",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--version", action="version", version="experiments.drive phase-1.1b")
    ap.add_argument("--gains", type=float, nargs="+", required=True)
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="results")
    try:
        args = ap.parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

    set_driver_provenance(args.workers, "experiments.drive")
    all_jobs = [(g, s) for g in args.gains for s in args.seeds]
    todo = [job for job in all_jobs if not _healthy(args.out, *job)]
    print(f"jobs: {len(todo)} to run, {len(all_jobs) - len(todo)} already done "
          f"(workers={args.workers})", flush=True)
    failures, summary = run_managed_driver_jobs(
        out_root=args.out,
        command_identity="experiments.drive",
        jobs=all_jobs,
        workers=args.workers,
        cell_name=lambda job: f"g{job[0]}",
        seed_identity=lambda job: job[1],
        is_healthy=lambda job: _healthy(args.out, *job),
        run_job=lambda job: _run(job[0], job[1], args.provider, args.out),
    )
    print(f"ALL DONE. failures: {len(failures)}; driver summary: {summary}", flush=True)


if __name__ == "__main__":
    main()
