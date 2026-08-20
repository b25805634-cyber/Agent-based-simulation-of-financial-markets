# V2 Teacher endpoint pilot: long-timeout execution successor

Status: preregistered execution successor profile
`minimax_m27_higgsai_finish_audit_long_timeout_joint54x3_v5`. This
protocol is frozen after the permanent failure of a4 and before any v5 live
request. It does not repair, resume, supplement, merge, or reinterpret a1,
a2, a3, or a4.

## Why a new successor is required

`v2-teacher-pilot-live-20260813-a4` is a permanently failed managed attempt.
Its authoritative
`results_v2_teacher_pilot/runs/v2-teacher-pilot-live-20260813-a4/run_manifest.json`
has SHA-256
`86c1deb28f85c2f49e9fd36c410c951f3fa27e8d8fa48b94cf3febf86d1f888e`.
The manifest and private Teacher records jointly establish:

- 162 samples were planned; six logical Provider requests were attempted and
  resolved, five Provider responses were received and valid, and 156 samples
  were skipped after the first failure;
- each of the first five responses reported model alias exactly `HiggsAI`,
  reported SDK `finish_reason` exactly `stop`, and passed response parsing and
  feasibility validation;
- the sixth request produced no Provider response and resolved as a
  `provider_exception`; the private exception type was `APITimeoutError` under
  v4's HTTPX timeout policy (10-second connect timeout and 120-second
  read/write/pool phase-inactivity timeouts), with no separate hard wall-clock
  deadline;
- Teacher honest-N was five; the failed sixth request and 156 skipped samples
  contribute no Teacher observation; and
- Student fitting was not run, market honest-N was zero, zero of 12 market
  runs started, and zero of 720 market rounds started. The all-or-nothing
  Teacher gate did not release downstream work.

The five valid records establish that this execution context could receive
valid responses during part of a4. The sixth record establishes only that no
response was received before the HTTPX/SDK request raised `APITimeoutError`
under that phase-inactivity policy. An HTTPX phase timeout is not a total
end-to-end request deadline: its read, write, and pool values bound inactivity
within those phases, while connect has its own bound. The preserved error does
not identify the precise phase or distinguish endpoint computation time,
queueing, network delay, proxy delay, or another transport/service cause. It
neither proves that the endpoint was unstable nor predicts that v5's timeout
contract will make all 162 requests succeed.

Accordingly, a4 is preserved as a failed partial execution. Its five valid
responses remain honest a4 observations, but none may enter v5, Student
training, market simulation, or a combined endpoint-response count.

## The v5 execution-only changes

V5 changes only the per-request execution timeout contract. It adds a true
600-second hard wall-clock deadline around each logical Provider request. It
also changes the HTTPX read, write, and pool phase-inactivity timeout values
from 120 seconds to 600 seconds. The HTTPX connect timeout remains 10 seconds,
application retry count remains zero, and requests remain strictly sequential
and fail-fast.

These are distinct controls. The hard wall-clock deadline bounds total elapsed
time for the logical request even if transport activity continues; each HTTPX
phase-inactivity timeout bounds inactivity in its own phase and is not itself
an end-to-end timer. Either control may end a request, and the 10-second connect
timeout can end connection establishment earlier. The larger values merely
allow more time than v4's phase policy did; they are not a retry,
endpoint-health claim, or guarantee of completion.

The following scientific and model-generation request semantics remain exactly
frozen from v4:

- 54 coherent price/volume and account states, three new real-provider
  replicates per state, and 162 planned requests in the same order;
- master seed 20260811, temperature 0, workers 1, no request seed, and the same
  first canary;
- Provider `openai`, requested model exactly `MiniMax-M2.7`, and
  `max_tokens=4096`;
- accepted safe SDK-reported alias exactly `HiggsAI`, with requested and
  reported identities retained as distinct provenance fields;
- unchanged Teacher system/user prompt bytes, prompt hash, state design,
  decision-response parser, action/intensity semantics, split, and privacy
  boundary;
- `v2_teacher_request/0.2` model-request/Teacher-row schema, including the
  requirement that SDK `finish_reason` be exactly `stop` and the rule that
  hidden `reasoning_content` is never decision content;
- `v2_teacher_request/0.1` sample-identity material, preserving the same
  `state_id + prompt_hash + replicate_index` sample IDs, order, and canary;
- 400 Student epochs and the same 48-agent, 60-round, three-seed paired
  conserving-market diagnostic; and
