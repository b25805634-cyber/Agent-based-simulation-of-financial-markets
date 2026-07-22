"""Managed and deliberately unmanaged run-lifecycle boundaries.

``ManagedRunContext`` is instrumentation only.  It owns provenance, event
writers, Record/Replay construction, completion accounting, and terminal
finalization while leaving :func:`nmsim.sim.run_sim` free of filesystem and Git
side effects.  ``NullRunContext`` is the explicit no-provenance alternative for
tests and diagnostics.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Iterable, Mapping, Optional

from .config import Config
from .provider_attempts import safe_reported_model
from .provenance import RunManager, redact_secrets


COMPLETION_SCHEMA_VERSION = "1.1"
MANAGED_CONTEXT_SCHEMA_VERSION = "1.0"
FAILURE_STAGES = (
    "bootstrap",
    "config_validation",
    "provider_setup",
    "replay_preflight",
    "simulation",
    "result_export",
    "finalization",
)

NEW = "NEW"
ACTIVE = "ACTIVE"
FINISHED = "FINISHED"
FAILED = "FAILED"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ManagedRunLifecycleError(RuntimeError):
    """A managed lifecycle operation violated the context contract."""


def _provider_in_wrapper_chain(value: Any, provider_id: str) -> Any:
    """Find one adapter through Record/Cache wrappers without importing it."""

    current = value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "kind", None) == provider_id and callable(
            getattr(current, "identity_snapshot", None)
        ):
            return current
        current = getattr(current, "inner", None)
    return None


def validate_run_id(run_id: Optional[str]) -> Optional[str]:
    """Validate a bootstrap run id without touching the filesystem."""

    if run_id is None:
        return None
    value = str(run_id)
    if value in (".", "..") or not _RUN_ID_RE.fullmatch(value):
        raise ValueError("invalid run_id; use only letters, digits, '.', '_' and '-'")
    return value


def safe_output_root(value: Any, default: str = "outputs") -> Path:
    """Resolve the bootstrap output root or fail before creating provenance."""

    text = default if value in (None, "") else str(value)
    if "\x00" in text:
        raise ValueError("output root contains an invalid NUL character")
    path = Path(text).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def completion_template(
    *,
    planned_rounds: Optional[int],
    planned_decisions: Optional[int],
    planned_simulation_runs: Optional[int] = 1,
) -> dict[str, Any]:
    """Return the additive, explicitly-unitized completion structure."""

    return {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "simulation_runs": {
            "unit": "simulation_runs",
            "planned": planned_simulation_runs,
            "started": 0,
            "completed": 0,
            "failed": 0,
        },
        "rounds": {
            "unit": "rounds",
            "planned": planned_rounds,
            "started": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
        },
        "agent_decisions": {
            "unit": "agent_decisions",
            "planned": planned_decisions,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
        },
        "llm_logical_requests": {
            "unit": "llm_logical_requests",
            "planned": planned_decisions,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
        },
        "response_sources": {
            "unit": "final_responses",
            "provider": 0,
            "cache": 0,
            "replay": 0,
        },
        "provider_calls": {
            "unit": "logical_provider_requests_after_cache_and_replay",
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "coverage": (
                "provider-interface requests; SDK-internal retry attempts are not observable"
            ),
        },
        "application_provider_attempts": {
            "unit": "visible_adapter_loop_attempts",
            "attempted": 0,
            "responses_received": 0,
            "parse_failed_responses": 0,
            "provider_exceptions": 0,
            "retries_scheduled": 0,
            "logical_requests_with_retry": 0,
            "exhausted_logical_requests": 0,
            "reported_models": [],
            "reported_models_truncated": False,
            "coverage": (
                "OpenAI/Anthropic application retry loops only; excludes SDK, "
                "transport, proxy, and server-internal retries"
            ),
        },
        "parsing": {
            "unit": "agent_decision_parse_operations",
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "fallbacks": 0,
        },
    }


class _ManagedEventObserver:
    """Count lifecycle facts while preserving the existing EventLogger API."""

    def __init__(self, context: "ManagedRunContext", writer: Any):
        self._context = context
        self._writer = writer
        self.run_id = writer.run_id
        self.schema_version = writer.schema_version
        self.public_path = writer.public_path
        self.private_path = writer.private_path

    def emit(
        self,
        event_type: str,
        round_i: Optional[int] = None,
        agent_id: Optional[str] = None,
        data: Optional[dict] = None,
        private_data: Optional[dict] = None,
        **extra: Any,
    ) -> str:
        event_id = self._writer.emit(
            event_type,
            round_i=round_i,
            agent_id=agent_id,
            data=data,
            private_data=private_data,
            **extra,
        )
        self._context._observe_event(event_type, data or {}, private_data or {})
        return event_id

    def emit_private(
        self,
        event_type: str,
        round_i: Optional[int] = None,
        agent_id: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> str:
        return self._writer.emit_private(
            event_type, round_i=round_i, agent_id=agent_id, data=data
        )

    def observe_provider_attempt(self, context: Any, observation: Any) -> None:
        """Persist one correlated Provider attempt through the managed logger."""

        def sanitize_private(value: Any) -> Any:
            if isinstance(value, str):
                return self._context._manager._sanitize_text(
                    value, max_length=None
                )
            if isinstance(value, Mapping):
                return {
                    str(key): sanitize_private(item)
                    for key, item in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [sanitize_private(item) for item in value]
            return value

        self.emit(
            "LLMProviderAttemptObserved",
            round_i=context.round_i,
            agent_id=context.agent,
            data=observation.public_payload(context),
            private_data=sanitize_private(observation.private_payload()),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)


class ManagedRunContext:
    """One provenance-complete managed attempt with exactly one terminal state."""

    @classmethod
    def create(
        cls,
        cfg: Config,
        *,
        out_root: Optional[os.PathLike[str]] = None,
        scenario_id: Optional[str] = None,
        run_id: Optional[str] = None,
        worker_count: Optional[int] = None,
        batching: Any = None,
        input_paths: Any = None,
        repo_root: Optional[os.PathLike[str]] = None,
        command_identity: str = "library:nmsim.run.run",
        run_kind: str = "simulation",
        planned_simulation_runs: Optional[int] = None,
        full_validation_completed: bool = True,
    ) -> "ManagedRunContext":
        manager = RunManager.create(
            cfg,
            out_root=out_root,
            scenario_id=scenario_id,
            run_id=validate_run_id(run_id),
            worker_count=worker_count,
            batching=batching,
            input_paths=input_paths,
            repo_root=repo_root,
        )
        return cls(
            manager,
            command_identity=command_identity,
            run_kind=run_kind,
            planned_simulation_runs=planned_simulation_runs,
            full_validation_completed=full_validation_completed,
        )

    @classmethod
    def bootstrap_attempt(
        cls,
        *,
        out_root: os.PathLike[str],
        run_id: Optional[str],
        command_identity: str,
        repo_root: Optional[os.PathLike[str]] = None,
    ) -> "ManagedRunContext":
        """Reserve a failed-attempt directory before full Config validation.

        The provisional Config is explicitly labelled and must never be read as
        the user's effective scientific configuration.
        """

        root = safe_output_root(out_root)
        provisional = Config(provider="mock", out_dir=str(root))
        context = cls.create(
            provisional,
            out_root=root,
            scenario_id="bootstrap-attempt",
            run_id=run_id,
            repo_root=repo_root,
            command_identity=command_identity,
            run_kind="bootstrap_attempt",
            planned_simulation_runs=None,
            full_validation_completed=False,
        )
        context.manifest["bootstrap"]["provisional_config"] = True
        context.manifest["bootstrap"]["effective_config_available"] = False
        context._write()
        return context

    @classmethod
    def create_driver(
        cls,
        *,
        out_root: os.PathLike[str],
        command_identity: str,
        planned_runs: Optional[int],
        run_id: Optional[str] = None,
        worker_count: Optional[int] = None,
        input_paths: Any = None,
    ) -> "ManagedRunContext":
        """Create a parent experiment-driver attempt measured in child runs."""

        cfg = Config(provider="mock", out_dir=str(out_root))
        context = cls.create(
            cfg,
            out_root=out_root,
            scenario_id=command_identity,
            run_id=run_id,
            worker_count=worker_count,
            input_paths=input_paths,
            command_identity=command_identity,
            run_kind="experiment_driver",
            planned_simulation_runs=planned_runs,
        )
        context.manifest["bootstrap"]["provisional_config"] = True
        context.manifest["bootstrap"]["effective_config_available"] = False
        context.set_experiment_completion(
            planned_runs=planned_runs,
            started_runs=0,
            completed_runs=0,
            failed_runs=0,
        )
        return context

    def __init__(
        self,
        manager: RunManager,
        *,
        command_identity: str,
        run_kind: str,
        planned_simulation_runs: Optional[int],
        full_validation_completed: bool,
    ) -> None:
        self._manager = manager
        self._lock = threading.RLock()
        self._state = NEW
        self.current_stage = "bootstrap"
        self.active_llm: Any = None
        self.tracker: Any = None
        self.llm_mode: Optional[str] = None
        self.network_access = False

        planned_rounds: Optional[int]
        planned_decisions: Optional[int]
        if run_kind == "simulation" and full_validation_completed:
            planned_rounds = int(getattr(manager.cfg, "n_rounds", 0))
            planned_agents = manager.manifest["personas"]["population"].get(
                "planned_llm_total"
            )
            planned_decisions = (
                planned_rounds * int(planned_agents)
                if planned_agents is not None
                else None
            )
            planned_runs = 1 if planned_simulation_runs is None else planned_simulation_runs
        else:
            planned_rounds = None
            planned_decisions = None
            planned_runs = planned_simulation_runs

        manager.manifest.update(
            {
                "managed_context": {
                    "schema_version": MANAGED_CONTEXT_SCHEMA_VERSION,
                    "state": ACTIVE,
                    "run_kind": run_kind,
                    "command_identity": str(command_identity),
                    "full_validation_completed": bool(full_validation_completed),
                },
                "bootstrap": {
                    "command_identity": str(command_identity),
                    "provisional_config": False,
                    "effective_config_available": bool(full_validation_completed),
                },
                "failure_stage": None,
                "failure_type": None,
                "outputs_complete": False,
                "simulation_computation_completed": False,
                "managed_run_completed": False,
                "completion": completion_template(
                    planned_rounds=planned_rounds,
                    planned_decisions=planned_decisions,
                    planned_simulation_runs=planned_runs,
                ),
                "honest_n": 0,
                "honest_n_unit": "agent_decisions",
                "honest_n_deprecated": True,
            }
        )
        manager.manifest["llm"].setdefault("runtime", {}).update(
            {
                "network_access": False,
                "provider_calls": 0,
                "provider_calls_succeeded": 0,
                "provider_calls_failed": 0,
                "response_sources": {"provider": 0, "cache": 0, "replay": 0},
            }
        )
        self._state = ACTIVE
        self.events = _ManagedEventObserver(self, manager.events)
        self.observer = self.events
        self._write()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    @property
    def state(self) -> str:
        return self._state

    @property
    def managed(self) -> bool:
        return True

    def _write(self) -> None:
        self._manager.manifest.write_atomic()

    def _observe_event(
        self, event_type: str, data: Mapping[str, Any], private_data: Mapping[str, Any]
    ) -> None:
        with self._lock:
            if self._state != ACTIVE:
                return
            completion = self.manifest["completion"]
            if event_type == "RoundStarted":
                completion["rounds"]["started"] += 1
            elif event_type == "RoundFinished":
                completion["rounds"]["completed"] += 1
            elif event_type == "LLMRequestRecorded":
                completion["llm_logical_requests"]["attempted"] += 1
                completion["agent_decisions"]["attempted"] += 1
            elif event_type == "LLMResponseRecorded":
                completion["llm_logical_requests"]["completed"] += 1
                source = str(data.get("source") or "record")
                if source == "replay":
                    completion["response_sources"]["replay"] += 1
                else:
                    completion["response_sources"]["provider"] += 1
            elif event_type == "LLMProviderAttemptObserved":
                attempts = completion["application_provider_attempts"]
                attempts["attempted"] += 1
                outcome = str(data.get("outcome") or "")
                if outcome in {"response_parseable", "response_parse_failed"}:
                    attempts["responses_received"] += 1
                if outcome == "response_parse_failed":
                    attempts["parse_failed_responses"] += 1
                elif outcome == "provider_exception":
                    attempts["provider_exceptions"] += 1
                if bool(data.get("will_retry")):
                    attempts["retries_scheduled"] += 1
                if int(data.get("attempt_index") or 0) == 2:
                    attempts["logical_requests_with_retry"] += 1
                if (
                    not bool(data.get("will_retry"))
                    and outcome in {"response_parse_failed", "provider_exception"}
                ):
                    attempts["exhausted_logical_requests"] += 1
                reported_model = safe_reported_model(data.get("reported_model"))
                reported_models = attempts["reported_models"]
                if (
                    isinstance(reported_model, str)
                    and reported_model
                    and reported_model not in reported_models
                ):
                    if len(reported_models) < 16:
                        reported_models.append(reported_model)
                        reported_models.sort()
                    else:
                        attempts["reported_models_truncated"] = True
            elif event_type == "AgentDecisionParsed":
                completion["agent_decisions"]["completed"] += 1
                parsing = completion["parsing"]
                parsing["attempted"] += 1
                parse_failed = data.get("parse_status") == "error"
                rationale = str(private_data.get("private_rationale") or "")
                fallback = parse_failed or rationale in {
                    "api-error; holding",
                    "parse-retries-exhausted; holding",
                }
                if parse_failed:
                    parsing["failed"] += 1
                else:
                    parsing["succeeded"] += 1
                if fallback:
                    parsing["fallbacks"] += 1
            self._refresh_derived(write=False)
            self._write()

    def _refresh_derived(self, *, write: bool = True) -> None:
        completion = self.manifest["completion"]
        rounds = completion["rounds"]
        decisions = completion["agent_decisions"]
        requests = completion["llm_logical_requests"]

        if self._state == FAILED and rounds["started"] > rounds["completed"]:
            rounds["failed"] = 1
        if rounds["planned"] is not None:
            rounds["skipped"] = max(
                0,
                int(rounds["planned"])
                - int(rounds["completed"])
                - int(rounds["failed"]),
            )

        decisions["failed"] = max(
            0, int(decisions["attempted"]) - int(decisions["completed"])
        )
        if decisions["planned"] is not None:
            decisions["skipped"] = max(
                0,
                int(decisions["planned"])
                - int(decisions["completed"])
                - int(decisions["failed"]),
            )
        requests["failed"] = max(
            0, int(requests["attempted"]) - int(requests["completed"])
        )
        self.manifest["honest_n"] = int(decisions["completed"])
        self.manifest["honest_n_unit"] = "agent_decisions"
        self.manifest["honest_n_deprecated"] = True
        if write:
            self._write()

    def set_stage(self, stage: str) -> None:
        if stage not in FAILURE_STAGES:
            raise ValueError("unknown failure stage: {}".format(stage))
        with self._lock:
            if self._state == ACTIVE:
                self.current_stage = stage
                self.manifest["managed_context"]["current_stage"] = stage
                self._write()

    @contextmanager
    def stage(self, stage: str):
        self.set_stage(stage)
        yield self

    def execute_simulation(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run the pure simulation while accounting for its computation only."""

        self.set_stage("simulation")
        runs = self.manifest["completion"]["simulation_runs"]
        with self._lock:
            if runs["started"] == 0:
                runs["started"] = 1
                self._write()
        try:
            result = function(*args, **kwargs)
        except BaseException:
            with self._lock:
                runs["failed"] = 1
                self._write()
            raise
        else:
            with self._lock:
                runs["completed"] = 1
                runs["failed"] = 0
                self.manifest["simulation_computation_completed"] = True
                self._write()
            return result

    def prepare_llm(self, replay_from: Optional[str] = None):
        """Construct Record or Strict Replay before entering the simulation."""

        from .llm import CachingLLM, CostTracker, build_llm
        from .provider_capabilities import provider_capability_snapshot
        from .recording import (
            RecordingLLM,
            ReplayLLM,
            recorded_model_config,
            runtime_model_config,
        )

        cfg = self.cfg
        if replay_from:
            self.set_stage("replay_preflight")
            source_path = Path(replay_from)
            records_path = str(
                source_path / "llm_records.jsonl" if source_path.is_dir() else source_path
            )
            self.register_llm_runtime(
                mode="replay",
                record_source=records_path,
                network_access=False,
                provider_calls=0,
                provider_connection_limit=0,
            )
            source_config = recorded_model_config(replay_from)
            model_config = runtime_model_config(cfg, recorded=source_config)
            resolved_provider = model_config.get("resolved_provider")
            adapter_contract = model_config.get("provider_adapter_contract")
            codex_reasoning_effort = (
                adapter_contract.get("reasoning_effort")
                if isinstance(adapter_contract, Mapping)
                else None
            )
            capability_snapshot = (
                None
                if resolved_provider in (None, "", "none")
                else provider_capability_snapshot(
                    str(resolved_provider),
                    endpoint=(
                        os.environ.get("OPENAI_BASE_URL")
                        or getattr(cfg, "openai_base_url", None)
                        if resolved_provider == "openai"
                        else None
                    ),
                    model=(
                        str(model_config.get("model"))
                        if resolved_provider == "codex_exec"
                        and model_config.get("model") is not None
                        else None
                    ),
                    reasoning_effort=(
                        str(codex_reasoning_effort)
                        if resolved_provider == "codex_exec"
                        and codex_reasoning_effort is not None
                        else None
                    ),
                )
            )
            llm = ReplayLLM(
                replay_from,
                model_config=model_config,
                event_logger=self.events,
                compatibility_metadata=self.replay_compatibility,
            )
            tracker = CostTracker()
            mode = "replay"
            network_access = False
            connection_limit = 0
        else:
            self.set_stage("provider_setup")
            requested_provider = str(
                os.environ.get("LLM_PROVIDER") or getattr(cfg, "provider", "auto")
            ).strip().lower()
            if requested_provider == "codex_exec":
                from .codex_exec import (
                    CodexExecError,
                    CodexExecLLM,
                    codex_reasoning_effort_from_environment,
                )

                requested_model = (
                    os.environ.get("LLM_MODEL")
                    or getattr(cfg, "model", "")
                    or ""
                )
                if not str(requested_model).strip():
                    raise CodexExecError(
                        "model_not_available",
                        "CodexExec requires an explicit requested model identity",
                    )
                reasoning_effort = (
                    getattr(cfg, "codex_reasoning_effort", None)
                    or codex_reasoning_effort_from_environment()
                )
                if reasoning_effort is None:
                    raise CodexExecError(
                        "model_not_available",
                        "CodexExec requires an explicit reasoning effort",
                    )
                tracker = CostTracker()
                codex_provider = CodexExecLLM(
                    model=str(requested_model),
                    reasoning_effort=reasoning_effort,
                    binary=os.environ.get("NMSIM_CODEX_EXECUTABLE", "codex"),
                    project_root=self.repo_root,
                    run_id=self.run_id,
                )
                inner = CachingLLM(
                    codex_provider,
                    tracker,
                    enabled=bool(getattr(cfg, "cache_enabled", False)),
                )
            else:
                inner, tracker = build_llm(cfg)
            model_config = runtime_model_config(cfg, llm=inner)
            resolved_provider = model_config.get("resolved_provider")
            adapter_contract = model_config.get("provider_adapter_contract")
            codex_reasoning_effort = (
                adapter_contract.get("reasoning_effort")
                if isinstance(adapter_contract, Mapping)
                else None
            )
            capability_snapshot = (
                None
                if resolved_provider in (None, "", "none")
                else provider_capability_snapshot(
                    str(resolved_provider),
                    endpoint=(
                        os.environ.get("OPENAI_BASE_URL")
                        or getattr(cfg, "openai_base_url", None)
                        if resolved_provider == "openai"
                        else None
                    ),
                    model=(
                        str(model_config.get("model"))
                        if resolved_provider == "codex_exec"
                        and model_config.get("model") is not None
                        else None
                    ),
                    reasoning_effort=(
                        str(codex_reasoning_effort)
                        if resolved_provider == "codex_exec"
                        and codex_reasoning_effort is not None
                        else None
                    ),
                )
            )
            llm = RecordingLLM(
                inner,
                self.run_dir,
                model_config=model_config,
                event_logger=self.events,
                compatibility_metadata=self.replay_compatibility,
            )
            mode = "record"
            codex_provider = _provider_in_wrapper_chain(inner, "codex_exec")
            if codex_provider is not None:
                network_access = bool(codex_provider.network_access)
                connection_limit = 1
                self.manifest["llm"]["codex_exec"] = {
                    **codex_provider.identity_snapshot(),
                    "last_call": None,
                    "call_history": [],
                    "usage_totals": dict(codex_provider.usage_totals),
                    "provider_calls": {
                        "attempted": 0,
                        "succeeded": 0,
                        "failed": 0,
                    },
                }
            else:
                network_access = getattr(llm, "kind", "mock") != "mock"
                connection_limit = (
                    40 if model_config.get("resolved_provider") == "openai" else None
                )

        self.active_llm = llm
        self.tracker = tracker
        self.llm_mode = mode
        self.network_access = bool(network_access)
        if mode == "replay" and model_config.get("resolved_provider") == "codex_exec":
            # Historical probe/auth evidence is descriptive; this replay run
            # performs no login check and starts no Codex subprocess.
            self.manifest["llm"]["codex_exec"] = {
                "provider_adapter_contract": model_config.get(
                    "provider_adapter_contract"
                ),
                "recorded_runtime_identity": model_config.get(
                    "provider_runtime_identity"
                ),
                "response_source": "replay",
                "auth_checked_this_run": False,
                "subprocess_started_this_run": False,
                "provider_transport_network_expected": True,
                "provider_transport_network_declared_or_observed": (
                    "not_observed_replay"
                ),
                "agent_tool_network_enabled": False,
                "tool_calls_observed": 0,
                "network_access": False,
                "provider_calls": {"attempted": 0, "succeeded": 0, "failed": 0},
            }
        if capability_snapshot is not None:
            # Descriptive provenance only: this field is not part of recording
            # schema 1.2 or the Strict Replay compatibility contract.
            self.manifest["llm"]["provider_capability_snapshot"] = capability_snapshot
        self.register_llm_runtime(
            llm=llm,
            provider=model_config.get("resolved_provider"),
            model=model_config.get("model"),
            mode=mode,
            record_source=(records_path if replay_from else llm.records_path),
            cache_enabled=model_config.get("cache_enabled"),
            model_config=model_config,
            network_access=bool(network_access),
            application_concurrency_limit=(
                1 if model_config.get("resolved_provider") == "codex_exec" else None
            ),
            provider_connection_limit=connection_limit,
        )
        return llm, tracker

    def sync_llm_accounting(self, llm: Any = None, tracker: Any = None) -> None:
        """Synchronize source/call counts without altering the provider classes."""

        llm = llm or self.active_llm
        tracker = tracker or self.tracker
        completion = self.manifest["completion"]
        requests = completion["llm_logical_requests"]
        sources = completion["response_sources"]
        provider_calls = completion["provider_calls"]

        if llm is not None:
            requests["attempted"] = max(
                int(requests["attempted"]), int(getattr(llm, "request_count", 0))
            )
            requests["completed"] = max(
                int(requests["completed"]), int(getattr(llm, "response_count", 0))
            )
            self.register_batch_sizes(getattr(llm, "batch_sizes", []))

        completed = int(requests["completed"])
        attempted = int(requests["attempted"])
        cache_hits = int(getattr(tracker, "cache_hits", 0)) if tracker is not None else 0
        cache_hits = min(cache_hits, completed)
        if self.llm_mode == "replay":
            sources.update({"provider": 0, "cache": 0, "replay": completed})
            provider_calls.update({"attempted": 0, "succeeded": 0, "failed": 0})
        elif self.llm_mode == "record":
            provider_completed = max(0, completed - cache_hits)
            provider_attempted = max(0, attempted - cache_hits)
            sources.update(
                {"provider": provider_completed, "cache": cache_hits, "replay": 0}
            )
            provider_calls.update(
                {
                    "attempted": provider_attempted,
                    "succeeded": provider_completed,
                    "failed": max(0, provider_attempted - provider_completed),
                }
            )
        else:
            sources.update({"provider": 0, "cache": 0, "replay": 0})
            provider_calls.update({"attempted": 0, "succeeded": 0, "failed": 0})

        codex_provider = _provider_in_wrapper_chain(llm, "codex_exec")
        if codex_provider is not None and self.llm_mode == "record":
            # A batch can stop part-way through on a fail-closed Codex case.
            # The adapter's process counters are therefore more honest than
            # inferring every provider call from the logical batch size.
            provider_calls.update(
                {
                    "attempted": int(codex_provider.provider_calls_attempted),
                    "succeeded": int(codex_provider.provider_calls_succeeded),
                    "failed": int(codex_provider.provider_calls_failed),
                }
            )
            self.network_access = bool(codex_provider.network_access)
            codex_manifest = self.manifest["llm"].setdefault("codex_exec", {})
            codex_manifest.update(codex_provider.identity_snapshot())
            codex_manifest["last_call"] = redact_secrets(
                dict(codex_provider.last_call_metadata or {})
            )
            codex_manifest["call_history"] = [
                redact_secrets(dict(item))
                for item in codex_provider.call_metadata_history
            ]
            codex_manifest["usage_totals"] = dict(codex_provider.usage_totals)
            codex_manifest["provider_calls"] = dict(provider_calls)

        requests["failed"] = max(0, attempted - completed)
        if llm is not None or tracker is not None:
            self.register_llm_runtime(
                logical_requests=attempted,
                recorded_responses=completed,
                provider_calls=provider_calls["attempted"],
                provider_calls_succeeded=provider_calls["succeeded"],
                provider_calls_failed=provider_calls["failed"],
                cache_hits=cache_hits,
                response_sources=dict(sources),
                network_access=bool(self.network_access),
            )
        self._refresh_derived()

    def assert_replay_exhausted(self) -> None:
        if self.llm_mode == "replay" and self.active_llm is not None:
            self.active_llm.assert_exhausted()

    def set_experiment_completion(
        self,
        *,
        planned_runs: Optional[int],
        started_runs: int,
        completed_runs: int,
        failed_runs: int,
        cells: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Record run-level N separately from decision-level completion."""

        payload = {
            "unit": "runs",
            "planned_runs": planned_runs,
            "started_runs": int(started_runs),
            "completed_runs": int(completed_runs),
            "failed_runs": int(failed_runs),
            "honest_n_runs": int(completed_runs),
        }
        if cells is not None:
            payload["cells"] = redact_secrets(dict(cells))
        with self._lock:
            self.manifest["experiment_completion"] = payload
            runs = self.manifest["completion"]["simulation_runs"]
            runs.update(
                {
                    "planned": planned_runs,
                    "started": int(started_runs),
                    "completed": int(completed_runs),
                    "failed": int(failed_runs),
                }
            )
            self._write()

    def _legacy_samples(self) -> tuple[Optional[int], int, int, int]:
        completion = self.manifest["completion"]
        decisions = completion["agent_decisions"]
        parsing = completion["parsing"]
        expected = decisions["planned"]
        completed = int(decisions["completed"])
        degraded = int(parsing["fallbacks"])
        return expected, completed, degraded, max(0, completed - degraded)

    def finish(
        self,
        *,
        legacy_filenames: Iterable[os.PathLike[str]] = (),
        extra_results: Any = None,
    ) -> Path:
        """Enter FINISHED once; repeated or conflicting terminal calls are no-ops."""

        with self._lock:
            if self._state != ACTIVE:
                return self.manifest_path
            self.set_stage("finalization")
            self.sync_llm_accounting()
            self._refresh_derived(write=False)
            self.manifest["outputs_complete"] = True
            self.manifest["managed_run_completed"] = True
            self.manifest["failure_stage"] = None
            self.manifest["failure_type"] = None
            self.manifest["managed_context"]["state"] = FINISHED
            expected, completed, degraded, legacy_honest = self._legacy_samples()
            try:
                path = self._manager.finish(
                    expected=expected,
                    completed=completed,
                    failed=degraded,
                    honest_n=legacy_honest,
                    extra_results=extra_results,
                )
            except BaseException as error:
                self.manifest["outputs_complete"] = False
                self.manifest["managed_run_completed"] = False
                self.manifest["managed_context"]["state"] = ACTIVE
                self._state = ACTIVE
                self.fail(error, failure_stage="finalization")
                raise
            self._state = FINISHED

        # Compatibility projections are published only after the canonical run
        # is terminally finished; a failed run can never become ``latest``.
        if legacy_filenames:
            try:
                self._manager.publish_legacy_links(legacy_filenames)
            except Exception as error:
                warning = "legacy compatibility publication failed: {}".format(
                    type(error).__name__
                )
                self.manifest["warnings"].append(warning)
                self._write()
        return path

    def fail(
        self,
        error: Any,
        *,
        failure_stage: Optional[str] = None,
        extra_results: Any = None,
    ) -> Path:
        """Enter FAILED once while retaining all completion observed so far."""

        with self._lock:
            if self._state != ACTIVE:
                return self.manifest_path
            stage = failure_stage or self.current_stage
            if stage not in FAILURE_STAGES:
                stage = "finalization"
            self.sync_llm_accounting()
            self._state = FAILED
            self._refresh_derived(write=False)
            runs = self.manifest["completion"]["simulation_runs"]
            if runs["started"] and not runs["completed"]:
                runs["failed"] = max(1, int(runs["failed"]))
            failure_type = (
                "keyboard_interrupt"
                if isinstance(error, KeyboardInterrupt)
                else "system_exit"
                if isinstance(error, SystemExit)
                else type(error).__name__
            )
            self.manifest["failure_stage"] = stage
            self.manifest["failure_type"] = failure_type
            self.manifest["outputs_complete"] = False
            self.manifest["managed_run_completed"] = False
            self.manifest["managed_context"]["state"] = FAILED
            self.manifest["managed_context"]["current_stage"] = stage
            expected, completed, degraded, legacy_honest = self._legacy_samples()
            return self._manager.fail(
                error,
                expected=expected,
                completed=completed,
                failed=degraded,
                honest_n=legacy_honest,
                extra_results=extra_results,
            )

    def close(self) -> Path:
        with self._lock:
            if self._state == ACTIVE:
                return self.fail(
                    ManagedRunLifecycleError(
                        "managed context closed without explicit finish"
                    ),
                    failure_stage="finalization",
                )
            return self.manifest_path

    def __enter__(self) -> "ManagedRunContext":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc is not None:
            self.fail(exc, failure_stage=self.current_stage)
        else:
            self.close()
        return False


@dataclass
class NullRunContext:
    """Explicit no-provenance context for pure tests and diagnostics."""

    observer: Any = None
    events: Any = None
    run_id: None = None
    run_dir: None = None
    managed: bool = False

    def execute_simulation(
        self, function: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        return function(*args, **kwargs)

    def finish(self, **_kwargs: Any) -> None:
        return None

    def fail(self, _error: Any, **_kwargs: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> "NullRunContext":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


__all__ = [
    "ACTIVE",
    "COMPLETION_SCHEMA_VERSION",
    "FAILED",
    "FAILURE_STAGES",
    "FINISHED",
    "MANAGED_CONTEXT_SCHEMA_VERSION",
    "ManagedRunContext",
    "ManagedRunLifecycleError",
    "NEW",
    "NullRunContext",
    "completion_template",
    "safe_output_root",
    "validate_run_id",
]
