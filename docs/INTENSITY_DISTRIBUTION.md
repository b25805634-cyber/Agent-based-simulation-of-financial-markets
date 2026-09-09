# Exact-support Student sizing distributions

Status: new exploratory mechanism, frozen before the first managed fit of real
Teacher labels. This library changes no historical Student payload, action
model, market rounding rule, or CLI default. It adds an optional law for the
intensity conditional on an already chosen buy or sell action. It is not a
claim about human heterogeneity, improved market realism, or Teacher fidelity.

## Problem and controls

A sigmoid regression head estimates a conditional intensity mean and normally
cannot return exactly one. Even if observed Teacher decisions include exact
full liquidation, applying integer floor to a mean below one can retain a
residual position. Increasing the mean, changing floor to round, or fabricating
full-exit labels would each introduce a different mechanism. This addition
instead preserves the observed intensity atoms and can sample exactly one
when it has positive fitted mass. Whether those sampled exits improve a
closed-loop comparison is an empirical question.

Three sizing comparisons must remain distinct:

- `legacy_mean`: the unchanged old selected Student intensity head.
- A fitted distribution's mean: deterministic sizing under the new fitted law.
- A draw from that same fitted distribution: stochastic sizing with the same
  conditional mean and support. Compare these two with paired market inputs
  and explicit independent sizing draws to isolate the sampling mechanism.

The primary market comparison must use `--quote-rule independent`. Under
`legacy_intensity_linked`, a different intensity changes both quantity and
quote urgency; that arm measures a compound change, not sizing alone.
New market modes explicitly require `--observation-policy available_only`
and use result schema `information-market/1.2.0`. The existing default
`legacy_mean` and its old result schemas remain unchanged. Sampling uses a
separate deterministic `(seed, day, agent, "sizing")` namespace; the action
and quote streams are not advanced by an extra sizing draw.

The empirical action-conditioned law is the feature-blind distribution control.
The conditional softmax uses observed information through the frozen original
MLP representation. The caller may choose either candidate explicitly for an
ablation; the default selection described below uses validation only.

## Frozen design

The API is
`fit_intensity_distributions(records, original_study, epochs=120, backend='auto')`.
`nmsim.intensity_distribution` is pure and opens no files, constructs no
Provider, performs no network request, and creates no run. A formal caller
must use the central entrypoint registry and `ManagedRunContext`, authenticate
the original study and public records' byte hashes, and save the result as a
new run. Synthetic tests are explicit unmanaged diagnostics, not research runs.

1. Reuse the original complete family/sample split. The existing
   `information_learning_curve` ingestion helper reads test identity metadata
   only and removes every test payload before normalization. Missing or
   malformed test decisions, observations, status and attempts are never read.
   All four views, failed requests, family identity and sample membership must
   retain the original roster. A failure does not become an observation.
2. Authenticate the selected original action payload and original MLP payload
   against their saved serialization hashes and encoder identity. Recompute
   the canonical **training** observations hash and the train-only standardizer
   and compare them to the frozen MLP. A changed training label or state fails
   closed. Neither original fitting nor action model selection is repeated.
3. For buy and sell separately, let the support be the sorted unique exact
   floating-point intensity values in that action's original valid training
   observations. Counts contain actual rows only. There is no binning,
   tolerance-based endpoint classification, smoothing, interpolation, synthetic
   row, prior pseudocount or injected atom. Exact zero or one is present if and
   only if the corresponding action has such a training label. Absence of an
   action in training is an explicit error; it is not filled from validation.
4. The empirical candidate assigns each support atom its observed training
   frequency. The conditional candidate is a separate linear softmax over the
   frozen original MLP hidden tanh vector for each action. This vector uses the
   original 56-field visible-value/mask/account encoding and its frozen train
   standardizer. Original hidden and action weights are never updated.
5. Initialize conditional softmax weights at zero and biases at the log of
   empirical probabilities. Fit action-conditional categorical cross entropy
   plus `0.5 * 1e-4 * sum(weight**2)` using full-batch Adam: learning rate 0.02,
   beta1 0.9, beta2 0.999, epsilon 1e-8. Biases are not regularized. The epoch
   budget is fixed, with no early stopping. One support atom gives a degenerate
   law and zero categorical loss without creating an extra class.
6. Select empirical or conditional separately for buy and sell by lowest mean
   original-validation CRPS. Exact ties prefer empirical. If validation has no
   observation of an action, retain empirical with `n=0` and null scores for
   that action. The old mean is reported as a diagnostic, not a third selection
   candidate. This is a new sizing selection; old action selection is fixed.
7. Use no original test label, observation, prediction, metric or response hash
   for fitting, support formation, selection or reporting. The old disclosed
   test remains development evidence and is not a fresh final holdout.

The support capacity is 256 exact atoms per action. Exceeding it fails rather
than silently changing the scientific support. The estimated fitting work is
`epochs * sum(action_training_rows * action_support_size * hidden_width)`.
It is capped at 2,000,000,000 for NumPy and 50,000,000 for Python, to prevent a
fallback backend from silently performing a prohibitively large fit. A refusal
requires an explicit backend or new frozen epoch budget; no automatic sampling,
downsampling, epoch reduction or binning occurs. NumPy remains optional and
introduces no production dependency; acceleration uses explicit unoptimized
`einsum` contractions. Serialized inference uses only the standard library.

## Scores and their limits

