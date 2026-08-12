# V2 Teacher endpoint pilot: immutable successor protocol

Status: preregistered exploratory successor profile
`minimax_m27_request_higgsai_reported_joint54x3_v2`. This document is an
immutable successor to `V2_TEACHER_PILOT.md`; it does not edit, reinterpret, or
supersede the recorded outcome of the v1/a1 attempt. This protocol must be
frozen before the first live v2/a2 Teacher request. It does not establish human
validity and must not be described as confirmatory evidence or human ground
truth.

## Scope and fixed configuration

This pilot exercises the existing V2 limited-attention
Teacher → Student → conserving-market pipeline. It does not modify or consume
the V1 Persona market, and no Teacher private rationale may enter a public
artifact, Student feature, social channel, or market observation.

The requested model and the SDK-reported model alias are separate provenance
fields. They must be recorded without rewriting one into the other:

| Identity field | Frozen value |
|---|---|
| Requested model | exactly `MiniMax-M2.7` |
| Sole accepted SDK-reported alias | exactly `HiggsAI` |

On 2026-08-12, the operator confirmed that this Higgs gateway reports
`HiggsAI` when the request selects `MiniMax-M2.7`. This operator confirmation
authorizes the explicit request/response alias mapping for this pilot only. It
does **not** verify the endpoint's underlying weights, model version, serving
implementation, or equivalence to any independently distributed model.
Neither code nor reports may replace the requested value with the reported
value, replace the reported value with the requested value, or use this mapping
to claim an underlying model identity.

All other fixed settings are:

| Field | Frozen value |
|---|---:|
| Profile ID | `minimax_m27_request_higgsai_reported_joint54x3_v2` |
| Provider protocol | OpenAI-compatible, explicit live gate |
| Designed states | 54 |
| Replicates per state | 3 |
| Planned Teacher requests | 162 |
| Master seed | 20260811 |
| Temperature | 0 |
| Maximum output tokens | 1024 |
| Workers | 1 |
| Student training epochs | 400 |
| Market agents per run | 48 |
| Market rounds per run | 60 |
| Paired market seeds | 3 |

Temperature zero is a request parameter, not a determinism claim. Real
Provider responses may remain stochastic, so every replicate retains its own
sample identity and raw response.

The frozen endpoint route identity remains the secret-free
`endpoint_identity_sha256`:

```text
66e21f44b31bae951b37de32684b004d81c0821d956eb3351432770f11aad0c1
```

The raw endpoint URL, API key, userinfo, query values, and other credentials
are deliberately absent from this document and all public artifacts. Before a
live request, the v2 dry-run manifest's
`v2_model_request_config.endpoint_identity.endpoint_identity_sha256` must equal
the value above. A mismatch is a preflight failure, not permission to update
this protocol in place.

## Permanent disposition of v1/a1

The live v1 attempt `v2-teacher-pilot-live-20260812-a1` is permanently failed.
Its frozen accounting is:

| Measure | Recorded value |
|---|---:|
| Planned | 162 |
| Attempted | 1 |
| Resolved | 1 |
| Valid | 0 |
| Skipped | 161 |
| Student honest-N | 0 |
| Market honest-N | 0 |

No downstream Student fitting or market simulation ran. The a1 canary,
response, failure classification, and honest-N remain part of that failed run.
They must not be reused, relabeled, repaired, supplemented, or merged into v2.

The v2/a2 attempt is a completely new 162-request plan with a new immutable
managed run directory and run ID. Its first planned sample is a new canary; it
does not continue at request 2 and does not inherit any valid or invalid sample
from a1. The a1 result is operational evidence motivating this successor
protocol, not a member of the v2 Teacher dataset.

## Why the design remains 54 x 3

Under the frozen deterministic state generator, 45 states leave two of the
nine designed tape × position cells with only four families. It therefore
falls back to tape-only stratification and cannot support the intended joint
nine-cell split.

A code-derived scan of this exact generator found that 54 is the smallest
state count for which all nine cells have at least five families. The 54-state
cell counts remain:

| Tape regime | Cash | Mixed | Invested |
|---|---:|---:|---:|
| Decline | 5 | 7 | 6 |
| Flat | 6 | 6 | 6 |
| Rise | 7 | 6 | 5 |

