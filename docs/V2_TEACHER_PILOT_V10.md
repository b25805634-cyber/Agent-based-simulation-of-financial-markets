# V2 Teacher endpoint pilot: audited transient-retry successor

Status: preregistered execution-reliability successor profile
`minimax_m27_higgsai_json_object_retry5_t1_p095_k40_timeout7200_output190000_joint54x3_v10`.
This protocol is frozen after the permanent failure of a9 and before any v10
live request. It does not repair, resume, supplement, merge, or reinterpret
a1, a2, a3, a4, a5, a6, a7, a8, or a9.

## Why a fresh successor is required

`v2-teacher-pilot-live-20260820-a9` is a permanently failed managed attempt.
Its authoritative
`results_v2_teacher_pilot/runs/v2-teacher-pilot-live-20260820-a9/run_manifest.json`
has SHA-256
`8b065ece52502230aed88e321cc3e3717fa6430c7549b0d526954235527a779e`.
The manifest and preserved failure metadata jointly establish:

- 162 samples were planned; 81 logical Teacher requests were attempted and
  resolved, 80 Provider responses were received and valid, one request failed,
  and 81 later samples were skipped;
- each of the first 80 responses reported model alias exactly `HiggsAI`,
  reported SDK `finish_reason=stop`, and passed response parsing and
  feasibility validation;
- the 81st logical request produced no Provider response and failed as a
  `provider_exception`; its private, mode-0600 failure record identifies an
  `APIConnectionError` after approximately five seconds;
- Teacher honest-N was 80. The failed request contributes no response, parsed
  decision, or Teacher observation; and
- Student fitting and market simulation were not run, so aggregated/Student
  and market honest-N remained zero. The all-or-nothing gate did not release
  downstream work.

The read-only monitor observed `ipsec0` and the endpoint route as present at
terminal time. That establishes only the route snapshot at observation time.
It does not prove continuous VPN health during the request, identify the layer
that raised `APIConnectionError`, or establish that a VPN event caused or did
not cause the failure. A9 remains a failed, non-reusable partial run.

## The v10 audited transient-retry policy

V10 preserves the complete v9 model-generation request payload:

- requested model `MiniMax-M2.7` and accepted reported alias `HiggsAI`;
- `max_tokens=190000`;
- `temperature=1`, `top_p=0.95`, and `top_k=40`;
- `response_format={"type":"json_object"}`;
- no request seed; and
- unchanged messages, prompt hash, and exact-`stop` termination contract.

V10 adds an application-layer technical retry policy around a single logical
Teacher request. One logical request may make at most five physical Provider
attempts: the initial attempt plus at most four retries. Retry delays are
exactly 10, 30, 60, and 120 seconds before physical attempts 2 through 5,
respectively. There is no jitter. The Provider SDK's own retry count remains
zero, so every application physical attempt is explicit and auditable rather
than hidden inside SDK behavior.

Only the following transient transport/service failures are retry-eligible:

- connection errors;
- OpenAI SDK `APITimeoutError` transport timeouts;
- the client hard-deadline `TimeoutError` raised by the 7200-second
  `asyncio.wait_for` boundary;
- HTTP 429 responses; and
- HTTP 5xx responses.

All other outcomes are terminal for that logical sample and are never retried.
In particular, v10 does not retry a received response because of null or
malformed content, reported-model mismatch, missing or non-`stop`
`finish_reason`, JSON/decision-parser failure, feasibility failure, content
policy result, authentication/authorization failure, context or other invalid
request, or any application-semantic rejection. This prevents selective
resampling until a desired label appears.

