# Hugin Conventions

The shared contract every `hugin-*` tool is expected to honour. If you write
a new tool in the stack, read this first.

## Config

- Config lives in `~/.config/hugin/`. Override with `HUGIN_CONFIG_DIR`.
- Two files per tool:
  - `hugin.yaml`        — shared across all tools
  - `<toolname>.yaml`   — tool-specific (e.g. `meetings.yaml`, `agenda.yaml`)
- The tool file wins on overlap. Deep-merge: nested mappings are merged
  recursively; lists and scalars are replaced wholesale.
- `~` and `$VARS` are expanded automatically in string values.

## Shared fields

These live at the top level of `hugin.yaml` and may be relied on by any tool:

| Field | Type | Notes |
|-------|------|-------|
| `language` | `en` \| `sv` | First-class languages. Drives weekday names, prompt selection, etc. |
| `user_name` | string | Used in personal sections, summaries |
| `vault_path` | path | Root of your Obsidian / notes vault |
| `journal_path` | path | Daily journal file inside the vault |
| `gws_bin` | string | `gws` (Google Workspace CLI) binary on PATH |
| `gws_config_dir` | path | Optional override for `gws`'s own config |

A tool may **read** these from the merged config but must not require them
to be present — pick sensible defaults when they're missing.

## Vault layout (suggested)

`hugin-init` scaffolds this. Tools should default to these paths but let
the user override.

```
<vault_path>/
  journal/
    journal.md                  # current year, header "# Journal YYYY"
    archive/                    # rotated archives (or `arkiv/` when language: sv)
  meetings/
    transcripts/
    summaries/
  projects/                     # one .md per project / account / client
  agenda_templates/             # optional user override of packaged templates
```

## Markdown header conventions

| Where | Header pattern |
|-------|----------------|
| Agenda day | `## <YYYY-MM-DD>` (auto-rewritten by hugin-agenda) |
| GTD week | `## Week` (en) / `## Vecka` (sv) |
| GTD day | `### Monday` (en) / `### Måndag` (sv); localised by `language` |
| Journal year | `# Journal YYYY` |
| Meeting summary section | `## Meeting Summary` (en) / `## Mötessammanfattning` (sv); configurable |
| Personal carve-out inside summary | `### For Me` (en) / `### För <Name>` (sv); optional |

Tools that rewrite vault files must touch only their designated section and
preserve everything around it.

## LLM provider naming

Tools that call coding-agent CLIs use these provider names:

| Provider | Binary | Notes |
|----------|--------|-------|
| `codex`  | `codex`  | Default. Honours `effort`. |
| `claude` | `claude` | Honours `effort`. Runs from a clean cwd so repo `CLAUDE.md` files are not discovered. |
| `gemini` | `gemini` | Ignores `effort`. Context discovery disabled. |
| `local`  | (user-supplied command) | Prompt on stdin, response on stdout. Use for llama.cpp etc. |

`model: default` means "let the provider CLI pick its configured model" —
no `-m` / `--model` flag is passed.

`effort` values: `low` | `medium` | `high`. Ignored by providers that don't
support it.

See `hugin.llm.LLMConfig` for the full schema and `hugin.llm.run_prompt`
for the runner.

## Languages

`en` and `sv` are first-class. A tool that ships language-dependent assets
(prompts, weekday names, templates) should:

- Ship the English version as the default.
- Ship the Swedish version alongside, picked when `language: sv`.
- Allow a user-supplied path to override either.

Other languages are welcome but treated as user-supplied overrides; the
project doesn't ship them.

## Prompt files

Tools that ship LLM prompt templates use this naming convention:

- ``<base>_default.md`` — the always-shipped fallback (English).
- ``<base>_<lang>.md`` — optional language variant, auto-picked when the
  shared ``language`` field matches.

A user-supplied explicit path in config (e.g. ``summarize_prompt_path``)
overrides both. Use `hugin.prompts.resolve_prompt` to get the right file for
the active language.

Files suffixed ``.example.md`` (e.g. ``summary_sv_personal.example.md``)
are starter templates — never auto-picked. They exist for users to copy
and adapt.

## State vs output

- **Output** (transcripts, summaries, generated agendas) goes into
  `vault_path` — it's content the user wants to keep and read.
- **State** (caches, model weights, embeddings, raw audio) goes into
  `~/.<tool>/` or a configurable `state_dir` — it's safe to wipe.

Never mix them.
