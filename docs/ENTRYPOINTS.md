# Entrypoint inventory and management policy

This document records the executable surfaces found through the Wave 0
endpoint-stochasticity addition and their current lifecycle and result-reuse policy. The machine-readable source is
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
| python3 -m experiments.run_seed | argparse → Config assembly → ManagedRunContext → live `run_sim`, strict replay, or provider-free `--price-csv` historical analysis → export | Managed, but previously duplicated lifecycle/provider/export logic | Canonical result, recording/manifest/events and non-overwriting legacy flat JSON | Direct, replay, or none in historical CSV analysis | Yes, direct_managed; `--price-csv` uses `run_kind=analysis` and is not child-run reuse |
| python3 -m experiments.capture_traces | argparse → Config → build_llm → bare run_sim → JSON | Unmanaged | Ordinary trace JSON containing private reasoning | Direct | Yes only after direct_managed; rationale must be a 0600 private artifact |
| python3 -m experiments.model_qualification | bootstrap → frozen protocol/fixture/rubric validation → Provider/real-use guard → ManagedRunContext → selected cases or dry-run → public/private export | New in Phase 1.2A | Managed qualification manifest, public case/aggregate output and 0600 private case records | Mock/Fake; experimental CodexExec only behind explicit future-use confirmation; dry-run constructs none | Yes, direct_managed with `run_kind=model_qualification`; it is not a market simulation |
| python3 -m experiments.endpoint_stochasticity | bootstrap → validate frozen qualification universe and 48-to-6 selection → Provider/live guard → ManagedRunContext → dry-run or 1080-sample grid plus separate two-call seed probe → public/private export | New in Wave 0 | Dry-run `dry_run_summary.json`, or full `endpoint_stochasticity_summary.json`, `endpoint_samples.jsonl`, and mode-0600 `private_endpoint_records.jsonl`, in a managed run | Fake offline; real OpenAI-compatible only with `--live`; dry-run constructs none | Yes, direct_managed with `run_kind=endpoint_stochasticity`; non-market noise diagnostic with zero simulation honest-N |

Evidence in the Phase 1.1A source:

- nmsim/run.py:225-238 creates RunManager and calls run_sim; lines 264-288
  write and finalize canonical outputs.
- experiments/run_seed.py creates a managed attempt and executes live/replay or
  explicit provider-free historical CSV analysis; the latter records input
  hashes and is not accepted as a resumed child simulation.
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

Phase 1.2A also routes every resume candidate through the centralized
`nmsim.result_reuse` policy. A path or healthy-looking flat JSON is not
completion evidence: only a finished, identity-matching managed child whose
registered artifacts re-hash correctly may increment `reused_runs`,
`completed_runs`, or `honest_n_runs`. Summaries now report `planned_runs`,
`started_runs`, `executed_runs`, `reused_runs`,
`reuse_candidates_examined`, `reuse_candidates_rejected`, `completed_runs`,
`failed_runs`, and `honest_n_runs`, plus rejection counts by stable reason
code. A rejected candidate is audited but not counted; the driver launches a
new immutable child attempt rather than overwriting or relabeling the old
result. A Git commit difference alone is allowed when every scientific,
runtime-config, model-request and artifact identity still matches.

### Derived research outputs

These commands do not call a Provider or run a market. They nevertheless
produce formal derived artifacts, so an analysis_managed attempt must record
the complete input set and hashes, calculation command, failures, and immutable
outputs. Making them managed does **not** repair or endorse known statistical
limitations, and Phase 1.1B must not alter their numerical formulas.

| Entrypoint | Actual input → output | Phase 1.1A overwrite behavior | Formal research after 1.1B |
|---|---|---|---|
| experiments.aggregate_grid | per-cell result JSON → grid_summary.json and envelope_2x2.png | write/savefig replace flat paths (aggregate_grid.py:112-168) | Yes after analysis management; retain independent-sample/health-filter caveats |
| experiments.aggregate_multi_event | explicit execution-plan selection + identity-validated managed children + hashed catalog/event inputs → complete-case event/cross-event estimates, multi_event_summary.json and three-panel PNG | New in Wave 1 | Yes, analysis_managed and Provider-free; live adherence partitions all 144 preregistered slots, while a smaller mock subset is explicitly non-adherent engineering output with no realism-claim eligibility; no glob or legacy-flat input |
| experiments.aggregate_seeds | gain/seed JSON → summary JSON and envelope PNG | replaces flat outputs (aggregate_seeds.py:80-118) | Yes after analysis management; historical gain semantics remain explicit |
| experiments.aggregate_sweep | paired sweep JSON → stdout statistics and sweep_main.png | replaces plot (aggregate_sweep.py:125-126) | Yes after analysis management; do not silently change historical CI |
| experiments.calib_n | calibration replicate JSON → calib_N.txt | replaces file (calib_n.py:54-55) | Yes; this file becomes scientific input to the next stage |
| experiments.lev_analyze | leverage JSON → contrasts and leverage PNG | replaces plot (lev_analyze.py:183-205) | Yes after analysis management |
| experiments.critsweep_analyze | critsweep + Phase2b baseline JSON → CI and PNG | replaces plot (critsweep_analyze.py:84-102) | Yes after analysis management |

