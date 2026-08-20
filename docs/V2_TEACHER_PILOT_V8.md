# V2 Teacher endpoint pilot: near-context-cap recommended-sampling successor

Status: preregistered model-request and execution successor profile
`minimax_m27_higgsai_finish_audit_t1_p095_k40_timeout7200_output190000_joint54x3_v8`.
This protocol is frozen after the permanent failure of a7 and before any v8
live request. It does not repair, resume, supplement, merge, or reinterpret
a1, a2, a3, a4, a5, a6, or a7.

## Why a new successor is required

`v2-teacher-pilot-live-20260820-a7` is a permanently failed managed attempt.
Its authoritative
`results_v2_teacher_pilot/runs/v2-teacher-pilot-live-20260820-a7/run_manifest.json`
has SHA-256
`137ebb06875c6853a73f58519c90168d9ba7fc879c15c00433d484f277453641`.
The manifest and preserved Provider/Teacher records jointly establish:

- 162 samples were planned; 45 logical Provider requests were attempted and
  resolved, 45 Provider responses were received, 44 were valid, and 117
  samples were skipped after the first invalid response;
- the first 44 responses reported model alias exactly `HiggsAI`, reported SDK
  `finish_reason=stop`, and passed response parsing and feasibility
  validation;
- the 45th response also reported model alias exactly `HiggsAI`, but reported
  SDK `finish_reason=length`, `input_tokens=745`,
  `output_tokens=65536`, and null decision `content`; it therefore failed
  closed as `provider_response_shape_invalid`, and the decision parser was not
  invoked;
- Teacher honest-N was 44; the invalid 45th response and 117 skipped samples
  contribute no Teacher observation; and
- Student fitting and market simulation were not run, so Student and market
  honest-N both remained zero. The all-or-nothing Teacher gate did not release
  downstream work.

The 45th record establishes that a7 reached its configured output cap without
producing an accepted final decision. It does not establish what decision, if
any, a longer generation would contain, whether a longer response would parse
or pass feasibility validation, or whether all 162 requests would complete
under a larger cap. Accordingly, a7 is preserved as a failed partial
execution. Its 44 valid responses remain honest a7 observations, but none may
enter v8, Student training, market simulation, or a combined endpoint-response
count.

## Official and route-specific basis

