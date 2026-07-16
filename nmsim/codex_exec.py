"""Experimental adapter for the official local ``codex exec`` CLI.

This module intentionally does not implement an HTTP API or inspect Codex
credential files.  Authentication is owned by the user's Codex installation;
the only supported check is the CLI's public ``codex login status`` command.

The adapter is deliberately fail-closed.  It accepts only a structured final
agent message and rejects every observed tool operation.  Provider failures are
raised to the managed run boundary rather than converted into a fallback market
decision.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


CODEX_EXEC_PROVIDER_ID = "codex_exec"
CODEX_WRAPPER_PROTOCOL_VERSION = "1.0"
CODEX_DECISION_SCHEMA_VERSION = "1.0"
CODEX_TOOL_SURFACE_CONTRACT_VERSION = "1.0"
CODEX_REASONING_EFFORT_ENV = "NMSIM_CODEX_REASONING_EFFORT"

_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "schemas"
    / "codex_exec_decision_v1.json"
)

_WRAPPER_TEMPLATE = """\
NMSIM CODEX WRAPPER PROTOCOL {wrapper_version}

This is a market-role decision task, not a coding task.
- Do not inspect files, execute commands, call tools, browse, or use MCP.
- Do not explain or redesign the task.
- Use only the Persona and Observation contained in the production prompts below.
- Return exactly one object satisfying the supplied JSON Schema.
- The `reasoning` value is a short private explanation authored for this task;
  it is not a request for hidden chain-of-thought.
- `public_take` is the only explanatory text eligible for public propagation.
- When action is `hold`, quantity must be 0.

<production_system_prompt>
{system}
</production_system_prompt>

<production_user_prompt>
{user}
</production_user_prompt>
"""

_REQUIRED_EXEC_FLAGS = (
    "--config",
    "--strict-config",
    "--ephemeral",
    "--sandbox",
    "--ignore-user-config",
    "--ignore-rules",
    "--skip-git-repo-check",
    "--json",
    "--output-schema",
    "--output-last-message",
    "--color",
    "--model",
    "--cd",
)
_REQUIRED_APP_SERVER_FLAGS = ("--strict-config", "--listen")
_ALLOWED_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh"}
)

# These settings are passed explicitly on every real Codex turn.  They are
# also validated, without submitting a model task, through the CLI's strict
# app-server config parser.  `tools.view_image` is deliberately retained even
# on CLI versions that do not yet recognize it: inability to validate any
# required control makes real use fail closed.
_NO_TOOLS_CONFIG_ITEMS: tuple[tuple[str, Any], ...] = (
    ("forced_login_method", "chatgpt"),
    ("approval_policy", "never"),
    ("sandbox_mode", "read-only"),
    ("web_search", "disabled"),
    ("history.persistence", "none"),
    ("hide_agent_reasoning", True),
    ("show_raw_agent_reasoning", False),
    ("personality", "none"),
    ("features.shell_tool", False),
    ("features.unified_exec", False),
    ("features.apps", False),
    ("tools.view_image", False),
    ("tools.web_search", False),
    ("mcp_servers", {}),
    ("features.browser_use", False),
    ("features.browser_use_external", False),
    ("features.browser_use_full_cdp_access", False),
    ("features.computer_use", False),
    ("features.image_generation", False),
    ("features.in_app_browser", False),
    ("features.enable_mcp_apps", False),
    ("features.plugins", False),
    ("features.remote_plugin", False),
    ("features.plugin_sharing", False),
    ("features.hooks", False),
    ("features.skill_mcp_dependency_install", False),
    ("features.tool_call_mcp_elicitation", False),
    ("features.request_permissions_tool", False),
    ("features.multi_agent", False),
    ("features.shell_snapshot", False),
    ("allow_login_shell", False),
    ("feedback.enabled", False),
    ("analytics.enabled", False),
    ("check_for_update_on_startup", False),
)

_ALLOWED_TOP_LEVEL_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "item.started",
        "item.updated",
        "item.completed",
        # These direct forms are accepted for forward-compatible fake/CLI
        # emitters only when they contain no prohibited nested operation.
        "reasoning",
        "agent_message",
        "usage",
    }
)
_ALLOWED_ITEM_TYPES = frozenset({"reasoning", "agent_message", "message"})
_PROHIBITED_EVENT_TYPES = frozenset(
    {
        "command_execution",
        "exec_command_begin",
        "exec_command_output",
        "exec_command_end",
        "shell",
        "shell_command",
        "unified_exec",
        "file_change",
        "file_read",
        "file_write",
        "apply_patch",
        "patch_apply",
        "mcp_tool_call",
        "mcp_call",
        "app_call",
        "connector_call",
        "web_search",
        "web_search_call",
        "browser_use",
        "image_generation",
        "image_tool_call",
        "view_image",
        "tool_call",
        "computer_use",
        "permission_request",
        "request_permissions",
        "privilege_escalation",
        "external_action",
    }
)

_API_ENV_EXACT = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_ACCESS_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_BASE_URL",
        "CODEX_API_BASE",
        "OPENAI_ORGANIZATION",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT",
        "OPENAI_PROJECT_ID",
    }
)
_API_ENV_SUFFIX_RE = re.compile(r"(?:^|_)API_KEY$", re.IGNORECASE)
_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:^|_)(?:AUTHORIZATION|COOKIE|CREDENTIALS?|PASSWORD|PRIVATE_KEY|SECRET|SESSION|TOKEN)(?:_|$)|"
    r"ACCESS_KEY|AUTH_SOCK",
    re.IGNORECASE,
)
_ISOLATION_ENV_KEYS = frozenset(
    {"PWD", "OLDPWD", "GIT_DIR", "GIT_WORK_TREE", "PYTHONPATH"}
)
_SAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_VERSION_RE = re.compile(r"(?:codex(?:-cli)?\s+)?(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", re.I)
_USAGE_LIMIT_RE = re.compile(
    r"(?:usage|rate)\s+limit|quota\s+(?:exceeded|reached)|insufficient\s+(?:credits|quota)",
    re.I,
)
_MODEL_UNAVAILABLE_RE = re.compile(
    r"model\s+(?:is\s+)?(?:not\s+available|unavailable|not\s+found)|unknown\s+model",
    re.I,
)
_SENSITIVE_OUTPUT_RES = (
    re.compile(r"authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"[\"']?(?:access_token|refresh_token|api_key)[\"']?\s*[:=]\s*[\"'][^\"']{8,}", re.I),
    re.compile(r"(?:cookie|session)\s*[:=]\s*[^\s;]{16,}", re.I),
)

_GLOBAL_CODEX_EXEC_LOCK = threading.Lock()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _toml_cli_value(value: Any) -> str:
    """Serialize the reviewed scalar/empty-table config values for ``-c``."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping) and not value:
        return "{}"
    raise TypeError("unsupported Codex CLI config value type")


