# Information Student: training-family learning curves

This is an exploratory, training-only data-efficiency diagnostic for an existing
`information-student-experiment/1.0` study. It answers whether the same fixed
prior, linear model and MLP improve on the existing validation partition as more
of the **original training families** are supplied. It does not request Teacher
labels, deploy a model, select a winner, evaluate the old test again, or establish
human behavioral validity.

## API and formal boundary

`nmsim.information_learning_curve.learning_curves(records, original_study,
fractions=(.125, .25, .5, 1.0), epochs=120, hidden_dim=16, backend='auto')` is a pure
library returning JSON-compatible `information-student-learning-curves/1.0`.
The caller must pass the public source records and a previously verified Student
study, including its original split, training seed and normalized-source SHA.
The library does not access files, Provider, the network, or a run context.
Official execution must use a registered entrypoint and `ManagedRunContext`;
in-memory tests are diagnostics, not provenance-complete research runs.

The managed caller must validate and record acquisition manifest identity,
public source file-byte SHA, original study artifact identity, execution context
and its precise configuration hash schemas. The library's reference to the old
normalized-source hash is not a new assertion that supplied bytes have been
verified. No private response, private rationale, prompt or endpoint is needed.

## Frozen split and test exclusion

The original split semantic SHA is verified before fitting. Sample, latent and
family rosters must match the original sample/family/latent assignments and
requested counts. Every latent state still needs all four requested information
views, including acquisition failures. Related states with an explicit shared
`family_id` cannot be separated. The original 70/15/15 assignments are copied
unchanged into the output.

Test rows are excluded using only schema, sample ID, latent ID, family ID and
profile ID. Their decisions, validity status, response hashes, observations and
attempt fields are **not accessed**, including for validation or normalization.
Consequently missing/malformed/changed test-label payloads do not affect any fit,
curve, or semantic hash. The managed caller still verifies the actual public
source artifact's bytes before a formal run; the library does not bypass that
source-provenance obligation.

Only non-test rows enter the existing `information_student.prepare_records`
validator. Its temporary split on this reduced roster is discarded in full:
all valid rows, from every temporary partition, are then assigned exclusively
using the original study's sample assignments. This reuse preserves existing
source schema/hash, paired-view, private-field, numerical-domain and failure
validation without ever normalizing an original test payload. Validation uses
the same fixed rows for every curve point. Failed requests remain in requested
denominators and family rosters; they never become training labels.

The old study already disclosed its test results. That test remains development
evidence, not an untouched final evaluation. New confirmatory claims after these
curves require a newly frozen, independent evaluation source.

## Nested subsets and unchanged fitting

Training families are ranked by the SHA-256 of schema, original training seed
and family ID (`information-student-nested-training-families/1.0`), with family
ID breaking ties. A fraction uses the first `max(1, ceil(fraction * N_families))`
families. Every associated latent state/view remains in that subset. Requested
fractions must strictly increase in `(0, 1]`; adding an intermediate fraction
cannot change another point. Small family counts may give repeated effective
subset sizes; both requested and effective fractions are reported. A selected
subset with zero valid rows fails explicitly instead of silently widening it.

Each point starts from fresh models using the same original seed. A new
standardizer is fitted only to that point's valid training rows. The prior is
the existing feature-blind null. Linear and MLP fits reuse the unchanged V2
initialization, loss and full-batch Adam; the optional NumPy path delegates to
`information_student._numpy_fit`. Metrics delegate to `_metrics`, but its inputs
are restricted to original train/validation rows. These two private helpers are
intentional compatibility dependencies, not rewritten scientific kernels.

Defaults remain 120 epochs and hidden width 16: 5 prior stored coefficients,
285 linear coefficients and 997 MLP coefficients (standardization excluded).
The MLP count is `62 * hidden_dim + 5`. At full training fraction and equal
backend/seed/epochs/width, fitted payloads and train/validation evaluations match
the original Student implementation. Backend behavior and optimizer settings
are documented in [INFORMATION_STUDENT.md](INFORMATION_STUDENT.md); this addition
does not alter old training defaults or serialized model schemas.

## Output and interpretation

Each point includes family IDs, latent/requested/valid/failed counts, valid
training sample IDs, train-observation semantic SHA, standardizer, serialized
models and their SHA values, parameter counts, loss histories, and train and
validation metrics. Metrics include CE, Brier score, accuracy, conditional
intensity MAE, F1/confusion matrix and per-information-view comparisons.

The output separately names the original full normalized-record reference hash,
the normalized non-test record hash, the label-free roster hash, the unchanged
split semantic hash and each training-observation/model hash. None is a raw file
SHA, acquisition manifest SHA or one of the managed configuration hashes.
`curve_semantic_hash` hashes the entire result before adding itself and the
`runtime_information` object. Measured standardizer, fitting and evaluation
seconds use `time.perf_counter` and are informational, not scientific identity
or deterministic performance guarantees.

There is one nested family order and one initialization seed. These curves do
not provide sampling uncertainty, a universal data requirement or a guarantee
that ten times more data will help. Fixed-epoch optimization, target noise,
model capacity and coverage all affect their shape. K=1 Teacher answers are
neither independent humans nor within-state probability estimates. A better
validation score is evidence about Teacher imitation on that partition; human
matching and closed-loop performance still require separate checks.

Scientific semantic change: an additive diagnostic of existing Student
training data quantity. Historical source data, prompts, split assignments,
models, training kernels, market behavior and test interpretation are unchanged.

## Verification

The synthetic tests cover exact original assignments, nested whole families,
fixed validation, subset-only standardization, excluded test payloads that raise
on any attempted access, full-fraction equality to the original fitter, source
contract failures, honest failure denominators, ordering stability, hash/runtime
boundaries and optional NumPy/Python parity. They need no ignored result files
and make no external requests.

```sh
python3 -m unittest tests.test_information_learning_curve -v
```

Implementation verification (2026-09-09; synthetic diagnostic inputs only):

```text
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-learning-curve-pycache python3 -W error -m unittest tests.test_information_learning_curve -v
Ran 10 tests in 0.561s
OK
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-learning-curve-pycache python3 -W error -m unittest tests.test_information_student tests.test_information_learning_curve -v
Ran 26 tests in 0.660s
OK
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-learning-curve-pycache python3 -m compileall -q nmsim/information_learning_curve.py tests/test_information_learning_curve.py
(no output; exit 0)
git diff --check
(no output; exit 0)
```
