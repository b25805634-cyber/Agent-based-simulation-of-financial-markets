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
import hashlib
import os
import sys
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from nmsim.managed_cli import (
    BootstrapCLIError,
    ManagedCLIError,
    RaisingArgumentParser,
    bootstrap_cli,
    fail_cli,
)
from experiments.drive import _wait_for_endpoint, BAD_THRESHOLD, MAX_RETRIES
from experiments.driver_utils import (
    DriverJobResult,
    ManagedDriverCompletion,
    archive_rejected_result,
    set_driver_provenance,
)

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


def _private_output(text, limit=32768):
    """Bound captured child output while retaining an integrity hash privately."""

    value = text or ""
    payload = {
        "text": value[:limit],
        "truncated": len(value) > limit,
    }
    if len(value) > limit:
        payload["sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        payload["original_characters"] = len(value)
    return payload


def _run(label, news, social, seed, provider, temp, out):
    tag = f"{label} s{seed}"
    path = os.path.join(out, f"{label}_s{seed}.json")
    if os.path.exists(path):
        bad = _bad_frac(out, label, seed)
        if bad <= BAD_THRESHOLD:
            return DriverJobResult(
                cell=label,
                tag=tag,
                seed=seed,
                ok=True,
                source="reused",
                attempts=0,
            )
        archive_rejected_result(path, f"pre-existing bad_frac={bad} > {BAD_THRESHOLD}")
    cmd = [sys.executable, "-m", "experiments.run_seed", "--seed", str(seed),
           "--provider", provider, "--out", out,
           "--news", news, "--social", social, "--label", label]
    if temp is not None:
        cmd += ["--temp", str(temp)]
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    last_code = "child_run_not_started"
    private_details = []
    for attempt in range(1, MAX_RETRIES + 1):
        if provider == "openai" and not _wait_for_endpoint():
            last_code = "endpoint_unreachable"
            private_details.append({
                "attempt": attempt,
                "reason_code": last_code,
                "detail": "configured endpoint did not become reachable",
            })
            continue
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            last_code = "subprocess_exit"
            private_details.append({
                "attempt": attempt,
                "reason_code": last_code,
                "returncode": p.returncode,
                "stdout": _private_output(p.stdout),
                "stderr": _private_output(p.stderr),
            })
            archive_rejected_result(
                path,
                f"subprocess exit {p.returncode}; details are in the managed driver private log",
            )
            continue
        bad = _bad_frac(out, label, seed)
        if bad <= BAD_THRESHOLD:
            return DriverJobResult(
                cell=label,
                tag=tag,
                seed=seed,
                ok=True,
                source="executed",
                attempts=attempt,
            )
        last_code = "health_gate_rejected"
        private_details.append({
            "attempt": attempt,
            "reason_code": last_code,
            "bad_frac": bad,
            "threshold": BAD_THRESHOLD,
        })
        archive_rejected_result(
            path, f"bad_frac={bad} > {BAD_THRESHOLD}; retrying"
        )
    return DriverJobResult(
        cell=label,
        tag=tag,
        seed=seed,
        ok=False,
        source="failed",
        attempts=MAX_RETRIES,
        reason_code=last_code,
        private_details=tuple(private_details),
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap = bootstrap_cli(
            argv,
            default_out="results_2x2",
            command_identity="experiments.grid2x2",
        )
    except BootstrapCLIError as error:
        print(
            "provenance_not_created_reason={}".format(type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(2)

    ap = RaisingArgumentParser(allow_abbrev=False)
    ap.add_argument("--version", action="version", version="experiments.grid2x2 phase-1.1b")
    ap.add_argument("--seeds", type=int, nargs="+", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--temp", type=float, default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", default="results_2x2")
    try:
        args = ap.parse_args(argv)
    except (ManagedCLIError, OSError, ValueError) as error:
        fail_cli(bootstrap, error, failure_stage="config_validation")

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

    cell_plans = {label: len(args.seeds) for label, _, _ in CELLS}
    managed = ManagedDriverCompletion.create(
        out_root=args.out,
        command_identity="experiments.grid2x2",
        cell_plans=cell_plans,
        worker_count=args.workers,
    )

    fails, n = [], 0
    with managed:
        for label, _, _ in CELLS:
            reused = sum(1 for seed in args.seeds if _healthy(args.out, label, seed))
            if reused:
                managed.record_reused(label, reused)

        def run_managed(job):
            label, news, social, seed = job
            managed.record_started(label)
            return _run(
                label, news, social, seed, args.provider, args.temp, args.out
            )

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(run_managed, job): job for job in jobs}
            for future in as_completed(futures):
                label, _, _, seed = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    result = DriverJobResult(
                        cell=label,
                        tag=f"{label} s{seed}",
                        seed=seed,
                        ok=False,
                        source="failed",
                        attempts=0,
                        reason_code="driver_job_exception",
                        private_details=({
                            "exception_type": type(error).__name__,
                            "message": str(error),
                        },),
                    )

                n += 1
                if result.ok:
                    managed.record_completed(
                        result.cell, reused=result.source == "reused"
                    )
                    message = (
                        "accepted existing result"
                        if result.source == "reused"
                        else f"completed after {result.attempts} attempt(s)"
                    )
                else:
                    managed.record_failed(result)
                    fails.append(result)
                    message = (
                        f"gave up after {result.attempts} attempt(s): "
                        f"{result.reason_code or 'child_run_failed'}"
                    )
                print(
                    f"[{n}/{len(jobs)}] {'OK ' if result.ok else 'FAIL'} "
                    f"{result.tag}: {message}",
                    flush=True,
                )

        managed.context.set_stage("result_export")
        failures_log = os.path.join(args.out, "failures.log")
        with open(failures_log, "a", encoding="utf-8") as fh:
            for result in fails:
                fh.write(
                    f"{result.tag}: gave up after {result.attempts} attempts: "
                    f"{result.reason_code or 'child_run_failed'}\n"
                )

        # Realized N is accepted child simulation runs, never decision counts.
        print("\nrealized N per cell:", flush=True)
        for label, _, _ in CELLS:
            cell = managed.cells[label]
            print(
                f"  {label}: {cell['honest_n_runs']}/{cell['planned_runs']}",
                flush=True,
            )
        summary_path = managed.finish(legacy_failures_log="failures.log")

    print(
        f"ALL DONE. failures: {len(fails)} (see {args.out}/failures.log); "
        f"driver summary: {summary_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
