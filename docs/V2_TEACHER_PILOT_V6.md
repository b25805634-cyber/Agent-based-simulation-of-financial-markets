# V2 Teacher endpoint pilot: larger-output-cap successor

Status: preregistered model-request successor profile
`minimax_m27_higgsai_finish_audit_timeout600_output16384_joint54x3_v6`.
This protocol is frozen after the permanent failure of a5 and before any v6
live request. It does not repair, resume, supplement, merge, or reinterpret
a1, a2, a3, a4, or a5.

## Why a new successor is required

`v2-teacher-pilot-live-20260813-a5` is a permanently failed managed attempt.
Its authoritative
`results_v2_teacher_pilot/runs/v2-teacher-pilot-live-20260813-a5/run_manifest.json`
has SHA-256
`76e79010a55a2c57766d52b147f686b4c0854c9abaa7cadc3abe468adf96b16a`.
The manifest and private Teacher records jointly establish:

- 162 samples were planned; seven logical Provider requests were attempted
  and resolved, seven Provider responses were received, six were valid, and
  155 samples were skipped after the first invalid response;
- each of the first six responses reported model alias exactly `HiggsAI`,
  reported SDK `finish_reason` exactly `stop`, and passed response parsing and
  feasibility validation;
- the seventh response also reported model alias exactly `HiggsAI`, but
  reported SDK `finish_reason=length` at `output_tokens=4096`; it therefore
  failed closed as `provider_response_shape_invalid` before the decision
  parser was invoked;
- Teacher honest-N was six; the invalid seventh response and 155 skipped
  samples contribute no Teacher observation; and
- Student fitting and market simulation were not run, so Student and market
  honest-N both remained zero. The all-or-nothing Teacher gate did not release
  downstream work.

The seventh record establishes that a5 reached its configured output cap and
did not satisfy the frozen exact-`stop` termination contract. It does not
establish what decision, if any, a longer response would contain, whether that
decision would parse or pass feasibility validation, or whether all 162
requests would complete under a larger cap. The endpoint's `/models` response
advertised `max_model_len=196608` in the same execution context. That is
run-time capacity evidence supporting a preregistered 16384-token request cap;
it is not a claim of continuous availability, a guarantee that every request
will receive 16384 output tokens, or evidence that v6 will succeed.

Accordingly, a5 is preserved as a failed partial execution. Its six valid
responses remain honest a5 observations, but none may enter v6, Student
training, market simulation, or a combined endpoint-response count.

## The v6 model-request change

V6 changes exactly one model-generation request field:
`max_tokens` increases from 4096 to 16384. This is an explicit
model-generation request-semantic change, not an execution-only change. It is
predeclared in response to a5's preserved `finish_reason=length` and
`output_tokens=4096` evidence; it does not relax the exact-`stop` acceptance
gate. A response that again reports `length`, reaches any other non-`stop`
termination reason, has an invalid response shape, fails parsing, or fails
feasibility validation still makes v6 fail closed at that resolved sample.

V6 inherits v5's execution contract unchanged:

- a 600-second hard wall-clock deadline around each logical Provider request;
- 600-second HTTPX read, write, and pool phase-inactivity timeouts;
- a 10-second HTTPX connect timeout;
- zero SDK/application retries; and
- strictly sequential, fail-fast release of requests.

The following scientific design and all other request/downstream semantics
remain frozen from v5:

- 54 coherent price/volume and account states, three new real-provider
  replicates per state, and 162 planned requests in the same order;
- master seed 20260811, temperature 0, workers 1, no request seed, and the same
  first canary;
- Provider `openai` and requested model exactly `MiniMax-M2.7`;
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

Increasing the output cap does not authorize a prompt, state, parser,
acceptance-rule, Student, or market change. It also does not mean v6 responses
must use the full allowance. Raw SDK termination and usage provenance remains
authoritative for every resolved response.

## Non-reuse and honest-N boundary

