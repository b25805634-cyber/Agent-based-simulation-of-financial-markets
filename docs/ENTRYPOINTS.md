# Entrypoint inventory and management policy

This document records the executable surfaces found in the Phase 1.1A codebase
and their Phase 1.1B lifecycle policy. The machine-readable source is
[nmsim/entrypoints.py](../nmsim/entrypoints.py). Importing that registry has no
provider, Git, network, or filesystem side effect.

The inventory was built from all Python main guards, argparse parsers, direct
run_sim and RunManager calls, subprocess commands, and direct JSON/CSV/PNG/text
writers. There is no pyproject.toml, setup.py, or setup.cfg, so the repository
currently has no installed console script.

## Categories and policies

| Category | Meaning |
|---|---|
| official_managed_research_entrypoint | May produce a formal research artifact, but only through the declared managed boundary. |
| test_or_diagnostic_entrypoint | Regression, inspection, benchmark, or offline audit; output is not a provenance-complete research result. |
| library_api | Caller-controlled low-level API; it does not claim managed provenance. |
| deprecated_or_unsupported | Historical divergent implementation or wrapper; not allowed for new formal research. |

Management policies are more precise than a managed/unmanaged boolean:

- direct_managed: one simulation leaf owns one ManagedRunContext.
- delegated_managed_driver: a parent experiment attempt counts simulation-run
  units; each child simulation is a separate managed experiments.run_seed.
- analysis_managed: provider-free derived analysis hashes its input set and
  writes immutable canonical outputs.
- null_context: intentional no-provenance/no-filesystem simulation or stdout
  diagnostic. It cannot be presented as a formal research run.
- dedicated_audit: the existing immutable offline reparse-audit lifecycle.
- library_unmanaged: direct Python API with no run lifecycle.
- unsupported: no supported formal execution path.

## Official managed research entrypoints

### Simulation leaves

| Entrypoint | Actual call chain | Before Phase 1.1B | Writes output | Provider | Formal research after 1.1B |
|---|---|---|---|---|---|
| python3 -m nmsim.run (nmsim/run.py) | argparse → cfg_from_args → run → RunManager.create → record/replay wrapper → run_sim → export | Managed, but lifecycle logic is embedded in run | Run directory, manifest/events, six canonical files and compatible flat links | Direct record or offline replay | Yes, direct_managed |
| nmsim.run.run(config, ...) | validated Config → same path as CLI | Managed high-level Python API with filesystem effects | Same as CLI | Direct record or offline replay | Yes, direct_managed; this is not the low-level library API |
| python3 -m experiments.run_seed | argparse → Config assembly → RunManager.create → _live → run_sim, or provider-free CSV reuse → export | Managed, but duplicates lifecycle/provider/export logic | Canonical result, recording/manifest/events and non-overwriting legacy flat JSON | Direct, replay, or none in CSV reuse | Yes, direct_managed |
| python3 -m experiments.capture_traces | argparse → Config → build_llm → bare run_sim → JSON | Unmanaged | Ordinary trace JSON containing private reasoning | Direct | Yes only after direct_managed; rationale must be a 0600 private artifact |

Evidence in the Phase 1.1A source:

- nmsim/run.py:225-238 creates RunManager and calls run_sim; lines 264-288
  write and finalize canonical outputs.
- experiments/run_seed.py:341-360 creates RunManager and executes live or
  CSV-reuse mode; lines 372-404 write canonical/legacy results and finish.
- experiments/capture_traces.py:41-58 builds a provider, directly calls run_sim,
  and writes reasoning into an unmanaged JSON.

### Experiment drivers

Each driver below is a formal experiment-level entrypoint. It must have one
managed parent attempt whose completion unit is **simulation runs**. Provider
access occurs only in separately managed run_seed children. A successful
child's agent_decisions.completed must never be reported as driver
honest_n_runs.

