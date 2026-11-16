"""Finance tools that read statements and the analysis from graph state.

They wrap the Phase 1 tools in tools/bank_statement_tool.py, but the model
never passes file contents or the analysis JSON: it passes file ids, and the
tools load everything else from state and the FileStore.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId
from langgraph.prebuilt import InjectedState

from kai.files import read_text, store_from_config
from kai.state import Analysis, ChartRef, find_file
from kai.tools._common import chart_url, reply, unknown_file
from tools._tooling import tool
from tools.bank_statement_tool import (
    analyze_bank_statement,
    render_financial_chart,
    simulate_mortgage_affordability,
)

_STATEMENT_KINDS = ("csv", "pdf")
_KEY_METRICS = (
    "avg_monthly_income", "avg_monthly_spending", "avg_monthly_savings",
    "avg_monthly_debt", "debt_obligations_pct", "top_spending_category",
    "positive_cashflow_months", "total_months", "savings_growth_pct",
)


def _metrics_brief(metrics: dict) -> str:
    lines = [f"- {k}: {metrics[k]}" for k in _KEY_METRICS if k in metrics]
    summary = metrics.get("summary")
    head = f"{summary}\n\n" if isinstance(summary, str) and summary.strip() else ""
    return head + "Key metrics (" + str(metrics.get("currency", "")) + "):\n" + "\n".join(lines)


@tool
def queue_statement(
    file_id: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> object:
    """Queue an uploaded bank statement to analyze later with the others.

    Use when the user uploads a statement and says "add" (they will upload
    more before analyzing). Does not analyze anything.

    Args:
        file_id: Id of the uploaded statement file (CSV or PDF).
    """
    ref = find_file(state, file_id)
    if ref is None:
        return unknown_file(file_id, state)
    if ref["kind"] not in _STATEMENT_KINDS:
        return f"Error: {ref['name']} is not a CSV or PDF statement."
    queued = list(state.get("statements") or [])
    count = len(queued) + (0 if file_id in queued else 1)
    return reply(
        tool_call_id,
        f"Statement {count} received: {ref['name']}. "
        "Upload the next file, or say 'analyze all' when you're done.",
        statements=[file_id],
    )


@tool
def analyze_statements(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: RunnableConfig,
    file_ids: Optional[list[str]] = None,
    currency: str = "EGP",
    language: Optional[str] = None,
) -> object:
    """Analyze bank statements and store the result for follow-up questions.

    With no file_ids, analyzes every statement queued with queue_statement.
    Running it again on the same files returns the stored result instead of
    re-analyzing.

    Args:
        file_ids: Ids of the statement files to analyze. Omit to use the queued statements.
        currency: Currency label shown in the statements, e.g. "EGP".
        language: "English" or "Arabic". Defaults to the conversation language.
    """
    ids = list(file_ids or state.get("statements") or [])
    if not ids:
        return ("Error: no statements to analyze. Pass file_ids, or queue "
                "statements with queue_statement first.")

    previous = state.get("analysis")
    if previous and set(previous["file_ids"]) == set(ids):
        return "Already analyzed these statements.\n\n" + _metrics_brief(previous["metrics"])

    refs = []
    for fid in ids:
        ref = find_file(state, fid)
        if ref is None:
            return unknown_file(fid, state)
        if ref["kind"] not in _STATEMENT_KINDS:
            return f"Error: {ref['name']} is not a CSV or PDF statement."
        refs.append(ref)

    kinds = {r["kind"] for r in refs}
    if len(kinds) > 1:
        return "Error: analyze CSV and PDF statements separately; got both kinds."

    store = store_from_config(config)
    texts = [read_text(store, r) for r in refs]
    args = {"currency": currency, "language": language or state.get("language", "English")}
    if kinds == {"csv"}:
        args["csv_files"] = json.dumps(texts)
    else:
        args["statement_text"] = "\n\n".join(texts)

    raw = analyze_bank_statement.func(**args)
    try:
        metrics = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return f"Analysis failed: {raw}"
    if not isinstance(metrics, dict) or not metrics.get("months"):
        return f"Analysis found no transactions: {raw[:500]}"

    analysis = Analysis(file_ids=ids, metrics=metrics)
    return reply(tool_call_id, _metrics_brief(metrics), analysis=analysis, statements=ids)


@tool
def simulate_mortgage(
    state: Annotated[dict, InjectedState],
    max_dti: float = 40.0,
) -> str:
    """Estimate an affordable monthly mortgage installment from the stored analysis.

    Args:
        max_dti: Maximum debt-to-income ratio in percent. Banks typically use 35 to 45.
    """
    analysis = state.get("analysis")
    if not analysis:
        return "Error: no analysis yet. Run analyze_statements first."
    metrics = analysis["metrics"]
    return simulate_mortgage_affordability.func(
        analysis_json=json.dumps(metrics),
        currency=metrics.get("currency", "EGP"),
        max_dti=max_dti,
    )


@tool
def financial_chart(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    chart_type: Literal["savings", "income_vs_spending", "cashflow", "categories"] = "savings",
    title: str = "",
) -> object:
    """Draw a chart from the stored statement analysis and keep it for reports.

    Args:
        chart_type: "savings", "income_vs_spending", "cashflow" or "categories".
        title: Optional chart title.
    """
    analysis = state.get("analysis")
    if not analysis:
        return "Error: no analysis yet. Run analyze_statements first."
    rendered = render_financial_chart.func(
        analysis_json=json.dumps(analysis["metrics"]), chart_type=chart_type, title=title,
    )
    url = chart_url(rendered)
    if not url:
        return rendered  # the wrapped tool's error message
    chart = ChartRef(title=title or chart_type.replace("_", " ").title(),
                     url=url, source=f"financial:{chart_type}")
    return reply(tool_call_id, rendered, charts=[chart])


FINANCE_TOOLS = [queue_statement, analyze_statements, simulate_mortgage, financial_chart]
