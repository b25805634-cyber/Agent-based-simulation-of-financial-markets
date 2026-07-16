# Experimental Codex Exec Provider

`CodexExecLLM` is an experimental, local adapter for the official Codex CLI.
It launches `codex exec` as an argv-based subprocess and consumes the CLI JSON
event stream. It is **not** the OpenAI API, an OpenAI-compatible HTTP endpoint,
or an HTTP reverse proxy. It does not replace the historical MiniMax/OpenAI-
compatible condition and is never the default Provider.

The adapter is intended only for personal, local, small-scale research. It
must not be exposed as a service. A more capable model is not automatically a
more realistic market Agent, and Codex is an agentic coding environment rather
than a neutral text-completion transport.

## Frozen local capability evidence

Phase 1.2B-CX1 used only non-task CLI probes. No model task was submitted.

| Probe | Sanitized result |
|---|---|
| `codex --version` | `codex-cli 0.144.4` |
| resolved executable | `/opt/homebrew/Caskroom/codex/0.144.4/codex-aarch64-apple-darwin` |
| executable SHA-256 | `3302acbda5f53de1a71ebdb0c0f2aae0d47f9324aa9fb6b4e78a47014fd51c7d` |
| `codex login status` | ChatGPT-managed login was reported; raw status is not persisted |
| `codex exec --help` | supports the flags listed below |

The observed `exec` help exposes `--strict-config`, `-m/--model`,
`-s/--sandbox` with `read-only`, `workspace-write`, and `danger-full-access`,
`-C/--cd`, `--skip-git-repo-check`, `--ephemeral`, `--ignore-user-config`,
`--ignore-rules`, `--output-schema`, `--json`, and
`-o/--output-last-message`. Omitting the positional prompt, or using `-`,
allows prompt input on stdin.

The help does **not** expose a direct flag that disables every tool. Read-only
sandboxing is therefore not treated as proof that the turn was tool-free. The
adapter must inspect the JSON event stream and reject a turn if any command,
file, MCP, web, image, or other external-operation event appears.

These observations describe local CLI version 0.144.4 only. Runtime probing is
fail-closed: missing required flags, an unparseable version, an unsupported
authentication mode, or a changed event contract must stop before a model
request rather than silently weakening isolation.

## Capability registry entry

The reviewed registry record uses capability schema `1.0` and records:

| Field | CodexExec value | Basis |
|---|---|---|
| `provider_id` | `codex_exec` | nmsim adapter identity |
| `transport_type` | `local_cli` | argv subprocess |
| `external_network_expected` | `true` | the CLI may contact the managed Codex service |
| `authentication_mode` | `chatgpt_managed_codex_cli` | accepted login mode |
| `supports_batch` | `false` | one CLI process per logical request |
| `supports_async` | `false` | no native async interface observed |
| `async_behavior` | `wrapper_level_only` | any async behavior is outside the CLI |
| `supports_temperature` | `false` | no supported exec flag observed |
| `supports_seed` | `false` | no supported exec flag observed |
| `supports_structured_output` | `true` | `--output-schema` plus JSON events |
| wrapper protocol/hash | `1.0` / `41b989cc…be75` | version-controlled Codex wrapper source |
| output schema/hash | `1.0` / `42bf5bd6․886` | version-controlled Decision JSON Schema |
| `supports_usage_metadata` | `true` | conditional on usage fields in JSON events |
| `supports_provider_response_id` | `false` | not asserted without a task event probe |
| `supports_record_replay` | `true` | nmsim outer wrapper |
| `supports_cache` | `true` | nmsim outer wrapper |
| `tool_access` | `technically_available_but_forbidden_for_this_provider` | agentic CLI boundary |
| `deterministic_claim` | `none` | no deterministic guarantee |
| `recommended_concurrency` | `1` | Phase 1.2B-CX1 hard limit |
| `experimental` | `true` | research-only adapter |

