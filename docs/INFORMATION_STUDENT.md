# Information-observation Student

This is a new exploratory Student pipeline for the existing four-view Teacher
acquisition. It does not alter any historical prompt, acquisition, V2 Student,
market, CLI, run, or serialized V2 model schema. It is not evidence that the
Teacher resembles humans. K=1 for each state/view supplies a single action label;
it does not estimate a human population or within-state endpoint distribution.

## Input and privacy

`nmsim.information_student` is a pure library. The managed caller verifies the
source manifest and file-byte SHA, then passes the public JSONL rows as objects.
No private artifact is required, opened, or inferred. The accepted source schema
is `information_weight_teacher_scale_sample/1.0`. Public view and response
schemas, view hashes, the twelve visible fields, account fields, numerical
domains, masks, decisions, and logical/physical attempt accounting are validated.
Malformed rows cause a failure, not silent filtering. Known private fields and
private fields in the decision are rejected; prompts and unrelated metadata
never enter the normalized dataset or fitted models.

Each inference input consists of exactly 56 numbers in an explicit order:

1. P8 + F8 + N8 numeric values (24); an unavailable field has a zero placeholder.
2. A corresponding actually-visible mask for each field (24).
3. The unchanged ACCOUNT8 fields (8), including their existing history masks.

An observed zero therefore differs from missing information. A latent ID,
profile label, sample ID, prompt, or hidden field is never a numeric model input.
The presence masks necessarily encode the information allocation itself. At
inference, finite values outside the historical design range are accepted
without clipping and must be accompanied by the separate support diagnostics.

## Split and honest-N

Freeze a 70/15/15 group split before excluding acquisition failures. The
assignment algorithm reuses `v2-family-group-split-v1`: deterministic SHA rank
and largest-remainder counts. Group by an explicit `family_id` if the source
supplies one; otherwise group by `latent_state_id`. All four requested views,
including failed requests, must be present for each latent group. Shared fields,
accounts, family IDs and latent identities must agree. Related states may share
a family; identities are used only for grouping and provenance.

Only valid public decisions enter optimization. Failed requests remain in the
denominator and partition counts. Physical attempts, logical requests, valid
answers, failures by code, latent states, complete paired groups, and zero human
participants are reported separately. Changing validity or test labels cannot
reassign an existing complete roster; dropping an entire group changes the
roster and its hash and constitutes a different dataset.

The normalized-data SHA (`information-student-normalized-public-records/1.0`)
covers the sorted whitelist projection, including failures and response/view
hashes. It is not the source JSONL byte SHA or a run manifest SHA. Split and
encoder identities have separately named schemas and hashes. Serialized model
SHA values cover the exact V2 JSON payloads. The managed caller is responsible
for source-byte identity and the four configuration-hash types; this pure
library must not guess those identities or claim a reusable experiment child.

## Fitting and selection

`train_models(records, seed=20260909, epochs=120, hidden_dim=16, backend='auto')`
fits the following fixed candidates on the training partition only:

| Candidate | Stored fitted coefficients | Function |
| --- | ---: | --- |
| Action prior | 5 | Feature-blind null: three probabilities and two conditional intensities |
| Linear softmax + sigmoid | 285 | 56-to-3 action logits and 56-to-2 intensity logits |
| Tanh MLP | 997 at hidden width 16 | 56-to-16 tanh, then action and intensity heads |

Counts exclude the train-only standardizer's means/scales. The prior's three
probabilities sum to one, so its five stored coefficients are not five
independent degrees of freedom. The MLP formula is `62 * hidden_dim + 5`.

Loss, initialization and optimizer reuse the V2 definitions: mean action cross
entropy plus action-conditional intensity MSE (weight 1), L2 coefficient 1e-4
on weight matrices only, full-batch Adam with learning rate 0.02, beta1 0.9,
beta2 0.999 and epsilon 1e-8. The epoch count is fixed before the evaluation.
There is no test-driven early stopping, model capacity search or retraining.
The winner is lowest validation action CE, with deterministic ties ordered
prior, linear, MLP. Intensity quality is reported independently; CE selection
does not claim optimal intensity performance. Only after freezing the winner
are all three test comparisons calculated once by that call. The standardizer
and support reference fit only the training rows.

