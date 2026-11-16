"""
fetch_text_from_url_tool.py
---------------------------
Tool that fetches plain-text / CSV content from a URL and returns it as a string.
Used by the agent to retrieve file content after uploading it to S3, so the
LLM can pass that content to analysis / charting tools.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from tools._tooling import tool


@tool
def fetch_text_from_url(
    url: str,
    max_chars: int = 100000,
) -> str:
    """
    Fetch the text content of a file from a URL (e.g. an S3 presigned download link)
    and return it as a plain-text string.

    Use this after `upload_chat_file_to_s3` to retrieve the uploaded CSV, JSON, or
    TXT content so it can be passed to analysis and charting tools.

    Args:
        url:       The URL to fetch. Typically the presigned download link returned
                   by `upload_chat_file_to_s3`.
        max_chars: Maximum number of characters to return. Defaults to 100 000
                   (covers most CSV files up to ~5 MB). Increase only if needed.

    Returns:
        The text content of the file as a plain string, suitable for passing
        directly to `inspect_csv_content`, `render_chart_from_csv`,
        `summarize_csv_statistics`, or any other tool that accepts csv_content.
        Returns an error message string if the fetch fails.
    """
    url = (url or "").strip()
    if not url:
        return "url is required."

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "watsonx-orchestrate-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()

        # Decode — try UTF-8 then fall back to latin-1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        if len(text) > max_chars:
            text = text[:max_chars]
            text += f"\n\n[... truncated at {max_chars:,} characters]"

        return text

    except urllib.error.HTTPError as e:
        return f"HTTP error {e.code} fetching URL: {e.reason}"
    except urllib.error.URLError as e:
        return f"URL error fetching file: {e.reason}"
    except Exception as e:
        return f"Error fetching file: {e}"
