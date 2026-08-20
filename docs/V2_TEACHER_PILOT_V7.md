# V2 Teacher endpoint pilot: documented-cap and extended-timeout successor

Status: preregistered model-request and execution successor profile
`minimax_m27_higgsai_finish_audit_timeout1800_output65536_joint54x3_v7`.
This protocol is frozen after the permanent failure of a6 and before any v7
live request. It does not repair, resume, supplement, merge, or reinterpret
a1, a2, a3, a4, a5, or a6.

## Why a new successor is required

`v2-teacher-pilot-live-20260820-a6` is a permanently failed managed attempt.
Its authoritative
`results_v2_teacher_pilot/runs/v2-teacher-pilot-live-20260820-a6/run_manifest.json`
has SHA-256
`341033808a8c15f02282100ab56b05edd14838fec004f7e65be7737a970fcfbb`.
The manifest and preserved Provider/Teacher records jointly establish:

- 162 samples were planned; 33 logical Provider requests were attempted and
  resolved, 33 Provider responses were received, 32 were valid, and 129
  samples were skipped after the first invalid response;
- each of the first 32 valid responses reported model alias exactly
  `HiggsAI`, passed the exact-`stop` response-shape gate, and passed response
  parsing and feasibility validation;
- the 33rd response also reported model alias exactly `HiggsAI`, but reported
  SDK `finish_reason=length`, `output_tokens=16384`, and null decision
  `content`; it therefore failed closed as
  `provider_response_shape_invalid`, and the decision parser was not invoked;
- Teacher honest-N was 32; the invalid 33rd response and 129 skipped samples
  contribute no Teacher observation; and
- Student fitting and market simulation were not run, so Student and market
  honest-N both remained zero. The all-or-nothing Teacher gate did not release
  downstream work.

The 33rd record establishes that a6 reached its configured output cap without
producing an accepted final decision. It does not establish what decision, if
any, a longer generation would contain, whether a longer response would parse
or pass feasibility validation, or whether all 162 requests would complete
under a larger cap. Accordingly, a6 is preserved as a failed partial
execution. Its 32 valid responses remain honest a6 observations, but none may
enter v7, Student training, market simulation, or a combined endpoint-response
count.

## Official and operational basis

