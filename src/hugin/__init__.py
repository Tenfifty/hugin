"""Shared library for the Hugin personal productivity stack.

See CONVENTIONS.md for the contract every hugin-* tool follows.
"""

from hugin.config import SharedConfig, load_tool, load_shared, config_dir
from hugin.llm import LLMConfig, run_prompt
from hugin.prompts import resolve_prompt

__all__ = [
    "SharedConfig",
    "load_tool",
    "load_shared",
    "config_dir",
    "LLMConfig",
    "run_prompt",
    "resolve_prompt",
]
