"""Verified historical analysis inputs for the new information-market pipeline.

This is not child-run resume. A source stays immutable and is only consumed as
an explicitly hashed input. No provider or managed-run object is constructed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "information-market-input-audit/1.0"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_unique_object,
                         parse_constant=lambda _x: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _source_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid artifact path")
    name = PurePosixPath(relative)
    if name.is_absolute() or ".." in name.parts or str(name) != relative:
        raise ValueError("artifact path must be canonical and relative")
    current = root
    for part in name.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink artifact is not accepted")
    if not current.is_file():
        raise ValueError("missing regular artifact")
    return current


def snapshot_run(root: Path) -> list[dict]:
    """Hash regular source files and their exact mode/mtime without editing."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source must be a regular run directory")
    rows = []
    for path in sorted(root.rglob("*")):
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ValueError("source contains a symlink")
        if stat.S_ISDIR(before.st_mode):
            continue
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source contains a non-regular artifact")
        digest = file_sha256(path)
        after = path.stat()
        stable_fields = ("st_size", "st_mtime_ns", "st_mode", "st_ino")
        if any(getattr(before, key) != getattr(after, key) for key in stable_fields):
            raise ValueError("source changed while being read")
        rows.append({"path": path.relative_to(root).as_posix(),
                     "size_bytes": before.st_size,
                     "mode": format(stat.S_IMODE(before.st_mode), "04o"),
                     "mtime_ns": before.st_mtime_ns, "sha256": digest})
    return rows


def verify_run(root: Path, expected_manifest_sha256: str | None = None) -> dict:
    """Check finished lifecycle and every registered internal artifact.

    The resulting receipt states analysis-input status, not source-code or
    child-resume compatibility. Private files are hashed but never parsed.
    """
    root = Path(root)
    manifest_path = _source_file(root, "run_manifest.json")
    manifest_sha = file_sha256(manifest_path)
    if expected_manifest_sha256 is not None and manifest_sha != expected_manifest_sha256:
        raise ValueError("source manifest SHA-256 mismatch")
    manifest = read_json(manifest_path)
    if (manifest.get("status") != "finished" or
            manifest.get("outputs_complete") is not True or
            manifest.get("managed_run_completed") is not True):
        raise ValueError("source is not a completed managed run")
    descriptors = manifest.get("results")
    if not isinstance(descriptors, list) or not descriptors:
        raise ValueError("source has no registered artifacts")
    snapshot = snapshot_run(root)
    inventory = {item["path"]: item for item in snapshot}
    if inventory["run_manifest.json"]["sha256"] != manifest_sha:
        raise ValueError("manifest changed during verification")
    registered = []
    for entry in descriptors:
        if entry.get("inside_run_directory") is not True:
            raise ValueError("external registered artifact unsupported for this input")
        name = entry.get("path")
        _source_file(root, name)
        if name in registered or name == "run_manifest.json":
            raise ValueError("duplicate or recursive artifact registration")
        item = inventory[name]
        if (entry.get("error") is not None or entry.get("exists") is not True or
                entry.get("kind") != "file" or entry.get("sha256") != item["sha256"] or
                entry.get("size_bytes") != item["size_bytes"]):
            raise ValueError("registered artifact integrity mismatch")
        if Path(name).name.startswith("private_") and item["mode"] != "0600":
            raise ValueError("private artifact mode must be 0600")
        registered.append(name)
    return {"schema_version": SCHEMA_VERSION,
            "input_use": "verified_historical_analysis_input_not_resumed_child",
            "run_id": manifest["run_id"],
            "manifest_sha256": manifest_sha,
            "manifest_pin_supplied": expected_manifest_sha256 is not None,
            "registered_artifacts_verified": len(registered),
            "registered_artifact_paths": sorted(registered),
            "regular_file_count": len(snapshot),
            "total_size_bytes": sum(item["size_bytes"] for item in snapshot),
            "files": snapshot, "snapshot_hash": canonical_hash(snapshot)}


def assert_unchanged(root: Path, receipt: dict) -> None:
    if snapshot_run(root) != receipt["files"]:
        raise ValueError("historical source bytes/modes/mtimes changed")


def ensure_separate_output(out_root: Path, input_runs) -> None:
    """Reject an output tree inside an immutable input before reserving a run.

    An output parent containing old sibling runs is allowed; an output root or
    its runs symlink resolving into an input is not. Callers must not try to
    write failed-attempt provenance to a rejected output location.
    """
    destinations = [Path(out_root).resolve(), (Path(out_root)/"runs").resolve()]
    for source in input_runs:
        if source is None:
            continue
        original = Path(source).resolve()
        if any(path == original or original in path.parents for path in destinations):
            raise ValueError("output overlaps an immutable input run")


def preflight_output_separation(argv, default_out: str) -> None:
    """Protect named immutable inputs even if later full CLI parsing fails."""
    if any(arg in ("--help", "-h", "--version") for arg in argv):
        return
    names = ("--out", "--source-run", "--model-run", "--student-run",
             "--distribution-run", "--market-run", "--plan-run")
    values = {}
    for index, arg in enumerate(argv):
        if arg == "--":
            break
        for name in names:
            if arg.startswith(name + "="):
                values[name] = arg[len(name)+1:]
            elif arg == name and index+1 < len(argv) and not argv[index+1].startswith("--"):
                values[name] = argv[index+1]
    ensure_separate_output(Path(values.get("--out") or default_out),
                           [values[name] for name in names[1:] if values.get(name) is not None])


