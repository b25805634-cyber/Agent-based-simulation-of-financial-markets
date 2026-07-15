"""Two-stage bootstrap helpers shared by official managed CLIs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

from .run_context import ManagedRunContext, safe_output_root, validate_run_id


class ManagedCLIError(ValueError):
    """Full CLI validation failed after a safe output location was known."""


class BootstrapCLIError(ValueError):
    """The output location or run identity could not be safely bootstrapped."""


class RaisingArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose validation errors can be provenance-managed."""

    def error(self, message: str) -> None:
        raise ManagedCLIError(message)


@dataclass(frozen=True)
class BootstrapInfo:
    out_root: Path
    run_id: Optional[str]
    replay_from: Optional[str]
    config_json: Optional[str]
    command_identity: str


def _option_present(argv: Sequence[str], option: str) -> bool:
    return option in argv or any(item.startswith(option + "=") for item in argv)


def bootstrap_cli(
    argv: Sequence[str],
    *,
    default_out: str,
    command_identity: str,
    allow_config_json: bool = False,
) -> Optional[BootstrapInfo]:
    """Parse only the safe fields needed to reserve a managed attempt.

    Help and version are intentionally returned as ``None`` so the full parser
    can perform its normal clean exit without creating a run directory.
    """

    args = list(argv)
    if "-h" in args or "--help" in args or "--version" in args:
        return None
    parser = RaisingArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--out", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--replay-from", default=None)
    if allow_config_json:
        parser.add_argument("--config-json", default=None)
    try:
        known, _unknown = parser.parse_known_args(args)
        run_id = validate_run_id(known.run_id)
        out_root = safe_output_root(known.out, default_out)
    except (ManagedCLIError, OSError, TypeError, ValueError) as error:
        raise BootstrapCLIError(type(error).__name__) from error
    return BootstrapInfo(
        out_root=out_root,
        run_id=run_id,
        replay_from=known.replay_from,
        config_json=getattr(known, "config_json", None),
        command_identity=command_identity,
    )


def safe_cli_error(error: BaseException) -> str:
    """Return a public error with option/field names but never input values."""

    error_type = type(error).__name__
    if error_type in {
        "ConfigAliasConflictError",
        "ConfigSchemaError",
        "UnknownConfigFieldError",
        "UnclassifiedConfigFieldError",
    }:
        return str(error)
    options = sorted(set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*", str(error))))
    if options:
        prefix = (
            "unrecognized arguments"
            if "unrecognized arguments" in str(error)
            else "command-line validation failed for"
        )
        return "{}: {} {}".format(error_type, prefix, ", ".join(options))
    return "{}: configuration validation failed".format(error_type)


def record_cli_failure(
    bootstrap: BootstrapInfo,
    error: BaseException,
    *,
    failure_stage: str = "config_validation",
) -> Optional[Path]:
    """Create one failed managed attempt, or report why provenance was impossible."""

    try:
        context = ManagedRunContext.bootstrap_attempt(
            out_root=bootstrap.out_root,
            run_id=bootstrap.run_id,
            command_identity=bootstrap.command_identity,
        )
    except (OSError, ValueError) as bootstrap_error:
        print(
            "provenance_not_created_reason={}".format(type(bootstrap_error).__name__),
            file=sys.stderr,
        )
        return None
    with context:
        return context.fail(error, failure_stage=failure_stage)


def fail_cli(
    bootstrap: Optional[BootstrapInfo],
    error: BaseException,
    *,
    failure_stage: str = "config_validation",
) -> "None":
    """Persist a safe managed failure when possible, then exit non-zero."""

    if bootstrap is None:
        print(
            "provenance_not_created_reason=bootstrap_output_location_unavailable",
            file=sys.stderr,
        )
    else:
        record_cli_failure(bootstrap, error, failure_stage=failure_stage)
    print(safe_cli_error(error), file=sys.stderr)
    raise SystemExit(2)


__all__ = [
    "BootstrapCLIError",
    "BootstrapInfo",
    "ManagedCLIError",
    "RaisingArgumentParser",
    "bootstrap_cli",
    "fail_cli",
    "record_cli_failure",
    "safe_cli_error",
]
