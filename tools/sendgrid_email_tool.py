"""
sendgrid_email_tool.py
----------------------
Standalone tool to send a file to a user via SendGrid email API.

Supports two input modes:
  1) file_url      -> download file bytes from URL
  2) file_content  -> raw text or base64 content (set is_base64 accordingly)
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import urllib.parse
import urllib.request

from ibm_watsonx_orchestrate.agent_builder.tools import tool
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Attachment,
    ContentId,
    Disposition,
    FileContent,
    FileName,
    FileType,
    Mail,
)


def _sg_env() -> tuple[str, str, str]:
    def _find_connection_value(key: str) -> str:
        # Accept any app-id injected by WXO, e.g.:
        # WXO_CONNECTION_sendgrid_credentials_SENDGRID_API_KEY
        # WXO_CONNECTION_sendgrid_SENDGRID_API_KEY
        pattern = re.compile(rf"^WXO_CONNECTION_[^=]+_{re.escape(key)}$")
        for env_key, env_value in os.environ.items():
            if pattern.match(env_key) and (env_value or "").strip():
                return env_value.strip()
        return ""

    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_sendgrid_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_sendgrid-credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_sendgrid_{key}", "").strip()
            or _find_connection_value(key)
            or os.environ.get(key, "").strip()
            or default
        )

    return (
        g("SENDGRID_API_KEY"),
        g("SENDGRID_FROM_EMAIL"),
        g("SENDGRID_FROM_NAME", "watsonx Orchestrate"),
    )


def _guess_content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _download_file(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "wxo-sendgrid-tool/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
    return body, (ctype or "application/octet-stream")


def _filename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path or ""
    name = os.path.basename(path).strip()
    return name or "attachment.bin"


@tool
def send_file_via_sendgrid(
    to_email: str,
    subject: str,
    body: str,
    filename: str = "",
    file_content: str = "",
    is_base64: bool = False,
    file_url: str = "",
) -> str:
    """
    Send a file to a user via SendGrid email API.

    Provide either:
      - file_url (tool downloads and attaches it), OR
      - file_content (+ filename), where content is plain text or base64.

    Args:
        to_email: Recipient email address.
        subject: Email subject.
        body: Email body text.
        filename: Attachment filename. Required with file_content; optional with file_url.
        file_content: Raw text or base64 string for attachment bytes.
        is_base64: Set True when file_content is base64-encoded.
        file_url: Optional URL to fetch and attach.

    Returns:
        Success/failure status including SendGrid response code.
    """
    to_email = (to_email or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()
    filename = (filename or "").strip()
    file_url = (file_url or "").strip()
    content_raw = (file_content or "").strip()

    if not to_email:
        return "to_email is required."
    if not subject:
        return "subject is required."
    if not body:
        return "body is required."

    api_key, from_email, from_name = _sg_env()
    if not api_key or not from_email:
        return (
            "SendGrid is not configured. Set SENDGRID_API_KEY and SENDGRID_FROM_EMAIL "
            "(or configure WXO connection env vars)."
        )

    if not file_url and not content_raw:
        return "Provide either file_url or file_content."

    try:
        if file_url:
            data, content_type = _download_file(file_url)
            if not filename:
                filename = _filename_from_url(file_url)
        else:
            if not filename:
                return "filename is required when sending file_content."
            if is_base64:
                data = base64.b64decode(content_raw)
            else:
                data = content_raw.replace("\\n", "\n").replace("\\t", "\t").encode("utf-8")
            content_type = _guess_content_type(filename)
    except Exception as e:
        return f"Failed to prepare attachment: {e}"

    attachment_b64 = base64.b64encode(data).decode("utf-8")
    message = Mail(
        from_email=(from_email, from_name),
        to_emails=to_email,
        subject=subject,
        plain_text_content=body,
    )
    message.attachment = Attachment(
        FileContent(attachment_b64),
        FileName(filename or "attachment.bin"),
        FileType(content_type),
        Disposition("attachment"),
        ContentId("file-attachment"),
    )

    try:
        sg = SendGridAPIClient(api_key)
        resp = sg.send(message)
    except Exception as e:
        return f"SendGrid send failed: {e}"

    if 200 <= int(resp.status_code) < 300:
        return (
            f"Email sent successfully via SendGrid.\n"
            f"  To        : {to_email}\n"
            f"  File      : {filename or 'attachment.bin'}\n"
            f"  Size      : {len(data)/1024:.1f} KB\n"
            f"  HTTP code : {resp.status_code}"
        )

    return (
        f"SendGrid returned non-success status. HTTP {resp.status_code}.\n"
        f"Response body: {getattr(resp, 'body', b'')!r}"
    )
