# Information-weight Teacher scale study: 2,500 x 4

## Purpose and status

`python3 -m experiments.information_weight_teacher_scale` is an additive,
Teacher-only scale successor to the preserved 50-state/200-request pilot.  It
asks whether the same four label-free observation allocations remain
behaviorally distinguishable over much broader synthetic state coverage.  It
does not train a Student, clear a market, produce a price path, or establish
human realism.

The frozen design is 2,500 complete latent market/account states x four views
= 10,000 logical requests.  The underlying state is identical within each
four-row group.  Only the numeric information allocation changes:

| Internal profile ID | Price/volume | Fundamentals | Public news | Account fields |
|---|---:|---:|---:|---:|
| `price_volume_8_2_2` | 8 | 2 | 2 | 8 |
| `fundamental_2_8_2` | 2 | 8 | 2 | 8 |
| `news_2_2_8` | 2 | 2 | 8 | 8 |
| `balanced_4_4_4` | 4 | 4 | 4 | 8 |

These IDs are internal metadata.  Neither the Provider-facing system prompt
nor user prompt says that the subject is a price/volume, fundamental, news, or
balanced trader.  The prompt contains the twelve selected numeric information
fields, the common eight account fields, neutral field semantics, and the
unchanged action contract.  `balanced_4_4_4` is an information-rich comparison
arm, not a null.

## Frozen design and identities

- Study ID: `information-weight-scale-10k-v1`
- Design seed: `20260825`
- Latent states / paired groups: `2,500`
- Views per state: `4`
- Logical requests: `10,000`
- Replicates per state x view: `K=1`
- Group order: contiguous four-row groups; profile order is cyclically rotated
  by latent-state index modulo four.
- Transport: groups are released in strict latent-state order; the four views
  within a group may run concurrently; worker limit `4`; the next group is not
  released until all four logical requests in the previous group resolve.

Exact frozen identities:

| Identity | SHA-256 |
|---|---|
| `information_weight_scale` contract | `2eab51ccfe7f0b87ab07d32715c787fff8e0c78e39432c4c8992ff01356d2ae5` |
| `state_design_hash` | `3f459bd12e3d47e83b5d6f3fc51d8c531baac17870be0aa14205c078fab3060d` |
| `sample_plan_hash` | `e75948e3d33dbcbd631bc3c2e39253be41537ed0c89814579248cf1dff4b324f` |
| first sample canary | `301fc9b3e779e8b128fe7fdbbfd171ff56de2347ed476f6a1d2a91439ca776e0` |
| last sample canary | `8f5dd3f97f1279498e94ad7380b885b41c89ab8154faf8f47790b98e71c150b5` |

The predecessor remains a separate `0.1`/pilot identity.  Its contract hash is
still `9df50ce3bcd3aadc58b9edee8f74c5190f29f8c00167569ac171539defb465cf`,
its 200-row plan hash is still
`774252b843ad63b873ce1fbc64f11ff7a9850acc9388f09a97ea1a29b20dd468`,
and the preserved live-a1 manifest SHA-256 is still
`e4ca60802f781f772703976c62410743c2c92074ca66f558ae079d0481d7c350`.
The scale study neither rewrites nor reinterprets that run.

The run also records the separately named `scientific_config_hash`,
`model_request_config_hash`, `execution_config_hash`, and
`full_effective_config_hash`.  Always report each by its exact name and run
context; none is a generic "config hash."

The credential-free serving-route identity is part of
`model_request_config_hash`, because a different route may resolve the same
requested alias to different weights.  Raw endpoint text and credentials are
not persisted.  The route is not an execution-only field in this 1.0 schema;
the predecessor 0.1 identities are historical and are not backfilled.
The required reported alias and exact-`stop` termination contract are bound to
the same model-request identity; transport concurrency, deadlines, and
application retry delays remain execution fields.

### Synthetic joint-distribution boundary

The coherent V2 generator supplies only the inherited P6/A8 price/account
core.  Each added P2, F8, and N8 field is then generated from its own
deterministic marginal Latin hypercube.  The design is **not** fitted to a
real joint company/event distribution and imposes no cross-domain accounting
or event consistency beyond each field's closed range.  Some combinations can
therefore be economically unusual.  They are broad endpoint-sensitivity
probes, not population-weighted states or estimates for real markets.

The scale design is also not nested in the predecessor's 50 latent states.  It
must not be stitched to the 200-row pilot or reported as `N=10,200`.  The
profiles operationalize information emphasis as visible field count and
composition under a fixed twelve-field budget; they are not continuous
cognitive attention weights or validated human trader types.

## Frozen request and retry envelope

Real acquisition is permitted only with all of the following:

- Provider adapter `openai`, explicit `--live`, and exact
  `--confirm-request-count 10000`;
- requested model `MiniMax-M2.7`;
- required public-safe response alias `HiggsAI` and
  `finish_reason=stop`;
- temperature `1.0`, top-p `0.95`, top-k `40`, JSON-object response format,
  and max tokens `190000`;
- SDK retry count `0`; at most five application attempts per logical request,
  with delays `10, 30, 60, 120` seconds;
- phase-inactivity and hard-request deadlines `7200` seconds and connection
  timeout `10` seconds.

Retries are physical Provider attempts, not extra scientific samples.  The
planned honest-N units must remain separate: 10,000 logical requests, between
10,000 and at most 50,000 physical application attempts, raw responses, valid
parsed decisions, resolved requests, and at most 2,500 complete four-view
latent-state groups.

## Capacity estimate before live execution

The estimate is deliberately a range, not a promise.  The predecessor live-a1
used 185,478 input tokens, 419,374 output tokens, and about 7.2 MiB for 200
requests, while strict-sequential wall time was about 85 minutes.  A naive
50x token/disk projection gives approximately:

- 9.27 million input tokens;
- 20.97 million output tokens;
- 30.24 million total reported tokens;
- about 360 MiB of run artifacts before retry or response-length variation.

With four-way within-group concurrency, the planning estimate is **20-36
hours**.  The group barrier makes each group's latency depend on its slowest
view; Provider load, VPN instability, long responses, and application retries
can extend this substantially.  Reserve at least **1 GiB** of free local disk
for the run, and more if responses approach unusual lengths.  This capacity
estimate does not alter honest-N or constitute a guarantee about the local
endpoint.

## Commands

Plan-only managed validation, with no Provider construction or network:

```bash
python3 -m experiments.information_weight_teacher_scale \
  --provider openai --model MiniMax-M2.7 --dry-run \
  --run-id information-weight-scale-10k-v1-dry-20260825-a1 \
  --out results_information_weight_scale_10k
```

Authorized real acquisition:

```bash
OPENAI_BASE_URL=http://HOST/v1 OPENAI_API_KEY=... \
python3 -m experiments.information_weight_teacher_scale \
  --provider openai --model MiniMax-M2.7 --live \
  --confirm-request-count 10000 \
  --run-id information-weight-scale-10k-live-20260825-a1 \
  --out results_information_weight_scale_10k
```

The regression suite intentionally does **not** execute the 10,000-row Fake
Teacher.  It constructs and hashes the full offline plan, runs a dry plan-only
managed attempt, and exercises the production group scheduler/acquisition path
with only two four-row groups.  Fake output remains an engineering test double,
not endpoint or human evidence.

## Artifacts, privacy, and interruption

A non-dry run writes an immutable latent-state design, exact public sample
plan, public sample JSONL, Teacher summary, and mode-`0600` private records.
Public rows may contain the exact visible observations, exact prompts, parsed
public action/intensity, response hashes, and public-safe failure codes.  Raw
responses, private rationale, raw Provider/SDK identity fields, and detailed
errors remain private.  Private rationale never enters a public artifact or a
social feed.

Every output uses exclusive creation.  The frozen live-a1 run is one managed,
immutable run and is **not resumable in place**.  An interruption preserves
the partial public/private evidence and exact logical/physical/unresolved
accounting, but the same run ID must not be reopened, appended, or overwritten.
A replacement requires an explicitly frozen successor run ID and full-plan
provenance decision; partial attempts must not be stitched together as though
they were one run.

## Scientific interpretation limits

This scale changes coverage only.  It does not change the predecessor prompt,
four allocation profiles, response parser, sampling tuple, Personas, Student,
market clearing, financing, or an existing CLI/result schema.  Source-code and
schema identities are new because this is a distinct additive protocol.

`K=1` is the central scientific limitation.  Ten thousand requests mean 2,500
paired state groups, not 10,000 independent people and not repeated sampling
of each state x view cell.  Any within-group action difference still combines
information-view sensitivity with endpoint stochasticity.  Greater state
coverage cannot estimate within-cell response variance, calibrate a human
population, establish causal attention weights, or prove that a Student will
remain faithful in closed-loop market states.  A later confirmatory design
needs repeated calls or a separately justified noise model, human anchors,
true null/sham controls, holdout and rollout evaluation, and preregistered
estimands before making those claims.

The balanced arm is information rich rather than a true null or sham.  The
four views of a latent state share a concurrent endpoint window, which reduces
slow temporal drift but may introduce common load-related response noise.
The requested model name and the SDK-reported alias remain separate evidence;
`HiggsAI` does not independently verify the underlying serving weights.