| Entrypoint | Jobs delegated to run_seed | Phase 1.1A driver output | Provider | Formal research after 1.1B |
|---|---|---|---|---|
| experiments.drive | gain × seed | child results, append failures.log, rejected-result archives | Indirect | Yes, delegated_managed_driver; legacy gain semantics retain their documented caveat |
| experiments.grid2x2 | four news/social cells × seeds | child results and failures.log; realized N is stdout only | Indirect | Yes; summary must store planned/started/completed/failed/honest_n_runs per cell |
| experiments.sweep | calibration or composition/social/seed jobs | child results and failures.log | Indirect | Yes |
| experiments.ablate | influencer arm × seed | child results and failures.log | Indirect | Yes |
| experiments.lev2x2 | leverage-on social cells × seed | child results and failures.log | Indirect | Yes |
| experiments.phase2b | leverage/social cells × seed | child results and failures.log | Indirect | Yes |
| experiments.critsweep | leverage level × seed | child results and failures.log | Indirect | Yes |

All seven build a python -m experiments.run_seed command and use
subprocess.run. Representative evidence is experiments/grid2x2.py:54-83; the
other command sites are drive.py:75, sweep.py:49, ablate.py:40, lev2x2.py:44,
phase2b.py:46, and critsweep.py:44. Before Phase 1.1B only each child was
managed. The current parent lifecycle now writes `driver_summary.json`,
unitized per-cell/total completion, and a 0600 private failure-detail file;
each simulation remains a separately managed child.

### Derived research outputs

These commands do not call a Provider or run a market. They nevertheless
produce formal derived artifacts, so an analysis_managed attempt must record
the complete input set and hashes, calculation command, failures, and immutable
outputs. Making them managed does **not** repair or endorse known statistical
limitations, and Phase 1.1B must not alter their numerical formulas.

| Entrypoint | Actual input → output | Phase 1.1A overwrite behavior | Formal research after 1.1B |
|---|---|---|---|
| experiments.aggregate_grid | per-cell result JSON → grid_summary.json and envelope_2x2.png | write/savefig replace flat paths (aggregate_grid.py:112-168) | Yes after analysis management; retain independent-sample/health-filter caveats |
| experiments.aggregate_seeds | gain/seed JSON → summary JSON and envelope PNG | replaces flat outputs (aggregate_seeds.py:80-118) | Yes after analysis management; historical gain semantics remain explicit |
| experiments.aggregate_sweep | paired sweep JSON → stdout statistics and sweep_main.png | replaces plot (aggregate_sweep.py:125-126) | Yes after analysis management; do not silently change historical CI |
| experiments.calib_n | calibration replicate JSON → calib_N.txt | replaces file (calib_n.py:54-55) | Yes; this file becomes scientific input to the next stage |
| experiments.lev_analyze | leverage JSON → contrasts and leverage PNG | replaces plot (lev_analyze.py:183-205) | Yes after analysis management |
| experiments.critsweep_analyze | critsweep + Phase2b baseline JSON → CI and PNG | replaces plot (critsweep_analyze.py:84-102) | Yes after analysis management |

For analysis attempts, simulation/round/decision/request planned counts are zero
or null as appropriate. Input-run counts require an explicit unit and must not
be confused with independent statistical N.

## Test and diagnostic entrypoints

| Entrypoint | Actual purpose | Writes files | Provider | Formal research allowed |
|---|---|---|---|---|
| experiments.repro_check | three PYTHONHASHSEED subprocesses → bare Mock run_sim → compare stdout | No | No | No; null_context deterministic regression |
| experiments.leverage_demo | two Mock configs → bare run_sim → print comparison | No | No | No; null_context mechanism smoke |
| nmsim.reparse_audit | historical recording/events → current parser → immutable audit directory | Public summary/diff and 0600 private rationale diff | No | No; dedicated_audit, not replay or a market run |
| nmsim.prompts | construct and print an example prompt | No | No | No |
| experiments.bench_concurrency | direct AsyncOpenAI client → endpoint benchmark | No | **Direct diagnostic network access** | No; excluded from offline regression |
| experiments.aggregate_mechanism | load trace pairs → print mechanism summary | No | No | No; stdout-only inspection |
| experiments.analyze_traces | load trace pair → print actions/public takes | No | No | No |
| experiments.flow_decomp | load trace/result JSON → print flow decomposition | No | No | No |
| experiments.ablation | load result JSON → print paired statistics | No | No | No; historical calculation is not a provenance-complete output |
| experiments.additive_test | load result JSON → print regressions and CI | No | No | No |
| python3 -m unittest discover -s tests -v | test discovery and helpers/low-level APIs | Temporary files only | No real Provider | No |

