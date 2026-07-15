"""Shared safety helpers for experiment driver subprocesses."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone


def set_driver_provenance(workers: int, driver: str) -> None:
    """Expose driver-level concurrency to each run_seed subprocess."""
    os.environ["NMSIM_DRIVER_WORKERS"] = str(workers)
    os.environ["NMSIM_DRIVER"] = driver


def archive_rejected_result(path: str, reason: str) -> str | None:
    """Move an unhealthy legacy result out of analyzer globs without deleting it.

    The destination and its reason sidecar are uniquely created.  The temporary
    destination reservation makes the operation non-overwriting even in the
    unlikely event of a generated-name collision.
    """
    if not os.path.exists(path):
        return None

    rejected_dir = os.path.join(os.path.dirname(path), "rejected")
    os.makedirs(rejected_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    archived_at = datetime.now(timezone.utc).isoformat()

    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        token = uuid.uuid4().hex[:12]
        archived = os.path.join(rejected_dir, f"{stem}__{stamp}-{token}{ext}")
        try:
            fd = os.open(archived, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(fd)
            break

    try:
        os.replace(path, archived)
    except Exception:
        os.unlink(archived)
        raise

    sidecar = os.path.splitext(archived)[0] + ".reason.json"
    with open(sidecar, "x") as fh:
        json.dump({
            "schema_version": "1.0",
            "archived_at": archived_at,
            "original_path": os.path.abspath(path),
            "archived_path": os.path.abspath(archived),
            "reason": reason,
        }, fh, indent=2)
    return archived
