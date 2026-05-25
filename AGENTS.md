# Repo guidance for Claude / Codex

This is the **shared library** for the Hugin stack — `hugin-meetings`,
`hugin-agenda`, and any future `hugin-*` tools depend on it.

The contract every consumer follows (config layout, vault structure,
markdown headers, language handling, LLM provider naming, prompt-file
convention) lives in [`CONVENTIONS.md`](CONVENTIONS.md). Read that
before changing any public API in `src/hugin/`.

## What's here

- `src/hugin/config.py` — `SharedConfig` + `load_tool(name, builder)` + helpers
- `src/hugin/llm.py` — `LLMConfig` + `run_prompt` (codex / claude / gemini / local)
- `src/hugin/prompts.py` — `resolve_prompt(base, language, explicit, package_dir)`
- `src/hugin/init.py` — the `hugin-init` CLI

## Tests

```
pytest                 # runs all
pytest tests/test_config.py::DeepMergeTests::test_lists_are_replaced_not_merged
```

Tests live in `tests/` and rely on `hugin` being editable-installed
(`pip install -e .`). They use `HUGIN_CONFIG_DIR` env-var overrides
and `tempfile.TemporaryDirectory()` rather than touching the real
`~/.config/hugin/`.

## Status

Early. The public surface (`SharedConfig`, `load_tool`, `LLMConfig`,
`run_prompt`, `resolve_prompt`) is stable; internals are not.