MiniMax's official OpenAI-compatible Chat Completions documentation says that
for models other than M3, "the recommended value is 65536 (64K)" and the
maximum is 204800 tokens. It also says, "For M2.x models, thinking cannot be
disabled." These statements appear next to the `max_completion_tokens` and
`thinking` request fields in the
[MiniMax Chat Completions API documentation](https://platform.minimax.io/docs/api-reference/text-chat-openai).
They justify preregistering 65536 as a documented-cap diagnostic for M2.7;
they do not prove that this private route implements every hosted-API feature,
that the model will stop before 65536 tokens, or that v7 will succeed.

Read-only operator diagnostics in the endpoint-reachable execution context
add narrower deployment evidence:

- `GET /version` returned HTTP 200 with the exact 51-byte body
  `{"version":"0.17.0rc1.dev204+g04b67d8f6.d20260311"}` and response-body
  SHA-256
  `beaff455e46c81e651b9d97dcd99ebce48972bb712f53c5cf73480eb279fb7d6`;
- `GET /v1/models` advertised `max_model_len=196608` for the reachable serving
  route; this is above v7's 65536 requested output cap but is a total model
  context limit, not a promise of 65536 generated tokens;
- the served OpenAPI schema did not expose `thinking_token_budget`, so v7 does
  not send that unsupported field; and
- non-generating tokenization of the frozen request with default template
  kwargs, `enable_thinking=false`, and `enable_thinking=true` returned HTTP
  200 in all three cases; each result had `count=23`, body SHA-256
  `abffb6526de48dcf55d157bfb1070c006f2cb3ad8a5abba6285b5a58bc4e4569`,
  and a tokenized prompt ending in `<think>`. This establishes only that these
  kwargs did not change those tokenized prompt bytes in the deployed template;
  it is not a model-behavior, performance, latency, or output-validity test.

The vLLM `/version`, `/models`, OpenAPI, and tokenization checks made no Teacher
observation and are not part of honest-N. They do not establish continuous
endpoint availability, underlying serving-weight identity, or successful
inference. In particular, v7 does not claim to disable or budget M2.7's
thinking, and it does not silently replace the deployed chat template.

## The v7 request and execution changes

V7 changes exactly one model-generation request field:
`max_tokens` increases from 16384 to 65536. This is an explicit
model-generation request-semantic change. The repository CLI retains its
existing `--max-tokens` spelling; the official hosted API's preferred spelling
is `max_completion_tokens`. V7 does not add a second token-limit field or
silently send both.

V7 also changes two execution controls:

- the hard wall-clock deadline around each logical Provider request increases
  from 600 to 1800 seconds; and
- the HTTPX read, write, and pool phase-inactivity timeouts increase from 600
  to 1800 seconds.

The HTTPX connect timeout remains 10 seconds, SDK/application retry count
remains zero, and requests remain strictly sequential and fail-fast. The hard
deadline bounds total elapsed time for one logical request; an HTTPX phase
timeout bounds inactivity in its particular transport phase. They remain
distinct controls even though both use 1800 seconds. The larger execution
allowance avoids retaining a 600-second ceiling while testing a four-times
larger output allowance; it is not a retry, throughput claim, endpoint-health
claim, proportional latency model, or guarantee of completion.

The following scientific design and all other model-request/downstream
semantics remain frozen from v6:

- 54 coherent price/volume and account states, three new real-provider
  replicates per state, and 162 planned requests in the same order;
- master seed 20260811, temperature exactly 0, workers 1, and no request seed;
  `top_p` and `top_k` remain unsent rather than acquiring new explicit values;
- the same first canary, prompt bytes, prompt hash, state design, sample IDs,
  split, decision parser, action/intensity semantics, and privacy boundary;
- Provider `openai`, requested model exactly `MiniMax-M2.7`, and accepted safe
  SDK-reported alias exactly `HiggsAI`, with requested and reported identities
  retained as distinct provenance fields;
- `v2_teacher_request/0.2` model-request/Teacher-row schema, the requirement
  that SDK `finish_reason` be exactly `stop`, and the rule that hidden
  reasoning is never decision content;
- `v2_teacher_request/0.1` sample-identity material, preserving the same
  `state_id + prompt_hash + replicate_index` sample IDs, order, and canary;
- 400 Student epochs and the same 48-agent, 60-round, three-seed paired
  conserving-market diagnostic; and
- the all-or-nothing gate: exactly 162 valid new Teacher samples, exactly
  three per state, before Student or market work may begin.

A response that reports `length`, has null decision content, reaches another
non-`stop` termination reason, has an invalid response shape, fails parsing,
or fails feasibility validation still makes v7 fail closed at that resolved
sample. The larger cap and timeouts do not relax any acceptance condition.

## Non-reuse and honest-N boundary

V7 starts at the frozen first canary and plans 162 entirely new requests. It
does not reuse the 32 valid a6 records and does not begin at a6's 33rd or 34th
sample. No a1/a2/a3/a4/a5/a6 prompt-response pair, parsed decision, sample row,
or honest-N enters v7. Historical attempts cannot be retried in place,
selectively supplemented, or merged to reach 162.

Every later request is released only after the preceding sample has been
durably persisted as valid. Any resolved failure stops before another request.
Student fitting and all 12 market runs remain forbidden unless the complete
162/162 gate passes. Raw responses, parsed decisions, failures, exact config
identities, and honest-N remain preserved under the existing public/private
artifact boundary; private artifacts remain mode 0600.

## Config-identity interpretation

V7 keeps the `v2_teacher_request/0.2` request/row schema but changes its
model-request projection from `max_tokens=16384` to `max_tokens=65536`. The
exact `pilot_profile_id` also changes. Therefore v7 must have a distinct
`v2_model_request_config_hash`; that difference must be described as the
explicit output-cap change plus successor-profile identity, not as a prompt,
model, temperature, sampling-default, state-plan, sample-identity, parser, or
termination-gate change.

Sample IDs remain derived from the unchanged `v2_teacher_request/0.1`
sample-identity material. Thus v7 preserves the planned sample IDs, order, and
canary while requiring fresh Provider requests. Identity equality never
authorizes reuse of a historical response.

V7 retains the `v2_attention_execution/0.2` execution schema but changes its
projection to `hard_request_deadline_seconds=1800` and
`httpx_phase_inactivity_timeout_seconds=1800`; it keeps
`connect_timeout_seconds=10` and `provider_retry_count=0`. Therefore v7 must
also have distinct `v2_execution_config_hash` and
`v2_full_effective_config_hash` values. Historical v1-v4 execution
projections remain `v2_attention_execution/0.1`; v5-v7 use 0.2. No historical
manifest may be backfilled, rewritten, or reinterpreted under a successor
schema.

The repository's deliberately conservative scientific component fingerprint
binds the managed entrypoint. Therefore implementation work for v7 may also
move `v2_scientific_config_hash`; such movement must not be misreported as a
numerical scientific-mechanism change. Final reporting must read all four
exact named hashes and schemas from the a7 managed manifest and name its exact
path/execution context. No a6 hash may be reused as an a7 identity.

## Frozen commands

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_timeout1800_output65536_joint54x3_v7 \
  --temperature 0 --max-tokens 65536 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v7-dry-20260820-a1
```

Live (must run in the explicitly authorized endpoint-reachable execution
context; this is a fresh set of exactly 162 planned requests):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_timeout1800_output65536_joint54x3_v7 \
  --temperature 0 --max-tokens 65536 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260820-a7
```

Implementation acceptance commands:

```bash
python3 -m unittest tests.test_v2_attention_market_entrypoint
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q nmsim experiments tests
git diff --check
```

The operational metadata diagnostics and live process must use the same
network-reachable execution context. Metadata/tokenization diagnostics are not
Teacher requests and must not be counted among the 162 planned requests.

## Interpretation boundary

V7 remains an exploratory endpoint and engineering-integrity pilot, not human
ground truth. Passing the gate would establish only that the frozen endpoint
produced a complete, schema-valid Teacher dataset under the 65536-token cap
and 1800-second execution controls, suitable for the already frozen
distillation and conserving-market diagnostics. It would not establish human
realism, causal validity, external validity, a particular underlying weight
identity, continuous endpoint availability, Provider determinism, or the
general adequacy of the larger cap and timeout controls.

Scientific-semantic change declaration: **no scientific-mechanism change;
one explicit model-generation request-semantic change and two explicit
execution-semantic changes**. V7 changes `max_tokens` from 16384 to 65536,
changes both the hard request deadline and HTTPX read/write/pool
phase-inactivity timeout from 600 to 1800 seconds, and changes run/profile
identity after an output-capped predecessor. It does not alter the prompt,
model, temperature, unsent `top_p`/`top_k`, request seed policy, response gate,
state/sample plan, parser, behavioral label, split, Student, market mechanism,
market parameters, 10-second connect timeout, or zero-retry policy. None of
these changes guarantees that v7 will complete or pass its gate.
