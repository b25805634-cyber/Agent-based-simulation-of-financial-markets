"""Stable scientific-component identity for strict LLM replay.

The fingerprint is deliberately narrower than a Git commit.  It covers the
code and data that can change prompts, parsed decisions, orders, prices,
propagation, leverage, validation metrics, or managed-run scientific ordering;
ordinary repository documentation is excluded.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Optional

from .events import SCHEMA_VERSION as EVENT_SCHEMA_VERSION
from .recording_schema import (
    CURRENT_RECORDING_SCHEMA_VERSION,
    SOURCE_COMPATIBILITY_FIELDS,
)


FINGERPRINT_SCHEMA_VERSION = "1.0"
DECISION_PARSER_SCHEMA_VERSION = "1.0"
RECORDING_SCHEMA_VERSION = CURRENT_RECORDING_SCHEMA_VERSION

# Conservative source boundary derived from the actual Config -> Agent prompt
# -> LLM parse -> simulation -> market/social/risk -> validation call graph.
# Instrumentation-only modules (recording, provenance, reparse audit), tests,
# experiment drivers, and documentation are intentionally absent.
SCIENTIFIC_COMPONENT_FILES = (
    "nmsim/agents.py",
    "nmsim/config.py",
    "nmsim/contagion.py",
    "nmsim/leverage.py",
    "nmsim/llm.py",
    "nmsim/market.py",
    "nmsim/prompts.py",
    "nmsim/sim.py",
    "nmsim/types.py",
    "nmsim/validation.py",
)

# Preserve Phase 1's established prompt-source identity: prompts.py owns the
# versioned Persona/system template source. Agent.build_prompt is independently
# covered by the simulation core source set.
PROMPT_SOURCE_FILES = (
    "nmsim/prompts.py",
)

STRICT_COMPATIBILITY_FIELDS = SOURCE_COMPATIBILITY_FIELDS


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _module_ast(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8")
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    return source, ast.parse(source, filename=path.name)


def _function_source(path: Path, function_name: str) -> bytes:
    """Return one top-level function's LF-normalised source from ``path``."""

    source, tree = _module_ast(path)
    lines = source.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            if node.end_lineno is None:
                raise ValueError("AST has no end line for {}".format(function_name))
            start_lineno = min(
                [node.lineno]
                + [decorator.lineno for decorator in node.decorator_list]
            )
            return "".join(lines[start_lineno - 1:node.end_lineno]).encode("utf-8")
    raise ValueError("function {} not found in {}".format(function_name, path.name))


def _literal_assignment(path: Path, variable_name: str) -> Any:
    """Read a literal top-level assignment without importing another tree."""

    _source, tree = _module_ast(path)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == variable_name for target in targets):
            return ast.literal_eval(node.value)
    raise ValueError("literal {} not found in {}".format(variable_name, path.name))


def hash_relative_files(repo_root: Path, relative_paths: Iterable[str]) -> str:
    """Hash relative names and raw bytes in a stable, unambiguous order.

    Absolute roots, filesystem traversal order, timestamps, permissions, and
    run artifacts never enter this digest.  Including the relative name avoids
    collisions between the same byte strings assigned to different components.
    """

    root = Path(repo_root).resolve()
    digest = hashlib.sha256(b"nmsim-relative-source-set-v1\0")
    for relative in sorted(str(path).replace("\\", "/") for path in relative_paths):
        relative_bytes = relative.encode("utf-8")
        content = (root / Path(relative)).read_bytes()
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def component_file_hashes(repo_root: Path) -> dict[str, str]:
    root = Path(repo_root).resolve()
    return {
        relative: _sha256((root / relative).read_bytes())
        for relative in sorted(SCIENTIFIC_COMPONENT_FILES)
    }


def _git_identity(repo_root: Path) -> dict[str, Any]:
    """Read commit/dirty only; failure is represented, never guessed."""

    root = Path(repo_root).resolve()
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
            text=True,
        )
        if commit.returncode != 0:
            return {"commit": None, "dirty": None}
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1",
             "--untracked-files=all"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
            text=True,
        )
        return {
            "commit": commit.stdout.strip() or None,
            "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "dirty": None}


def scientific_compatibility_metadata(
    repo_root: Optional[Path] = None,
    *,
    git_state: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Return the complete strict-replay compatibility identity."""

    root = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
    parser_hash = _sha256(_function_source(root / "nmsim/llm.py", "parse_order"))
    prompt_hash = _sha256((root / PROMPT_SOURCE_FILES[0]).read_bytes())
    persona_hash = _sha256(
        _canonical_json(_literal_assignment(root / "nmsim/prompts.py", "PERSONAS"))
    )
    simulation_hash = hash_relative_files(root, SCIENTIFIC_COMPONENT_FILES)
    file_hashes = component_file_hashes(root)

    scientific_payload = {
        "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
        "decision_parser_schema_version": DECISION_PARSER_SCHEMA_VERSION,
        "decision_parser_source_hash": parser_hash,
        "prompt_source_hash": prompt_hash,
        "persona_source_hash": persona_hash,
        "simulation_core_source_hash": simulation_hash,
        "scientific_component_files": list(sorted(SCIENTIFIC_COMPONENT_FILES)),
    }
    scientific_fingerprint = _sha256(_canonical_json(scientific_payload))
    git = dict(git_state) if git_state is not None else _git_identity(root)

    return {
        **scientific_payload,
        "scientific_component_file_hashes": file_hashes,
        "scientific_component_fingerprint": scientific_fingerprint,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "recording_schema_version": RECORDING_SCHEMA_VERSION,
        "git_commit": git.get("commit"),
        "git_dirty": git.get("dirty"),
    }


__all__ = [
    "DECISION_PARSER_SCHEMA_VERSION",
    "EVENT_SCHEMA_VERSION",
    "FINGERPRINT_SCHEMA_VERSION",
    "PROMPT_SOURCE_FILES",
    "RECORDING_SCHEMA_VERSION",
    "SCIENTIFIC_COMPONENT_FILES",
    "STRICT_COMPATIBILITY_FIELDS",
    "component_file_hashes",
    "hash_relative_files",
    "scientific_compatibility_metadata",
]