def read_public_samples(root: Path, receipt: dict) -> list[dict]:
    name = "information_weight_scale_samples.jsonl"
    if name not in receipt["registered_artifact_paths"]:
        raise ValueError("public Teacher samples are not registered")
    path = _source_file(Path(root), name)
    expected = next(row for row in receipt["files"] if row["path"] == name)
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                raise ValueError("empty public sample row")
            rows.append(json.loads(line, object_pairs_hook=_unique_object))
    if file_sha256(path) != expected["sha256"]:
        raise ValueError("public samples changed during read")
    return rows


def write_json_exclusive(path: Path, value: Any, *, private: bool = False) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         indent=2, allow_nan=False) + "\n"
    write_text_exclusive(path, payload, private=private)


def write_text_exclusive(path: Path, text: str, *, private: bool = False) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600 if private else 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def archive_verified_run(source: Path, destination: Path, receipt: dict) -> dict:
    """Create or verify an exclusive content-addressed backup and restore drill.

    Destination must be explicit; no external disk/cloud destination is guessed.
    Existing files are never overwritten. The temporary restore is the only
    tree cleaned up, by TemporaryDirectory under its unique owned path.
    """
    source, destination = Path(source), Path(destination)
    assert_unchanged(source, receipt)
    if source.resolve() == destination.resolve() or source.resolve() in destination.resolve().parents:
        raise ValueError("backup cannot be placed inside its source")
    if destination.is_symlink():
        raise ValueError("backup destination cannot be a symlink")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = destination / "run"
    if target.resolve() == source.resolve() or target.resolve() in source.resolve().parents:
        raise ValueError("backup must be independent of its source tree")
    if target.exists():
        assert_unchanged(target, receipt)
    else:
        shutil.copytree(source, target, copy_function=shutil.copy2, symlinks=False)
        assert_unchanged(target, receipt)
    checked = verify_run(target, receipt["manifest_sha256"])
    with tempfile.TemporaryDirectory(prefix="agent-market-restore-") as temporary:
        restored = Path(temporary) / "run"
        shutil.copytree(target, restored, copy_function=shutil.copy2)
        restored_receipt = verify_run(restored, receipt["manifest_sha256"])
        assert_unchanged(restored, receipt)
    now = datetime.now(timezone.utc).isoformat()
    archive = {"schema_version": "information-market-archive/1.0",
               "run_id": receipt["run_id"],
               "manifest_sha256": receipt["manifest_sha256"],
               "source_identity": {"run_id": receipt["run_id"],
                                   "snapshot_hash": receipt["snapshot_hash"]},
               "backup_identity": {"layout": "run/", "snapshot_hash": checked["snapshot_hash"]},
               "file_count": receipt["regular_file_count"],
               "total_size_bytes": receipt["total_size_bytes"],
               "files": receipt["files"], "created_at": now,
               "restore_verified_at": now,
               "restore_registered_artifacts": restored_receipt["registered_artifacts_verified"]}
    receipt_path = destination / "archive_receipt.json"
    if receipt_path.exists():
        old = read_json(receipt_path)
        if (old.get("manifest_sha256") != receipt["manifest_sha256"] or
                old.get("files") != receipt["files"]):
            raise ValueError("existing archive receipt mismatch")
    else:
        write_json_exclusive(receipt_path, archive, private=True)
    checksum_text = "".join(f'{row["sha256"]}  run/{row["path"]}\n' for row in receipt["files"])
    checksums = destination / "checksums.sha256"
    if checksums.exists():
        if checksums.read_text(encoding="utf-8") != checksum_text:
            raise ValueError("existing backup checksums mismatch")
    else:
        write_text_exclusive(checksums, checksum_text, private=True)
    restore_doc = destination / "RESTORE.md"
    if not restore_doc.exists():
        write_text_exclusive(restore_doc,
            "# Restore\n\nCopy run/ with metadata preservation to a NEW empty directory. "
            "Never restore on top of a historical run. Verify checksums.sha256 from "
            "the archive root, the manifest SHA in archive_receipt.json, every "
            "registered artifact, file modes (private files 0600), and mtimes. "
            "This archive was verified through a separate temporary restore.\n", private=True)
    assert_unchanged(source, receipt)
    return {"run_id": receipt["run_id"], "manifest_sha256": receipt["manifest_sha256"],
            "registered_artifacts_verified": checked["registered_artifacts_verified"],
            "restore_verified_at": now, "restore_passed": True,
            "source_unchanged": True, "regular_file_count": receipt["regular_file_count"]}


__all__ = ["canonical_hash", "file_sha256", "read_json", "snapshot_run",
           "verify_run", "assert_unchanged", "read_public_samples",
           "write_json_exclusive", "write_text_exclusive", "archive_verified_run",
           "ensure_separate_output", "preflight_output_separation"]
