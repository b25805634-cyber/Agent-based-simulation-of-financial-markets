# V2 limited-attention behavior-distillation market

Status: engineering prototype protocol `v2_attention_market/0.1`. This document
freezes the implementation contract used by the offline test double. It does
**not** establish that an LLM is human-like, that the Student reproduces real
traders, or that a simulated price path is a bubble.

## Research question and boundary

V2 asks a deliberately narrower question than the legacy Persona market:

> Given only a fixed daily price/volume tape and one trader's own account
> state, can an offline Teacher produce a stable conditional decision
> distribution, can a small numerical Student approximate that distribution,
> and what changes when the Student is embedded in a closed, budget-constrained
> batch market?

The V2 path is isolated from the legacy Persona, social-feed, leverage, prompt,
Config, market-clearing, CLI, and result schemas. It does not modify
`nmsim/prompts.py` or `nmsim.market.clear_by_pressure`. Private Teacher
reasoning is never a public signal and never enters the market state. There is
no brokerage connection, real money, reinforcement learning, social channel,
fundamental-value claim, or full limit-order book.

## Fixed time, state, and action semantics

One round is one trading day. At the start of day `t`, every trader observes
the same immutable close/volume history through `t` and its own account
snapshot. All decisions are simultaneous. Orders are constrained, cleared in
one call auction, and atomically settled to form the close for `t+1`; no trader
can observe another trader's day-`t` decision before choosing.

The visible state has a fixed numeric order:

1. one-, five-, and twenty-day returns;
2. twenty-day realized volatility and drawdown;
3. volume z-score;
4. invested fraction;
5. unrealized return plus an explicit known/missing mask;
6. scaled days since the trader's last trade plus a mask;
7. post-sale return plus a mask; and
8. base-10 log wealth.

The five price-derived tape summaries are not sampled independently. For each
designed state, twenty simple daily returns define one coherent sequence of 21
strictly positive closes, `P_0,...,P_20`, with
`P_i=P_(i-1)*(1+r_i)`. The implementation then derives
`return_1d=P_20/P_19-1`, `return_5d=P_20/P_15-1`,
`return_20d=P_20/P_0-1`, population standard deviation of the twenty daily
returns times `sqrt(252)`, and `drawdown_20d=P_20/max(P_0,...,P_20)-1` from that
same tape. LHS rows construct the first nineteen log returns around an exactly
matched 20-day total and set the twentieth return to the sampled one-day
return; the nine frozen anchors use nine explicit twenty-return tapes. Thus a
row cannot combine independently invented one-, five-, and twenty-day returns,
volatility, and drawdown. Volume is a separate share-volume series and is not
implied by the close tape.

The default 96-state design consists of nine fixed, auditable anchors followed
by 87 deterministic LHS rows. The anchors are neutral cash, cash rally, holder
rally, holder drawdown, post-sale rally, fully invested, maximum trade age,
zero-variance-volume upward jump, and zero-variance-volume downward drop. They
make reachable closed-domain boundaries explicit, including position fraction
`0/1`, scaled trade age `0/1`, and volume z-score `-6/+6`. LHS rows are assigned
in balanced cyclic order across the nine latent cells
`{decline,flat,rise} x {cash,mixed,invested}`; within those cells the 20-day
return and position fraction are sampled from their cell-specific ranges.
Diagnostic labels use `return_20d < -0.10` for decline, `> 0.10` for rise, and
flat otherwise; position fraction `< 0.20` is cash, `> 0.80` is invested, and
the interval between them is mixed.
"Balanced" means deterministic near-balance subject to the requested row count
and the nine anchors, not identical observed counts in every cell.
These inequalities, the nine-cell requirement, the minimum five families per
cell for joint stratification, the `0.70/0.15/0.15` allocation, and the
tape-only small-design fallback are emitted together as the machine-readable
`classification_and_allocation_contract` inside `v2_scientific_config_hash`;
they are not undocumented implementation defaults.

Feature units and formulas are frozen as follows:

| Feature | Unit / effective formula |
|---|---|
| `return_1d`, `return_5d`, `return_20d` | dimensionless simple close returns over 1, 5, and 20 daily sessions |
| `realized_vol_20d` | population standard deviation of the latest 20 simple daily returns, annualized by `sqrt(252)` |
| `drawdown_20d` | `P_t/max(P_(t-20),...,P_t)-1` |
| `volume_z` | current share volume minus the prior-20-session mean, divided by the prior-20 population standard deviation |
| `position_fraction` | security value divided by cash plus security value |
| `unrealized_return` | `P_t/cost_basis_price-1` when its mask is 1 |
| `days_since_trade_scaled` | daily decision intervals since last trade divided by 20, then bounded at 1, when its mask is 1 |
| `post_sale_return` | `P_t/most_recent_sale_price-1` when its mask is 1 |
| the three `*_mask` fields | exact binary `0/1`; `0` requires the paired numeric field's canonical `0.0` placeholder |
| `log10_wealth` | base-10 logarithm of net account wealth measured in integer cents |

