# Multi-event paired-workers2 execution protocol

## Status and scope

This document describes the separately versioned acquisition protocol in
[`experiments/multi_event_protocol_workers2.json`](../experiments/multi_event_protocol_workers2.json).
Its exact frozen SHA-256 is
`245be765444b5f93b86d5d05c95e81c7eee13b127180739c88a966eca3371d4a`.
The protocol is a preregistered execution-acceleration pilot, not a
confirmatory experiment and not an automatic replacement for the historical
workers=1 study.

The workers=2 profile preserves the three events, two social arms, eight
scientific seeds, three repeat indices, prompts, model request, strict decision
schema, health threshold, and five-attempt full-stage cap from the workers=1
protocol. The changed mechanism is acquisition: the driver may have two child
processes in flight, but only as the two arms of one predeclared
event/seed/repeat pair. Endpoint load, acquisition timing, failure
distribution, and real-provider responses can therefore change. No
determinism claim is made.

## Exact profile identity and durable roots

The central protocol-profile registry binds all of these values together:

- protocol schema `multi_event_protocol_v2`;
- protocol id `multi_event_distribution_workers2_v1`;
- the exact protocol path and SHA-256 above;
- exactly `workers=2`;
- canonical live root `results_multi_event_workers2`;
- the paired-arm barrier launch policy and execution-acceleration contract.

An analyzer or driver must resolve that complete profile from the protocol
bytes. Mixing a path, hash, root, worker count, launch policy, or stage contract
from another profile is an input error. Live work uses the lexical,
non-symlink canonical root and the clean Git commit that last changed this
exact protocol path. The source snapshot records that repository-relative path
and is independently rechecked by the analyzer.

`results_multi_event` remains the workers=1 root.
`results_multi_event_workers2` is the workers=2 root. Both roots, including
failed attempts and canary artifacts, are long-lived research records. They
must not be overwritten, renamed into one another, deleted to reset an attempt
budget, or combined through a compatibility symlink.

## Paired launch policy

The full 144-slot canonical order retains the workers=1 counterbalancing:
repeat position first, then seed position, with rotated event order and
balanced arm-first parity. In the workers=2 profile, each adjacent
`social_on`/`social_off` pair for one event, seed, and repeat is one scheduling
unit:

1. submit only the two jobs in the current canonical pair;
2. permit at most those two child processes in flight;
3. wait until both jobs are terminal before submitting the next pair;
4. preserve the canonical within-pair submission order;
5. make no claim about OS/provider start order or completion order;
6. on resume, filter already terminal or ineligible slots without reordering
   the remaining canonical pairs.

This is a barrier, not a general two-worker queue. A self-consistent plan that
interleaves pairs, advances after only one terminal child, changes the
counterbalanced ordinal, or declares a different scheduling policy is
ineligible.

## Canary and full stages

Stage identity is part of the parent, plan, selection, ledger, and summary
contract. It is not inferred from how many files happen to exist.

The `canary` stage contains exactly the first adjacent two-arm pair in the
frozen canonical launch plan: two slots total, with at most one technical
attempt per slot. No later slot may be launched by a canary parent. A canary
may be analyzed to produce honest, incomplete descriptive diagnostics, but it
cannot claim a complete grid, a preregistered realism result, a complete-case
N=8/K=3 estimand, a variance-component result, or promotion by itself.

Promotion to `full` requires explicit approval and every preregistered canary
gate below to pass. A full parent uses the same canonical root and identities.
It reuses an accepted canary child, preserves a rejected `ta1`, and resumes
that slot at `ta2`; it never resets the five-attempt full-stage series cap.
The full stage accounts for all 144 slots and preserves every prior canary
artifact.

For offline engineering acceptance only, explicit mock `--n`/`--k` overrides
make `full` cover every slot in that smaller paired grid. Such a run remains
protocol-nonadherent and cannot support a live or realism claim.

## Frozen canary promotion gate

Missing, non-finite, unregistered, or integrity-invalid evidence fails the
gate. All numeric conditions must pass:

- at least two terminal children;
- combined logical-request throughput at least `0.6` requests/second;
- provider-exception fraction over visible application attempts at most
  `0.10`;
- type-7 p95 latency among non-exception visible provider attempts at most
  `50,000 ms`;
- pooled terminal bad fraction at most `0.25`.

Accepted slots per parent wall hour is reported but is not a promotion
criterion for a two-child canary. If the gate fails, the full stage is not
launched, all canary artifacts remain preserved, and workers=1 remains the
operational default. Workers=3 or workers=4 require a new reviewed protocol;
there is no result-driven automatic escalation.

The gate does not authorize changing N, K, `health_bad_frac_max=0.15`, the
full-stage five-attempt cap, prompts, parser, model request, or worker count.
Thresholds and sample extent are not relaxed after observing the canary.

## Workers=1 control and no-pooling rule

The registered control is the separately preserved workers=1 protocol
[`experiments/multi_event_protocol.json`](../experiments/multi_event_protocol.json),
SHA-256
`f5ff63c16ca8393b8f801ce52c2ba66455c3c3aef384b38e654592fb4888987e`,
with parent `wave1-t2-live-20260722-a1`. It is an external acquisition-regime
control, not a child or resume source for workers=2.

Workers=1 and workers=2 children are never pooled into one primary estimate,
variance component, trajectory envelope, complete-case N, retry series, or
model-identity claim. Comparisons between their separately produced summaries
are explicitly acquisition-regime diagnostics. The historical control is also
time-separated, so any observed difference may include endpoint/time drift;
it does not isolate concurrency causally.

## Analyzer semantics

`experiments.aggregate_multi_event` remains Provider-free and uses the same
entrypoint id for both allowlisted profiles. It derives its input from one
finished parent manifest and validates that profile's exact protocol, root,
source snapshot, worker count, launch policy, execution stage, registered
artifacts, child identities, and health evidence. It never discovers or
combines another result root.

A complete full-stage workers=2 summary remains descriptive and
non-confirmatory. A canary summary is additionally marked incomplete and
claim-ineligible. Neither stage changes the scientific N/K of the workers=1
study, and neither licenses a model-specific claim from an endpoint-reported
alias.

## Scientific-semantics declaration

The simulator's event grid and stated social estimand are unchanged. The
execution mechanism changes from sequential acquisition to paired concurrent
acquisition, which may change endpoint contention, request timing, failure
patterns, and stochastic outputs. This is a real acquisition-regime change,
not proof of equivalent samples and not full determinism. Private rationale
remains private; only public statements and explicitly public sentiment may
propagate.
