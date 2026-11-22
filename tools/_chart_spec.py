"""Chart specs: the data behind each chart, so the chat page can draw it interactively.

Every chart tool builds a Chart.js config and turns it into a QuickChart image
URL in its `_chart_url` helper. `@records_chart` on that helper also records
(url, config) while a `capture()` block is open. `to_spec` reduces a config to
plain data (type, labels, numbers, a few flags): no functions, no styling and
no HTML, so the browser draws it with its own formatting and nothing the model
wrote can run as code. The PNG URL stays the fallback and is what reports use.
"""
from __future__ import annotations

import copy
import functools
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Optional

_recorded: ContextVar[Optional[list]] = ContextVar("kai_chart_specs", default=None)

MAX_LABELS = 500
MAX_DATASETS = 12
MAX_TEXT = 120
KINDS = {"bar": "bar", "horizontalBar": "bar", "line": "line", "pie": "pie",
         "doughnut": "doughnut", "radar": None, "scatter": None, "bubble": None}


_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def png_safe(config: Any) -> Any:
    """A copy of the config for the PNG: the public QuickChart renderer has no
    Arabic font and draws Arabic text as '?????', so an Arabic chart title or
    axis title is hidden there. The report shows the title as a caption under
    the image, and the interactive chart (built from the original) shows it all."""
    if not isinstance(config, dict):
        return config
    cfg = copy.deepcopy(config)
    options = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
    plugins = options.get("plugins") if isinstance(options.get("plugins"), dict) else {}
    title = plugins.get("title")
    if isinstance(title, dict) and _ARABIC.search(_text(title.get("text"))):
        title["display"] = False
    scales = options.get("scales") if isinstance(options.get("scales"), dict) else {}
    for scale in scales.values():
        st = scale.get("title") if isinstance(scale, dict) else None
        if isinstance(st, dict) and _ARABIC.search(_text(st.get("text"))):
            st["display"] = False
    return cfg


def records_chart(chart_url: Callable[..., str]) -> Callable[..., str]:
    """Decorate a tool's `_chart_url(config, ...)`: the PNG gets a `png_safe` copy,
    and the original config is recorded with its URL."""

    @functools.wraps(chart_url)
    def wrapper(config: dict, *args, **kwargs) -> str:
        url = chart_url(png_safe(config), *args, **kwargs)
        found = _recorded.get()
        if found is not None and isinstance(url, str):
            found.append((url, config))
        return url

    return wrapper


@contextmanager
def capture() -> Iterator[list]:
    """Collect (url, config) for every chart drawn inside the block."""
    token = _recorded.set([])
    try:
        yield _recorded.get()
    finally:
        _recorded.reset(token)


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    return str(value if value is not None else "")[:MAX_TEXT]


def _number(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("y", value.get("r"))
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def to_spec(config: Any, unit: str = "") -> Optional[dict]:
    """Plain-data spec for the chat page, or None when the chart can't be drawn interactively."""
    if not isinstance(config, dict):
        return None
    raw_type = str(config.get("type") or "")
    kind = KINDS.get(raw_type)
    data = config.get("data") if isinstance(config.get("data"), dict) else {}
    labels = data.get("labels")
    datasets = data.get("datasets")
    if not kind or not isinstance(labels, list) or not isinstance(datasets, list) or not datasets:
        return None
    labels = [_text(l) for l in labels[:MAX_LABELS]]
    options = config.get("options") if isinstance(config.get("options"), dict) else {}
    scales = options.get("scales") if isinstance(options.get("scales"), dict) else {}
    plugins = options.get("plugins") if isinstance(options.get("plugins"), dict) else {}
    title = plugins.get("title") if isinstance(plugins.get("title"), dict) else {}

    out_sets = []
    for ds in datasets[:MAX_DATASETS]:
        if not isinstance(ds, dict) or not isinstance(ds.get("data"), list):
            continue
        values = [_number(v) for v in ds["data"][:len(labels)]]
        if not any(v is not None for v in values):
            continue
        entry = {"label": _text(ds.get("label")), "data": values}
        if kind == "line" and ds.get("fill") not in (None, False):
            entry["area"] = True
        if ds.get("type") in ("line", "bar") and ds.get("type") != kind:
            entry["kind"] = ds["type"]      # mixed bar + line chart
        out_sets.append(entry)
    if not out_sets:
        return None

    stacked = any(isinstance(s, dict) and s.get("stacked") for s in scales.values())
    spec = {"kind": kind, "title": _text(title.get("text")) if title.get("display", True) else "",
            "labels": labels, "datasets": out_sets}
    if stacked:
        spec["stacked"] = True
    if raw_type == "horizontalBar" or options.get("indexAxis") == "y":
        spec["horizontal"] = True
    if unit:
        spec["unit"] = _text(unit)[:12]
    return spec
