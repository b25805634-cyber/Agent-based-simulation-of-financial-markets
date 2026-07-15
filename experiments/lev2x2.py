"""Driver for the leverage 2x2 — runs the TWO new leverage-ON cells (social
on/off, leverage on), paired by seed against the existing leverage-OFF baseline
(m{m}_real_on / m{m}_real_off, already in results_sweep). Resumable, health-
gated, VPN-wait, workers<=2.

The full 2x2 {social on/off} x {leverage on/off}:
  lev OFF row = m{m}_real_on  + m{m}_real_off   (ALREADY RUN — reused free)
  lev ON  row = m{m}_real_on_lev + m{m}_real_off_lev   (THIS DRIVER)

Usage:
  python -m experiments.lev2x2 --m 0.7 --seeds 1 2 3 4 5 6 7 8 9 10 11 12 \
      --workers 2 --out results_sweep
"""
from __future__ import annotations
import os
import sys
import json
import argparse
import subprocess

from experiments.drive import _wait_for_endpoint, BAD_THRESHOLD, MAX_RETRIES
from experiments.driver_utils import archive_rejected_result, set_driver_provenance


def _path(out, m, social, seed):
    return os.path.join(out, f"m{m:g}_real_{social}_lev_s{seed}.json")


def _bad(path):
    try:
        return json.load(open(path))["health"]["bad_frac"]
    except Exception:
        return 1.0


def _run(out, m, social, seed, provider, temp, total, model):
    path = _path(out, m, social, seed)
    tag = os.path.basename(path)[:-5]
    if os.path.exists(path):
        bad = _bad(path)
        if bad <= BAD_THRESHOLD:
            return (tag, True, "cached")
        archive_rejected_result(path, f"pre-existing bad_frac={bad} > {BAD_THRESHOLD}")
    cmd = [sys.executable, "-m", "experiments.run_seed", "--seed", str(seed),
           "--provider", provider, "--out", out, "--social", social,
           "--leverage", "--m", str(m), "--total", str(total), "--temp", str(temp)]
    if model:
        cmd += ["--model", model]
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    last = ""
    for attempt in range(1, MAX_RETRIES + 1):
        if provider == "openai" and not _wait_for_endpoint():
            last = "endpoint unreachable (VPN down >180s)"
            continue
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            last = ((p.stderr or p.stdout).strip().splitlines() or ["error"])[-1]
            archive_rejected_result(path, f"subprocess exit {p.returncode}: {last}")
            continue
        if _bad(path) <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac>{BAD_THRESHOLD}, retrying"
        archive_rejected_result(path, last)
    return (tag, False, f"gave up after {MAX_RETRIES}: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", type=float, default=0.7)
    ap.add_argument("--socials", nargs="+", default=["off", "on"])
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--model", default=None)
    ap.add_argument("--temp", type=float, default=0.3)
    ap.add_argument("--total", type=int, default=30)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default="results_sweep")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    set_driver_provenance(args.workers, "experiments.lev2x2")

    # cell-major: finish each leverage-ON cell's full seed set so an interruption
    # still leaves a complete, analyzable cell.
    jobs = [(args.m, soc, s) for soc in args.socials for s in args.seeds]
    print(f"leverage 2x2 (new ON cells) m={args.m:g} socials={args.socials}: "
          f"{len(jobs)} runs (workers={args.workers}, model={args.model or 'MiniMax-M2.7'})",
          flush=True)

    fails, done = [], 0
    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(_run, args.out, m, soc, s, args.provider,
                              args.temp, args.total, args.model)
                    for (m, soc, s) in jobs]
            for f in as_completed(futs):
                tag, ok, msg = f.result(); done += 1
                print(f"[{done}/{len(jobs)}] {'OK ' if ok else 'FAIL'} {tag}: {msg}", flush=True)
                if not ok:
                    fails.append((tag, msg))
    else:
        for m, soc, s in jobs:
            tag, ok, msg = _run(args.out, m, soc, s, args.provider,
                                args.temp, args.total, args.model)
            done += 1
            print(f"[{done}/{len(jobs)}] {'OK ' if ok else 'FAIL'} {tag}: {msg}", flush=True)
            if not ok:
                fails.append((tag, msg))

    with open(os.path.join(args.out, "failures.log"), "a") as fh:
        for tag, msg in fails:
            fh.write(f"{tag}: {msg}\n")
    print(f"DONE. failures: {len(fails)}", flush=True)


if __name__ == "__main__":
    main()
