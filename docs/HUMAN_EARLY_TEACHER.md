# Frozen Teacher comparison on the early six-asset human task

This new experiment compares one Teacher response per reconstructed human
decision with the recorded choice and a deterministic no-trade control. It
does not train or select a model. K=1 cannot estimate a Teacher distribution
at each state, and repeated model calls are not new human participants.
Implementation and tests do not authorize or perform a real endpoint run.

## Source and scientific scope

The only source for this version is the completed managed `human-early` task
export, pinned by manifest byte SHA-256:

```text
results_information_diagnostics/runs/human-early-tasks-20260909-a1
5f7ffcbede579c520756f92570b6e79de3d73fc97ff146378829f692be920286
```

All registered source artifacts, private modes and the canonical task-bank
hash are verified. The plan requires 195 tasks: 39 historical people in 13
three-person groups, each contributing paid rounds 1–5 in condition `spt2`.
Each task is one six-asset joint allocation, not six independent people or
six independently feasible buys. There are 1,170 stock opportunities. No new
human participation or market simulation occurs.

Prices are exogenous. The published clock is reconstructed as source raw
round minus four. The current-price, cash, holdings and personal history
come from the frozen source task. The original UI retention of practice
prices and display of memory remain unverified. Net sale proceeds may fund
same-submission purchases in this explicit reconstructed contract. Four
human choices in this early `spt2` window require that interpretation.
It is not a claim that the original validator has been recovered. Full source
evidence and remaining gaps are in [HUMAN_TASK_ALIGNMENT.md](HUMAN_TASK_ALIGNMENT.md).

