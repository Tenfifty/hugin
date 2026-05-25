"""Resolve packaged prompt templates with a language-aware fallback.

The convention every hugin-* tool follows for a prompt named ``<base>``:

1. If the user set an explicit override path in config, use that.
2. Else look for ``<package_dir>/<base>_<language>.md``.
3. Else fall back to ``<package_dir>/<base>_default.md``.

The default file ships with the tool and must always exist. Language
variants are optional — tools may ship them or leave the slot open for
user/community contributions.
"""

from __future__ import annotations

from pathlib import Path


def resolve_prompt(
    base: str,
    language: str,
    explicit: Path | None,
    package_dir: Path,
) -> Path:
    """Return the path to a prompt file, honouring the resolution order.

    Raises FileNotFoundError if no default prompt exists for ``base`` —
    that's a tool-side packaging bug, not a user-config issue.
    """
    if explicit is not None:
        return explicit

    language_path = package_dir / f"{base}_{language}.md"
    if language_path.exists():
        return language_path

    default_path = package_dir / f"{base}_default.md"
    if default_path.exists():
        return default_path

    raise FileNotFoundError(
        f"No prompt found for base={base!r} language={language!r} in {package_dir}"
    )
