"""
chart_analytics_tools.py
------------------------
ADK tools that generate charts as QuickChart.io image URLs,
returned as Markdown so they render inline in watsonx Orchestrate chat.

Available tools:
  - render_pie_chart      : Pie / donut chart
  - render_time_series    : Line / area time-series chart
  - render_bar_chart      : Grouped / stacked bar chart
"""
from __future__ import annotations

import json
import urllib.parse

from ibm_watsonx_orchestrate.agent_builder.tools import tool


# ── Internal helpers ──────────────────────────────────────────────────────────

_QUICKCHART = "https://quickchart.io/chart"

# Default colour palette
_COLORS = [
    "#2E6DB4", "#C8A84B", "#E05C2E", "#3DAA6D",
    "#9B59B6", "#1ABC9C", "#E74C3C", "#F39C12",
]


def _chart_url(config: dict, width: int = 600, height: int = 400) -> str:
    """Build a QuickChart.io URL for the given Chart.js config dict."""
    import copy, urllib.request
    cfg = copy.deepcopy(config)
    opts = cfg.setdefault("options", {})
    pl = opts.setdefault("plugins", {})
    pl.setdefault("title", {}).setdefault("font", {}).setdefault("size", 11)
    pl.setdefault("legend", {}).setdefault("labels", {}).setdefault("font", {}).setdefault("size", 10)
    for sc in opts.get("scales", {}).values():
        sc.setdefault("ticks", {}).setdefault("font", {}).setdefault("size", 9)
        if "title" in sc:
            sc["title"].setdefault("font", {}).setdefault("size", 9)
    # POST to /chart/create for a short URL (avoids URL length limits)
    body = json.dumps({"chart": cfg, "width": width, "height": height, "backgroundColor": "white"}).encode("utf-8")
    req = urllib.request.Request(
        "https://quickchart.io/chart/create",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["url"]
    except Exception:
        params = urllib.parse.urlencode({
            "c": json.dumps(cfg, separators=(",", ":")),
            "w": width,
            "h": height,
            "bkg": "white",
        })
        return f"{_QUICKCHART}?{params}"


def _parse_json_arg(raw, name: str):
    """Parse a JSON string argument; also accepts already-parsed list/dict."""
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"'{name}' must be valid JSON. Error: {exc}") from exc


def _to_bool(value) -> bool:
    """Safely coerce LLM-supplied bool/string values to Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _parse_json_arg(raw, name: str):
    """Parse a JSON string argument; also accepts already-parsed list/dict."""
    if isinstance(raw, (list, dict)):
        return raw  # IBM Cloud runtime may pre-parse JSON parameters
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"'{name}' must be valid JSON. Error: {exc}") from exc


def _to_bool(value) -> bool:
    """Safely coerce LLM-supplied bool/string values to Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


# ── Tool 1: Pie / Donut chart ─────────────────────────────────────────────────

@tool
def render_pie_chart(
    labels_json: str,
    values_json: str,
    title: str = "Pie Chart",
    donut: str = "false",
) -> str:
    """
    Generate a pie or donut chart and return it as a Markdown image.

    Args:
        labels_json: JSON array of slice labels. Example: ["USD","EUR","GBP"]
        values_json: JSON array of numeric values matching labels. Example: [50,30,20]
        title: Chart title shown at the top.
        donut: Set to "true" for a donut chart (hollow centre). Default "false".

    Returns:
        Markdown image string that renders as an inline chart.

    Example:
        render_pie_chart(
            labels_json='["USD","EUR","GBP","SAR"]',
            values_json='[45, 25, 20, 10]',
            title="Currency Distribution",
            donut="true"
        )
    """
    labels = _parse_json_arg(labels_json, "labels_json")
    values = _parse_json_arg(values_json, "values_json")
    is_donut = _to_bool(donut)
    n = len(labels)
    colors = [_COLORS[i % len(_COLORS)] for i in range(n)]

    config = {
        "type": "doughnut" if is_donut else "pie",
        "data": {
            "labels": labels,
            "datasets": [{"data": values, "backgroundColor": colors}],
        },
        "options": {
            "plugins": {
                "title": {"display": True, "text": title},
                "legend": {"position": "bottom"},
            }
        },
    }
    url = _chart_url(config)
    return f'<img src="{url}" alt="{title}" style="max-width:60%;display:block" />\n\n<a href="{url}">&#11015;&#xFE0F; Download chart</a>\n\n'


