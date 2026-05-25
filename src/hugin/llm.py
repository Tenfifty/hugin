"""Run prompts through coding-agent CLIs (codex / claude / gemini) or a
user-supplied local command.

Lifted from hugin-meetings. See CONVENTIONS.md for provider semantics.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LLM_PROVIDERS = {"codex", "claude", "gemini", "local"}
DEFAULT_REMOTE_MODEL = "default"


def _string_list(data: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = data.get(key, default)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return value


@dataclass
class LLMConfig:
    """Settings for the LLM provider.

    Build from a config dict via ``LLMConfig.from_dict``. Pass to
    ``run_prompt`` along with the prompt text.
    """

    provider: str = "codex"
    clean_cwd: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "hugin-llm-clean"
    )
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    gemini_bin: str = "gemini"
    codex_args: list[str] = field(default_factory=list)
    # Claude runs from clean_cwd by default so repo-local CLAUDE.md is not discovered.
    claude_args: list[str] = field(default_factory=list)
    gemini_args: list[str] = field(default_factory=list)
    # Local provider: receives prompt on stdin, returns text on stdout.
    # Arguments may contain {model} and {effort} placeholders.
    local_command: list[str] = field(default_factory=list)
    # Gemini has no exact --bare equivalent. Use a clean cwd plus a workspace
    # setting that points context discovery at an intentionally absent file.
    gemini_disable_context: bool = True
    gemini_context_file_name: str = ".hugin-no-gemini-context.md"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMConfig":
        provider = str(data.get("provider", "codex")).lower()
        if provider not in LLM_PROVIDERS:
            raise ValueError(
                f"llm.provider must be one of: {', '.join(sorted(LLM_PROVIDERS))}"
            )
        clean_cwd = data.get("clean_cwd")
        return cls(
            provider=provider,
            clean_cwd=Path(clean_cwd).expanduser() if clean_cwd else cls().clean_cwd,
            codex_bin=data.get("codex_bin", "codex"),
            claude_bin=data.get("claude_bin", "claude"),
            gemini_bin=data.get("gemini_bin", "gemini"),
            codex_args=_string_list(data, "codex_args", []),
            claude_args=_string_list(data, "claude_args", []),
            gemini_args=_string_list(data, "gemini_args", []),
            local_command=_string_list(data, "local_command", []),
            gemini_disable_context=data.get("gemini_disable_context", True),
            gemini_context_file_name=data.get(
                "gemini_context_file_name", ".hugin-no-gemini-context.md"
            ),
        )


def _uses_default_model(model: str | None) -> bool:
    return not model or model == DEFAULT_REMOTE_MODEL


def _clean_cwd(cfg: LLMConfig) -> Path:
    cfg.clean_cwd.mkdir(parents=True, exist_ok=True)
    return cfg.clean_cwd


def _run(cmd: list[str], prompt: str, cwd: Path, timeout: int, provider: str) -> str:
    result = subprocess.run(
        cmd,
        input=prompt,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"{provider} prompt failed")
    return result.stdout


def _run_codex(cfg: LLMConfig, model: str, prompt: str, effort: str | None, timeout: int) -> str:
    cwd = _clean_cwd(cfg)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as out:
        out_path = Path(out.name)

    try:
        cmd = [
            cfg.codex_bin,
            "exec",
            "-C",
            str(cwd),
            "--skip-git-repo-check",
            "--ephemeral",
        ]
        if not _uses_default_model(model):
            cmd.extend(["-m", model])
        if effort:
            cmd.extend(["-c", f"model_reasoning_effort={effort}"])
        cmd.extend([*cfg.codex_args, "-o", str(out_path), "-"])
        _run(cmd, prompt, cwd, timeout, "codex")
        return out_path.read_text(encoding="utf-8").strip()
    finally:
        out_path.unlink(missing_ok=True)


def _run_claude(cfg: LLMConfig, model: str, prompt: str, effort: str | None, timeout: int) -> str:
    cwd = _clean_cwd(cfg)
    cmd = [
        cfg.claude_bin,
        *cfg.claude_args,
        "--print",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--tools",
        "",
    ]
    if not _uses_default_model(model):
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    return _run(cmd, prompt, cwd, timeout, "claude").strip()


def _prepare_gemini_cwd(cfg: LLMConfig) -> Path:
    cwd = _clean_cwd(cfg)
    if cfg.gemini_disable_context:
        settings_dir = cwd / ".gemini"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "context": {
                "fileName": cfg.gemini_context_file_name,
                "includeDirectoryTree": False,
                "discoveryMaxDirs": 0,
            },
            "ui": {
                "hideBanner": True,
                "hideTips": True,
            },
        }
        (settings_dir / "settings.json").write_text(
            json.dumps(settings, indent=2) + "\n",
            encoding="utf-8",
        )
    return cwd


def _run_gemini(cfg: LLMConfig, model: str, prompt: str, effort: str | None, timeout: int) -> str:
    cwd = _prepare_gemini_cwd(cfg)
    cmd = [
        cfg.gemini_bin,
        "--prompt",
        "",
        "--output-format",
        "text",
        "--raw-output",
        "--accept-raw-output-risk",
        *cfg.gemini_args,
    ]
    if not _uses_default_model(model):
        cmd[1:1] = ["--model", model]
    return _run(cmd, prompt, cwd, timeout, "gemini").strip()


def _run_local(cfg: LLMConfig, model: str, prompt: str, effort: str | None, timeout: int) -> str:
    if not cfg.local_command:
        raise RuntimeError("llm.local_command must be configured when llm.provider is local")
    cwd = _clean_cwd(cfg)
    model_value = "" if _uses_default_model(model) else model
    effort_value = effort or ""
    cmd = [
        part.replace("{model}", model_value).replace("{effort}", effort_value)
        for part in cfg.local_command
    ]
    return _run(cmd, prompt, cwd, timeout, "local").strip()


_PROVIDERS = {
    "codex": _run_codex,
    "claude": _run_claude,
    "gemini": _run_gemini,
    "local": _run_local,
}


def run_prompt(
    cfg: LLMConfig,
    model: str,
    prompt: str,
    effort: str | None = None,
    timeout: int = 300,
) -> str:
    """Run ``prompt`` through the configured provider and return stdout text."""
    runner = _PROVIDERS.get(cfg.provider)
    if runner is None:
        raise ValueError(f"Unsupported LLM provider: {cfg.provider}")
    return runner(cfg, model, prompt, effort, timeout)
