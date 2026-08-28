"""Persistent multi-turn sessions on top of coding-agent CLIs.

``llm.run_prompt`` is deliberately one-shot: it passes ``--ephemeral`` to codex
and ``--no-session-persistence`` to claude so nothing is left behind. This
module is the opposite. It keeps a provider-side conversation alive and resumes
it by id, one process per turn.

One process per turn does *not* cost prompt caching. The cache is server-side
and keyed on the content prefix, not on the client process. Measured
2026-08-25 with claude: a resumed turn in a fresh process read 26665 tokens
from cache and wrote 630, with 2 uncached input tokens. codex reported 16768 of
17343 input tokens cached on resume, agy 12193. So there is no reason to keep a
pty or a long-lived child alive, and per-turn processes give crash isolation
plus sessions the user can open interactively with ``claude --resume`` /
``codex resume`` / ``agy --conversation``.

See CONVENTIONS.md for provider naming. Unlike ``run_prompt``, sessions run in
a caller-supplied ``cwd`` and keep their tools: callers here generally *want*
the agent to be able to look things up itself. ``extra_dirs`` widens that reach
beyond cwd, which matters when the working directory is one repo but the
material lives in a vault somewhere else.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .llm import DEFAULT_REMOTE_MODEL, LLMConfig, _uses_default_model

SESSION_PROVIDERS = ("codex", "claude", "agy")


@dataclass
class Usage:
    """Token counts normalised across providers.

    The providers disagree on whether ``input_tokens`` includes the cached
    prefix, so the arithmetic differs per provider and the result is always
    "new input the model had to read" vs "prefix served from cache":

    * claude reports them separately        -> new = input + cache_creation
    * codex reports a total plus the cached -> new = input - cached
    * agy reports a total plus the cached   -> new = input - cache_read
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    # The model's window, where the provider reports it. None is not zero: agy
    # and codex do not say, so a caller must render the absence rather than
    # invent a denominator.
    context_window: int | None = None

    @property
    def read_tokens(self) -> int:
        """Everything the model read during the turn, cached prefix included.

        Not a context size, and it was called ``context_tokens`` until a real
        agentic turn made the difference obvious: one gather turn with 43 tool
        calls reported 2.29M read against a 200k window, because the providers
        sum the usage over every request the turn made. The true context was
        104k, visible only in claude's own transcript and in no field of
        ``--output-format json``. So this is reported as what it is, and nothing
        divides it by :attr:`context_window`.
        """
        return self.input_tokens + self.cached_input_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "context_window": self.context_window,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Usage":
        return cls(
            input_tokens=_int(data, "input_tokens"),
            cached_input_tokens=_int(data, "cached_input_tokens"),
            output_tokens=_int(data, "output_tokens"),
            cost_usd=data.get("cost_usd"),
            context_window=data.get("context_window"),
        )


@dataclass
class Turn:
    """One completed exchange."""

    text: str
    usage: Usage
    session_id: str
    wall_seconds: float | None = None


@dataclass
class Event:
    """One thing a provider did mid-turn, normalised across the three.

    Only what a person watching wants: which tool, on what. The providers
    disagree about everything else, and the parts they disagree about are not
    worth a common vocabulary.
    """

    kind: str  # "tool", "text" or "notice"
    name: str = ""
    detail: str = ""


class SessionError(RuntimeError):
    """A turn failed. Carries the provider's own message where there is one."""


def _loads(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _first(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) else 0


# --------------------------------------------------------------------------
# provider adapters
#
# Each returns (argv, stdin_text). Parsing is a separate function so a failed
# turn can still be reported with the provider's own wording.
# --------------------------------------------------------------------------


# Read, search, and look things up on the web; write nothing, run nothing.
READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "WebSearch", "WebFetch")