Every physical attempt must increment `provider_calls` accounting whose v10
unit is `physical_provider_attempts_after_cache_and_replay`; SDK retries are
disabled and therefore cannot create hidden attempts. The separate
`llm_logical_requests` accounting remains logical-sample based. Every physical
attempt also appends a mode-0600 private audit record with its logical sample identity,
physical attempt index, timing/status, retry classification, and sanitized
error metadata. Public artifacts expose only safe aggregate attempt/retry
accounting. A successful retry resolves exactly one logical Teacher sample and
can contribute at most one unit to honest-N. Exhausting all five physical
attempts resolves exactly one failed logical sample, contributes zero honest-N,
and triggers the existing fail-fast gate. Physical attempts must never be
counted as extra replicates, Teacher rows, logical requests, or honest-N.

Retries use the same frozen sample ID, state, prompt bytes, model request, and
replicate index. They remain strictly sequential: no later logical sample is
released while a retry or retry delay for the current sample is pending.

## Inherited scientific and execution contract

V10 inherits v9's 54-state x three-replicate plan, 162-sample order, first
canary, master seed 20260811, workers 1, state design, prompt, decision parser,
action/intensity labels, family split, privacy boundary, 400-epoch Student,
and 48-agent x 60-round x three-seed paired conserving-market diagnostic.
Exactly 162 valid new logical Teacher samples, exactly three per state, remain
mandatory before any Student or market work may begin.

The per-physical-attempt transport values remain:

- 7200-second hard wall-clock deadline;
- 7200-second HTTPX read, write, and pool phase-inactivity timeouts; and
- 10-second HTTPX connect timeout.

The application retry maximum and delay schedule are new. SDK retry remains
zero. If each of five physical attempts reaches its full 7200-second hard
deadline, one logical request plus the four preregistered delays can occupy up
to 36,220 seconds (10 hours, 3 minutes, 40 seconds). This is an upper-bound
execution risk, not an expected latency or endpoint-health claim.

V10 uses additive request/public-private row schema
`v2_teacher_request/0.5` so logical-versus-physical attempt provenance and the
inherited sampling/format contract remain explicit. Sample identity remains
`v2_teacher_request/0.1`, derived from
`state_id + prompt_hash + replicate_index`; retries cannot change that
identity, sample order, or canary. Execution schema advances to
`v2_attention_execution/0.3` to bind the new application retry policy,
eligibility classes, attempt maximum, delays, and SDK-retry-zero rule.

The model-generation wire payload is unchanged from v9, but the profile and
request/row schema change conservatively identify the new acquisition
condition. Technical retries can change whether and when an observation is
obtained after transient failure. They must therefore be reported as an
execution/acquisition semantic change and cannot be silently treated as the
same effective experiment.

## Non-reuse and honest-N boundary

V10 starts at the frozen first canary and plans 162 entirely new logical
requests. It does not reuse the 80 valid a9 records and does not resume or
retry a9's failed 81st sample. No a1-a9 response, parsed decision, row,
physical attempt, or honest-N enters v10. Historical attempts cannot be
selectively supplemented or merged to reach 162.

Within v10, multiple eligible physical attempts belong to one logical request;
they do not create multiple Teacher observations. Every accepted logical row
still requires exact alias, exact `stop`, valid decision JSON, and feasibility.
Private rationale remains excluded from public samples, social information,
Student features, and market state. Raw responses and attempt audit records
remain private mode 0600.

## Config-identity interpretation

V10 uses `v2_teacher_request/0.5` for model-request projection and Teacher
rows while preserving v9's exact generation payload. The new profile ID and
schema conservatively distinguish the audited retry acquisition condition, so
`v2_model_request_config_hash` is expected to differ even though model,
messages, cap, sampling tuple, and response format do not. This difference
must not be misreported as a new generation field.

V10 uses `v2_attention_execution/0.3`, binding:

- `application_max_attempts=5`, meaning at most five physical attempts for
  each one logical Teacher request;
- retry delays `[10, 30, 60, 120]` seconds;
- the exact eligible failure classes: connection, timeout, HTTP 429, HTTP 5xx;
- non-retryable semantic/validation classes;
- `provider_sdk_retry_count=0`; and
- the inherited 7200/7200/10-second transport values.

