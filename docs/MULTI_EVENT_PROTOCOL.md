# Multi-event reference protocol (data v1)

## Scope and scientific role

Wave1-T1 adds three outcome-shape references so validation is no longer framed
around one Meta crash. These files are descriptive calibration inputs for a
research simulator, not forecasts, causal estimates, or trading signals. The
categories are deliberately qualitative: a negative jump that does not recover
inside its captured horizon, a crash followed by a full nominal-price recovery,
and a positive earnings jump.

This task does **not** inject a timeline into the simulation. Existing `Config`,
`--reference`, `news_round`, `news_text`, CLI defaults, prompts, agent behavior,
and market mechanics are unchanged. The new timeline loader is an explicit
opt-in data boundary for a later experiment driver, which must add the timeline
path/content hash to its scientific configuration before claiming a formal run.

## Frozen event set

| dataset ID | instrument/window | t=0 | observed outcome |
|---|---|---|---|
| `meta_2022_02_crash_v1` | FB/META, 2022-02-01 through 2022-02-18, 14 sessions | 2022-02-02 close, $323.00 | trough/final $206.16, -36.17%; no recovery inside this short horizon |
| `spy_2020_03_covid_v_recovery_v1` | SPY, 2020-02-19 through 2020-08-18, 127 sessions | 2020-02-19 close, $338.34 | trough $222.95 at t=23, -34.10%; $338.64 at t=126, a 0.09% nominal recovery above t=0 |
| `meta_2023_02_efficiency_jump_v1` | META, 2023-02-01 through 2023-02-21, 14 sessions | 2023-02-01 close, $153.12 | next close $188.77, +23.28%; captured peak $191.62 and final $172.08 |

The SPY series is long because the category requires observing recovery, not
merely the crash. A default 24-round simulation has 25 prices including its
initial price. The existing trajectory comparator therefore uses only the
common t=0..24 prefix, ending at the 2020-03-24 SPY close. That prefix contains
the trough and one rebound session, **not** the full V recovery at t=126. A
result may claim a full-recovery match only when an explicitly preregistered
horizon or resampling rule actually includes the recovery segment.

The SPY t=0 marker is the regular-session close on the date of the first selected
WHO warning and the pre-reaction price peak. It is not a claim that the WHO
briefing occurred after the U.S. close. The two Meta t=0 rows are pre-reaction
regular-session closes before scheduled after-hours earnings events.

## Price observations and provenance

Each new CSV uses `timestamp,price[,news]`. The structured loader also accepts
the tracked legacy `date,price[,news]` header as an exact timestamp-column
alias, so existing `--reference` inputs remain usable. `price` is the
nominal, unadjusted USD historical `close` returned by Nasdaq; dividends are not
reinvested. Every regular trading-session observation returned inside each
inclusive window is retained. There is no interpolation, forward fill, rounding
beyond the source's displayed precision, or synthetic row.

The exact Nasdaq API queries, retrieval date (2026-07-22), inclusive filters,
price convention, ticker note, and t=0 rule are recorded in
`nmsim/reference_data/v1/catalog.json`. Retrieval uses `fromdate` and
`limit=5000` without `todate`, then filters the larger response locally to the
catalog's inclusive window. Adding `todate` returned zero old rows during
verification and is intentionally not part of the reproducible request. The
catalog retains the raw response byte length/SHA-256 and total record count;
`v1/source_snapshots/` retains every returned OHLCV object in each filtered
window. The human-facing source pages are:

