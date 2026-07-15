"""Fail-closed mapping ingestion for :class:`nmsim.config.Config`.

The Config dataclass is part of the raw-byte scientific source fingerprint.
Input validation is an instrumentation boundary rather than a market rule, so
it lives outside that allowlist and is explicitly installed by ``nmsim`` at
package import.  Config defaults and simulation behaviour remain untouched.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
import difflib
import hashlib
from typing import Any, Type, TypeVar


class ConfigSchemaError(ValueError):
    """A mapping cannot be ingested as an unambiguous Config."""


class UnknownConfigFieldError(ConfigSchemaError):
    """Config input contains keys outside the declared schema and alias map."""


class ConfigAliasConflictError(ConfigSchemaError):
    """Multiple input keys resolve to the same canonical Config field."""


# No historical Config-mapping alias is evidenced in the repository.  Keep the
# mechanism centralized and empty until an auditable legacy artifact justifies
# a name.  argparse flag names are intentionally not treated as mapping aliases.
CONFIG_FIELD_ALIASES: dict[str, str] = {}

_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "api_key",
    "authorization",
    "bearer",
    "password",
    "private",
    "rationale",
    "secret",
    "token",
)

_ConfigT = TypeVar("_ConfigT")


def _safe_config_key(name: str) -> str:
    lowered = name.lower()
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
        return "<redacted-config-key sha256:{}>".format(digest)
    printable = "".join(
        character if character.isprintable() and character not in "\r\n" else "?"
        for character in name
    )
    if len(printable) > 80:
        return "{}...<sha256:{}>".format(printable[:64], digest)
    return printable


def _unknown_config_error(
    unknown: list[str], known_fields: set[str]
) -> UnknownConfigFieldError:
    names = sorted(unknown)
    label = "field" if len(names) == 1 else "fields"
    message = "Unknown Config {}: {}".format(
        label, ", ".join(_safe_config_key(name) for name in names)
    )
    suggestions: list[tuple[str, str]] = []
    candidates = sorted(known_fields | set(CONFIG_FIELD_ALIASES))
    for name in names:
        close = difflib.get_close_matches(name, candidates, n=1, cutoff=0.72)
        if close and _safe_config_key(name) == name:
            suggestion = CONFIG_FIELD_ALIASES.get(close[0], close[0])
            suggestions.append((name, suggestion))
    if suggestions:
        rendered = (
            suggestions[0][1]
            if len(names) == 1 and len(suggestions) == 1
            else ", ".join(
                "{} -> {}".format(name, suggestion)
                for name, suggestion in suggestions
            )
        )
        message += ". Did you mean: {}?".format(rendered)
    return UnknownConfigFieldError(message)


def strict_config_from_dict(
    config_class: Type[_ConfigT],
    data: Mapping[str, Any],
    *,
    strict: bool = True,
) -> _ConfigT:
    """Construct ``config_class`` without ignoring unknown or duplicate input."""

    if strict is not True:
        raise ConfigSchemaError(
            "strict=False Config ingestion is not supported; no fields were ignored"
        )
    if not isinstance(data, Mapping):
        raise ConfigSchemaError("Config.from_dict requires a mapping")

    non_string = sorted(
        "<non-string-key:{}>".format(type(key).__name__)
        for key in data
        if not isinstance(key, str)
    )
    if non_string:
        raise UnknownConfigFieldError(
            "Unknown Config fields: {}".format(", ".join(non_string))
        )

    known = {item.name for item in fields(config_class)}
    sources: dict[str, list[str]] = {}
    for source in sorted(data):
        canonical = CONFIG_FIELD_ALIASES.get(source, source)
        sources.setdefault(canonical, []).append(source)

    conflicts = {
        canonical: source_names
        for canonical, source_names in sources.items()
        if len(source_names) > 1
    }
    if conflicts:
        details = [
            "{} <- {}".format(
                _safe_config_key(canonical),
                ", ".join(
                    _safe_config_key(source) for source in conflicts[canonical]
                ),
            )
            for canonical in sorted(conflicts)
        ]
        raise ConfigAliasConflictError(
            "Duplicate Config field source: {}".format("; ".join(details))
        )

    unknown = sorted(canonical for canonical in sources if canonical not in known)
    if unknown:
        raise _unknown_config_error(unknown, known)

    canonical_values = {
        canonical: data[source_names[0]]
        for canonical, source_names in sources.items()
    }
    return config_class(**canonical_values)


def install_config_ingestion_contract(config_class: Type[_ConfigT]) -> Type[_ConfigT]:
    """Bind strict mapping ingestion to the existing Config class explicitly."""

    def from_dict(
        cls: Type[_ConfigT], data: Mapping[str, Any], *, strict: bool = True
    ) -> _ConfigT:
        return strict_config_from_dict(cls, data, strict=strict)

    from_dict.__name__ = "from_dict"
    from_dict.__qualname__ = "Config.from_dict"
    from_dict.__doc__ = (
        "Build Config from a mapping; unknown and duplicate sources fail closed."
    )
    setattr(config_class, "from_dict", classmethod(from_dict))
    return config_class


__all__ = [
    "CONFIG_FIELD_ALIASES",
    "ConfigAliasConflictError",
    "ConfigSchemaError",
    "UnknownConfigFieldError",
    "install_config_ingestion_contract",
    "strict_config_from_dict",
]
