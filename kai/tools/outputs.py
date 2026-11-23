"""Files the agent makes (reports, exports).

They are saved in the same FileStore as uploads (local folder or S3, under
outputs/), recorded in the thread's `outputs`, and downloaded through the
server at /api/threads/{thread}/files/{id}.{ext}, which checks the API token
and that the thread belongs to the caller. No public or presigned link is
handed out, so the file is only reachable from the signed-in chat.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from kai.files import store_from_config
from kai.state import FileRef
from kai.tools._common import reply

MAX_NAME = 60


def file_name(title: str, ext: str, fallback: str = "report") -> str:
    """A readable file name from a title: letters (Arabic too), digits, _ and -."""
    slug = re.sub(r"[^\w\s-]", "", title or "")
    slug = re.sub(r"[\s-]+", "_", slug).strip("_")[:MAX_NAME].strip("_")
    return f"{slug or fallback}.{ext}"


def download_path(thread_id: str, ref: FileRef) -> str:
    return f"/api/threads/{thread_id}/files/{ref['id']}{Path(ref['name']).suffix.lower()}"


def _size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def save_output(config: Optional[dict], name: str, data: bytes) -> tuple[FileRef, str]:
    """Store a generated file; returns its FileRef and the chat download path."""
    thread_id = ((config or {}).get("configurable") or {}).get("thread_id") or ""
    ref = store_from_config(config).put(name, data, prefix="outputs")
    return ref, download_path(thread_id, ref)


def output_reply(tool_call_id: str, ref: FileRef, link: str, what: str, **update):
    """Answer the tool call with the download link; the chat draws a download card from
    the artifact, so the file is offered even if the model leaves the link out."""
    fmt = Path(ref["name"]).suffix.lstrip(".").upper()
    text = (f"{what} is ready: [{ref['name']}]({link}) ({_size(ref['size'])}).\n"
            "Give the user this link exactly as written. The chat shows a Download button "
            "for it; the file opens only for the signed-in user of this conversation.")
    download = {"id": ref["id"], "name": ref["name"], "format": fmt, "size": ref["size"], "url": link}
    return reply(tool_call_id, text, artifact={"download": download}, outputs=[ref], **update)