`supports_async=false` deliberately describes the CLI itself. A future nmsim
coroutine may wait for the subprocess without blocking other work, but that
wrapper must not be reported as native Provider batching or concurrency.

## Authentication boundary

The user logs in outside nmsim with `codex login`. The Provider may invoke only
the supported `codex login status` command and accept the sanitized conclusion
that authentication is ChatGPT-managed. It must reject API-key authentication
to avoid accidental API billing.

The Provider must never read, parse, copy, hash, or persist `~/.codex/auth.json`,
browser cookies, ChatGPT sessions, access tokens, or refresh tokens. It never
calls an OAuth refresh endpoint. Its child environment removes
`OPENAI_API_KEY`, `CODEX_API_KEY`, and other conflicting API-key variables. A
public manifest records only:

```json
{"auth_mode":"chatgpt_managed_codex","auth_verified":true}
```

The raw login-status output is neither a public event nor a substitute for a
model identity.

## Isolated execution

Each request uses a newly created temporary working directory that contains no
repository source, repository `AGENTS.md`, historical result, other Agent
prompt, private log, or credential. The CLI receives that directory via
`--cd`; the directory is removed after the request. The adapter uses an argv
array with `shell=False`, sends the combined input through stdin, applies a
hard timeout and stdout/stderr byte limits, and permits only one concurrent
Codex process.

For CLI 0.144.4, the intended supported isolation flags are:

```text
codex exec --strict-config --ephemeral --sandbox read-only --ignore-user-config
  --ignore-rules --skip-git-repo-check --json
  --output-schema <decision-schema> --output-last-message <private-file>
  --color never --cd <isolated-temporary-directory>
  --model <requested-model> -
```

This is explanatory notation, not a shell command. The implementation passes
each token separately. Unsupported flags are a capability failure; they are
not silently omitted. Run ids, Agent ids, and temporary filename components
are sanitized before use.

## Prompt adapter and structured result

The production System Prompt, User Prompt, Persona, and Observation remain
byte-for-byte outside the Provider adapter. CodexExec adds a versioned outer
wrapper which tells the coding agent not to inspect files or use tools, to use
only the supplied Persona and Observation, and to return the supplied JSON
Schema. Provenance records the wrapper protocol/hash, original system/user
Prompt hashes, and final combined-input hash.

Consequently the experimental condition is:

> underlying Codex model + Codex agent system behavior + Codex wrapper

It must not be described as identical to an ordinary completion Provider using
the same visible production Prompt.

The version-controlled Decision schema uses `additionalProperties=false` and
the current fields `reasoning`, `sentiment`, `public_take`, `action`,
`quantity`, and `limit_price`. `reasoning` means a short, model-authored private
explanation; it is not hidden chain-of-thought. `public_take` is the only text
eligible for public propagation, and a missing public take is never filled
from private reasoning.

The frozen adapter identities are:

- wrapper protocol `1.0`, SHA-256
  `41b989cc843d3a6fe961db9dfd97325e847c20564e609b62ed8acb2e3777be75`;
- Decision schema `1.0`, SHA-256
  `42bf5bd6aad5ee671c47fe72be0043b8cdc06a8ae3809b4dea4a4334fe534886`.

## Model-request identity

For `provider=codex_exec` only, the effective-config contract adds
`_provider_adapter_contract` to `model_request_config_summary` before computing
`model_request_config_hash`. The contract contains the requested model,
wrapper version/hash, Decision-schema version/hash, executable name/status and
binary-byte hash, read-only sandbox, ephemeral/strict-config state, and explicit
facts that no auth probe or subprocess was performed while creating this
static identity.

This extension is conditional. Mock, Anthropic, and OpenAI-compatible runs do
not receive the extra key, so their established model-request hashes remain
byte-for-byte unchanged. `full_effective_config_hash` continues to identify the
effective Config dataclass itself; it is not a vague substitute for the
Codex-specific model-request identity.

