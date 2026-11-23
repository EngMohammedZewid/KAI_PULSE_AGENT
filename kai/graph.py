"""The KAI Pulse graph.

    START -> detect_language -> ingest --("add" statements)--> END
                                  |
                                router --> finance | documents | data | cognos | general --> END

detect_language and ingest are fixed nodes (no LLM). The router makes the one
routing decision per turn; each specialist is a ReAct agent with only its own
tools and a short prompt plus a context block built from state.
"""
from __future__ import annotations

from typing import Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.managed import RemainingSteps
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import BaseModel, Field
from typing_extensions import NotRequired

from kai.nodes import after_ingest, detect_language, ingest, route_target
from kai.prompts import ROUTER_PROMPT, context_block, specialist_prompt
from kai.state import KaiState
from kai.tools._common import record_charts
from kai.tools.aggregate import aggregate_file
from kai.tools.data_files import DATA_FILE_TOOLS, list_files
from kai.tools.documents import create_report
from kai.tools.exports import export_table_to_file
from kai.tools.finance import FINANCE_TOOLS, financial_chart
from kai.tools.financing import FINANCING_TOOLS
from tools.chart_analytics_tools import render_bar_chart, render_pie_chart, render_time_series
from tools.chart_cos_tool import inspect_cos_file, list_cos_files, render_chart_from_cos
from tools.chart_s3_tool import inspect_s3_file, list_s3_files, render_chart_from_s3
from tools.cognos_analytics_tools import (
    cognos_generate_embed_url,
    cognos_get_widget_bundle,
    cognos_list_content,
    cognos_render_widget_iframe,
)
from tools.fetch_text_from_url_tool import fetch_text_from_url
from tools.sendgrid_email_tool import send_file_via_sendgrid

SPECIALIST_TOOLS = {
    "finance": [*FINANCE_TOOLS, *FINANCING_TOOLS, list_files],
    "documents": [create_report, financial_chart, aggregate_file, export_table_to_file,
                  send_file_via_sendgrid],
    "data": [*DATA_FILE_TOOLS, aggregate_file, render_bar_chart, render_pie_chart, render_time_series,
             list_s3_files, inspect_s3_file, render_chart_from_s3,
             list_cos_files, inspect_cos_file, render_chart_from_cos, fetch_text_from_url],
    "cognos": [cognos_list_content, cognos_generate_embed_url,
               cognos_render_widget_iframe, cognos_get_widget_bundle],
}
AGENTS = (*SPECIALIST_TOOLS, "general")
ROUTER_HISTORY = 6  # recent messages the router sees


class Route(BaseModel):
    """The specialist that should answer the user's latest message."""

    agent: Literal["finance", "documents", "data", "cognos", "general"] = Field(
        description="Which specialist answers this message.")


class SpecialistState(KaiState):
    remaining_steps: NotRequired[RemainingSteps]


def _router(model: BaseChatModel):
    picker = model.with_structured_output(Route)

    def router(state: dict) -> dict:
        history = [m for m in state["messages"] if m.type in ("human", "ai") and m.content]
        prompt = [SystemMessage(f"{ROUTER_PROMPT}\n\n{context_block(state)}"),
                  *history[-ROUTER_HISTORY:]]
        try:
            choice: Optional[Route] = picker.invoke(prompt)
        except Exception:
            choice = None  # an unparseable answer falls back to the general agent
        return {"route": choice.agent if isinstance(choice, Route) else "general"}

    return router


def _specialist(model: BaseChatModel, name: str):
    return create_react_agent(
        model,
        ToolNode(SPECIALIST_TOOLS[name], wrap_tool_call=record_charts),
        prompt=lambda state: [SystemMessage(specialist_prompt(name, state)), *state["messages"]],
        state_schema=SpecialistState,
        name=name,
    )


def _general(model: BaseChatModel):
    def general(state: dict) -> dict:
        answer = model.invoke([SystemMessage(specialist_prompt("general", state)),
                               *state["messages"]])
        return {"messages": [answer if isinstance(answer, AIMessage) else AIMessage(str(answer))]}

    return general


def build_graph(model: Optional[BaseChatModel] = None, checkpointer=None):
    """Compile the graph. Without a model, uses kai.models.get_chat_model()."""
    if model is None:
        from kai.models import get_chat_model

        model = get_chat_model()

    builder = StateGraph(KaiState)
    builder.add_node("detect_language", detect_language)
    builder.add_node("ingest", ingest)
    builder.add_node("router", _router(model))
    for name in SPECIALIST_TOOLS:
        builder.add_node(name, _specialist(model, name))
    builder.add_node("general", _general(model))

    builder.add_edge(START, "detect_language")
    builder.add_edge("detect_language", "ingest")
    builder.add_conditional_edges("ingest", after_ingest, {"done": END, "router": "router"})
    builder.add_conditional_edges("router", route_target, {a: a for a in AGENTS})
    for name in AGENTS:
        builder.add_edge(name, END)
    return builder.compile(checkpointer=checkpointer)
