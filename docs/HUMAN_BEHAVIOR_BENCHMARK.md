# Human behavior benchmark: tasks prepared, human evidence pending

The new `human-information-market-task/1.0.0` specification provides 24 neutral
numeric vignettes arranged in 12 pairs. It is a proposed exploratory task bank,
not collected human data and not a completed validation study. The pure library
is `nmsim/human_benchmark.py`; official exports/scoring must use the registered
managed entrypoint. The task instructions introduce no persona, trader label,
named behavioral theory or instruction to exhibit a bias.

`build_tasks(seed=20260909)` generates six contrast families at two scenario
dates: price-heavy versus financial-heavy visibility, price-heavy versus
news-heavy visibility, security position fraction, account wealth, purchase
reference return, and time since the last executed trade. Both conditions have
the same twelve-field budget and A8 account contract. Visibility contrasts
change the supplied field set. Other contrasts keep the observed market fields
constant and change the named account condition. The purchase-reference
contrast changes cost basis while placing both prior purchases at least twenty
days in the past, so the supplied twenty-day tape does not falsely locate that
purchase. Numeric account contrasts represent counterfactual endowments and
histories, not claimed replays of a real participant's trades.

The synthetic market/company scenario and field limitations, including the
daily-close proxy for unobserved intraday high-low ranges, are described in
[INFORMATION_MARKET.md](INFORMATION_MARKET.md). These tasks inherit those
limitations. The task seed fixes synthetic conditions; it does not create or
identify a human participant. The public `contrast`, `pair_id`, and `condition`
fields are research metadata and must not be appended as descriptive labels to
the model's numeric prompt or shown as behavioral suggestions to participants.

## Researcher collection protocol still required

The researcher must recruit participants, obtain appropriate consent, define
inclusion criteria and incentives, and freeze a counterbalanced task order.
Humans and model predictions need the same visible information, action meanings,
and explicitly documented incentive/horizon framing. Do not tell participants
the expected behavioral effect. Separate paired scenarios in randomized order
to reduce demand effects; record the assignment outside the model prompt.
Pilot comprehension, including intensity as a feasible-cash or held-share
fraction, before collecting the main sample. A hypothetical questionnaire must
not be presented as equivalent to incentivized repeated trading.

Allocate anonymous participant IDs and keep identity/consent mapping outside
the repository. Allowed human response fields are exactly:

```json
{"subject_id":"participant-001","task_id":"pair-01-a",
 "action":"buy","intensity":0.4,"source_kind":"human"}
```

Hold intensity must be zero. Duplicate participant/task answers fail validation
rather than increasing sample size. `source_kind="synthetic_fixture"` is
allowed for engineering rehearsal, but such rows are excluded from every
human-evidence metric and human N. Declaring `source_kind="human"` is a
researcher assertion: the scorer cannot establish consent, identity,
recruitment provenance or response authenticity from a string. Unit tests use
numerical fixtures to exercise this branch; their outputs are not research
artifacts and do not establish real human counts.

Model predictions for the identical task use:

```json
{"task_id":"pair-01-a","action_probs":[0.4,0.3,0.3],
 "intensities":[0.5,0.4],"source_kind":"model"}
```

Probabilities follow buy/hold/sell; intensities are conditional buy/sell means.
No raw reasoning or personal explanation belongs in either scoring input.
Model rows marked `synthetic_fixture` are also excluded. Teacher one-shot
answers do not supply a calibrated probability vector; obtaining one needs an
explicit protocol, and repeated model calls must not count as independent
human respondents. A Student's probabilities can be evaluated directly but
must retain the model artifact and training identity in the managed run.

## Scores and their interpretation

`score_responses(tasks, human_rows, prediction_rows)` reports:

- Per-task human action counts/distributions and Jensen–Shannon divergence in
  natural-log units; the range is 0 to log(2). It compares distributions, not
  eloquence or profitability.
- Mean categorical negative log likelihood and the summed three-class Brier
  score (range 0–2), evaluated against individual human actions. A zero model
  probability for an observed human action yields an explicitly infinite NLL
  status (`null` numeric value plus a zero-probability count), never a hidden
  epsilon-clipped score.
- Buy/sell conditional intensity mean absolute error, with the number of
  relevant human responses. No observed action produces a missing metric,
  rather than a zero error.
- Within-person paired contrasts, always condition b minus a. The direction
  coding is buy=+1, hold=0, sell=-1. Signed intensity multiplies that sign by
  the reported intensity. Expected model direction and expected signed size
  are compared with observed within-person differences. Only participants who
  answered both conditions enter the paired participant count.

Aggregate JS gives each scored task equal weight. Aggregate NLL/Brier weights
individual scored responses; repeated responses from the same participant are
not independent people. A later inferential study needs participant-level
uncertainty estimation, preregistered primary outcomes and a sample-size plan.
This first scorer is descriptive and supplies no confidence intervals or
universal pass/fail threshold. Setting the acceptance threshold after seeing
the test answers would invalidate that test as an untouched confirmation set.

Human participants, human responses, tasks answered, scored responses,
complete paired participants and excluded fixtures are reported separately.
Missing human responses produce `pending_human_responses`; missing overlapping
predictions produce `pending_matching_predictions`. Some overlap permits
`descriptive_comparison_available`, with missing task IDs still listed. Every
status keeps `human_likeness_validated=false`; a populated table is not by
itself proof of human validity.

Before claiming validation, compare with a training-only population-prior
baseline, freeze holdout conditions and participant criteria, assess prompt
and temporal stability, and examine joint/conditional responses as well as
means. The tasks are intentionally modest. They do not validate social
interaction, endogenous information seeking, institutional constraints,
multi-asset choice, or the entire market's distribution.

## Engineering verification

`python3 -m unittest tests.test_human_benchmark -v` verifies 24 neutral paired
tasks, stable JSON round trips, exact analytical metrics, explicit missing
evidence, participant rather than response counts, fixture exclusion,
duplicate rejection and an I/O-free diagnostic path. The module adds no
production dependency and changes no frozen prompt or historical schema.