V6 starts at the frozen first canary and plans 162 entirely new requests. It
does not reuse the six valid a5 records and does not begin at a5's seventh or
eighth sample. No a1/a2/a3/a4/a5 prompt-response pair, parsed decision, sample
row, or honest-N enters v6. Historical attempts cannot be retried in place,
selectively supplemented, or merged to reach 162.

Every later request is released only after the preceding sample has been
durably persisted as valid. Any resolved failure stops before another request.
Student fitting and all 12 market runs remain forbidden unless the complete
162/162 gate passes. Raw responses, parsed decisions, failures, exact config
identities, and honest-N remain preserved under the existing public/private
artifact boundary; private artifacts remain mode 0600.

## Config-identity interpretation

V6 keeps the `v2_teacher_request/0.2` request/row schema but changes its
model-request projection from `max_tokens=4096` to `max_tokens=16384`. The
exact `pilot_profile_id` also changes. Therefore v6 must have a distinct
`v2_model_request_config_hash`; that difference must be described as the
explicit output-cap change plus successor-profile identity, not as a prompt,
model, temperature, state-plan, sample-identity, parser, or termination-gate
change.

Sample IDs remain derived from the unchanged `v2_teacher_request/0.1`
sample-identity material. Thus v6 preserves the planned sample IDs, order, and
canary while requiring fresh Provider requests. Identity equality never
authorizes reuse of a historical response.

V6 retains the `v2_attention_execution/0.2` execution projection with
`hard_request_deadline_seconds=600`,
`httpx_phase_inactivity_timeout_seconds=600`,
`connect_timeout_seconds=10`, and `provider_retry_count=0`. Its new run/profile
identity still requires distinct `v2_execution_config_hash` and
`v2_full_effective_config_hash` values. Historical v1-v4 execution projections
remain `v2_attention_execution/0.1`; v5 and v6 use 0.2. No historical manifest
may be backfilled, rewritten, or reinterpreted under a successor schema.

The repository's deliberately conservative scientific component fingerprint
binds the managed entrypoint. Therefore implementation work for v6 may also
move `v2_scientific_config_hash`; such movement must not be misreported as a
numerical scientific-mechanism change. Final reporting must read all four
exact named hashes and schemas from the a6 managed manifest and name its exact
path/execution context. No a5 hash may be reused as an a6 identity.

## Frozen commands

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_timeout600_output16384_joint54x3_v6 \
  --temperature 0 --max-tokens 16384 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v6-dry-20260820-a1
```

Live (must run in the explicitly authorized endpoint-reachable execution
context; this is a fresh set of exactly 162 planned requests):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_timeout600_output16384_joint54x3_v6 \
  --temperature 0 --max-tokens 16384 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260820-a6
```

Implementation acceptance commands:

```bash
python3 -m unittest tests.test_v2_attention_market_entrypoint
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q nmsim experiments tests
git diff --check
```

The `/models` capacity preflight and live process must use the same
network-reachable execution context. Preflight output is operational evidence
only and must not be counted as one of the 162 Teacher requests.

## Interpretation boundary

V6 remains an exploratory endpoint and engineering-integrity pilot, not human
ground truth. Passing the gate would establish only that the frozen endpoint
produced a complete, schema-valid Teacher dataset under the 16384-token cap,
suitable for the already frozen distillation and conserving-market
diagnostics. It would not establish human realism, causal validity, external
validity, a particular underlying weight identity, continuous endpoint
availability, Provider determinism, or the general adequacy of the larger
output cap and inherited timeout controls.

Scientific-semantic change declaration: **no scientific-mechanism change;
one explicit model-generation request-semantic change**. V6 changes
`max_tokens` from 4096 to 16384 and changes the run/profile identity after an
output-capped predecessor. It does not alter the prompt, model, temperature,
request seed policy, response gate, state/sample plan, parser, behavioral
label, split, Student, market mechanism, market parameters, 600-second hard
deadline, 600-second HTTPX read/write/pool phase timeouts, 10-second connect
timeout, or zero-retry policy.