The frozen family split is 36 train, 9 validation, and 9 test states. Each
partition contains all nine tape × position cells. The request budget is
therefore 54 × 3 = 162. No 45-state result or v1/a1 sample may be relabeled,
merged, or presented as this joint-stratified v2 pilot.

## Canary, serial release, and immediate stop

The first sample in the new frozen 162-sample v2 plan is the canary; it is not
an extra request and is not selected after seeing a response. Requests are
released strictly serially with one worker and zero Provider-level retries.
The remaining 161 requests may begin only after the canary is durably recorded
as valid.

After the canary, every response is subject to the same fail-fast rule. The run
stops before releasing another request after any of the following:

- Provider or transport failure;
- missing response, invalid response shape, or unavailable raw response;
- JSON, schema, range, or action parse failure;
- an action that is infeasible for the planned account state;
- missing, unsafe, ambiguous, or malformed reported model identity; or
- an SDK-reported model alias that is not exactly `HiggsAI`.

The requested model must independently remain exactly `MiniMax-M2.7` for every
request. A different requested value is a protocol violation even if the SDK
reports `HiggsAI`. Conversely, a response alias other than `HiggsAI` is a
protocol violation even if the request used `MiniMax-M2.7`.

The failed completion must first be persisted under the normal honest-N and
durability rules. Fail-fast does not permit discarding evidence or converting
an invalid action to `hold`.

## Teacher acceptance gate

Student fitting and all market cells are forbidden unless the completed v2
Teacher phase passes every condition below:

1. exactly 162 of 162 newly planned requests were attempted and resolved;
2. all 162 responses are valid, so Teacher failures, unresolved attempts, and
   skipped attempts are all zero;
3. every one of the 54 designed states has exactly three valid replicates;
4. every request records the requested model exactly as `MiniMax-M2.7`; and
5. the unique set of safe SDK-reported model aliases is exactly `{HiggsAI}`.

The gate is operational and integrity-preserving; it is not a claim that the
Teacher behaves like a human or that the endpoint used particular underlying
weights. Passing it permits the preregistered exploratory Student and market
diagnostics to run. Failing it leaves Student and market honest-N at zero for
that run.

## Failure, privacy, and non-reuse policy

An interrupted or rejected run is a permanent partial run. It must never be
overwritten, resumed by treating flat files as reusable children, selectively
supplemented with replacement samples, merged with another partial run, or
promoted to a complete dataset. A later attempt requires a new run ID and a
new immutable managed run directory; earlier partial artifacts remain labeled
as failures.

Full prompts, raw responses, private rationales, and detailed errors are
private artifacts created exclusively with mode `0600`. Public projections
contain only permitted state/sample identities, parsed decisions, separately
labeled safe requested/reported model fields, and accounting. Raw responses,
parsed decisions, failures, configs, and honest-N must all be preserved.
Historical result directories are never overwritten.

## Frozen command templates

The operator supplies the endpoint and key through pre-existing environment
variables; their values must not be pasted into this document or a public run
report. Dry-run constructs no Provider and performs no network request.

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_request_higgsai_reported_joint54x3_v2 \
  --temperature 0 --max-tokens 1024 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v2-dry-20260812-a1
```

The live command differs only in its explicit live authorization, exact call
confirmation, and unique a2 run ID:

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_request_higgsai_reported_joint54x3_v2 \
  --temperature 0 --max-tokens 1024 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260812-a2
```

The live command must run from the reviewed clean commit used by the v2
dry-run. The dry-run manifest records the exact
`v2_scientific_config_hash`, `v2_model_request_config_hash`,
`v2_execution_config_hash`, and `v2_full_effective_config_hash`; those exact
values are not guessed in this document. Dry-run and live execution hashes are
expected to differ because their execution contexts differ, while all frozen
scientific and model-request fields must remain traceable and reviewed.

## Interpretation boundary

This is an exploratory endpoint pilot and engineering integrity test. It does
not prove that the gateway served any particular underlying weights, that the
Teacher represents real traders, that the Student is a human behavioral model,
that any simulated path is a bubble, or that a mechanism has causal or external
validity. The operator-confirmed alias mapping is a routing/provenance contract,
not scientific validation. No human-ground-truth acceptance threshold is
frozen here. Any later confirmatory claim requires a separately reviewed
protocol, human comparison data, and thresholds fixed before looking at
confirmatory results.
