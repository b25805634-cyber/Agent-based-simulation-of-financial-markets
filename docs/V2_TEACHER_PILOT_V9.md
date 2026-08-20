# V2 Teacher endpoint pilot: JSON-object constrained successor

Status: preregistered model-request successor profile
`minimax_m27_higgsai_json_object_t1_p095_k40_timeout7200_output190000_joint54x3_v9`.
This protocol is frozen after the permanent external-execution failure of a8
and before any v9 live request. It does not repair, resume, supplement, merge,
or reinterpret a1, a2, a3, a4, a5, a6, a7, or a8.

## Why a fresh successor is required

`v2-teacher-pilot-live-20260820-a8` is a permanently failed managed attempt.
Its authoritative
`results_v2_teacher_pilot/runs/v2-teacher-pilot-live-20260820-a8/run_manifest.json`
has SHA-256
`7ff43b7ca17939af0f0785c68ae095428d8d1466794d2dd5f439c79ad02c099b`.
The public manifest and events establish:

- 162 samples were planned; 35 logical Provider requests began, 34 resolved
  with Provider responses, all 34 resolved responses reported
  `finish_reason=stop` and were valid, and 127 later samples were skipped;
- Teacher honest-N was 34, parsing attempted and succeeded 34 times, and the
  35th request remained unresolved when the process was interrupted;
- the unresolved request is recorded as
  `unresolved_after_interruption`; it contributes no Provider response,
  parsed decision, or Teacher observation;
- the managed attempt ended with `failure_type=keyboard_interrupt` and
  `failure_reason=KeyboardInterrupt: managed run interrupted`, not with an
  SDK response, a Provider exception returned through the normal adapter path,
  the 7200-second hard deadline, or a failed decision parse; and
- Student fitting and market simulation were not run. Student/aggregated and
  market honest-N remained zero, and downstream work was never released.

The read-only monitor observed intervals during the unresolved 35th request
when `ipsec0` was absent and the route to the endpoint was empty or used
`en0`; it later observed `ipsec0` and the endpoint route recover before the
terminal `KeyboardInterrupt`. This is operational coexistence evidence only.
It does not establish that a VPN interruption caused the process interrupt,
that the Provider would otherwise have returned a valid response, or that the
endpoint was continuously unavailable. A8 is therefore classified as an
external-execution failure with one unresolved request, not a model-response
or response-format result.

Accordingly, a8's 34 valid rows remain honest a8 observations, but none may
enter v9, Student training, market simulation, or a combined endpoint-response
count.

## Basis for the JSON-object constraint

The v8 prompt already instructed the Teacher to return the frozen JSON
decision object. V9 additionally sends the standard Chat Completions field
`response_format={"type":"json_object"}` so the serving layer applies a
JSON-object generation constraint rather than relying on prompt instruction
alone.

