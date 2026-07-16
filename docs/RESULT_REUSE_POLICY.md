# Result Reuse Policy

Phase 1.2A defines result-reuse policy `1.0`. Its purpose is narrow: an
experiment driver may count an old child simulation as part of the current
experiment only after the child's managed lifecycle, scientific identity,
model-request identity, and artifact bytes have all been verified. A filename
or a plausible-looking metric is not a run identity.

The sealed Phase 1.2A policy reference is
`phase1.2a-model-foundation-v1` at
`a358a940c618b505fa3996d4e6acd22f617c6edf`. Commit
`3594909b9cf72d97a835539770ba32486090d71f` is an earlier implementation
commit, not the sealed tag target.

This is an experiment-orchestration safety gate. It does not change Agent
behavior, prompts, market clearing, accounting, contagion, leverage, validation
metrics, or the meaning of an already-produced historical result.

## Audit of result-reading paths

The table distinguishes **driver resume** from **historical analysis input**.
The latter may be scientifically useful, but it is not evidence that a child
run with the current Provider/model/configuration has executed.

| Entrypoint / path | Phase 1.1 condition or behavior | Risk before 1.2A | Phase 1.2A rule | Class | Legacy flat files |
|---|---|---|---|---|---|
| `experiments.drive` | `g{gain}_s{seed}.json` existed and `health.bad_frac <= 0.15` | A result from another Provider, model, config, prompt, or code version could be called cached | Assess the managed child manifest with policy 1.0, then apply the driver health gate; otherwise launch a new immutable child | Driver resume | Never sufficient for reuse |
| `experiments.grid2x2` | `{cell}_s{seed}.json` existed and passed the same health threshold; bespoke code counted it as reused | Same identity confusion; manual counting also bypassed the central gate | Use the same centralized assessor and run-level accounting as every other driver | Driver resume | Never sufficient for reuse |
| `experiments.sweep` | `m{m}_real_{on/off}_s{seed}[_r{rep}].json` existed and was healthy | Sampling repeat, population, rounds, Provider/model, and request settings were not authenticated | Build the expected identity from the final effective Config and validate its managed candidate | Driver resume | Never sufficient for reuse |
| `experiments.ablate` | Arm filename existed and was healthy | An arm label did not prove the influencer/social configuration or model identity | Validate the complete child identity before health acceptance | Driver resume | Never sufficient for reuse |
| `experiments.lev2x2` | Leverage filename existed and was healthy | A label did not prove leverage parameters, population, Provider/model, or input identity | Validate the complete child identity; rejected candidates do not count | Driver resume | Never sufficient for reuse |
| `experiments.phase2b` | Cell filename existed and was healthy | `temp=0` and a label were not proof of deterministic Provider output or matching leverage settings | Validate source/config/request/artifacts; temperature remains one request field, not a determinism claim | Driver resume | Never sufficient for reuse |
| `experiments.critsweep` | Critical-level filename existed and was healthy | Ratio encoded in a filename did not prove spread, maintenance, fraction, model, or code identity | Validate the complete expected identity, then the health gate | Driver resume | Never sufficient for reuse |
| `experiments.driver_utils` | Centralized honest-N and retries, but Phase 1.1B accepted an `is_healthy` callback | A driver could still equate file health with identity | `assess_reuse` must return a policy decision; only `reusable=true` increments reuse/completed/honest-N | Driver infrastructure | Bare flat input returns `legacy_flat_result_unverified` |
| `experiments.run_seed --price-csv` | Explicitly recomputed metrics from saved CSV files without a Provider | The word "reuse" can be confused with resuming the original simulation | This remains offline derived-result computation with hashed inputs; it is not child simulation reuse and cannot satisfy a current model experiment slot | Historical/derived input | Allowed only as explicit input, never as a resumed child |
| `aggregate_grid`, `aggregate_seeds`, `aggregate_sweep` | Globs and reads historical flat result JSON | Files may lack a manifest or verifiable model/config identity | Hash and record every explicit input as `legacy_unverified_input`; report readable/failed/unverified counts separately | Managed historical analysis | Allowed as marked input |
| `calib_n`, `lev_analyze`, `critsweep_analyze` | Globs/reads calibration, leverage, critical-sweep and baseline JSON | The analysis is reproducible at the file level, but the upstream run identity may be unverifiable | Same explicit legacy-input provenance; never synthesize child manifests | Managed historical analysis | Allowed as marked input |
| `aggregate_mechanism`, `analyze_traces`, `flow_decomp`, `ablation`, `additive_test` | Reads flat JSON and prints diagnostics | These diagnostic commands do not form provenance-complete analysis runs | Remain historical inspection only; their inputs are not reused experiment children | Diagnostic historical analysis | Readable, but not a formal reuse claim |
| `RunManager.publish_legacy_links` | Publishes success-only flat compatibility symlinks to an immutable run | A copied file, retargeted link, or link escaping the results root could impersonate a child | Resolve and validate the link, its manifest-declared target, allowed root, and child run directory | Compatibility projection | A verified managed link may locate a child; a regular flat file may not |

