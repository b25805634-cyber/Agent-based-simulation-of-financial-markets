# Information-weight Teacher pilot

## Purpose and scientific boundary

This additive pilot asks one narrow question: when the same complete latent
market/account state is rendered with different allocations of observable
numeric information, does the frozen Teacher return different portfolio
adjustments? The Provider is never told a trader identity. Internal profile
IDs organize paired observations, but no profile, type, Persona, investor
category, domain weight, or behavioral instruction appears in the request.

The pilot contains **50 synthetic latent states and four paired views per
state, for exactly 200 logical Teacher requests**. It is Teacher-only: it
trains no Student, runs no market, clears no order, and creates no price path.
It also has no human ground truth. A real-endpoint result is therefore an
exploratory endpoint-policy diagnostic, not evidence that the endpoint
replicates people or that any market mechanism has been identified.

"Information weight" is operationalized here as a fixed allocation of
visible numeric fields under a common 12-field information budget. It is not
a numeric attention coefficient, a hidden model weight, or an instruction to
pay more attention. Every view additionally receives the same eight account
fields and the same action contract.

## Frozen state variables

All ranges are closed. Out-of-range values fail validation rather than being
silently clipped. The first six price/volume variables and all eight account
variables reuse the frozen V2 definitions; the remaining fields are additive
to this protocol.

### Price/volume pool P8

| Field | Closed range |
|---|---:|
| `return_1d` | `[-0.25, 0.25]` |
| `return_5d` | `[-0.75, 1.25]` |
| `return_20d` | `[-0.95, 4.00]` |
| `realized_vol_20d` | `[0.00, 1.50]` |
| `drawdown_20d` | `[-1.00, 0.00]` |
| `volume_z` | `[-6.00, 6.00]` |
| `intraday_range_5d_mean` | `[0.00, 0.50]` |
| `turnover_change_5d` | `[-0.95, 5.00]` |

### Company-fundamental pool F8

| Field | Closed range |
|---|---:|
| `earnings_yield_ttm` | `[-1.00, 1.00]` |
| `revenue_growth_yoy` | `[-1.00, 5.00]` |
| `debt_to_assets` | `[0.00, 2.00]` |
| `operating_margin_ttm` | `[-2.00, 1.00]` |
| `return_on_assets_ttm` | `[-1.00, 1.00]` |
| `operating_cashflow_to_assets` | `[-1.00, 1.00]` |
| `book_to_market` | `[-2.00, 5.00]` |
| `current_ratio` | `[0.00, 10.00]` |

These are company-level accounting/valuation summaries. The pilot does not
add a fundamental-value process, dividends, or an equilibrium price.

### Public-news/event pool N8

| Field | Closed range |
|---|---:|
| `signed_event_surprise` | `[-1.00, 1.00]` |
| `affected_revenue_fraction` | `[0.00, 1.00]` |
| `age_20d_scaled` | `[0.00, 1.00]` |
| `official_source_mask` | `{0, 1}` |
| `confirmation_count_scaled` | `[0.00, 1.00]` |
| `scheduled_event_mask` | `{0, 1}` |
| `duration_20d_scaled` | `[0.00, 1.00]` |
| `source_disagreement` | `[0.00, 1.00]` |

The news variables are numeric public-event summaries, not free-text stories.
The two masks are binary; all other event variables are continuous on their
declared closed domains.

### Common account pool A8

| Field | Closed range |
|---|---:|
| `position_fraction` | `[0.00, 1.00]` |
| `unrealized_return` | `[-0.95, 5.00]` |
| `unrealized_return_mask` | `{0, 1}` |
| `days_since_trade_scaled` | `[0.00, 1.00]` |
| `days_since_trade_scaled_mask` | `{0, 1}` |
| `post_sale_return` | `[-0.95, 5.00]` |
| `post_sale_return_mask` | `{0, 1}` |
| `log10_wealth` | `[3.00, 10.00]` |

These eight fields are identical across all four views of one latent state;
they are not charged against the 12-field information budget. Existing V2
mask/value and cross-field constraints remain in force.

