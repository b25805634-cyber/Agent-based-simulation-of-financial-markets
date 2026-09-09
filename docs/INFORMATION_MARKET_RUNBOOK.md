# Running the information-market pipeline

Use `.worktrees/agent-market-loop`, branch `feat/agent-market-loop`. This is
an additive development protocol, independent of the preserved a10 and 10k
acquisitions. All commands below use `ManagedRunContext`, create new immutable
run directories and make zero Provider calls. No live option exists.

## Train on verified public Teacher observations

```sh
python3 -m experiments.information_market --task train \
  --source-run ../v2-teacher-pilot/results_information_weight_scale_10k/runs/information-weight-scale-10k-live-20260825-a1 \
  --source-manifest-sha256 3a8297416e2f0d5b9959be94a76d6f93a05e443ef4f24761302f270c3ed6c3c4 \
  --epochs 120 --hidden-dim 16 --backend numpy \
  --run-id information-student-10k-20260909-a1 \
  --out results_information_market
```

NumPy is an optional local training accelerator, not a new mandatory production
dependency. `--backend python` uses the dependency-free implementation;
prediction always supports the existing standard-library model format. Backend
and version are recorded. Numerical parity is unit tested; cross-backend bit
identity is not claimed. Candidate definitions and validation-only selection
are in [INFORMATION_STUDENT.md](INFORMATION_STUDENT.md).

Training emits `student_study.json`, grouped split, evaluations for all three
candidates, source receipt, identities and reports. Reports from this first
development test must not later be reused as an untouched confirmation set.

## Simulate actual transactions with learned policy and controls

```sh
python3 -m experiments.information_market --task simulate \
  --model-run results_information_market/runs/information-student-10k-20260909-a1 \
  --agents 200 --rounds 60 --seeds 3 --profile-weights 1 1 1 1 \
  --policies selected prior random --quote-rule both --news-mode eventful \
  --run-id information-market-200-20260909-a1 \
  --out results_information_market
```

This plans 18 independent market cells: three seeds x three policies x two
quote mappings. It is not 18 humans or 216,000 independent scientific samples.
Every market uses finite resources and retains intents, constraints, fills,
account updates, raw/effective observations and source/model identities.
Within each seed, policy/quote arms share initial accounts and exogenous world.
Quote and behavioral effects require analysis across whole-market replicates.

`--news-mode neutral` is the event treatment control. An additional run with
different `--profile-weights` varies the actual information allocation while
holding financial initialization fixed at the same seed. The current daily
market's range proxy is explicitly structurally outside the original intraday
measurement semantics. Marginal train-range diagnostics do not establish joint
support or fidelity on these new states.

## Prepare human comparison tasks

```sh
python3 -m experiments.information_market --task benchmark \
  --model-run results_information_market/runs/information-student-10k-20260909-a1 \
  --run-id information-human-tasks-20260909-a1 --out results_information_market
```

This exports 24 tasks and Student predictions, not human data. A researcher can
later provide `--human-responses anonymous-responses.jsonl` in a NEW run.
Schema and recruitment/consent boundaries are in
[HUMAN_BEHAVIOR_BENCHMARK.md](HUMAN_BEHAVIOR_BENCHMARK.md). No identities,
contacts or reasoning are accepted; public scoring contains aggregates only.
There is no universal pass threshold, and no data means `pending_human_responses`.

## Progress and identity

`progress.jsonl` is durably appended with stage/round counts; completed runs
have `report.html`, `report.md`, `summary.json` and `run_manifest.json`.
`--help` and `--version` create no run. `--dry-run` validates source and plan
without fitting a model or executing the requested market experiment.

The new `information-market-identities/1.0` envelope records:

- `scientific_config_hash`: effective science, relevant numerical source
  hashes, public training sample bytes or consumed model identities;
- `model_request_config_hash`: this provider-free task's zero-call contract;
- `execution_config_hash`: backend/environment, entrypoint/reporting source,
  and the complete historical input manifest/snapshot;
