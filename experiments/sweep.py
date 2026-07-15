"""Driver for the population-composition sweep + its step-0 noise calibration.

Modes:
  calibrate  same (m, seed), social on/off x R repeated draws (temp>0 sampling)
             -> measures within-config drop spread, to size N pairs per m-level
  sweep      m-levels x paired seeds, social on/off per pair, real Meta news

Sequential by default (each 30-agent run already fires 30 concurrent calls per
round; the vLLM endpoint hangs above ~70 sustained concurrent). Resumable
(skips existing JSONs), VPN-wait, health gate, honest failures.log.

Usage:
  python -m experiments.sweep calibrate --m 0.5 --seed 1 --reps 5 --out results_sweep
  python -m experiments.sweep sweep --m-levels 0.3 0.5 0.7 --seeds 1 2 3 4 5 6 7 8 \
      --out results_sweep
"""
from __future__ import annotations
import os
import sys
import json
import argparse
import subprocess

from experiments.drive import _wait_for_endpoint, BAD_THRESHOLD, MAX_RETRIES
from experiments.driver_utils import archive_rejected_result, set_driver_provenance


def _bad_frac(path):
    try:
        with open(path) as fh:
            return json.load(fh)["health"]["bad_frac"]
    except Exception:
        return 1.0


def _fname(out, m, social, seed, rep):
    sfx = f"_r{rep}" if rep is not None else ""
    return os.path.join(out, f"m{m:g}_real_{social}_s{seed}{sfx}.json")


def _run_one(out, m, social, seed, rep, provider, temp, total, rounds, model=None):
    path = _fname(out, m, social, seed, rep)
    tag = os.path.basename(path)[:-5]
    if os.path.exists(path):
        bad = _bad_frac(path)
        if bad <= BAD_THRESHOLD:
            return (tag, True, "cached")
        archive_rejected_result(path, f"pre-existing bad_frac={bad} > {BAD_THRESHOLD}")
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
        bad = _bad_frac(path)
        if bad <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac={bad} > {BAD_THRESHOLD}, retrying"
        archive_rejected_result(path, last)
    return (tag, False, f"gave up after {MAX_RETRIES} attempts: {last}")


def main():
    ap = argparse.ArgumentParser()
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
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    set_driver_provenance(args.workers, "experiments.sweep")
    if args.mode == "calibrate":
        jobs = [(args.m, soc, args.seed, rep)
                for rep in range(1, args.reps + 1) for soc in ("on", "off")]
    else:
        # pair-major order: finish (m, seed) on+off together so partial progress
        # still yields complete PAIRS (the unit of analysis)
        jobs = [(m, soc, s, None)
                for m in args.m_levels for s in args.seeds for soc in ("on", "off")]

    todo = [j for j in jobs
            if not (os.path.exists(_fname(args.out, j[0], j[1], j[2], j[3]))
                    and _bad_frac(_fname(args.out, j[0], j[1], j[2], j[3])) <= BAD_THRESHOLD)]
    print(f"{args.mode}: {len(jobs)} runs total, {len(todo)} to do "
          f"(total={args.total} agents, temp={args.temp}, rounds={args.rounds or 24})",
          flush=True)

    fails = []
    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(_run_one, args.out, m, soc, s, rep,
                              args.provider, args.temp, args.total, args.rounds,
                              args.model)
                    for (m, soc, s, rep) in todo]
            for f in as_completed(futs):
                tag, ok, msg = f.result()
                done += 1
                print(f"[{done}/{len(todo)}] {'OK ' if ok else 'FAIL'} {tag}: {msg}",
                      flush=True)
                if not ok:
                    fails.append((tag, msg))
    else:
        for i, (m, soc, s, rep) in enumerate(todo, 1):
            tag, ok, msg = _run_one(args.out, m, soc, s, rep,
                                    args.provider, args.temp, args.total, args.rounds, args.model)
            print(f"[{i}/{len(todo)}] {'OK ' if ok else 'FAIL'} {tag}: {msg}", flush=True)
            if not ok:
                fails.append((tag, msg))

    with open(os.path.join(args.out, "failures.log"), "a") as fh:
        for tag, msg in fails:
            fh.write(f"{tag}: {msg}\n")
    print(f"DONE. failures: {len(fails)}", flush=True)


if __name__ == "__main__":
    main()
