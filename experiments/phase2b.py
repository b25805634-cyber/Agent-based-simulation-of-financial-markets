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
            archive_rejected_result(
                path,
                f"subprocess exit {p.returncode}; details are in the managed driver private log",
            )
            continue
        if _bad(path) <= BAD_THRESHOLD:
            return (tag, True, f"{p.stdout.strip()} (attempt {attempt})")
        last = f"bad_frac>{BAD_THRESHOLD}"
        archive_rejected_result(path, last)
    return (tag, False, f"gave up: {last}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_phase2b",
            command_identity="experiments.phase2b",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--version", action="version", version="experiments.phase2b phase-1.1b")
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
    try:
        args = ap.parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")
    set_driver_provenance(args.workers, "experiments.phase2b")

    levs = {"levoff": [False], "levon": [True], "both": [False, True]}[args.cells]
    jobs = [(s, lev, soc) for lev in levs for soc in args.socials for s in args.seeds]
    print(f"phase2b temp=0 m={args.m:g} cells={args.cells} socials={args.socials}: {len(jobs)} runs "
          f"(workers={args.workers}, lev L={args.leverage_ratio}±{args.leverage_spread} "
          f"maint={args.maint} frac={args.lev_fraction})", flush=True)

    failures, summary = run_managed_driver_jobs(
        out_root=args.out,
        command_identity="experiments.phase2b",
        jobs=jobs,
        workers=args.workers,
        cell_name=lambda job: f"{job[2]}_{'lev' if job[1] else 'cash'}",
        seed_identity=lambda job: job[0],
        is_healthy=lambda job: (
            os.path.exists(_path(args.out, args.m, job[2], job[1], job[0]))
            and _bad(_path(args.out, args.m, job[2], job[1], job[0]))
            <= BAD_THRESHOLD
        ),
        run_job=lambda job: _run(
            args.out,
            args.m,
            job[2],
            job[1],
            job[0],
            args.provider,
            args.total,
            args.model,
            args.leverage_ratio,
            args.leverage_spread,
            args.maint,
            args.lev_fraction,
        ),
    )
    print(f"DONE. failures: {len(failures)}; driver summary: {summary}", flush=True)


if __name__ == "__main__":
    main()
