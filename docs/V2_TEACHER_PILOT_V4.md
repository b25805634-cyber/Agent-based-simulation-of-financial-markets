# V2 Teacher endpoint pilot: external-network execution successor

Status: preregistered execution successor profile
`minimax_m27_higgsai_finish_audit_external_joint54x3_v4`. This protocol is
frozen after the permanent failure of a3 and before any v4 live request. It
does not repair, resume, supplement, merge, or reinterpret a1, a2, or a3.

## Why a new successor is required

`v2-teacher-pilot-live-20260813-a3` is a permanently failed managed attempt.
Its authoritative
`results_v2_teacher_pilot/runs/v2-teacher-pilot-live-20260813-a3/run_manifest.json`
has SHA-256
`ede68ce4d7c069feb2424884cf62f0f0d92546c56638c5e5550cb56f6ee326cf`.
The manifest and private Teacher record jointly establish:

- 162 samples were planned; one logical Provider request was attempted, zero
  Provider responses were received, that request resolved as one
  `provider_exception`, and 161 samples were skipped;
- the private exception type was `APIConnectionError` with no response body,
  reported model, token usage, or SDK `finish_reason`;
- parsing was never attempted, Teacher honest-N was zero, and no partial
  Teacher response is available for reuse;
- Student fitting was not run, market honest-N was zero, zero of 12 market
  runs started, and zero of 720 market rounds started; and
- the Teacher release gate did not pass and no downstream result was
  produced.

In the same observation window, a curl reachability probe received HTTP 405,
while a Python/httpx `GET /v1/models` probe explicitly executed outside the
restricted sandbox received HTTP 200. HTTP 405 only shows that the contacted
server answered a method it did not accept; it is not a successful model
inference. Together with a3's zero-response `APIConnectionError`, these facts
are consistent with a network-access restriction in a3's Python execution
environment. They do not prove that the endpoint was continuously healthy,
exclude an intermittent service fault, or establish anything about model
behavior.

Accordingly, a3 is execution evidence, not a Teacher observation. It must not
enter endpoint-response counts, behavioral analysis, Student training, or
market simulation.

## The only v4 change

V4 changes only the execution boundary: the same managed command is executed
from an explicitly authorized environment that can reach the private Higgs
endpoint. It still uses the official registered entrypoint and
`ManagedRunContext`; external network access is not permission to bypass
managed provenance, privacy, honest-N, fail-fast, or exclusive-creation
boundaries.

The following scientific and request semantics remain exactly frozen from v3:

- 54 coherent price/volume and account states, three new real-provider
  replicates per state, and 162 planned requests in the same order;
- master seed 20260811, temperature 0, workers 1, serial retry-free transport,
  and the same first canary;
- Provider `openai`, requested model exactly `MiniMax-M2.7`, no request seed,
  and `max_tokens=4096`;
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

Changing the profile and run identity does not authorize changing an HTTP
request field. If the endpoint is again unreachable, returns an invalid shape,
reports a non-accepted alias or termination reason, or produces a parser- or
feasibility-invalid answer, v4 fails closed at that resolved sample.

## Non-reuse and honest-N boundary

V4 starts at the frozen first canary and plans 162 entirely new requests. It
issues each later request only after the preceding sample is durably valid. No
a1/a2/a3 prompt-response pair, parsed decision, sample row, or honest-N enters
v4. In particular, a3's attempted request is not a valid replicate and cannot
be retried in place, selectively supplemented, or counted toward 162.

Every later request is released only after the preceding sample has been
durably persisted as valid. Any resolved failure stops before another request.
Student fitting and all 12 market runs remain forbidden unless the complete
162/162 gate passes. Raw responses, parsed decisions, failures, exact config
identities, and honest-N remain preserved under the existing public/private
artifact boundary; private artifacts remain mode 0600.

## Config-identity interpretation

V4 keeps request semantics and the `v2_teacher_request/0.2` projection schema,
but the exact `pilot_profile_id` is itself bound by the model-request config.
Therefore the v4 `v2_model_request_config_hash` is expected to differ from
v3 even though the wire-level request fields are unchanged. This conservative
identity difference must be described as a successor-profile identity change,
not as a prompt, model, token-cap, sample-plan, or termination-contract change.

The v4 run ID/profile and live execution context also produce a distinct
`v2_execution_config_hash`, and thus a distinct
`v2_full_effective_config_hash`. The repository's deliberately conservative
scientific component fingerprint binds the managed entrypoint, so an
implementation-only profile addition may also change
`v2_scientific_config_hash`; that hash movement must not be misreported as a
numerical scientific-mechanism change. Final reporting must read all four exact
named hashes from the a4 managed manifest and include their schemas and run
path rather than reuse a3 values or say only "Config hash".

The external sandbox/network policy is an execution precondition. A successful
preflight is evidence of reachability at that moment only; it is neither a
model-identity assertion nor a guarantee that all 162 later requests will
succeed.

## Frozen commands

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_external_joint54x3_v4 \
  --temperature 0 --max-tokens 4096 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v4-dry-20260813-a1
```

Live (must run in the explicitly authorized external-network execution
context; this is a fresh authorization for exactly 162 planned requests):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_external_joint54x3_v4 \
  --temperature 0 --max-tokens 4096 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260813-a4
```

The preflight and live process must use the same network-reachable execution
context. Preflight output is operational evidence only and must not be counted
as one of the 162 Teacher requests.

## Interpretation boundary

V4 remains an exploratory endpoint and engineering-integrity pilot, not human
ground truth. Passing the gate would establish only that the frozen endpoint
produced a complete, schema-valid Teacher dataset suitable for the already
frozen distillation and conserving-market diagnostics. It would not establish
human realism, causal validity, external validity, a particular underlying
weight identity, continuous endpoint availability, or Provider determinism.

Scientific-semantic change declaration: **execution-only successor; no
scientific or request-semantic change**. V4 changes the authorized network
execution context and run/profile identity after a zero-response predecessor.
It does not alter the prompt, model request, output cap, response gate, sample
plan, state, behavioral label, split, Student, market mechanism, or market
parameter.
