# Continuous information market — new exploratory protocol

`nmsim.information_market` implements `information-market/1.0.0`. It is a new
research mechanism, not a reinterpretation of a10 or the frozen 10k Teacher
experiment. Existing V1/V2 APIs, prompts, Student artifacts and historical runs
are unchanged. The library performs no I/O and creates no run context. Official
execution belongs to the registered managed entrypoint; calls from unit tests
are pure diagnostic computations, not provenance-complete research runs.

## Time, information and accounting

At daily close t every agent sees the same completed price/volume tape through
t, and only company reports/public events released by t. Its order clears in
one batch at t+1. Every policy call is completed before settlement; no agent can
observe another agent's current-round order or updated account. The full
scenario is returned for audit, but only the current numeric fields reach the
policy. Extending the requested horizon preserves the earlier scenario and
rollout prefix exactly for a pure deterministic policy.

The warm-up is 21 explicitly synthetic closes and volumes. These are initialized
observations, not accounted historical trades. The last warm-up close is 10,000
cents; all initial holdings have this disclosed cost basis. Initial account
cash and inventory vary independently of information allocation. Initial cash
is 1,000,000–4,000,000 integer cents and inventory is 50–250 integer shares.
Their sum defines the fixed share supply and the company's shares outstanding.

One structured public event arrives every ten days. Its release date, direction,
affected revenue fraction, stated duration and source fields are committed by
a seeded exogenous scenario. It changes company revenue over the following
ten decision dates. An event is not a message written by an agent. There are no
private explanations, social feed, analyst identity instructions or inferred
off-screen facts. The neutral-event ablation zeros surprise and exposed revenue
fraction while retaining release timing and the other random draws.

The synthetic company starts with 10,000 cents of assets per share, 40% of
assets as liabilities and the remainder as common equity. Baseline revenue is
40 cents per share per day, with a fixed 6% sinusoidal component (day/13) and
the event multiplier `1 + surprise * affected_fraction`. Net income is 8% of
revenue, operating income 12%, and operating cash flow 9%, rounded to cents.
Daily net income is retained in company assets; liabilities remain fixed and
equity is always assets minus liabilities. There are no investor dividends,
issuance, redemptions or external changes to investor cash.

Reports arrive every ten days and remain frozen between release dates. TTM
revenue is the sum of the last 252 synthetic daily revenues, and the preceding
252 days define the comparison year. Both years have an explicitly constant
revenue warm-up before t=0. Current assets are 30% of assets and current
liabilities 25% of liabilities. Financial features derive from those same
statements. Earnings yield and book-to-market are recalculated using the
currently observed price and fixed share count, so a changed market price
cannot leave a stale valuation ratio. These simple statements are an
exploratory scenario, not a calibrated corporate model or fundamental asset
valuation. They do not justify labelling a price path a bubble.

## Information allocation and policy interface

All agents use the same callable:

```python
prediction = policy(visible_fields, account_state)
# prediction = {"action_probs": [p_buy, p_hold, p_sell],
#               "intensities": [buy_cash_fraction, sell_share_fraction]}
```

`visible_fields` contains exactly twelve fields selected by the existing
`PROFILE_FIELDS`: P8/F2/N2, P2/F8/N2, P2/F2/N8, or P4/F4/N4. `account_state`
contains the existing A8 only. Profile labels and account identifiers are audit
metadata and are never passed as policy inputs. Input copies prevent a policy
from modifying shared observations. Only the validated numerical prediction is
retained; additional returned fields, including private rationale, cannot enter
the ledger. A callable must be pure for repeatable replay: the simulator cannot
make a stateful or externally random arbitrary Python callable deterministic.

Persistent account metadata stores weighted average purchase cost, the last
executed trade day, and most recent executed sale price. Rejected or unfilled
intentions do not alter that memory. Features describe actual fills and current
holdings; the last sale remains observable after repurchasing. Cash and shares
settle with the unchanged integer `v2_market` constraint/auction/settlement core.
No leverage or credit is offered. Each account submits at most one order and
buys are feasible even at their limit price. No-cross/no-orders rounds have
zero volume and preserve the previous price. Scarce purchasing power therefore
does not by construction force a crash.

## New approximations and explicit domain projection

P6 and A8 retain their existing formulas. The daily call has no intraday tape:
`intraday_range_5d_mean` is supplied as the mean absolute close-to-close change
over five completed days. This is a declared lower-information daily-close
proxy, not an observed high-low range. Every run reports this structural
out-of-distribution substitution; forecasts using a Student trained on the
original range meaning require further validation.

