from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hugin import llm as hugin_llm
from hugin.llm import DEFAULT_REMOTE_MODEL, LLMConfig, run_prompt


class Completed:
    returncode = 0
    stdout = "ok from stdout\n"
    stderr = ""


class CompletedAgy:
    returncode = 0
    stdout = """{"event":"init","conversation_id":"test"}
{"event":"result","result":{"status":"SUCCESS","response":"ok from agy\\n"}}
"""
    stderr = ""


class RemoteLLMTests(unittest.TestCase):
    def test_codex_uses_default_model_with_effort_and_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LLMConfig(provider="codex", clean_cwd=Path(tmp))

            def fake_run(cmd, **kwargs):
                out_path = Path(cmd[cmd.index("-o") + 1])
                out_path.write_text('{"ok": true}\n', encoding="utf-8")
                return Completed()

            with patch.object(hugin_llm.subprocess, "run", side_effect=fake_run) as run:
                text = run_prompt(cfg, DEFAULT_REMOTE_MODEL, "hello", effort="low")

        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertNotIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "model_reasoning_effort=low")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(text, '{"ok": true}')

    def test_claude_uses_clean_cwd_print_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LLMConfig(provider="claude", clean_cwd=Path(tmp))
            with patch.object(hugin_llm.subprocess, "run", return_value=Completed()) as run:
                text = run_prompt(cfg, DEFAULT_REMOTE_MODEL, "hello", effort="high")

        cmd = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["cwd"], Path(tmp))
        self.assertNotIn("--bare", cmd)
        self.assertNotIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertIn("--print", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(text, "ok from stdout")

    def test_agy_uses_clean_cwd_plan_and_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean_cwd = Path(tmp)
            cfg = LLMConfig(provider="agy", clean_cwd=clean_cwd)
            with patch.object(hugin_llm.subprocess, "run", return_value=CompletedAgy()) as run:
                text = run_prompt(cfg, DEFAULT_REMOTE_MODEL, "hello", effort="high")

        cmd = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(cmd[0], "agy")
        self.assertNotIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--input-format") + 1], "stream-json")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertEqual(cmd[-1], "--print=")
        stream_input = json.loads(kwargs["input"])
        self.assertEqual(stream_input["event"], "user")
        self.assertEqual(stream_input["message"]["content"], "hello")
        self.assertEqual(kwargs["cwd"], clean_cwd)
        self.assertEqual(text, "ok from agy")

    def test_local_provider_runs_configured_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LLMConfig(
                provider="local",
                clean_cwd=Path(tmp),
                local_command=["fake-llm", "--model", "{model}", "--effort", "{effort}"],
            )
            with patch.object(hugin_llm.subprocess, "run", return_value=Completed()) as run:
                text = run_prompt(cfg, "gemma-local", "hello", effort="low")

        cmd = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(cmd, ["fake-llm", "--model", "gemma-local", "--effort", "low"])
        self.assertEqual(kwargs["input"], "hello")
        self.assertEqual(kwargs["cwd"], Path(tmp))
        self.assertEqual(text, "ok from stdout")

    def test_local_provider_requires_command(self) -> None:
        cfg = LLMConfig(provider="local")
        with self.assertRaisesRegex(RuntimeError, "local_command"):
            run_prompt(cfg, DEFAULT_REMOTE_MODEL, "hello")


if __name__ == "__main__":
    unittest.main()
