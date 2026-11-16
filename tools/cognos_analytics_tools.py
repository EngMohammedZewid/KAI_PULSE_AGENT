from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urljoin

import requests
from tools._tooling import tool


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _base_url() -> str:
    base = _env("COGNOS_BASE_URL")
    if not base:
        raise ValueError("COGNOS_BASE_URL is not set.")
    return base.rstrip("/")


def _auth_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Accept": "application/json"}

    bearer = _env("COGNOS_BEARER_TOKEN")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    api_key = _env("COGNOS_API_KEY")
    if api_key:
        # Some gateways use X-API-Key, others use custom header names.
        key_header_name = _env("COGNOS_API_KEY_HEADER", "X-API-Key")
        headers[key_header_name] = api_key

    return headers


def _cookies() -> Dict[str, str]:
    campassport = _env("COGNOS_CAM_PASSPORT")
    if not campassport:
        return {}
    return {"CAMPassport": campassport}


def _api_get(path: str, params: Optional[Dict[str, Any]] = None, timeout_sec: int = 30) -> Dict[str, Any]:
    base = _base_url()
    endpoint = urljoin(base + "/", path.lstrip("/"))

    response = requests.get(
        endpoint,
        headers=_auth_headers(),
        cookies=_cookies(),
        params=params or {},
        timeout=timeout_sec,
    )
    response.raise_for_status()

    if not response.text.strip():
        return {"status_code": response.status_code, "data": None}

    try:
        return response.json()
    except Exception:
        return {
            "status_code": response.status_code,
            "raw_text": response.text,
        }


def _build_iframe(embed_url: str, width: str, height: str, title: str) -> str:
    safe_title = (title or "Cognos Widget").replace('"', "'")
    safe_url = embed_url.replace('"', "&quot;")
    return (
        f'<iframe src="{safe_url}" title="{safe_title}" '
        f'width="{width}" height="{height}" '
        'style="border:0;" loading="lazy" referrerpolicy="no-referrer"></iframe>'
    )


@tool
def cognos_list_content(
    search: str = "",
    limit: int = 20,
    path: str = "/api/v1/content",
) -> str:
    """
    List Cognos content entries through Cognos REST API.

    Args:
        search: Optional free-text filter/query supported by your Cognos API gateway.
        limit: Maximum number of items to request.
        path: API path for content listing. Defaults to /api/v1/content.

    Returns:
        JSON string with content entries and metadata.
    """
    params: Dict[str, Any] = {"limit": max(1, min(limit, 200))}
    if search:
        params["search"] = search

    result = _api_get(path, params=params)
    return json.dumps(result, ensure_ascii=False)


@tool
def cognos_generate_embed_url(
    object_id: str,
    perspective: str = "dashboard",
    hide_navigation: bool = True,
    extra_query_json: str = "",
) -> str:
    """
    Build a Cognos embed URL for a dashboard/report object.

    Args:
        object_id: Cognos object identifier (for example, dashboard/report id).
        perspective: Cognos perspective, typically 'dashboard' or 'report'.
        hide_navigation: If true, app navigation bars are hidden for widget-style embedding.
        extra_query_json: Optional JSON object for additional query params.

    Returns:
        A fully-qualified embed URL.
    """
    if not object_id.strip():
        raise ValueError("object_id is required.")

    base = _base_url()
    query: Dict[str, Any] = {
        "perspective": perspective,
        "id": object_id,
    }

    if hide_navigation:
        query.update(
            {
                "ui_appbar": "false",
                "ui_navbar": "false",
                "shareLink": "true",
            }
        )

    if extra_query_json.strip():
        extra = json.loads(extra_query_json)
        if not isinstance(extra, dict):
            raise ValueError("extra_query_json must be a JSON object.")
        query.update(extra)

    return f"{base}/bi/?{urlencode(query)}"


@tool
def cognos_render_widget_iframe(
    embed_url: str,
    width: str = "100%",
    height: str = "640",
    title: str = "Cognos Analytics Widget",
) -> str:
    """
    Render embeddable iframe HTML for a Cognos widget URL.

    Args:
        embed_url: Fully-qualified Cognos embed URL.
        width: Iframe width (for example 100% or 1200).
        height: Iframe height in pixels/string.
        title: Iframe title attribute for accessibility.

    Returns:
        HTML iframe string.
    """
    if not embed_url.strip():
        raise ValueError("embed_url is required.")

    return _build_iframe(embed_url=embed_url, width=width, height=height, title=title)


@tool
def cognos_get_widget_bundle(
    object_id: str,
    perspective: str = "dashboard",
    width: str = "100%",
    height: str = "640",
    title: str = "Cognos Analytics Widget",
) -> str:
    """
    Convenience tool: generate both embed URL and iframe HTML for a Cognos object.

    Args:
        object_id: Cognos dashboard/report object id.
        perspective: 'dashboard' or 'report'.
        width: Iframe width.
        height: Iframe height.
        title: Iframe title.

    Returns:
        JSON string containing embed_url and iframe_html.
    """
    embed_url = cognos_generate_embed_url(
        object_id=object_id,
        perspective=perspective,
        hide_navigation=True,
        extra_query_json="",
    )
    iframe_html = cognos_render_widget_iframe(
        embed_url=embed_url,
        width=width,
        height=height,
        title=title,
    )

    bundle = {
        "object_id": object_id,
        "perspective": perspective,
        "embed_url": embed_url,
        "iframe_html": iframe_html,
    }
    return json.dumps(bundle, ensure_ascii=False)