- the all-or-nothing gate: exactly 162 valid new Teacher samples, exactly
  three per state, before Student or market work may begin.

The HTTP/model-generation payload is unchanged. The hard deadline and HTTPX
phase-inactivity values are execution-layer transport policy. Changing the
profile and run identity does not authorize changing any prompt, state,
model-generation field,
acceptance rule, Student setting, or market setting. A 10-second connect
timeout, a 600-second HTTPX phase-inactivity timeout, the 600-second hard
wall-clock deadline, any other Provider exception, an invalid response shape,
a non-accepted alias or termination reason, or a parser- or
feasibility-invalid answer still makes v5 fail closed at that resolved sample.

## Non-reuse and honest-N boundary

V5 starts at the frozen first canary and plans 162 entirely new requests. It
does not reuse the five valid a4 records and does not begin at a4's sixth
sample. No a1/a2/a3/a4 prompt-response pair, parsed decision, sample row, or
honest-N enters v5. Historical attempts cannot be retried in place, selectively
supplemented, or merged to reach 162.

Every later request is released only after the preceding sample has been
durably persisted as valid. Any resolved failure stops before another request.
Student fitting and all 12 market runs remain forbidden unless the complete
162/162 gate passes. Raw responses, parsed decisions, failures, exact config
identities, and honest-N remain preserved under the existing public/private
artifact boundary; private artifacts remain mode 0600.

## Config-identity interpretation

The new 600-second hard wall-clock deadline and the 120-to-600-second HTTPX
read/write/pool phase-inactivity changes are execution semantics. They must be
traceable in the v5 `v2_attention_execution/0.2` execution projection as
`hard_request_deadline_seconds=600`,
`httpx_phase_inactivity_timeout_seconds=600`,
`connect_timeout_seconds=10`, and `provider_retry_count=0`. V1-v4
retain their existing `v2_attention_execution/0.1` projection without these
new explicit transport fields. This is a profile-specific additive schema
successor, not a migration: historical v1-v4 manifests remain authoritative
as written and must not be backfilled, rewritten, or reinterpreted as schema
0.2. V1-v4 had no hard wall-clock request deadline; documentation must not
retroactively infer one from their HTTPX phase timeouts. Consequently, v5 must
have a distinct
`v2_execution_config_hash` and `v2_full_effective_config_hash`.

V5 keeps the `v2_teacher_request/0.2` request projection and its
model-generation fields unchanged. However, the exact `pilot_profile_id` is
itself conservatively bound by the model-request config, so the v5
`v2_model_request_config_hash` is expected to differ from v4. That difference
must be described as a successor-profile identity change, not as a prompt,
model, temperature, token-cap, state-plan, or termination-contract change.

The repository's deliberately conservative scientific component fingerprint
binds the managed entrypoint. Therefore an implementation-only v5 profile and
transport change may also move `v2_scientific_config_hash`; such movement must
not be misreported as a numerical scientific-mechanism change. Final reporting
must read all four exact named hashes and schemas from the a5 managed manifest,
and name its exact path/execution context. No a4 hash may be reused as an a5
identity.

## Frozen commands

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_long_timeout_joint54x3_v5 \
  --temperature 0 --max-tokens 4096 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v5-dry-20260813-a1
```

Live (must run in the explicitly authorized endpoint-reachable execution
context; this is a fresh set of exactly 162 planned requests):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_long_timeout_joint54x3_v5 \
  --temperature 0 --max-tokens 4096 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260813-a5
```

The preflight and live process must use the same network-reachable execution
context. Preflight output is operational evidence only and must not be counted
as one of the 162 Teacher requests.

## Interpretation boundary

V5 remains an exploratory endpoint and engineering-integrity pilot, not human
ground truth. Passing the gate would establish only that the frozen endpoint
produced a complete, schema-valid Teacher dataset suitable for the already
frozen distillation and conserving-market diagnostics. It would not establish
human realism, causal validity, external validity, a particular underlying
weight identity, continuous endpoint availability, Provider determinism, or
the general adequacy of either 600-second timeout control.

Scientific-semantic change declaration: **execution-only successor; no
scientific or model-generation request-semantic change**. V5 adds a 600-second
hard wall-clock deadline per logical Provider request, changes HTTPX
read/write/pool phase-inactivity values from 120 to 600 seconds, and changes
the run/profile identity after a timed-out predecessor. It does not alter the
10-second connect timeout, zero-retry policy, prompt, model-generation payload,
output cap, response gate, sample plan, state, behavioral label, split,
Student, market mechanism, or market parameter.