def _cmd_claude(s: "Session", prompt: str, stream: bool = False) -> tuple[list[str], str | None]:
    cmd = [
        s.cfg.claude_bin,
        *s.cfg.claude_args,
        "--print",
        "--output-format",
        # stream-json is the same result object, preceded by one line per step.
        # --verbose is not optional: claude refuses stream-json in print mode
        # without it.
        *(["stream-json", "--verbose"] if stream else ["json"]),
    ]
    if s.session_id:
        cmd.extend(["--resume", s.session_id])
    else:
        # We mint the uuid ourselves, so the id is known before the first turn.
        s.session_id = str(uuid.uuid4())
        cmd.extend(["--session-id", s.session_id])
    if s.read_only:
        # Not `--permission-mode plan`. Plan mode is Claude Code's planning
        # workflow, not a sandbox: a council member run under it wrote its whole
        # answer to ~/.claude/plans/ as a side effect and was primed to produce
        # an implementation plan rather than an answer. An explicit tool list is
        # the actual read-only setting. --strict-mcp-config matters as much as
        # the list: without it the user's own MCP servers are inherited, so a
        # "read-only" session can post to Slack. The `=` form is deliberate,
        # since both options are variadic and would otherwise swallow the prompt.
        cmd.append(f"--tools={','.join(READ_ONLY_TOOLS)}")
        cmd.extend(["--strict-mcp-config", '--mcp-config={"mcpServers":{}}'])
    for extra in s.extra_dirs:
        cmd.extend(["--add-dir", str(extra)])
    if not _uses_default_model(s.model):
        cmd.extend(["--model", s.model])
    if s.effort:
        cmd.extend(["--effort", s.effort])
    cmd.append(prompt)
    return cmd, None


