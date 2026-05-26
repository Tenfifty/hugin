# Hugin

Hugin is a personal productivity stack — a set of small Unix-style tools that
read from your calendar, email, meeting recordings, and notes vault, and write
back useful synthesis. The pieces are independently installable; this
repository is the **shared library and conventions** they all build on.

All text is stored in markdown and interacts especially well with Obsidian.

## The stack

| Repo | What it does |
|------|--------------|
| [`hugin`](.) | Shared config loader, LLM provider, conventions |
| [`hugin-meetings`](https://github.com/Tenfifty/hugin-meetings) | Record, transcribe, diarize, summarize meetings |
| [`hugin-agenda`](https://github.com/Tenfifty/hugin-agenda) | Generate daily agendas from calendar + GTD |

Each tool depends on `hugin` for its config layer and (optionally) LLM
provider, but is otherwise self-contained. You only install the tools you want.

## Install

```bash
pip install -e .
hugin-init           # scaffolds ~/.config/hugin/ and a vault layout
```

`hugin-init` is interactive by default. For non-interactive use:

```bash
hugin-init --vault ~/Documents/notes --language en --user-name "Your Name"
hugin-init --dry-run        # show what would happen
```

The script never overwrites existing files unless you pass `--force`.

## What's in the library

```
src/hugin/
  config.py       SharedConfig + load_tool(name, builder)
  llm.py          LLMConfig + run_prompt() for codex/claude/gemini/local
  init.py         hugin-init CLI
```

`SharedConfig` is the dataclass every `hugin-*` tool extends. `load_tool`
reads `~/.config/hugin/hugin.yaml` + `~/.config/hugin/<tool>.yaml`,
deep-merges them (tool wins), expands `~`/env vars, and hands the result to
your builder.

```python
from dataclasses import dataclass

from hugin.config import SharedConfig, load_tool

@dataclass
class MyToolConfig(SharedConfig):
    extra_field: str = "default"

    @classmethod
    def from_merged(cls, merged: dict) -> "MyToolConfig":
        tool = merged.get("mytool", {})
        return cls(
            **SharedConfig.fields_from_merged(merged),
            extra_field=tool.get("extra_field", "default"),
        )

def load_config() -> MyToolConfig:
    return load_tool("mytool", MyToolConfig.from_merged)
```

## Conventions

See [`CONVENTIONS.md`](CONVENTIONS.md) for the contract every `hugin-*` tool
honours: config file layout, vault structure, markdown header naming,
supported languages, LLM provider naming.

## Status

Early. The shared config boundary is stable; the LLM runner is intentionally
small and wraps Codex, Claude, Gemini, or a local command. MIT licensed.