def validate_codex_reasoning_effort(reasoning_effort: str) -> str:
    """Return a reviewed explicit reasoning-effort value."""

    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        raise ValueError("CodexExec requires an explicit reasoning effort")
    normalised = reasoning_effort.strip().lower()
    if normalised not in _ALLOWED_REASONING_EFFORTS:
        raise ValueError(
            "CodexExec reasoning effort must be one of {}".format(
                ", ".join(sorted(_ALLOWED_REASONING_EFFORTS))
            )
        )
    return normalised


def codex_reasoning_effort_from_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    source = os.environ if environment is None else environment
    value = source.get(CODEX_REASONING_EFFORT_ENV)
    if value is None or not str(value).strip():
        return None
    return validate_codex_reasoning_effort(str(value))


def codex_tool_surface_contract(
    *, reasoning_effort: Optional[str] = None
) -> dict[str, Any]:
    """Return the stable no-tools request contract, without probing the CLI."""

    effort = (
        None
        if reasoning_effort is None
        else validate_codex_reasoning_effort(reasoning_effort)
    )
    config = {key: value for key, value in _NO_TOOLS_CONFIG_ITEMS}
    if effort is not None:
        config["model_reasoning_effort"] = effort
    return {
        "contract_version": CODEX_TOOL_SURFACE_CONTRACT_VERSION,
        "required_config": config,
        "provider_transport_network_expected": True,
        "agent_tool_network_enabled": False,
        "web_search_mode": "disabled",
        "shell_tool_enabled": False,
        "unified_exec_enabled": False,
        "apps_enabled": False,
        "view_image_enabled": False,
        "history_persistence": "none",
        "agent_reasoning_events_hidden": True,
        "show_raw_agent_reasoning": False,
        "personality": "none",
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "forced_login_method": "chatgpt",
        "ephemeral": True,
        "reasoning_effort": effort,
    }


def _schema_bytes() -> bytes:
    return _SCHEMA_PATH.read_bytes()


CODEX_DECISION_SCHEMA_HASH = _sha256_bytes(_schema_bytes())
CODEX_WRAPPER_SOURCE_HASH = _sha256_text(_WRAPPER_TEMPLATE)
CODEX_TOOL_SURFACE_CONTRACT_HASH = _sha256_text(
    _canonical_json(codex_tool_surface_contract())
)


class CodexExecError(RuntimeError):
    """A public-safe, stable Codex adapter failure.

    Raw subprocess output, prompts, and model rationale are deliberately absent
    from both the exception text and ``public_metadata``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        public_metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.code = code
        self.public_metadata = dict(public_metadata or {})
        super().__init__("{}: {}".format(code, message))


@dataclass(frozen=True)
class CodexCapabilityProbe:
    cli_version: str
    binary_name: str
    binary_sha256: str
    auth_mode: str
    auth_verified: bool
    supported_exec_flags: tuple[str, ...]
    supported_config_keys: tuple[str, ...]
    tool_surface_verified: bool
    capability_probe_method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cli_version": self.cli_version,
            "binary_identity": {
                "name": self.binary_name,
                "sha256": self.binary_sha256,
            },
            "auth_mode": self.auth_mode,
            "auth_verified": self.auth_verified,
            "supported_exec_flags": list(self.supported_exec_flags),
            "supported_config_keys": list(self.supported_config_keys),
            "tool_surface_verified": self.tool_surface_verified,
            "capability_probe_method": self.capability_probe_method,
        }


@dataclass(frozen=True)
class _ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    latency_seconds: float


def sanitized_subprocess_environment(
    source: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Copy an environment while removing conflicting billable API settings.

    ``CODEX_HOME`` is intentionally preserved so the official CLI can use its
    own managed login.  This function never opens or interprets that directory.
    """

    original = os.environ if source is None else source
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in original.items():
        key = str(raw_key)
        upper = key.upper()
        if (
            upper in _API_ENV_EXACT
            or upper in _ISOLATION_ENV_KEYS
            or _API_ENV_SUFFIX_RE.search(upper)
            or _SENSITIVE_ENV_NAME_RE.search(upper)
        ):
            continue
        cleaned[key] = str(raw_value)
    return cleaned


def _safe_identifier(value: Optional[str], fallback: str) -> str:
    text = _SAFE_IDENTIFIER_RE.sub("-", str(value or "")).strip(".-_")
    return (text[:40] or fallback)