For `volume_z`, if the prior-20 population standard deviation is zero, equality
with that constant history maps to `0`; a higher or lower current share volume
maps to the signed reachable boundary `+6` or `-6`. This sentinel is explicit,
not a division-by-zero imputation. Market-generated raw fields outside the
closed feature domain are recorded as clamped and only the effective bounded
state is passed to a policy.

Unknown fields, non-finite values, out-of-contract ranges, or inconsistent
masks fail closed. Missing values are represented by a neutral numeric value
and a separate mask; they are never guessed from text. The prompt contains no
identity type (for example retail, institution, dealer), no named behavioral
effect, and no instruction to imitate such an effect.

The Teacher response schema is exactly:

```json
{"action":"buy|hold|sell","intensity":0.0,"reasoning":"private text"}
```

`intensity` is a fraction in `[0,1]` of feasible purchasing power for a buy or
sellable holdings for a sell. A hold must have zero intensity. Selling with no
position and buying from a fully invested state are invalid Teacher samples;
they are recorded as failures, not rewritten as holds. `reasoning` is private
and is excluded from every public dataset, report, feature, and market input.

## Teacher sampling and honest N

The state design combines hand-auditable anchors with a deterministic
Latin-hypercube design. Integer sub-seeds are derived by SHA-256 for the
design, bundled Fake Teacher, Student initialization, and market mechanisms.
The real OpenAI-compatible Teacher does not receive a request seed; repeated
calls are identified by a SHA-256 content identity but are not claimed to be
deterministic. Identity is
hierarchical: a `family_id` binds related states, a `state_id` binds the exact
visible state, and a sample identity additionally binds the replicate.

Repeated calls use the exact same prompt, have no conversational memory, and
must bypass response caching. A parse error, provider exception, infeasible
action, or schema violation remains a failed attempt. The public record keeps
state/sample hashes and the parsed action/intensity; the full prompt, raw
response, rationale, and detailed error are written only to an exclusive
mode-`0600` artifact. Reported counts distinguish states, planned attempts,
attempted requests, raw responses, valid parsed responses, failures, and
aggregated examples. `honest_n_teacher_samples` means valid parsed responses,
not requested calls and not state count.

Each completion is committed in a fixed durability order: its full private
record is appended to the exclusive mode-`0600` JSONL and `fsync`ed; its public
projection is then appended and `fsync`ed; only then are the in-memory result
and manifest counters advanced. Accounting is refreshed again in `finally`.
After an interruption, `honest_n_teacher_samples` therefore counts only valid
publicly committed samples, while attempted-but-unresolved calls are reported
as `unresolved_attempts` and failures rather than inferred as responses. A
private record may be the last durable evidence if interruption occurs between
the two commits; it does not silently increase public honest-N.

The built-in `fake_test_teacher` and its constant null are deterministic
engineering fixtures. Their output is intentionally structured enough to test
the data and training code, but it is not evidence about an endpoint or a
person. A real OpenAI-compatible Teacher is available only behind both an
explicit provider selection and `--live`; construction failure never falls
back to Fake or Mock.

## Dataset split and Student

All replicates and related states in one `family_id` enter the same split.
Family assignments are frozen deterministically to train/validation/test
before any Teacher request, and input row order cannot change the assignment.
A state with zero valid Teacher responses retains its pre-frozen family and
partition identity for dataset accounting, but contributes no Student training
row. Feature mean and scale are fitted on train groups only. The frozen test
groups are never used for model selection.

At the default design size, the split is stratified on the two-dimensional
`return_20d` tape regime x `position_fraction` regime. The implementation uses
that joint split only when all nine observed cells exist and each contains at
least five families. For a smaller design that cannot support that allocation,
it fails over explicitly to tape-regime-only stratification; it never pretends
that a missing joint cell was covered. Preflight still requires non-empty
train/validation/test partitions and every tape regime in every partition, or
the run is rejected before Provider construction. The split manifest reports
the selected stratification unit, per-partition tape and tape x position
counts, all missing cells, and the fact that no confirmatory coverage threshold
has yet been frozen. It reports
`planned_design_partition_regime_coverage` over every pre-request family and
`training_eligible_partition_regime_coverage` over only states with a valid
aggregate; these are deliberately non-interchangeable. The actual post-Teacher
dataset is projected through the pre-frozen assignments; zero-valid-response
families retain their planned family, stratum, and partition identity but
supply no training row.

