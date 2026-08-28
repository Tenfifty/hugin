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
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


class SessionError(RuntimeError):
    """A turn failed. Carries the provider's own message where there is one."""


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) else 0


# --------------------------------------------------------------------------
# provider adapters
#
# Each returns (argv, stdin_text). Parsing is a separate function so a failed
# turn can still be reported with the provider's own wording.
# --------------------------------------------------------------------------


def _cmd_claude(s: "Session", prompt: str) -> tuple[list[str], str | None]:
    cmd = [
        s.cfg.claude_bin,
        *s.cfg.claude_args,
        "--print",
        "--output-format",
        "json",
    ]
    if s.session_id:
        cmd.extend(["--resume", s.session_id])
    else:
        # We mint the uuid ourselves, so the id is known before the first turn.
        s.session_id = str(uuid.uuid4())
        cmd.extend(["--session-id", s.session_id])
    if s.read_only:
        cmd.extend(["--permission-mode", "plan"])
    for extra in s.extra_dirs:
        cmd.extend(["--add-dir", str(extra)])
    if not _uses_default_model(s.model):
        cmd.extend(["--model", s.model])
    if s.effort:
        cmd.extend(["--effort", s.effort])
    cmd.append(prompt)
    return cmd, None


def _parse_claude(s: "Session", stdout: str) -> Turn:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SessionError(f"claude did not return JSON: {stdout[:200]}") from exc
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


def _cmd_codex(s: "Session", prompt: str) -> tuple[list[str], str | None]:
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


def _cmd_agy(s: "Session", prompt: str) -> tuple[list[str], str | None]:
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


_ADAPTERS = {
    "claude": (_cmd_claude, _parse_claude),
    "codex": (_cmd_codex, _parse_codex),
    "agy": (_cmd_agy, _parse_agy),
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

    def send(self, prompt: str, timeout: int = 900) -> Turn:
        """Run one turn and return the parsed result."""
        build, parse = _ADAPTERS[self.provider]
        cmd, stdin_text = build(self, prompt)
        self.cwd.mkdir(parents=True, exist_ok=True)
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
            raise SessionError(
                f"{self.provider} turn exceeded {timeout}s"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise SessionError(detail or f"{self.provider} turn failed")
        turn = parse(self, result.stdout)
        self.session_id = turn.session_id
        self.turns += 1
        self.last_usage = turn.usage
        if turn.usage.cost_usd:
            # claude reports a per-turn cost; the others report none, so this
            # stays a partial total rather than a bill.
            self.total_cost_usd += turn.usage.cost_usd
        return turn

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
