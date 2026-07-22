# Endpoint stochasticity diagnostic

`python3 -m experiments.endpoint_stochasticity` is the Wave 0 measurement of
first-response variability at the configured OpenAI-compatible endpoint. It is
an official `direct_managed` research entrypoint with
`run_kind=endpoint_stochasticity`, but it is **not** a market simulation, a
model-quality qualification, or proof that a Provider is deterministic. Its
purpose is to measure a noise floor before choosing the repeat count `K` and
simulation-seed count `N` for later experiments.

Temperature zero, a local seed, request caching, or two equal responses must
not be interpreted as full determinism for a real Provider. Record/Replay can
audit an already observed response; it does not remove first-sampling noise
from an estimand.

## Frozen design

The diagnostic validates the existing model-qualification protocol and builds
its complete 6 Personas x 8 Observation fixtures = 48-case universe. It then
selects a versioned, frozen six-case diagnostic panel from that universe. The
panel covers cascade fuel, dampener/narrative-immune, and spark roles. Case
identities retain the qualification protocol version, Persona id, fixture id,
fixture input hash, and stable case id; the endpoint diagnostic does not
silently edit a Prompt, Persona, fixture, or parser to create its panel.

| Role | Persona | Fixture | Qualification case id |
|---|---|---|---|
| fuel | `retail_crowd` | `negative_news_price_unchanged` | `qcase-cc0dec3634c3c8bd87d254f8` |
| fuel | `fomo_momentum` | `price_crash_no_news` | `qcase-0587fd0f734b430d3ef77b41` |
| dampener | `value_institution` | `deep_discount_to_fundamental` | `qcase-b1760dde034a888e00e388cb` |
| dampener / narrative-immune | `quant_arb` | `neutral_placebo_news` | `qcase-d05d85bfa6ecc87c74905e43` |
| spark | `influencer_amplifier` | `conflicting_neighbor_views` | `qcase-ddaedf5354a0d12a03ba066b` |
| dampener | `contrarian_fund` | `unanimous_neighbor_panic` | `qcase-a5a5109b01a620d9dbc25f51` |

The frozen `selection_hash` is
`db008c386d6eb5a9dccdd91d4c8e978c22523a969857187b1e3e56327488223c`.
Startup fails closed if rebuilding the panel from the validated source bundle
does not reproduce these identities and this hash.

The complete plan, including the balanced wave schedule and metric contract,
has `study_plan_hash`
`d45392b4741a97006987325287cb76f9c1a450ecdb46cc0a220a65a2c69c993d`.
Changing it requires an explicit study-plan schema review rather than a silent
constant edit.

The main grid is:

| Dimension | Frozen values | Count |
|---|---|---:|
| qualification cases | representative subset of the validated 48 cases | 6 |
| requested temperature | `0`, `0.3` | 2 |
| repeats per cell (`K`) | `30` | 30 |
| client concurrency limit | `1`, `8`, `32` | 3 |
| planned main-grid endpoint samples | 6 x 2 x 30 x 3 | **1080** |

A separate same-seed probe sends two otherwise identical requests with the
same explicit seed. Its two calls are recorded separately and never added to
the 1080 main-grid denominator. The probe asks whether this endpoint accepts
and appears to honor that request field for those two observations; agreement
does not establish general or future determinism.

Concurrency is an experimental condition, not merely a worker tuning flag.
The reported value is the client-side maximum in-flight request count. It
cannot reveal the Provider's hidden server batching, scheduling, retry, or
hardware occupancy.

The six conditions are not run as six monolithic time-ordered blocks. The
frozen plan divides `K=30` into six waves and cyclically rotates all six
temperature/concurrency conditions so every condition occupies every
within-wave position once. Repeat spans are `6+5+5+5+5+4`; the first wave
therefore has 36 requests per condition and can exercise the declared
concurrency-32 ceiling. This controls first-order ordering and connection
warm-up imbalance, but it cannot eliminate unobserved endpoint drift within a
wave or across the entire measurement window.