V10 `provider_calls` is explicitly measured in
`physical_provider_attempts_after_cache_and_replay`, while
`llm_logical_requests` retains logical-request units. Neither field may be
used as a substitute for the other.

Consequently v10 must have distinct `v2_execution_config_hash` and
`v2_full_effective_config_hash` values. Historical execution schemas and
manifests remain authoritative as written and must not be backfilled or
reinterpreted as retry-enabled.

The conservative scientific component fingerprint may move because the
managed entrypoint changes. That movement is not by itself a state/action,
Student, or market-mechanism change. Final reporting must read all four exact
named hashes and schemas from the a10 managed manifest and name its exact
path/execution context. No a9 hash may be reused as an a10 identity.

## Frozen commands

The retry policy has no new public CLI flags. Its exact attempt maximum,
delays, eligibility rules, and SDK-retry-zero value are frozen by this opt-in
profile and must appear in the execution identity and managed artifacts.

Dry-run (zero Provider construction and zero network access):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_json_object_retry5_t1_p095_k40_timeout7200_output190000_joint54x3_v10 \
  --temperature 1 --max-tokens 190000 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --dry-run --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-v10-dry-20260820-a1
```

Live (must run in an explicitly authorized endpoint-reachable execution
context; this is a fresh set of exactly 162 logical requests):

```bash
OPENAI_BASE_URL="$V2_PILOT_BASE_URL" OPENAI_API_KEY="$V2_PILOT_API_KEY" \
python3 -m experiments.v2_attention_market \
  --provider openai --model MiniMax-M2.7 \
  --pilot-profile minimax_m27_higgsai_json_object_retry5_t1_p095_k40_timeout7200_output190000_joint54x3_v10 \
  --temperature 1 --max-tokens 190000 \
  --states 54 --replicates 3 --workers 1 --seed 20260811 \
  --training-epochs 400 \
  --market-agents 48 --market-rounds 60 --market-seeds 3 \
  --live --confirm-request-count 162 \
  --out results_v2_teacher_pilot \
  --run-id v2-teacher-pilot-live-20260820-a10
```

Implementation acceptance commands:

```bash
python3 -m unittest tests.test_v2_attention_market_entrypoint
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q nmsim experiments tests
git diff --check
```

The preflight and live process must use the same network-reachable execution
context. Preflight metadata calls are not Teacher requests and must not enter
logical request, physical attempt, or honest-N counts.

## Interpretation and risk boundary

V10 remains an exploratory endpoint and engineering-integrity pilot, not
human ground truth. Passing the gate would establish only that the route
produced a complete, schema-valid Teacher dataset under the frozen v9 request
and v10 retry-acquisition contract. It would not establish human realism,
causal validity, continuous endpoint availability, Provider determinism, or
that retries are harmless to the distribution of obtained observations.

Five physical attempts do not guarantee resolution. A connection/OpenAI SDK
timeout/client hard-deadline `TimeoutError`/429/
5xx condition may persist through all attempts; the process or VPN may be
interrupted during an attempt or delay; the gateway may reject the request;
or a received response may still terminate with `length`, contain null or
malformed content, or fail decision/feasibility validation. Non-retryable
response and semantic failures remain terminal by design.

Scientific-semantic change declaration: **state/action, model-generation
payload, Student, and market mechanisms are unchanged; the audited technical
acquisition policy changes**. V10 adds at most five application physical
attempts with fixed delays and a narrow transient-error allowlist, keeps SDK
retry zero, advances request/row schema from 0.4 to 0.5 and execution schema
from 0.2 to 0.3, and changes run/profile identity. Logical sample count,
replicate count, sample identity/order, acceptance gate, and honest-N remain
unchanged. The retry policy may affect missingness and completion, so v10
cannot support a simple causal claim that any result difference is unrelated
to acquisition.
