"""Shared ``@tool`` decorator for every KAI Pulse tool.

Wraps LangChain's ``tool`` so each function becomes a ``BaseTool`` whose
argument descriptions come from its Google-style docstring (the ``Args:``
section), the same docstrings the watsonx Orchestrate ADK used.
"""
from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as _lc_tool

# Names of tools whose docstring could not be parsed per argument.
DOCSTRING_FALLBACKS: set[str] = set()

_DEFAULTS: dict[str, Any] = {
    "parse_docstring": True,
    # Some docstrings document extra lines LangChain can't parse; fall back to
    # the plain description instead of failing at import time.
    "error_on_invalid_docstring": False,
}


def tool(func: Callable[..., Any] | None = None, **kwargs: Any):
    """Use as ``@tool`` or ``@tool(name=..., return_direct=...)``."""
    options = {**_DEFAULTS, **kwargs}

    def wrap(f: Callable[..., Any]) -> BaseTool:
        try:
            return _lc_tool(f, **options)
        except ValueError:
            # The parser reads wrapped Args lines like "x (y): z" as extra args
            # and rejects them. Keep the whole docstring as the description so
            # the model still sees every argument note.
            fallback = {**options, "parse_docstring": False}
            fallback.pop("error_on_invalid_docstring", None)
            built = _lc_tool(f, **fallback)
            DOCSTRING_FALLBACKS.add(built.name)
            return built

    return wrap(func) if func is not None else wrap
