"""2x2 contagion experiment: {real Meta news / placebo} x {social ON / OFF}.

Toggles ONLY the social channel (price visibility is always on, identical in all
cells) — so any real_on vs real_off difference is attributable to the narrative
channel, not the price tape. N seeds per cell.

Resumable (skips existing), self-healing (health gate + VPN wait), and HONEST
about N: every dropped seed is logged to failures.log, none silently vanish.

Usage:
  # free, instant pipeline validation + preview:
  python -m experiments.grid2x2 --seeds 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 \
      --provider mock --out results_2x2_mock
  # real model, genuine heterogeneity (temp>0 auto-disables cache):
  python -m experiments.grid2x2 --seeds <15 seeds> --provider openai --temp 0.3 \
      --workers 1 --out results_2x2
"""
from __future__ import annotations
import os
import sys
import json
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from experiments.drive import _wait_for_endpoint, BAD_THRESHOLD, MAX_RETRIES
from experiments.driver_utils import archive_rejected_result, set_driver_provenance

# (label, news, social)
CELLS = [
    ("real_on",  "real",    "on"),
    ("real_off", "real",    "off"),
    ("plac_on",  "placebo", "on"),
    ("plac_off", "placebo", "off"),
]


def _exists(out, label, seed):
    return os.path.exists(os.path.join(out, f"{label}_s{seed}.json"))


def _bad_frac(out, label, seed):
    try:
        with open(os.path.join(out, f"{label}_s{seed}.json")) as fh:
            return json.load(fh)["health"]["bad_frac"]
    except Exception:
        return 1.0


def _healthy(out, label, seed):
    return _exists(out, label, seed) and _bad_frac(out, label, seed) <= BAD_THRESHOLD


def _run(label, news, social, seed, provider, temp, out):
    tag = f"{label} s{seed}"
    path = os.path.join(out, f"{label}_s{seed}.json")
    if os.path.exists(path):
        bad = _bad_frac(out, label, seed)
        if bad <= BAD_THRESHOLD:
            return (tag, True, "cached")
        archive_rejected_result(path, f"pre-existing bad_frac={bad} > {BAD_THRESHOLD}")
    cmd = [sys.executable, "-m", "experiments.run_seed", "--seed", str(seed),
           "--provider", provider, "--out", out,
           "--news", news, "--social", social, "--label", label]
    if temp is not None:
        cmd += ["--temp", str(temp)]
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
        bad = _bad_frac(out, label, seed)
        if bad <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac={bad} > {BAD_THRESHOLD}, retrying"
        archive_rejected_result(path, last)
    return (tag, False, f"gave up after {MAX_RETRIES} attempts: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--temp", type=float, default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", default="results_2x2")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    set_driver_provenance(args.workers, "experiments.grid2x2")
    jobs = [(lbl, news, soc, s) for (lbl, news, soc) in CELLS for s in args.seeds
            if not _healthy(args.out, lbl, s)]
    already = sum(1 for (lbl, _, _) in CELLS for s in args.seeds
                  if _healthy(args.out, lbl, s))
    total = len(CELLS) * len(args.seeds)
    print(f"2x2 grid: {len(CELLS)} cells x {len(args.seeds)} seeds = {total} runs; "
          f"{len(jobs)} to run, {already} already done "
          f"(workers={args.workers}, temp={args.temp})", flush=True)

    fails, n = [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_run, lbl, news, soc, s, args.provider, args.temp, args.out)
                for (lbl, news, soc, s) in jobs]
        for f in as_completed(futs):
            tag, ok, msg = f.result()
            n += 1
            print(f"[{n}/{len(jobs)}] {'OK ' if ok else 'FAIL'} {tag}: {msg}", flush=True)
            if not ok:
                fails.append((tag, msg))

    with open(os.path.join(args.out, "failures.log"), "a") as fh:
        for tag, msg in fails:
            fh.write(f"{tag}: {msg}\n")
    # realized N per cell (honest reporting)
    print("\nrealized N per cell:", flush=True)
    for (lbl, _, _) in CELLS:
        got = sum(1 for s in args.seeds if _healthy(args.out, lbl, s))
        print(f"  {lbl}: {got}/{len(args.seeds)}", flush=True)
    print(f"ALL DONE. failures: {len(fails)} (see {args.out}/failures.log)", flush=True)


if __name__ == "__main__":
    main()