MiniMax's official M2.7 model README recommends exactly
`temperature=1.0`, `top_p=0.95`, and `top_k=40` for best performance; see the
[MiniMax-M2.7 official README](https://github.com/MiniMax-AI/MiniMax-M2.7/blob/main/README.md#inference-parameters).
V8 adopts that complete sampling tuple rather than retaining the earlier
temperature-zero diagnostic request. This official recommendation is not a
human-validity claim, a determinism claim, or evidence that these settings
will produce schema-valid decisions on the private route.

The same route's read-only `/v1/models` diagnostic advertised
`max_model_len=196608`. The a7 capped response reported 745 input tokens. V8
therefore requests `max_tokens=190000`, leaving an arithmetic margin of 5863
tokens under `196608 - 745`. This is a preregistered engineering choice, not an
official MiniMax recommended output value and not an assertion that every v8
request will have exactly 745 input tokens. Chat-template rendering, server
accounting, prompt-token usage, special tokens, future deployment changes, or
gateway policy may consume the margin differently or reject the request.

The user explicitly declined another artificially small incremental cap. V8
therefore moves close to the route's advertised total context rather than
performing another 2x or 4x cap escalation. It still uses a finite cap because
the route has a finite advertised context; `190000` is neither unlimited nor
guaranteed to be accepted or generated. MiniMax's official OpenAI-compatible
documentation separately states that M2.x thinking cannot be disabled; see
the [`thinking` field](https://platform.minimax.io/docs/api-reference/text-chat-openai).
V8 does not send an undocumented thinking switch, does not change the chat
template, and does not treat hidden reasoning as decision content.

These metadata and documentation observations make no Teacher observation and
are not part of honest-N. They do not establish continuous endpoint
availability, underlying serving-weight identity, or successful inference.

## The v8 model-request and execution changes

V8 changes four model-generation request fields together:

- `max_tokens`: 65536 to 190000;
- `temperature`: 0 to 1;
- `top_p`: previously unsent, now explicitly 0.95; and
- `top_k`: previously unsent, now explicitly 40.

The larger cap is intended to avoid another known small-cap truncation. The
sampling tuple follows the official M2.7 recommendation. These changes are
material request semantics: temperature, nucleus sampling, and top-k sampling
can change individual Teacher responses, replicate dispersion, aggregated
soft labels, and the eventual Student training distribution. Real-provider
requests still send no seed, so neither repeatability nor full determinism is
claimed.

`top_p` and `top_k` have no new public CLI switches. Their exact values are
frozen by this opt-in profile, sent by the Provider adapter, and recorded in
the model-request identity/managed manifest. The exact commands therefore name
only the existing `--temperature` and `--max-tokens` CLI fields; omission of
`--top-p`/`--top-k` from the command does not mean those wire fields are
unspecified.

V8 also changes two execution controls:

- the hard wall-clock deadline around each logical Provider request increases
  from 1800 to 7200 seconds; and
- the HTTPX read, write, and pool phase-inactivity timeouts increase from 1800
  to 7200 seconds.

The HTTPX connect timeout remains 10 seconds, SDK/application retry count
remains zero, and requests remain strictly sequential and fail-fast. The hard
deadline bounds total elapsed time for one logical request; an HTTPX phase
timeout bounds inactivity in its particular transport phase. They remain
distinct controls even though both use 7200 seconds. The larger values allow
up to two hours for one logical request; they are not retries, endpoint-health
claims, throughput models, or guarantees of completion.

Because v8 changes the output cap, three sampling fields, and two timeout
controls simultaneously, no comparison with a7 or any earlier partial run may
attribute a changed completion rate, label distribution, latency, or market
result to a single one of those changes. This successor is a joint operational
and Teacher-sampling condition, not a one-factor causal ablation.

The following design and downstream semantics remain frozen from v7:

- 54 coherent price/volume and account states, three entirely new
  real-provider replicates per state, and 162 planned requests in the same
  order;
- master seed 20260811, workers 1, no request seed, and the same first canary;
- Provider `openai`, requested model exactly `MiniMax-M2.7`, and accepted safe
  SDK-reported alias exactly `HiggsAI`, with requested and reported identities
  retained as distinct provenance fields;
- unchanged Teacher system/user prompt bytes, prompt hash, state design,
  sample IDs, split, decision parser, action/intensity semantics, and privacy
  boundary;
- the exact-`stop` termination gate and the rule that hidden reasoning is
  never decision content;
- `v2_teacher_request/0.1` sample-identity material, preserving the same
  `state_id + prompt_hash + replicate_index` sample IDs, order, and canary;
- 400 Student epochs and the same 48-agent, 60-round, three-seed paired
  conserving-market diagnostic; and
- the all-or-nothing gate: exactly 162 valid new Teacher samples, exactly
  three per state, before Student or market work may begin.

V8 uses the additive `v2_teacher_request/0.3` model-request/public-private row
schema so the newly explicit `top_p` and `top_k`, along with the full sampling
tuple, remain traceable. The sample-identity material deliberately remains
`v2_teacher_request/0.1`; changing the request/row schema must not reorder or
redefine the planned samples.

A response that reports `length`, has null decision content, reaches another
non-`stop` termination reason, has an invalid response shape, fails parsing,
or fails feasibility validation still makes v8 fail closed at that resolved
sample. The cap, sampling, and timeout changes do not relax any acceptance
condition.

## Non-reuse and honest-N boundary

V8 starts at the frozen first canary and plans 162 entirely new requests. It
does not reuse the 44 valid a7 records and does not begin at a7's 45th or 46th
sample. No a1/a2/a3/a4/a5/a6/a7 prompt-response pair, parsed decision, sample
row, or honest-N enters v8. Historical attempts cannot be retried in place,
selectively supplemented, or merged to reach 162.

Every later request is released only after the preceding sample has been
durably persisted as valid. Any resolved failure stops before another request.
Student fitting and all 12 market runs remain forbidden unless the complete
162/162 gate passes. Raw responses, parsed decisions, failures, exact config
identities, and honest-N remain preserved under the existing public/private
artifact boundary; private artifacts remain mode 0600.

## Config-identity interpretation

V8 uses `v2_teacher_request/0.3` for its model-request projection and
Teacher rows. It binds `max_tokens=190000`, `temperature=1`, `top_p=0.95`,
`top_k=40`, and the new exact `pilot_profile_id`. Therefore v8 must have a
distinct `v2_model_request_config_hash`. That difference must be reported as a
joint output-cap and sampling-regime change, not as a profile-only identity
change.

Sample IDs remain derived from the unchanged `v2_teacher_request/0.1`
sample-identity material. Thus v8 preserves the planned sample IDs, order, and
canary while requiring fresh Provider requests. Identity equality never
authorizes reuse of a historical response.

V8 retains the `v2_attention_execution/0.2` execution schema but changes its
projection to `hard_request_deadline_seconds=7200` and
`httpx_phase_inactivity_timeout_seconds=7200`; it keeps
`connect_timeout_seconds=10` and `provider_retry_count=0`. Therefore v8 must
also have distinct `v2_execution_config_hash` and
`v2_full_effective_config_hash` values. Historical v1-v4 execution
projections remain `v2_attention_execution/0.1`; v5-v8 use 0.2. No historical
manifest may be backfilled, rewritten, or reinterpreted under a successor
schema.

The repository's deliberately conservative scientific component fingerprint
binds the managed entrypoint. Implementation work for v8 may therefore move
`v2_scientific_config_hash`; additionally, the new sampling regime has a real
Teacher data-generating consequence even though the coherent-state,
action-label, Student, and market mechanisms remain unchanged. Final reporting
must read all four exact named hashes and schemas from the a8 managed manifest
and name its exact path/execution context. No a7 hash may be reused as an a8
identity.

## Frozen commands

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_t1_p095_k40_timeout7200_output190000_joint54x3_v8 \
  --temperature 1 --max-tokens 190000 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v8-dry-20260820-a1
```

Live (must run in the explicitly authorized endpoint-reachable execution
context; this is a fresh set of exactly 162 planned requests):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_t1_p095_k40_timeout7200_output190000_joint54x3_v8 \
  --temperature 1 --max-tokens 190000 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260820-a8
```

Implementation acceptance commands:

```bash
python3 -m unittest tests.test_v2_attention_market_entrypoint
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q nmsim experiments tests
git diff --check
```

The operational metadata diagnostics and live process must use the same
network-reachable execution context. Metadata diagnostics are not Teacher
requests and must not be counted among the 162 planned requests.

## Interpretation and operational-risk boundary

V8 remains an exploratory endpoint and engineering-integrity pilot, not human
ground truth. Passing the gate would establish only that the frozen endpoint
produced a complete, schema-valid Teacher dataset under the joint 190000-token,
recommended-sampling, and 7200-second condition, suitable for the already
frozen distillation and conserving-market diagnostics. It would not establish
human realism, causal validity, external validity, a particular underlying
weight identity, continuous endpoint availability, Provider determinism, or
the adequacy of any one changed control in isolation.

One request may remain in flight for up to two hours before the hard deadline;
162 strictly serial worst-case deadlines would span up to 324 hours before
downstream work, although any resolved failure stops the run immediately. The
route may reject `max_tokens=190000` because of total-context accounting,
gateway policy, or deployment changes. It may still return `length`, null
content, malformed content, or an unparsable/infeasible decision. Every such
case remains a terminal fail-closed outcome with honest-N preserved.

Scientific-semantic change declaration: **the coherent-state, action,
Student, and market mechanisms are unchanged, but the Teacher data-generating
sampling regime changes materially**. V8 changes the output cap from 65536 to
190000, temperature from 0 to 1, sends new `top_p=0.95` and `top_k=40`, changes
the request/row schema from 0.2 to 0.3, changes both the hard request deadline
and HTTPX read/write/pool phase-inactivity timeout from 1800 to 7200 seconds,
and changes run/profile identity. It does not alter prompt/state/sample
identity or order, response gate, decision parser, behavioral label, split,
Student, market mechanism or parameters, 10-second connect timeout, or
zero-retry policy. Because these changes are bundled, v8 cannot support a
single-factor causal attribution, and none guarantees successful completion.