- [META historical prices](https://www.nasdaq.com/market-activity/stocks/meta/historical)
- [SPY historical prices](https://www.nasdaq.com/market-activity/etf/spy/historical-nocp)

`nmsim.reference_data.source_verification.fetch_nasdaq_window()` implements the
recorded GET/filter operation without writing or overwriting artifacts;
`verify_snapshot_matches_csv()` performs the offline date/close check used by
the tests. Network retrieval is a maintainer action and never runs in tests.

`nmsim/reference_data/v1/SHA256SUMS` records SHA-256 over every committed v1
CSV, JSONL, and catalog artifact. Formal managed runs already hash the selected
`reference_path` bytes. A later timeline-consuming driver must also bind the
exact JSONL bytes into a scientific hash or an equally strict scenario-content
identity; a path label alone is insufficient.

The historical `nmsim/meta_feb2022_reference.csv` is untouched. Its rounded
2022-02-07 and 2022-02-10 values remain historical inputs. New v1 data uses the
Nasdaq values retrieved under the convention above; it does not rewrite an old
run or silently substitute for the legacy file.

## Timeline JSONL schema

Every nonblank line is one UTF-8 JSON object with these required fields:

| field | rule |
|---|---|
| `schema_version` | exactly `news_timeline_v1` |
| `event_id` | nonblank and unique within the file |
| `timestamp` | ISO date, or ISO datetime carrying `Z`/an explicit UTC offset |
| `public_text` | paraphrased public information; never private agent rationale |
| `source_title` | nonblank human-readable citation title |
| `source_url` | absolute HTTPS URL |
| `source_published_date` | date-only ISO calendar date reported by the source; datetimes are rejected |

The timeline is an editorial, source-traceable subset, not an exhaustive news
archive. It separates public information arrival from the price observations;
the latter remain in CSV.

### Meta February 2022 sources

| timeline item | source |
|---|---|
| Q4 results and scheduled earnings event | [Meta Investor Relations, 2022-02-02](https://investor.atmeta.com/investor-events/event-details/2022/Meta-Q4-2021-Earnings/default.aspx) |
| accepted annual report | [SEC EDGAR accession 0001326801-22-000018](https://www.sec.gov/Archives/edgar/data/1326801/000132680122000018/0001326801-22-000018-index.htm) |
| JPMorgan downgrade and price-target cut | [Bloomberg, 2022-02-03](https://www.bloomberg.com/news/articles/2022-02-03/jpmorgan-s-meta-analyst-cuts-his-rating-for-first-time-since-ipo) |
| mature-growth narrative after the fall | [Axios, 2022-02-04](https://www.axios.com/2022/02/04/facebook-meta-price-drop-shakes-confidence) |

### SPY COVID crash and recovery sources

| timeline item | source |
|---|---|
| warning of potential social/economic upheaval | [WHO mission briefing, 2020-02-19](https://www.who.int/news-room/speeches/item/who-director-general-s-opening-remarks-at-the-mission-briefing-on-covid-19) |
| initial Iran cases and deaths | [WHO media briefing, 2020-02-20](https://www.who.int/news-room/speeches/item/who-director-general-s-opening-remarks-at-the-media-briefing-on-covid-19-on-20-february-2020) |
| possible first U.S. community spread | [CDC media statement, 2020-02-26](https://archive.cdc.gov/www_cdc_gov/media/releases/2020/s0226-Covid-19-spread.html) |
| pandemic assessment | [WHO media briefing, 2020-03-11](https://www.who.int/news-room/speeches/item/who-director-general-s-opening-remarks-at-the-media-briefing-on-covid-19---11-march-2020) |
| emergency rate cut and asset purchases | [Federal Reserve, 2020-03-15](https://www.federalreserve.gov/newsevents/pressreleases/monetary20200315a.htm) |
| expanded market and credit support | [Federal Reserve, 2020-03-23](https://www.federalreserve.gov/newsevents/pressreleases/monetary20200323b.htm) |
| CARES Act becomes law | [Congress.gov H.R. 748 actions, 2020-03-27](https://www.congress.gov/bill/116th-congress/house-bill/748/all-info) |

### Meta February 2023 sources

| timeline item | source |
|---|---|
| accepted results filing, expense outlook, and repurchase authorization | [SEC EDGAR accession 0001326801-23-000008](https://www.sec.gov/Archives/edgar/data/1326801/000132680123000008/0001326801-23-000008-index.html) |
| scheduled call and Year of Efficiency framing | [Meta Investor Relations, 2023-02-01](https://investor.atmeta.com/investor-events/event-details/2023/Q4-2022-Earnings/default.aspx) |
| next-session +23.3% market reaction | [Associated Press, 2023-02-02](https://apnews.com/article/4be2e8e2e8b7334ae41abb0b84eba585) |

## Loader and alignment contract

The structured interface is:

```python
load_reference_episode(
    csv_path,
    news_timeline_path=exact_jsonl_path,
    include_news_timeline=True,
    exchange_timezone="America/New_York",
)
```

CSV-only loading is the default. Supplying a timeline path without the boolean,
or enabling the boolean without an exact path, is an error. The loader never
discovers a sibling JSONL file. `nmsim.validation.load_reference(path)` and the
existing `--reference CSV` route retain their original return shape and
timeline-free behavior.

t=0 selection is deterministic:

1. first CSV row with nonblank inline `news`;
2. otherwise, when and only when timeline loading is explicitly enabled, the
   last reference session on or before the first timeline event's exchange-local
   date;
3. otherwise the established fallback: the row before the largest one-step
   price drop.

Each reference point receives integer `t = row_index - shock_idx`. A timeline
event receives two deliberately distinct relative indices:

- `price_anchor_t` is the last observed close on or before its exchange-local
  publication date. It is descriptive and must never be used as delivery time.
- `delivery_t` is the first session at which the public information may be
  delivered without looking backwards. An offset-aware event before 16:00 New
  York time uses the first observed session on or after that date. An event at
  or after 16:00 uses the first strictly later session. A date-only item has
  unknown publication time and conservatively uses the first strictly later
  session. Weekends and holidays therefore roll forward automatically.

Both coordinates use `row_index - shock_idx`; consequently the first session
after the t=0 row is `delivery_t=1`.

For example, the Sunday 2020-03-15 Federal Reserve announcement anchors to the
Friday close but has Monday 2020-03-16 as `delivery_t`; both after-close Meta
earnings events deliver on their next observed sessions. Events with no price
anchor or no later safe delivery session inside the CSV range are rejected.
The fixed 16:00 boundary is not a general early-close exchange calendar. A
future simulator driver must still preregister round mapping and visibility and
must consume `delivery_t`, never `price_anchor_t`, for information arrival.

Date-only strings are exchange-calendar labels and undergo no timezone
conversion. Offset-aware datetimes denote instants and are converted to
`America/New_York` before the session date is selected. Naive datetimes are
rejected. CSV session dates must be unique and strictly increasing; timeline
events must be ordered by exchange-local date, though multiple events may share
a date.

Missing timestamps, blank/non-numeric/non-finite/non-positive prices, missing
required timeline fields, non-HTTPS source URLs, duplicate event IDs, malformed
JSON, and unknown timezones fail closed. Prices are never imputed. Blank inline
`news` is valid because the column remains optional.

## Controls, interpretation, and known limitations

- CSV-only loading is the null/control for timeline semantics; it is still the
  default and is tested alongside explicit timeline loading.
- Daily closes do not establish that selected news caused a price move. SPY's
  path combines public-health news, policy responses, liquidity, fiscal support,
  and many omitted variables.
- Nasdaq's live historical endpoint is not an immutable archive. The committed
  bytes and `SHA256SUMS`, not a later API response, define data v1.
- SPY is an ETF and the nominal close excludes dividend reinvestment. “Recovered”
  here means its nominal closing price exceeded the chosen t=0 close.
- Publication dates can be less precise than event times. Date-only records are
  kept date-only rather than assigned fabricated times.
- The Meta analyst/narrative and next-session reaction items use traceable
  secondary reporting; corporate results and filings use issuer/SEC sources.
- Meta traded as FB in February 2022 and later changed ticker to META. Nasdaq's
  current META history supplies the preserved observations.
- The current reaction summary is drop-oriented. A positive-jump reference has
  no genuine post-t0 drop and therefore should not be forced into a fabricated
  recovery statistic; signed positive-outcome metrics belong in the later
  preregistered multi-event analysis.
## Preregistered analysis tranche

This section freezes the Wave 1 multi-event variance-components pilot and the
managed analysis boundary. It is intentionally additive to the event-source,
`t=0`, timezone, and `delivery_t` data protocol maintained with Issue #2. The
machine-readable authority for the design and estimator is
[`experiments/multi_event_protocol.json`](../experiments/multi_event_protocol.json);
the single authoritative catalog is
`nmsim/reference_data/v1/catalog.json` (`reference_data_catalog_v1`, data
version `v1`). Its unique dataset mapping and the committed file hashes remain
the authority for bytes delivered to a child simulation. The protocol itself
freezes the catalog SHA-256 and all six CSV/timeline SHA-256 values, so changing
a same-path `v1` file cannot pass by updating only the selection manifest.

## Frozen pilot design

The selected events are `meta_2022_02_crash_v1`,
`spy_2020_03_covid_v_recovery_v1`, and
`meta_2023_02_efficiency_jump_v1`. The treatment arms are `social_off` and
`social_on`; only `Config.social_enabled` changes between the two arms. Seeds
are the frozen set `{11,13,17,19,23,29,31,37}` and fresh Provider repeat
indices are `1..3`. Thus the preregistered
pilot is `3 events × 2 arms × N=8 seeds × K=3 repeats = 144` planned child
simulations. `temperature=0.3`, cache is off, the per-child health gate is
`bad_frac <= 0.15`, and a driver may make at most five immutable child attempts
for a planned cell. Record/Replay is an audit of an observed run, not evidence
that a real Provider is deterministic.

This is explicitly a `preregistered_variance_components_pilot`, not a
confirmatory study. It has two distinct primary questions. The primary realism
criterion asks whether the `social_on` seed-mean trajectory matches all three
fixed crash/recovery/jump targets and the fixed cross-event depth order. The
primary social estimand is the paired event-seed mean-K `drop_depth` difference
between social on and off. Neither primary silently substitutes for the other;
RMSE and DTW remain supplementary.

The primary outcome is `drop_depth`. For each event and seed, the treatment
effect is

```text
mean_K(drop_depth | social_on) - mean_K(drop_depth | social_off).
```

A positive value therefore means that the social arm is less negative (or
more positive) than the no-social arm. Repeated Provider responses are not
independent seed-level samples. The primary statistical unit for an event is
one seed complete across both arms and all three repeat indices for that event.
If any planned child is absent, fails managed identity or artifact-integrity
validation, exceeds the health gate, or lacks a finite primary outcome, that
event-seed is excluded from that event contrast and remains in its exclusion
ledger. It does not erase the same seed from another otherwise complete event.
The cross-event aggregate uses the stricter intersection of seeds complete for
all three events. This preserves honest event-specific N while preventing
selected repeats within an event.

The 95% interval is a percentile clustered bootstrap: resample complete seed
IDs with replacement, keep all event and repeat observations belonging to the
drawn seed cluster, use `B=10000`, RNG seed `20260722`, and `alpha=0.05`.
Events receive equal weight within each seed. Cohen's d is also computed from
the two vectors of K-repeat arm means at seed level, using the n-weighted pooled
sample standard deviation. It is `null` when that denominator is zero; a large
number is never fabricated. A paired standardized effect is supplementary and
is likewise `null` at zero variance.

With at most eight seed clusters, a percentile bootstrap has visibly discrete
support. `B=10000` reduces Monte Carlo jitter but cannot create information not
present in N=8. Intervals may be wide or unstable, and this pilot must not be
reported as a significance test, confirmation, or proof of an effect.

For each event, the balanced method-of-moments decomposition reports the mean
within-seed sample variance over K repeats for each arm, the corresponding
repeat-noise contribution `(s²_on + s²_off)/K`, the observed sample variance of
seed effects, and the non-negative remainder attributed to between-seed effect
heterogeneity. The review target is repeat noise at most 25% of observed effect
variance. A reviewed K extension must add the same repeat index to every cell;
selective repetition is forbidden, and `K > 6` is a no-go under this protocol.
The endpoint diagnostic alone is not a substitute for this market-level
variance estimate.

## Frozen effective configuration

Every field currently classified as scientific by `nmsim.config_contract` is
present in the JSON protocol. Unvaried values are the executable `Config`
defaults. The explicit 30-LLM-agent cast is exactly
`build_population(m=0.5, total=30)`: one influencer, seven retail, seven FOMO,
and five each of value, contrarian, and quant. `n_llm_agents` remains the exact
stored default `6`; the explicit `population` is the effective cast, capped by
`max_llm_agents=40`. As in the existing `run_seed` construction,
`seed_fraction=2/30`. `news_round=1` is an explicit preregistered override (not
the executable default of 12), so the full 24-round simulation horizon is
post-event. Leverage is disabled, while its inactive defaults remain frozen
and hashed rather than omitted.

The integrated core tranche adds scientific `Config.news_timeline`. It is
event-bound to the exact normalized public timeline;
legacy `news_text` is frozen to the empty string so a one-shot headline cannot
silently duplicate timeline delivery. The analyzer independently recomputes
the normalized entries from the authoritative source material and requires the
child manifest's exact `config.news_timeline`; it never injects the field.

The only scientific factors are seed, event-bound public news/reference
inputs, and `social_enabled`. A child must bind the exact reference CSV and
non-leaky `delivery_t` news timeline by content hash. The analyzer neither
injects news nor interprets `price_anchor_t`; it verifies the identity emitted
by the future managed driver. The model request is OpenAI-compatible
`MiniMax-M2.7`, `temperature=0.3`, `max_tokens=1024`, and cache off. Requested,
resolved, and endpoint identities are preserved separately, so an endpoint
that reports an alias such as `HiggsAI` cannot be silently described as a
model-specific MiniMax result.

Driver worker count is execution-only under the existing configuration
contract, but it is held at one for this pilot and recorded in the execution
manifest. Changing workers does not change the stated estimand; it does create
a different execution identity and must not be mixed invisibly into one study.

## Explicit managed analyzer input

The official analyzer accepts one explicit selection manifest and never scans
a result directory. Each event entry supplies the authoritative `event_id`,
reference CSV, timeline, and their expected SHA-256 values. Each child entry
supplies its exact planned cell, managed `run_manifest.json` path and hash,
registered `experiment_result.json` hash, and the expected config/input/model
identity fields listed in the JSON protocol. `catalog_inputs` contains exactly
the authoritative v1 catalog, and event paths must exactly match both the
protocol and the catalog dataset before hashes are considered. Paths are
resolved under explicit child and reference roots; traversal or escaping
symlinks fail closed.

The driver writes `multi_event_selection.json` only after child finalization.
Its `children` array contains accepted managed children only. Its separate
`missing_or_rejected_slots` array names every other planned cell, a status of
`missing` or `rejected`, and non-empty public reason codes. For a
protocol-adherent live execution, the two arrays must be disjoint and their
union must equal all 144 event/arm/seed/repeat cells. A bounded mock engineering
execution instead partitions every slot in its explicitly declared seed/repeat
subset; it is labeled non-adherent and cannot support a preregistered realism
claim.
Omitting an inconvenient failed slot is therefore a schema failure, not a way
to reduce N. `build_selection_document()` is the pure shared builder for this
partition; it performs no discovery and writes no file.

Every accepted child also carries a non-empty, unique `attempt_run_ids` list,
bounded by the frozen five-attempt cap, plus `accepted_run_id` exactly equal to
`identity.run_id` and present in that list. Rejected slots list their bounded
attempts; missing slots list none. One managed attempt ID cannot be assigned to
multiple cells. This preserves the retry ledger without promoting a failed
attempt into honest N.

Path semantics are fixed: `children[].manifest_path` is relative to CLI
`--child-root`; event CSV/timeline and the required catalog path are relative to
`--reference-root`; `result_artifact.path` is exactly
`experiment_result.json` inside the selected managed run. No path is inferred
from an event ID or filename. Each event entry also carries the exact 25-point
`transformed_reference.norm_log_path` and its transform identity hash.

The central managed-child reuse validator then rechecks terminal lifecycle,
completion accounting, recording and scientific schema identity, and every
registered artifact hash. The result itself must contain a first-class
`multi_event_identity` object with protocol hash, event, arm, seed,
`repeat_idx`, both event-input hashes, and the transformed-reference identity.
`rep` filenames or labels are never used to infer identity. A flat JSON without
a managed manifest is ineligible.

The frozen population identity is recomputed from the executable Config
contract, including insertion-order-sensitive effective cast order; child and
selection hashes cannot merely agree with each other. Likewise, the reference
path is bound by content identity rather than by requiring an identical
absolute path string. For live calls, endpoint-reported model aliases bind
three ways across application-attempt evidence, selection identity, and the
result projection. Each child has exactly one non-empty alias and the study
records their sorted union. A mixed union is an endpoint-mixture result and
prohibits pooled model-specific inference. Mock runs fabricate no alias.

Every raw post-`t0` reference episode is transformed to exactly 25 points
(`t=0..24`) by linear interpolation in normalized log price over the entire
captured horizon. For target index `t`, let `u=t*(L-1)/24`; the value is the
linear interpolation between raw indices `floor(u)` and `ceil(u)`. The
transformed values and their SHA-256 are explicit in the selection plan and
child identity, and the analyzer independently recomputes them from the hashed
CSV. RMSE uses this 25-point path. DTW reports both this path and the
uncompressed full reference episode. The identical event-independent transform
was fixed before grid outcomes: there is no outcome-dependent curve fit, time
warp, truncation, or fitted parameter.

Timeline `delivery_t=d` maps non-leakily to simulation round
`min(24, 1 + floor((max(1,d)-1)*24/terminal_t))`, preserving source order when
events map to the same round. Descriptive `price_anchor_t` never drives delivery.

The analyzer writes an immutable JSON summary and a three-panel figure. The
panels show paired seed effects and intervals, mean arm trajectories, and the
within-repeat versus between-seed variance components. RMSE and DTW are
supplementary trajectory diagnostics computed through the existing
`nmsim.validation` functions against the explicitly hashed reference input;
they are not fitted objectives.

The trajectory panel is a real envelope, not six unbanded means. Within each
event/arm complete case, first average the K paths pointwise for each seed;
then, across those seed-mean paths, draw the fixed pointwise 10th and 90th
percentiles (linear type-7 interpolation). Child repeats are not treated as
independent envelope units. Both the all-accepted per-cell distributions and
the stricter primary-complete-case distributions are retained in the JSON.

## Preregistered pilot qualitative criteria

Under the user's broad authorization to proceed, the implementation selected
these reference-derived defaults before any multi-event simulation outcomes;
the user did not explicitly approve the numeric values. “Crash” means
`drop_depth <= -0.15`; “positive jump” means the maximum normalized price over
`t=0..24`, minus one, is `>= +0.10`;
“full recovery” requires a crash and terminal normalized price `>= 0.95`.
All bounds are inclusive. A missing or non-finite value is indeterminate and
cannot pass. Recovery fraction is also reported but does not replace the
terminal-price boundary.

Targets are Meta 2022 crash/no full recovery/no positive jump; SPY 2020
crash/full recovery/no positive jump; and Meta 2023 no crash/no full recovery/
positive jump. Reference depth must rank from most to least negative as Meta
2022, SPY 2020, Meta 2023, with every adjacent margin `>= 0.01`. The analyzer
applies the same fixed boundaries to each social arm for transparency, but the
primary realism criterion is the `social_on` assessment. It never derives
labels or thresholds from observed simulation values.

## Scientific semantics and compatibility

The integrated Issue #1 change adds an opt-in scientific `news_timeline` and
public timeline observation to the Prompt when that tuple is non-empty. This is
a scientific simulator change. An empty tuple preserves the legacy one-shot
news path. Personas, market clearing, leverage, private rationale visibility,
and the private-to-public information boundary do not change. This analysis
tranche adds the protocol and a Provider-free managed derived analysis;
existing legacy analyzers remain unchanged. Historical flat files remain
usable only in explicitly labeled legacy analyses and cannot enter this
identity-validated pilot input contract. Driver retry and attempt-level
Provider provenance must pass their own tests before the 144-run live grid is
eligible.