- `full_effective_config_hash`: binds the three separately named identities.

These are under `information_market_identities` in the managed manifest and in
`identities.json`. The old Config identity is lifecycle instrumentation only.
Historical source hashes are not overwritten; manifests and artifact bytes are
validated again after processing. This is historical analysis, never resume.

## Preservation

Fresh verified local archives live under the workspace's
`private_backups/agent_market/{a10,information10k}/<manifest-sha>/`. Each contains
`run/`, checksums, a secret-free receipt and restore instructions. a10 verified
42/42 artifacts and 43 files; 10k verified 7/7 artifacts and 8 files. Both passed
a separate temporary restore including nanosecond mtime and private mode checks.
An older a10 copy had byte-identical contents but second-resolution mtimes; it
was retained untouched and not represented as a complete metadata-preserving
backup. `V2_A10_ARCHIVE_DEST` was unset, so a second independent medium remains
to be configured. No backup belongs in Git.

## Optional exact-support sizing (new development protocol)

Fit only new sizing heads; original action selection, representation, original
test and original mean heads are frozen. See
[INTENSITY_DISTRIBUTION.md](INTENSITY_DISTRIBUTION.md) for controls and scores.

```sh
python3 -m experiments.intensity_distribution \
  --source-run ../v2-teacher-pilot/results_information_weight_scale_10k/runs/information-weight-scale-10k-live-20260825-a1 \
  --source-manifest-sha256 3a8297416e2f0d5b9959be94a76d6f93a05e443ef4f24761302f270c3ed6c3c4 \
  --student-run results_information_market/runs/information-student-10k-20260909-a1 \
  --student-manifest-sha256 a836bc8bb18e04bcc190afdb299aaf1583ea2242b0f0503e80d4864e94adf5af \
  --epochs 120 --backend numpy --run-id intensity-distribution-20260909-a1

python3 -m experiments.information_market --task simulate \
  --model-run results_information_market/runs/information-student-10k-20260909-a1 \
  --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 \
  --sizing-policy distribution_mean --sizing-candidate selected \
  --observation-policy available_only --policies selected \
  --quote-rule independent --agents 200 --rounds 60 --seeds 3 \
  --run-id information-market-sizing-mean-20260909-a1

python3 -m experiments.information_market --task simulate \
  --model-run results_information_market/runs/information-student-10k-20260909-a1 \
  --distribution-run results_intensity_distribution/runs/intensity-distribution-20260909-a1 \
  --sizing-policy distribution_sampled --sizing-candidate selected \
  --observation-policy available_only --policies selected \
  --quote-rule independent --agents 200 --rounds 60 --seeds 3 \
  --run-id information-market-sizing-sampled-20260909-a1
```

Use `--sizing-candidate empirical` as the feature-blind distribution control.
Compare mean vs sampled with the same candidate. Intensity-linked quoting is
a compound quantity-and-price treatment, not a clean sizing comparison.
These are new run IDs: an existing directory is never overwritten. Only
registered public model artifacts are consumed; source hashes and snapshots
are verified before and after each run.

## Published human task reconstruction (no model calls)

```sh
python3 -m experiments.information_diagnostics --task human-early \
  --csv results_human_reference_inputs/lieu_pelster_v1/327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333/data_de_scopic.csv \
  --run-id human-early-tasks-20260909-a1
```

This exports 195 joint six-asset tasks from 39 published participants before
the first realized peer ranking, not 195 independent people. The exact UI is
not recovered; chart retention and net-budget interpretation are explicit
assumptions. Both the labelled task bank and prompt-only bank stay private
(0600): later prompts contain legitimate own-history information that could
reveal earlier task labels if the bank were supplied together. Public output
contains only a trajectory-free catalog. Future prediction sessions receive
one task each, never the complete bank. The no-trade baseline is synthetic,
not a Teacher result. See [HUMAN_TASK_ALIGNMENT.md](HUMAN_TASK_ALIGNMENT.md).
