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
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from experiments.driver_utils import archive_rejected_result, set_driver_provenance

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
            archive_rejected_result(path, f"subprocess exit {p.returncode}: {last}")
            continue
        bad = _bad_frac(out, gain, seed)
        if bad <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        # corrupted -> drop it and retry
        last = f"bad_frac={bad} > {BAD_THRESHOLD}, retrying"
        archive_rejected_result(path, last)
    return (tag, False, f"gave up after {MAX_RETRIES} attempts: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gains", type=float, nargs="+", required=True)
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    set_driver_provenance(args.workers, "experiments.drive")

    jobs = [(g, s) for g in args.gains for s in args.seeds
            if not _healthy(args.out, g, s)]
    skipped = [(g, s) for g in args.gains for s in args.seeds
               if _healthy(args.out, g, s)]
    print(f"jobs: {len(jobs)} to run, {len(skipped)} already done "
          f"(workers={args.workers})", flush=True)

    done, fails = 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_run, g, s, args.provider, args.out) for g, s in jobs]
        for f in as_completed(futs):
            tag, ok, msg = f.result()
            done += 1
            status = "OK " if ok else "FAIL"
            print(f"[{done}/{len(jobs)}] {status} {tag}: {msg}", flush=True)
            if not ok:
                fails.append((tag, msg))
    with open(os.path.join(args.out, "failures.log"), "a") as fh:
        for tag, msg in fails:
            fh.write(f"{tag}: {msg}\n")
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