# ── Tool 2: Time-series (line / area) chart ───────────────────────────────────

@tool
def render_time_series(
    series_json: str,
    title: str = "Time Series",
    x_label: str = "Date",
    y_label: str = "Value",
    area: str = "false",
) -> str:
    """
    Generate a time-series line or area chart and return it as a Markdown image.

    Args:
        series_json: JSON array of series objects. Each object must have:
                     "name" (string), "x" (array of labels), "y" (array of numbers).
                     Example: [{"name":"USD Buy","x":["Jan","Feb","Mar"],"y":[52.1,53.4,53.9]}]
        title:   Chart title.
        x_label: X-axis label.
        y_label: Y-axis label.
        area:    Set to "true" to fill the area under each line. Default "false".

    Returns:
        Markdown image string that renders as an inline chart.
    """
    series = _parse_json_arg(series_json, "series_json")
    is_area = _to_bool(area)
    if not isinstance(series, list) or not series:
        raise ValueError("series_json must be a non-empty JSON array.")

    labels = series[0].get("x", [])
    datasets = []
    for i, s in enumerate(series):
        color = _COLORS[i % len(_COLORS)]
        datasets.append({
            "label": s.get("name", f"Series {i+1}"),
            "data": s.get("y", []),
            "borderColor": color,
            "backgroundColor": color + "33" if is_area else "transparent",
            "fill": is_area,
            "tension": 0.3,
            "pointRadius": 4,
            "pointBackgroundColor": color,
        })

    config = {
        "type": "line",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "plugins": {
                "title": {"display": True, "text": title},
                "legend": {"position": "bottom"},
            },
            "scales": {
                "x": {"title": {"display": bool(x_label), "text": x_label}},
                "y": {"title": {"display": bool(y_label), "text": y_label}},
            },
        },
    }
    url = _chart_url(config)
    return f'<img src="{url}" alt="{title}" style="max-width:60%;display:block" />\n\n<a href="{url}">&#11015;&#xFE0F; Download chart</a>\n\n'


# ── Tool 3: Bar chart (grouped / stacked) ────────────────────────────────────

@tool
def render_bar_chart(
    categories_json: str,
    series_json: str,
    title: str = "Bar Chart",
    x_label: str = "",
    y_label: str = "",
    stacked: str = "false",
    horizontal: str = "false",
) -> str:
    """
    Generate a grouped or stacked bar chart and return it as a Markdown image.

    Args:
        categories_json: JSON array of category labels. Example: ["Jan","Feb","Mar"]
        series_json:     JSON array of series objects, each with:
                         "name" (string) and "values" (array of numbers).
                         Example: [{"name":"USD","values":[53.1,53.4,53.9]}]
        title:      Chart title.
        x_label:    X-axis label.
        y_label:    Y-axis label.
        stacked:    Set to "true" to stack bars. Default "false".
        horizontal: Set to "true" for horizontal bars. Default "false".

    Returns:
        Markdown image string that renders as an inline chart.
    """
    categories = _parse_json_arg(categories_json, "categories_json")
    series     = _parse_json_arg(series_json,     "series_json")
    is_stacked = _to_bool(stacked)
    is_horiz   = _to_bool(horizontal)

    datasets = []
    for i, s in enumerate(series):
        color = _COLORS[i % len(_COLORS)]
        datasets.append({
            "label": s.get("name", f"Series {i+1}"),
            "data": s.get("values", []),
            "backgroundColor": color,
            "borderColor": color,
            "borderWidth": 1,
        })

    scale_opts: dict = {}
    if is_stacked:
        scale_opts = {"x": {"stacked": True}, "y": {"stacked": True}}
    if x_label:
        scale_opts.setdefault("x", {})["title"] = {"display": True, "text": x_label}
    if y_label:
        scale_opts.setdefault("y", {})["title"] = {"display": True, "text": y_label}

    config = {
        "type": "bar",
        "data": {"labels": categories, "datasets": datasets},
        "options": {
            "indexAxis": "y" if is_horiz else "x",
            "plugins": {
                "title": {"display": True, "text": title},
                "legend": {"position": "bottom"},
            },
            "scales": scale_opts,
        },
    }
    url = _chart_url(config)
    return f'<img src="{url}" alt="{title}" style="max-width:60%;display:block" />\n\n<a href="{url}">&#11015;&#xFE0F; Download chart</a>\n\n'
