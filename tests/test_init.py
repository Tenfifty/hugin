from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hugin.init import Answers, _plan, _render_hugin_yaml, _vault_subdirs, main


class VaultSubdirsTests(unittest.TestCase):
    def test_english_uses_archive(self) -> None:
        self.assertIn("journal/archive", _vault_subdirs("en"))
        self.assertNotIn("journal/arkiv", _vault_subdirs("en"))

    def test_swedish_uses_arkiv(self) -> None:
        self.assertIn("journal/arkiv", _vault_subdirs("sv"))
        self.assertNotIn("journal/archive", _vault_subdirs("sv"))

    def test_unknown_language_falls_back_to_archive(self) -> None:
        self.assertIn("journal/archive", _vault_subdirs("fr"))


class RenderTests(unittest.TestCase):
    def test_yaml_contains_user_answers(self) -> None:
        a = Answers(
            vault=Path("/v"), language="sv", user_name="Alice", scaffold_vault=True
        )
        rendered = _render_hugin_yaml(a)
        self.assertIn("language: sv", rendered)
        self.assertIn('user_name: "Alice"', rendered)
        self.assertIn("vault_path: /v", rendered)
        self.assertIn("journal_path: /v/journal/journal.md", rendered)


class PlanTests(unittest.TestCase):
    def test_plan_marks_new_files_as_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            vault = Path(tmp) / "v"
            answers = Answers(vault=vault, language="en", user_name="X", scaffold_vault=True)
            actions = _plan(answers, cfg_dir, force=False)
        statuses = {(a.path.name, a.kind, a.status) for a in actions}
        self.assertIn(("cfg", "dir", "create"), statuses)
        self.assertIn(("hugin.yaml", "file", "create"), statuses)
        self.assertIn(("journal.md", "file", "create"), statuses)

    def test_plan_skips_existing_files_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            cfg_dir.mkdir()
            (cfg_dir / "hugin.yaml").write_text("preserved")
            answers = Answers(
                vault=Path(tmp) / "v", language="en", user_name="", scaffold_vault=False
            )
            actions = _plan(answers, cfg_dir, force=False)
        yaml_action = next(a for a in actions if a.path.name == "hugin.yaml")
        self.assertEqual(yaml_action.status, "skip-exists")

    def test_plan_overwrites_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            cfg_dir.mkdir()
            (cfg_dir / "hugin.yaml").write_text("preserved")
            answers = Answers(
                vault=Path(tmp) / "v", language="en", user_name="", scaffold_vault=False
            )
            actions = _plan(answers, cfg_dir, force=True)
        yaml_action = next(a for a in actions if a.path.name == "hugin.yaml")
        self.assertEqual(yaml_action.status, "overwrite")


class MainCLITests(unittest.TestCase):
    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            vault = Path(tmp) / "v"
            with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": str(cfg_dir)}):
                main([
                    "--vault", str(vault),
                    "--language", "en",
                    "--user-name", "X",
                    "--dry-run",
                ])
            self.assertFalse(cfg_dir.exists())
            self.assertFalse(vault.exists())

    def test_first_run_scaffolds_then_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            vault = Path(tmp) / "v"
            args = [
                "--vault", str(vault),
                "--language", "sv",
                "--user-name", "Test",
            ]
            with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": str(cfg_dir)}):
                main(args)
                self.assertTrue((cfg_dir / "hugin.yaml").exists())
                self.assertTrue((vault / "journal" / "arkiv").is_dir())
                self.assertTrue((vault / "meetings" / "summaries").is_dir())

                # Preserve user-edited content on a second run
                yaml_path = cfg_dir / "hugin.yaml"
                yaml_path.write_text("user-edited\n")
                main(args)
                self.assertEqual(yaml_path.read_text(), "user-edited\n")

    def test_no_vault_flag_skips_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "cfg"
            vault = Path(tmp) / "v"
            with patch.dict(os.environ, {"HUGIN_CONFIG_DIR": str(cfg_dir)}):
                main([
                    "--vault", str(vault),
                    "--language", "en",
                    "--user-name", "X",
                    "--no-vault",
                ])
            self.assertTrue((cfg_dir / "hugin.yaml").exists())
            self.assertFalse(vault.exists())


if __name__ == "__main__":
    unittest.main()
