"""Shared config loading for the Hugin stack.

Every hugin-* tool reads two YAML files from ~/.config/hugin/:

    hugin.yaml      -- shared across all tools
    <tool>.yaml     -- tool-specific (e.g. meetings.yaml, agenda.yaml)

The tool file overrides the shared file via deep merge. ``HUGIN_CONFIG_DIR``
overrides the config directory.

Tool-specific config classes should subclass ``SharedConfig`` and use
``load_tool`` to wire up loading.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml

T = TypeVar("T")

ARCHIVE_DIRNAME_BY_LANGUAGE = {"en": "archive", "sv": "arkiv"}


def config_dir() -> Path:
    """Resolve the active config directory, honouring ``HUGIN_CONFIG_DIR``."""
    override = os.environ.get("HUGIN_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "hugin"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_shared() -> dict[str, Any]:
    """Read ``hugin.yaml`` only, expanded. Useful for tools that need just
    the shared bits or for sanity-checking the config dir."""
    return _expand(_load_yaml(config_dir() / "hugin.yaml"))


def load_tool(tool: str, builder: Callable[[dict[str, Any]], T]) -> T:
    """Read ``hugin.yaml`` + ``<tool>.yaml``, deep-merge, expand, hand off.

    ``builder`` receives the fully merged-and-expanded dict and returns the
    tool's typed config object.
    """
    cfg_dir = config_dir()
    shared = _load_yaml(cfg_dir / "hugin.yaml")
    tool_data = _load_yaml(cfg_dir / f"{tool}.yaml")
    merged = _expand(_deep_merge(shared, tool_data))
    return builder(merged)


def _opt_path(value: Any) -> Path | None:
    return Path(value).expanduser() if value else None


@dataclass
class SharedConfig:
    """Fields every hugin-* tool can rely on.

    Subclass this in your tool's config module and add your own fields.
    Use ``SharedConfig.fields_from_merged(merged)`` to pull shared values
    out of the merged dict your builder receives.
    """

    language: str = "en"
    user_name: str = ""
    vault_path: Path | None = None
    journal_path: Path | None = None
    gws_bin: str = "gws"
    gws_config_dir: Path | None = None
    # Subdirectory name used for rotated archives next to the journal.
    # Defaults to "archive" for en / "arkiv" for sv when not set explicitly.
    archive_dirname: str = "archive"

    # The full merged config dict, for anything not explicitly modeled.
    raw: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def fields_from_merged(merged: dict[str, Any]) -> dict[str, Any]:
        """Extract shared-field kwargs from a merged config dict.

        Use as ``cls(**SharedConfig.fields_from_merged(merged), my_field=...)``
        inside your tool's builder.
        """
        language = merged.get("language", "en")
        return {
            "language": language,
            "user_name": merged.get("user_name", ""),
            "vault_path": _opt_path(merged.get("vault_path")),
            "journal_path": _opt_path(merged.get("journal_path")),
            "gws_bin": merged.get("gws_bin", "gws"),
            "gws_config_dir": _opt_path(merged.get("gws_config_dir")),
            "archive_dirname": merged.get(
                "archive_dirname",
                ARCHIVE_DIRNAME_BY_LANGUAGE.get(language, "archive"),
            ),
            "raw": merged,
        }
