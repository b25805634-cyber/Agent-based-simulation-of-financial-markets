# Available observations and repeated rollout fidelity probes

This is a new development protocol, not a reinterpretation of a10, the 10k
Teacher study, or the first 28 markets. It addresses two observed gaps: the
daily market has no measured intraday range, and static held-out agreement does
not establish Student accuracy on its own visited states.

## Available-only observation contract

The existing `legacy_proxy` mode remains the default for compatibility. New
work uses the explicit `--observation-policy available_only` mode under
`information-market/1.1.0`. It never supplies the daily-close change as an
intraday high-low range. `intraday_range_5d_mean` is omitted, and its Student
visibility mask is zero. Undefined turnover ratios, including zero/zero,
are also omitted when their prior-five-volume denominator is zero. The existing
volume-z zero-variance rule is retained because it is explicitly part of the
original Teacher feature semantics.

The historical allocation IDs remain internal grouping keys. Their effective
field sets are recorded, rather than claiming that a now-unavailable eighth
price field is visible. The price-heavy view supplies seven price fields,
two financial fields and two news fields when turnover is defined; other
views retain their available fields. No identity label is sent to the policy.
Every new-mode decision records its exact visible fields. Legacy-mode result
structure and numerical behavior remain compatible.

This removes a semantic substitution, but introduces a visibility pattern the
old Student did not see during training. That is a separate fidelity question,
not proof that the model is now accurate. No old weight, label or run is changed.

## Prospective probe selection

`experiments.rollout_fidelity --task plan` accepts a finished available-only
market run and its exact originating Student run. All registered artifacts are
verified. Only the selected Student's market cells are eligible; recorded
predictions are checked against the loaded model. Full daily ledgers, not just
one convenient account per profile, supply candidate observations.

The default budget is 24 states, K=5 requests per selected state (at most 120
logical requests). The actual count is emitted in the frozen plan. Selection
strata are information-allocation ID x quote mapping x daily-time third x
portfolio boundary (empty/interior/full). Deterministic SHA ranks select states
within each stratum and round-robin across seeded strata. Duplicate visible
states are not separate cases. Candidate counts, selected counts and uncovered
strata remain explicit. This is coverage-oriented diagnosis, not a
population-weighted estimate. It does not look at Teacher answers or outcomes.

Replicates are spread over complete passes through the cases, with case order
rotated each pass. Prompts contain only the exact effective visible values,
own account, neutral field definitions and action contract. Unavailable fields,
source IDs, model predictions and identity labels are absent from prompts.
The unchanged neutral system instruction is reused; the new payload schema is
`available-rollout-teacher-prompt/1.0`.

## Acquisition and preservation

The plan is a finished managed input to a NEW acquisition run. Real requests
require `--provider openai --live --confirm-request-count <exact plan count>`.
`--dry-run` constructs no Provider. `fake_test_teacher` is a constant-hold
engineering null; it contributes zero endpoint responses and zero humans.

Real requests retain the historical sampling tuple: MiniMax-M2.7, temperature
1, top-p 0.95, top-k 40, JSON object format and 190,000 max tokens. The required
SDK-reported alias is HiggsAI and the termination must be stop. Alias identity
does not prove underlying weights. The exact route identity, adapter source,
sampling tuple and plan are recorded separately from execution identity.

There are no SDK retries and one application attempt per logical request.
At most four requests run concurrently (default two), with the existing
10-second connection timeout and 7,200-second phase/hard timeout. A batch with
only Provider/response-shape failures stops further batches; remaining slots
remain unattempted/unresolved and the managed run is failed, never resumed or
silently stitched to a successor. These transport rules are execution controls,
not extra scientific replicates.

The complete private response/SDK envelope is fsynced to a 0600 exclusive file
before parsing. Public projection follows, then counters. A callback failure
drains other requests owned by that batch before closing streams. Raw strings
have separately named UTF-8 byte SHA values; secret sanitization remains the
existing private-record policy. Parse failures are failures, not hold labels.
The plan, public samples, public attempt ledger, private records and summary
are collected by ManagedRunContext; old inputs are rechecked at the end.

## Metrics and limits

For each state report valid K, all failures, Teacher empirical action
probabilities, Student cross entropy and total variation; for buy/sell
separately report Teacher mean intensity, sample variance, Student intensity,
MAE and conditional N. Zero Student probability on an observed action is
reported explicitly with undefined/infinite CE represented as null, not hidden
behind an arbitrary floor. Also report probability mass assigned to actions
that violate the Teacher's exact empty/full portfolio constraint.

Finite-K estimates are noisy, and requests may share endpoint/time effects.
No universal acceptance threshold or human-validity claim is introduced. Human
comparison requires its own matched task evidence. The published six-asset
reference is not silently converted into this one-asset probe.

## Commands

From the `agent-market-loop` worktree:

```sh
python3 -m experiments.information_market --task simulate \
  --model-run results_information_market/runs/information-student-10k-20260909-a1 \
  --agents 200 --rounds 60 --seeds 3 --policies selected --quote-rule both \
  --observation-policy available_only \
  --run-id information-market-available-20260909-a1 --out results_information_market

python3 -m experiments.rollout_fidelity --task plan \
  --market-run results_information_market/runs/information-market-available-20260909-a1 \
  --model-run results_information_market/runs/information-student-10k-20260909-a1 \
  --max-states 24 --replicates 5 --run-id rollout-fidelity-plan-20260909-a1

python3 -m experiments.rollout_fidelity --task acquire \
  --plan-run results_rollout_fidelity/runs/rollout-fidelity-plan-20260909-a1 \
  --provider fake_test_teacher --run-id rollout-fidelity-fake-20260909-a1
```

A real acquisition is a separately named run with `--provider openai --live`
and an exact `--confirm-request-count` matching the frozen plan. The original
`experiments.information_market` entrypoint remains entirely Provider-free.

Learning curves and the published reference use the independent managed
`experiments.information_diagnostics` entrypoint. Both are offline, preserve
their inputs, and record four named hashes under `information-diagnostics/1.0`.
The reference importer pins the official CSV SHA and protects reconstructed
individual records with mode 0600. It never feeds the future-marked
`result_final` into a predecision state.
