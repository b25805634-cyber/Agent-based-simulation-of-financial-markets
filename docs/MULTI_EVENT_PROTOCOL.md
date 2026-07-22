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