## Four paired information profiles

Profile IDs exist only in the design, provenance, and analysis outputs. The
Teacher receives the selected fields and their neutral definitions, never the
ID or an explanation of the allocation.

| Internal profile ID | Price/volume | Fundamentals | News | Exact visible fields |
|---|---:|---:|---:|---|
| `price_volume_8_2_2` | 8 | 2 | 2 | P8 + `earnings_yield_ttm`, `revenue_growth_yoy` + `signed_event_surprise`, `affected_revenue_fraction` |
| `fundamental_2_8_2` | 2 | 8 | 2 | `return_1d`, `return_5d` + F8 + `signed_event_surprise`, `affected_revenue_fraction` |
| `news_2_2_8` | 2 | 2 | 8 | `return_1d`, `return_5d` + `earnings_yield_ttm`, `revenue_growth_yoy` + N8 |
| `balanced_4_4_4` | 4 | 4 | 4 | first four P8 + first four F8 + first four N8 |

`balanced_4_4_4` is a balanced-information comparison arm, **not a true null**:
it still exposes 12 market-information variables plus A8. This version has no
zero-information, shuffled-information, or constant-information null arm.

For each latent state, all four views share the same complete P8/F8/N8/A8
values before visibility selection. The release order is a cyclic left
rotation by latent-state index modulo four. Each profile therefore appears
exactly 50 times; because 50 is not divisible by four, each within-group slot
contains 12 or 13 instances of each profile rather than exactly equal counts.

## Synthetic design and K=1 limitation

The 50 complete latent states use seed `20260824`. The existing V2 state
generator supplies the coherent P6/A8 core. Each added continuous field uses
its own deterministic Latin hypercube over its declared marginal range; the
two binary news masks threshold their corresponding draws at 0.5.

This is broad synthetic coverage, not a sample from a fitted joint empirical
distribution. Cross-domain combinations can be economically unusual because
fundamental and event fields are not generated by a coherent company/event
time series. Results must not be interpreted as population-weighted effects or
as estimates for real markets without a later constrained design and human or
empirical anchors.

There is one Provider request per `(latent state, profile)` cell (`K=1`). The
design therefore cannot estimate within-cell endpoint stochasticity, and 200
repeated model outputs must not be described as 200 independent people. For a
paired profile contrast, the independent design unit is at most 50 latent
states. Endpoint noise must be taken from a separately justified measurement
or measured in a later `K>1` protocol.

## Request contract

The exact system prompt is version controlled in
`nmsim/information_weight.py`. It asks for one JSON object with exactly:

```json
{"action":"buy|hold|sell","intensity":0.0,"reasoning":"private diagnostic"}
```

`intensity` is the fraction of feasible cash for `buy`, the fraction of held
shares for `sell`, and exactly zero for `hold`. The existing strict V2 parser
and feasibility checks are reused. The `reasoning` field is private and is
never projected into the public sample artifact.

The real request tuple is frozen as follows:

| Setting | Value |
|---|---|
| requested model | `MiniMax-M2.7` |
| required reported alias | `HiggsAI` |
| required finish reason | `stop` |
| temperature | `1.0` |
| top-p | `0.95` |
| top-k | `40` |
| maximum output tokens | `190000` |
| response format | `{"type":"json_object"}` |
| workers/order | `1`, strict sequential |
| SDK retries | `0` |
| application attempts | at most `5` per logical request |
| retry delays | `10, 30, 60, 120` seconds |
| inactivity/hard deadline | `7200 / 7200` seconds |

The requested model name and Provider-reported alias are recorded separately;
an alias is not proof of the underlying deployed weights.

## Honest-N and artifacts

The following units are deliberately non-interchangeable:

- `logical_requests`: up to 200 planned cells for which Provider acquisition
  was attempted, whether or not a response was ultimately resolved;
- `physical_provider_attempts`: transport/application attempts, from 200 up to
  1000 if every logical request consumes all five attempts;