For analysis attempts, simulation/round/decision/request planned counts are zero
or null as appropriate. Input-run counts require an explicit unit and must not
be confused with independent statistical N.

Phase 1.2A makes a second boundary explicit. `experiments.run_seed
--price-csv` and the six managed analyzers may read explicitly selected
historical flat files, but record each path, size and SHA-256 with
`provenance_class=legacy_unverified_input` and readable/failed/unverified
counts. Those files are analysis inputs, not resumed child runs: they never
increase `executed_runs`, `reused_runs`, or `honest_n_runs`, and no synthetic
child manifest is created. Diagnostic analyzers remain non-provenance-complete
historical inspection paths.

## Model qualification entrypoint

`python3 -m experiments.model_qualification` is an official managed research
entrypoint with `run_kind=model_qualification`. The version-controlled protocol
combines six existing Personas with eight frozen Observation fixtures, yielding
48 stable case identities. It records engineering metrics and relative
behavioral diagnostics; it does not prescribe one correct trading action,
clear a market, create a price path, or contribute a simulation replicate.

Mock and the in-process `fake_test_provider` remain offline paths. Phase
1.2B-CX1 registers experimental `codex_exec` for a future explicitly authorized
pilot, but a live case requires the Provider id, real-use confirmation, bounded
case count (plus a matching count confirmation above one), and one worker.
`--dry-run` constructs no Provider; Provider calls remain zero and network
access is false. This phase runs only fake-executable tests and does not consume
Codex quota. The capability registry is a descriptive adapter contract rather
than a model-quality score. See
[MODEL_QUALIFICATION_PROTOCOL.md](MODEL_QUALIFICATION_PROTOCOL.md),
[PROVIDER_CAPABILITIES.md](PROVIDER_CAPABILITIES.md), and
[CODEX_QUALIFICATION_RUNBOOK.md](CODEX_QUALIFICATION_RUNBOOK.md).

## Endpoint stochasticity entrypoint

`python3 -m experiments.endpoint_stochasticity` is an official managed
research entrypoint with `run_kind=endpoint_stochasticity`. It reuses the
validated qualification case identities without changing their frozen
Prompts, Personas, fixtures, or parser. Its versioned six-case panel covers
cascade fuel, dampener/narrative-immune, and spark roles from the 48-case
qualification universe.

The main plan is six cases x temperatures `{0, 0.3}` x `K=30` x client
concurrency `{1, 8, 32}` = 1080 logical endpoint samples. A two-call same-seed
probe is separately labeled and accounted, rather than being folded into the
main-grid denominator. Public aggregation compares raw-response hashes for
pairwise within-case byte agreement and reports pooled within-case sample
sigma for parsed sentiment and signed order (`+quantity` buy, `0` hold,
`-quantity` sell).

The dry-run validates and writes the exact 48-to-6 plan to
`dry_run_summary.json` while constructing no Provider and reporting zero
network calls; it does not fabricate full-run sample artifacts.
`fake_test_provider` executes the
full grid offline. OpenAI-compatible execution is rejected before Provider
construction unless `--live` is explicit. Public artifacts contain response
hashes and explicitly public parsed fields; full Prompts, raw responses,
private reasoning, and detailed failures exist only in the mode-`0600` private
record. The 1080 main samples, two seed-probe calls, completed raw responses,
parsed decisions, failures, and derived pairs retain distinct units, and the
run contributes `honest_n_runs=0` because it never clears a market.

See [ENDPOINT_STOCHASTICITY.md](ENDPOINT_STOCHASTICITY.md) for exact commands,
artifact schemas, estimators, N/K power interpretation, limitations, and the
no-scientific-semantics-change statement.

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

The eighteen test files that also have standalone unittest.main guards are:

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
- tests/test_driver_result_reuse_accounting.py
- tests/test_result_reuse.py
- tests/test_provider_capabilities.py
- tests/test_model_qualification.py
- tests/test_codex_exec_provider.py
- tests/test_endpoint_stochasticity.py
- tests/test_aggregate_multi_event.py

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

- Pre-Phase-1.2A flat results without a verifiable managed child manifest are
  intentionally ineligible for formal resume, even when scientifically useful
  as explicitly marked historical-analysis inputs.
- Result-reuse policy authenticates identities and registered bytes; it does
  not establish the causal validity of an experiment or make a real Provider
  deterministic.
- Driver retries are attempts, while accepted child simulations are runs; both
  need separate counters.
- Partial files and plots do not make a failed analysis or simulation a
  successful sample.
- Historical pre-1.1B capture_traces artifacts mixed private rationale with
  ordinary JSON; new managed traces are 0600, but old artifacts are not
  retroactively rewritten.
- Managed provenance records what calculation ran; it does not cure known
  inferential limitations of legacy analyzers.
- Qualification protocol/rubric 1.1 and visibility contract 1.0 are frozen
  before any real-model call. Because the current real User Prompt does not
  expose the fixture's numeric fundamental value, the corresponding diagnostic
  is machine-readably `not_scored`; raw actions/sentiments remain descriptive
  evidence only.