The source study and materials are [Liêu–Pelster raw data](https://data.mendeley.com/datasets/jfg8s32xdm/1),
[OSF experimental instructions](https://osf.io/download/ntqdu/), and
[AEA trial 4304](https://www.socialscienceregistry.org/trials/4304).

## Frozen design before acquisition

Protocol: `human-early-teacher-k1-shuffled-contexts/0.1`.

- Exactly 195 tasks and K=1. There is no replicate, sample-count, prompt,
  training or tuning flag.
- Request order is ascending SHA-256 of canonical JSON
  `["human-early-teacher-k1-shuffled-contexts/0.1", 20260909, task_id]`, using
  `task_id` only to break a hash tie. This is an explicitly new acquisition
  design, avoiding five consecutive same-person requests being systematically
  associated with endpoint time. It does not alter the original task prompt.
- Record every request index, task ID, exact UTF-8 prompt hash and canonical
  state hash. Also record the numbers of distinct prompt and state hashes;
  195 person-decisions must not be reported as 195 distinct market states.
- Keep the existing task prompt verbatim. A constant system message requests
  completion of that task in its required JSON format.
- Supply one current task per independent request context. No cache, shared
  conversation, full-bank prompt, later task history, current human label,
  source participant identity or source group identity enters a request.
  Legitimate own earlier decisions remain in that one task's history. They
  are human-history conditioning, not a Teacher-generated closed-loop rollout.
- Freeze this document and all prompt, planning, parser, artifact-validation,
  entrypoint and reused transport component hashes in the plan. Acquisition
  rejects changed components or a changed shared request contract. A changed
  study requires a separately reviewed plan; no historical plan is rewritten.

Real transport settings are fixed from `experiments.rollout_fidelity.REQUEST`:

| Field | Frozen value |
| --- | --- |
| Requested model | `MiniMax-M2.7` |
| Required response model alias | `HiggsAI` |
| Temperature / top_p / top_k | `1.0 / 0.95 / 40` |
| max_tokens | `190000` |
| response_format | `{"type":"json_object"}` |
| Required finish_reason | `stop` |
| Application attempts / SDK retries | `1 / 0` |
| Request timeout / hard deadline | `7200 / 7200` seconds |
| Provider seed / cache | None sent / disabled |

The default is `fake_test_teacher`, which returns six zero trades and marks
its rationale as a fake null. Default workers=2, permitted range 1–4. This
changes execution concurrency, not task membership or order. The shared
batch helper drains all owned requests before propagating a callback error.
The transport creates a fresh two-message request for every task, without
carrying earlier responses into later requests.

## Managed lifecycle and private outputs

`python3 -m experiments.human_early_teacher` has `--task plan` and `--task
acquire`. The former verifies the source and creates a frozen plan. The
latter requires the completed plan run plus its explicit manifest SHA. All
official invocations use the central entrypoint registry and
`ManagedRunContext`. `--help` and `--version` create no run. `--dry-run`
creates a managed plan-only artifact and constructs no Provider, including
when `--provider openai` is selected.

Real acquisition requires all of `--task acquire --provider openai --live
--confirm-request-count 195`. An absent, wrong or additional count is rejected
before Provider construction. A plan cannot acquire; a dry-run cannot acquire.
Selection overrides, resume and replacement samples are not available.

| Artifact | Visibility and purpose |
| --- | --- |
| `private_teacher_plan.json` | 0600, frozen complete task bank with human labels and histories |
| `teacher_plan.json` | Public hashes, request schedule, units and protocol only; no prompt, history or labels |
| `private_teacher_raw.jsonl` | 0600, secret-redacted completion/SDK envelope, received-text SHA and private operational errors |
| `private_teacher_decisions.jsonl` | 0600, per-task gate outcomes, parsed allocations and private rationale |
| `private_teacher_attempts.jsonl` | 0600, attempt callbacks, including raw private transport errors |
| `private_comparison.json` | 0600, descriptive comparison with per-task failure references |
| `teacher_summary.json` | Public aggregate agreement, failures, denominators and hashes |
| `plan_summary.json` | Plan/dry-run only, zero acquired requests |
| `source_receipt.json`, `identities.json` | Input integrity receipt and separately named identity hashes |

Files use exclusive creation. Received completion data pass through the
existing `_safe_private` secret-redaction policy, then are written, flushed
and fsynced to 0600 storage before finish, alias or decision parsing.
`raw_response_utf8_sha256` hashes the originally received response text. The
persisted copy can differ when configured/environment credentials are
redacted; its separate artifact byte hash identifies the actual stored file.
No raw exception message or private rationale enters public results. The
complete bank is private even when its top-level current labels are removed:
later tasks' legitimate histories would expose earlier tasks' answers.

Unknown model alias, non-`stop` completion, unexpected retry, invalid JSON,
non-integer share sizes, short sales or negative joint cash count as failures.
There is no clipping, automatic hold or retry. After a batch containing only
provider failures, stop and leave remaining tasks unattempted. A callback,
runtime failure or unfinished task marks the run failed while preserving the
received evidence. It is not a resumable child. An acquisition in which all
195 slots resolve can finish with invalid responses; “complete” describes
request accounting, not scientific success.

Report planned, attempted, resolved, unattempted and unresolved logical
requests separately from application attempts, real physical endpoint
attempts, raw text responses, valid joint decisions and failure counts. Fake
attempts are labelled synthetic and contribute zero real endpoint requests.
The historical 39 people remain separate from the zero new human count.

## Evaluation and interpretation

Use the existing strict `parse_joint_response` and
`evaluate_joint_predictions`. Compare against the same tasks' human labels
with exact six-asset agreement, asset action agreement and mean absolute
signed-share error. Metrics use the paired-valid denominator, while coverage
retains missing and invalid responses. Public reports aggregate failures and
never expose per-task predictions. The deterministic hold baseline is
reported on all 195 tasks and, separately, on exactly the Teacher-valid task
subset. Only the latter has the same paired denominator as the Teacher
agreement metrics when responses fail. A hash identifies that subset without
publishing its individual labels or predictions. Neither control is another
Teacher run.

Also report a 3x3 buy/hold/sell confusion table, with human actions as rows
and predictions as columns, for the Teacher-valid joint tasks and the hold
control on exactly that subset. Each class recall uses its human support;
an absent class has recall `null`, and macro recall averages only observed
human classes. With no valid tasks, recalls and macro recall are `null`.
This prevents a high hold frequency from hiding failure on buys or sells.
These descriptive summaries add no threshold, population claim or
pseudo-independent stock-level confidence interval.

No metric is assigned a universal pass threshold. No Teacher ranking,
Student training or human-likeness selection is performed. K=1 does not
estimate a conditional response distribution. Results concern this explicitly
reconstructed six-asset laboratory decision task and cannot validate the
current single-asset market or endogenous human price formation. Reusing
another human's recorded past actions is conditional evaluation, not proof of
a stable simulated individual across a self-generated trajectory.

## Commands to prepare and inspect

Plan after code, registry and this document have been reviewed and frozen:

```bash
python3 -m experiments.human_early_teacher --task plan \
  --source-run results_information_diagnostics/runs/human-early-tasks-20260909-a1 \
  --source-manifest-sha256 5f7ffcbede579c520756f92570b6e79de3d73fc97ff146378829f692be920286 \
  --run-id human-early-teacher-plan-20260909-a1
```

Fake or dry acquisition must use the resulting exact plan manifest pin:

```bash
python3 -m experiments.human_early_teacher --task acquire \
  --plan-run PATH_TO_FINISHED_PLAN --plan-manifest-sha256 EXACT_PLAN_SHA \
  --provider fake_test_teacher --run-id UNIQUE_FAKE_RUN_ID
```

The shown fake command does not call a model endpoint. The implementation
delivery runs only synthetic tests under explicit temporary roots. It does
not create the formal source plan or a real acquisition. The source and all
historical runs are unchanged.

## Verification

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_human_early_teacher -v
git diff --check
```

Tests cover fixed counts and order, duplicate-prompt units, plan tampering,
source manifest/artifact/bank integrity, private file modes, per-task request
isolation, current-label exclusion, default fake behavior, the real gates,
alias and finish enforcement with fake transport, raw-before-parse, drained
callbacks, partial honest-N, and failure-message privacy. No production
dependency or existing CLI/schema/prompt/market mechanism is changed.

Implementation verification: the command above passed **20 synthetic tests
in 8.315 seconds**. The tests also verify same-subset hold comparisons,
3x3 confusion counts, class recalls and absent-class `null` handling. Real-mode
gate tests replace the Provider with a local fixture and prohibit outbound
socket connections; their callback counts are not endpoint observations.
Every test run and source is confined to an explicitly named temporary root.
The suite does not depend on the ignored real human input or create a formal
195-request research plan.
