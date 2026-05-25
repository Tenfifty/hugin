from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hugin.prompts import resolve_prompt


class ResolvePromptTests(unittest.TestCase):
    def test_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / "summary_default.md").write_text("default")
            (pkg / "summary_sv.md").write_text("swedish")
            explicit = pkg / "user.md"
            explicit.write_text("user")

            self.assertEqual(
                resolve_prompt("summary", "sv", explicit, pkg),
                explicit,
            )

    def test_language_variant_picked_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / "summary_default.md").write_text("default")
            (pkg / "summary_sv.md").write_text("swedish")

            self.assertEqual(
                resolve_prompt("summary", "sv", None, pkg),
                pkg / "summary_sv.md",
            )

    def test_falls_back_to_default_when_language_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / "summary_default.md").write_text("default")

            self.assertEqual(
                resolve_prompt("summary", "sv", None, pkg),
                pkg / "summary_default.md",
            )

    def test_english_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / "summary_default.md").write_text("default")
            # An en variant could exist but the convention says English
            # is the default — if both exist, the variant still wins.
            (pkg / "summary_en.md").write_text("explicit english")

            self.assertEqual(
                resolve_prompt("summary", "en", None, pkg),
                pkg / "summary_en.md",
            )

    def test_raises_when_no_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                resolve_prompt("summary", "en", None, pkg)

    def test_example_files_not_picked(self) -> None:
        # `.example.md` files exist as starter templates — must never be
        # auto-selected by the resolver.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / "summary_default.md").write_text("default")
            (pkg / "summary_sv_personal.example.md").write_text("example")

            self.assertEqual(
                resolve_prompt("summary", "sv", None, pkg),
                pkg / "summary_default.md",
            )


if __name__ == "__main__":
    unittest.main()
