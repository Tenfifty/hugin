from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from hugin.config import (
    ARCHIVE_DIRNAME_BY_LANGUAGE,
    SharedConfig,
    _deep_merge,
    _expand,
    config_dir,
    load_shared,
    load_tool,
)


class ConfigDirTests(unittest.TestCase):
    def test_default_is_home_config_hugin(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HUGIN_CONFIG_DIR", None)
            self.assertEqual(config_dir(), Path.home() / ".config" / "hugin")

    def test_env_override_wins(self) -> None:
        with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": "/tmp/elsewhere"}):
            self.assertEqual(config_dir(), Path("/tmp/elsewhere"))

    def test_env_override_expands_user(self) -> None:
        with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": "~/custom"}):
            self.assertEqual(config_dir(), Path.home() / "custom")


class DeepMergeTests(unittest.TestCase):
    def test_nested_dicts_merge(self) -> None:
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        override = {"b": 2, "nested": {"y": 99, "z": 3}}
        self.assertEqual(
            _deep_merge(base, override),
            {"a": 1, "b": 2, "nested": {"x": 1, "y": 99, "z": 3}},
        )

    def test_lists_are_replaced_not_merged(self) -> None:
        base = {"items": [1, 2, 3]}
        override = {"items": [9]}
        self.assertEqual(_deep_merge(base, override), {"items": [9]})

    def test_scalar_override_beats_dict_base(self) -> None:
        base = {"x": {"nested": True}}
        override = {"x": "plain"}
        self.assertEqual(_deep_merge(base, override), {"x": "plain"})


class ExpandTests(unittest.TestCase):
    def test_expands_user_and_env(self) -> None:
        with patch.dict(os.environ, {"MY_DIR": "/data"}):
            self.assertEqual(_expand("~/notes"), str(Path.home() / "notes"))
            self.assertEqual(_expand("$MY_DIR/x"), "/data/x")

    def test_recurses_into_dicts_and_lists(self) -> None:
        with patch.dict(os.environ, {"FOO": "bar"}):
            result = _expand({"k": ["~/x", {"deep": "$FOO"}]})
        self.assertEqual(result["k"][0], str(Path.home() / "x"))
        self.assertEqual(result["k"][1]["deep"], "bar")

    def test_leaves_non_strings_alone(self) -> None:
        self.assertEqual(_expand(42), 42)
        self.assertIsNone(_expand(None))


class SharedConfigTests(unittest.TestCase):
    def test_archive_dirname_defaults_by_language(self) -> None:
        kw = SharedConfig.fields_from_merged({"language": "sv"})
        self.assertEqual(kw["archive_dirname"], "arkiv")

        kw = SharedConfig.fields_from_merged({"language": "en"})
        self.assertEqual(kw["archive_dirname"], "archive")

    def test_archive_dirname_explicit_wins_over_language_default(self) -> None:
        kw = SharedConfig.fields_from_merged(
            {"language": "sv", "archive_dirname": "vault"}
        )
        self.assertEqual(kw["archive_dirname"], "vault")

    def test_unknown_language_falls_back_to_archive(self) -> None:
        kw = SharedConfig.fields_from_merged({"language": "fr"})
        self.assertEqual(kw["archive_dirname"], "archive")

    def test_paths_are_expanded_and_typed(self) -> None:
        kw = SharedConfig.fields_from_merged(
            {"vault_path": str(Path.home() / "v"), "journal_path": "/abs/j.md"}
        )
        self.assertEqual(kw["vault_path"], Path.home() / "v")
        self.assertEqual(kw["journal_path"], Path("/abs/j.md"))

    def test_archive_dirname_table_is_sane(self) -> None:
        # If this ever grows, the convention must keep "archive" as the
        # fallback for unknown languages — see CONVENTIONS.md.
        self.assertEqual(ARCHIVE_DIRNAME_BY_LANGUAGE["en"], "archive")
        self.assertEqual(ARCHIVE_DIRNAME_BY_LANGUAGE["sv"], "arkiv")


@dataclass
class _ToyConfig:
    language: str
    foo: str

    @classmethod
    def from_merged(cls, merged: dict) -> "_ToyConfig":
        return cls(
            language=merged.get("language", "en"),
            foo=merged.get("toy", {}).get("foo", "default"),
        )


class LoadToolTests(unittest.TestCase):
    def test_shared_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "hugin.yaml").write_text("language: sv\n")
            with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": tmp}):
                cfg = load_tool("toy", _ToyConfig.from_merged)
        self.assertEqual(cfg.language, "sv")
        self.assertEqual(cfg.foo, "default")

    def test_tool_overrides_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "hugin.yaml").write_text("language: en\n")
            (Path(tmp) / "toy.yaml").write_text("language: sv\ntoy:\n  foo: bar\n")
            with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": tmp}):
                cfg = load_tool("toy", _ToyConfig.from_merged)
        self.assertEqual(cfg.language, "sv")
        self.assertEqual(cfg.foo, "bar")

    def test_missing_files_yield_empty_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": tmp}):
                cfg = load_tool("toy", _ToyConfig.from_merged)
        self.assertEqual(cfg.language, "en")
        self.assertEqual(cfg.foo, "default")

    def test_invalid_yaml_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "hugin.yaml").write_text("- just a list\n")
            with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": tmp}):
                with self.assertRaisesRegex(ValueError, "mapping at top level"):
                    load_shared()


if __name__ == "__main__":
    unittest.main()
