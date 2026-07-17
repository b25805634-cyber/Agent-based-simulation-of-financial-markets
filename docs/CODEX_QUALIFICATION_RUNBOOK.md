# CodexExec Qualification Runbook

This runbook governs a future, explicitly authorized qualification pilot for
the experimental `codex_exec` Provider. Phase 1.2B-CX1 does **not** execute any
real case. All implementation and regression tests use a fake executable.

CodexExec uses the locally installed official Codex CLI and the user's
manually established ChatGPT-managed login. It is not an API, proxy, shared
service, or replacement for the historical MiniMax baseline.

The sealed Phase 1.2A foundation is
`phase1.2a-model-foundation-v1` at
`a358a940c618b505fa3996d4e6acd22f617c6edf`. The earlier
`3594909b9cf72d97a835539770ba32486090d71f` commit is not the sealed Phase
1.2A tag target.

At the time of sealing, the local `codex-cli 0.144.4` strict config probe
rejects `tools.view_image`. The real-use gate is therefore closed and this
runbook is a future protocol, not an executable authorization.
The same rejection was reproduced on isolated official 0.144.5 and
0.145.0-alpha.16 runtimes; see
[CODEX_RUNTIME_COMPATIBILITY.md](CODEX_RUNTIME_COMPATIBILITY.md). No reviewed
version-specific alias exists.

## Before any quota-consuming run

1. Confirm the repository is on an explicitly approved experiment commit with
   a clean worktree and empty remote policy as applicable.
2. Review the frozen qualification protocol, fixture-set, rubric, visibility
   contract, Codex wrapper, Decision schema, no-tools configuration, and
   model-request identity versions/hashes.
3. Run the dry-run command. Dry-run must not construct a Provider, probe login,
   spawn Codex, access the network, or increment Provider calls.
4. Outside nmsim, the user may run `codex login`. nmsim never reads the auth
   file. The adapter accepts only the sanitized result of `codex login status`
   that indicates ChatGPT-managed authentication.
5. Verify the explicit requested model, explicit reasoning effort, and case
   selection. A subset is a pilot and must not be labelled a complete 48-case
   qualification.
6. Obtain explicit authorization for quota use. Merely supplying
   `--provider codex_exec` is not confirmation.
7. Confirm that every required no-tools config key passes the current CLI's
   non-task strict probe. `read-only` alone is insufficient. If any control,
   including `tools.view_image`, is unsupported, stop before Provider launch.

For a reviewed side-by-side runtime, set
`CODEX_EXEC_BINARY=/absolute/path/to/codex`. The older
`NMSIM_CODEX_EXECUTABLE` name is retained only for compatibility. Do not set
both to different paths.

Do not set `OPENAI_API_KEY` or `CODEX_API_KEY` for this adapter. The subprocess
sanitizes conflicting variables regardless, and API-key auth is rejected.

## Dry-run

The safe planning form is:

```bash
python3 -m experiments.model_qualification \
  --provider codex_exec \
  --model <user-reviewed-model> \
  --reasoning-effort low \
  --dry-run \
  --max-cases 1 \
  --out /tmp/nmsim-codex-qualification-dry
```

The first smoke must use a model id explicitly selected after a local,
non-task capability review, with `reasoning_effort=low`. This repository does
not hard-code or claim availability of an unverified model id. Dry-run may
display a missing model or reasoning effort for planning, but real-use
validation must reject either omission. If `--max-cases` is omitted in a Codex
dry-run, it defaults to one;
the example spells out `1` for audit clarity. Mock/Fake continue to default to
all 48 cases.

It reports protocol, fixture, rubric, wrapper and schema identities; selected
case ids and selection hash; requested model and reasoning effort; Provider
transport network expectation; disabled Agent-tool network state;
logical-request count; and output location. It must report Provider calls zero
and no observed Provider transport or Agent-tool network use without printing
full private Prompts.

The manifest labels model/reasoning selection as a separately hashed Provider
request option outside the frozen scientific Config dataclass. It remains part
of `model_request_config_hash`, Replay identity, and result-reuse identity.

## Triple real-use guard

A future live pilot requires all three:

```text
--provider codex_exec
--confirm-real-codex-usage
--max-cases N
```

These are the three quota/case confirmations. A real request additionally
requires an explicit model and explicit reasoning effort.

For example, the one-case shape is:

```bash
python3 -m experiments.model_qualification \
  --provider codex_exec \
  --model <user-reviewed-model> \
  --reasoning-effort low \
  --confirm-real-codex-usage \
  --max-cases 1 \
  --out /tmp/nmsim-codex-qualification-one
```

This command is documentation only and was not executed in Phase 1.2B-CX1.

The default and first pilot maximum is one case. A request above one also
requires `--confirm-case-count N`, with exactly the same integer. The hard
ceiling is 48 and workers must equal one. Missing or inconsistent confirmation
fails before Provider construction.

`experiments.model_qualification.run_qualification()` is a low-level library
helper, not an official quota-authorization boundary. Formal Codex pilots must
enter through the managed CLI above so the confirmation, worker and case-count
guards execute and leave a failed manifest when rejected.

Stable subset selectors are:

- `--case-id`
- `--fixture-id`
- `--persona-id`
- `--max-cases`

Selection is deterministic and stored with a selection hash, protocol hash,
fixture-set hash, rubric hash, wrapper hash, and schema hash. Selection order
must not depend on filesystem traversal or dictionary insertion order.

## Staged sequence

Run real qualification only after separate approval at each stage:

1. **One case.** Validate CLI/auth/model identity, output schema, event
   allowlist, active no-tools configuration, network separation, history and
   reasoning privacy, completion accounting, and managed finalization.
2. **Twelve-case pilot.** Use a frozen, balanced Persona/fixture subset. Report
   it only as a pilot; inspect failures and distributional diagnostics without
   editing the frozen rubric in reaction to the outputs.
3. **Forty-eight-case qualification.** Run all six Personas across all eight
   fixtures only after the pilot is accepted. This is the first result that may
   be described as the complete frozen protocol.

Codex being more capable does not establish greater Agent realism. Compare the
raw action/sentiment/quantity distributions, engineering metrics, soft
behavioral diagnostics, failures, and honest-N against the retained MiniMax
historical baseline. Do not reduce the comparison to one score.

## Qualification success and failure

The qualification continues to enforce Phase 1.2A visibility and privacy:

- only fields actually visible in the Provider Prompt may be scored;
- `fundamental_anchor_score` remains `not_scored` with
  `fundamental_anchor_not_visible` while the production Observation does not
  reveal fundamental value;
- private rationale is stored only in mode-`0600` private case records;
- public output may contain `public_take` and sentiment, not private rationale
  or a complete Prompt;
- missing `public_take` is never backfilled from reasoning.

Before a turn, the Provider must actively set forced ChatGPT login, approval
`never`, read-only sandboxing, disabled Web search/shell/unified-exec/Apps/image
tools and memories, `history.persistence=none`, hidden CLI reasoning,
personality `none`, disabled feedback and update checks. If the current CLI
cannot confirm every control, the run fails with
`codex_tool_surface_cannot_be_disabled`. Read-only is not a no-tools guarantee.

After launch, JSONL inspection remains the second defence. Any command,
file-change, `apply_patch`, MCP, App/connector, Web, image, computer-use,
permission-escalation, `request_permissions`, or other external-operation
event is a `tool_use_violation`. The response is not a Decision and is not
admitted to a market; public output records only the safe event type, not tool
output. A schema-invalid, oversized, missing, sensitive, timed-out, nonzero, or
usage-limited response is likewise a failed case with its stable reason code.
Any Codex adapter exception makes the enclosing managed qualification run
`failed`; partial completed-case counts remain honest, but no success summary
is published for the pilot.

CLI reasoning/progress event text is not the project's
`Decision.private_rationale` and is not hidden chain-of-thought that nmsim
claims to obtain. It is hidden by effective config and, if still emitted, is
reduced to safe event metadata. The final JSON `reasoning` field remains only a
short model-authored private decision explanation.

Usage limits are final for that attempt: no automatic retry, account rotation,
API-key fallback, alternative endpoint, or hidden continuation is allowed.

## Required provenance review

For each approved run, review the public manifest for:

- `run_kind=model_qualification` and experimental Provider identity;
- Codex CLI version and binary hash;
- requested/reported model, verification status, and reasoning effort;
- ChatGPT-managed auth verification without raw status output;
- wrapper, Decision schema, protocol, fixtures, rubric, visibility, and
  selection identities;
- exact execution flags, forced login/approval/personality settings, complete
  no-tools config, read-only sandbox, ephemeral mode, history persistence,
  reasoning visibility, and isolated-cwd identity;
- separate Provider-transport network expectation/observation and
  Agent-tool-network disabled state;
- Provider/logical-request/response-source/completion counts;
- event-type and tool-use counts;
- available input/cached/output/reasoning-token usage;
- timeout, process-exit, latency, failure-stage, and final-response hash;
- `honest_n_cases` for completed cases and zero simulation replicates.

Also verify that the private files are `0600`, no market `price_path.csv` was
created, and no full Prompt or private rationale appears in public files.

## Replay after a live record

Strict replay is the first required follow-up to an accepted live record. It
must not check login, spawn Codex, access the network, or increment Provider
calls. The replay must consume the same recorded final structured responses
and match the original case outputs under the complete source, runtime-config,
model-request, wrapper, schema, CLI/binary/model/reasoning-effort/no-tools,
Prompt, request-order, and batch identity contract.

The source recording's runtime CLI/auth evidence remains historical. Replay
does not revalidate login or run version/help probes; it recomputes the static
wrapper/schema/model/binary and per-request combined-input identities without
starting Codex, then rejects any mismatch.

Replay is an audit/debugging mechanism for an observed run. It is not a
counterfactual model response for a different market state, and it is not a
claim of statistical reproducibility or real-provider determinism.

## Stop conditions

Stop immediately and preserve the failed managed run if:

- authentication is not ChatGPT-managed;
- the CLI probe no longer supports a required isolation/output flag;
- any required no-tools/history/reasoning config key cannot be confirmed;
- explicit model or reasoning effort is missing;
- actual model identity cannot meet the declared policy;
- a tool-use event appears;
- sensitive output is detected;
- the quota limit is reported;
- the recording/model-request identity is incomplete;
- public/private separation or completion accounting fails.

Do not continue to a larger stage until the failure is explained, tested with
the fake executable where possible, and explicitly reviewed.

The installed 0.144.4 CLI currently meets the stop condition because
`tools.view_image` is not recognized by the strict parser, before effective
readback can confirm it disabled. Side-by-side stable
0.144.5 and prerelease 0.145.0-alpha.16 have the same limitation. No one-case
smoke, 12-case pilot, or 48-case qualification may start until that gap has a
reviewed fail-closed resolution. This phase did not run a real turn and did
not empirically validate upstream history persistence.