## Safe commands

Inspect and validate the full 48-to-6 plan without constructing a Provider or
accessing the network:

```bash
python3 -m experiments.endpoint_stochasticity \
  --provider openai --dry-run \
  --out /tmp/nmsim-endpoint-stochasticity-dry
```

Run the complete 1080-sample grid and the two-call probe against the
deterministic in-process test double:

```bash
python3 -m experiments.endpoint_stochasticity \
  --provider fake_test_provider \
  --out /tmp/nmsim-endpoint-stochasticity-fake
```

Real OpenAI-compatible endpoint traffic requires the explicit `--live` guard;
credentials and endpoint configuration continue to come from the existing
environment/configuration boundary and are never written to public artifacts:

```bash
python3 -m experiments.endpoint_stochasticity \
  --provider openai --live \
  --out /tmp/nmsim-endpoint-stochasticity-live
```

An OpenAI execution request without `--live` is rejected before Provider
construction. `--dry-run` is a zero-network planning path and must report zero
Provider calls; combining it with `--live` is rejected as contradictory.
`--help` is side-effect free and lists the available flags, while the three
reviewed command forms are shown above.

## Measurements

Each completed endpoint sample records, at minimum:

- the qualification case identity, temperature, concurrency level, repeat
  index, and whether it belongs to the main grid or the same-seed probe;
- a SHA-256 hash of the exact raw response bytes;
- the parsed `sentiment`, `action`, `quantity`, `limit_price`, and explicitly
  public `public_take` fields;
- the signed order quantity, defined as `+quantity` for `buy`, `0` for `hold`,
  and `-quantity` for `sell`;
- observed token usage when the endpoint supplies it, latency, parse status,
  Provider failure/degraded status, and sanitized error classification.

Raw responses are compared by SHA-256 of the exact response-content string
returned by the SDK encoded as UTF-8, so the raw text need not cross the
privacy boundary. This is the model message content, not the complete HTTP
response bytes. Normalized JSON, equivalent meaning, equal parsed decisions,
or equal `public_take` strings do not count as identical responses.

### Pairwise within-case byte agreement

Agreement is computed within a fixed main-grid cell: one qualification case,
one requested temperature, and one concurrency level. For `n_c` completed raw
responses in cell `c`, every unordered pair is compared:

```text
agreement_c = matching raw-response-hash pairs / choose(n_c, 2)
```

The aggregate reports matching and eligible pair counts, not only a rounded
rate. Its `(temperature, concurrency)` rate is the sum of matching within-case
pairs divided by the sum of eligible within-case pairs; it never compares two
different qualification cases. A failed or missing response does not become a
disagreement pair; it is reported through honest-N/failure accounting. Cells
with fewer than two eligible responses have no defined agreement rate.

### Within-case and pooled noise sigma

For both parsed sentiment and signed order, each cell first uses the ordinary
sample standard deviation with Bessel's correction (`ddof=1`). A noise-floor
row for a fixed `(temperature, concurrency)` pools the six within-case sample
variances without treating between-case level differences as endpoint noise:

```text
pooled_sigma = sqrt(
    sum_c ((n_c - 1) * sample_variance_c)
    / sum_c (n_c - 1)
)
```

The summary therefore exposes separate `sentiment` and `signed_order` sigma
values for every temperature/concurrency row. It does not take the standard
deviation of all six case means, which would mix intended stimulus/Persona
differences into the stochastic noise estimate. Parse failures reduce the
eligible `n_c` for parsed metrics and remain visible in completion counts.

## Artifacts and privacy

Every execution is owned by `ManagedRunContext` and writes to a unique,
non-overwriting managed run directory. The diagnostic's schema artifacts are:

| Artifact | Mode and contents |
|---|---|
| `dry_run_summary.json` | Dry-run only; public validated 48-case source inventory, exact frozen six-case selection and hash, full 1082-request plan, schema declarations, and zero-call/zero-network accounting. |
| `endpoint_stochasticity_summary.json` | Public protocol/selection hashes, secret-free Provider/model identity, mode/live guard, planned and realized counts, per-cell agreement and sample variability, pooled sigma table, and separately labeled same-seed result. |
| `endpoint_samples.jsonl` | Public per-sample identities, raw-response hash, parsed public decision fields, token/latency observations, and parse/failure flags. No raw Prompt, raw response, credential, or private rationale. |
| `private_endpoint_records.jsonl` | Mode `0600`; full system/user Prompts, raw responses, private reasoning and detailed failure material required for audit. |

The full public summary has a versioned `output_schema_version` and the
following top-level contract: `run_id`, `run_kind`, `study_plan`,
`study_plan_hash`, secret-free `provider` and `model`, `live`, `completion`
with separately unitized `grid`/`seed_probe`/`total` planned and realized
counts, `honest_n_endpoint_responses`, `honest_n_parsed_decisions`,
`sigma_table`, per-case `cell_summaries`, `seed_probe`, and the artifact-name
map. A sigma row identifies temperature and concurrency and retains its
eligible case/sample/pair counts, byte-agreement numerator/denominator and
rate, sentiment sample variance/sigma, and signed-order sample variance/sigma.

Every public sample row has its own `sample_schema_version` and includes
`measurement_kind`, `sample_id`, `attempt_order`, `case_id`, `fixture_id`,
`persona_id`, `mechanism_role`, `temperature`, `concurrency_level`,
`repeat_index`, `schedule_block_index`, `wave_index`,
`within_wave_position`, status,
`raw_response_sha256`, `parse_failed`, parsed `sentiment`, parsed `order`
(`side`, `quantity`, `limit_price`, `signed_quantity`), `public_take`, token
counts when observed, and `latency_ms`. Same-seed rows additionally carry the
requested `seed`. Private rows bind to the same sample id and add the exact
system/user Prompts, raw response, private rationale, and detailed error.
`attempt_order` follows actual scheduled submission order (the seed probe is
last), not a separate temperature-sorted canonical order; streamed complete
runs therefore contain monotonically increasing values `0..1081`.

The managed `run_manifest.json` and public/private event streams retain the
normal lifecycle, artifact-integrity, configuration-identity, and completion
contract. Public hashes are evidence identities, not a license to publish the
private bytes from which they were calculated. Only `sentiment`, `action`,
`quantity`, `limit_price`, and `public_take` are parsed public response fields;
private rationale must never enter a public artifact or the social feed.

The manifest carries a versioned capability snapshot for the dedicated
endpoint-stochasticity adapter. It does not reuse the similarly named
production OpenAI or qualification Fake capability record: the diagnostic
contract explicitly records zero retries/cache/replay, optional usage and
response-id handling, seed-field probing, connection limits, environment-only
credentials, and `http_trust_env=false`. This distinction is provenance, not a
claim about hidden server capabilities.

Because one managed diagnostic intentionally sends two temperatures, its
ordinary `model_request_config_hash` describes only the bootstrap `Config`
baseline and is not a standalone identity for this study. The authoritative
request-plan identity is `endpoint_stochasticity.study_plan_hash`, together
with each sample's case, temperature, concurrency, repeat, model, and endpoint
identity recorded in the manifest/row. The manifest labels this scope
explicitly; callers must not report a vague "default Config hash" for this
multi-temperature run.

Dry-run writes `dry_run_summary.json` instead of the three execution artifacts.
It records the validated 48-case universe, exact six-case selection, full grid
and seed-probe plan, artifact schemas, and zero-network/zero-call accounting.
It does not construct the Provider and does not fabricate sample rows or noise
estimates.

During execution, public and mode-`0600` private sample rows are appended and
fsynced after each frozen schedule block, and endpoint-specific honest-N is
checkpointed in the manifest at the same boundary. A failed or interrupted
managed attempt therefore retains all completed prior blocks rather than
reporting endpoint honest-N as zero. The terminal summary is written only for
a fully completed run; partial files remain labeled by the failed manifest and
must not be reused as a completed experiment.