The vLLM official reasoning-output support table lists MiniMax M2 with
structured output support for JSON and regex; see
[vLLM Reasoning Outputs: Supported Models](https://docs.vllm.ai/en/stable/features/reasoning_outputs/#supported-models).
The endpoint's read-only OpenAPI schema also exposed `response_format` on its
Chat Completions request. These are compatibility grounds for a new guarded
diagnostic, not proof that this gateway accepts the exact request, that its
runtime parser/config implements the same behavior as current upstream vLLM,
or that the constraint will produce a valid frozen Teacher decision.

Adding a serving-layer constraint is not redundant merely because the prompt
already asks for JSON. It may alter decoding, termination, token use, decision
content, parse success, replicate dispersion, aggregated soft labels, and the
eventual Student training distribution. It is therefore an explicit
model-generation request and Teacher data-generating semantic change.

## The v9 change and inherited contract

V9 changes exactly one model-generation request field relative to v8:

```json
{"response_format":{"type":"json_object"}}
```

V9 inherits all other v8 model-request fields unchanged:

- requested model exactly `MiniMax-M2.7` and accepted SDK-reported alias
  exactly `HiggsAI`;
- `max_tokens=190000`;
- `temperature=1`, `top_p=0.95`, and `top_k=40`;
- no request seed and no determinism claim; and
- the exact-`stop` termination requirement and the rule that hidden reasoning
  is never decision content.

V9 also inherits the complete v8 execution contract unchanged:

- a 7200-second hard wall-clock deadline around each logical request;
- 7200-second HTTPX read, write, and pool phase-inactivity timeouts;
- a 10-second connect timeout;
- zero SDK/application retries; and
- workers 1 with strictly sequential, fail-fast request release.

The coherent-state design, prompt bytes and hash, sample identity and order,
first canary, parser, action/intensity label contract, split, privacy boundary,
400-epoch Student, and 48-agent x 60-round x three-seed paired conserving
market diagnostic remain unchanged. Exactly 162 entirely new valid Teacher
rows, exactly three per state, remain mandatory before any Student or market
work may begin.

V9 uses additive request/public-private row schema
`v2_teacher_request/0.4`. Each row identifies the constrained generation
contract, while the managed model-request projection binds the exact sampling
tuple and `response_format`. Sample IDs remain derived from
`v2_teacher_request/0.1` material:
`state_id + prompt_hash + replicate_index`. Thus request/row schema evolution
does not change sample IDs, order, or canary and never authorizes reuse.

Although response format is the only planned v8-to-v9 request change, a8 did
not produce a complete baseline and real-provider requests have no request
seed. V9 therefore does not support causal attribution of any changed
completion rate, latency, label, replicate distribution, Student result, or
market result solely to `response_format`.

## Non-reuse and honest-N boundary

V9 starts at the frozen first canary and plans 162 entirely new requests. It
does not reuse the 34 valid a8 records and does not resume the unresolved 35th
request. No a1/a2/a3/a4/a5/a6/a7/a8 prompt-response pair, parsed decision,
sample row, or honest-N enters v9. Historical attempts cannot be retried in
place, selectively supplemented, or merged to reach 162.

Every later request is released only after the preceding sample has been
durably persisted as valid. Any resolved Provider exception, rejected request,
alias mismatch, non-`stop` termination, null or malformed content, JSON
constraint failure, parser failure, or feasibility failure stops before
another request. Student fitting and all 12 market runs remain forbidden
unless the complete 162/162 gate passes. Raw responses, parsed decisions,
failures, exact config identities, and honest-N remain preserved under the
existing public/private artifact boundary; private artifacts remain mode 0600.

## Config-identity interpretation

V9 uses `v2_teacher_request/0.4` for the model-request projection and Teacher
rows. It adds and binds
`response_format={"type":"json_object"}` while retaining
`max_tokens=190000`, `temperature=1`, `top_p=0.95`, and `top_k=40`. The exact
`pilot_profile_id` also changes. Therefore v9 must have a distinct
`v2_model_request_config_hash`; this must be reported as a JSON-object
generation-constraint plus successor-profile change, not as a prompt-only or
execution-only change.

V9 retains `v2_attention_execution/0.2` and the same 7200/7200/10/zero-retry
transport values as v8. Its new run/profile identity still requires distinct
`v2_execution_config_hash` and `v2_full_effective_config_hash` values.
Historical v1-v4 execution projections remain
`v2_attention_execution/0.1`; v5-v9 use 0.2. Historical manifests must not be
backfilled, rewritten, or reinterpreted under a successor schema.

The repository's deliberately conservative scientific component fingerprint
binds the managed entrypoint, so implementation work may also move
`v2_scientific_config_hash`. Independently of that conservative movement, the
new generation constraint may materially change Teacher observations even
though state/action, Student, and market mechanisms remain unchanged. Final
reporting must read all four exact named hashes and schemas from the a9 managed
manifest and name its exact path/execution context. No a8 hash may be reused as
an a9 identity.

## Frozen commands

`response_format` has no new public CLI flag. Its exact JSON object is frozen
by this opt-in profile, sent by the Provider adapter, and recorded in the
request identity and managed artifacts.

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_json_object_t1_p095_k40_timeout7200_output190000_joint54x3_v9 \
  --temperature 1 --max-tokens 190000 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v9-dry-20260820-a1
```

Live (must run in an explicitly authorized endpoint-reachable execution
context; this is a fresh set of exactly 162 planned requests):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_json_object_t1_p095_k40_timeout7200_output190000_joint54x3_v9 \
  --temperature 1 --max-tokens 190000 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260820-a9
```

Implementation acceptance commands:

```bash
python3 -m unittest tests.test_v2_attention_market_entrypoint
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q nmsim experiments tests
git diff --check
```

The operational metadata checks and live process must use the same
network-reachable execution context. Metadata checks are not Teacher requests
and must not be counted among the 162 planned requests.

## Interpretation and operational-risk boundary

V9 remains an exploratory endpoint and engineering-integrity pilot, not human
ground truth. Passing the gate would establish only that the frozen route
produced a complete, schema-valid Teacher dataset under the joint 190000-token,
recommended-sampling, JSON-object-constrained, and 7200-second condition. It
would not establish human realism, causal validity, external validity,
continuous endpoint availability, Provider determinism, or the adequacy of
the generation constraint in isolation.

The gateway may reject or ignore `response_format` despite its OpenAPI schema.
The deployed vLLM/parser configuration may handle JSON constraints differently
from current upstream documentation. A request may remain in flight for up to
two hours, may still reach `length`, may return null/malformed content, or may
produce syntactically valid JSON that violates the frozen decision schema or
feasibility rules. Every such outcome remains fail-closed and preserves
honest-N.

Scientific-semantic change declaration: **state/action, Student, and market
mechanisms are unchanged, but the Teacher generation constraint and data
distribution semantics change**. V9 adds standard
`response_format={"type":"json_object"}` and advances request/row schema from
0.3 to 0.4. It does not alter the prompt, state/sample identity or order,
sampling tuple, token cap, response gate, decision parser, behavioral label,
split, downstream mechanisms or parameters, timeout values, connect timeout,
or zero-retry policy. Because a8 is an incomplete, externally interrupted,
unseeded partial run, v9 cannot support single-factor causal attribution, and
the new constraint does not guarantee successful completion.