With a constant share supply, turnover change equals current volume divided by
the preceding five-session volume average minus one. Zero/zero maps to zero.
Positive volume over a zero reference is mathematically undefined; its explicit
representation is 5 and the ledger flags `undefined_turnover_ratio`, with a
separate round count. No infinite JSON number is emitted. For this special
case the raw observation records a representation, not a finite mathematical
ratio. Current/prior volumes remain reconstructible from the ledger.

Other closed-domain exceedances retain numerical `market_raw` and
`account_state_raw`, projected policy inputs, and individual raw/effective
change records. For example, zero current volume over a positive reference
has raw turnover change -1 and effective -0.95. Counts distinguish each common
market field per round from each account field per decision; they are not
unique states or people. Intraday proxy counts are separate from numerical
projection counts. Clipping is a disclosed adapter for using the frozen input
domain, not evidence that a rollout remained in the Teacher training support.

## Quote controls

The default `independent` rule samples a reservation offset of at most ±200
basis points around the current close, independently of chosen intensity.
Intensity controls the requested cash/share fraction only. The
`legacy_intensity_linked` comparison exactly reuses V2's ±300 bp intensity
urgency plus ±200 bp reservation variation, with its ±500 bp bound. This is
the old quote mapping, not a claim to reproduce the entire old experiment.
Both rules use matched action/quote draws at fixed seed, day and account. A
price difference after changing the mapping is a quote-mechanism effect, not
automatically a difference in Teacher behavior.

`random_policy` supplies uniform buy/hold/sell probabilities and 0.5 conditional
intensity; the simulator samples its directions. `prior_policy(probabilities,
intensities)` supplies a fixed observation-independent null or an empirical
training-only prior. Hold-only and one-sided controls validate zero-trade
behavior. Profile-mixture changes provide information-selection contrasts;
`news_mode="neutral"` tests the event/business transmission mechanism. More
realistic attention, social influence, borrowing, learning and human
heterogeneity require separately specified future mechanisms and controls.

## Parameters and evidence

| Parameter | Effect path | Verification |
| --- | --- | --- |
| `seed` | Independent seeded initialization, scenario, allocation, actions, reservation draws | Exact replay and changed-seed test |
| `agents` | Account count, finite cash/stock supply, company scale | Conservation and 200/1,000-agent tests |
| `rounds` | Number of daily observations and settlement batches | Prefix/no-lookahead test |
| `profile_counts` | Exact count of each observed field set | Visible-key and causal action tests |
| `profile_weights` | Largest-remainder allocation in fixed profile order | Count and 80/20 allocation tests |
| `quote_rule` | Independent versus intensity-linked limit price | Intensity invariance and legacy equality test |
| `news_mode` | Eventful versus zero event exposure, including revenue impact | Paired release timing and changed revenue test |
| `policy` | One shared numeric observation-to-prediction function | Null controls and field-only boundary test |
| `on_round` | Progress callback on a deep copy of each completed ledger row | Callback mutation isolation test |

All fixed scenario coefficients and schemas appear in `descriptor()` for
inclusion in the managed caller's named effective scientific configuration.
`world_hash` is a byte SHA-256 of canonical JSON
`{"schema": WORLD_SCHEMA_VERSION, "schedule": world_schedule}`. It is not a
model hash or one of the central configuration hashes. The latter must be
provided and named by the managed entrypoint. Identical scenario hashes are
expected only for arms with identical seed, horizon, total share supply and
news mode; market prices and policy decisions do not enter the scenario hash.

The result contains a replay ledger with common raw/effective fields, each
agent's A8 and public numeric predictions, submitted/constrained orders, fills,
and post-settlement accounts. A compact `state_probes` selection includes one
account per present profile per day. Its rows are descriptive probes, not
independent replications. Full input reconstruction uses the ledger and profile
field registry. Keeping full decisions can consume substantial memory for long
1,000-agent runs; the default is 40 agents and 60 days.

Honest units are one simulated market, `rounds` batches and `agents * rounds`
policy decisions, with zero Teacher requests and zero human participants.
Multiple market seeds, holdout Student fidelity and human-task evaluation are
separate requirements. Price/volume patterns alone do not validate human
likeness or identify a unique market composition.

Validation command: `python3 -m unittest tests.test_information_market -v`.
Tests exercise integer conservation, feasible limits, no-cross behavior,
observation privacy, chronology, persistent own history, ratio consistency,
controls, deterministic replay and a no-files/no-network diagnostic path.

## Explicit available-only successor

`--observation-policy available_only` adds schema `information-market/1.1.0`.
It omits unobserved intraday range and undefined turnover instead of supplying
a proxy/sentinel as an observed value. Each new-mode decision stores the exact
visible field set. Existing defaults and old outputs retain 1.0 behavior. The
new visibility pattern is itself out of the historical training design and
requires fidelity checks. See [ROLLOUT_FIDELITY.md](ROLLOUT_FIDELITY.md).