For categorical predictive law `F`, arbitrary continuous observation `y`, and
independent `X, X' ~ F`, the primary score is

`CRPS(F, y) = E|X-y| - 0.5 E|X-X'|`.

This proper score is computed on the actual intensity scale [0, 1]. A
validation intensity absent from training support is still a valid observation
with a finite score. It is neither snapped to a bin nor assigned an artificial
infinite cross entropy. The exact off-support count is reported for each law.

Additional diagnostics, conditioned on the **observed** buy/sell action, are:

- `wasserstein1_to_point`: `E|X-y|`. This descriptive distance alone is not a
  proper conditional distribution scoring rule; it can favor a median point.
- `marginal_wasserstein1`: exact distance between the average predicted CDF and
  the empirical intensity CDF for that partition/action. Matching a marginal
  does not establish conditional calibration or behavior along a rollout.
- Mean prediction MAE, signed mean error, observed and predicted mean.
- Exact endpoint 0 and 1 event Brier scores, observed counts, predicted endpoint
  probabilities, and ten fixed reliability bins `[0,.1),...,[.9,1]`. Empty bins
  have `n=0` and null averages. ECE is a bin-dependent descriptive diagnostic.

K=1 per state/view provides sparse conditional labels across differing states.
The new distribution is a fitted generalization over those observations, not
an identified per-state Teacher distribution or a population of independent
human traders. Scores have row denominators, not human-participant counts.
Paired profiles and related families are not independent replications. No
confidence interval or real-world validity claim is generated by this fit.

## Prediction and compatibility

`make_distribution_predictor(study, original_study, candidate='selected')`
loads and validates hashes once. The optional candidate is exactly `selected`,
`empirical`, or `conditional_softmax`. Its callable receives
`(visible_fields, account_state)` and returns:

```text
action_probs              unchanged original selected Student [buy,hold,sell]
intensities               unchanged original selected Student [buy,sell]
intensity_means            new distribution means [buy,sell]
intensity_distributions    {buy:{support,probabilities}, sell:{support,probabilities}}
```

`validate_distribution` rejects nonfinite, duplicate, unordered, out-of-range,
or non-normalized laws; it does not normalize arbitrary weights. Probability
sum tolerance is 1e-10 for roundoff. `quantile_sample(law, u)` uses the first
positive atom whose cumulative mass exceeds `u`. At `u=0`, zero-probability
leading atoms cannot be selected. At `u=1`, a possible rounded uniform endpoint,
the last **positive** atom is returned; zero-mass trailing atoms cannot be
selected. The function owns no RNG, consumes no extra draws, and performs no
quantity rounding. Market integration must freeze and report how the sizing
draw is derived independently of its action and quote randomness.

Keeping legacy `intensities` prevents accidental behavior changes in old
consumers. A caller must explicitly opt into a mean or sampled new law. Exact
full-exit intensity remains exactly one; the existing market handles it using
its unchanged lot, affordability, account and clearing rules.

## Identity and accounting

The new study schema is `information-intensity-distribution-study/1.0`; its
conditional head schema is `frozen-mlp-exact-support-softmax/1.0`. The study
records original model and encoder identity, the original normalized-data hash
as a reference, the newly computed non-test normalized-record hash, complete
label-free roster hash, original split hash, and canonical train/validation
observation hashes. They are distinct from source-file byte SHA and manifest
SHA. Source-byte authentication remains the managed caller's responsibility:
an old whole-dataset hash cannot be recomputed while deliberately not reading
test payloads. The managed run must supply its four precisely named config
hashes and source paths; this pure library cannot infer execution provenance.

Train/validation sample and family IDs, excluded test IDs, per-action training
IDs, atom counts, candidate model hashes, parameter counts, losses, backend and
versions are retained. A conditional head stores `support_size * (hidden_width
+ 1)` fitted coefficients; empirical probabilities store `support_size`
coefficients with a sum constraint. Reused frozen models and standardizer
statistics are reported separately and are not newly trained parameters.

`study_semantic_hash` covers every scientific result field except the explicitly
informational `runtime_information`. It does not certify original acquisition
bytes, real human observations, historical identity, or resume eligibility.
Loading validates the original source/model/encoder/split binding, study and
candidate hashes, exact support counts, probabilities and parameter dimensions.
The artifact contains no private reasoning or raw Teacher response.

## Verification

The tests use public synthetic four-view families, not ignored historical
results. They check exact full-sale outputs, inverse-CDF boundaries, CRPS against
off-support observations, finite proper-score behavior, unchanged action and
legacy intensity predictions, exact train-only support, test payload exclusion,
source and family binding, validation-only selection, endpoint calibration
denominators, JSON/hash checks, loss decrease, Python/NumPy equivalence, model
capacity refusal, and absence of invented endpoints or missing-action labels.

```text
PYTHONPYCACHEPREFIX=/private/tmp/intensity-distribution-pycache python3 -W error -m unittest tests.test_intensity_distribution tests.test_information_student tests.test_information_learning_curve -v
Ran 43 tests in 1.295s
OK
git diff --check
(no output, exit 0)
```

Scientific semantic change: only a new, opt-in conditional intensity law and
its explicit controls/evaluation are added. Historical prompts, Teacher
acquisition, action predictions, old intensity heads, market clearing,
financing and integer accounting are unchanged. Whether the new law preserves
Teacher behavior in a continuous market and improves human-task matching
requires separate managed evidence.