## Event allowlist and tool-use failure

The parser may consume lifecycle events, private reasoning/progress metadata,
the final agent message, completion status, and usage metadata. It must not
publish internal reasoning text. Any event representing command execution,
file change, MCP call, web search, image operation, or another external tool is
a `tool_use_violation` even when the read-only sandbox would have prevented a
write.

A violating response is not parsed as a trading Decision, does not enter the
market, and finalizes the managed run as failed. In model qualification, any
Codex adapter error stops the pilot immediately; it is not aggregated into an
apparently successful qualification. Public output contains only the safe
event type and classification.

## Provenance and privacy

The managed manifest may record CLI/binary identity, requested and reported
model identity when supplied, model-verification status, sanitized auth mode,
wrapper/schema identity, execution flags, sandbox and ephemeral status,
isolated-cwd identity, exit/timeout/latency facts, event-type counts, tool-use
count, available token usage, response source, request/batch identity, and the
final-response hash. Formal simulation manifests keep a safe metadata row for
every attempted Codex call as well as the final call, so partial failures do
not erase earlier process evidence.

It never records credentials, authorization material, a complete private
Prompt, or private rationale in public output. Private request/response records
remain mode `0600`.

The child environment removes API-key variables, common token/secret/password/
credential variables, SSH auth sockets, repository `PWD`/`OLDPWD`, Git worktree
overrides, and `PYTHONPATH`. `HOME`/`CODEX_HOME` remain available because the
official CLI owns the user's manually established login; nmsim does not open or
parse that authentication storage. This is defense in depth, not a claim that
an agentic CLI can be made equivalent to a neutral completion endpoint.

## Record and strict replay

Record mode invokes the reviewed executable and saves the exact final
structured response consumed by the simulator. Its schema 1.2 `model_config`
contains both the static `provider_adapter_contract` and the recorded runtime
identity from the successfully probed adapter. Each Codex request also carries
`provider_adapter_identity`, including the production system/user Prompt
hashes and `final_combined_input_hash`; full Prompt text remains private.

Replay mode does not construct Codex, probe version/help/login, spawn a Codex
process, or access the network; Provider-call counts remain zero. The recorded
runtime identity is historical evidence, not a claim that authentication was
checked again. Replay recomputes the current static adapter/request identities
without a subprocess and compares them with the recording.

Strict replay must match the existing recording schema 1.2 identities plus the
Codex wrapper, Decision output schema, requested/resolved model and CLI/binary
identity encoded by the model-request contract. A mismatch fails closed and
never falls through to a live process. Changing ordinary documentation alone
must not invalidate replay.

## Error taxonomy

The adapter distinguishes at least:

- `codex_binary_missing`
- `unsupported_codex_cli_version`
- `codex_not_authenticated`
- `auth_mode_not_chatgpt`
- `model_not_available`
- `reported_model_mismatch`
- `usage_limit_reached`
- `subprocess_timeout`
- `subprocess_nonzero_exit`
- `json_event_stream_invalid`
- `output_missing`
- `output_too_large`
- `schema_validation_failed`
- `tool_use_violation`
- `sensitive_output_detected`
- `provider_internal_error` (unexpected adapter failure after a call started)

`usage_limit_reached` is not retried and never triggers account rotation,
API-key fallback, or a non-official endpoint. Completion and honest-N retain
the actual failed count.

The capability probe checks every option actually passed by the adapter,
including `--color`. The installed 0.144.4 help and fake runtime tests also
confirm stdin prompt delivery using positional `-`; future CLI text drift in
that positional convention may still be detected only when the first guarded
fake/pilot invocation runs.

## Phase boundary

Phase 1.2B-CX1 adds and tests the adapter with a fake executable only. It makes
no real Codex model call, consumes no ChatGPT Pro quota, changes no default
Provider, and changes no market, Agent, social, leverage, validation, Prompt,
Persona, recording-schema, or scientific-fingerprint semantics.