The deprecated `run_pipeline.sh` and unsupported single-file simulator do not
gain reuse authority from this policy. `repro_check`, low-level `run_sim`, and
other Null/diagnostic paths do not produce provenance-complete reusable runs.

## Central identity types

`nmsim.result_reuse` owns the contract. Drivers must not reproduce subsets of
it. The main representations are:

- `ChildRunIdentity`: parsed from one existing managed child manifest;
- `ExpectedRunIdentity`: derived before execution from the driver's final
  effective Config and current reviewed source/request contracts, without
  constructing a Provider;
- `ReusableRunCandidate`: a supplied path plus its allowed result root;
- `ReuseDecision`: a public-safe acceptance/rejection, verified-artifact count,
  reason codes, and cross-commit flag.

The identity contains the following components.

| Component | Fields / checks |
|---|---|
| Lifecycle | `run_id`, `run_kind`, command/entrypoint identity, manifest schema, status, `managed_run_completed`, `outputs_complete`, empty failure stage, simulation completion, decision completion, recording schema |
| Scientific source | scientific component fingerprint, Decision parser schema/source hash, event schema, Prompt hash, Persona hash, simulation-core hash |
| Runtime science | config-hash schema, scientific-config hash, normalized scientific-input identity, `reference_path` content hash, scenario-definition hash, population-contract identity and realized population completeness, seed |
| Model request | requested and resolved Provider, requested and resolved model, credential-free endpoint identity, model-request-config hash, temperature, max tokens, cache policy; for CodexExec also CLI/binary identity, explicit reasoning effort, wrapper/schema identities, forced login/approval/personality, read-only/ephemeral/history/reasoning settings, and the complete no-tools configuration |
| Results | driver-required canonical artifact names, every manifest-registered artifact's relative path, registration state, SHA-256 and byte size, `experiment_result.json` identity consistency, declared legacy-link identities |
| Git provenance | commit, dirty state, and diff hash; these are retained for audit but the commit alone is not the compatibility gate |

No API key, Authorization value, complete private Prompt, private rationale, or
raw model response is part of a public reuse decision.

Codex reasoning effort is not added to the frozen scientific Config dataclass
or `full_effective_config_hash`. It is recorded as a separately hashed Provider
request option and incorporated into `model_request_config_hash`, so reuse
still fails closed when it differs.

## Acceptance contract

Policy `result_reuse_policy_version = "1.0"` accepts a candidate only when all
of the following hold:

1. The candidate resolves safely inside the explicitly allowed result root.
2. A `run_manifest.json` exists, parses, and uses the current compatible
   manifest schema.
3. The run is a completed managed simulation: status is `finished`,
   `managed_run_completed=true`, `outputs_complete=true`, failure stage is
   empty, one simulation was started/completed with no failed simulation, and
   the planned/completed decision counts are internally complete.
