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
| `agy`    | `agy`    | Google Antigravity CLI. Honours `effort`; runs in plan+sandbox mode from a clean cwd. |
| `local`  | (user-supplied command) | Prompt on stdin, response on stdout. Use for llama.cpp etc. |

`agy` replaced the former `gemini` provider when Google moved Business CLI
access to Antigravity. Update existing configs from `provider: gemini` to
`provider: agy`; the old name is not retained as an alias.
Hugin uses Antigravity's stream-JSON mode so prompts stay on stdin rather than
appearing in the process argument list.

`model: default` means "let the provider CLI pick its configured model" —
no `-m` / `--model` flag is passed.

`effort` values: `low` | `medium` | `high`. Ignored by providers that don't
support it.

See `hugin.llm.LLMConfig` for the full schema and `hugin.llm.run_prompt`
for the runner.

## Persistent sessions

`hugin.llm.run_prompt` is one-shot on purpose: it passes `--ephemeral` to codex
and `--no-session-persistence` to claude, so nothing is left behind. When a
tool needs a conversation that survives across turns, use `hugin.session`
instead.

`Session` keeps a provider-side conversation and resumes it by id, running one
process per turn. That does **not** cost prompt caching: the cache is
server-side and keyed on the content prefix, not on the client process.
Measured 2026-08-25, resumed turns in a fresh process read 26665 (claude),
16768 (codex) and 12193 (agy) tokens from cache. So there is no reason to drive
a pty or keep a long-lived child alive, and per-turn processes buy crash
isolation plus sessions the user can open by hand.

| Provider | Id minted by | Resume flag |
|----------|-------------|-------------|
| `claude` | us, `--session-id <uuid>` | `--resume <uuid>` |
| `codex`  | the CLI, `thread.started.thread_id` | `resume <id>` |
| `agy`    | the CLI, `init.conversation_id` | `--conversation <id>` |

Traps worth knowing:

- **codex options must precede the `resume` subcommand.** `codex exec resume
  <id> -s read-only` fails with `unexpected argument '-s'`.
- **agy bakes the reasoning level into the model slug** for gemini models
  (`gemini-3.7-flash-high`), and also has an `--effort` flag. Precedence is
  undocumented, so `Session` lets the slug win and only forwards `--effort`
  when the model is left at the provider default. Note `gemini-3.1-pro` has no
  `medium`, so the `low|medium|high` triple does not map cleanly onto it; a
  roster must be able to pass a raw slug through untouched.
- **Usage arithmetic differs per provider.** claude reports new and cached
  input separately; codex and agy report a total that already includes the
  cached prefix. `session.Usage` normalises to new-vs-cached, so don't compare
  raw provider numbers. `total_cost_usd` is claude-only.
- **`read_only` is a tool list for claude, not a permission mode.**
  `--permission-mode plan` is Claude Code's planning workflow: it writes the
  answer to `~/.claude/plans/` as a side effect and shapes it into an
  implementation plan. Read-only is `--tools=Read,Grep,Glob,WebSearch,WebFetch`
  plus `--strict-mcp-config --mcp-config={"mcpServers":{}}` — without the
  latter the session inherits the user's own MCP servers and can post to Slack.
  Both options are variadic, hence the `=` form: otherwise they swallow the
  prompt.
- **Watching a turn happen: `send(..., on_event=cb)`.** All three providers
  emit NDJSON, so one mechanism covers them: claude needs
  `--output-format stream-json --verbose` (it refuses stream-json in print mode
  without `--verbose`), codex `exec --json` and agy `--output-format
  stream-json` already do. Events are normalised to
  `Event(kind, name, detail)` with kind `tool`, `text` or `notice`. The
  per-provider shapes are: claude `assistant` messages carrying `tool_use`
  blocks; codex `item.started` with an `item.type` (`command_execution`,
  `agent_message`, …); agy `step_update` with `step_type: tool` and a `state`
  of `ACTIVE` or `DONE`. codex and agy report every step twice, so only the
  starting half is emitted. Streaming uses `Popen` with `start_new_session=True`
  and kills the **process group** on timeout: killing the CLI alone leaves its
  children holding the stdout pipe, and the read loop then waits for them
  rather than for the timeout that just fired.
- Unlike `run_prompt`, sessions run in a **caller-supplied cwd**, keep their
  tools, and can be widened past cwd with `extra_dirs` (`--add-dir` for claude
  and agy; codex needs nothing, its read-only sandbox already reads the disk). The clean-cwd rule in the provider table above exists so that one-shot
  prompts don't pick up a repo `CLAUDE.md`; a session whose whole point is that
  the agent can look things up itself needs the opposite. Pass the real
  directory deliberately.

See `hugin.session.Session` and `hugin.session.parse_spec` (the shared
`provider[:model[:effort]]` roster grammar used by both config files and CLI
flags).

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
