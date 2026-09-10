# Liêu–Pelster public human trading reference

The public data have been acquired and their two publisher-provided SHA-256
hashes verified. They contain 81 incentivized human participants. They provide
an external behavioral reference, but they do **not** answer the existing
single-asset, twenty-day information-emphasis benchmark. No model–human score
or human-validity claim follows from this source audit.

## Sources and preserved inputs

- Liêu, M.-L. and Pelster, M. (2020), *Framing and the disposition effect in a
  scopic regime: Raw data*, Mendeley Data, version 1, published 13 February
  2020, DOI [10.17632/jfg8s32xdm.1](https://data.mendeley.com/datasets/jfg8s32xdm/1).
  The dataset page specifies CC BY 4.0.
- Authors' data article, *The disposition effect in a scopic regime: Data from
  a laboratory experiment*, Data in Brief 31, 105680,
  [DOI 10.1016/j.dib.2020.105680](https://doi.org/10.1016/j.dib.2020.105680).
  Its final full-text XML was obtained from the
  [Europe PMC record](https://www.ebi.ac.uk/europepmc/webservices/rest/PMC7256287/fullTextXML).
  Its own license statement is CC BY 4.0.
- Authors' [Paderborn working paper, July 2019, no. 43](https://en.wiwi.uni-paderborn.de/fileadmin-wiwi/cetar/TAF_Working_Paper_Series/TAF_WP_043_MinhLyPelster_2019.pdf),
  including full experimental instructions and the task screenshot. This is
  the author working paper, not the downloaded final publisher PDF. The
  journal article is [QREF 78 (2020), 175–185](https://doi.org/10.1016/j.qref.2020.01.008).
  Do not extend the dataset's CC license to the working-paper PDF.

Local acquisition time: 2026-09-09 UTC. Inputs are create-only and ignored by
Git, under this path relative to the `agent-market-loop` worktree:

```text
results_human_reference_inputs/lieu_pelster_v1/
  327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333/
```

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| data_de_scopic.csv | 329,461 | `327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333` |
| data_de_scopic.RData | 22,405 | `2f7b253637402d1dbafe0928b8c706bda9ad4fe1087fa0a3166c88f7c8d1f3d1` |
| data_article.xml | 27,933 | `8c8b5c041f940cf5259a6b55932180303734dd525988eb434da4b3e66c07fa7d` |
| author_working_paper_2019.pdf | 554,419 | `f32eabfb37b88961be59ba89c2f4fb70c280ee3c18771007bf1bb46eb83d5c1f` |

`source_inventory.json` also identifies the exact official download URLs,
saved dataset HTML and API file list. `source_audit.json` contains the
independent, one-off source audit, including field missingness and observed
price values. These are external input diagnostics, not managed model runs.
The RData bytes are preserved and hash-checked; the numerical audit uses the
CSV, with no claim of a separate RData object-equivalence verification.

## Source task and count reconciliation

The experiment used six assets, initially 10,000 Talers, integer quantities
and exogenous price changes. Participants could buy, sell or leave each asset
unchanged. Price-generating processes were described to them, but each
asset's type was hidden. The treatment changed how three-person peer groups
were ranked. `spt1` used profitable-trade percentages and also displayed
portfolio value; `spt2` used portfolio value. Rankings occurred after main
periods 5, 10 and 14. The article reports six laboratory sessions in June
2019. The observations are experimental periods, not calendar trading days.
[Data article, sections 1–2](https://pmc.ncbi.nlm.nih.gov/articles/PMC7256287/)

The working-paper appendix explicitly describes three practice rounds and a
fresh 10,000-Taler endowment before the fourteen paid rounds. Its screenshot
uses six separate buy/sell/none controls and integer share amounts. The
instructions prohibit borrowing and selling more shares than held. Final
positions are liquidated; stock-type guesses and later preference tasks have
separate payoffs. See Appendix A, printed pages 12–18, especially the screenshot
on page 14 and practice reset on page 17 of the author working paper.

The CSV independently confirms the reset: all 81 participants have zero
pre-trade holdings at raw rounds 1 and 4. Interpret raw rounds 1–3 as practice
and 4–17 as main rounds 1–14. Do not silently select raw rounds 1–14.

| Audited unit | Count |
| --- | ---: |
| Distinct participants | 81 |
| Sessions / three-person peer groups | 6 / 27 |
| Participants in spt1 / spt2 | 42 / 39 |
| Raw participant-period rows, including practice | 1,377 |
| Practice participant-period rows | 243 |
| Main joint six-asset decision occasions | 1,134 |
| Main stock opportunities | 6,804 |
| Columns, including the blank-named export index | 66 |
| Duplicate participant-period keys | 0 |
| Missing trading quantities, prices or reported portfolio values | 0 |

The 6,804 stock opportunities are neither 6,804 independent people nor 6,804
independent portfolio decisions. Raw participant IDs are unique in this file;
group IDs require the session key. Every peer group contains three people and
one treatment condition. All participants share one recorded price path per
asset: there is exactly one price value per asset and raw round across all
sessions. This source does not provide thousands of independent market paths.

## Column meanings and decision timing

The pure adapter in `nmsim/human_reference.py` freezes the exact original
66-column order in `SOURCE_COLUMNS`. In particular, the CSV treatment column
is `participant._current_app_name`; the data article's abbreviated name
`participant_current_app` is not the literal CSV header.

| Source fields | Use and timing |
| --- | --- |
| `participant.code`, `session.code`, `group.id_in_subsession` | Source participant, session and peer-group keys, retained outside model state |
| `subsession.round_number` | Original 1–17 counter, with practice distinguished explicitly |
| `player.quantity_pre_a` … `_f` | Holdings immediately before that round's joint decision |
| `player.quantity_buy_a` … `_f`, `player.quantity_sell_a` … `_f` | Observed integer purchase/sale quantities at the round's recorded prices |
| `player.quantity_a` … `_f` | Holdings after the decision; used to check the accounting identity |
| `player.price_a` … `_f` | Current execution prices in Talers |
| `player.result_final` | An outcome value, **excluded from every pre-decision state** |
| Demographic, risk-task and guess fields | Retained in raw input; not automatically supplied as model state |

For each participant, reconstruct cash independently of `result_final`:

```text
cash_before = 10,000 at raw rounds 1 and 4
cash_after = cash_before + sum(sell_quantity * current_price)
                         - sum(buy_quantity * current_price)
next cash_before = cash_after, except at the practice/main reset
predecision wealth = cash_before + sum(preholdings * current_price)
```

All 8,262 stock holding-balance checks and 7,290 within-phase holding-continuity
checks pass. Reconstructed cash never becomes negative; its minimum in the
main task is 4 Talers.

Crucially, this identity holds **exactly for all 1,296 rows with a next recorded
price**:

```text
result_final[t] = cash_after[t] + sum(postholdings[t] * price[t+1])
```

That is an empirical timing result from the CSV, not an assumed definition
from the column name. Same-row `result_final` therefore leaks the next price
change if used as current wealth. The final 81 rows have no next price in the
CSV and cannot be checked by this identity. Their reported terminal wealth
must not be inverted and quietly introduced as observed input prices.

Observed main stock actions are 1,465 buys, 4,268 no-trades and 1,071 sells.
Of the no-trades, 2,005 retain a positive position and 2,263 have zero prior
holdings. No same-stock row buys and sells simultaneously. However, 418 joint
decisions buy one asset and sell another. In 49 main decisions, gross purchase
cost exceeds pre-decision cash, while net cash after contemporaneous sales
remains nonnegative. Preserve this finding: the simplified written cash-only
wording does not fully establish the implemented joint-budget validation.
The adapter checks net cash and reports those 49 cases, rather than clipping
them or treating them as failed human responses.

The data article says period-invariant data are attached to the last period.
In the CSV, age and standard demographic fields occur at raw round 1, whereas
risk/lottery/coin answers occur at raw round 17. Missing `NA` and empty strings
are preserved. Do not drop people by filtering demographics at round 17 or
fill those structural blanks with zeros.

## What can be integrated now

`decode_csv(text)` reads text in memory and preserves raw cell values.
`audit_records(records)` produces aggregate counts and accounting diagnostics
without participant identifiers. `reconstruct_decisions(records)` returns
joint six-asset states, choices and immediate settlements, with public source
keys outside the state. It rejects incomplete 17-round sequences and invalid
holdings/cash accounting. All three functions are filesystem- and network-free.
The managed caller must verify the original CSV hash: structural validity
alone cannot authenticate human data, and engineering fixtures exercise the
same functions without becoming evidence about humans.

Every reconstructed state contains pre-action cash, holdings, current prices,
recorded earlier prices and earlier own actions. Future `result_final`, final
guess answers and subsequent actions never enter those features. The output
also explicitly records that original UI observations are incomplete.

The next integration steps are:

1. Use the managed source-audit entrypoint to register the exact CSV byte hash,
   source/license metadata, aggregate audit and reconstructed decisions as
   external reference inputs. Keep honest-N at 81 people / 1,134 main joint
   choices, with practice counts separate.
2. Retain a new six-asset, integer-quantity task contract for a future matched
   Teacher comparison. Verify the missing original prehistory and ranking
   displays, purchase-lot conventions and joint-budget behavior before calling
   it a full task replay. Keep a source-state reconstruction distinct from
   an experimental recreation with altered information.
3. Freeze the comparison and holdout before any model selection. At minimum,
   whole participant histories and interacting peer groups must stay together;
   six sessions and one shared price path limit inference. Compare a
   training-only prior and simple rules, conditional sale/holding behavior,
   integer-size errors and joint allocation, with missing opportunity sets
   distinguished from chosen holds.
4. Only then acquire matching model predictions in a separately authorized
   managed run. A score would support this particular six-asset laboratory
   task, not the current information-emphasis market or real-market validity.

## Why this cannot be silently fed to the existing Student

The source has seventeen recorded price observations per asset, no twenty-day
tape, no calendar-day timestamps, no trading volume, no company financials and
no news fields. Its six-asset joint choices share a budget. Current Student
inputs require twenty-day features, additional information views and a
single-asset buy/hold/sell intensity contract. Padding prices, calling periods
days, manufacturing financial/news fields, treating omitted fields as zeros,
or splitting joint buys into independently feasible single-asset decisions
would change the task.

Selling intensity relative to own holdings could be calculated descriptively,
but a unique comparable buying intensity cannot be obtained by dividing every
stock purchase by pre-cash: the joint budget and the 49 sale-funded cases
matter. No such transformation is supplied. Original purchase-lot matching
and ranking calculation code were not included in the two-file deposit, so
no PGR/PLR replication is claimed by this adapter.

## Verification and scientific semantics

Exact executed commands in the formal worktree:

```text
node /private/tmp/lieu_pelster_source_acquire.mjs
python3 /private/tmp/lieu_pelster_source_audit.py
PYTHONPYCACHEPREFIX=/private/tmp/lieu-pelster-audit-pycache python3 -m unittest tests.test_human_reference -v
```

Acquisition: six preserved source files; both publisher-provided data hashes
matched. Independent source audit: 81 participants, 1,134 main joint choices,
6,804 stock opportunities, no accounting violations, 1,296 exact valuation
matches. Unit tests: 14 passed in 0.018 seconds. Tests use manufactured
accounting fixtures, including sale-funded purchases and future-outcome
mutations. These fixture counts are not human research results. Rendered and
visually inspected the author paper's task screenshot and practice-reset page.

This addition changes no existing prompt, Student, market mechanism, schema
or historical artifact. It adds a separately named external reference adapter
and documents its scope. No real model/provider request was made during this
source acquisition or audit. Human-likeness remains unvalidated.