4. Recording schema is the current formal schema (`1.2`). This policy does not
   upgrade or guess the identity of a legacy recording.
5. Command identity and run kind match the intended child entrypoint.
6. Scientific fingerprint, parser identity, event schema, simulation-core
   hash, Prompt hash, and Persona hash match.
7. Scientific-config, scenario, scientific input/reference content, seed, and
   population identities match.
8. Model-request hash, requested/resolved Provider and model, endpoint
   identity, temperature, max tokens, and cache policy match. For CodexExec,
   the CLI/binary, explicit reasoning effort, wrapper/schema, and full
   no-tools/history/reasoning/personality contract must also match.
9. Every driver-required canonical artifact is registered. Every registered
   canonical artifact is resolved inside the immutable child directory and is
   re-hashed; its bytes and size must match the manifest.
10. When present, `experiment_result.json` agrees with the manifest on run id,
    seed, model, and completion counts.
11. A compatibility link is a symlink declared by that manifest, remains under
    the allowed result root, and resolves into that same immutable child run.
12. Any driver-specific health gate also passes. Health is an additional
    quality check, never a substitute for identity.

Validation is read-only. A rejected candidate is not overwritten, relabeled,
or counted. The driver creates a new uniquely named managed child attempt. A
blocked old flat projection may remain for historical analysis; the new run's
canonical files still live in its immutable run directory.

### Cross-commit reuse

A Git commit difference alone does not reject a run. Reuse may be accepted
across commits when every contract above matches, especially the scientific
component fingerprint, scientific Config, model-request Config, and artifacts.
The decision records
`cross_commit_same_scientific_fingerprint=true`. A README or ordinary docs
change therefore does not block reuse. Conversely, a dirty or committed change
to a covered scientific component changes its fingerprint and is rejected;
Git identity never overrides that mismatch.

## Stable rejection reason codes

The public decision can contain more than one code. Order is stable and values
are secret-free.

| Code | Meaning |
|---|---|
| `manifest_missing` | No child manifest can be found |
| `manifest_invalid` | Manifest shape or a required identity field is invalid |
| `manifest_schema_incompatible` | Manifest schema is not the current contract |
| `status_not_finished` | Child terminal status is not `finished` |
| `managed_run_incomplete` | Managed lifecycle did not complete |
| `outputs_incomplete` | Outputs were not finalized as complete |
| `failure_stage_present` | A failure stage remains on the candidate |
| `run_kind_mismatch` | Candidate is not the expected simulation run kind |
| `entrypoint_mismatch` | Command/entrypoint identity differs |
| `completion_incomplete` | Simulation or decision completion is inconsistent/incomplete |
| `recording_schema_incompatible` | Recording schema is not the required formal version |
| `scientific_fingerprint_mismatch` | Covered scientific source identity differs |
| `decision_parser_mismatch` | Parser schema or source hash differs |
| `event_schema_mismatch` | Event schema differs |
| `simulation_core_mismatch` | Simulation-core hash differs |
| `scientific_config_mismatch` | Scientific effective Config identity differs |
| `model_request_config_mismatch` | Canonical model-request Config identity differs, including CodexExec model/effort/wrapper/schema/no-tools identity |
| `provider_mismatch` | Requested or resolved Provider differs |
| `model_mismatch` | Requested or resolved model differs |
| `endpoint_mismatch` | Credential-free endpoint identity differs |
| `model_request_detail_mismatch` | Temperature, max tokens, or cache policy differs |
| `prompt_mismatch` | Prompt source hash differs |
| `persona_mismatch` | Persona source hash differs |
| `scenario_mismatch` | Scenario content/timing identity differs |
| `input_identity_mismatch` | Scientific input or reference-file content differs |
| `seed_mismatch` | Local simulation seed differs |
| `population_mismatch` | Population contract differs or realized population is incomplete |
| `artifact_missing` | A required or registered canonical artifact is absent |
| `artifact_invalid` | Artifact registration/type/size metadata is invalid |
| `artifact_hash_mismatch` | Re-read bytes or size differ from the manifest |
| `result_identity_mismatch` | `experiment_result.json` disagrees with its child manifest |
| `unsafe_symlink` | A link escapes its allowed result/child root or cannot be safely resolved |
| `unsafe_artifact_path` | A canonical path is absolute, traverses, or escapes its run |
| `legacy_link_identity_mismatch` | Compatibility link does not match the manifest-declared link identity |
| `legacy_flat_result_unverified` | A regular flat JSON/CSV/PNG has no authenticated managed-child identity |
| `health_gate_rejected` | Identity may be inspectable, but the driver's declared health threshold failed |