Each state aggregates valid replicates into a soft action target
`(p_buy, p_hold, p_sell)` and conditional buy/sell intensity targets, variances,
and effective sample counts. The null Student is the train-set action prior
plus train-set conditional intensity means. The learned Student is a
dependency-free one-hidden-layer network with a shared `tanh` representation,
a three-class softmax action head, and two sigmoid intensity heads. Its fixed
loss is soft-label cross entropy plus probability-weighted conditional
intensity squared error and L2 regularization. Model weights and preprocessing
are JSON, never pickle.

Reports include group-held-out cross entropy, Brier score, argmax accuracy,
conditional intensity error, Teacher disagreement, and out-of-design
diagnostics. Passing code tests does not imply a scientific acceptance
threshold. A real-data acceptance threshold must be frozen before a live
confirmatory run; it must not be invented after inspecting the test set.

The OOD reference is fit from train rows only: a per-feature minimum/maximum
rectangle plus the train-only standardizer. Validation and frozen-test rows are
reported separately against it. During the market, the same reference is
applied to every effective, clamped, pre-decision agent-round state and reported
across all cells, by each budget x behavior cell, and by named feature. These
are marginal rectangular-range and standardized-tail diagnostics. They do
**not** estimate joint support or density, do not establish extrapolation
validity, and do not make an outside-range state invalid. Momentum cells are
also diagnosed on the complete effective state for comparability even though
their policy consumes only the tape features.
The standardized-tail rule is frozen as `abs(z) > 3.0`; its train-only
reference, rectangular geometry, evaluation scopes, threshold, and explicit
`joint_support_assessed=false` are serialized under the scientific config.

Teacher disagreement is also descriptive: across states with at least two
valid replicates, the report gives mean action Gini impurity and mean
conditional buy/sell intensity variance. The fixed MLP is deployed because the
protocol fixes it in advance;
its frozen-test cross entropy is displayed alongside the action-prior and
linear baselines even if it loses to either. This comparison is not used for
model selection and is not a human-validity or acceptance result.

## Conserving market and the 2 x 2 controls

V2 uses integer cents and integer shares. An intent, a constrained order, and
a fill are distinct records. A trader may submit at most one active order per
day. Buy quantity is bounded by cash plus unused, explicitly configured credit;
sell quantity is bounded by owned shares. Invalid and duplicate orders fail
closed.

The call auction evaluates the previous close and submitted limit prices. It
chooses the price by, in order: maximum matched shares, minimum absolute
buy/sell imbalance, minimum distance from the previous close, and a fixed
deterministic final tie-break. Oversubscribed sides are allocated pro rata with
integer largest remainders resolved by stable order identity. With no crossing
interest, volume is zero and the close is unchanged. There is no hidden market
maker and no phantom fill.

Settlement is atomic. Every successful round checks:

```text
sum(buyer shares) == sum(seller shares) == matched shares
total cash including the credit facility is unchanged
total shares are unchanged
all cash, shares, debt, and facility balances are non-negative
sum(borrower debt) == facility loan asset
every fill respects both submitted limits
```

The budget control is an explicit credit facility, not negative cash. In the
finite arm every credit limit is zero. In the credit arm the same facility
transfers only the actual settlement shortfall, and records equal borrower
debt and facility loan assets. The facility has the same initial balance and
is present in all cells, but is idle in the finite arm.

Credit charges zero interest and zero fees. There is deliberately no automatic
principal repayment within the finite simulation horizon, including after a
later sale; borrowed principal remains outstanding in each account's terminal
`debt_cents` and as the facility's terminal loan asset. Results must therefore
report ending debt rather than imply that credit was repaid or free in an
economic-welfare sense.

The complete engineering diagnostic is paired on initialization and random
sub-streams:

| Budget | Behavior | Purpose |
|---|---|---|
| finite | distilled Student | full proposed mechanism |
| finite | momentum-only policy | behavior ablation |
| credit | distilled Student | budget relaxation |
| credit | momentum-only policy | joint control |

The momentum-only policy sees only the tape, not identity or account-history
features. Initial accounts, observation timing, limit conversion, clearing,
and settlement are otherwise identical. The report labels run-up, reversal,
drawdown, turnover, locked days, credit use, and ending debt descriptively. Without an
independently specified fundamental value, it does not label a path as a
bubble or estimate causal effects from one engineering seed.

