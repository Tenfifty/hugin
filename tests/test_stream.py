from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hugin.session import (
    Event,
    Session,
    SessionError,
    _cmd_claude,
    _events_agy,
    _events_claude,
    _events_codex,
    _parse_claude,
    parse_spec,
)

CLAUDE_RESULT = {
    "type": "result",
    "is_error": False,
    "result": "the answer",
    "session_id": "11111111-2222-3333-4444-555555555555",
    "total_cost_usd": 0.5,
    "usage": {"input_tokens": 2, "cache_read_input_tokens": 100, "output_tokens": 7},
    "modelUsage": {"claude-opus-5": {"contextWindow": 200000}},
}
CLAUDE_STREAM = [
    {"type": "system", "subtype": "init", "cwd": "/tmp"},
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "I'll gather the material now."}]},
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls /etc", "description": "x"}}
            ]
        },
    },
    {"type": "system", "subtype": "permission_denied", "tool_name": "Write"},
    CLAUDE_RESULT,
]


class ExtractTests(unittest.TestCase):
    def test_claude_yields_text_tools_and_denials(self) -> None:
        events = [e for obj in CLAUDE_STREAM for e in _events_claude(obj)]
        self.assertEqual(
            events,
            [
                Event("text", "", "I'll gather the material now."),
                Event("tool", "Bash", "ls /etc"),
                Event("notice", "denied", "Write"),
            ],
        )

    def test_codex_reports_a_call_when_it_starts_not_when_it_finishes(self) -> None:
        started = {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}}
        self.assertEqual(_events_codex(started), [Event("tool", "command_execution", "ls")])
        finished = dict(started, type="item.completed")
        self.assertEqual(_events_codex(finished), [])
        # The narration between calls is not a tool call.
        chatter = {"type": "item.started", "item": {"type": "agent_message", "text": "hi"}}
        self.assertEqual(_events_codex(chatter), [])

    def test_agy_reports_the_active_half_of_each_step(self) -> None:
        step = {
            "event": "step_update",
            "step_update": {"state": "ACTIVE", "step_type": "tool", "tool_name": "run_command"},
        }
        self.assertEqual(_events_agy(step), [Event("tool", "run_command", "")])
        done = {"event": "step_update", "step_update": dict(step["step_update"], state="DONE")}
        self.assertEqual(_events_agy(done), [])
        other = {
            "event": "step_update",
            "step_update": {"state": "ACTIVE", "step_type": "agent_response"},
        }
        self.assertEqual(_events_agy(other), [])


class StreamCommandTests(unittest.TestCase):
    def test_claude_asks_for_stream_json_only_when_watched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude:claude-opus-5:high", Path(tmp))
            plain, _ = _cmd_claude(s, "hi", False)
            watched, _ = _cmd_claude(s, "hi", True)
        self.assertIn("json", plain)
        self.assertNotIn("--verbose", plain)
        self.assertIn("stream-json", watched)
        # Not optional: claude refuses stream-json in print mode without it.
        self.assertIn("--verbose", watched)

    def test_the_result_line_is_found_among_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = parse_spec("claude", Path(tmp))
            stdout = "\n".join(json.dumps(o) for o in CLAUDE_STREAM)
            turn = _parse_claude(s, stdout)
        self.assertEqual(turn.text, "the answer")
        self.assertEqual(turn.usage.cost_usd, 0.5)


class StreamRunTests(unittest.TestCase):
    """Drives the real Popen path with a shell script standing in for a CLI."""

    def _session(self, script: str) -> Session:
        s = parse_spec("claude", Path(tempfile.mkdtemp()))
        s.cfg.claude_bin = "sh"
        s.cfg.claude_args = ["-c", script, "--"]
        return s

    def test_events_arrive_and_the_turn_still_parses(self) -> None:
        body = "\n".join(json.dumps(o) for o in CLAUDE_STREAM).replace("'", "")
        s = self._session(f"cat <<'EOF'\n{body}\nEOF")
        seen: list[Event] = []
        turn = s.send("hi", timeout=30, on_event=seen.append)
        self.assertEqual(turn.text, "the answer")
        self.assertEqual([e.kind for e in seen], ["text", "tool", "notice"])
        self.assertEqual(s.turns, 1)

    def test_the_answer_is_not_echoed_as_an_event(self) -> None:
        # The final text block is the answer the caller prints in full; only
        # narration between tool calls is worth watching.
        stream = list(CLAUDE_STREAM)
        stream.insert(-1, {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "the answer"}]},
        })
        body = "\n".join(json.dumps(o) for o in stream).replace("'", "")
        s = self._session(f"cat <<'EOF'\n{body}\nEOF")
        seen: list[Event] = []
        s.send("hi", timeout=30, on_event=seen.append)
        self.assertEqual([e.kind for e in seen], ["text", "tool", "notice"])
        self.assertNotIn("the answer", [e.detail for e in seen])

    def test_a_failing_stream_surfaces_stderr(self) -> None:
        s = self._session("echo 'boom' >&2; exit 3")
        with self.assertRaisesRegex(SessionError, "boom"):
            s.send("hi", timeout=30, on_event=lambda e: None)
        self.assertEqual(s.turns, 0)

    def test_a_hanging_stream_is_killed_and_reported(self) -> None:
        s = self._session("sleep 30")
        with self.assertRaisesRegex(SessionError, "exceeded 1s"):
            s.send("hi", timeout=1, on_event=lambda e: None)


if __name__ == "__main__":
    unittest.main()