- `raw_responses`: non-null raw response envelopes;
- `parsed_decisions`: responses that pass the strict parser;
- `complete_paired_latent_states`: at most 50 states for which all four views
  have valid parsed decisions.

A failed or retried physical attempt does not become another logical sample.
None of these units is a human-sample count or a market-run count.

Full execution writes immutable managed artifacts:

| Artifact | Mode | Content boundary |
|---|---:|---|
| `latent_state_design.json` | `0644` | complete synthetic design and design hash |
| `sample_plan.json` | `0644` | exact 200-request order and prompt identities |
| `information_weight_samples.jsonl` | `0644` | visible inputs, exact prompts, public decisions, public failure codes |
| `private_information_weight_records.jsonl` | `0600` | raw responses, private rationale, raw SDK/Provider identity fields and detailed errors |
| `information_weight_teacher_summary.json` | `0644` | completion by profile, honest-N, claim boundary and named hashes |

Dry-run writes only `dry_run_summary.json` in a managed attempt and constructs
no Provider. Files use exclusive creation; an existing run ID is never
overwritten.

## Named identities

Schema `information_weight_teacher_identity/0.1` records four different hashes:

- `scientific_config_hash`: the frozen field contract, 50 latent states, four
  views, exact state-design hash and exact sample-plan hash;
- `model_request_config_hash`: requested model, sampling tuple, response format
  and system-prompt hash;
- `execution_config_hash`: Provider/mode/run ID, strict ordering, retry/deadline
  policy, required reported alias/finish reason, and credential-free endpoint
  route identity;
- `full_effective_config_hash`: an envelope over the preceding three hashes.

The artifacts also name `state_design_hash` and `sample_plan_hash`. Raw API
keys, endpoint userinfo, sensitive query values, and private rationale must not
enter any public identity or artifact.

These named identities are scoped projections, not strictly orthogonal
statistical factors. In particular, the exact system prompt and response format
are intentionally bound both as part of the scientific observation contract
and as part of the model-request envelope. A prompt mutation must therefore
change both the scientific and model-request hashes; this deliberate
cross-binding prevents either identity from understating the effective
Teacher intervention.

## Exact commands and frozen run IDs

Plan-only validation, no Provider or network:

```bash
python3 -m experiments.information_weight_teacher \
  --provider openai --model MiniMax-M2.7 --dry-run \
  --run-id information-weight-pilot-v1-dry-20260824-a2 \
  --out results_information_weight_pilot
```

Offline 200-row engineering control, not endpoint behavior:

```bash
python3 -m experiments.information_weight_teacher \
  --provider fake_test_teacher \
  --run-id information-weight-pilot-v1-fake-20260824-a1 \
  --out results_information_weight_pilot
```

Real endpoint acquisition is allowed only with explicit credentials, `--live`,
and the exact 200-request confirmation:

```bash
OPENAI_BASE_URL=http://HOST/v1 OPENAI_API_KEY=... \
python3 -m experiments.information_weight_teacher \
  --provider openai --model MiniMax-M2.7 --live \
  --confirm-request-count 200 \
  --run-id information-weight-pilot-live-20260824-a1 \
  --out results_information_weight_pilot
```

Do not reuse any of these run IDs. A successor attempt requires a new frozen
run ID and an explicit protocol/provenance decision; historical outputs are
never overwritten or silently resumed.

## What this pilot can and cannot establish

It can establish whether the managed 200-request plan was executed, whether
the endpoint's public decisions differ across paired visibility profiles, and
whether the information-view machinery preserves identity, privacy, and
honest-N accounting.

It cannot establish human realism, causal attention weights, a behavioral
trader taxonomy, Student fidelity, closed-loop stability, market stylized
facts, price discovery, bubbles, or the recoverability of trader composition
from real price/volume paths. Those require later human anchors, `K>1`, true
null/sham controls, constrained state generation, Student holdouts and
rollouts, and separately preregistered market experiments.

Scientific semantics change: **additive only**. No existing Persona, prompt,
Student, market-clearing, financing, CLI default, or historical result schema
is modified.
