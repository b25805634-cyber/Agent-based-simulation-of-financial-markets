# V2 Teacher endpoint pilot: output-complete successor protocol

Status: preregistered exploratory successor profile
`minimax_m27_higgsai_finish_audit_joint54x3_v3`. This document freezes a new
run after the permanent failures of a1 and a2. It does not repair, resume,
supplement, merge, or reinterpret either predecessor.

## What remains unchanged

The scientific design remains the limited-attention price/volume Teacher →
Student → conserving-market pipeline: 54 designed states, three independent
real-provider replicates per state, master seed 20260811, temperature 0,
workers 1, 400 Student epochs, and the same 48-agent, 60-round, three-seed
paired market diagnostic. The Teacher prompt, state design, sample order,
canary, split, Teacher decision-response schema, Student architecture, market
design, privacy boundary, and acceptance requirement of 162 valid new samples
are unchanged.

The request model remains exactly `MiniMax-M2.7`; the only accepted safe
SDK-reported alias remains exactly `HiggsAI`. These are distinct provenance
fields. The alias does not independently verify underlying serving weights.

## Evidence from the failed predecessors

`v2-teacher-pilot-live-20260812-a1` is permanently failed: one attempt, zero
valid samples, 161 skipped. Its manifest SHA-256 is
`8e9fde17ed2c71267c2c45cc110c6c951690269d997f84fbf080e5e7e9881c7d`.

`v2-teacher-pilot-live-20260812-a2` is permanently failed: four attempts,
three valid samples, one `provider_response_shape_invalid`, and 158 skipped.
The failed fourth response reported 1024 output tokens—exactly the frozen v2
cap—but no string `message.content`. This is consistent with output-budget
exhaustion, but a2 did not preserve `finish_reason` or a full SDK envelope, so
it does not prove that diagnosis. Teacher honest-N was three; Student was not
run and market honest-N remained zero.
Its manifest SHA-256 is
`1dc925131c31ed24e85df891c9b6bcbb6a879082e0db6ef2f38b02c5007dc6c8`.

No a1/a2 prompt, response, decision, or sample enters v3. V3 begins again at
the first frozen canary and plans 162 entirely new Provider requests.

## Frozen engineering change

V3 makes one request-level engineering change: `max_tokens` increases from
1024 to 4096. The purpose is to give the endpoint more room to finish the
required short JSON answer when the smaller budget is insufficient. A larger
cap does not relax the response schema,
does not turn hidden reasoning into a decision, and does not make a real model
deterministic.

V3 also adds response-termination provenance. For every completion, the SDK
choice `finish_reason` is recorded separately from content. A valid v3 Teacher
sample requires all of the following:

1. a non-empty string `message.content` that passes the unchanged strict JSON,
   schema, numeric-range, and account-feasibility parser;
2. requested model exactly `MiniMax-M2.7`;
3. safe SDK-reported alias exactly `HiggsAI`; and
4. SDK `finish_reason` exactly `stop`.

A missing, unsafe, unknown, `length`, `content_filter`, `tool_calls`, or other
finish reason fails the sample and immediately stops the run. Provider
`reasoning_content` or any equivalent hidden-reasoning field is never used as
decision content. The full SDK response serialization, when the SDK exposes
one, is preserved only in the 0600 private record for audit and is never
projected into a public sample row.

Failure classification has an explicit priority. If `message.content` is null,
missing, or non-string, the canonical failure remains
`provider_response_shape_invalid`, even when the same SDK choice reports
`finish_reason=length` (or another non-`stop` value). The termination value and
full SDK envelope are still retained under their public/private boundaries;
the response still fails closed and no parser attempt is claimed. This priority
preserves the directly observed response-shape fact without discarding the new
termination evidence.

## Identity and schema boundary

V3 uses `v2_teacher_request/0.2` for its model-request config projection and
Teacher public/private row schema. Version 0.2 adds explicit termination
provenance and the exact-`stop` acceptance contract. Consequently, its
`v2_model_request_config_hash` and `v2_full_effective_config_hash` must not be
reported as interchangeable with v1/v2 values.

The sample identity material deliberately remains
`v2_teacher_request/0.1`: `state_id`, the unchanged prompt hash, and replicate
index are hashed exactly as before. This preserves the preregistered sample
IDs, order, and first canary while the row/model-request projection advances to
0.2. A shared sample ID is only a plan identity; it does not authorize reuse of
any a1/a2 response or artifact. V3 still issues 162 entirely new requests.

## Honest-N and release gate

Requests are serial and retry-free. The first planned sample is the canary;
each later request is released only after the preceding sample has been
durably persisted as valid. Any resolved failure stops before another request.

Student fitting and every market cell remain forbidden unless exactly 162 of
162 new requests resolve valid, all 54 states have exactly three valid
replicates, all public rows retain the frozen requested/reported identities,
and every finish reason is exactly `stop`. No invalid response is converted to
`hold`; no predecessor or partial run is supplemented. Raw responses, SDK
envelopes, parsed decisions, failures, configuration identities, and honest-N
are retained under their public/private boundaries.

## Frozen commands

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_joint54x3_v3 \
  --temperature 0 --max-tokens 4096 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v3-dry-20260813-a1
```

Live:

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_finish_audit_joint54x3_v3 \
  --temperature 0 --max-tokens 4096 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260813-a3
```

## Interpretation boundary

V3 remains an exploratory endpoint and engineering-integrity pilot, not human
ground truth. Passing the gate would establish only that the frozen endpoint
produced a complete, schema-valid Teacher dataset suitable for the already
frozen distillation and conserving-market diagnostics. It would not establish
human realism, causal validity, external validity, a particular underlying
weight identity, or a deterministic Provider.

Scientific-semantic change declaration: **engineering/provenance additions
only**. V3 changes the request output budget, records the SDK termination state,
and rejects responses whose termination is not exactly `stop`; it does not add
or alter a market mechanism, behavioral variable, prompt, state, label, split,
Student, or market parameter. Because the repository intentionally
over-binds the V2 scientific component fingerprint to the managed entrypoint,
an implementation hash may nevertheless change; that conservative hash change
must not be misreported as a numerical scientific-mechanism change.
