# Human task alignment: source reconstruction and remaining gaps

Audit date: 2026-09-09. This is an external-source diagnostic, not a model
evaluation. Model requests: 0. No human-likeness score is reported.

The Liêu–Pelster main-task price series is substantially more recoverable than
the original source audit established. The four plotted observations labelled
−3, −2, −1 and 0 are the prices already present in CSV raw rounds 1–4. An
independent terminal-wealth calculation also identifies the final price vector
and matches the paper figure. The remaining barrier to exact task replay is
the original display and validation implementation, especially peer-ranking
timing. This distinction matters: arithmetic recovery proves a price series,
not the precise screen a participant saw.

## Sources inspected and preserved

The original CSV, RData, data-article XML and author working paper retain the
hashes in [HUMAN_REFERENCE_LIEU_PELSTER.md](HUMAN_REFERENCE_LIEU_PELSTER.md).
Additional evidence is create-only under:

```text
results_human_reference_inputs/human_task_alignment_20260909/
```

These public inputs are ignored by Git. They are source materials, not new
research runs or reusable managed children.

| Primary source | What it establishes |
| --- | --- |
| [OSF preregistration 4nrmx](https://api.osf.io/v2/registrations/4nrmx/) | Registration of the framing experiment, 81 planned students, original instructions attachment. The correct API type is `registrations`; `/nodes/4nrmx/` returns 404. |
| [AEA trial 4304](https://www.socialscienceregistry.org/trials/4304) | Links public data to OSF 348yz; explicitly lists “Program Files: No”. |
| [OSF data deposit 348yz](https://api.osf.io/v2/nodes/348yz/files/osfstorage/) | Three files: the same CSV and RData as Mendeley, plus an instructions PDF absent from the two-file Mendeley listing. No pagination remains. |
| [Original instructions PDF](https://osf.io/download/ntqdu/) | Six separate integer buy/sell/none choices; own cash and holdings; treatment rules; practice reset. This is an English attachment, not recovered production oTree source. |
| [Author working paper](https://en.wiwi.uni-paderborn.de/fileadmin-wiwi/cetar/TAF_Working_Paper_Series/TAF_WP_043_MinhLyPelster_2019.pdf) | Figure 1 contains recoverable vector price paths; printed pages 12–17 describe the task. |

The instructions PDF is 217,520 bytes. Its byte SHA-256 is
`7bb5d4aa37e75649d6b36e1e228f37bfd737a92d05d6359c5e5f1922a4a8b554`, matching
both the OSF deposit and archived preregistration metadata. The OSF data
listing independently reports the same CSV and RData SHA-256 values already
verified from Mendeley. The preregistration archive contains only the same
instructions PDF. Its original project `zkyqm` returns HTTP 410.

`source_inventory.json` records URLs, bytes and byte hashes of the seven
downloaded source documents. Do not extend the Mendeley CSV's CC BY licence
automatically to every paper or instructions document.

## Price history and the apparent missing prehistory

Figure 1 is on PDF page 21, printed page 19. Its `/Im2` Form XObject has six
19-vertex stroked paths. The first 18 x positions carry labels −3 through 14;
the nineteenth position is plotted without an x-axis label. The chart was
also rendered and visually inspected. The archived task screenshot does not
contain its price-history panel, so Figure 1 is not itself an archived UI
screen.

The diagnostic extracts the paths and converts y coordinates using the
printed 40- and 160-Taler tick positions. Every first-17 decoded price agrees
with the CSV at the same ordinal position: **102/102 matches**. The greatest
distance to the nearest integer is 0.002049 Talers, consistent with the
two-decimal PDF coordinates. No curve fitting to model decisions is involved.

| Figure position | A | B | C | D | E | F | CSV relationship |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| −3 | 88 | 92 | 151 | 129 | 65 | 78 | Raw round 1, practice |
| −2 | 93 | 87 | 156 | 134 | 70 | 73 | Raw round 2, practice |
| −1 | 88 | 82 | 161 | 129 | 65 | 78 | Raw round 3, practice |
| 0 | 91 | 85 | 164 | 126 | 62 | 75 | Raw round 4, first main decision |
| 13 | 114 | 98 | 173 | 117 | 51 | 90 | Raw round 17, last main decision |
| 14 | 117 | 101 | 176 | 120 | 48 | 93 | Terminal valuation, independently verified below |
| Unlabelled next point | 112 | 96 | 181 | 115 | 43 | 98 | No matching CSV decision; not used as a state |

The source descriptions call the paid decisions periods 1–14. The figure
therefore indexes the corresponding decision-price observations as 0–13.
Retain both clocks explicitly: `raw_round`, `main_round = raw_round - 3`, and
`figure_price_index = raw_round - 4`. Do not rename raw round 4 to practice,
drop it, or move the account reset to raw round 5 to make the labels agree.

The hypothesis that the first main decision already has four known price
observations, raw rounds 1–4, is consistent with all recovered evidence. It is
no longer defensible to assert that four entirely separate numerical price
points before CSV raw round 1 must be missing from the main phase. What
remains unverified is whether all those points were retained on the actual
main chart, how its time labels were rendered, and whether any still-earlier
practice display existed. Personal exposure to the three practice prices is
known; persistent main-screen display of them is a different assertion.

### Terminal valuation check

For each of the 81 participants, reconstruct cash from the recorded trades
and the 10,000-Taler reset, then form the outcome-only equation:

```text
sum(final_holdings[asset] * terminal_price[asset])
    = raw_round_17.result_final - cash_after_raw_round_17
```

Exact rational Gaussian elimination yields rank 6 and a unique solution:
`A=117, B=101, C=176, D=120, E=48, F=93` Talers. All 81 equations have zero
residual, and this solution matches Figure 1's point at axis label 14. The
extra nineteenth plotted point does not: only 24 of 81 equations match it.

Together with the earlier audit's 1,296 next-price valuation matches, this
reconciles all **1,377 reported wealth values**, including practice, without
using a reported future value as a pre-decision feature. This is a diagnostic
identity, not permission to expose terminal prices early or treat the
nineteenth plotted point as an additional decision round.

Receipts: `figure_price_diagnostic.json` and
`terminal_valuation_diagnostic.json`. Neither contains participant identifiers.

## Rankings: known rule versus unrecovered implementation

In `spt1`, ranking uses successful sales divided by initiated purchases; the
working paper also says portfolio value is shown. The instructions' partial
sale example counts as a successful trade. They do not define how multiple
purchase lots, repeated partial sales, equal prices, zero purchases or ties
are treated. This is not a verified FIFO, LIFO or average-cost rule.

In `spt2`, ranking uses cash plus the stock portfolio value. The same two
peers persist. Rankings follow main rounds 5, 10 and 14. They have no direct
payment effect. These general rules are available, but no archived ranking
screen or production ranking code was found in the primary deposits.

For the 39 `spt2` participants in 13 groups, peer balances can be calculated
at either current execution prices or the next prices used by `result_final`.
The two choices are not equivalent:

| Ranking after | Raw round | Groups with changed relative order under the two price timings | Tie groups at current / next prices |
| --- | ---: | ---: | ---: |
| Main round 5 | 8 | 5 / 13 | 0 / 0 |
| Main round 10 | 13 | 6 / 13 | 1 / 0 |

The absence of ties under next-price valuation does not prove that it was the
display rule. Nor can stable order in another group justify claiming a full
screen match: the displayed portfolio values may still differ. These counts
are recorded in the terminal-valuation diagnostic, not chosen after seeing
any model performance.

The earliest preceding ranking may enter a main-round-6 state only after its
release has been established. Same-round `result_final` must never be used
as pre-action wealth simply because it is convenient for the ranking.

## Joint budget and purchase lots

All observed share positions are integer and nonnegative. Cash reconstructed
from net purchases and sales is also nonnegative. However, 49 main joint
choices spend more than start-of-round cash while contemporaneous sales fund
the difference. This includes 4 of the 195 `spt2` first-five-round choices and
5 of the 210 corresponding `spt1` choices. Raw main round 1 has no such cases.

The screenshot and wording state that purchases cannot exceed current cash,
and sales immediately credit cash. This does not distinguish implementation
as sales-first execution, net-budget validation, a dynamically updated UI or
another equivalent mechanism. The six-asset adapter must preserve observed
choices and label its net-budget check; it must not reject those choices or
silently claim to have recovered the production validator. Removing the
sale-funded rows would condition evaluation on the human action and alter
the population being compared.

Lot matching is needed for an exact `spt1` ranking and published PGR/PLR
replication. It is not needed to reconstruct cash, integer holdings, execution
prices, or a `spt2` wealth calculation. Purchase-size conventions and
purchase-lot attribution are separate issues.

## Feasible matched-task evaluation

The immediately usable contract remains **one joint six-asset decision**, in
Talers and integer shares, with the full shared budget and own history. This
source cannot become a single-stock intensity label without changing the
task. It also has exogenous prices, so it cannot establish that agents create
human-like endogenous market prices.

There is currently no independently certified full-UI replay subset. There
are useful, precisely bounded source-task comparisons that can be prepared:

1. **First main decision:** 81 people, two framing conditions (42/39), no
   preceding ranking, cash 10,000 and zero holdings. Provide the four known
   price observations and original economic instructions. This minimizes
   budget and ranking ambiguity, but there is only one common market state
   per frame. Retain the practice exposure and avoid claiming 81 independent
   market states or tests of selling behaviour.
2. **First five main decisions:** raw rounds 4–8, 405 joint choices from 81
   people. The `spt2` slice has 195 choices, 39 people and 13 peer groups.
   No realized ranking has yet affected these choices, though the announced
   future ranking rule may already affect decisions. This is the strongest
   near-term conditional decision benchmark after explicitly freezing the
   chart reconstruction and budget interpretation. Call it a reconstructed
   information comparison, not byte-identical UI replication.
3. **All `spt2` main decisions:** 546 choices from 39 people. Cash, holdings,
   price sequence and candidate peer values are reconstructible. Exact
   ranking release valuation and the intervening stock-type guess screen
   still require evidence before making a full same-task claim. A first-five
   score must not be extended to this population automatically.
4. **All `spt1` main decisions:** 588 choices from 42 people. Add lot matching,
   successful-trade accounting and ranking display as unresolved inputs.

For development, compare a participant/group-held-out prior and simple rules
against joint allocations, trade/no-trade probabilities, integer quantities,
and conditional selling with a strictly defined opportunity set. Keep whole
peer groups and participant histories together; use session-level sensitivity
because there are only six sessions and one shared exogenous price path.
Do not label these controls as evidence that a model is human-like. Freeze
model selection and the holdout before asking a Teacher on these states.

The early-window implementation carries source-state reconstruction, missing-display
flags and the first-block scope explicitly. It does not turn all
`observation_completeness` booleans true merely because the numerical price
series now reconciles. If a reconstructed screen is implemented, its economic
assumptions must be declared in a new task identity, with the source
comparison kept separate from claims about exact original UI exposure.

### Pure early-window adapter added

`nmsim/human_early_tasks.py` adds these filesystem- and network-free APIs:

- `build_early_tasks(records)` selects only `spt2` paid rounds 1–5. It returns
  task IDs, source origin, explicit protocol assumptions, state, prompt and a
  separate human label. Source subject/group/session IDs and the current human
  choice never enter the prompt. Earlier own actions are supplied as
  reconstructed memory, separately labelled for practice and main phases.
- `parse_joint_response(raw, task)` requires exactly a six-asset
  `signed_quantities` object and a string `private_reasoning`. Positive, zero
  and negative integers mean buy, no trade and sell. It rejects duplicate JSON
  keys, extra fields, non-integers, short sales and a negative joint net cash
  balance. Its `public_decision` and `private_rationale` are separate fields.
- `evaluate_joint_predictions(tasks, predictions)` accepts at most one raw
  response per task. Missing and invalid responses remain explicit. Agreement
  metrics use the paired-valid denominator and never turn failures into holds.
  It reports counts, exact six-asset agreement, asset action agreement and
  absolute signed-share error. It has no pass threshold or human-validity flag
  that can become true from agreement alone.
- `hold_baseline_predictions(tasks)` supplies an explicit deterministic
  no-trade null. Its outputs are not Teacher samples.

New protocol identity:
`spt2-main-1-through-5-published-clock-net-budget/0.1`. Task IDs namespace source
origins under this protocol; they do not authenticate source bytes. An official
caller must verify the CSV SHA, register the task/protocol/component identity,
and use `ManagedRunContext`. These pure functions are not a new official CLI.

A read-only diagnostic verified the exact original CSV byte SHA before
building tasks: **195 tasks, 39 people, 13 groups, 1,170 stock opportunities**.
The deterministic hold control produced 195 feasible submissions with 25 exact
joint matches, 666/1,170 stock-action matches (56.923%), and mean absolute
signed-share error 2.296581. Human actions in this window are 386 buys, 666
holds and 118 sells. These are descriptive null results, not model outputs or
a human-validity threshold. This diagnostic creates no managed research run.

The addition makes a new prompt and explicitly assumed joint-budget contract.
It changes no existing prompt or market. It must not be described as an exact
recovery of the original production UI or silently substituted into the
single-asset Student benchmark.

### Cross-task label protection

Each individual prompt excludes its current human label. The complete
prompt bank is nevertheless label-bearing: the next task's legitimate
`own_prior_trades` reveals the preceding task's human choice. In the verified
195-task bank, this directly recovers **156 earlier task labels**. Removing
only the top-level `human_action` key is insufficient. Earlier human history
must remain available for the individual conditional task; deleting it would
change the scientific comparison.

The managed `human-early` export therefore keeps both
`private_human_early_tasks.json` and `private_human_early_prompts.json` at mode
0600. `human_early_catalog.json` publishes only the task IDs, protocol,
counts, task-bank hash and acquisition policy. It contains no trajectory,
prompt, source origin or individual label. New runs no longer create a
public `human_early_prompts.json`; historical artifacts are not rewritten.
Task IDs remain pseudonymous source-linked identifiers, not a claim of
irreversible anonymisation.

Future acquisition must supply only the current task in an independent
request context. It must not send the complete bank, later histories, gold
labels or a conversation that has already seen later tasks. This restriction
also applies when producing a batch payload: each model context must remain
independent. The managed integration regression test demonstrates the
cross-task recovery inside the private bank, verifies 0600 modes and checks
that the public catalog lacks the revealing fields.

## Endogenous-market alternative search

[Henning et al., arXiv 2502.15800v3](https://arxiv.org/html/2502.15800v3)
describes 19 human markets and a single-asset call market with instructions
and prompts in appendices. Its reproducibility statement promises code with
the camera-ready submission. The inspected version does not link a public
raw-data archive or working code deposit. Exact-title and identifier searches
did not locate an author-linked repository. OpenReview's forum required a
browser challenge and its notes API returned 403, so any later release there
is unverified. This is a strong candidate task, not a dataset acquired here.

A concrete public-data lead is **Holt, Porzio and Song (2017), Price Bubbles,
Gender, and Expectations in Experimental Asset Markets**. The institutional
deposit has DOI [10.18130/V3-YKX5-H224](https://doi.org/10.18130/V3-YKX5-H224).
Its [DataCite metadata](https://api.datacite.org/dois/10.18130/v3-ykx5-h224)
identifies Charles Holt, University of Virginia, an issue date of 2019-08-19
and CC BY 4.0 rights. This metadata has been downloaded and hashed.

The author's [data-appendix readme](https://libraopen.lib.virginia.edu/downloads/8w32r569d)
is searchable and describes three Excel files: 16 main 25-round sessions,
10 flat-value 15-round checks, and 10 declining-value checks. It documents
per-person bid/offer prices and quantities, realized trades, cash, holdings,
dividends, interest and forecasts. The principal flat fundamental value is
28, not the Henning task's 14. It therefore offers a more promising eventual
endogenous-market reference than repurposing Liêu–Pelster.

Actual Libra downloads returned HTTP 202 with zero-byte bodies in two fresh
probes. **No usable Excel files were acquired or audited**, no file inventory
or task completeness is claimed, and no count from the readme is reported as
a locally verified human dataset. Preserve this as a specific next acquisition
target, not an excuse to manufacture observations. See
`alternative_source_inventory.json` for the HTTP probes and metadata hash.

## Commands and scope

Executed in or explicitly targeting the `agent-market-loop` worktree:

```text
node /private/tmp/human_task_alignment_acquire.mjs
pdftotext -layout results_human_reference_inputs/human_task_alignment_20260909/experimental_instructions_osf.pdf -
pdftoppm -f 21 -l 21 -r 140 -singlefile -png results_human_reference_inputs/lieu_pelster_v1/327f7c512733fffe0efb8ee83944cefb4320ab538a930b920b5cdd25595ac333/author_working_paper_2019.pdf /private/tmp/human_task_alignment_fig1
pdftoppm -f 3 -l 3 -r 100 -singlefile -png results_human_reference_inputs/human_task_alignment_20260909/experimental_instructions_osf.pdf /private/tmp/human_task_alignment_osf_ui
PYTHONDONTWRITEBYTECODE=1 python3 /private/tmp/human_task_alignment_figure_audit.py
PYTHONDONTWRITEBYTECODE=1 python3 /private/tmp/human_task_alignment_terminal_audit.py
node /private/tmp/human_task_alignment_alternative_acquire.mjs
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_human_reference tests.test_human_early_tasks -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_information_diagnostics -v
git diff --check -- docs/HUMAN_TASK_ALIGNMENT.md
git diff --no-index --check /dev/null nmsim/human_early_tasks.py
git diff --no-index --check /dev/null tests/test_human_early_tasks.py
git diff --no-index --check /dev/null docs/HUMAN_TASK_ALIGNMENT.md
```

Results: source acquisition passed, OSF instructions publisher hash matched,
102/102 figure/CSV price matches, 81/81 exact terminal-wealth matches, and
both relevant PDF pages visually checked. All diagnostics are read-only with
respect to raw sources. The new module's 13 fixture tests cover phase/clock
selection, missing display flags, current-label/future-value exclusion,
source-ID exclusion, budget constraints, strict parsing, private text separation,
the null control and missing/failed prediction denominators. The combined
source-adapter/early-task suite passed **27 tests in 0.036 seconds**.
The managed integration suite passed **4 tests in 0.400 seconds**, including
the private-bank cross-task leakage regression and a network-forbidden
`human-early` invocation on synthetic input. No genuine research artifact was
created by these tests. No-index
whitespace checks reported no whitespace diagnostics (exit 1 denotes the new
file content difference from `/dev/null`). No production
dependencies, existing prompts, Student
parameters, market rules, source schemas or historical outputs changed. No
live Provider call, commit or contact with researchers occurred in this task.
