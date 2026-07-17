# CodexExec Runtime Compatibility

This note records the no-model investigation completed on 2026-07-17. No
`codex exec` task, market Prompt, or model turn was submitted. The probes used
only `--version`, `exec --help`, `app-server --help`, `features list`,
`login status`, strict config parsing with empty stdin, and the app-server
`config/read` method.

The canonical Codex configuration reference currently documents
`tools.view_image` as the boolean switch for the local-image tool. The
installed and side-by-side runtimes below nevertheless reject that key. The
evidence proves a documentation/runtime strict-parser contract mismatch; it
does not by itself establish the upstream root cause and is not permission to
use an undocumented alias or omit the control.

Official references:

- [Codex CLI installation](https://developers.openai.com/codex/cli)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [Codex CLI changelog](https://developers.openai.com/codex/changelog)

## Runtime identities

| Runtime | Executable | SHA-256 | ChatGPT auth | Daily binary changed |
|---|---|---|---|---|
| Homebrew `0.144.4` | `/opt/homebrew/Caskroom/codex/0.144.4/codex-aarch64-apple-darwin` | `3302acbda5f53de1a71ebdb0c0f2aae0d47f9324aa9fb6b4e78a47014fd51c7d` | verified by `codex login status` | no |
| side-by-side stable `0.144.5` | `$HOME/.local/share/nmsim/codex-runtimes/0.144.5/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex` | `5e29ab10ca1171be158f7335dd6bd8ce1aaf9af1556939db36a5ee338be6f5f2` | verified | not on `PATH` |
| side-by-side prerelease `0.145.0-alpha.16` | `$HOME/.local/share/nmsim/codex-runtimes/0.145.0-alpha.16/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex` | `645dc190c5ef85e265d75d7c2342f31a5b47aab96ce9b38d9c815795ba493c3d` | verified | not on `PATH` |

The stable runtime came from the official package name
`@openai/codex@0.144.5` with an explicit
`https://registry.npmjs.org` registry and an isolated npm prefix. The
prerelease was investigated only after the latest stable runtime reproduced
the same parser rejection. Neither installation overwrote
`/opt/homebrew/bin/codex`.

Formal entrypoints select a side-by-side runtime with
`CODEX_EXEC_BINARY=/absolute/path/to/codex`. The older
`NMSIM_CODEX_EXECUTABLE` variable remains a compatibility alias. If both are
set to different runtimes, configuration fails closed before Provider setup.
The path is not a scientific Config field; the resolved executable byte hash,
CLI version, and runtime control mapping enter Codex Provider identity.

## Error attribution

The minimum reproduction is:

```text
codex app-server --strict-config --listen stdio:// \
  -c tools.view_image=false
```

It uses an isolated empty `CODEX_HOME`, an empty temporary cwd, empty stdin,
and no model argument. All three runtimes return exit code `1` and the
sanitized error:

```text
Error: unknown configuration field `tools.view_image` in -c/--config override
```

The classification is therefore:

| Classification | Result |
|---|---|
| `project_probe_rejected_key` | no; nmsim serializes and passes the canonical key |
| `codex_cli_rejected_key` | yes |
| `invocation_syntax_error` | no; the same argv accepts the other reviewed keys |
| `unsupported_in_installed_version` | yes, for all three investigated runtimes |

`inspect_codex_runtime_compatibility()` now preserves those dimensions in a
public-safe matrix and performs its own JSONL `config/read` effective readback.
The formal Provider still returns the stable outer error
`codex_tool_surface_cannot_be_disabled`; it does not leak arbitrary diagnostic
output or weaken the gate.

## Capability matrix

“Effective” means the value was confirmed through the no-model app-server
`config/read` response; feature controls must additionally match
`features list`. Strict parse acceptance by itself never makes a required
control ready. The CLI acceptance result is identical for 0.144.4, 0.144.5,
and 0.145.0-alpha.16.

| Canonical key | Requested value | Project probe | CLI parser | Verification | Required for real use | Result |
|---|---:|---|---|---|---:|---|
| `forced_login_method` | `chatgpt` | accepted | accepted | config/read effective | yes | confirmed |
| `approval_policy` | `never` | accepted | accepted | config/read effective | yes | confirmed |
| `sandbox_mode` | `read-only` | accepted | accepted | config/read effective | yes | confirmed |
| `web_search` | `disabled` | accepted | accepted | config/read effective | yes | confirmed |
| `history.persistence` | `none` | accepted | accepted | config/read effective | yes | confirmed |
| `hide_agent_reasoning` | `true` | accepted | accepted | config/read effective | yes | confirmed |
| `show_raw_agent_reasoning` | `false` | accepted | accepted | config/read effective | yes | confirmed |
| `personality` | `none` | accepted | accepted | config/read effective | yes | confirmed |
| `features.shell_tool` | `false` | accepted | accepted | features list + config/read | yes | confirmed |
| `features.unified_exec` | `false` | accepted | accepted | features list + config/read | yes | confirmed |
| `features.apps` | `false` | accepted | accepted | features list + config/read | yes | confirmed |
| `features.multi_agent` | `false` | accepted | accepted | features list + config/read | yes | confirmed |
| `features.hooks` | `false` | accepted | accepted | features list + config/read | yes | confirmed |
| `features.memories` | `false` | accepted | accepted | features list + config/read | yes | confirmed |
| `features.remote_plugin` | `false` | accepted | accepted | features list + config/read | yes | confirmed |
| `tools.view_image` | `false` | accepted | **rejected** | unavailable | yes | `unsupported_in_installed_version` |
| `tools.web_search` | `false` | accepted | accepted | config/read normalizes to `null` | no | supplementary only; top-level `web_search=disabled` is authoritative |
| `feedback.enabled` | `false` | accepted | accepted | config/read effective | yes | confirmed |
| `analytics.enabled` | `false` | accepted | accepted | config/read effective | yes | confirmed |
| `check_for_update_on_startup` | `false` | accepted | accepted | config/read effective | yes | confirmed |
| `allow_login_shell` | `false` | accepted | accepted | config/read effective | yes | confirmed |

The complete contract additionally confirms empty MCP servers and disabled
browser, external-browser/CDP, computer-use, image-generation, in-app-browser,
MCP Apps, plugins/plugin sharing, permission request, MCP elicitation, skill
MCP dependency installation, and shell snapshot controls.

The project feature parser now accepts maturity labels containing spaces, such
as `under development`; previously that formatting could falsely report
`features.enable_mcp_apps` and `features.request_permissions_tool` as
unconfirmed.

## Version-specific mappings

The canonical-to-actual mapping policy is exact-version and fail-closed.
There are currently no reviewed aliases, so every actual key equals its
canonical key. The following candidates were parser-rejected by both
side-by-side runtimes and are not used:

- `features.view_image_tool`
- `view_image_tool`
- `features.image_tool`
- `tools.image`
- `tools.view_image_tool`

The static mapping-policy hash enters the Codex model-request identity. A
successful runtime probe would additionally record the CLI-version-specific
resolved mapping hash and per-control matrix. Unknown canonical controls,
duplicate resolved keys, missing controls, changes to a reviewed
value/verification method/required status, or an unrecognized alias all fail
before a model turn. A caller-supplied capability object is revalidated against
the exact contract and cannot substitute a different resolved argv.

## Readiness

`runtime_compatible` covers the binary, no-tools controls, effective readback,
and ChatGPT-managed authentication. `real_use_ready` additionally requires an
explicit model and reasoning effort. A bare capability inspection therefore
cannot claim real-use readiness even if a future runtime passes the tool gate.

For every investigated runtime:

```json
{
  "real_use_ready": false,
  "stable_block_reason": "codex_tool_surface_cannot_be_disabled",
  "failed_required_control": "tools.view_image",
  "real_codex_model_turns": 0
}
```

The side-by-side installation therefore improves diagnosis and runtime
selection but does not authorize a one-case smoke. Strict Replay remains
offline: it does not inspect local CLI capability, check login, or start a
subprocess.

The pre-execution experiment identity hashes the binary bytes and static
exact-version mapping policy. A successful managed Provider run would also
record the CLI-reported version, effective per-control matrix, and resolved
mapping hash in runtime/recording provenance. The current child-reuse gate does
not separately re-probe that CLI-reported version; binary-byte identity is its
pre-execution runtime discriminator. This is an explicit limitation for
launcher binaries whose unchanged bytes could dispatch a mutable payload.