The eleven test files that also have standalone unittest.main guards are:

- tests/test_phase1_integration.py
- tests/test_privacy_invariant.py
- tests/test_recording_schema_config_ingestion.py
- tests/test_config_replay_contract.py
- tests/test_provenance_unit.py
- tests/test_reparse_audit.py
- tests/test_recording_unit.py
- tests/test_replay_contract.py
- tests/test_managed_run_context.py
- tests/test_managed_entrypoints.py
- tests/test_managed_analysis_entrypoints.py

They share the test-suite registry policy; individual execution does not create
a formal research run.

## Library API

nmsim.sim.run_sim(config, llm, tracker, event_logger=None, run_id=None) is the
low-level simulation API (nmsim/sim.py:54). The caller supplies every runtime
object. It must remain able to run entirely in memory and must not:

- create a directory or manifest;
- inspect Git;
- read credentials or construct a Provider;
- claim provenance completeness;
- change its trajectory because no managed context was supplied.

Tests, repro_check, and leverage_demo may use it with NullRunContext/no-op
observation. A result from this path is not a formal managed research result.

aggregate_grid.aggregate() and aggregate_seeds.aggregate() are callable helpers
with file side effects. Direct Python calls remain library use, not a formal
analysis run; their official CLIs must supply the managed analysis boundary.

## Deprecated or unsupported entrypoints

| Entrypoint | Why unsupported | Writes output | Provider | Formal research |
|---|---|---|---|---|
| python3 narrative_market_sim.py | Independent old Config, Persona, Mock, parser, market, and loop; package code is not called | Hard-coded /mnt/user-data/outputs CSV and PNG | Mock only | No |
| bash run_pipeline.sh | Historical wrapper hard-codes real-provider calibration/sweep, results_sweep, and root append log; no parent lifecycle | sweep_pipeline.log and results_sweep artifacts | Indirect real Provider | No; invoke supported managed stages explicitly |

Attaching a manifest to narrative_market_sim.py would not make its divergent
simulation supported. Phase 1.1B therefore documents it without refactoring or
silently redirecting it.

## Bootstrap and lifecycle implications

At the Phase 1.1A baseline both production CLIs perform full argparse before a
manager exists (nmsim/run.py:420-427 and experiments/run_seed.py:217-263).
Consequently unknown flags, missing required arguments, invalid choices, and
type errors left no failed manifest. Phase 1.1B now provides:

1. A side-effect-free help/version check; neither creates a run.
2. A minimal bootstrap parser for safe output root, optional run id, command
   identity, and replay locator.
3. A provisional failed managed attempt when full validation fails after those
   values are safe.
4. Full argparse/strict Config/provider/replay validation inside the attempt,
   with an explicit failure_stage.

Invalid output roots, path-traversing/invalid run ids, inability to create a
safe directory, and input from which output location cannot be determined
remain pre-provenance failures. They must emit only a sanitized
provenance_not_created_reason.

## Registry-based enforcement

Tests import ENTRYPOINTS/official_entrypoints() and assert that every
official entry has direct_managed, delegated_managed_driver, or
analysis_managed policy. This is stronger than source-string scanning and is
paired with behavior tests that invoke managed CLI paths. A lightweight discovery audit also compares
main-guard modules against the registry to detect a newly added executable; it
must not be the only enforcement mechanism.

Important remaining risks:

- A flat result currently called cached by a driver is accepted by filename and
  health fraction, not by manifest/schema/fingerprint identity.
- archive_rejected_result moves flat compatibility files; canonical managed run
  directories must never be moved or overwritten.
- Driver retries are attempts, while accepted child simulations are runs; both
  need separate counters.
- Partial files and plots do not make a failed analysis or simulation a
  successful sample.
- Historical pre-1.1B capture_traces artifacts mixed private rationale with
  ordinary JSON; new managed traces are 0600, but old artifacts are not
  retroactively rewritten.
- Managed provenance records what calculation ran; it does not cure known
  inferential limitations of legacy analyzers.