Reported metrics include action CE, Brier score, accuracy, per-action and macro
F1, a confusion matrix, conditional intensity MAE, and per-view comparisons.
The fixed test becomes development evidence after disclosure; future model
selection requires a new frozen evaluation source. The old a10 test is never
used for this pipeline's candidate selection.

## Backend and inference

NumPy is optional; `auto` uses it if installed, otherwise fitting uses the
existing stdlib implementation. The accelerated full-batch loss and gradients
are tested against the existing implementation. Backend and NumPy version are
recorded. Matrix products use explicit NumPy `einsum` contractions with
optimization disabled, avoiding external BLAS thread/reduction variability.
Floating-point details can differ across backends/platforms; no
cross-platform bitwise determinism is claimed. No dependency is added to the
production installation.

All exported models retain the existing `v2-action-prior-v1`,
`v2-linear-softmax-v1` or `v2-tanh-mlp-v1` serialization. `make_predictor(model)`
loads once and returns a stdlib-only `(visible_fields, account_state)` callable
for repeated market decisions. `predict_model` is the one-shot equivalent.
The return is `{action_probs: [buy, hold, sell], intensities: [buy, sell]}`.
The market defines and records how these scores produce an order; class scores
must not be described as calibrated human heterogeneity.

The returned `ood_reference` stores per-feature train min/max and train-only
standardization. `make_support_checker(reference)` returns per-observation
range flags and maximum absolute z-score. These checks detect marginal
extrapolation, not joint support, behavioral validity, distribution shift in
general, or closed-loop fidelity. In particular, omitted channels have masked
zero placeholders; support statistics describe that exact encoding.

## API and checks

`prepare_records(records, seed=...)` returns `examples_by_partition`, `split`,
`accounting`, and `normalized_records_hash`. `train_models(...)` additionally
returns all three serialized `models`, `selected_model`, a frozen `selection`
record, `evaluation`, training history/settings, encoder and model provenance,
and train-reference OOD diagnostics. These functions create no files or runs.

The self-contained public synthetic fixtures in
`tests/test_information_student.py` exercise hidden-field invariance, masks,
privacy rejection, source/hash/domain validation, group isolation, failure
accounting, explicit family grouping, no test-label selection influence,
parameter counts, JSON round trips, finite inference, deterministic fitting,
and NumPy versus stdlib loss/graph equivalence. Run:

```sh
python3 -m unittest tests.test_information_student -v
```

Implementation verification (2026-09-09, `feat/agent-market-loop`):

```text
PYTHONPYCACHEPREFIX=/private/tmp/agent-market-student-pycache python3 -W error -m unittest tests.test_information_student -v
Ran 16 tests in 0.221s
OK
git diff --check
(no output, exit 0)
```

An in-memory diagnostic ingestion of the existing public 10k collaboration
export also passed: 10,000 logical requests, 10,349 physical attempts, 9,918
valid answers, 82 failures, and 2,478 complete valid paired groups. Train,
validation and test contain 6,954/1,476/1,488 valid rows from 1,750/375/375 latent
groups. This check did not train or select models or evaluate the test labels.
The normalized projection semantic SHA is
`2273bf7b690c53ba461c629005ab4f0dade3146db674b4940afd7eb6669c4349`;
the split semantic SHA is
`04baef896f2b9362981ed99aecfbc1535f9f793167deb8f27901bf385db6f2ff`.
These are not acquisition manifest or source-byte hashes.

Scientific semantic change: only the new 56-input observation-masked Student,
its group split and exploratory evaluation are added. Historical mechanisms
remain untouched. Human matching, transaction realism, joint support, and
Student closed-loop fidelity still require their own evidence.
