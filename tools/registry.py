"""Collects every KAI Pulse tool into one list, grouped by specialist agent.

The groups match the target architecture: Finance, Documents and Export,
Data and Charts, Cognos. Import fails loudly if two tools share a name, so
the duplicate-definition problem from the Orchestrate version can't return.
"""
from __future__ import annotations

from importlib import import_module

from langchain_core.tools import BaseTool

# module name -> specialist group
_MODULES: dict[str, str] = {
    "bank_statement_tool": "finance",
    "pdf_tool": "documents",
    "pdf_report_tool": "documents",
    "export_table_tool": "documents",
    "sendgrid_email_tool": "documents",
    "session_summary_tools": "documents",
    "chart_analytics_tools": "data",
    "chart_csv_tool": "data",
    "chart_excel_ts_tool": "data",
    "chart_s3_tool": "data",
    "chart_cos_tool": "data",
    "file_browser_tool": "data",
    "file_upload_s3_tool": "data",
    "fetch_text_from_url_tool": "data",
    "cognos_analytics_tools": "cognos",
    "hello_tool": "general",
}


def _tools_in(module_name: str) -> list[BaseTool]:
    module = import_module(f"tools.{module_name}")
    return [v for v in vars(module).values() if isinstance(v, BaseTool)]


def load_tools() -> dict[str, list[BaseTool]]:
    """Return {group: [tools]} and check that every tool name is unique."""
    groups: dict[str, list[BaseTool]] = {}
    seen: dict[str, str] = {}
    for module_name, group in _MODULES.items():
        for t in _tools_in(module_name):
            if t.name in seen and seen[t.name] != module_name:
                raise ValueError(
                    f"Tool name '{t.name}' is defined in both "
                    f"{seen[t.name]} and {module_name}"
                )
            if t.name in seen:
                continue  # same object imported twice within one module
            seen[t.name] = module_name
            groups.setdefault(group, []).append(t)
    return groups


def all_tools() -> list[BaseTool]:
    return [t for group in load_tools().values() for t in group]