## Legacy analysis input

`inspect_legacy_analysis_inputs` hashes explicitly selected historical files
without pretending to recover their original model/config identity. A managed
analysis stores:

- `provenance_class = "legacy_unverified_input"`;
- path, byte size, SHA-256, readable status, and safe error code per file;
- total, readable, failed, and identity-unverified file counts.

The original files are not modified, and no child manifest is synthesized.
These file counts never increment `executed_runs`, `reused_runs`, or
`honest_n_runs`. Any sample count computed by the analysis must retain its own
explicit unit and provenance limitation.

## Driver accounting

Every cell and driver total reports:

- `planned_runs`: requested independent simulation replicates;
- `executed_runs`: new child attempts actually launched in this invocation;
  each launched `run_seed` subprocess counts, so health retries may make it
  exceed planned replicates without increasing honest-N;
- `reused_runs`: pre-existing children accepted by policy 1.0;
- `reuse_candidates_examined` and `reuse_candidates_rejected`;
- `completed_runs`: successful new children plus valid reused children;
- `failed_runs`: newly executed children that failed;
- `honest_n_runs`: accepted independent simulation replicates available to the
  current experiment;
- rejection counts by reason code and a public-safe per-candidate audit.

Thus a rejected candidate changes only examination/rejection counts until a
new child is executed. It never inflates completion or honest-N. Re-running a
fully matching grid increments `reused_runs`, performs no Provider request, and
keeps `honest_n_runs` equal to the planned replicate count rather than adding a
second copy of the same runs.

## Scope and limitations

- CodexExec 的 CLI/binary/requested-model/reasoning-effort、wrapper/schema 和
  no-tools/history/reasoning/personality 身份通过条件化
  `_provider_adapter_contract` 进入 `model_request_config_hash`，因此正式 child
  reuse 会在该信息不同时以 model-request 身份不匹配拒绝。这一
  扩展不改变 Mock/Anthropic/OpenAI-compatible 的已有 config hash。
- `provider_transport_network_expected` 与
  `agent_tool_network_enabled=false` 是不同身份事实。前者允许 Provider
  transport 联系托管服务，后者禁止 Agent 使用 Web/Apps/MCP 等工具。
- `tool_calls_observed` 和
  `provider_transport_network_declared_or_observed` 是运行结果/运行时
  provenance，不作为预执行 expected-config 的猜测值。禁用工具的请求配置
  进入 model-request identity；成功候选的 observed tool-call count 则作为
  manifest/运行完整性证据核验。
- 当前本机 Codex CLI 0.144.4 不识别 `tools.view_image`，因此真实 turn
  fail-closed，不能产生可复用的正式 Codex child。普通文档变化不会阻止
  reuse，但任何未来允许完整 no-tools contract 的代码/配置身份变化必须由
  `model_request_config_hash` 区分。
- Policy 1.0 authenticates what the current manifest and hashes can represent;
  it does not prove the causal validity of the experiment.
- A cryptographic match does not make a real Provider deterministic.
- Historical flat files remain usable for explicitly marked analysis, but are
  intentionally ineligible for formal resume.
- Provider capability descriptions are separate from this gate and do not
  measure model quality.