## Honest-N

The summary keeps the following units distinct:

- 48 qualification cases are the validated source universe; they are not 48
  observations in this diagnostic.
- 6 selected cases are fixed stimuli and are not independent market
  replicates.
- 1080 is the planned number of main-grid logical endpoint samples.
- 2 is the separately planned same-seed probe call count.
- started, raw-response-completed, parse-completed, Provider-failed, and
  degraded/fallback counts are reported rather than inferred from file rows.
- pairwise comparisons are derived comparisons, not new samples.
- `honest_n_runs=0`: no market simulation ran, no price path was produced, and
  the endpoint calls do not count as simulation replicates.

The realized denominator for response agreement is completed raw responses;
the realized denominator for sentiment/order sigma is successfully parsed
decisions. Token usage may be unknown even for an otherwise completed sample.
Neither missing usage nor a Provider's invisible internal retry is guessed.

## Interpreting the sigma table for later N/K design

For a later two-arm experiment, let `N` be independent simulation seeds (or
other defensible independent design units), `K` be repeated endpoint draws per
condition/seed, and `sigma_0`, `sigma_1` be relevant within-condition response
noise standard deviations. If repeated draws are approximately independent
and their mean feeds the estimand, the endpoint-noise contribution to the
variance of an arm difference is approximately:

```text
Var_noise(delta_hat) ~= sigma_0^2 / (N*K) + sigma_1^2 / (N*K)
SE_noise(delta_hat)  ~= sqrt((sigma_0^2 + sigma_1^2) / (N*K))
```

Under the equal-noise approximation this becomes
`sqrt(2) * sigma / sqrt(N*K)`. For a two-sided test with target effect `delta`,
significance `alpha`, and power `1-beta`, the noise-only lower-bound relation is:

```text
N*K >= (z_(1-alpha/2) + z_(1-beta))^2
       * (sigma_0^2 + sigma_1^2) / delta^2
```

Use the row matching the planned concurrency, or a predeclared conservative
upper row if deployment concurrency can vary. If concurrency materially raises
sigma, power calculations and the production runner must not silently use
different concurrency regimes.

This diagnostic measures decision-level sentiment and signed-order noise, not
the downstream variance of price drawdown, liquidation, or cascade statistics.
Those nonlinear market outcomes require a pilot that propagates repeated
decisions through the simulator. `K` averages response noise within a fixed
condition/seed; `N` covers seed-, population-, event-, and market-path
heterogeneity. Therefore an equal product `N*K` is not generally an equal
scientific design, and increasing `K` must not replace independent `N` without
an explicit variance-components/power analysis.

## Limitations and scientific-semantics statement

- The six frozen cases cover named Persona roles but do not estimate
  variability over all 48 qualification cases or arbitrary production
  Prompts.
- Results identify one observed endpoint/model/configuration/time window.
  Provider weights, serving stack, quantization, batching, or retry policy may
  change later.
- Client concurrency does not expose actual server batch membership. Latency
  and token reporting are endpoint/environment diagnostics, not quality scores.
- The balanced wave schedule reduces first-order order/warm-up imbalance but
  does not prove stationarity or remove drift within the measurement window.
- The same-seed probe has only two calls. Acceptance or equality is not proof
  that a seed is honored in all requests; inequality is direct evidence that
  the requested setup is not byte deterministic.
- Pairwise byte agreement is stricter than parsed-decision agreement but does
  not describe the economic size of a disagreement. Signed order omits limit
  price and `public_take` content.
- Fake full-grid output verifies orchestration, accounting, privacy, and schema;
  it is not an empirical noise-floor estimate for a real endpoint.

This Wave 0 mechanism adds a managed, non-market diagnostic and new artifacts
only. It does not change existing Agent observations, Prompts, Personas,
Provider behavior, parser semantics, social propagation, market clearing,
portfolio/risk logic, Config defaults, official simulation outputs, recording
schema, or historical results. Existing scientific semantics and result
schemas remain unchanged.
