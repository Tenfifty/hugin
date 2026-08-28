from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hugin.session import Session, SessionError, parse_spec


class Completed:
    returncode = 0
    stderr = ""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


CLAUDE_OUT = json.dumps(
    {
        "is_error": False,
        "result": "hello",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "total_cost_usd": 0.25,
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 630,
            "cache_read_input_tokens": 26665,
            "output_tokens": 7,
        },
    }
)

CODEX_OUT = """{"type":"thread.started","thread_id":"01a038e2-2058-7551-9574-60556c7f17aa"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"hello"}}
{"type":"turn.completed","usage":{"input_tokens":17343,"cached_input_tokens":16768,"output_tokens":8,"reasoning_output_tokens":48}}
"""

AGY_OUT = """{"event":"init","conversation_id":"30dc4628-9c6b-4e83-bad2-ed2233217566"}
{"event":"result","result":{"conversation_id":"30dc4628-9c6b-4e83-bad2-ed2233217566","status":"SUCCESS","response":"hello\\n","duration_seconds":23.8,"usage":{"input_tokens":31301,"output_tokens":411,"thinking_tokens":239,"cache_read_tokens":12193}}}
"""


class SpecTests(unittest.TestCase):
    def test_full_triple(self) -> None:
        s = parse_spec("claude:fable-5:high", Path("/tmp"))
        self.assertEqual((s.provider, s.model, s.effort), ("claude", "fable-5", "high"))
        self.assertEqual(s.label, "claude:fable-5:high")

    def test_provider_only_gets_default_model(self) -> None:
        s = parse_spec("agy", Path("/tmp"))
        self.assertEqual((s.provider, s.model, s.effort), ("agy", "default", None))

    def test_bad_specs_raise(self) -> None:
        for bad in ("", "claude:a:b:c", "gemini"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_spec(bad, Path("/tmp"))


class TurnTests(unittest.TestCase):
    def _send(self, spec: str, stdout: str, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec(spec, Path(tmp), **kwargs)
            with patch("hugin.session.subprocess.run", return_value=Completed(stdout)) as run:
                turn = s.send("hi")
            return s, turn, run.call_args

    def test_claude_mints_its_own_id_then_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude:fable-5:high", Path(tmp))
            with patch("hugin.session.subprocess.run", return_value=Completed(CLAUDE_OUT)) as run:
                s.send("hi")
                first = run.call_args[0][0]
                s.send("again")
                second = run.call_args[0][0]
        self.assertIn("--session-id", first)
        self.assertNotIn("--resume", first)
        self.assertIn("--resume", second)
        self.assertIn("11111111-2222-3333-4444-555555555555", second)
        self.assertNotIn("--permission-mode", first)

    def test_claude_usage_splits_new_from_cached(self) -> None:
        _, turn, _ = self._send("claude", CLAUDE_OUT)
        self.assertEqual(turn.text, "hello")
        self.assertEqual(turn.usage.input_tokens, 632)
        self.assertEqual(turn.usage.cached_input_tokens, 26665)
        self.assertEqual(turn.usage.cost_usd, 0.25)

    def test_codex_options_precede_resume_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("codex:gpt-5.5:high", Path(tmp))
            with patch("hugin.session.subprocess.run", return_value=Completed(CODEX_OUT)) as run:
                s.send("hi")
                s.send("again")
                cmd = run.call_args[0][0]
        self.assertEqual(cmd.index("resume"), len(cmd) - 3)
        self.assertLess(cmd.index("-s"), cmd.index("resume"))
        self.assertLess(cmd.index("-m"), cmd.index("resume"))

    def test_codex_usage_subtracts_cached_from_total(self) -> None:
        _, turn, _ = self._send("codex", CODEX_OUT)
        self.assertEqual(turn.text, "hello")
        self.assertEqual(turn.usage.input_tokens, 17343 - 16768)
        self.assertEqual(turn.usage.cached_input_tokens, 16768)
        self.assertEqual(turn.usage.output_tokens, 8 + 48)

    def test_agy_reads_conversation_id_and_sends_stream_json(self) -> None:
        s, turn, call = self._send("agy:gemini-3.1-pro-high", AGY_OUT)
        self.assertEqual(s.session_id, "30dc4628-9c6b-4e83-bad2-ed2233217566")
        self.assertEqual(turn.text, "hello")
        self.assertEqual(turn.usage.cached_input_tokens, 12193)
        payload = json.loads(call.kwargs["input"].strip())
        self.assertEqual(payload["message"]["content"], "hi")

    def test_agy_effort_yields_to_an_explicit_model_slug(self) -> None:
        # The slug already carries the reasoning level, so --effort is dropped.
        _, _, call = self._send("agy:gemini-3.7-flash-high:high", AGY_OUT)
        self.assertNotIn("--effort", call[0][0])
        _, _, call = self._send("agy::high", AGY_OUT)
        self.assertIn("--effort", call[0][0])

    def test_claude_read_only_is_a_tool_list_and_no_inherited_mcp(self) -> None:
        # Plan mode is a workflow, not a sandbox: it writes to ~/.claude/plans
        # and shapes the answer. And without --strict-mcp-config the user's own
        # MCP servers come along, so a "read-only" member could post to Slack.
        _, _, call = self._send("claude", CLAUDE_OUT)
        cmd = call[0][0]
        self.assertIn("--tools=Read,Grep,Glob,WebSearch,WebFetch", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn('--mcp-config={"mcpServers":{}}', cmd)
        # Both options are variadic, so the `=` form is what keeps them from
        # swallowing the prompt, which is the last argument.
        self.assertEqual(cmd[-1], "hi")

    def test_claude_read_write_session_keeps_every_tool(self) -> None:
        _, _, call = self._send("claude", CLAUDE_OUT, read_only=False)
        cmd = call[0][0]
        self.assertFalse([a for a in cmd if a.startswith("--tools")])
        self.assertNotIn("--strict-mcp-config", cmd)

    def test_read_write_session_drops_the_plan_flags(self) -> None:
        _, _, call = self._send("codex", CODEX_OUT, read_only=False)
        self.assertNotIn("read-only", call[0][0])
        _, _, call = self._send("agy", AGY_OUT, read_only=False)
        self.assertNotIn("--mode", call[0][0])


class FailureTests(unittest.TestCase):
    def test_nonzero_exit_surfaces_provider_stderr(self) -> None:
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "unexpected argument '-s' found"

        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("codex", Path(tmp))
            with patch("hugin.session.subprocess.run", return_value=Failed()):
                with self.assertRaises(SessionError) as ctx:
                    s.send("hi")
        self.assertIn("unexpected argument", str(ctx.exception))

    def test_agy_without_terminal_result_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("agy", Path(tmp))
            with patch(
                "hugin.session.subprocess.run",
                return_value=Completed('{"event":"init","conversation_id":"x"}\n'),
            ):
                with self.assertRaises(SessionError):
                    s.send("hi")

    def test_failed_turn_does_not_count(self) -> None:
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "boom"

        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude", Path(tmp))
            with patch("hugin.session.subprocess.run", return_value=Failed()):
                with self.assertRaises(SessionError):
                    s.send("hi")
            self.assertEqual(s.turns, 0)


class StateTests(unittest.TestCase):
    def test_round_trip_through_disk_keeps_the_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("codex:gpt-5.5:high", Path(tmp))
            with patch("hugin.session.subprocess.run", return_value=Completed(CODEX_OUT)):
                s.send("hi")
            revived = Session.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(revived.session_id, s.session_id)
        self.assertEqual(revived.label, s.label)
        self.assertEqual(revived.turns, 1)
        self.assertTrue(revived.read_only)


if __name__ == "__main__":
    unittest.main()


class ExtraDirTests(unittest.TestCase):
    def test_claude_and_agy_get_add_dir_per_extra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for spec, stdout in (("claude", CLAUDE_OUT), ("agy", AGY_OUT)):
                with self.subTest(spec=spec):
                    s = parse_spec(
                        spec, Path(tmp), extra_dirs=[Path("/a"), Path("/b")]
                    )
                    with patch(
                        "hugin.session.subprocess.run", return_value=Completed(stdout)
                    ) as run:
                        s.send("hi")
                    cmd = run.call_args[0][0]
                    self.assertEqual(cmd.count("--add-dir"), 2)
                    self.assertIn("/a", cmd)

    def test_codex_needs_no_flag_since_read_only_reads_the_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("codex", Path(tmp), extra_dirs=[Path("/a")])
            with patch(
                "hugin.session.subprocess.run", return_value=Completed(CODEX_OUT)
            ) as run:
                s.send("hi")
            self.assertNotIn("--add-dir", run.call_args[0][0])

    def test_extra_dirs_survive_the_disk_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude", Path(tmp), extra_dirs=[Path("/a")])
            revived = Session.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(revived.extra_dirs, [Path("/a")])


class UsageTrackingTests(unittest.TestCase):
    def test_claude_context_window_comes_from_model_usage(self) -> None:
        payload = json.loads(CLAUDE_OUT)
        payload["modelUsage"] = {"claude-fable-5": {"contextWindow": 1_000_000}}
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude", Path(tmp))
            with patch(
                "hugin.session.subprocess.run",
                return_value=Completed(json.dumps(payload)),
            ):
                turn = s.send("hi")
        self.assertEqual(turn.usage.context_window, 1_000_000)
        self.assertEqual(turn.usage.read_tokens, 632 + 26665)

    def test_a_provider_that_does_not_say_leaves_the_window_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("codex", Path(tmp))
            with patch("hugin.session.subprocess.run", return_value=Completed(CODEX_OUT)):
                turn = s.send("hi")
        self.assertIsNone(turn.usage.context_window)

    def test_last_usage_and_cost_accumulate_and_survive_a_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude", Path(tmp))
            with patch(
                "hugin.session.subprocess.run", return_value=Completed(CLAUDE_OUT)
            ):
                s.send("one")
                s.send("two")
            revived = Session.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertAlmostEqual(s.total_cost_usd, 0.5)
        self.assertAlmostEqual(revived.total_cost_usd, 0.5)
        self.assertEqual(revived.last_usage.cached_input_tokens, 26665)

    def test_a_failed_turn_leaves_usage_untouched(self) -> None:
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "boom"

        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude", Path(tmp))
            with patch("hugin.session.subprocess.run", return_value=Failed()):
                with self.assertRaises(SessionError):
                    s.send("hi")
        self.assertIsNone(s.last_usage)
        self.assertEqual(s.total_cost_usd, 0.0)
