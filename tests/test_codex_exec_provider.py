"""Offline tests for the experimental Codex CLI adapter.

Every subprocess in this module is a local fake executable.  No real Codex
model task, Provider network call, or subscription usage can occur.
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import stat
import tempfile
import textwrap
import unittest
from unittest import mock

import nmsim.codex_exec as codex_exec_module
from nmsim.config import Config
from nmsim.config_contract import build_effective_config_contract
from nmsim.codex_exec import (
    CODEX_DECISION_SCHEMA_HASH,
    CODEX_DECISION_SCHEMA_VERSION,
    CODEX_WRAPPER_PROTOCOL_VERSION,
    CODEX_WRAPPER_SOURCE_HASH,
    CodexExecError,
    CodexExecLLM,
    codex_request_identity,
    codex_static_adapter_identity,
    probe_codex_cli,
    sanitized_subprocess_environment,
    validate_codex_decision,
)
from nmsim.events import EventLogger
from nmsim.recording import (
    RecordingLLM,
    ReplayLLM,
    ReplayMismatchError,
    runtime_model_config,
)
from nmsim.result_reuse import ExpectedRunIdentity
from nmsim.run_context import ManagedRunContext


_EXPECTED_NO_TOOLS_CONFIG = {
    "forced_login_method": "chatgpt",
    "approval_policy": "never",
    "sandbox_mode": "read-only",
    "web_search": "disabled",
    "history.persistence": "none",
    "hide_agent_reasoning": True,
    "show_raw_agent_reasoning": False,
    "personality": "none",
    "features.shell_tool": False,
    "features.unified_exec": False,
    "features.apps": False,
    "tools.view_image": False,
    "feedback.enabled": False,
    "check_for_update_on_startup": False,
}


def _decode_cli_config_value(value):
    if not isinstance(value, str):
        return value
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {'"', "'"}
    ):
        return value[1:-1]
    return value


_FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

args = sys.argv[1:]

def append_invocation(kind, *, stdin_text=None, config_items=None):
    log_path = os.environ.get("FAKE_CODEX_INVOCATION_LOG")
    if not log_path:
        return
    row = {
        "kind": kind,
        "argv": args,
        "stdin": stdin_text,
        "config_items": config_items or [],
    }
    with pathlib.Path(log_path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")

def config_items():
    values = []
    index = 0
    while index < len(args):
        if args[index] in {"-c", "--config"}:
            if index + 1 >= len(args):
                print("missing config value", file=sys.stderr)
                raise SystemExit(2)
            raw = args[index + 1]
            key, separator, value = raw.partition("=")
            if not separator:
                print("invalid config value", file=sys.stderr)
                raise SystemExit(2)
            values.append([key, value])
            index += 2
            continue
        index += 1
    return values

def config_map(items):
    def decode(value):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            return value[1:-1]
        return value
    return {key: decode(value) for key, value in items}

if args == ["--version"]:
    append_invocation("version_probe")
    print("codex-cli 0.144.4")
    raise SystemExit(0)

if args == ["exec", "--help"]:
    flags = [
        "--strict-config", "--ephemeral", "--sandbox", "--ignore-user-config", "--ignore-rules",
        "--skip-git-repo-check", "--json", "--output-schema",
        "--output-last-message", "--model", "--cd", "--color", "-c", "--config"
    ]
    if os.environ.get("FAKE_CODEX_HELP_MODE") == "unsupported":
        flags.remove("--ignore-rules")
    append_invocation("exec_help_probe")
    print("Usage: codex exec " + " ".join(flags))
    raise SystemExit(0)

if args == ["app-server", "--help"]:
    append_invocation("app_server_help_probe")
    print("Usage: codex app-server --strict-config --listen <URI> -c <key=value>")
    raise SystemExit(0)

if args and args[0] == "app-server":
    stdin_text = sys.stdin.read()
    items = config_items()
    append_invocation(
        "app_server_config_probe",
        stdin_text=stdin_text,
        config_items=items,
    )
    if "--strict-config" not in args or "--listen" not in args:
        print("missing strict app-server probe flags", file=sys.stderr)
        raise SystemExit(2)
    unsupported = {
        value.strip()
        for value in os.environ.get(
            "FAKE_CODEX_UNSUPPORTED_CONFIG_KEY", ""
        ).split(",")
        if value.strip()
    }
    rejected = sorted(
        key for key, _value in items if key in unsupported
    )
    if rejected:
        print("unknown config key: " + ",".join(rejected), file=sys.stderr)
        raise SystemExit(2)
    if stdin_text:
        print("app-server capability probe stdin must be empty", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0)

if args and args[:2] == ["features", "list"]:
    items = config_items()
    append_invocation("features_list_probe", config_items=items)
    config = config_map(items)
    for key, value in sorted(config.items()):
        if key.startswith("features."):
            print("{} stable {}".format(
                key.removeprefix("features."),
                "true" if value is True else "false",
            ))
    raise SystemExit(0)

if args == ["login", "status"]:
    append_invocation("login_status_probe")
    mode = os.environ.get("FAKE_CODEX_AUTH", "chatgpt")
    if mode == "chatgpt":
        print("Logged in using ChatGPT")
        raise SystemExit(0)
    if mode == "api_key":
        print("Logged in using an API key")
        raise SystemExit(0)
    print("Not logged in", file=sys.stderr)
    raise SystemExit(1)

if not args or args[0] != "exec":
    print("unsupported fake invocation", file=sys.stderr)
    raise SystemExit(9)

mode = os.environ.get("FAKE_CODEX_EXEC_MODE", "normal")
prompt = sys.stdin.read()
items = config_items()
append_invocation("model_turn", stdin_text=prompt, config_items=items)

def arg_value(flag):
    index = args.index(flag)
    return args[index + 1]

output_path = pathlib.Path(arg_value("--output-last-message"))
schema_path = pathlib.Path(arg_value("--output-schema"))
capture_path = os.environ.get("FAKE_CODEX_CAPTURE")
if capture_path:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    explicit_config = config_map(items)
    user_config = json.loads(os.environ.get("FAKE_CODEX_USER_CONFIG_JSON", "{}"))
    effective_config = {}
    if "--ignore-user-config" not in args:
        effective_config.update(user_config)
    effective_config.update(explicit_config)
    codex_home = pathlib.Path(
        os.environ.get("CODEX_HOME", pathlib.Path.cwd())
    )
    if effective_config.get("history.persistence") != "none":
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "FAKE_HISTORY_TRANSCRIPT").write_text(
            "history should have been disabled",
            encoding="utf-8",
        )
    pathlib.Path(capture_path).write_text(json.dumps({
        "argv": args,
        "cwd": os.getcwd(),
        "prompt": prompt,
        "config_items": items,
        "explicit_config": explicit_config,
        "effective_config": effective_config,
        "ignore_user_config": "--ignore-user-config" in args,
        "schema_additional_properties": schema.get("additionalProperties"),
        "has_openai_api_key": "OPENAI_API_KEY" in os.environ,
        "has_codex_api_key": "CODEX_API_KEY" in os.environ,
        "has_anthropic_api_key": "ANTHROPIC_API_KEY" in os.environ,
        "has_github_token": "GITHUB_TOKEN" in os.environ,
        "has_database_password": "DATABASE_PASSWORD" in os.environ,
        "has_pwd": "PWD" in os.environ,
        "has_oldpwd": "OLDPWD" in os.environ,
        "codex_home_preserved": os.environ.get("CODEX_HOME"),
    }, sort_keys=True), encoding="utf-8")

if mode == "timeout":
    time.sleep(5)
    raise SystemExit(0)
if mode == "nonzero":
    print("generic local CLI failure", file=sys.stderr)
    raise SystemExit(7)
if mode == "usage_limit":
    print("Usage limit reached", file=sys.stderr)
    raise SystemExit(8)
if mode == "model_unavailable":
    print("model not available", file=sys.stderr)
    raise SystemExit(8)
if mode == "stdout_too_large":
    print("x" * 200000)
    raise SystemExit(0)

decision = {
    "reasoning": "PRIVATE_RATIONALE_MARKER",
    "sentiment": 0.25,
    "public_take": "Waiting for confirmation.",
    "action": "hold",
    "quantity": 0,
    "limit_price": None,
}
if mode == "schema_mismatch":
    decision["unexpected"] = True
if mode == "missing_public_take":
    decision.pop("public_take")
if mode == "sensitive":
    decision["reasoning"] = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
if mode == "final_too_large":
    decision["reasoning"] = "r" * 200000

final_text = json.dumps(decision, sort_keys=True, separators=(",", ":"))
event_text = final_text
if mode == "event_mismatch":
    event_decision = dict(decision)
    event_decision["sentiment"] = -0.25
    event_text = json.dumps(event_decision, sort_keys=True, separators=(",", ":"))
reported_model = "different-codex-model" if mode == "model_mismatch" else "test-codex-model"
events = [
    {"type": "thread.started", "thread_id": "fake-thread", "model": reported_model},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "reasoning"}},
]
if mode == "reasoning_text":
    events.append({
        "type": "reasoning",
        "text": "DIRECT_REASONING_EVENT_MUST_NOT_ESCAPE",
    })
tool_type = {
    "command": "command_execution",
    "file": "file_change",
    "apply_patch": "apply_patch",
    "mcp": "mcp_tool_call",
    "app": "app_call",
    "connector": "connector_call",
    "web": "web_search",
    "image": "image_tool_call",
    "computer": "computer_use",
    "permission": "privilege_escalation",
    "request_permissions": "request_permissions",
}.get(mode)
if tool_type:
    events.append({
        "type": "item.completed",
        "item": {
            "type": tool_type,
            "id": "forbidden",
            "output": "FORBIDDEN_TOOL_OUTPUT_MUST_NOT_ESCAPE",
        },
    })
if mode not in {"missing_output", "malformed_jsonl"}:
    events.append({"type": "item.completed", "item": {"type": "agent_message", "text": event_text}})
events.append({
    "type": "turn.completed",
    "usage": {
        "input_tokens": 101,
        "cached_input_tokens": 17,
        "output_tokens": 29,
        "reasoning_output_tokens": 11,
    },
})

if mode == "malformed_jsonl":
    print("{definitely-not-json")
else:
    for event in events:
        print(json.dumps(event, sort_keys=True))

if mode != "missing_output":
    if mode == "invalid_utf8":
        output_path.write_bytes(b"\xff\xfe")
    else:
        output_path.write_text(final_text, encoding="utf-8")
'''


class CodexExecProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        # Shell metacharacters and spaces expose accidental shell=True use.
        fake_dir = self.root / "fake codex; no-shell"
        fake_dir.mkdir()
        self.binary = fake_dir / "codex fake"
        self.binary.write_text(textwrap.dedent(_FAKE_CODEX), encoding="utf-8")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.capture = self.root / "capture.json"
        self.invocation_log = self.root / "invocations.jsonl"
        self.base_environment = dict(os.environ)
        self.base_environment.update(
            {
                "FAKE_CODEX_CAPTURE": str(self.capture),
                "FAKE_CODEX_INVOCATION_LOG": str(self.invocation_log),
                "FAKE_CODEX_AUTH": "chatgpt",
                "CODEX_HOME": str(self.root / "managed-codex-home"),
            }
        )
        self.probe = probe_codex_cli(
            self.binary,
            reasoning_effort="low",
            environment=self.base_environment,
            temp_root=self.root,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provider(self, mode: str = "normal", **kwargs) -> CodexExecLLM:
        environment = dict(self.base_environment)
        environment["FAKE_CODEX_EXEC_MODE"] = mode
        return CodexExecLLM(
            model="test-codex-model",
            reasoning_effort="low",
            binary=self.binary,
            environment=environment,
            temp_root=self.root,
            project_root=pathlib.Path.cwd(),
            probe=self.probe,
            **kwargs,
        )

    def invocations(self) -> list[dict]:
        if not self.invocation_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.invocation_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def model_turn_count(self) -> int:
        return sum(
            row.get("kind") == "model_turn" for row in self.invocations()
        )

    def invocation_config(
        self, kind: str, *, last: bool = True
    ) -> dict[str, object]:
        matching = [
            row for row in self.invocations() if row.get("kind") == kind
        ]
        self.assertTrue(matching, "missing fake Codex invocation {!r}".format(kind))
        rows = [matching[-1]] if last else matching
        result: dict[str, object] = {}
        for row in rows:
            for key, value in row.get("config_items", []):
                result[key] = _decode_cli_config_value(value)
        return result

    def test_version_capability_and_chatgpt_auth_probe(self) -> None:
        self.assertEqual(self.probe.cli_version, "0.144.4")
        self.assertEqual(self.probe.auth_mode, "chatgpt_managed_codex")
        self.assertTrue(self.probe.auth_verified)
        self.assertRegex(self.probe.binary_sha256, r"^[0-9a-f]{64}$")
        self.assertIn("--output-schema", self.probe.supported_exec_flags)
        self.assertIn("--ignore-user-config", self.probe.supported_exec_flags)
        self.assertIn("--strict-config", self.probe.supported_exec_flags)
        self.assertIn("--color", self.probe.supported_exec_flags)
        self.assertTrue(self.probe.tool_surface_verified)
        self.assertIn(
            "model_reasoning_effort", self.probe.supported_config_keys
        )
        invocation_kinds = [row["kind"] for row in self.invocations()]
        self.assertIn("app_server_help_probe", invocation_kinds)
        self.assertIn("app_server_config_probe", invocation_kinds)
        self.assertIn("login_status_probe", invocation_kinds)
        self.assertEqual(self.model_turn_count(), 0)
        probed = self.invocation_config(
            "app_server_config_probe", last=False
        )
        for key, value in _EXPECTED_NO_TOOLS_CONFIG.items():
            self.assertEqual(probed.get(key), value, key)
        self.assertEqual(probed.get("model_reasoning_effort"), "low")
        self.assertTrue(
            all(
                row.get("stdin") == ""
                for row in self.invocations()
                if row.get("kind") == "app_server_config_probe"
            )
        )

    def test_missing_safe_capability_fails_closed(self) -> None:
        environment = dict(self.base_environment)
        environment["FAKE_CODEX_HELP_MODE"] = "unsupported"
        with self.assertRaises(CodexExecError) as raised:
            probe_codex_cli(
                self.binary, environment=environment, temp_root=self.root
            )
        self.assertEqual(raised.exception.code, "unsupported_codex_cli_version")
        self.assertIn("--ignore-rules", raised.exception.public_metadata["missing_flags"])

    def test_each_required_no_tools_config_failure_is_fail_closed_without_turn(
        self,
    ) -> None:
        environment = dict(self.base_environment)
        environment["FAKE_CODEX_UNSUPPORTED_CONFIG_KEY"] = ",".join(
            sorted(_EXPECTED_NO_TOOLS_CONFIG)
        )
        with self.assertRaises(CodexExecError) as raised:
            probe_codex_cli(
                self.binary,
                environment=environment,
                temp_root=self.root,
            )
        self.assertEqual(
            raised.exception.code,
            "codex_tool_surface_cannot_be_disabled",
        )
        self.assertEqual(
            set(raised.exception.public_metadata["unsupported_controls"]),
            set(_EXPECTED_NO_TOOLS_CONFIG),
        )
        self.assertNotIn(
            "FORBIDDEN_TOOL_OUTPUT_MUST_NOT_ESCAPE",
            str(raised.exception),
        )
        self.assertEqual(self.model_turn_count(), 0)

    def test_api_key_auth_and_unauthenticated_modes_are_distinct(self) -> None:
        for mode, expected in (
            ("api_key", "auth_mode_not_chatgpt"),
            ("none", "codex_not_authenticated"),
        ):
            environment = dict(self.base_environment)
            environment["FAKE_CODEX_AUTH"] = mode
            with self.subTest(mode=mode):
                with self.assertRaises(CodexExecError) as raised:
                    probe_codex_cli(
                        self.binary, environment=environment, temp_root=self.root
                    )
                self.assertEqual(raised.exception.code, expected)

    def test_real_provider_requires_explicit_model_and_reasoning_effort(
        self,
    ) -> None:
        cases = (
            {"model": "", "reasoning_effort": "low"},
            {"model": "test-codex-model", "reasoning_effort": ""},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CodexExecLLM(
                    binary=self.binary,
                    environment=self.base_environment,
                    temp_root=self.root,
                    project_root=pathlib.Path.cwd(),
                    probe=self.probe,
                    **kwargs,
                )
        self.assertEqual(self.model_turn_count(), 0)

    def test_conflicting_api_environment_is_removed_but_codex_home_is_preserved(self) -> None:
        environment = dict(self.base_environment)
        environment.update(
            {
                "OPENAI_API_KEY": "OPENAI_SECRET_VALUE",
                "CODEX_API_KEY": "CODEX_SECRET_VALUE",
                "ANTHROPIC_API_KEY": "ANTHROPIC_SECRET_VALUE",
                "SOME_NEW_API_KEY": "OTHER_SECRET_VALUE",
                "GITHUB_TOKEN": "GITHUB_SECRET_VALUE",
                "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_VALUE",
                "DATABASE_PASSWORD": "DATABASE_SECRET_VALUE",
                "PWD": "/private/repository/path",
                "OLDPWD": "/private/previous/path",
            }
        )
        cleaned = sanitized_subprocess_environment(environment)
        for key in (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "ANTHROPIC_API_KEY",
            "SOME_NEW_API_KEY",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "DATABASE_PASSWORD",
            "PWD",
            "OLDPWD",
        ):
            self.assertNotIn(key, cleaned)
        self.assertEqual(cleaned["CODEX_HOME"], environment["CODEX_HOME"])
        provider = CodexExecLLM(
            model="test-codex-model",
            reasoning_effort="low",
            binary=self.binary,
            environment=environment,
            temp_root=self.root,
            project_root=pathlib.Path.cwd(),
            probe=self.probe,
        )
        provider.complete("system", "user")
        capture = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertFalse(capture["has_openai_api_key"])
        self.assertFalse(capture["has_codex_api_key"])
        self.assertFalse(capture["has_anthropic_api_key"])
        self.assertFalse(capture["has_github_token"])
        self.assertFalse(capture["has_database_password"])
        self.assertFalse(capture["has_pwd"])
        self.assertFalse(capture["has_oldpwd"])
        self.assertEqual(capture["codex_home_preserved"], environment["CODEX_HOME"])

    def test_normal_jsonl_schema_usage_and_final_message(self) -> None:
        provider = self.provider()
        response = provider.complete("PRIVATE_SYSTEM", "PRIVATE_USER")
        parsed = json.loads(response)
        self.assertEqual(parsed["action"], "hold")
        self.assertEqual(parsed["quantity"], 0)
        self.assertEqual(parsed["reasoning"], "PRIVATE_RATIONALE_MARKER")
        self.assertEqual(provider.provider_calls_attempted, 1)
        self.assertEqual(provider.provider_calls_succeeded, 1)
        self.assertEqual(provider.provider_calls_failed, 0)
        self.assertEqual(len(provider.call_metadata_history), 1)
        self.assertIs(provider.call_metadata_history[0], provider.last_call_metadata)
        self.assertTrue(provider.network_access)
        metadata = provider.last_call_metadata
        assert metadata is not None
        self.assertEqual(metadata["status"], "finished")
        self.assertEqual(metadata["actual_model_verification"], "verified")
        self.assertEqual(metadata["requested_model"], "test-codex-model")
        self.assertEqual(metadata["reasoning_effort"], "low")
        self.assertTrue(metadata["provider_transport_network_expected"])
        self.assertIsInstance(
            metadata["provider_transport_network_declared_or_observed"], str
        )
        self.assertTrue(
            metadata["provider_transport_network_declared_or_observed"]
        )
        self.assertFalse(metadata["agent_tool_network_enabled"])
        self.assertEqual(metadata["web_search_mode"], "disabled")
        self.assertFalse(metadata["shell_tool_enabled"])
        self.assertFalse(metadata["unified_exec_enabled"])
        self.assertFalse(metadata["apps_enabled"])
        self.assertFalse(metadata["view_image_enabled"])
        self.assertEqual(metadata["tool_calls_observed"], 0)
        self.assertEqual(metadata["reasoning_event_count"], 1)
        self.assertFalse(metadata["effective_config_anomaly"])
        self.assertEqual(metadata["history_persistence"], "none")
        self.assertTrue(metadata["agent_reasoning_events_hidden"])
        self.assertEqual(metadata["personality"], "none")
        self.assertEqual(
            metadata["usage"],
            {
                "input_tokens": 101,
                "cached_input_tokens": 17,
                "output_tokens": 29,
                "reasoning_tokens": 11,
            },
        )
        self.assertEqual(metadata["event_type_counts"]["turn.completed"], 1)
        self.assertRegex(metadata["final_response_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(CODEX_DECISION_SCHEMA_VERSION, "1.0")
        self.assertRegex(CODEX_DECISION_SCHEMA_HASH, r"^[0-9a-f]{64}$")

    def test_prompt_uses_stdin_shell_false_and_isolated_cleaned_cwd(self) -> None:
        provider = self.provider(run_id="../../unsafe run", agent_id="agent/../../x")
        real_popen = codex_exec_module.subprocess.Popen
        with mock.patch.object(
            codex_exec_module.subprocess, "Popen", wraps=real_popen
        ) as popen:
            provider.complete("SYSTEM_SENTINEL", "USER_SENTINEL")
        self.assertFalse(popen.call_args.kwargs["shell"])
        capture = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertIn("SYSTEM_SENTINEL", capture["prompt"])
        self.assertIn("USER_SENTINEL", capture["prompt"])
        self.assertEqual(capture["argv"][-1], "-")
        isolated_cwd = pathlib.Path(capture["cwd"])
        self.assertNotEqual(isolated_cwd, pathlib.Path.cwd().resolve())
        self.assertFalse(str(isolated_cwd).startswith(str(pathlib.Path.cwd().resolve())))
        self.assertFalse(isolated_cwd.exists(), "request cwd must be cleaned after completion")
        self.assertFalse(capture["schema_additional_properties"])
        self.assertIn("--strict-config", capture["argv"])

    def test_model_turn_has_explicit_no_tools_config_and_reasoning_effort(
        self,
    ) -> None:
        provider = self.provider()
        provider.complete("system", "user")
        capture = json.loads(self.capture.read_text(encoding="utf-8"))
        effective = capture["effective_config"]
        for key, value in _EXPECTED_NO_TOOLS_CONFIG.items():
            self.assertEqual(effective.get(key), value, key)
        self.assertEqual(effective.get("model_reasoning_effort"), "low")
        self.assertTrue(capture["ignore_user_config"])
        self.assertIn("--ignore-user-config", capture["argv"])
        self.assertIn("--ignore-rules", capture["argv"])
        self.assertIn("--ephemeral", capture["argv"])
        self.assertFalse(
            (pathlib.Path(self.base_environment["CODEX_HOME"])
             / "FAKE_HISTORY_TRANSCRIPT").exists()
        )
        self.assertEqual(self.model_turn_count(), 1)

    def test_malicious_user_config_cannot_relax_explicit_safety_controls(
        self,
    ) -> None:
        environment = dict(self.base_environment)
        environment["FAKE_CODEX_EXEC_MODE"] = "normal"
        environment["FAKE_CODEX_USER_CONFIG_JSON"] = json.dumps(
            {
                "forced_login_method": "api",
                "approval_policy": "on-request",
                "sandbox_mode": "danger-full-access",
                "web_search": "live",
                "history.persistence": "save-all",
                "hide_agent_reasoning": False,
                "show_raw_agent_reasoning": True,
                "personality": "friendly",
                "features.shell_tool": True,
                "features.unified_exec": True,
                "features.apps": True,
                "tools.view_image": True,
            },
            sort_keys=True,
        )
        provider = CodexExecLLM(
            model="test-codex-model",
            reasoning_effort="low",
            binary=self.binary,
            environment=environment,
            temp_root=self.root,
            project_root=pathlib.Path.cwd(),
            probe=self.probe,
        )
        provider.complete("system", "user")
        capture = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertTrue(capture["ignore_user_config"])
        for key, value in _EXPECTED_NO_TOOLS_CONFIG.items():
            self.assertEqual(capture["effective_config"].get(key), value, key)
        self.assertFalse(
            (pathlib.Path(environment["CODEX_HOME"])
             / "FAKE_HISTORY_TRANSCRIPT").exists()
        )

    def test_wrapper_and_request_hashes_are_explicit_and_private_safe(self) -> None:
        first = codex_request_identity(
            "SYSTEM_RAW_CONTENT_SENTINEL",
            "USER_RAW_CONTENT_SENTINEL",
            "test-codex-model",
            reasoning_effort="low",
        )
        second = codex_request_identity(
            "SYSTEM_RAW_CONTENT_SENTINEL",
            "changed",
            "test-codex-model",
            reasoning_effort="low",
        )
        changed_effort = codex_request_identity(
            "SYSTEM_RAW_CONTENT_SENTINEL",
            "USER_RAW_CONTENT_SENTINEL",
            "test-codex-model",
            reasoning_effort="high",
        )
        self.assertEqual(
            first["codex_wrapper_protocol_version"], CODEX_WRAPPER_PROTOCOL_VERSION
        )
        self.assertEqual(first["wrapper_source_hash"], CODEX_WRAPPER_SOURCE_HASH)
        self.assertNotEqual(
            first["final_combined_input_hash"], second["final_combined_input_hash"]
        )
        self.assertEqual(first["reasoning_effort"], "low")
        self.assertNotEqual(
            first["tool_surface_contract_hash"],
            changed_effort["tool_surface_contract_hash"],
        )
        encoded = json.dumps(first, sort_keys=True)
        self.assertNotIn("SYSTEM_RAW_CONTENT_SENTINEL", encoded)
        self.assertNotIn("USER_RAW_CONTENT_SENTINEL", encoded)

    def test_static_identity_does_not_probe_auth_or_start_a_subprocess(self) -> None:
        with mock.patch.object(
            codex_exec_module.subprocess,
            "Popen",
            side_effect=AssertionError("static identity must not start a subprocess"),
        ):
            identity = codex_static_adapter_identity(
                self.binary,
                model="test-codex-model",
                reasoning_effort="low",
            )
            missing = codex_static_adapter_identity(
                self.root / "missing-codex",
                model="test-codex-model",
                reasoning_effort="low",
            )
        self.assertEqual(identity["binary_identity"]["status"], "available")
        self.assertRegex(identity["binary_identity"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(missing["binary_identity"]["status"], "missing")
        self.assertIsNone(missing["binary_identity"]["sha256"])
        self.assertFalse(identity["auth_probe_performed"])
        self.assertFalse(identity["subprocess_started"])
        self.assertEqual(identity["reasoning_effort"], "low")
        self.assertRegex(identity["tool_surface_contract_hash"], r"^[0-9a-f]{64}$")
        for key, value in _EXPECTED_NO_TOOLS_CONFIG.items():
            self.assertEqual(
                identity["tool_surface_contract"]["required_config"].get(key),
                value,
                key,
            )
        self.assertNotIn(str(self.binary.resolve()), json.dumps(identity, sort_keys=True))

    def test_codex_identity_enters_model_request_hash_without_changing_defaults(self) -> None:
        default_contract = build_effective_config_contract(Config())
        # The full identity intentionally includes the resolved relative
        # ``out_dir`` path, so its exact digest changes across valid checkout
        # locations.  Pin the path-independent scientific/model categories
        # below and retain structural coverage for the full/execution identity.
        self.assertEqual(default_contract["config_hash_schema_version"], "1.0")
        self.assertRegex(default_contract["full_effective_config_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(default_contract["execution_config_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            default_contract["execution_config_summary"]["config_fields"]["out_dir"][
                "kind"
            ],
            "path_identity",
        )
        self.assertEqual(
            default_contract["scientific_config_hash"],
            "0d405ada90b2d8a3a3580e1b52db6a9ec23bfd35909b259a7ea8dace09542a5c",
        )
        self.assertEqual(
            default_contract["model_request_config_hash"],
            "3ebfb99a377b7184878b779fdbd252e2711dd847f167a3f85567478a11e6250a",
        )

        cfg = Config(provider="codex_exec", model="test-codex-model")
        with mock.patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "codex_exec",
                "LLM_MODEL": "test-codex-model",
                "NMSIM_CODEX_EXECUTABLE": str(self.binary),
                "NMSIM_CODEX_REASONING_EFFORT": "low",
            },
        ), mock.patch.object(
            codex_exec_module.subprocess,
            "Popen",
            side_effect=AssertionError("config identity must not start Codex"),
        ):
            contract = build_effective_config_contract(cfg, base_dir=self.root)
        adapter = contract["model_request_config_summary"][
            "_provider_adapter_contract"
        ]
        self.assertEqual(adapter["wrapper_source_hash"], CODEX_WRAPPER_SOURCE_HASH)
        self.assertEqual(adapter["decision_schema_hash"], CODEX_DECISION_SCHEMA_HASH)
        self.assertEqual(adapter["binary_identity"]["sha256"], self.probe.binary_sha256)
        self.assertEqual(adapter["reasoning_effort"], "low")
        self.assertRegex(adapter["tool_surface_contract_hash"], r"^[0-9a-f]{64}$")
        self.assertFalse(adapter["agent_tool_network_enabled"])
        self.assertFalse(adapter["subprocess_started"])
        self.assertNotEqual(
            contract["model_request_config_hash"],
            default_contract["model_request_config_hash"],
        )

    def test_result_reuse_expected_identity_binds_codex_binary_wrapper_and_model(self) -> None:
        cfg = Config(
            provider="codex_exec",
            model="test-codex-model",
            reference_path="nmsim/meta_feb2022_reference.csv",
        )
        environment = {
            "LLM_PROVIDER": "codex_exec",
            "LLM_MODEL": "test-codex-model",
            "NMSIM_CODEX_EXECUTABLE": str(self.binary),
            "NMSIM_CODEX_REASONING_EFFORT": "low",
        }
        with mock.patch.dict(os.environ, environment):
            first = ExpectedRunIdentity.from_effective_config(
                cfg,
                command_identity="python -m experiments.run_seed",
                required_artifacts=("experiment_result.json",),
                base_dir=pathlib.Path.cwd(),
            )
            first_contract = build_effective_config_contract(cfg)
            adapter = first_contract["model_request_config_summary"][
                "_provider_adapter_contract"
            ]
            self.assertEqual(first.requested_provider, "codex_exec")
            self.assertEqual(first.resolved_provider, "codex_exec")
            self.assertEqual(first.resolved_model, "test-codex-model")
            self.assertEqual(adapter["wrapper_source_hash"], CODEX_WRAPPER_SOURCE_HASH)
            self.assertEqual(adapter["decision_schema_hash"], CODEX_DECISION_SCHEMA_HASH)
            self.assertEqual(adapter["reasoning_effort"], "low")
            self.assertFalse(adapter["agent_tool_network_enabled"])
            self.assertRegex(
                adapter["tool_surface_contract_hash"], r"^[0-9a-f]{64}$"
            )

            high_environment = dict(environment)
            high_environment["NMSIM_CODEX_REASONING_EFFORT"] = "high"
            with mock.patch.dict(os.environ, high_environment):
                changed_effort = ExpectedRunIdentity.from_effective_config(
                    cfg,
                    command_identity="python -m experiments.run_seed",
                    required_artifacts=("experiment_result.json",),
                    base_dir=pathlib.Path.cwd(),
                )
            self.assertNotEqual(
                first.model_request_config_hash,
                changed_effort.model_request_config_hash,
            )

            self.binary.write_text(
                self.binary.read_text(encoding="utf-8") + "\n# identity change\n",
                encoding="utf-8",
            )
            second = ExpectedRunIdentity.from_effective_config(
                cfg,
                command_identity="python -m experiments.run_seed",
                required_artifacts=("experiment_result.json",),
                base_dir=pathlib.Path.cwd(),
            )
        self.assertNotEqual(
            first.model_request_config_hash, second.model_request_config_hash
        )

    def test_managed_record_and_replay_bind_codex_without_replay_process(self) -> None:
        cfg = Config(
            provider="codex_exec",
            model="test-codex-model",
            cache_enabled=False,
            out_dir=str(self.root / "managed"),
        )
        environment = {
            **self.base_environment,
            "LLM_PROVIDER": "codex_exec",
            "LLM_MODEL": "test-codex-model",
            "NMSIM_CODEX_EXECUTABLE": str(self.binary),
            "NMSIM_CODEX_REASONING_EFFORT": "low",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            recorded = ManagedRunContext.create(
                cfg,
                out_root=cfg.out_dir,
                run_id="codex-record",
                command_identity="test:codex-managed-record",
                run_kind="diagnostic",
                planned_simulation_runs=0,
            )
            with recorded:
                llm, _tracker = recorded.prepare_llm()
                llm.set_batch_context(
                    1, [{"agent_id": "agent-a", "persona_id": "persona-a"}]
                )
                expected = llm.complete("system", "user")
                recorded.finish()

            record = json.loads(
                (recorded.run_dir / "llm_records.jsonl").read_text(encoding="utf-8")
            )
            adapter_request = record["request"]["provider_adapter_identity"]
            self.assertEqual(
                adapter_request["final_combined_input_hash"],
                codex_request_identity(
                    "system",
                    "user",
                    "test-codex-model",
                    reasoning_effort="low",
                )["final_combined_input_hash"],
            )
            self.assertEqual(record["schema_version"], "1.2")
            self.assertIn("provider_adapter_contract", record["model_config"])
            self.assertIn("provider_runtime_identity", record["model_config"])

            calls_after_record = json.loads(
                (recorded.run_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            codex_manifest = calls_after_record["llm"]["codex_exec"]
            provider_options = calls_after_record["llm"][
                "provider_request_options"
            ]["codex_exec"]
            self.assertEqual(
                provider_options["classification"],
                "provider_specific_model_request_options_outside_Config",
            )
            self.assertEqual(provider_options["reasoning_effort"], "low")
            self.assertRegex(
                calls_after_record["llm"][
                    "provider_request_options_sha256"
                ],
                r"^[0-9a-f]{64}$",
            )
            self.assertTrue(
                codex_manifest["provider_transport_network_expected"]
            )
            self.assertEqual(
                codex_manifest[
                    "provider_transport_network_declared_or_observed"
                ],
                "process_started_network_not_observed",
            )
            self.assertFalse(codex_manifest["agent_tool_network_enabled"])
            self.assertEqual(codex_manifest["web_search_mode"], "disabled")
            self.assertEqual(
                len(codex_manifest["call_history"]), 1
            )
            self.assertEqual(
                codex_manifest["call_history"][0][
                    "final_response_hash"
                ],
                codex_manifest["last_call"][
                    "final_response_hash"
                ],
            )
            calls_after_record = calls_after_record["completion"]["provider_calls"]
            self.assertEqual(calls_after_record["attempted"], 1)
            self.assertEqual(calls_after_record["succeeded"], 1)
            fake_turns_after_record = self.model_turn_count()

            replayed = ManagedRunContext.create(
                cfg,
                out_root=cfg.out_dir,
                run_id="codex-replay",
                command_identity="test:codex-managed-replay",
                run_kind="diagnostic",
                planned_simulation_runs=0,
            )
            with mock.patch.object(
                codex_exec_module,
                "_run_limited_process",
                side_effect=AssertionError("replay must not start Codex"),
            ):
                with replayed:
                    replay_llm, _tracker = replayed.prepare_llm(recorded.run_dir)
                    replay_llm.set_batch_context(
                        1,
                        [{"agent_id": "agent-a", "persona_id": "persona-a"}],
                    )
                    self.assertEqual(replay_llm.complete("system", "user"), expected)
                    replayed.assert_replay_exhausted()
                    replayed.finish()
            replay_manifest = json.loads(
                (replayed.run_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                replay_manifest["completion"]["provider_calls"]["attempted"], 0
            )
            self.assertFalse(replay_manifest["llm"]["runtime"]["network_access"])
            self.assertFalse(
                replay_manifest["llm"]["codex_exec"]["subprocess_started_this_run"]
            )
            self.assertFalse(
                replay_manifest["llm"]["codex_exec"]["auth_checked_this_run"]
            )
            replay_contract = replay_manifest["llm"]["codex_exec"][
                "provider_adapter_contract"
            ]
            self.assertEqual(replay_contract["reasoning_effort"], "low")
            self.assertFalse(replay_contract["agent_tool_network_enabled"])
            self.assertEqual(
                replay_contract["history_persistence"], "none"
            )
            self.assertEqual(self.model_turn_count(), fake_turns_after_record)

    def test_timeout_nonzero_model_and_usage_limit_errors_are_distinct(self) -> None:
        cases = (
            ("timeout", "subprocess_timeout", {"timeout_seconds": 0.1}),
            ("nonzero", "subprocess_nonzero_exit", {}),
            ("model_unavailable", "model_not_available", {}),
            ("usage_limit", "usage_limit_reached", {}),
        )
        for mode, expected, kwargs in cases:
            with self.subTest(mode=mode):
                provider = self.provider(mode, **kwargs)
                with self.assertRaises(CodexExecError) as raised:
                    provider.complete("system", "user")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(provider.provider_calls_succeeded, 0)
                self.assertEqual(provider.provider_calls_failed, 1)
                self.assertIsNotNone(
                    provider.last_call_metadata["latency_seconds"]
                )
                self.assertIsNotNone(
                    provider.last_call_metadata["process_exit_code"]
                )

    def test_process_launch_failure_does_not_claim_started_or_network_observed(
        self,
    ) -> None:
        provider = self.provider()
        with mock.patch.object(
            codex_exec_module.subprocess,
            "Popen",
            side_effect=OSError("safe launch failure"),
        ), self.assertRaises(CodexExecError) as raised:
            provider.complete("system", "user")
        self.assertEqual(raised.exception.code, "subprocess_nonzero_exit")
        self.assertFalse(provider.network_access)
        self.assertFalse(provider.model_turn_process_started)
        metadata = provider.last_call_metadata
        assert metadata is not None
        self.assertFalse(metadata["model_turn_subprocess_started"])
        self.assertEqual(
            metadata["provider_transport_network_declared_or_observed"],
            "declared_expected_process_launch_failed",
        )

    def test_malformed_jsonl_missing_output_and_output_caps(self) -> None:
        cases = (
            ("malformed_jsonl", "json_event_stream_invalid", {}),
            ("missing_output", "output_missing", {}),
            ("invalid_utf8", "schema_validation_failed", {}),
            ("stdout_too_large", "output_too_large", {"max_stdout_bytes": 1024}),
            ("final_too_large", "output_too_large", {"max_final_message_bytes": 1024}),
        )
        for mode, expected, kwargs in cases:
            with self.subTest(mode=mode):
                provider = self.provider(mode, **kwargs)
                with self.assertRaises(CodexExecError) as raised:
                    provider.complete("system", "user")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(provider.provider_calls_attempted, 1)
                self.assertEqual(provider.provider_calls_failed, 1)
                self.assertEqual(provider.last_call_metadata["status"], "failed")

    def test_output_file_and_event_message_must_agree(self) -> None:
        provider = self.provider("event_mismatch")
        with self.assertRaises(CodexExecError) as raised:
            provider.complete("system", "user")
        self.assertEqual(raised.exception.code, "json_event_stream_invalid")
        self.assertEqual(provider.provider_calls_succeeded, 0)

    def test_reported_model_mismatch_fails_before_decision_is_consumed(self) -> None:
        provider = self.provider("model_mismatch")
        with self.assertRaises(CodexExecError) as raised:
            provider.complete("system", "user")
        self.assertEqual(raised.exception.code, "reported_model_mismatch")
        self.assertEqual(provider.provider_calls_succeeded, 0)
        self.assertEqual(
            provider.last_call_metadata["actual_model_verification"], "mismatch"
        )

    def test_schema_mismatch_and_missing_public_take_never_fallback(self) -> None:
        for mode in ("schema_mismatch", "missing_public_take"):
            with self.subTest(mode=mode):
                provider = self.provider(mode)
                with self.assertRaises(CodexExecError) as raised:
                    provider.complete("system", "user")
                self.assertEqual(raised.exception.code, "schema_validation_failed")
                self.assertNotIn("PRIVATE_RATIONALE_MARKER", str(raised.exception))

    def test_every_tool_operation_is_rejected_without_consuming_decision(self) -> None:
        for mode, event_type in (
            ("command", "command_execution"),
            ("file", "file_change"),
            ("apply_patch", "apply_patch"),
            ("mcp", "mcp_tool_call"),
            ("app", "app_call"),
            ("connector", "connector_call"),
            ("web", "web_search"),
            ("image", "image_tool_call"),
            ("computer", "computer_use"),
            ("permission", "privilege_escalation"),
            ("request_permissions", "request_permissions"),
        ):
            with self.subTest(mode=mode):
                provider = self.provider(mode)
                with self.assertRaises(CodexExecError) as raised:
                    provider.complete("system", "user")
                self.assertEqual(raised.exception.code, "tool_use_violation")
                self.assertEqual(
                    raised.exception.public_metadata["event_type"], event_type
                )
                self.assertEqual(provider.provider_calls_succeeded, 0)
                self.assertEqual(
                    provider.last_call_metadata["tool_use_violation_count"], 1
                )
                self.assertEqual(
                    provider.last_call_metadata["tool_calls_observed"], 1
                )
                public = "{}\n{}".format(
                    raised.exception,
                    json.dumps(provider.last_call_metadata, sort_keys=True),
                )
                self.assertNotIn(
                    "FORBIDDEN_TOOL_OUTPUT_MUST_NOT_ESCAPE", public
                )

    def test_sensitive_output_is_rejected_and_never_disclosed_by_error(self) -> None:
        provider = self.provider("sensitive")
        with self.assertRaises(CodexExecError) as raised:
            provider.complete("system", "user")
        self.assertEqual(raised.exception.code, "sensitive_output_detected")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(raised.exception))
        self.assertNotIn(
            "abcdefghijklmnopqrstuvwxyz",
            json.dumps(provider.last_call_metadata, sort_keys=True),
        )

    def test_private_rationale_and_internal_progress_do_not_enter_public_metadata(self) -> None:
        provider = self.provider("reasoning_text")
        with self.assertRaises(CodexExecError) as raised:
            provider.complete("system-private", "user-private")
        self.assertEqual(
            raised.exception.code, "codex_reasoning_visibility_violation"
        )
        public = "{}\n{}".format(
            raised.exception,
            json.dumps(provider.last_call_metadata, sort_keys=True),
        )
        self.assertNotIn("PRIVATE_RATIONALE_MARKER", public)
        self.assertNotIn("DIRECT_REASONING_EVENT_MUST_NOT_ESCAPE", public)
        self.assertNotIn("system-private", public)
        self.assertNotIn("user-private", public)
        self.assertTrue(provider.last_call_metadata["agent_reasoning_events_hidden"])
        self.assertEqual(provider.last_call_metadata["reasoning_event_count"], 1)
        self.assertTrue(provider.last_call_metadata["effective_config_anomaly"])
        self.assertEqual(provider.provider_calls_succeeded, 0)

    def test_module_never_names_or_reads_codex_auth_file(self) -> None:
        source = inspect.getsource(codex_exec_module)
        forbidden_literal = "auth" + ".json"
        self.assertNotIn(forbidden_literal, source.lower())
        snapshot = self.provider().identity_snapshot()
        encoded = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn(str(self.root / "managed-codex-home"), encoded)

    def test_local_schema_validator_rejects_hold_quantity_and_nonfinite_values(self) -> None:
        base = {
            "reasoning": "private",
            "sentiment": 0.0,
            "public_take": "public",
            "action": "hold",
            "quantity": 0,
            "limit_price": None,
        }
        self.assertEqual(validate_codex_decision(json.dumps(base)), base)
        changed = dict(base, quantity=1)
        with self.assertRaises(CodexExecError) as raised:
            validate_codex_decision(json.dumps(changed))
        self.assertEqual(raised.exception.code, "schema_validation_failed")
        changed = dict(base, sentiment=float("nan"))
        with self.assertRaises(CodexExecError):
            validate_codex_decision(json.dumps(changed))

    def test_batch_is_sequential_wrapper_only(self) -> None:
        provider = self.provider()
        outputs = provider.complete_batch([("s1", "u1"), ("s2", "u2")])
        self.assertEqual(len(outputs), 2)
        self.assertEqual(provider.provider_calls_attempted, 2)
        self.assertEqual(provider.provider_calls_succeeded, 2)
        self.assertEqual(len(provider.call_metadata_history), 2)
        self.assertEqual(
            [item["request_sequence"] for item in provider.call_metadata_history],
            [1, 2],
        )
        self.assertEqual(provider.max_concurrency, 1)

    def test_record_then_replay_never_starts_codex_and_identity_mismatch_rejects(self) -> None:
        provider = self.provider()
        record_dir = self.root / "record"
        event_dir = self.root / "record-events"
        record_dir.mkdir()
        event_dir.mkdir()
        cfg = Config(
            provider="mock",
            model="test-codex-model",
            cache_enabled=False,
            out_dir=str(record_dir),
        )
        compatibility = build_effective_config_contract(
            cfg,
            base_dir=self.root,
            execution_context={"offline_fake_codex_test": True},
        )
        model_config = {
            **provider.identity_snapshot(),
            "provider": "codex_exec",
            "resolved_provider": "codex_exec",
            "model": provider.model,
            "temperature": None,
            "max_tokens": None,
            "cache_enabled": False,
        }
        recorder = RecordingLLM(
            provider,
            record_dir,
            model_config,
            EventLogger("fake-codex-record", event_dir),
            compatibility_metadata=compatibility,
        )
        recorder.set_batch_context(
            1, [{"agent_id": "agent-a", "persona_id": "persona-a"}]
        )
        expected = recorder.complete("system", "user")
        calls_after_record = provider.provider_calls_attempted
        fake_turns_after_record = self.model_turn_count()

        replay_events = self.root / "replay-events"
        replay_events.mkdir()
        with mock.patch.object(
            codex_exec_module,
            "_run_limited_process",
            side_effect=AssertionError("strict replay must not start Codex"),
        ):
            replay = ReplayLLM(
                record_dir,
                model_config,
                EventLogger("fake-codex-replay", replay_events),
                compatibility_metadata=compatibility,
            )
            replay.set_batch_context(
                1, [{"agent_id": "agent-a", "persona_id": "persona-a"}]
            )
            self.assertEqual(replay.complete("system", "user"), expected)
            replay.assert_exhausted()
        self.assertEqual(provider.provider_calls_attempted, calls_after_record)
        self.assertEqual(replay.records_consumed, 1)
        self.assertEqual(self.model_turn_count(), fake_turns_after_record)

        for changed_field, changed_value in (
            ("model", "different-model"),
            ("reasoning_effort", "high"),
            ("wrapper_source_hash", "0" * 64),
            ("decision_schema_hash", "1" * 64),
            ("tool_surface_contract_hash", "2" * 64),
        ):
            changed = dict(model_config)
            changed[changed_field] = changed_value
            with self.subTest(field=changed_field):
                with self.assertRaises(ReplayMismatchError):
                    ReplayLLM(
                        record_dir,
                        changed,
                        compatibility_metadata=compatibility,
                    )


if __name__ == "__main__":
    unittest.main()
