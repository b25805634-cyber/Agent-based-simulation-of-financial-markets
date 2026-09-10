"""Small, explicitly diagnostic tests; only owned temporary files are used."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from nmsim.information_artifacts import (
    archive_verified_run, assert_unchanged, verify_run, write_json_exclusive, ensure_separate_output,
)
from nmsim.run_context import NullRunContext


class ArtifactInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="information-artifact-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        path = self.root / "private_events.jsonl"
        path.write_bytes(b"{}\n")
        path.chmod(0o600)
        self.manifest = {"run_id": "test-run", "status": "finished", "outputs_complete": True,
                         "managed_run_completed": True,
                         "results": [{"path": path.name, "kind": "file", "exists": True,
                                      "error": None, "inside_run_directory": True,
                                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                      "size_bytes": path.stat().st_size}]}
        self._write_manifest()

    def _write_manifest(self):
        (self.root / "run_manifest.json").write_text(json.dumps(self.manifest))

    def test_byte_mode_mtime_and_restore_drill(self):
        with NullRunContext():
            receipt = verify_run(self.root)
            destination = Path(self.temp.name) / "backup"
            first = archive_verified_run(self.root, destination, receipt)
            self.assertTrue(first["restore_passed"])
            second = archive_verified_run(self.root, destination, receipt)
            self.assertTrue(second["source_unchanged"])
            assert_unchanged(self.root, receipt)
            path = self.root / "private_events.jsonl"
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1000))
            with self.assertRaises(ValueError):
                assert_unchanged(self.root, receipt)

    def test_hash_pin_and_corruption_rejected(self):
        with self.assertRaises(ValueError):
            verify_run(self.root, "0" * 64)
        (self.root / "private_events.jsonl").write_bytes(b"bad")
        with self.assertRaises(ValueError):
            verify_run(self.root)

    def test_archive_inside_source_is_rejected_before_copy(self):
        receipt = verify_run(self.root)
        target = self.root/"nested-backup"
        with self.assertRaises(ValueError):
            archive_verified_run(self.root, target, receipt)
        self.assertFalse(target.exists())

    def test_analysis_output_root_and_symlink_cannot_enter_input(self):
        with self.assertRaises(ValueError):
            ensure_separate_output(self.root/"subdir", [self.root])
        outer = Path(self.temp.name)/"outer"
        outer.mkdir()
        (outer/"runs").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            ensure_separate_output(outer, [self.root])
        ensure_separate_output(Path(self.temp.name), [self.root])

    def test_symlink_and_path_escape_rejected(self):
        (self.root / "escape").symlink_to(Path(self.temp.name))
        with self.assertRaises(ValueError):
            verify_run(self.root)

    def test_failed_run_and_private_mode_rejected(self):
        self.manifest["status"] = "failed"
        self._write_manifest()
        with self.assertRaises(ValueError):
            verify_run(self.root)
        self.manifest["status"] = "finished"
        self._write_manifest()
        (self.root / "private_events.jsonl").chmod(0o644)
        with self.assertRaises(ValueError):
            verify_run(self.root)

    def test_exclusive_output_and_no_partial_nonfinite_json(self):
        target = Path(self.temp.name) / "result.json"
        with self.assertRaises(ValueError):
            write_json_exclusive(target, {"x": float("nan")})
        self.assertFalse(target.exists())
        write_json_exclusive(target, {"ok": True})
        with self.assertRaises(FileExistsError):
            write_json_exclusive(target, {})
