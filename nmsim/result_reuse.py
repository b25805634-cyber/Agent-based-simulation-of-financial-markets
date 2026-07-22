"""Fail-closed identity gate for reusing managed child simulation runs.

This module is deliberately read-only.  It neither launches a provider nor
mutates a candidate run.  Experiment drivers may only count a candidate as a
reused simulation replicate after :func:`validate_child_run_reuse` accepts it.

The manifest is the provenance trust root available in Phase 1.2A.  Artifact
bytes are always re-hashed; a bare legacy JSON/CSV/PNG is never promoted into a
managed child run merely because its filename looks familiar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Sequence

from .config_contract import build_effective_config_contract
from .fingerprint import scientific_compatibility_metadata
from .provenance import MANIFEST_SCHEMA_VERSION, sha256_file
from .recording import runtime_model_config
from .recording_schema import CURRENT_RECORDING_SCHEMA_VERSION


RESULT_REUSE_POLICY_VERSION = "1.1"

MANIFEST_MISSING = "manifest_missing"
MANIFEST_INVALID = "manifest_invalid"
MANIFEST_SCHEMA_INCOMPATIBLE = "manifest_schema_incompatible"
STATUS_NOT_FINISHED = "status_not_finished"
MANAGED_RUN_INCOMPLETE = "managed_run_incomplete"
OUTPUTS_INCOMPLETE = "outputs_incomplete"
FAILURE_STAGE_PRESENT = "failure_stage_present"
RUN_KIND_MISMATCH = "run_kind_mismatch"
ENTRYPOINT_MISMATCH = "entrypoint_mismatch"
COMPLETION_INCOMPLETE = "completion_incomplete"
RECORDING_SCHEMA_INCOMPATIBLE = "recording_schema_incompatible"
SCIENTIFIC_FINGERPRINT_MISMATCH = "scientific_fingerprint_mismatch"
DECISION_PARSER_MISMATCH = "decision_parser_mismatch"
EVENT_SCHEMA_MISMATCH = "event_schema_mismatch"
SIMULATION_CORE_MISMATCH = "simulation_core_mismatch"
SCIENTIFIC_CONFIG_MISMATCH = "scientific_config_mismatch"
MODEL_REQUEST_CONFIG_MISMATCH = "model_request_config_mismatch"
PROVIDER_MISMATCH = "provider_mismatch"
MODEL_MISMATCH = "model_mismatch"
ENDPOINT_MISMATCH = "endpoint_mismatch"
MODEL_REQUEST_DETAIL_MISMATCH = "model_request_detail_mismatch"
PROMPT_MISMATCH = "prompt_mismatch"
PERSONA_MISMATCH = "persona_mismatch"
SCENARIO_MISMATCH = "scenario_mismatch"
INPUT_IDENTITY_MISMATCH = "input_identity_mismatch"
SEED_MISMATCH = "seed_mismatch"
POPULATION_MISMATCH = "population_mismatch"
ARTIFACT_MISSING = "artifact_missing"
ARTIFACT_INVALID = "artifact_invalid"
ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
RESULT_IDENTITY_MISMATCH = "result_identity_mismatch"
UNSAFE_SYMLINK = "unsafe_symlink"
UNSAFE_ARTIFACT_PATH = "unsafe_artifact_path"
LEGACY_LINK_IDENTITY_MISMATCH = "legacy_link_identity_mismatch"
LEGACY_FLAT_RESULT_UNVERIFIED = "legacy_flat_result_unverified"
HEALTH_GATE_REJECTED = "health_gate_rejected"
EXPERIMENT_SLOT_MISMATCH = "experiment_slot_mismatch"
REPORTED_MODEL_GATE_REJECTED = "reported_model_gate_rejected"

REUSE_REASON_CODES = frozenset(
    {
        MANIFEST_MISSING,
        MANIFEST_INVALID,
        MANIFEST_SCHEMA_INCOMPATIBLE,
        STATUS_NOT_FINISHED,
        MANAGED_RUN_INCOMPLETE,
        OUTPUTS_INCOMPLETE,
        FAILURE_STAGE_PRESENT,
        RUN_KIND_MISMATCH,
        ENTRYPOINT_MISMATCH,
        COMPLETION_INCOMPLETE,
        RECORDING_SCHEMA_INCOMPATIBLE,
        SCIENTIFIC_FINGERPRINT_MISMATCH,
        DECISION_PARSER_MISMATCH,
        EVENT_SCHEMA_MISMATCH,
        SIMULATION_CORE_MISMATCH,
        SCIENTIFIC_CONFIG_MISMATCH,
        MODEL_REQUEST_CONFIG_MISMATCH,
        PROVIDER_MISMATCH,
        MODEL_MISMATCH,
        ENDPOINT_MISMATCH,
        MODEL_REQUEST_DETAIL_MISMATCH,
        PROMPT_MISMATCH,
        PERSONA_MISMATCH,
        SCENARIO_MISMATCH,
        INPUT_IDENTITY_MISMATCH,
        SEED_MISMATCH,
        POPULATION_MISMATCH,
        ARTIFACT_MISSING,
        ARTIFACT_INVALID,
        ARTIFACT_HASH_MISMATCH,
        RESULT_IDENTITY_MISMATCH,
        UNSAFE_SYMLINK,
        UNSAFE_ARTIFACT_PATH,
        LEGACY_LINK_IDENTITY_MISMATCH,
        LEGACY_FLAT_RESULT_UNVERIFIED,
        HEALTH_GATE_REJECTED,
        EXPERIMENT_SLOT_MISMATCH,
        REPORTED_MODEL_GATE_REJECTED,
    }
)

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MULTI_EVENT_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "protocol_sha256",
        "event_id",
        "arm",
        "seed",
        "repeat_idx",
        "reference_csv_sha256",
        "news_timeline_sha256",
        "reference_transform_sha256",
    }
)
_MULTI_EVENT_MATERIAL_KEYS = frozenset(
    {
        *_MULTI_EVENT_RESULT_KEYS,
        "catalog_sha256",
        "event_definition_sha256",
        "reference_transform_id",
        "timeline_transform_sha256",
        "combined_transform_sha256",
    }
)


class ResultReuseError(ValueError):
    """A candidate cannot be safely represented by the reuse contract."""

    def __init__(self, reason_code: str, field: Optional[str] = None) -> None:
        if reason_code not in REUSE_REASON_CODES:
            raise ValueError("unknown result reuse reason code")
        self.reason_code = reason_code
        self.field = field
        message = reason_code if field is None else "{}: {}".format(reason_code, field)
        super().__init__(message)


@dataclass(frozen=True)
class ArtifactIdentity:
    """One manifest-registered canonical artifact."""

    path: str
    sha256: str
    size_bytes: int
    registered: bool


@dataclass(frozen=True)
class LegacyLinkIdentity:
    """Safe identity of a compatibility projection without exposing its root."""

    path_name: str
    target: str


@dataclass(frozen=True)
class ChildRunIdentity:
    """Identity extracted from one completed managed child manifest."""

    manifest_path: Path
    run_dir: Path
    run_id: str
    run_kind: str
    command_identity: str
    managed_context_state: str
    manifest_schema_version: str
    status: str
    managed_run_completed: bool
    outputs_complete: bool
    failure_stage: Optional[str]
    simulation_runs_planned: Optional[int]
    simulation_runs_started: int
    simulation_runs_completed: int
    simulation_runs_failed: int
    agent_decisions_planned: Optional[int]
    agent_decisions_completed: int
    agent_decisions_failed: int
    completion_identity: str
    completion_complete: bool
    recording_schema_version: str
    scientific_component_fingerprint: str
    decision_parser_schema_version: str
    decision_parser_source_hash: str
    event_schema_version: str
    prompt_source_hash: str
    persona_source_hash: str
    simulation_core_source_hash: str
    config_hash_schema_version: str
    scientific_config_hash: str
    model_request_config_hash: str
    scientific_input_identity: str
    reference_path_content_hash: Optional[str]
    scenario_definition_hash: str
    population_identity: str
    population_complete: bool
    seed: int
    requested_provider: str
    resolved_provider: str
    requested_model: Optional[str]
    resolved_model: str
    endpoint_identity: str
    temperature: float
    max_tokens: int
    cache_enabled: bool
    experiment_slot: Optional[Mapping[str, Any]]
    multi_event_identity: Optional[Mapping[str, Any]]
    multi_event_material_identity: Optional[Mapping[str, Any]]
    reported_model_aliases: Optional[tuple[str, ...]]
    artifacts: tuple[ArtifactIdentity, ...]
    legacy_links: tuple[LegacyLinkIdentity, ...]
    git_commit: Optional[str]
    git_dirty: Optional[bool]
    git_diff_hash: Optional[str]

    @classmethod
    def from_manifest(cls, manifest_path: os.PathLike[str] | str) -> "ChildRunIdentity":
        path = Path(manifest_path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ResultReuseError(MANIFEST_MISSING) from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ResultReuseError(MANIFEST_INVALID) from error
        if not isinstance(raw, Mapping):
            raise ResultReuseError(MANIFEST_INVALID)
        return cls.from_manifest_data(path, raw)

    @classmethod
    def from_manifest_data(
        cls, manifest_path: os.PathLike[str] | str, raw: Mapping[str, Any]
    ) -> "ChildRunIdentity":
        path = Path(manifest_path)
        try:
            managed = _mapping(raw, "managed_context")
            completion = _mapping(raw, "completion")
            simulation_runs = _mapping(completion, "simulation_runs")
            decisions = _mapping(completion, "agent_decisions")
            llm = _mapping(raw, "llm")
            runtime = _mapping(llm, "runtime")
            model_config = _mapping(runtime, "model_config")
            scenario = _mapping(raw, "scenario")
            rng = _mapping(raw, "rng")
            personas = _mapping(raw, "personas")
            population = _mapping(personas, "population")
            git = _mapping(raw, "git")

            input_identity, reference_hash = _input_identity(raw.get("inputs"))
            experiment_slot = _optional_experiment_slot(
                raw.get("experiment_slot")
            )
            multi_event_identity = _manifest_multi_event_identity(
                raw, experiment_slot
            )
            multi_event_material_identity = (
                _manifest_multi_event_material_identity(
                    raw, experiment_slot, multi_event_identity
                )
            )
            reported_model_aliases = _manifest_reported_model_aliases(
                raw, experiment_slot
            )
            artifacts = _artifact_identities(raw.get("results"))
            legacy_links = _legacy_link_identities(raw.get("compatibility"))
            endpoint_identity = _endpoint_identity(raw, model_config)

            identity = cls(
                manifest_path=path,
                run_dir=path.parent,
                run_id=_required_text(raw, "run_id"),
                run_kind=_required_text(managed, "run_kind"),
                command_identity=_required_text(managed, "command_identity"),
                managed_context_state=_required_text(managed, "state"),
                manifest_schema_version=_required_text(raw, "schema_version"),
                status=_required_text(raw, "status"),
                managed_run_completed=_required_bool(raw, "managed_run_completed"),
                outputs_complete=_required_bool(raw, "outputs_complete"),
                failure_stage=_optional_text(raw.get("failure_stage")),
                simulation_runs_planned=_optional_int(simulation_runs.get("planned")),
                simulation_runs_started=_required_int(simulation_runs, "started"),
                simulation_runs_completed=_required_int(simulation_runs, "completed"),
                simulation_runs_failed=_required_int(simulation_runs, "failed"),
                agent_decisions_planned=_optional_int(decisions.get("planned")),
                agent_decisions_completed=_required_int(decisions, "completed"),
                agent_decisions_failed=_required_int(decisions, "failed"),
                completion_identity=_stable_hash(completion),
                completion_complete=_completion_payload_complete(completion),
                recording_schema_version=_required_text(raw, "recording_schema_version"),
                scientific_component_fingerprint=_required_hash(
                    raw, "scientific_component_fingerprint"
                ),
                decision_parser_schema_version=_required_text(
                    raw, "decision_parser_schema_version"
                ),
                decision_parser_source_hash=_required_hash(
                    raw, "decision_parser_source_hash"
                ),
                event_schema_version=_required_text(raw, "event_schema_version"),
                prompt_source_hash=_required_hash(raw, "prompt_source_hash"),
                persona_source_hash=_required_hash(raw, "persona_source_hash"),
                simulation_core_source_hash=_required_hash(
                    raw, "simulation_core_source_hash"
                ),
                config_hash_schema_version=_required_text(
                    raw, "config_hash_schema_version"
                ),
                scientific_config_hash=_required_hash(raw, "scientific_config_hash"),
                model_request_config_hash=_required_hash(
                    raw, "model_request_config_hash"
                ),
                scientific_input_identity=input_identity,
                reference_path_content_hash=reference_hash,
                scenario_definition_hash=_required_hash(
                    scenario, "definition_sha256"
                ),
                population_identity=_population_contract_identity(raw),
                population_complete=_population_complete(population),
                seed=_required_int(rng, "seed"),
                requested_provider=_required_text(llm, "provider"),
                resolved_provider=_required_text(llm, "resolved_provider"),
                requested_model=_optional_text(llm.get("model")),
                resolved_model=_required_text(llm, "resolved_model"),
                endpoint_identity=endpoint_identity,
                temperature=_required_number(llm, "temperature"),
                max_tokens=_required_int(llm, "max_tokens"),
                cache_enabled=_required_bool(llm, "cache_enabled"),
                experiment_slot=experiment_slot,
                multi_event_identity=multi_event_identity,
                multi_event_material_identity=multi_event_material_identity,
                reported_model_aliases=reported_model_aliases,
                artifacts=artifacts,
                legacy_links=legacy_links,
                git_commit=_optional_text(git.get("commit")),
                git_dirty=_optional_bool(git.get("dirty")),
                git_diff_hash=_optional_hash(git.get("diff_hash")),
            )
        except ResultReuseError:
            raise
        except (TypeError, ValueError, OverflowError) as error:
            raise ResultReuseError(MANIFEST_INVALID) from error

        if path.name != "run_manifest.json" or path.parent.name != identity.run_id:
            raise ResultReuseError(MANIFEST_INVALID, "run_id")
        if not _RUN_ID.fullmatch(identity.run_id):
            raise ResultReuseError(MANIFEST_INVALID, "run_id")
        return identity


@dataclass(frozen=True)
class ExpectedRunIdentity:
    """The identity a driver expects before accepting a previous child run.

    ``required_artifacts`` is explicit because output names are entrypoint
    specific.  At least one canonical artifact must be named; file existence
    alone is never accepted.
    """

    run_kind: str
    command_identity: str
    manifest_schema_version: str
    recording_schema_version: str
    scientific_component_fingerprint: str
    decision_parser_schema_version: str
    decision_parser_source_hash: str
    event_schema_version: str
    prompt_source_hash: str
    persona_source_hash: str
    simulation_core_source_hash: str
    config_hash_schema_version: str
    scientific_config_hash: str
    model_request_config_hash: str
    scientific_input_identity: str
    reference_path_content_hash: Optional[str]
    scenario_definition_hash: str
    population_identity: str
    seed: int
    requested_provider: str
    resolved_provider: str
    requested_model: Optional[str]
    resolved_model: str
    endpoint_identity: str
    temperature: float
    max_tokens: int
    cache_enabled: bool
    experiment_slot: Optional[Mapping[str, Any]]
    multi_event_identity: Optional[Mapping[str, Any]]
    multi_event_material_identity: Optional[Mapping[str, Any]]
    required_artifacts: tuple[str, ...]
    git_commit: Optional[str]

    @classmethod
    def from_child(
        cls,
        child: ChildRunIdentity,
        *,
        required_artifacts: Sequence[str],
        git_commit: Optional[str] = None,
    ) -> "ExpectedRunIdentity":
        required = tuple(sorted(set(str(item) for item in required_artifacts)))
        if not required:
            raise ValueError("required_artifacts must not be empty")
        for item in required:
            _safe_relative_artifact(item)
        return cls(
            run_kind=child.run_kind,
            command_identity=child.command_identity,
            manifest_schema_version=child.manifest_schema_version,
            recording_schema_version=child.recording_schema_version,
            scientific_component_fingerprint=child.scientific_component_fingerprint,
            decision_parser_schema_version=child.decision_parser_schema_version,
            decision_parser_source_hash=child.decision_parser_source_hash,
            event_schema_version=child.event_schema_version,
            prompt_source_hash=child.prompt_source_hash,
            persona_source_hash=child.persona_source_hash,
            simulation_core_source_hash=child.simulation_core_source_hash,
            config_hash_schema_version=child.config_hash_schema_version,
            scientific_config_hash=child.scientific_config_hash,
            model_request_config_hash=child.model_request_config_hash,
            scientific_input_identity=child.scientific_input_identity,
            reference_path_content_hash=child.reference_path_content_hash,
            scenario_definition_hash=child.scenario_definition_hash,
            population_identity=child.population_identity,
            seed=child.seed,
            requested_provider=child.requested_provider,
            resolved_provider=child.resolved_provider,
            requested_model=child.requested_model,
            resolved_model=child.resolved_model,
            endpoint_identity=child.endpoint_identity,
            temperature=child.temperature,
            max_tokens=child.max_tokens,
            cache_enabled=child.cache_enabled,
            experiment_slot=child.experiment_slot,
            multi_event_identity=child.multi_event_identity,
            multi_event_material_identity=child.multi_event_material_identity,
            required_artifacts=required,
            git_commit=child.git_commit if git_commit is None else git_commit,
        )

    @classmethod
    def from_manifest(
        cls,
        manifest_path: os.PathLike[str] | str,
        *,
        required_artifacts: Sequence[str],
        git_commit: Optional[str] = None,
    ) -> "ExpectedRunIdentity":
        return cls.from_child(
            ChildRunIdentity.from_manifest(manifest_path),
            required_artifacts=required_artifacts,
            git_commit=git_commit,
        )

    @classmethod
    def from_effective_config(
        cls,
        cfg: Any,
        *,
        command_identity: str,
        required_artifacts: Sequence[str],
        run_kind: str = "simulation",
        input_paths: Any = None,
        repo_root: Optional[os.PathLike[str] | str] = None,
        base_dir: Optional[os.PathLike[str] | str] = None,
        experiment_slot: Optional[Mapping[str, Any]] = None,
        multi_event_identity: Optional[Mapping[str, Any]] = None,
        multi_event_material_identity: Optional[Mapping[str, Any]] = None,
        effective_environment: Optional[Mapping[str, str]] = None,
    ) -> "ExpectedRunIdentity":
        """Build the pre-execution identity without constructing a Provider.

        This uses the same effective Config contract, scientific source
        metadata, and ``runtime_model_config(cfg)`` request resolution as a
        managed run.  It does not call ``build_llm`` and therefore performs no
        Provider request or network access.
        """

        required = tuple(sorted(set(str(item) for item in required_artifacts)))
        if not required:
            raise ValueError("required_artifacts must not be empty")
        for item in required:
            _safe_relative_artifact(item)
        root = Path(
            repo_root or Path(__file__).resolve().parent.parent
        ).resolve()
        working = Path(base_dir or Path.cwd()).resolve()
        source = scientific_compatibility_metadata(root)
        contract = build_effective_config_contract(cfg, base_dir=working)
        environment = (
            os.environ
            if effective_environment is None
            else effective_environment
        )
        model_config = runtime_model_config(cfg, environment=environment)
        input_identity, reference_hash = _effective_input_identity(
            cfg, input_paths=input_paths, base_dir=working
        )
        endpoint_identity = _endpoint_identity_from_parts(
            contract.get("model_request_config_summary", {}).get(
                "openai_base_url"
            ),
            model_config.get("endpoint_sha256"),
        )
        requested_provider = str(
            environment.get("LLM_PROVIDER") or getattr(cfg, "provider", "auto")
        )
        requested_model = (
            environment.get("LLM_MODEL") or getattr(cfg, "model", "") or None
        )
        resolved_provider = str(model_config.get("resolved_provider") or "")
        resolved_model = _resolved_model_without_provider(cfg, model_config)
        if not resolved_provider or not resolved_model:
            raise ResultReuseError(MANIFEST_INVALID, "resolved_model_identity")
        return cls(
            run_kind=str(run_kind),
            command_identity=str(command_identity),
            manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            recording_schema_version=CURRENT_RECORDING_SCHEMA_VERSION,
            scientific_component_fingerprint=str(
                source["scientific_component_fingerprint"]
            ),
            decision_parser_schema_version=str(
                source["decision_parser_schema_version"]
            ),
            decision_parser_source_hash=str(source["decision_parser_source_hash"]),
            event_schema_version=str(source["event_schema_version"]),
            prompt_source_hash=str(source["prompt_source_hash"]),
            persona_source_hash=str(source["persona_source_hash"]),
            simulation_core_source_hash=str(source["simulation_core_source_hash"]),
            config_hash_schema_version=str(contract["config_hash_schema_version"]),
            scientific_config_hash=str(contract["scientific_config_hash"]),
            model_request_config_hash=str(contract["model_request_config_hash"]),
            scientific_input_identity=input_identity,
            reference_path_content_hash=reference_hash,
            scenario_definition_hash=_scenario_definition_hash(cfg),
            population_identity=_population_summary_identity(
                contract["scientific_config_summary"]
            ),
            seed=int(getattr(cfg, "seed")),
            requested_provider=requested_provider,
            resolved_provider=resolved_provider,
            requested_model=requested_model,
            resolved_model=resolved_model,
            endpoint_identity=endpoint_identity,
            temperature=float(getattr(cfg, "temperature")),
            max_tokens=int(getattr(cfg, "max_tokens")),
            cache_enabled=bool(getattr(cfg, "cache_enabled")),
            experiment_slot=(
                _optional_experiment_slot(experiment_slot)
                if experiment_slot is not None
                else None
            ),
            multi_event_identity=(
                _normalize_multi_event_result_identity(multi_event_identity)
                if multi_event_identity is not None
                else None
            ),
            multi_event_material_identity=(
                _normalize_multi_event_material_identity(
                    multi_event_material_identity
                )
                if multi_event_material_identity is not None
                else None
            ),
            required_artifacts=required,
            git_commit=_optional_text(source.get("git_commit")),
        )


@dataclass(frozen=True)
class ReusableRunCandidate:
    path: Path
    allowed_result_root: Path


@dataclass(frozen=True)
class ReuseDecision:
    policy_version: str
    reusable: bool
    reason_codes: tuple[str, ...]
    run_id: Optional[str]
    manifest_path: Optional[Path]
    artifacts_verified: int
    cross_commit_same_scientific_fingerprint: bool

    @property
    def primary_reason(self) -> Optional[str]:
        return self.reason_codes[0] if self.reason_codes else None

    def public_summary(self) -> dict[str, Any]:
        """Return a secret-free summary suitable for a driver manifest."""

        return {
            "result_reuse_policy_version": self.policy_version,
            "reusable": self.reusable,
            "reason_codes": list(self.reason_codes),
            "run_id": self.run_id,
            "artifacts_verified": self.artifacts_verified,
            "cross_commit_same_scientific_fingerprint": (
                self.cross_commit_same_scientific_fingerprint
            ),
        }


@dataclass(frozen=True)
class LegacyAnalysisInput:
    """One explicitly requested historical input, never a reusable child run."""

    path: str
    readable: bool
    size_bytes: Optional[int]
    sha256: Optional[str]
    error_code: Optional[str]
    provenance_class: str = "legacy_unverified_input"


@dataclass(frozen=True)
class LegacyAnalysisInputSummary:
    """Provenance payload for an analysis that reads legacy flat artifacts."""

    inputs: tuple[LegacyAnalysisInput, ...]
    total_files: int
    readable_files: int
    failed_files: int
    identity_unverified_files: int
    provenance_class: str = "legacy_unverified_input"

    def as_manifest_payload(self) -> dict[str, Any]:
        return {
            "provenance_class": self.provenance_class,
            "total_files": self.total_files,
            "readable_files": self.readable_files,
            "failed_files": self.failed_files,
            "identity_unverified_files": self.identity_unverified_files,
            # These are inputs, never executed/reused child-run counters.
            "inputs": [
                {
                    "path": item.path,
                    "readable": item.readable,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "error_code": item.error_code,
                    "provenance_class": item.provenance_class,
                }
                for item in self.inputs
            ],
        }


def inspect_legacy_analysis_inputs(
    paths: Sequence[os.PathLike[str] | str],
) -> LegacyAnalysisInputSummary:
    """Hash explicit historical inputs without creating child-run identities.

    The returned payload intentionally has no ``executed_runs``,
    ``reused_runs``, or ``honest_n_runs`` fields.  An analysis entrypoint may
    attach it to its managed manifest as input provenance, but must not count
    these files as simulation replicates executed by the current experiment.
    """

    inspected: list[LegacyAnalysisInput] = []
    for raw in paths:
        path = Path(raw)
        try:
            if not path.is_file():
                inspected.append(
                    LegacyAnalysisInput(str(path), False, None, None, "not_regular_file")
                )
                continue
            inspected.append(
                LegacyAnalysisInput(
                    str(path),
                    True,
                    path.stat().st_size,
                    sha256_file(path),
                    None,
                )
            )
        except (OSError, RuntimeError):
            inspected.append(
                LegacyAnalysisInput(str(path), False, None, None, "input_read_failed")
            )
    readable = sum(1 for item in inspected if item.readable)
    return LegacyAnalysisInputSummary(
        inputs=tuple(inspected),
        total_files=len(inspected),
        readable_files=readable,
        failed_files=len(inspected) - readable,
        identity_unverified_files=len(inspected),
    )


def load_child_run_identity(
    candidate: ReusableRunCandidate,
) -> tuple[ChildRunIdentity, Optional[Path]]:
    """Resolve a manifest or managed compatibility link inside its result root."""

    root = candidate.allowed_result_root.resolve(strict=True)
    supplied = candidate.path
    if supplied.is_absolute():
        absolute = supplied
    else:
        from_cwd = (Path.cwd() / supplied).resolve(strict=False)
        absolute = from_cwd if _within(from_cwd, root) else root / supplied
    try:
        resolved = absolute.resolve(strict=True)
    except FileNotFoundError as error:
        if absolute.name == "run_manifest.json" or absolute.is_dir():
            raise ResultReuseError(MANIFEST_MISSING) from error
        raise ResultReuseError(LEGACY_FLAT_RESULT_UNVERIFIED) from error
    except (OSError, RuntimeError) as error:
        raise ResultReuseError(UNSAFE_SYMLINK) from error
    if not _within(resolved, root):
        raise ResultReuseError(UNSAFE_SYMLINK)

    compatibility_link: Optional[Path] = None
    if resolved.is_dir():
        manifest_path = resolved / "run_manifest.json"
    elif resolved.name == "run_manifest.json":
        manifest_path = resolved
    else:
        compatibility_link = absolute
        manifest_path = resolved.parent / "run_manifest.json"
        if not manifest_path.is_file():
            raise ResultReuseError(LEGACY_FLAT_RESULT_UNVERIFIED)

    if not manifest_path.is_file():
        raise ResultReuseError(MANIFEST_MISSING)
    if not _within(manifest_path.resolve(strict=True), root):
        raise ResultReuseError(UNSAFE_SYMLINK)
    child = ChildRunIdentity.from_manifest(manifest_path)
    if not _within(child.run_dir.resolve(strict=True), root):
        raise ResultReuseError(UNSAFE_SYMLINK)
    if compatibility_link is not None:
        _validate_compatibility_link(
            compatibility_link, resolved, child, allowed_root=root
        )
    return child, compatibility_link


def validate_child_run_reuse(
    candidate: ReusableRunCandidate,
    expected: ExpectedRunIdentity,
) -> ReuseDecision:
    """Return a stable, public-safe decision without mutating the candidate."""

    try:
        child, _link = load_child_run_identity(candidate)
    except ResultReuseError as error:
        return ReuseDecision(
            RESULT_REUSE_POLICY_VERSION,
            False,
            (error.reason_code,),
            None,
            None,
            0,
            False,
        )

    reasons: list[str] = []
    _mismatch(reasons, child.manifest_schema_version, MANIFEST_SCHEMA_VERSION,
              MANIFEST_SCHEMA_INCOMPATIBLE)
    _mismatch(reasons, child.manifest_schema_version, expected.manifest_schema_version,
              MANIFEST_SCHEMA_INCOMPATIBLE)
    if child.status != "finished":
        reasons.append(STATUS_NOT_FINISHED)
    if child.managed_context_state != "FINISHED":
        reasons.append(MANAGED_RUN_INCOMPLETE)
    if not child.managed_run_completed:
        reasons.append(MANAGED_RUN_INCOMPLETE)
    if not child.outputs_complete:
        reasons.append(OUTPUTS_INCOMPLETE)
    if child.failure_stage is not None:
        reasons.append(FAILURE_STAGE_PRESENT)
    _mismatch(reasons, child.run_kind, "simulation", RUN_KIND_MISMATCH)
    _mismatch(reasons, child.run_kind, expected.run_kind, RUN_KIND_MISMATCH)
    _mismatch(reasons, child.command_identity, expected.command_identity,
              ENTRYPOINT_MISMATCH)

    if not _completion_is_complete(child):
        reasons.append(COMPLETION_INCOMPLETE)
    _mismatch(reasons, child.recording_schema_version,
              CURRENT_RECORDING_SCHEMA_VERSION, RECORDING_SCHEMA_INCOMPATIBLE)
    _mismatch(reasons, child.recording_schema_version,
              expected.recording_schema_version, RECORDING_SCHEMA_INCOMPATIBLE)

    comparisons = (
        (child.scientific_component_fingerprint,
         expected.scientific_component_fingerprint,
         SCIENTIFIC_FINGERPRINT_MISMATCH),
        ((child.decision_parser_schema_version, child.decision_parser_source_hash),
         (expected.decision_parser_schema_version,
          expected.decision_parser_source_hash), DECISION_PARSER_MISMATCH),
        (child.event_schema_version, expected.event_schema_version,
         EVENT_SCHEMA_MISMATCH),
        (child.simulation_core_source_hash, expected.simulation_core_source_hash,
         SIMULATION_CORE_MISMATCH),
        (child.prompt_source_hash, expected.prompt_source_hash, PROMPT_MISMATCH),
        (child.persona_source_hash, expected.persona_source_hash, PERSONA_MISMATCH),
        ((child.config_hash_schema_version, child.scientific_config_hash),
         (expected.config_hash_schema_version, expected.scientific_config_hash),
         SCIENTIFIC_CONFIG_MISMATCH),
        (child.model_request_config_hash, expected.model_request_config_hash,
         MODEL_REQUEST_CONFIG_MISMATCH),
        ((child.requested_provider, child.resolved_provider),
         (expected.requested_provider, expected.resolved_provider),
         PROVIDER_MISMATCH),
        ((child.requested_model, child.resolved_model),
         (expected.requested_model, expected.resolved_model), MODEL_MISMATCH),
        (child.endpoint_identity, expected.endpoint_identity, ENDPOINT_MISMATCH),
        ((child.temperature, child.max_tokens, child.cache_enabled),
         (expected.temperature, expected.max_tokens, expected.cache_enabled),
         MODEL_REQUEST_DETAIL_MISMATCH),
        (child.scenario_definition_hash, expected.scenario_definition_hash,
         SCENARIO_MISMATCH),
        (child.scientific_input_identity, expected.scientific_input_identity,
         INPUT_IDENTITY_MISMATCH),
        (child.reference_path_content_hash, expected.reference_path_content_hash,
         INPUT_IDENTITY_MISMATCH),
        (child.seed, expected.seed, SEED_MISMATCH),
        (child.population_identity, expected.population_identity,
         POPULATION_MISMATCH),
    )
    for actual, wanted, code in comparisons:
        _mismatch(reasons, actual, wanted, code)
    # Policy 1.1 is additive: a legacy caller that does not expect a slot keeps
    # the complete 1.0 compatibility behavior.  A multi-event caller fails
    # closed when the child omits or changes any canonical slot field.
    if expected.experiment_slot is not None:
        _mismatch(
            reasons,
            child.experiment_slot,
            expected.experiment_slot,
            EXPERIMENT_SLOT_MISMATCH,
        )
        _mismatch(
            reasons,
            child.multi_event_identity,
            expected.multi_event_identity,
            RESULT_IDENTITY_MISMATCH,
        )
        _mismatch(
            reasons,
            child.multi_event_material_identity,
            expected.multi_event_material_identity,
            RESULT_IDENTITY_MISMATCH,
        )
        aliases = child.reported_model_aliases
        if (
            aliases is None
            or (expected.requested_provider == "openai" and len(aliases) != 1)
            or (expected.requested_provider == "mock" and aliases != ())
        ):
            reasons.append(REPORTED_MODEL_GATE_REJECTED)
    if not child.population_complete:
        reasons.append(POPULATION_MISMATCH)

    artifacts_verified = 0
    artifact_reasons, artifacts_verified = _verify_artifacts(child, expected)
    reasons.extend(artifact_reasons)
    if not artifact_reasons:
        reasons.extend(_verify_result_identity(child))

    ordered_reasons = tuple(dict.fromkeys(reasons))
    reusable = not ordered_reasons
    cross_commit = bool(
        reusable
        and child.git_commit
        and expected.git_commit
        and child.git_commit != expected.git_commit
        and child.scientific_component_fingerprint
        == expected.scientific_component_fingerprint
    )
    return ReuseDecision(
        RESULT_REUSE_POLICY_VERSION,
        reusable,
        ordered_reasons,
        child.run_id,
        child.manifest_path,
        artifacts_verified,
        cross_commit,
    )


def _mapping(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    item = value.get(field)
    if not isinstance(item, Mapping):
        raise ResultReuseError(MANIFEST_INVALID, field)
    return item


def _required_text(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ResultReuseError(MANIFEST_INVALID, field)
    return item


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResultReuseError(MANIFEST_INVALID)
    return value


def _required_bool(value: Mapping[str, Any], field: str) -> bool:
    item = value.get(field)
    if not isinstance(item, bool):
        raise ResultReuseError(MANIFEST_INVALID, field)
    return item


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise ResultReuseError(MANIFEST_INVALID)


def _required_int(value: Mapping[str, Any], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ResultReuseError(MANIFEST_INVALID, field)
    return item


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultReuseError(MANIFEST_INVALID)
    return value


def _required_number(value: Mapping[str, Any], field: str) -> float:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ResultReuseError(MANIFEST_INVALID, field)
    return float(item)


def _required_hash(value: Mapping[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not _HEX_SHA256.fullmatch(item):
        raise ResultReuseError(MANIFEST_INVALID, field)
    return item


def _optional_hash(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX_SHA256.fullmatch(value):
        raise ResultReuseError(MANIFEST_INVALID)
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _optional_experiment_slot(value: Any) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    try:
        from .multi_event import validate_experiment_slot

        return validate_experiment_slot(value)
    except (TypeError, ValueError) as error:
        raise ResultReuseError(EXPERIMENT_SLOT_MISMATCH) from error


def _input_hashes_by_label(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        raise ResultReuseError(MANIFEST_INVALID, "inputs")
    result: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ResultReuseError(MANIFEST_INVALID, "inputs")
        label = _required_text(item, "label")
        if label in result:
            raise ResultReuseError(MANIFEST_INVALID, "inputs")
        result[label] = _required_hash(item, "sha256")
    return result


def _manifest_multi_event_identity(
    raw: Mapping[str, Any],
    slot: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    value = raw.get("multi_event")
    if slot is None:
        if value is None:
            return None
        raise ResultReuseError(EXPERIMENT_SLOT_MISMATCH)
    if not isinstance(value, Mapping):
        raise ResultReuseError(EXPERIMENT_SLOT_MISMATCH)
    try:
        protocol_hash = _required_hash(value, "protocol_sha256")
        reference_hash = _required_hash(value, "reference_csv_sha256")
        timeline_hash = _required_hash(value, "news_timeline_sha256")
        transform_hash = _required_hash(value, "reference_transform_sha256")
        if value.get("schema_version") != "1.0":
            raise ResultReuseError(EXPERIMENT_SLOT_MISMATCH)
        if value.get("event_id") != slot["event_id"]:
            raise ResultReuseError(EXPERIMENT_SLOT_MISMATCH)
        if protocol_hash != slot["protocol_hash"]:
            raise ResultReuseError(EXPERIMENT_SLOT_MISMATCH)
        input_hashes = _input_hashes_by_label(raw.get("inputs"))
        expected_inputs = {
            "reference_path": reference_hash,
            "news_timeline_jsonl": timeline_hash,
            "multi_event_protocol": protocol_hash,
            "reference_catalog": _required_hash(value, "catalog_sha256"),
        }
        if any(input_hashes.get(label) != digest for label, digest in expected_inputs.items()):
            raise ResultReuseError(INPUT_IDENTITY_MISMATCH)
    except KeyError as error:
        raise ResultReuseError(EXPERIMENT_SLOT_MISMATCH) from error
    return {
        "schema_version": "1.0",
        "protocol_sha256": protocol_hash,
        "event_id": slot["event_id"],
        "arm": slot["social_arm"],
        "seed": slot["seed"],
        "repeat_idx": slot["repeat_idx"],
        "reference_csv_sha256": reference_hash,
        "news_timeline_sha256": timeline_hash,
        "reference_transform_sha256": transform_hash,
    }


def _normalize_multi_event_result_identity(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != _MULTI_EVENT_RESULT_KEYS:
        raise ResultReuseError(RESULT_IDENTITY_MISMATCH, "multi_event_identity")
    try:
        normalized = {
            "schema_version": "1.0",
            "protocol_sha256": _required_hash(value, "protocol_sha256"),
            "event_id": _required_text(value, "event_id"),
            "arm": _required_text(value, "arm"),
            "seed": _required_int(value, "seed"),
            "repeat_idx": _required_int(value, "repeat_idx"),
            "reference_csv_sha256": _required_hash(
                value, "reference_csv_sha256"
            ),
            "news_timeline_sha256": _required_hash(
                value, "news_timeline_sha256"
            ),
            "reference_transform_sha256": _required_hash(
                value, "reference_transform_sha256"
            ),
        }
    except ResultReuseError as error:
        raise ResultReuseError(
            RESULT_IDENTITY_MISMATCH, "multi_event_identity"
        ) from error
    if (
        value.get("schema_version") != "1.0"
        or normalized["arm"] not in {"social_on", "social_off"}
        or normalized["repeat_idx"] < 1
        or dict(value) != normalized
    ):
        raise ResultReuseError(RESULT_IDENTITY_MISMATCH, "multi_event_identity")
    return normalized


def _normalize_multi_event_material_identity(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != _MULTI_EVENT_MATERIAL_KEYS:
        raise ResultReuseError(
            RESULT_IDENTITY_MISMATCH, "multi_event_material_identity"
        )
    result = _normalize_multi_event_result_identity(
        {key: value[key] for key in _MULTI_EVENT_RESULT_KEYS}
    )
    try:
        normalized = {
            **result,
            "catalog_sha256": _required_hash(value, "catalog_sha256"),
            "event_definition_sha256": _required_hash(
                value, "event_definition_sha256"
            ),
            "reference_transform_id": _required_text(
                value, "reference_transform_id"
            ),
            "timeline_transform_sha256": _required_hash(
                value, "timeline_transform_sha256"
            ),
            "combined_transform_sha256": _required_hash(
                value, "combined_transform_sha256"
            ),
        }
    except ResultReuseError as error:
        raise ResultReuseError(
            RESULT_IDENTITY_MISMATCH, "multi_event_material_identity"
        ) from error
    if dict(value) != normalized:
        raise ResultReuseError(
            RESULT_IDENTITY_MISMATCH, "multi_event_material_identity"
        )
    return normalized


def _manifest_multi_event_material_identity(
    raw: Mapping[str, Any],
    slot: Optional[Mapping[str, Any]],
    result_identity: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    if slot is None:
        return None
    value = raw.get("multi_event")
    if not isinstance(value, Mapping) or result_identity is None:
        raise ResultReuseError(RESULT_IDENTITY_MISMATCH, "multi_event")
    candidate = {
        **result_identity,
        "catalog_sha256": value.get("catalog_sha256"),
        "event_definition_sha256": value.get("event_definition_sha256"),
        "reference_transform_id": value.get("reference_transform_id"),
        "timeline_transform_sha256": value.get("timeline_transform_sha256"),
        "combined_transform_sha256": value.get("combined_transform_sha256"),
    }
    return _normalize_multi_event_material_identity(candidate)


def _normalized_aliases(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(alias, str) or not alias for alias in value
    ):
        raise ResultReuseError(REPORTED_MODEL_GATE_REJECTED)
    aliases = tuple(value)
    if list(aliases) != sorted(set(aliases)):
        raise ResultReuseError(REPORTED_MODEL_GATE_REJECTED)
    return aliases


def _manifest_reported_model_aliases(
    raw: Mapping[str, Any], slot: Optional[Mapping[str, Any]]
) -> Optional[tuple[str, ...]]:
    if slot is None:
        return None
    multi_event = raw.get("multi_event")
    completion = raw.get("completion")
    if not isinstance(multi_event, Mapping) or not isinstance(completion, Mapping):
        raise ResultReuseError(REPORTED_MODEL_GATE_REJECTED)
    attempts = completion.get("application_provider_attempts")
    if (
        not isinstance(attempts, Mapping)
        or attempts.get("reported_models_truncated") is not False
    ):
        raise ResultReuseError(REPORTED_MODEL_GATE_REJECTED)
    manifest_aliases = _normalized_aliases(
        multi_event.get("reported_model_aliases")
    )
    completion_aliases = _normalized_aliases(attempts.get("reported_models"))
    if manifest_aliases != completion_aliases:
        raise ResultReuseError(REPORTED_MODEL_GATE_REJECTED)
    return manifest_aliases


def _input_identity(value: Any) -> tuple[str, Optional[str]]:
    if not isinstance(value, list):
        raise ResultReuseError(MANIFEST_INVALID, "inputs")
    items = []
    reference_hash = None
    for item in value:
        if not isinstance(item, Mapping):
            raise ResultReuseError(MANIFEST_INVALID, "inputs")
        label = _required_text(item, "label")
        exists = _required_bool(item, "exists")
        kind = _required_text(item, "kind")
        size = _required_int(item, "size_bytes")
        digest = _required_hash(item, "sha256")
        if not exists or kind != "file" or size < 0 or item.get("error") is not None:
            raise ResultReuseError(MANIFEST_INVALID, "inputs")
        safe = {"label": label, "kind": kind, "size_bytes": size, "sha256": digest}
        items.append(safe)
        if label == "reference_path":
            reference_hash = digest
    items.sort(key=lambda item: _canonical_json(item))
    return _stable_hash(items), reference_hash


def _normalise_input_paths(input_paths: Any) -> list[tuple[str, Path]]:
    if input_paths is None:
        return []
    if isinstance(input_paths, Mapping):
        return [(str(label), Path(path)) for label, path in input_paths.items() if path]
    if isinstance(input_paths, (str, os.PathLike)):
        return [("input", Path(input_paths))]
    return [
        ("input_{}".format(index), Path(path))
        for index, path in enumerate(input_paths)
        if path
    ]


def _effective_input_identity(
    cfg: Any, *, input_paths: Any, base_dir: Path
) -> tuple[str, Optional[str]]:
    paths = _normalise_input_paths(input_paths)
    reference_path = getattr(cfg, "reference_path", "")
    if reference_path and not any(label == "reference_path" for label, _ in paths):
        paths.append(("reference_path", Path(reference_path)))
    descriptors = []
    reference_hash = None
    for label, raw in paths:
        path = raw if raw.is_absolute() else base_dir / raw
        path = path.resolve(strict=False)
        if not path.is_file():
            raise ResultReuseError(MANIFEST_INVALID, "scientific_input")
        digest = sha256_file(path)
        item = {
            "label": label,
            "kind": "file",
            "size_bytes": path.stat().st_size,
            "sha256": digest,
        }
        descriptors.append(item)
        if label == "reference_path":
            reference_hash = digest
    descriptors.sort(key=lambda item: _canonical_json(item))
    return _stable_hash(descriptors), reference_hash


def _scenario_definition_hash(cfg: Any) -> str:
    return _stable_hash(
        {
            "n_rounds": getattr(cfg, "n_rounds", None),
            "news_round": getattr(cfg, "news_round", None),
            "news_text": getattr(cfg, "news_text", None),
            "news_timeline": _scenario_jsonable(
                getattr(cfg, "news_timeline", ())
            ),
            "population": getattr(cfg, "population", None),
            "seed_fraction": getattr(cfg, "seed_fraction", None),
        }
    )


def _scenario_jsonable(value: Any) -> Any:
    """Mirror provenance._jsonable for the offline scenario identity."""

    if is_dataclass(value):
        return _scenario_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _scenario_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scenario_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _resolved_model_without_provider(
    cfg: Any, model_config: Mapping[str, Any]
) -> Optional[str]:
    model = model_config.get("model")
    if isinstance(model, str) and model:
        return model
    if model_config.get("resolved_provider") != "anthropic":
        return None
    # AnthropicLLM's defaults are source constants, not a network discovery.
    # Importing them does not construct the SDK client or read a credential.
    from .llm import _DEFAULT_CHEAP, _DEFAULT_MODEL

    if bool(getattr(cfg, "use_cheap_model", False)):
        return str(
            os.environ.get("LLM_CHEAP_MODEL")
            or getattr(cfg, "cheap_model", "")
            or _DEFAULT_CHEAP
        )
    return _DEFAULT_MODEL


_POPULATION_CONFIG_FIELDS = (
    "max_llm_agents",
    "n_llm_agents",
    "n_noise_agents",
    "population",
)


def _population_summary_identity(scientific_summary: Mapping[str, Any]) -> str:
    try:
        payload = {name: scientific_summary[name] for name in _POPULATION_CONFIG_FIELDS}
    except (KeyError, TypeError) as error:
        raise ResultReuseError(MANIFEST_INVALID, "population_contract") from error
    return _stable_hash(payload)


def _population_contract_identity(raw: Mapping[str, Any]) -> str:
    summary = raw.get("scientific_config_summary")
    if not isinstance(summary, Mapping):
        raise ResultReuseError(MANIFEST_INVALID, "scientific_config_summary")
    return _population_summary_identity(summary)


def _population_complete(population: Mapping[str, Any]) -> bool:
    try:
        planned_llm = _required_int(population, "planned_llm_total")
        planned_noise = _required_int(population, "planned_noise_total")
        actual_llm = _required_int(population, "actual_llm_total")
        actual_noise = _required_int(population, "actual_noise_total")
        agent_ids = population.get("actual_agent_ids")
        return bool(
            isinstance(agent_ids, list)
            and planned_llm == actual_llm
            and planned_noise == actual_noise
            and len(agent_ids) == actual_llm + actual_noise
        )
    except ResultReuseError:
        return False


def _artifact_identities(value: Any) -> tuple[ArtifactIdentity, ...]:
    if not isinstance(value, list):
        raise ResultReuseError(MANIFEST_INVALID, "results")
    artifacts = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping) or item.get("inside_run_directory") is not True:
            continue
        path = _required_text(item, "path")
        _safe_relative_artifact(path)
        if path in seen:
            raise ResultReuseError(MANIFEST_INVALID, "results")
        seen.add(path)
        digest = _required_hash(item, "sha256")
        size = _required_int(item, "size_bytes")
        registered = bool(
            item.get("exists") is True
            and item.get("kind") == "file"
            and item.get("error") is None
            and size >= 0
        )
        artifacts.append(ArtifactIdentity(path, digest, size, registered))
    if not artifacts:
        raise ResultReuseError(MANIFEST_INVALID, "results")
    return tuple(sorted(artifacts, key=lambda item: item.path))


def _legacy_link_identities(value: Any) -> tuple[LegacyLinkIdentity, ...]:
    if not isinstance(value, Mapping):
        raise ResultReuseError(MANIFEST_INVALID, "compatibility")
    raw = value.get("legacy_links", [])
    if not isinstance(raw, list):
        raise ResultReuseError(MANIFEST_INVALID, "compatibility.legacy_links")
    links = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ResultReuseError(MANIFEST_INVALID, "compatibility.legacy_links")
        path = _required_text(item, "path")
        target = _required_text(item, "target")
        links.append(LegacyLinkIdentity(Path(path).name, target))
    return tuple(sorted(links, key=lambda item: (item.path_name, item.target)))


def _endpoint_identity(raw: Mapping[str, Any], model_config: Mapping[str, Any]) -> str:
    model_summary = raw.get("model_request_config_summary")
    endpoint_summary = (
        model_summary.get("openai_base_url")
        if isinstance(model_summary, Mapping)
        else None
    )
    return _endpoint_identity_from_parts(
        endpoint_summary, model_config.get("endpoint_sha256")
    )


def _endpoint_identity_from_parts(
    endpoint_summary: Any, runtime_hash: Any
) -> str:
    payload = {
        "configured_endpoint": endpoint_summary,
        "runtime_endpoint_sha256": runtime_hash,
    }
    # Endpoint summaries are already credential-redacted/hash-only by the
    # config contract.  Reject an accidental raw string rather than storing it.
    if endpoint_summary is not None and not isinstance(endpoint_summary, Mapping):
        raise ResultReuseError(MANIFEST_INVALID, "endpoint_identity")
    if runtime_hash is not None and (
        not isinstance(runtime_hash, str) or not _HEX_SHA256.fullmatch(runtime_hash)
    ):
        raise ResultReuseError(MANIFEST_INVALID, "endpoint_identity")
    return _stable_hash(payload)


def _safe_relative_artifact(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise ResultReuseError(UNSAFE_ARTIFACT_PATH)
    return path


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_compatibility_link(
    link: Path,
    resolved: Path,
    child: ChildRunIdentity,
    *,
    allowed_root: Path,
) -> None:
    if not link.is_symlink():
        # A copied flat file is historical input, not a managed resume handle.
        raise ResultReuseError(LEGACY_FLAT_RESULT_UNVERIFIED)
    if not _within(resolved, allowed_root) or not _within(
        resolved, child.run_dir.resolve(strict=True)
    ):
        raise ResultReuseError(UNSAFE_SYMLINK)
    matches = [
        item
        for item in child.legacy_links
        if item.path_name == link.name
        and (allowed_root / item.target).resolve(strict=False) == resolved
    ]
    if not matches:
        raise ResultReuseError(LEGACY_LINK_IDENTITY_MISMATCH)


def _completion_is_complete(child: ChildRunIdentity) -> bool:
    if not child.completion_complete:
        return False
    if (
        child.simulation_runs_started != 1
        or child.simulation_runs_completed != 1
        or child.simulation_runs_failed != 0
    ):
        return False
    if child.simulation_runs_planned not in (None, 1):
        return False
    if child.agent_decisions_completed <= 0 or child.agent_decisions_failed != 0:
        return False
    if (
        child.agent_decisions_planned is not None
        and child.agent_decisions_planned != child.agent_decisions_completed
    ):
        return False
    return True


def _completion_payload_complete(completion: Mapping[str, Any]) -> bool:
    """Check internal unit/count consistency without changing fallback policy."""

    try:
        simulations = _mapping(completion, "simulation_runs")
        rounds = _mapping(completion, "rounds")
        decisions = _mapping(completion, "agent_decisions")
        requests = _mapping(completion, "llm_logical_requests")
        sources = _mapping(completion, "response_sources")
        provider_calls = _mapping(completion, "provider_calls")
        parsing = _mapping(completion, "parsing")

        simulation_ok = (
            _required_int(simulations, "planned") == 1
            and _required_int(simulations, "started") == 1
            and _required_int(simulations, "completed") == 1
            and _required_int(simulations, "failed") == 0
        )
        planned_rounds = _required_int(rounds, "planned")
        rounds_ok = (
            planned_rounds > 0
            and _required_int(rounds, "started") == planned_rounds
            and _required_int(rounds, "completed") == planned_rounds
            and _required_int(rounds, "failed") == 0
            and _required_int(rounds, "skipped") == 0
        )
        planned_decisions = _required_int(decisions, "planned")
        decisions_ok = (
            planned_decisions > 0
            and _required_int(decisions, "attempted") == planned_decisions
            and _required_int(decisions, "completed") == planned_decisions
            and _required_int(decisions, "failed") == 0
            and _required_int(decisions, "skipped") == 0
        )
        planned_requests = _required_int(requests, "planned")
        completed_requests = _required_int(requests, "completed")
        requests_ok = (
            planned_requests == planned_decisions
            and _required_int(requests, "attempted") == planned_requests
            and completed_requests == planned_requests
            and _required_int(requests, "failed") == 0
        )
        source_total = sum(
            _required_int(sources, name) for name in ("provider", "cache", "replay")
        )
        sources_ok = source_total == completed_requests
        provider_attempted = _required_int(provider_calls, "attempted")
        provider_succeeded = _required_int(provider_calls, "succeeded")
        provider_failed = _required_int(provider_calls, "failed")
        provider_ok = (
            provider_attempted == provider_succeeded + provider_failed
            and provider_failed == 0
            and _required_int(sources, "provider") == provider_succeeded
        )
        parse_attempted = _required_int(parsing, "attempted")
        parsing_ok = (
            parse_attempted == planned_decisions
            and _required_int(parsing, "succeeded")
            + _required_int(parsing, "failed")
            == parse_attempted
            and _required_int(parsing, "fallbacks") >= 0
        )
        return all(
            (
                simulation_ok,
                rounds_ok,
                decisions_ok,
                requests_ok,
                sources_ok,
                provider_ok,
                parsing_ok,
            )
        )
    except ResultReuseError:
        return False


def _mismatch(reasons: list[str], actual: Any, expected: Any, code: str) -> None:
    if actual != expected:
        reasons.append(code)


def _verify_artifacts(
    child: ChildRunIdentity, expected: ExpectedRunIdentity
) -> tuple[list[str], int]:
    reasons: list[str] = []
    by_path = {item.path: item for item in child.artifacts}
    verified = 0
    required = tuple(sorted(set(expected.required_artifacts)))
    if not required:
        return [ARTIFACT_MISSING], 0
    missing_required = sorted(set(required) - set(by_path))
    if missing_required:
        reasons.append(ARTIFACT_MISSING)
    # Re-hash every registered canonical artifact, not merely the driver's
    # primary result.  ``required`` is the entrypoint-specific completeness
    # gate; the manifest artifact set is the integrity gate.
    for relative in sorted(set(by_path) | set(required)):
        try:
            rel = _safe_relative_artifact(relative)
        except ResultReuseError as error:
            reasons.append(error.reason_code)
            continue
        identity = by_path.get(relative)
        if identity is None:
            reasons.append(ARTIFACT_MISSING)
            continue
        if not identity.registered:
            reasons.append(ARTIFACT_INVALID)
            continue
        path = child.run_dir / rel
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            reasons.append(ARTIFACT_MISSING)
            continue
        except (OSError, RuntimeError):
            reasons.append(UNSAFE_SYMLINK)
            continue
        run_root = child.run_dir.resolve(strict=True)
        if not _within(resolved, run_root):
            reasons.append(UNSAFE_SYMLINK if path.is_symlink() else UNSAFE_ARTIFACT_PATH)
            continue
        if not resolved.is_file():
            reasons.append(ARTIFACT_INVALID)
            continue
        try:
            size = resolved.stat().st_size
            digest = sha256_file(resolved)
        except OSError:
            reasons.append(ARTIFACT_INVALID)
            continue
        if size != identity.size_bytes or digest != identity.sha256:
            reasons.append(ARTIFACT_HASH_MISMATCH)
            continue
        verified += 1
    return list(dict.fromkeys(reasons)), verified


def _verify_result_identity(child: ChildRunIdentity) -> list[str]:
    artifact = {item.path: item for item in child.artifacts}.get(
        "experiment_result.json"
    )
    if artifact is None:
        return []
    path = child.run_dir / "experiment_result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, Mapping):
            return [RESULT_IDENTITY_MISMATCH]
        result_completion = _mapping(result, "completion")
        run_completion = _mapping(result_completion, "simulation_runs")
        decisions = _mapping(result_completion, "agent_decisions")
        checks = [
            result.get("run_id") == child.run_id,
            result.get("seed") == child.seed,
            result.get("model") == child.resolved_model,
            run_completion.get("completed") == child.simulation_runs_completed,
            run_completion.get("failed") == child.simulation_runs_failed,
            decisions.get("completed") == child.agent_decisions_completed,
            decisions.get("failed") == child.agent_decisions_failed,
            _stable_hash(result_completion) == child.completion_identity,
        ]
        if child.experiment_slot is not None:
            material = {
                **dict(child.multi_event_identity or {}),
                "catalog_sha256": result.get("catalog_sha256"),
                "event_definition_sha256": result.get(
                    "event_definition_sha256"
                ),
                "reference_transform_id": result.get("reference_transform_id"),
                "timeline_transform_sha256": result.get(
                    "timeline_transform_sha256"
                ),
                "combined_transform_sha256": result.get(
                    "combined_transform_sha256"
                ),
            }
            checks.extend(
                (
                    result.get("experiment_slot") == child.experiment_slot,
                    result.get("multi_event_identity")
                    == child.multi_event_identity,
                    result.get("repeat_idx")
                    == child.experiment_slot["repeat_idx"],
                    result.get("rep")
                    == child.experiment_slot["repeat_idx"],
                    tuple(result.get("reported_model_aliases", ()))
                    == child.reported_model_aliases,
                    material == child.multi_event_material_identity,
                )
            )
        return [] if all(checks) else [RESULT_IDENTITY_MISMATCH]
    except (OSError, UnicodeError, json.JSONDecodeError, ResultReuseError):
        return [RESULT_IDENTITY_MISMATCH]


__all__ = [
    "RESULT_REUSE_POLICY_VERSION",
    "REUSE_REASON_CODES",
    "EXPERIMENT_SLOT_MISMATCH",
    "REPORTED_MODEL_GATE_REJECTED",
    "ArtifactIdentity",
    "ChildRunIdentity",
    "ExpectedRunIdentity",
    "LegacyLinkIdentity",
    "ReusableRunCandidate",
    "ReuseDecision",
    "ResultReuseError",
    "LegacyAnalysisInput",
    "LegacyAnalysisInputSummary",
    "inspect_legacy_analysis_inputs",
    "load_child_run_identity",
    "validate_child_run_reuse",
    # Stable public reason codes.
    "MANIFEST_MISSING",
    "MANIFEST_INVALID",
    "STATUS_NOT_FINISHED",
    "MANAGED_RUN_INCOMPLETE",
    "OUTPUTS_INCOMPLETE",
    "SCIENTIFIC_FINGERPRINT_MISMATCH",
    "SCIENTIFIC_CONFIG_MISMATCH",
    "MODEL_REQUEST_CONFIG_MISMATCH",
    "PROVIDER_MISMATCH",
    "MODEL_MISMATCH",
    "PROMPT_MISMATCH",
    "PERSONA_MISMATCH",
    "SCENARIO_MISMATCH",
    "SEED_MISMATCH",
    "POPULATION_MISMATCH",
    "ARTIFACT_MISSING",
    "ARTIFACT_HASH_MISMATCH",
    "UNSAFE_SYMLINK",
    "LEGACY_FLAT_RESULT_UNVERIFIED",
    "HEALTH_GATE_REJECTED",
]
