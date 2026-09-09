# Agent market: delivery and evidence ledger

## Objective

Implement a research system in which bounded-information agents make decisions,
trade with one another, retain their own account histories, and generate the
next market state. Establish separately whether Teacher behavior matches human
choices, whether a small Student preserves that behavior, and whether the
resulting market remains valid under controls. User authorization: 2026-09-09,
“按照你的想法 全部做完吧 就是agent模拟市场这件事情”.

This goal is not satisfied by a working simulator alone. In particular, human
validity must remain **unestablished** until observed same-task human choices
support it. No synthetic or LLM response may be counted as a human participant.
The project is research software; brokerage, real money, RL, and a full limit
order book remain outside this implementation.

## Starting evidence (read-only preflight)

- Source worktree: `.worktrees/v2-teacher-pilot`, commit
  `59b668eaf105bab2f1e7c03dc5c93823baf2dcc3`; tracked tree clean.
- Implementation worktree: `.worktrees/agent-market-loop`, branch
  `feat/agent-market-loop`, created from that exact commit.
- No information-weight, V2, or multi-event experiment process was found in
  the preflight process inventory.
- Historical 10k acquisition: 10,000 logical requests; 9,918 valid decisions;
  2,500 latent groups; 2,478 complete four-view groups; K=1. It did not train
  a Student or run a market. These are endpoint observations, not people.
- Historical a10: 54 aggregate states (162 responses); fixed MLP frozen-test
  action CE 1.021521, linear 0.502385, prior 0.625329. Test has nine states.
  The historical test is development evidence, not future model selection.
- Historical files and the original worktree must remain unchanged. A new
  training run uses a verified, hashed historical input, not a resumed child.

## Required delivery and current status

| Requirement | Evidence required | Status |
|---|---|---|
| Preserve historical evidence | Original manifests, artifact hashes, modes and mtimes unchanged; verified backup | local archives and restore verified; second medium pending |
| Train from 10k public data | Verified input, grouped split, masked input, prior/linear/MLP, validation-only selection, final test report | completed development run; MLP selected, test CE 0.382743 |
| Continuous information world | Coherent company ratios, timestamped news, endogenous price/volume, public information views | available_only successor removes unobserved range/undefined turnover; new mask patterns still need fidelity checks |
| Interacting agents | Private account/trade history, finite resources, order/fill distinction, atomic conserving settlement | 46 market runs, 2,760 rounds, 600,000 decisions including sizing controls; conservation passed |
| Separate order-rule effects | Independent vs intensity-linked quote treatment plus no-state controls under the same accounting | implemented and run; large rule sensitivity observed |
| Closed-loop fidelity | Rollout coverage and audited Teacher probes; unchanged held-out evaluation | 24-state/K=5 plan frozen from 72,000 agent-rounds; real 120-request acquisition now running in isolated b95aca4 worktree; not yet a completed result |
| Human benchmark | Neutral tasks, anonymous response schema, scoring, actual same-task human choices | 81 published humans audited; reconstructed pre-ranking subset of 39 people/195 joint tasks exported privately, no Teacher comparison yet; own 24-task responses absent |
| Sizing fidelity | Frozen action model, exact-support laws, validation-only selection and paired mean/sample controls | completed offline fit and 12 paired markets; 63 actual closed-position events in conditional sampled arm vs 0 for its mean; no human-validity claim |
| Empirical market validation | Matched time/institution/data, whole-market replicates, controls, uncertainty | pending |
| Social mechanism | Public-only messages and no-social/sham controls, if needed after the basic loop | pending |
| Scale and operation | 200/1,000-agent timings, progress, immutable outputs, failure evidence | 200 and 1,000 run timings recorded; durable progress/report available; always-open dashboard pending |
| Shareable review | Current run report, model/data provenance, scientific changes, commands/tests, GitHub branch | code/report pushed; Draft PR #13 open against feat/v2-teacher-pilot |
| Composition inference | Known-composition recovery under nuisance changes before real-market inference | deferred until identifiable |

## Implementation decisions

These are explicitly new development mechanisms and use new schemas. Existing
V1/V2 prompts, schemas, defaults, a10 and 10k identities are not rewritten.

1. Reuse the integer-cash/share V2 auction and settlement. One round is one
   daily call auction. No claim to continuous intraday execution.
2. Use a shared Student with visible values, visibility masks, and account
   variables. Information-allocation labels are grouping metadata only.
3. Split all four views of a latent state together before filtering failures.
   Select among candidates with validation data only. No accuracy or human
   similarity threshold will be invented after looking at test results.
4. K=1 labels permit supervised prediction, not a measured per-state Teacher
   distribution. Training performance is separate from human validity.
5. World variables must have an effect path; news must not arrive before its
   event; price-dependent company ratios must be recomputed from current price.
   Any daily-only proxy or domain clamp is explicit in the new schema/report.
6. Decision intensity and quote aggression must be separable treatments.
   A no-state baseline shares market rules and resource constraints.
7. All official runs cross the central registry and ManagedRunContext. Pure
   libraries and tests have no provider, filesystem, or run-lifecycle effects.
8. First use existing data offline. Later Teacher acquisition is targeted at
   repeated diagnostic states and rollout gaps, not arbitrary bulk expansion.

## Success assessment

Maintain three separate claims: human–Teacher agreement, Teacher–Student
agreement, and market-level agreement. A failure of any claim remains visible.
Human recruitment, observed choices, and empirical validation are external
evidence requirements, not values that code can manufacture. Progress on the
software does not change their status.

Every implementation stage must record exact commands, observed results,
scientific changes, compatibility, and remaining work. This ledger must remain
open until the requested research system and its evidence have been verified.

## Next continuation priorities

1. Inspect the same live handle/run `rollout-fidelity-live-20260909-a1` until it
   finishes; never restart based on a polling timeout. Higgs is connected and
   the existing watchdog now recognizes this exact run. The plan is unchanged.
   These probes concern old mean-Student rollout states, not automatic coverage
   of the newly sampled sizing trajectories.
2. Complete same-task human evidence. The early six-asset task bank has 195
   joint decisions from 39 published participants, with original-display gaps
   explicitly labelled. Keep the complete prompt bank private because later
   own histories disclose earlier gold; acquire in independent single-task
   contexts. It is not automatically compatible with the single-asset benchmark.
3. Assess the completed sizing mechanism against the final Teacher probe data
   and any newly reached states. Both conditional and feature-blind distributions
   now have mean/sample controls; original floor, action model and test remain
   unchanged. Market prices still fall about 50% on average. Do not choose a
   preferred price curve or call full-sell intent an executed liquidation.
4. Improve researcher-facing monitoring and compile empirical market
   comparisons, then add independently controlled social information.

Actual results, hashes and boundaries: [2026-09-09 report](AGENT_MARKET_RESULTS_20260909.md).
Second-stage evidence: [validation continuation](AGENT_MARKET_VALIDATION_20260909.md).
Sizing and matched-task preparation: [2026-09-09 sizing report](AGENT_MARKET_SIZING_20260909.md).

GitHub review: [PR #13](https://github.com/b25805634-cyber/Agent-based-simulation-of-financial-markets/pull/13).
The new branch is `feat/agent-market-loop`. Main has not been merged. Local
model/data/ledger files and private archives are not uploaded with this code PR.