def _safe_public_label(value: str, fallback: str = "redacted") -> str:
    """Keep conventional identity labels; hash arbitrary untrusted text."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value):
        return value
    return "{}-sha256:{}".format(fallback, _sha256_text(value)[:12])


def validate_codex_model_identity(model: str) -> str:
    """Return a safe explicit model id without echoing rejected input."""

    if not isinstance(model, str) or not model.strip():
        raise ValueError("CodexExec requires an explicit requested model identity")
    normalised = model.strip()
    if not _SAFE_MODEL_RE.fullmatch(normalised):
        raise ValueError("CodexExec model identity contains unsafe characters")
    return normalised


def _resolve_executable(binary: os.PathLike[str] | str) -> pathlib.Path:
    requested = os.fspath(binary)
    candidate = shutil.which(requested)
    if candidate is None and (os.sep in requested or (os.altsep and os.altsep in requested)):
        path = pathlib.Path(requested).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            candidate = str(path)
    if candidate is None:
        raise CodexExecError("codex_binary_missing", "Codex CLI executable was not found")
    resolved = pathlib.Path(candidate).resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise CodexExecError("codex_binary_missing", "Codex CLI executable is not executable")
    return resolved


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - exercised only on non-POSIX platforms
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


def _run_limited_process(
    argv: Sequence[str],
    *,
    stdin: bytes,
    cwd: pathlib.Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
    on_process_started: Optional[Callable[[], None]] = None,
) -> _ProcessResult:
    """Run an argv-only subprocess with independently bounded output pipes."""

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=dict(env),
            shell=False,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise CodexExecError(
            "codex_binary_missing",
            "Codex CLI executable disappeared before launch",
            public_metadata={"process_started": False},
        ) from error
    except OSError as error:
        raise CodexExecError(
            "subprocess_nonzero_exit",
            "Codex CLI process could not be launched",
            public_metadata={"process_started": False},
        ) from error
    if on_process_started is not None:
        on_process_started()

    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limit_exceeded = threading.Event()

    def read_stream(name: str, stream: Any, limit: int) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                remaining = max(0, limit + 1 - len(buffers[name]))
                if remaining:
                    buffers[name].extend(chunk[:remaining])
                if len(buffers[name]) > limit or len(chunk) > remaining:
                    limit_exceeded.set()
                    _kill_process_group(process)
                    break
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def write_stdin() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(stdin)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout, stdout_limit),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr, stderr_limit),
            daemon=True,
        ),
        threading.Thread(target=write_stdin, daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        process.wait()
    except BaseException:
        _kill_process_group(process)
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=2.0)

    latency = time.monotonic() - started
    if limit_exceeded.is_set():
        raise CodexExecError(
            "output_too_large",
            "Codex CLI output exceeded the configured public safety limit",
            public_metadata={
                "process_exit_code": int(process.returncode or 0),
                "latency_seconds": latency,
                "process_started": True,
            },
        )
    if timed_out:
        raise CodexExecError(
            "subprocess_timeout",
            "Codex CLI exceeded the hard subprocess timeout",
            public_metadata={
                "timeout_seconds": timeout_seconds,
                "process_exit_code": int(process.returncode or 0),
                "latency_seconds": latency,
                "process_started": True,
            },
        )
    return _ProcessResult(
        argv=tuple(str(value) for value in argv),
        returncode=int(process.returncode or 0),
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
        latency_seconds=latency,
    )


def _decode_bounded(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _classify_nonzero(result: _ProcessResult) -> CodexExecError:
    combined = "{}\n{}".format(
        _decode_bounded(result.stdout), _decode_bounded(result.stderr)
    )
    if _USAGE_LIMIT_RE.search(combined):
        code = "usage_limit_reached"
        message = "Codex usage limit was reached; no automatic retry is permitted"
    elif _MODEL_UNAVAILABLE_RE.search(combined):
        code = "model_not_available"
        message = "The requested Codex model is unavailable"
    else:
        code = "subprocess_nonzero_exit"
        message = "Codex CLI exited unsuccessfully"
    return CodexExecError(
        code,
        message,
        public_metadata={
            "process_exit_code": result.returncode,
            "latency_seconds": result.latency_seconds,
        },
    )


def _effective_config_items(
    reasoning_effort: Optional[str],
) -> tuple[tuple[str, Any], ...]:
    items = list(_NO_TOOLS_CONFIG_ITEMS)
    if reasoning_effort is not None:
        items.append(
            (
                "model_reasoning_effort",
                validate_codex_reasoning_effort(reasoning_effort),
            )
        )
    return tuple(items)


def _config_assignment(key: str, value: Any) -> str:
    return "{}={}".format(key, _toml_cli_value(value))


def _config_argv(items: Iterable[tuple[str, Any]]) -> list[str]:
    argv: list[str] = []
    for key, value in items:
        argv.extend(["-c", _config_assignment(key, value)])
    return argv


def _feature_states(text: str) -> dict[str, bool]:
    states: dict[str, bool] = {}
    for raw_line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s+\S+\s+(true|false)\s*$", raw_line)
        if match is not None:
            states[match.group(1)] = match.group(2) == "true"
    return states


def probe_codex_cli(
    binary: os.PathLike[str] | str = "codex",
    *,
    reasoning_effort: Optional[str] = None,
    timeout_seconds: float = 10.0,
    environment: Optional[Mapping[str, str]] = None,
    temp_root: Optional[os.PathLike[str] | str] = None,
) -> CodexCapabilityProbe:
    """Validate safe CLI controls and auth without submitting a model task."""

    executable = _resolve_executable(binary)
    auth_env = sanitized_subprocess_environment(environment)
    config_items = _effective_config_items(reasoning_effort)
    with tempfile.TemporaryDirectory(
        prefix="nmsim-codex-probe-",
        dir=None if temp_root is None else os.fspath(temp_root),
    ) as temporary:
        cwd = pathlib.Path(temporary)
        isolated_home = cwd / "codex-home"
        isolated_home.mkdir(mode=0o700)
        probe_env = dict(auth_env)
        probe_env["CODEX_HOME"] = str(isolated_home)

        def run(
            args: Sequence[str], *, use_managed_auth: bool = False
        ) -> _ProcessResult:
            return _run_limited_process(
                [str(executable), *args],
                stdin=b"",
                cwd=cwd,
                env=auth_env if use_managed_auth else probe_env,
                timeout_seconds=timeout_seconds,
                stdout_limit=256 * 1024,
                stderr_limit=64 * 1024,
            )

        def run_tool_surface(args: Sequence[str]) -> _ProcessResult:
            try:
                return run(args)
            except CodexExecError as error:
                raise CodexExecError(
                    "codex_tool_surface_cannot_be_disabled",
                    "Codex no-tools capability validation did not complete safely",
                    public_metadata={"probe_failure_code": error.code},
                ) from error

        version_result = run(["--version"])
        if version_result.returncode != 0:
            raise CodexExecError(
                "unsupported_codex_cli_version", "Codex CLI version probe failed"
            )
        version_text = "{}\n{}".format(
            _decode_bounded(version_result.stdout),
            _decode_bounded(version_result.stderr),
        )
        version_match = _VERSION_RE.search(version_text)
        if version_match is None:
            raise CodexExecError(
                "unsupported_codex_cli_version", "Codex CLI version was not recognizable"
            )

        help_result = run(["exec", "--help"])
        if help_result.returncode != 0:
            raise CodexExecError(
                "unsupported_codex_cli_version", "Codex exec capability probe failed"
            )
        help_text = "{}\n{}".format(
            _decode_bounded(help_result.stdout), _decode_bounded(help_result.stderr)
        )
        missing_flags = [flag for flag in _REQUIRED_EXEC_FLAGS if flag not in help_text]
        if missing_flags:
            raise CodexExecError(
                "unsupported_codex_cli_version",
                "Codex exec is missing required safe execution capabilities",
                public_metadata={"missing_flags": sorted(missing_flags)},
            )

        app_server_help = run_tool_surface(["app-server", "--help"])
        app_server_help_text = "{}\n{}".format(
            _decode_bounded(app_server_help.stdout),
            _decode_bounded(app_server_help.stderr),
        )
        missing_probe_flags = [
            flag
            for flag in _REQUIRED_APP_SERVER_FLAGS
            if flag not in app_server_help_text
        ]
        if app_server_help.returncode != 0 or missing_probe_flags:
            raise CodexExecError(
                "codex_tool_surface_cannot_be_disabled",
                "Codex CLI cannot strictly validate the required no-tools configuration",
                public_metadata={"missing_probe_flags": sorted(missing_probe_flags)},
            )

        validation = run_tool_surface(
            [
                "app-server",
                "--strict-config",
                "--listen",
                "stdio://",
                *_config_argv(config_items),
            ]
        )
        validation_text = "{}\n{}".format(
            _decode_bounded(validation.stdout),
            _decode_bounded(validation.stderr),
        )
        reported_unsupported = sorted(
            key
            for key, _value in config_items
            if key.lower() in validation_text.lower()
        )
        unsupported: list[str] = []
        if validation.returncode != 0:
            # Strict config parsing may stop after the first error. Diagnose
            # every reviewed key on the failure path so provenance never
            # overstates what the installed CLI accepted.
            for key, value in config_items:
                single = run_tool_surface(
                    [
                        "app-server",
                        "--strict-config",
                        "--listen",
                        "stdio://",
                        "-c",
                        _config_assignment(key, value),
                    ]
                )
                if single.returncode != 0:
                    unsupported.append(key)
        if validation.returncode != 0:
            raise CodexExecError(
                "codex_tool_surface_cannot_be_disabled",
                "Codex CLI cannot validate every required no-tools control",
                public_metadata={
                    "unsupported_controls": sorted(unsupported)
                    or reported_unsupported
                    or ["<strict-config-validation-failed>"]
                },
            )

        feature_result = run_tool_surface(
            ["features", "list", *_config_argv(config_items)]
        )
        feature_text = "{}\n{}".format(
            _decode_bounded(feature_result.stdout),
            _decode_bounded(feature_result.stderr),
        )
        feature_states = _feature_states(feature_text)
        unconfirmed_features = sorted(
            key
            for key, expected in config_items
            if key.startswith("features.")
            and (
                key.removeprefix("features.") not in feature_states
                or feature_states[key.removeprefix("features.")] is not bool(expected)
            )
        )
        if feature_result.returncode != 0 or unconfirmed_features:
            raise CodexExecError(
                "codex_tool_surface_cannot_be_disabled",
                "Codex CLI did not confirm the requested disabled feature states",
                public_metadata={
                    "unconfirmed_feature_controls": unconfirmed_features
                },
            )

        auth_result = run(["login", "status"], use_managed_auth=True)
        auth_text = "{}\n{}".format(
            _decode_bounded(auth_result.stdout), _decode_bounded(auth_result.stderr)
        )
        if re.search(r"api[ -]?key", auth_text, re.I):
            raise CodexExecError(
                "auth_mode_not_chatgpt",
                "Codex is authenticated with an API key rather than ChatGPT-managed login",
            )
        if auth_result.returncode != 0 or re.search(
            r"not\s+(?:logged|authenticated)|logged\s+out", auth_text, re.I
        ):
            raise CodexExecError(
                "codex_not_authenticated",
                "Codex CLI is not authenticated; login must be completed manually",
            )
        if not re.search(r"logged\s+in\s+using\s+chatgpt|chatgpt", auth_text, re.I):
            raise CodexExecError(
                "auth_mode_not_chatgpt",
                "Codex authentication mode could not be verified as ChatGPT-managed",
            )

    return CodexCapabilityProbe(
        cli_version=version_match.group(1),
        binary_name=_safe_public_label(executable.name, "binary"),
        binary_sha256=_sha256_file(executable),
        auth_mode="chatgpt_managed_codex",
        auth_verified=True,
        supported_exec_flags=tuple(sorted(_REQUIRED_EXEC_FLAGS)),
        supported_config_keys=tuple(sorted(key for key, _value in config_items)),
        tool_surface_verified=True,
        capability_probe_method=(
            "isolated_CODEX_HOME_app_server_strict_config_plus_features_list"
        ),
    )


def codex_static_adapter_identity(
    binary: os.PathLike[str] | str = "codex",
    *,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> dict[str, Any]:
    """Return replay/reuse identity without running Codex or checking auth.

    Executable discovery and byte hashing are local filesystem operations.  The
    resolved absolute path is intentionally never returned.  A missing binary
    is represented explicitly instead of turning dry-run identity construction
    into a subprocess or Provider setup attempt.
    """

    if model is not None:
        model = validate_codex_model_identity(model)
    effort = (
        None
        if reasoning_effort is None
        else validate_codex_reasoning_effort(reasoning_effort)
    )
    tool_surface = codex_tool_surface_contract(reasoning_effort=effort)

    requested = os.fspath(binary)
    try:
        executable = _resolve_executable(binary)
    except CodexExecError as error:
        if error.code != "codex_binary_missing":
            raise
        binary_identity = {
            "status": "missing",
            "name": _safe_public_label(pathlib.Path(requested).name or "codex", "binary"),
            "sha256": None,
        }
    else:
        binary_identity = {
            "status": "available",
            "name": _safe_public_label(executable.name, "binary"),
            "sha256": _sha256_file(executable),
        }
    return {
        "provider": CODEX_EXEC_PROVIDER_ID,
        "experimental": True,
        "requested_model": model,
        "reasoning_effort": effort,
        "binary_identity": binary_identity,
        "codex_wrapper_protocol_version": CODEX_WRAPPER_PROTOCOL_VERSION,
        "wrapper_source_hash": CODEX_WRAPPER_SOURCE_HASH,
        "decision_schema_version": CODEX_DECISION_SCHEMA_VERSION,
        "decision_schema_hash": CODEX_DECISION_SCHEMA_HASH,
        "tool_surface_contract": tool_surface,
        "tool_surface_contract_hash": _sha256_text(
            _canonical_json(tool_surface)
        ),
        "sandbox_mode": "read-only",
        "ephemeral": True,
        "strict_config": True,
        "provider_transport_network_expected": True,
        "provider_transport_network_declared_or_observed": "declared_expected",
        "agent_tool_network_enabled": False,
        "web_search_mode": "disabled",
        "shell_tool_enabled": False,
        "unified_exec_enabled": False,
        "apps_enabled": False,
        "view_image_enabled": False,
        "history_persistence": "none",
        "agent_reasoning_events_hidden": True,
        "auth_probe_performed": False,
        "subprocess_started": False,
        "model_turn_subprocess_started": False,
        "real_use_ready": False,
        "real_use_readiness": (
            "requires_runtime_tool_surface_and_auth_probe"
            if model and effort
            else "missing_explicit_request_identity"
        ),
        "real_use_missing_requirements": [
            name
            for name, present in (
                ("model", bool(model)),
                ("reasoning_effort", bool(effort)),
            )
            if not present
        ],
    }


def build_codex_wrapper(system: str, user: str) -> str:
    if not isinstance(system, str) or not isinstance(user, str):
        raise TypeError("Codex prompts must be strings")
    return _WRAPPER_TEMPLATE.format(
        wrapper_version=CODEX_WRAPPER_PROTOCOL_VERSION,
        system=system,
        user=user,
    )


def codex_request_identity(
    system: str,
    user: str,
    model: str,
    *,
    reasoning_effort: Optional[str] = None,
) -> dict[str, Any]:
    model = validate_codex_model_identity(model)
    effort = (
        None
        if reasoning_effort is None
        else validate_codex_reasoning_effort(reasoning_effort)
    )
    wrapper = build_codex_wrapper(system, user)
    tool_surface = codex_tool_surface_contract(reasoning_effort=effort)
    return {
        "provider": CODEX_EXEC_PROVIDER_ID,
        "requested_model": model,
        "reasoning_effort": effort,
        "codex_wrapper_protocol_version": CODEX_WRAPPER_PROTOCOL_VERSION,
        "wrapper_source_hash": CODEX_WRAPPER_SOURCE_HASH,
        "decision_schema_version": CODEX_DECISION_SCHEMA_VERSION,
        "decision_schema_hash": CODEX_DECISION_SCHEMA_HASH,
        "tool_surface_contract_hash": _sha256_text(
            _canonical_json(tool_surface)
        ),
        "production_system_prompt_hash": _sha256_text(system),
        "production_user_prompt_hash": _sha256_text(user),
        "final_combined_input_hash": _sha256_text(wrapper),
    }


def _all_type_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "type" and isinstance(child, str):
                yield child
            yield from _all_type_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_type_values(child)


def _is_prohibited_type(event_type: str) -> bool:
    normalized = (
        event_type.strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace("/", "_")
    )
    if normalized in _PROHIBITED_EVENT_TYPES:
        return True
    return any(
        marker in normalized
        for marker in (
            "command_execution",
            "exec_command",
            "shell_command",
            "unified_exec",
            "file_change",
            "file_read",
            "file_write",
            "apply_patch",
            "patch_apply",
            "mcp",
            "app_call",
            "connector_call",
            "web_search",
            "browser_use",
            "image_tool",
            "view_image",
            "image_generation",
            "tool_call",
            "computer_use",
            "permission_request",
            "request_permissions",
            "privilege_escalation",
        )
    )


def _reasoning_text_present(event: Mapping[str, Any]) -> bool:
    """Detect CLI reasoning prose without returning or persisting its content."""

    event_type = str(event.get("type") or "")
    item = event.get("item")
    is_reasoning = event_type == "reasoning" or (
        isinstance(item, Mapping) and item.get("type") == "reasoning"
    )
    if not is_reasoning:
        return False
    sources: list[Mapping[str, Any]] = [event]
    if isinstance(item, Mapping):
        sources.append(item)
    for source in sources:
        for key in ("text", "content", "summary", "reasoning", "message"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, list) and value:
                return True
    return False


def _extract_agent_message(event: Mapping[str, Any]) -> Optional[str]:
    event_type = event.get("type")
    if event_type == "agent_message":
        value = event.get("text", event.get("content"))
        return value if isinstance(value, str) else None
    if event_type in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") in {
            "agent_message",
            "message",
        }:
            value = item.get("text", item.get("content"))
            return value if isinstance(value, str) else None
    return None


def _usage_from_event(event: Mapping[str, Any]) -> dict[str, int]:
    source = event.get("usage")
    if not isinstance(source, Mapping):
        source = event.get("token_usage")
    if not isinstance(source, Mapping):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "cached_input_tokens": ("cached_input_tokens", "cached_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "reasoning_tokens": ("reasoning_tokens", "reasoning_output_tokens"),
    }
    result: dict[str, int] = {}
    for canonical, candidates in aliases.items():
        for candidate in candidates:
            value = source.get(candidate)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[canonical] = value
                break
    return result


def parse_codex_json_events(stdout: bytes | str) -> dict[str, Any]:
    """Validate JSONL events without retaining internal reasoning text."""

    text = stdout if isinstance(stdout, str) else _decode_bounded(stdout)
    counts: dict[str, int] = {}
    usage: dict[str, int] = {}
    final_messages: list[str] = []
    reported_model: Optional[str] = None
    reasoning_event_count = 0
    saw_event = False
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        saw_event = True
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError) as error:
            raise CodexExecError(
                "json_event_stream_invalid",
                "Codex JSON event stream contains malformed JSON",
                public_metadata={"line_number": line_number},
            ) from error
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise CodexExecError(
                "json_event_stream_invalid",
                "Codex JSON event stream contains an invalid event envelope",
                public_metadata={"line_number": line_number},
            )
        types = list(_all_type_values(event))
        violation = next((value for value in types if _is_prohibited_type(value)), None)
        if violation is not None:
            raise CodexExecError(
                "tool_use_violation",
                "Codex emitted a forbidden tool-operation event",
                public_metadata={
                    "event_type": _safe_public_label(violation, "event-type")
                },
            )
        event_type = event["type"]
        if event_type not in _ALLOWED_TOP_LEVEL_EVENTS:
            raise CodexExecError(
                "json_event_stream_invalid",
                "Codex emitted an unsupported event type",
                public_metadata={
                    "event_type": _safe_public_label(event_type, "event-type")
                },
            )
        if event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, Mapping) or item.get("type") not in _ALLOWED_ITEM_TYPES:
                raise CodexExecError(
                    "json_event_stream_invalid",
                    "Codex emitted an unsupported non-tool item event",
                    public_metadata={"event_type": event_type},
                )
        if event_type == "reasoning" or (
            isinstance(event.get("item"), Mapping)
            and event["item"].get("type") == "reasoning"
        ):
            reasoning_event_count += 1
            if _reasoning_text_present(event):
                raise CodexExecError(
                    "codex_reasoning_visibility_violation",
                    "Codex emitted reasoning content despite hidden-reasoning controls",
                    public_metadata={
                        "event_type": _safe_public_label(
                            event_type, "event-type"
                        ),
                        "effective_config_anomaly": True,
                    },
                )
        counts[event_type] = counts.get(event_type, 0) + 1
        message = _extract_agent_message(event)
        if message is not None:
            final_messages.append(message)
        for key, value in _usage_from_event(event).items():
            # A completed-turn usage object is normally cumulative.  max avoids
            # double-counting if a CLI version emits interim usage events.
            usage[key] = max(usage.get(key, 0), value)
        model_value = event.get("model")
        if isinstance(model_value, str) and model_value:
            reported_model = model_value
    if not saw_event:
        raise CodexExecError(
            "json_event_stream_invalid", "Codex JSON event stream was empty"
        )
    return {
        "event_type_counts": dict(sorted(counts.items())),
        "usage": usage,
        "final_messages": final_messages,
        "reported_model": reported_model,
        "reasoning_event_count": reasoning_event_count,
    }


def _loads_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise CodexExecError(
            "schema_validation_failed", "Final Codex response is not a JSON object"
        ) from error
    if not isinstance(value, dict):
        raise CodexExecError(
            "schema_validation_failed", "Final Codex response is not a JSON object"
        )
    return value


def validate_codex_decision(raw: str) -> dict[str, Any]:
    """Validate the versioned decision schema without third-party packages."""

    decision = _loads_json_object(raw)
    expected = {
        "reasoning",
        "sentiment",
        "public_take",
        "action",
        "quantity",
        "limit_price",
    }
    missing = sorted(expected - set(decision))
    extra = sorted(set(decision) - expected)
    if missing or extra:
        raise CodexExecError(
            "schema_validation_failed",
            "Final Codex response fields do not match the decision schema",
            public_metadata={"missing_fields": missing, "unexpected_fields": extra},
        )

    reasoning = decision["reasoning"]
    public_take = decision["public_take"]
    if not isinstance(reasoning, str) or len(reasoning) > 240:
        raise CodexExecError(
            "schema_validation_failed", "Decision reasoning violates its private field schema"
        )
    if not isinstance(public_take, str) or len(public_take) > 140:
        raise CodexExecError(
            "schema_validation_failed", "Decision public_take violates its public field schema"
        )
    sentiment = decision["sentiment"]
    if (
        isinstance(sentiment, bool)
        or not isinstance(sentiment, (int, float))
        or not math.isfinite(float(sentiment))
        or not -1.0 <= float(sentiment) <= 1.0
    ):
        raise CodexExecError(
            "schema_validation_failed", "Decision sentiment is outside [-1, 1]"
        )
    action = decision["action"]
    if action not in {"buy", "sell", "hold"}:
        raise CodexExecError(
            "schema_validation_failed", "Decision action is not buy, sell, or hold"
        )
    quantity = decision["quantity"]
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise CodexExecError(
            "schema_validation_failed", "Decision quantity is not a non-negative integer"
        )
    if action == "hold" and quantity != 0:
        raise CodexExecError(
            "schema_validation_failed", "A hold decision must have quantity 0"
        )
    limit_price = decision["limit_price"]
    if limit_price is not None and (
        isinstance(limit_price, bool)
        or not isinstance(limit_price, (int, float))
        or not math.isfinite(float(limit_price))
        or float(limit_price) <= 0.0
    ):
        raise CodexExecError(
            "schema_validation_failed", "Decision limit_price must be positive or null"
        )
    return decision


def _contains_sensitive_output(*values: str) -> bool:
    return any(pattern.search(value) for value in values for pattern in _SENSITIVE_OUTPUT_RES)


def _path_is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class CodexExecLLM:
    """Single-concurrency, experimental ``codex exec`` LLM adapter."""

    kind = CODEX_EXEC_PROVIDER_ID
    provider_id = CODEX_EXEC_PROVIDER_ID
    experimental = True
    temperature = None
    max_concurrency = 1
    external_network_expected = True

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: str,
        binary: os.PathLike[str] | str = "codex",
        timeout_seconds: float = 120.0,
        max_stdout_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 128 * 1024,
        max_final_message_bytes: int = 256 * 1024,
        max_input_bytes: int = 1024 * 1024,
        environment: Optional[Mapping[str, str]] = None,
        temp_root: Optional[os.PathLike[str] | str] = None,
        project_root: Optional[os.PathLike[str] | str] = None,
        run_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        probe: Optional[CodexCapabilityProbe] = None,
    ) -> None:
        model = validate_codex_model_identity(model)
        reasoning_effort = validate_codex_reasoning_effort(reasoning_effort)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        for name, value in (
            ("max_stdout_bytes", max_stdout_bytes),
            ("max_stderr_bytes", max_stderr_bytes),
            ("max_final_message_bytes", max_final_message_bytes),
            ("max_input_bytes", max_input_bytes),
        ):
            if value <= 0:
                raise ValueError("{} must be positive".format(name))
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.binary_path = _resolve_executable(binary)
        self.timeout_seconds = float(timeout_seconds)
        self.max_stdout_bytes = int(max_stdout_bytes)
        self.max_stderr_bytes = int(max_stderr_bytes)
        self.max_final_message_bytes = int(max_final_message_bytes)
        self.max_input_bytes = int(max_input_bytes)
        self._source_environment = dict(os.environ if environment is None else environment)
        self._temp_root = None if temp_root is None else pathlib.Path(temp_root).resolve()
        self._project_root = pathlib.Path(
            pathlib.Path.cwd() if project_root is None else project_root
        ).resolve()
        self._run_id = _safe_identifier(run_id, "run")
        self._agent_id = _safe_identifier(agent_id, "agent")
        self._request_sequence = 0
        self._lock = threading.Lock()
        self.provider_calls_attempted = 0
        self.provider_calls_succeeded = 0
        self.provider_calls_failed = 0
        self.network_access = False
        self.model_turn_process_started = False
        self.agent_tool_network_enabled = False
        self.last_call_metadata: Optional[dict[str, Any]] = None
        self.call_metadata_history: list[dict[str, Any]] = []
        self.usage_totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }
        self.probe = probe or probe_codex_cli(
            self.binary_path,
            reasoning_effort=self.reasoning_effort,
            timeout_seconds=min(self.timeout_seconds, 10.0),
            environment=self._source_environment,
            temp_root=self._temp_root,
        )
        if self.probe.binary_sha256 != _sha256_file(self.binary_path):
            raise CodexExecError(
                "unsupported_codex_cli_version",
                "Codex CLI binary identity changed after capability probing",
            )
        required_keys = {
            key for key, _value in _effective_config_items(self.reasoning_effort)
        }
        if (
            not self.probe.tool_surface_verified
            or not required_keys.issubset(set(self.probe.supported_config_keys))
        ):
            raise CodexExecError(
                "codex_tool_surface_cannot_be_disabled",
                "Codex no-tools capability probe did not verify every required control",
                public_metadata={
                    "unverified_controls": sorted(
                        required_keys - set(self.probe.supported_config_keys)
                    )
                },
            )

    @property
    def auth_mode(self) -> str:
        return self.probe.auth_mode

    @property
    def auth_verified(self) -> bool:
        return self.probe.auth_verified

    def set_request_identity(
        self, *, run_id: Optional[str] = None, agent_id: Optional[str] = None
    ) -> None:
        if run_id is not None:
            self._run_id = _safe_identifier(run_id, "run")
        if agent_id is not None:
            self._agent_id = _safe_identifier(agent_id, "agent")

    def identity_snapshot(self) -> dict[str, Any]:
        tool_surface = codex_tool_surface_contract(
            reasoning_effort=self.reasoning_effort
        )
        return {
            "provider": CODEX_EXEC_PROVIDER_ID,
            "experimental": True,
            "codex_cli_version": self.probe.cli_version,
            "binary_identity": {
                "name": self.probe.binary_name,
                "sha256": self.probe.binary_sha256,
            },
            "requested_model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "auth_mode": self.probe.auth_mode,
            "auth_verified": self.probe.auth_verified,
            "codex_wrapper_protocol_version": CODEX_WRAPPER_PROTOCOL_VERSION,
            "wrapper_source_hash": CODEX_WRAPPER_SOURCE_HASH,
            "decision_schema_version": CODEX_DECISION_SCHEMA_VERSION,
            "decision_schema_hash": CODEX_DECISION_SCHEMA_HASH,
            "tool_surface_contract": tool_surface,
            "tool_surface_contract_hash": _sha256_text(
                _canonical_json(tool_surface)
            ),
            "execution_flags": self._normalized_execution_flags(),
            "sandbox_mode": "read-only",
            "ephemeral": True,
            "strict_config": True,
            "auth_probe_performed": True,
            "forced_login_method": "chatgpt",
            "capability_probe_method": self.probe.capability_probe_method,
            "tool_surface_verified": self.probe.tool_surface_verified,
            "tool_access": "disabled_by_explicit_config_and_monitored",
            "provider_transport_network_expected": True,
            "provider_transport_network_declared_or_observed": (
                "process_started_network_not_observed"
                if self.model_turn_process_started
                else "declared_expected"
            ),
            "agent_tool_network_enabled": False,
            "web_search_mode": "disabled",
            "shell_tool_enabled": False,
            "unified_exec_enabled": False,
            "apps_enabled": False,
            "view_image_enabled": False,
            "history_persistence": "none",
            "agent_reasoning_events_hidden": True,
            "show_raw_agent_reasoning": False,
            "personality": "none",
            "model_turn_subprocess_started": self.model_turn_process_started,
        }

    def _normalized_execution_flags(self) -> list[str]:
        return [
            "exec",
            "--ephemeral",
            "--strict-config",
            "--sandbox=read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--output-schema=<isolated-schema>",
            "--output-last-message=<isolated-output>",
            "--color=never",
            "--model=<requested-model>",
            "--config=<reviewed-no-tools-contract>",
            "--config=model_reasoning_effort=<explicit>",
            "--cd=<isolated-cwd>",
            "stdin=-",
        ]

    def _argv(
        self, *, cwd: pathlib.Path, schema_path: pathlib.Path, output_path: pathlib.Path
    ) -> list[str]:
        return [
            str(self.binary_path),
            "exec",
            "--strict-config",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--color",
            "never",
            "--model",
            self.model,
            *_config_argv(_effective_config_items(self.reasoning_effort)),
            "--cd",
            str(cwd),
            "-",
        ]

    def _base_call_metadata(
        self, *, request_identity: Mapping[str, Any], isolated_cwd: pathlib.Path
    ) -> dict[str, Any]:
        return {
            **self.identity_snapshot(),
            **dict(request_identity),
            "request_sequence": self._request_sequence,
            "batch_identity": "sequential-single-request",
            "isolated_cwd_identity": _sha256_text(str(isolated_cwd.resolve())),
            "response_source": "provider",
            "process_exit_code": None,
            "latency_seconds": None,
            "timeout": False,
            "event_type_counts": {},
            "tool_use_violation_count": 0,
            "tool_calls_observed": 0,
            "reasoning_event_count": 0,
            "effective_config_anomaly": False,
            "usage": {},
            "reported_model": None,
            "actual_model_verification": "unavailable",
            "final_response_hash": None,
            "status": "active",
        }

    def complete(self, system: str, user: str) -> str:
        wrapper = build_codex_wrapper(system, user)
        wrapper_bytes = wrapper.encode("utf-8")
        if len(wrapper_bytes) > self.max_input_bytes:
            raise CodexExecError(
                "input_too_large", "Combined Codex input exceeded the configured limit"
            )
        request_identity = codex_request_identity(
            system,
            user,
            self.model,
            reasoning_effort=self.reasoning_effort,
        )
        with self._lock, _GLOBAL_CODEX_EXEC_LOCK:
            self._request_sequence += 1
            prefix = "nmsim-codex-{}-{}-".format(self._run_id, self._agent_id)
            with tempfile.TemporaryDirectory(
                prefix=prefix,
                dir=None if self._temp_root is None else str(self._temp_root),
            ) as temporary:
                cwd = pathlib.Path(temporary).resolve()
                if _path_is_within(cwd, self._project_root):
                    raise CodexExecError(
                        "unsafe_isolated_cwd",
                        "Codex isolated working directory must be outside the project repository",
                    )
                metadata = self._base_call_metadata(
                    request_identity=request_identity, isolated_cwd=cwd
                )
                schema_path = cwd / "decision.schema.json"
                output_path = cwd / "final-message.json"
                schema_path.write_bytes(_schema_bytes())
                schema_path.chmod(0o600)
                # Pre-create a private path.  The CLI may replace or truncate it.
                output_path.touch(mode=0o600, exist_ok=False)
                argv = self._argv(
                    cwd=cwd, schema_path=schema_path, output_path=output_path
                )
                env = sanitized_subprocess_environment(self._source_environment)
                self.last_call_metadata = metadata
                self.call_metadata_history.append(metadata)
                self.provider_calls_attempted += 1
                metadata["provider_transport_network_declared_or_observed"] = (
                    "declared_expected_process_launch_pending"
                )

                def mark_model_turn_process_started() -> None:
                    self.model_turn_process_started = True
                    self.network_access = True
                    metadata[
                        "provider_transport_network_declared_or_observed"
                    ] = "process_started_network_not_observed"
                    metadata["model_turn_subprocess_started"] = True

                try:
                    result = _run_limited_process(
                        argv,
                        stdin=wrapper_bytes,
                        cwd=cwd,
                        env=env,
                        timeout_seconds=self.timeout_seconds,
                        stdout_limit=self.max_stdout_bytes,
                        stderr_limit=self.max_stderr_bytes,
                        on_process_started=mark_model_turn_process_started,
                    )
                    metadata["process_exit_code"] = result.returncode
                    metadata["latency_seconds"] = result.latency_seconds
                    if result.returncode != 0:
                        raise _classify_nonzero(result)
                    parsed_events = parse_codex_json_events(result.stdout)
                    metadata["event_type_counts"] = parsed_events["event_type_counts"]
                    metadata["usage"] = parsed_events["usage"]
                    metadata["reasoning_event_count"] = parsed_events[
                        "reasoning_event_count"
                    ]
                    reported_model = parsed_events["reported_model"]
                    metadata["reported_model"] = (
                        _safe_public_label(reported_model, "model")
                        if isinstance(reported_model, str)
                        else None
                    )
                    if reported_model is None:
                        metadata["actual_model_verification"] = "unavailable"
                    elif reported_model == self.model:
                        metadata["actual_model_verification"] = "verified"
                    else:
                        metadata["actual_model_verification"] = "mismatch"
                        raise CodexExecError(
                            "reported_model_mismatch",
                            "Codex reported a model different from the requested model",
                            public_metadata={
                                "requested_model": self.model,
                                "reported_model": _safe_public_label(
                                    reported_model, "model"
                                ),
                            },
                        )

                    try:
                        size = output_path.stat().st_size
                    except FileNotFoundError as error:
                        raise CodexExecError(
                            "output_missing", "Codex did not write a final output message"
                        ) from error
                    if size > self.max_final_message_bytes:
                        raise CodexExecError(
                            "output_too_large",
                            "Codex final message exceeded the configured size limit",
                        )
                    try:
                        raw_response = output_path.read_text(encoding="utf-8")
                    except UnicodeError as error:
                        raise CodexExecError(
                            "schema_validation_failed",
                            "Codex final output was not valid UTF-8 JSON",
                        ) from error
                    except OSError as error:
                        raise CodexExecError(
                            "output_missing", "Codex final output could not be read"
                        ) from error
                    if not raw_response.strip():
                        raise CodexExecError(
                            "output_missing", "Codex did not write a final output message"
                        )
                    if _contains_sensitive_output(
                        raw_response,
                        _decode_bounded(result.stdout),
                        _decode_bounded(result.stderr),
                    ):
                        raise CodexExecError(
                            "sensitive_output_detected",
                            "Codex output matched a credential-like safety pattern",
                        )
                    decision = validate_codex_decision(raw_response)
                    final_messages = parsed_events["final_messages"]
                    if not final_messages:
                        raise CodexExecError(
                            "output_missing", "Codex event stream did not contain a final agent message"
                        )
                    event_decision = validate_codex_decision(final_messages[-1])
                    if event_decision != decision:
                        raise CodexExecError(
                            "json_event_stream_invalid",
                            "Codex event final message disagreed with its output file",
                        )
                    final_response = _canonical_json(decision)
                    metadata["final_response_hash"] = _sha256_text(final_response)
                    metadata["status"] = "finished"
                    for key, value in parsed_events["usage"].items():
                        self.usage_totals[key] = self.usage_totals.get(key, 0) + value
                    self.provider_calls_succeeded += 1
                    return final_response
                except CodexExecError as error:
                    self.provider_calls_failed += 1
                    metadata["status"] = "failed"
                    metadata["error_code"] = error.code
                    if error.code == "subprocess_timeout":
                        metadata["timeout"] = True
                    if error.code == "tool_use_violation":
                        metadata["tool_use_violation_count"] = 1
                        metadata["tool_calls_observed"] = 1
                        metadata["event_type_counts"] = {
                            str(error.public_metadata.get("event_type", "unknown")): 1
                        }
                    if error.code == "codex_reasoning_visibility_violation":
                        metadata["effective_config_anomaly"] = True
                        metadata["reasoning_event_count"] = 1
                    if "process_exit_code" in error.public_metadata:
                        metadata["process_exit_code"] = error.public_metadata[
                            "process_exit_code"
                        ]
                    if "latency_seconds" in error.public_metadata:
                        metadata["latency_seconds"] = error.public_metadata[
                            "latency_seconds"
                        ]
                    if error.public_metadata.get("process_started") is True:
                        self.model_turn_process_started = True
                        self.network_access = True
                        metadata[
                            "provider_transport_network_declared_or_observed"
                        ] = "process_started_network_not_observed"
                    elif error.public_metadata.get("process_started") is False:
                        self.network_access = False
                        metadata["model_turn_subprocess_started"] = False
                        metadata[
                            "provider_transport_network_declared_or_observed"
                        ] = "declared_expected_process_launch_failed"
                    raise
                except BaseException as error:
                    self.provider_calls_failed += 1
                    metadata["status"] = "failed"
                    metadata["error_code"] = (
                        "keyboard_interrupt"
                        if isinstance(error, KeyboardInterrupt)
                        else "provider_internal_error"
                    )
                    raise

    async def acomplete(self, system: str, user: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.complete, system, user)

    def complete_batch(self, prompts: Sequence[tuple[str, str]]) -> list[str]:
        # The CLI has no native batch API.  Sequential calls preserve the hard
        # concurrency limit of one and make request order explicit.
        return [self.complete(system, user) for system, user in prompts]


__all__ = [
    "CODEX_DECISION_SCHEMA_HASH",
    "CODEX_DECISION_SCHEMA_VERSION",
    "CODEX_EXEC_PROVIDER_ID",
    "CODEX_REASONING_EFFORT_ENV",
    "CODEX_TOOL_SURFACE_CONTRACT_HASH",
    "CODEX_TOOL_SURFACE_CONTRACT_VERSION",
    "CODEX_WRAPPER_PROTOCOL_VERSION",
    "CODEX_WRAPPER_SOURCE_HASH",
    "CodexCapabilityProbe",
    "CodexExecError",
    "CodexExecLLM",
    "build_codex_wrapper",
    "codex_request_identity",
    "codex_reasoning_effort_from_environment",
    "codex_static_adapter_identity",
    "codex_tool_surface_contract",
    "parse_codex_json_events",
    "probe_codex_cli",
    "sanitized_subprocess_environment",
    "validate_codex_model_identity",
    "validate_codex_reasoning_effort",
    "validate_codex_decision",
]