def _events_claude(obj: dict[str, Any]) -> list[Event]:
    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "permission_denied":
        return [Event("notice", "denied", _first(obj, "tool_name"))]
    if kind != "assistant":
        return []
    events = []
    for block in (obj.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            data = block.get("input") or {}
            detail = _first(data, "command", "pattern", "file_path", "url", "query", "path")
            events.append(Event("tool", str(block.get("name") or "tool"), detail))
        elif block.get("type") == "text" and str(block.get("text") or "").strip():
            events.append(Event("text", "", str(block["text"]).strip()))
    return events


def _parse_claude(s: "Session", stdout: str) -> Turn:
    data: dict[str, Any] | None = None
    for line in stdout.splitlines():
        obj = _loads(line)
        # Under --output-format json there is one object and no "type"; under
        # stream-json the last result line is the same object, so both land here.
        if obj is not None and (obj.get("type") == "result" or "type" not in obj):
            data = obj
    if data is None:
        raise SessionError(f"claude did not return JSON: {stdout[:200]}")
    if data.get("is_error"):
        raise SessionError(str(data.get("result") or "claude turn failed"))
    raw = data.get("usage") or {}
    window: int | None = None
    for entry in (data.get("modelUsage") or {}).values():
        if isinstance(entry, dict) and entry.get("contextWindow"):
            window = int(entry["contextWindow"])
            break
    usage = Usage(
        input_tokens=_int(raw, "input_tokens") + _int(raw, "cache_creation_input_tokens"),
        cached_input_tokens=_int(raw, "cache_read_input_tokens"),
        output_tokens=_int(raw, "output_tokens"),
        cost_usd=data.get("total_cost_usd"),
        context_window=window,
    )
    session_id = data.get("session_id") or s.session_id
    return Turn(
        text=str(data.get("result") or "").strip(),
        usage=usage,
        session_id=str(session_id),
    )


def _cmd_codex(s: "Session", prompt: str, stream: bool = False) -> tuple[list[str], str | None]:
    # Already NDJSON either way, so streaming needs no flag of its own.
    # Options must precede the `resume` subcommand: `codex exec resume <id> -s
    # read-only` dies with "unexpected argument '-s'".
    # extra_dirs needs no flag here: codex's read-only sandbox already grants
    # disk-wide reads, and a read-write session is not confined to cwd either.
    cmd = [s.cfg.codex_bin, "exec", "--json", "--skip-git-repo-check"]
    if s.read_only:
        cmd.extend(["-s", "read-only"])
    if not _uses_default_model(s.model):
        cmd.extend(["-m", s.model])
    if s.effort:
        cmd.extend(["-c", f"model_reasoning_effort={s.effort}"])
    cmd.extend(s.cfg.codex_args)
    if s.session_id:
        cmd.extend(["resume", s.session_id, prompt])
    else:
        cmd.append(prompt)
    return cmd, None


def _events_codex(obj: dict[str, Any]) -> list[Event]:
    if obj.get("type") != "item.started":
        # item.completed would repeat the same call with its output attached,
        # and the point is to see the work as it happens.
        return []
    item = obj.get("item") or {}
    kind = str(item.get("type") or "")
    if kind == "agent_message":
        return []
    detail = _first(item, "command", "path", "query", "url", "text")
    return [Event("tool", kind or "tool", detail)]


def _parse_codex(s: "Session", stdout: str) -> Turn:
    session_id = s.session_id
    text: list[str] = []
    usage = Usage()
    error: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id") or session_id
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text.append(str(item.get("text") or ""))
        elif kind == "turn.completed":
            raw = event.get("usage") or {}
            cached = _int(raw, "cached_input_tokens")
            usage = Usage(
                input_tokens=max(_int(raw, "input_tokens") - cached, 0),
                cached_input_tokens=cached,
                output_tokens=_int(raw, "output_tokens")
                + _int(raw, "reasoning_output_tokens"),
            )
        elif kind == "turn.failed":
            err = event.get("error") or {}
            error = str(err.get("message") or err or "codex turn failed")
    if error:
        raise SessionError(error)
    if not session_id:
        raise SessionError("codex did not report a thread_id")
    return Turn(text="\n".join(text).strip(), usage=usage, session_id=session_id)


def _cmd_agy(s: "Session", prompt: str, stream: bool = False) -> tuple[list[str], str | None]:
    # Already NDJSON either way, so streaming needs no flag of its own.
    cmd = [
        s.cfg.agy_bin,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    if s.read_only:
        cmd.extend(["--mode", "plan", "--sandbox"])
    if s.session_id:
        cmd.extend(["--conversation", s.session_id])
    for extra in s.extra_dirs:
        cmd.extend(["--add-dir", str(extra)])
    if not _uses_default_model(s.model):
        cmd.extend(["--model", s.model])
    # For gemini models agy bakes the reasoning level into the model slug
    # (gemini-3.7-flash-high). Passing --effort as well is two channels for one
    # setting with undocumented precedence, so the slug wins and effort is only
    # forwarded when the model is left at the provider default.
    if s.effort and _uses_default_model(s.model):
        cmd.extend(["--effort", s.effort])
    cmd.extend([*s.cfg.agy_args, "--print="])
    payload = json.dumps(
        {"event": "user", "message": {"role": "user", "content": prompt}},
        ensure_ascii=False,
    )
    return cmd, payload + "\n"


def _events_agy(obj: dict[str, Any]) -> list[Event]:
    if obj.get("event") != "step_update":
        return []
    step = obj.get("step_update") or {}
    # ACTIVE only: every step is reported twice, once starting and once done.
    if step.get("state") != "ACTIVE" or step.get("step_type") != "tool":
        return []
    detail = _first(step, "command", "tool_input", "path", "query")
    return [Event("tool", _first(step, "tool_name") or "tool", detail)]


def _parse_agy(s: "Session", stdout: str) -> Turn:
    session_id = s.session_id
    terminal: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "init":
            session_id = (event.get("conversation_id") or session_id)
        elif event.get("event") == "result":
            terminal = event.get("result") or {}
            session_id = terminal.get("conversation_id") or session_id
    if not terminal:
        raise SessionError("agy did not emit a terminal result")
    if terminal.get("status") != "SUCCESS":
        raise SessionError(str(terminal.get("error") or "agy turn failed"))
    response = terminal.get("response")
    if not isinstance(response, str):
        raise SessionError("agy result did not contain a text response")
    raw = terminal.get("usage") or {}
    # agy's terminal usage is the sum over the steps in this invocation, not
    # the session total.
    cached = _int(raw, "cache_read_tokens")
    usage = Usage(
        input_tokens=max(_int(raw, "input_tokens") - cached, 0),
        cached_input_tokens=cached,
        output_tokens=_int(raw, "output_tokens") + _int(raw, "thinking_tokens"),
    )
    if not session_id:
        raise SessionError("agy did not report a conversation_id")
    return Turn(
        text=response.strip(),
        usage=usage,
        session_id=session_id,
        wall_seconds=terminal.get("duration_seconds"),
    )


# build, parse, extract-events. All three providers emit NDJSON when asked, so
# watching a turn happen is the same mechanism everywhere.
_ADAPTERS = {
    "claude": (_cmd_claude, _parse_claude, _events_claude),
    "codex": (_cmd_codex, _parse_codex, _events_codex),
    "agy": (_cmd_agy, _parse_agy, _events_agy),
}


@dataclass
class Session:
    """A provider-side conversation that survives across processes.

    ``session_id`` is None until the first :meth:`send`, except for claude
    where we mint the uuid ourselves. Persist it with :meth:`to_dict` and
    reattach later with :meth:`from_dict`; nothing else needs to be kept.
    """

    provider: str
    cwd: Path
    model: str = DEFAULT_REMOTE_MODEL
    effort: str | None = None
    read_only: bool = True
    session_id: str | None = None
    extra_dirs: list[Path] = field(default_factory=list)
    cfg: LLMConfig = field(default_factory=LLMConfig)
    turns: int = 0
    # The most recent turn's counts, kept so a caller can show context use
    # without replaying the conversation. Survives to_dict/from_dict.
    last_usage: Usage | None = None
    total_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.provider not in _ADAPTERS:
            raise ValueError(
                f"session provider must be one of: {', '.join(SESSION_PROVIDERS)}"
            )
        self.cwd = Path(self.cwd).expanduser()
        self.extra_dirs = [Path(d).expanduser() for d in self.extra_dirs]

    def send(
        self,
        prompt: str,
        timeout: int = 900,
        on_event: Callable[[Event], None] | None = None,
    ) -> Turn:
        """Run one turn and return the parsed result.

        Pass ``on_event`` to watch the turn happen: it is called with an
        :class:`Event` per tool call and per piece of text the model emits
        before its answer. A turn that runs 40 tool calls behind a silent
        prompt is indistinguishable from one that has hung.
        """
        build, parse, _ = _ADAPTERS[self.provider]
        cmd, stdin_text = build(self, prompt, on_event is not None)
        self.cwd.mkdir(parents=True, exist_ok=True)
        if on_event is None:
            stdout = self._run(cmd, stdin_text, timeout)
        else:
            stdout = self._stream(cmd, stdin_text, timeout, on_event)
        turn = parse(self, stdout)
        self.session_id = turn.session_id
        self.turns += 1
        self.last_usage = turn.usage
        if turn.usage.cost_usd:
            # claude reports a per-turn cost; the others report none, so this
            # stays a partial total rather than a bill.
            self.total_cost_usd += turn.usage.cost_usd
        return turn

    def _run(self, cmd: list[str], stdin_text: str | None, timeout: int) -> str:
        try:
            result = subprocess.run(
                cmd,
                input=stdin_text,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SessionError(f"{self.provider} turn exceeded {timeout}s") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SessionError(detail or f"{self.provider} turn failed")
        return result.stdout

    def _stream(
        self,
        cmd: list[str],
        stdin_text: str | None,
        timeout: int,
        on_event: Callable[[Event], None],
    ) -> str:
        extract = _ADAPTERS[self.provider][2]
        # stderr to a file rather than a pipe: reading one pipe while the other
        # fills is the classic deadlock, and stderr is only wanted on failure.
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errors:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
                cwd=self.cwd,
                text=True,
                # Own process group, so the whole tree can be killed. Killing
                # the CLI alone leaves its children holding the stdout pipe,
                # and the read loop then waits for them rather than for the
                # timeout that just fired.
                start_new_session=True,
            )

            def kill_tree() -> None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            # A watchdog, not a per-line deadline: a provider that hangs emits
            # no line at all, which is exactly the case worth killing.
            expired = threading.Event()

            def on_timeout() -> None:
                expired.set()
                kill_tree()

            watchdog = threading.Timer(timeout, on_timeout)
            watchdog.start()
            lines: list[str] = []
            # Text is held back one event. The last thing a model says is its
            # answer, which the caller is about to print in full; what is worth
            # watching is the narration *between* tool calls. Deferring means
            # the final text is simply never flushed.
            pending: Event | None = None
            try:
                if stdin_text and proc.stdin is not None:
                    proc.stdin.write(stdin_text)
                    proc.stdin.close()
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.append(line)
                    parsed = _loads(line)
                    if parsed is None:
                        continue
                    for event in extract(parsed):
                        if pending is not None:
                            on_event(pending)
                            pending = None
                        if event.kind == "text":
                            pending = event
                        else:
                            on_event(event)
                code = proc.wait()
            except BaseException:
                # Ctrl-C included: the group is detached from ours, so nothing
                # else will reap it.
                kill_tree()
                raise
            finally:
                watchdog.cancel()
            if expired.is_set():
                raise SessionError(f"{self.provider} turn exceeded {timeout}s")
            if code != 0:
                errors.seek(0)
                detail = errors.read().strip() or "".join(lines).strip()
                raise SessionError(detail or f"{self.provider} turn failed")
        return "".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "cwd": str(self.cwd),
            "model": self.model,
            "effort": self.effort,
            "read_only": self.read_only,
            "session_id": self.session_id,
            "extra_dirs": [str(d) for d in self.extra_dirs],
            "turns": self.turns,
            "last_usage": self.last_usage.to_dict() if self.last_usage else None,
            "total_cost_usd": self.total_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], cfg: LLMConfig | None = None) -> "Session":
        return cls(
            provider=str(data["provider"]),
            cwd=Path(str(data["cwd"])),
            model=str(data.get("model") or DEFAULT_REMOTE_MODEL),
            effort=data.get("effort"),
            read_only=bool(data.get("read_only", True)),
            session_id=data.get("session_id"),
            extra_dirs=[Path(d) for d in (data.get("extra_dirs") or [])],
            cfg=cfg or LLMConfig(),
            turns=int(data.get("turns") or 0),
            last_usage=(
                Usage.from_dict(data["last_usage"]) if data.get("last_usage") else None
            ),
            total_cost_usd=float(data.get("total_cost_usd") or 0.0),
        )

    @property
    def label(self) -> str:
        """Stable id for state dirs and roster keys: ``claude:opus-5:high``."""
        parts = [self.provider, self.model]
        if self.effort:
            parts.append(self.effort)
        return ":".join(parts)


def parse_spec(spec: str, cwd: Path, cfg: LLMConfig | None = None, **kwargs: Any) -> Session:
    """Build a Session from a ``provider[:model[:effort]]`` string.

    This is the roster grammar shared by config files and CLI flags, so
    ``--member claude:fable-5:high`` and a YAML roster entry parse identically.
    """
    parts = [p.strip() for p in spec.split(":")]
    if not parts or not parts[0]:
        raise ValueError("empty session spec")
    provider = parts[0].lower()
    model = parts[1] if len(parts) > 1 and parts[1] else DEFAULT_REMOTE_MODEL
    effort = parts[2] if len(parts) > 2 and parts[2] else None
    if len(parts) > 3:
        raise ValueError(f"too many fields in session spec: {spec!r}")
    return Session(
        provider=provider,
        cwd=Path(cwd),
        model=model,
        effort=effort,
        cfg=cfg or LLMConfig(),
        **kwargs,
    )