## Versioned identities and artifacts

The V2 entrypoint emits four explicit, secret-free identities rather than the
phrase “default Config hash”:

- `v2_scientific_config_hash`: state/action contract, data design, split,
  Student architecture/loss, market rules, and 2 x 2 parameters;
- `v2_model_request_config_hash`: provider, requested model, temperature,
  token cap, and replicate plan;
- `v2_execution_config_hash`: worker count, live/dry mode, and implementation
  execution settings, including the V2 execution-component fingerprint; and
- `v2_full_effective_config_hash`: an envelope binding the preceding three.

These hashes are V2-specific because the existing `Config` and scientific
fingerprint are legacy-V1 contracts. The managed context's legacy Config is
used only to obtain the existing immutable run lifecycle; the manifest marks
that scope explicitly and does not claim the V1 Persona fingerprint describes
V2 science.

A full offline run writes a public sample stream, private Teacher records,
aggregated dataset, group split, preprocessing, Student and baseline JSON,
evaluation/OOD summaries, four market ledgers, a machine-readable summary, and
Markdown/HTML reports. Every file is created exclusively inside a new managed
run directory and hashed before terminal success. Historical runs are never
overwritten.

`student_model_envelope.json` binds the feature order and state-contract hash,
dataset and training-projection hashes, frozen split and OOD-reference hashes,
the protocol-fixed deployed model, and every model artifact. Three hash kinds
must not be conflated:

- `model_semantic_hash` hashes the canonical model payload and identifies its
  numerical/structural meaning;
- `artifact_sha256` hashes the exact bytes written for that model file; and
- `model_envelope_hash` hashes the envelope before its self-hash field is
  added, while the envelope file has its own separate artifact SHA-256.

The market `model_lineage` carries the deployed model semantic hash and exact
artifact SHA-256, both envelope hashes, and the contract/dataset/projection/OOD
bindings. It is copied into the 2 x 2 index, each completed run ledger, and each
durable round row so a market path cannot silently detach from its Student.

Market persistence is incremental. A run opens an exclusive round JSONL; each
settled round is appended with `model_lineage` and `fsync`ed before its
completion counter advances. Only a fully completed cell x seed receives its
exclusive, `fsync`ed full-run JSON and enters `honest_n_market_runs`. On interruption,
durably settled rounds remain counted, a started incomplete round/run is
reported failed, and unstarted slots remain skipped; a partial round stream is
not promoted to a completed market run.

The scientific component fingerprint intentionally takes the conservative
fail-closed choice of hashing the four V2 modules **and the whole managed V2
entrypoint**. Because report rendering lives in that entrypoint, a report-only
edit can currently churn `v2_scientific_config_hash` even when numerical
semantics did not change. This is known overbinding, not proof that prose
changed the science; it prevents underbinding until the entrypoint can be
split under a separately reviewed fingerprint schema.

For an OpenAI-compatible run, `v2_model_request_config_hash` uses a V2-local,
secret-free endpoint route identity: userinfo and fragments are omitted, query
keys may identify routing but query values are omitted, and a configured API
key found in the path is redacted before hashing. Credentials and raw endpoint
text are never hash inputs. The real request sends no seed because this
transport contract does not support one; the SHA-256 replicate identity is an
accounting identity, not a Provider randomness control or determinism claim.

## Commands and interpretation

Plan validation without constructing a Provider:

```bash
python3 -m experiments.v2_attention_market --dry-run --out results_v2
```

Complete offline engineering run:

```bash
python3 -m experiments.v2_attention_market \
  --provider fake_test_teacher --out results_v2
```

Real endpoint execution is intentionally separate and must not be run until
the prompt, sample size, acceptance criteria, and endpoint have been reviewed:

```bash
OPENAI_BASE_URL=http://HOST/v1 OPENAI_API_KEY=... \
python3 -m experiments.v2_attention_market \
  --provider openai --model MODEL --live \
  --states 96 --replicates 5 --confirm-request-count 480 \
  --out results_v2
```

`--confirm-request-count` must equal `--states * --replicates` exactly; any
mismatch is rejected before Provider construction.

The offline command proves that the schemas, privacy boundary, accounting,
training, clearing, controls, and reports execute end to end. It does not prove
human validity. This repository currently has no human labels, no frozen
scientific acceptance threshold, and no authorized real-endpoint result under
this protocol. Human comparison data, real Teacher qualification, live sample
size, and preregistered scientific thresholds remain explicitly pending.
