"""Phase 2b — TEMP=0 deterministic leverage 2x2 (clean causal leverage effect).

At temp=0 the LLM is greedy/deterministic, so for a given seed the lev-OFF and
lev-ON runs are byte-identical UNTIL the first forced liquidation, then diverge
ONLY because of the forced-sell pressure. The paired drop difference is therefore
the PURE leverage effect, with NO temp-0.3 sampling noise (the endpoint ignores
`seed`, so temp=0 is the only way to control LLM sampling here).

Self-contained 4-cell 2x2 written to a FRESH dir (no collision with the temp=0.3
baseline): m{m}_real_{off,on}[_lev]_s{seed}.json. Analyse with lev_analyze --out <dir>.

Two passes (so leverage thresholds can be calibrated to the measured crash depth):
  --cells levoff   -> run only the lev-OFF baseline cells (calibration + baseline)
  --cells levon    -> run only the lev-ON cells (with calibrated --leverage-* params)
  --cells both     -> everything

Usage:
  python -m experiments.phase2b --cells levoff --seeds 1 2 3 4 5 6 7 8 --out results_phase2b
  python -m experiments.phase2b --cells levon  --seeds 1..8 --leverage-ratio 3.0 --leverage-spread 0.7 --out results_phase2b
"""
from __future__ import annotations
import os, sys, json, argparse, subprocess
from experiments.drive import _wait_for_endpoint, BAD_THRESHOLD, MAX_RETRIES
from experiments.driver_utils import archive_rejected_result, set_driver_provenance


def _path(out, m, social, lev, seed):
    return os.path.join(out, f"m{m:g}_real_{social}{'_lev' if lev else ''}_s{seed}.json")


def _bad(p):
    try:
        return json.load(open(p))["health"]["bad_frac"]
    except Exception:
        return 1.0


def _run(out, m, social, lev, seed, provider, total, model, lr, lspread, maint, lfrac):
    path = _path(out, m, social, lev, seed)
    tag = os.path.basename(path)[:-5]
    if os.path.exists(path):
        bad = _bad(path)
        if bad <= BAD_THRESHOLD:
            return (tag, True, "cached")
        archive_rejected_result(path, f"pre-existing bad_frac={bad} > {BAD_THRESHOLD}")
    cmd = [sys.executable, "-m", "experiments.run_seed", "--seed", str(seed),
           "--provider", provider, "--out", out, "--social", social,
           "--m", str(m), "--total", str(total), "--temp", "0"]
    if lev:
        cmd += ["--leverage"]
        if lr is not None:      cmd += ["--leverage-ratio", str(lr)]
        if lspread is not None: cmd += ["--leverage-spread", str(lspread)]
        if maint is not None:   cmd += ["--maint", str(maint)]
        if lfrac is not None:   cmd += ["--lev-fraction", str(lfrac)]
    if model:
        cmd += ["--model", model]
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    last = ""
    for attempt in range(1, MAX_RETRIES + 1):
        if provider == "openai" and not _wait_for_endpoint():
            last = "endpoint unreachable (VPN down >180s)"; continue
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            last = ((p.stderr or p.stdout).strip().splitlines() or ["error"])[-1]
            archive_rejected_result(path, f"subprocess exit {p.returncode}: {last}")
            continue
        if _bad(path) <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac>{BAD_THRESHOLD}"
        archive_rejected_result(path, last)
    return (tag, False, f"gave up: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", type=float, default=0.7)
    ap.add_argument("--cells", choices=["levoff", "levon", "both"], default="both")
    ap.add_argument("--socials", nargs="+", default=["off", "on"])
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--model", default=None)
    ap.add_argument("--total", type=int, default=30)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default="results_phase2b")
    ap.add_argument("--leverage-ratio", type=float, default=None)
    ap.add_argument("--leverage-spread", type=float, default=None)
    ap.add_argument("--maint", type=float, default=None)
    ap.add_argument("--lev-fraction", type=float, default=None)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    set_driver_provenance(args.workers, "experiments.phase2b")

    levs = {"levoff": [False], "levon": [True], "both": [False, True]}[args.cells]
    jobs = [(s, lev, soc) for lev in levs for soc in args.socials for s in args.seeds]
    print(f"phase2b temp=0 m={args.m:g} cells={args.cells} socials={args.socials}: {len(jobs)} runs "
          f"(workers={args.workers}, lev L={args.leverage_ratio}±{args.leverage_spread} "
          f"maint={args.maint} frac={args.lev_fraction})", flush=True)

    fails, done = [], 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_run, args.out, args.m, soc, lev, s, args.provider, args.total,
                          args.model, args.leverage_ratio, args.leverage_spread,
                          args.maint, args.lev_fraction)
                for (s, lev, soc) in jobs]
        for f in as_completed(futs):
            tag, ok, msg = f.result(); done += 1
            print(f"[{done}/{len(jobs)}] {'OK ' if ok else 'FAIL'} {tag}: {msg}", flush=True)
            if not ok: fails.append((tag, msg))
    with open(os.path.join(args.out, "failures.log"), "a") as fh:
        for tag, msg in fails: fh.write(f"{tag}: {msg}\n")
    print(f"DONE. failures: {len(fails)}", flush=True)


if __name__ == "__main__":
    main()
