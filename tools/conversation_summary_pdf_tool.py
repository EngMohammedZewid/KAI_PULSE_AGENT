"""
conversation_summary_pdf_tool.py
----------------------------------
Tool that fetches ALL user + assistant messages from the current WXO thread,
sends them to Llama 4 Maverick to generate a structured summary, then builds
a properly-formatted PDF and uploads it to S3.

One-step replacement for the manual sequence:
  get_current_thread_id → capture_thread_payload → summarize → export_summary_pdf
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import io
import os
import re
import struct
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ibm_watsonx_orchestrate.agent_builder.tools import tool


# ---------------------------------------------------------------------------
# WXO credentials & API helpers
# ---------------------------------------------------------------------------

def _get_env(*keys: str) -> Optional[str]:
    for key in keys:
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return None


def _wxo_env() -> tuple[str, str]:
    base_url = (
        _get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
        or "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/16cd2449-1e4f-4fc9-a4d8-9d69c3864a61"
    )
    api_key = (
        _get_env("WXO_API_KEY", "WO_API_KEY")
    )
    return base_url, api_key


def _watsonx_env() -> tuple[str, str, str]:
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_watsonx_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_watsonx-ai_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )
    return (
        g("WATSONX_API_KEY",    ""),
        g("WATSONX_PROJECT_ID", "ab0de2fc-5f68-4e0e-8dc8-d1677b6b689b"),
        g("WATSONX_URL",        "https://us-south.ml.cloud.ibm.com"),
    )


def _normalize_rows(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, list):
        return data
    return []


def _extract_text(content: Any) -> str:
    import json
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return "" if content is None else str(content)


def _fetch_messages(thread_id: str) -> List[Dict[str, Any]]:
    """Fetch all user + assistant messages for the thread via WXO Threads API."""
    from ibm_watsonx_orchestrate.client.client import Client
    from ibm_watsonx_orchestrate.client.credentials import Credentials as WXOCreds
    from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient

    base_url, api_key = _wxo_env()
    creds = WXOCreds(url=base_url, api_key=api_key)
    client = Client(creds)
    tc = ThreadsClient(base_url=base_url, api_key=client.token)

    raw = _normalize_rows(tc.get_thread_messages(thread_id))
    messages: List[Dict[str, Any]] = []
    for msg in raw:
        role = str(msg.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        ts = msg.get("created_at") or msg.get("updated_at") or msg.get("created_on")
        messages.append({
            "role": role,
            "content": _extract_text(msg.get("content")),
            "timestamp": str(ts) if ts else "",
        })
    messages.sort(key=lambda m: m.get("timestamp") or "")
    return messages


def _build_transcript(messages: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for i, msg in enumerate(messages, 1):
        role_label = "User" if msg["role"] == "user" else "Assistant"
        content = (msg["content"] or "").strip()
        if content:
            lines.append(f"[{i}] {role_label}: {content}")
    return "\n\n".join(lines)


def _llm_summarize(transcript: str, language: str, model_id: str) -> str:
    """Send transcript to watsonx.ai LLM and return structured Markdown summary."""
    from ibm_watsonx_ai import Credentials
    from ibm_watsonx_ai.foundation_models import ModelInference

    api_key, project_id, url = _watsonx_env()
    creds = Credentials(url=url, api_key=api_key)
    llm = ModelInference(model_id=model_id, credentials=creds, project_id=project_id)

    lang_line = f"Write the ENTIRE summary in {language} (RTL)." if language.lower() in ("arabic", "ar", "عربي") else ""

    prompt = (
        f"You are a professional conversation analyst. Read the following conversation "
        f"between a user and an AI assistant, then produce a well-structured summary.\n\n"
        f"{lang_line}\n"
        f"Use STRICT Markdown format — follow these rules EXACTLY:\n"
        f"- Use ## for every section heading (always Latin ## symbol, even in Arabic)\n"
        f"- Use - (hyphen space) for every bullet point\n"
        f"- Separate each section with a blank line\n"
        f"- Do NOT include a top-level # heading\n"
        f"- Do NOT use **bold** or *italic* markers\n"
        f"- Do NOT wrap output in code blocks\n\n"
        f"Required sections:\n"
        f"## Overview\n"
        f"Brief description of the conversation topic and context.\n\n"
        f"## Key Topics Discussed\n"
        f"Bullet list of the main subjects covered.\n\n"
        f"## Important Outcomes and Answers\n"
        f"Bullet list of the most important conclusions, answers, or results.\n\n"
        f"## Action Items / Next Steps\n"
        f"Any pending tasks, follow-ups, or open questions. Write 'None' if there are none.\n\n"
        f"Conversation:\n"
        f"{transcript}\n\n"
        f"Begin the summary now:"
    )

    result = llm.generate(prompt=prompt, params={"max_new_tokens": 2000, "temperature": 0.2})
    return (result["results"][0]["generated_text"] or "").strip()


# ---------------------------------------------------------------------------
# Arabic rendering helpers (same pattern as pdf_report_tool.py)
# ---------------------------------------------------------------------------

_AR_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)
_INLINE_MD_RE = re.compile(r"\*{1,2}([^*\n]+)\*{1,2}|_{1,2}([^_\n]+)_{1,2}")
_ARABIC_FONT_CACHE: dict[str, str] = {}
_BLUE  = (0.102, 0.306, 0.533)
_LGREY = (0.600, 0.600, 0.600)
_BLACK = (0.0, 0.0, 0.0)
_WHITE = (1.0, 1.0, 1.0)
_STRIPE = (0.929, 0.949, 0.969)
_BULLET = "\u2022  "


def _has_arabic(text: str) -> bool:
    return bool(_AR_RE.search(text))


def _strip_md(text: str) -> str:
    return _INLINE_MD_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)


def _ar_reshape(text: str) -> str:
    try:
        import arabic_reshaper
        return arabic_reshaper.reshape(text)
    except ImportError:
        return text


def _ar_bidi(text: str) -> str:
    try:
        from bidi.algorithm import get_display
        return get_display(text, base_dir='R')
    except ImportError:
        return text


def _get_amiri(bold: bool = False) -> Optional[str]:
    key = "bold" if bold else "path"
    if key in _ARABIC_FONT_CACHE:
        return _ARABIC_FONT_CACHE[key]
    fname = "amiri_bold.ttf" if bold else "amiri_regular.ttf"
    tmp = os.path.join(tempfile.gettempdir(), fname)
    if os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
        _ARABIC_FONT_CACHE[key] = tmp
        return tmp
    s3_name = "amiri_bold.ttf" if bold else "amiri_regular.ttf"
    variant = "Amiri-Bold" if bold else "Amiri-Regular"
    urls = [
        f"https://aws-buc-for-orchestrate.s3.us-east-2.amazonaws.com/fonts/{s3_name}",
        f"https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/amiri/{variant}.ttf",
        f"https://github.com/google/fonts/raw/main/ofl/amiri/{variant}.ttf",
        f"https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/{variant}.ttf",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "python-pdf/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) > 50_000:
                with open(tmp, "wb") as f:
                    f.write(data)
                _ARABIC_FONT_CACHE[key] = tmp
                return tmp
        except Exception:
            continue
    return None


def _ar_wrap(text: str, font_name: str, font_size: float, avail_w: float) -> List[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth as sw
    words = text.split(" ")
    lines: List[str] = []
    cur = ""
    for w in words:
        if not w:
            continue
        candidate = (cur + " " + w).strip()
        if sw(candidate, font_name, font_size) <= avail_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


# ---------------------------------------------------------------------------
# PDF builder
# ---------------------------------------------------------------------------

class _PDF:
    W, H = 595.28, 841.89
    ML, MR, MT, MB = 56.7, 56.7, 56.7, 70.0

    def __init__(self, title: str) -> None:
        from reportlab.pdfgen import canvas as _c
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        self._buf = io.BytesIO()
        self._c = _c.Canvas(self._buf, pagesize=(self.W, self.H))
        self._uw = self.W - self.ML - self.MR
        self._cy = self.H - self.MT
        self._page = 1
        self._ar_ok = False
        fp = _get_amiri(bold=False)
        if fp:
            try:
                pdfmetrics.registerFont(TTFont("Amiri", fp))
                fpb = _get_amiri(bold=True) or fp
                pdfmetrics.registerFont(TTFont("Amiri-Bold", fpb))
                self._ar_ok = True
            except Exception:
                pass

    def _fn(self, bold=False, arabic=False) -> str:
        if arabic and self._ar_ok:
            return "Amiri-Bold" if bold else "Amiri"
        return "Helvetica-Bold" if bold else "Helvetica"

    def _new_page(self) -> None:
        self._c.setFont("Helvetica", 8)
        self._c.setFillColorRGB(*_LGREY)
        self._c.drawCentredString(self.W / 2, self.MB - 18, str(self._page))
        self._c.showPage()
        self._page += 1
        self._cy = self.H - self.MT

    def _check(self, need: float = 20.0) -> None:
        if self._cy - need < self.MB:
            self._new_page()

    def paragraph(self, text: str, size: float = 10.0, bold: bool = False,
                  color: tuple = _BLACK, indent: float = 0.0,
                  space_after: float = 5.0) -> None:
        from reportlab.lib.utils import simpleSplit
        is_ar = _has_arabic(text)
        fn = self._fn(bold=bold, arabic=is_ar)
        avail_w = self._uw - indent
        lh = size * 1.4
        if is_ar and self._ar_ok:
            shaped = _ar_reshape(text)
            raw = _ar_wrap(shaped, fn, size, avail_w)
            lines = [_ar_bidi(ln) for ln in raw]
        else:
            lines = simpleSplit(text, fn, size, avail_w) or [""]
        for ln in lines:
            self._check(lh)
            self._c.setFont(fn, size)
            self._c.setFillColorRGB(*color)
            y = self._cy - size
            x = self.ML + indent
            if is_ar:
                self._c.drawRightString(x + avail_w, y, ln)
            else:
                self._c.drawString(x, y, ln)
            self._cy -= lh
        self._cy -= space_after

    def bullet_paragraph(self, text: str, size: float = 10.0,
                         space_after: float = 2.0) -> None:
        from reportlab.pdfbase.pdfmetrics import stringWidth as sw
        fn = self._fn(arabic=True)
        bw = sw("\u2022 ", "Helvetica", size) + 4.0
        avail_w = self._uw - bw
        lh = size * 1.4
        right = self.ML + self._uw
        shaped = _ar_reshape(text)
        raw = _ar_wrap(shaped, fn, size, avail_w)
        visual = [_ar_bidi(ln) for ln in raw]
        for i, ln in enumerate(visual):
            self._check(lh)
            y = self._cy - size
            if i == 0:
                self._c.setFont("Helvetica", size)
                self._c.setFillColorRGB(*_BLACK)
                self._c.drawString(right - bw + 2, y, "\u2022 ")
            self._c.setFont(fn, size)
            self._c.setFillColorRGB(*_BLACK)
            self._c.drawRightString(right - bw, y, ln)
            self._cy -= lh
        self._cy -= space_after

    def hline(self) -> None:
        self._check(14)
        self._cy -= 6
        self._c.setStrokeColorRGB(*_BLUE)
        self._c.setLineWidth(0.8)
        self._c.line(self.ML, self._cy, self.W - self.MR, self._cy)
        self._cy -= 6

    def vspace(self, pts: float) -> None:
        self._cy -= pts

    def build(self) -> bytes:
        self._c.save()
        return self._buf.getvalue()


def _build_pdf(title: str, content: str, subtitle: str = "") -> bytes:
    doc = _PDF(title)
    doc.vspace(20)
    doc.paragraph(title, size=22, bold=True, color=_BLUE, space_after=6)
    if subtitle:
        doc.paragraph(subtitle, size=11, color=_LGREY, space_after=4)
    ts = datetime.datetime.utcnow().strftime("%B %d, %Y  %H:%M UTC")
    doc.paragraph(f"Generated: {ts}", size=9, color=_LGREY, space_after=10)
    doc.hline()
    doc.vspace(10)

    lines = content.replace("\\n", "\n").splitlines()
    para_buf: List[str] = []

    def flush() -> None:
        if para_buf:
            doc.paragraph(_strip_md(" ".join(para_buf).strip()))
            para_buf.clear()

    for raw in lines:
        line = raw.rstrip()
        if re.match(r"^## [^#]", line):
            flush()
            doc.vspace(6)
            doc.paragraph(_strip_md(line[3:].strip()), size=13, bold=True,
                          color=_BLUE, space_after=3)
            continue
        if re.match(r"^### ", line):
            flush()
            doc.vspace(4)
            doc.paragraph(_strip_md(line[4:].strip()), size=11, bold=True, space_after=3)
            continue
        if re.fullmatch(r"[-=*]{3,}", line.strip()):
            flush()
            doc.hline()
            continue
        if re.match(r"^\s*[-*+] ", line) or re.match(r"^\s*\d+[.)]\s", line):
            flush()
            text = _strip_md(re.sub(r"^\s*[-*+\d.)]+\s*", "", line).strip())
            if _has_arabic(text):
                doc.bullet_paragraph(text, size=10, space_after=2)
            else:
                doc.paragraph(_BULLET + text, size=10, indent=14, space_after=2)
            continue
        if not line.strip():
            flush()
            doc.vspace(4)
            continue
        para_buf.append(_strip_md(line))

    flush()
    return doc.build()


# ---------------------------------------------------------------------------
# AWS S3 helpers (stdlib-only SigV4)
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, "s3")
    k = _hmac_sha256(k, "aws4_request")
    return k


def _s3_env() -> tuple[str, str, str, str]:
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_aws_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_aws-credentials_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )
    return (
        g("AWS_ACCESS_KEY_ID",     ""),
        g("AWS_SECRET_ACCESS_KEY", ""),
        g("AWS_S3_BUCKET",         "aws-buc-for-orchestrate"),
        g("AWS_S3_REGION",         "us-east-2"),
    )


def _s3_put(s3_key: str, body: bytes, content_type: str,
            ak: str, sk: str, bucket: str, region: str) -> None:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    canonical_uri = "/" + urllib.parse.quote(s3_key.lstrip("/"), safe="/")
    ph = hashlib.sha256(body).hexdigest()
    ch = f"content-type:{content_type}\nhost:{host}\nx-amz-content-sha256:{ph}\nx-amz-date:{amz_date}\n"
    sh = "content-type;host;x-amz-content-sha256;x-amz-date"
    cr = "\n".join(["PUT", canonical_uri, "", ch, sh, ph])
    cs = f"{date_stamp}/{region}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, cs, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, date_stamp, region), sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={ak}/{cs}, SignedHeaders={sh}, Signature={sig}"
    req = urllib.request.Request(
        f"https://{host}{canonical_uri}", method="PUT", data=body,
        headers={"Content-Type": content_type, "x-amz-date": amz_date,
                 "x-amz-content-sha256": ph, "Authorization": auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=120):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"S3 PUT {e.code}: {e.read().decode('utf-8', errors='replace')}") from e


def _s3_presign(s3_key: str, ak: str, sk: str, bucket: str, region: str,
                expires: int = 86400) -> str:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    canonical_uri = "/" + urllib.parse.quote(s3_key.lstrip("/"), safe="/")
    cs = f"{date_stamp}/{region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{ak}/{cs}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    cqs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(params.items())
    )
    cr = "\n".join(["GET", canonical_uri, cqs, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, cs, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, date_stamp, region), sts.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{cqs}&X-Amz-Signature={sig}"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def summarize_conversation_to_pdf(
    thread_id: str,
    title: str = "",
    language: str = "English",
    model: str = "llama4",
    s3_folder: str = "conversation-summaries",
) -> str:
    """
    Summarize the ENTIRE conversation (all user and assistant messages) using
    an LLM, then generate a formatted PDF report and upload it to S3.

    Use this when the user asks to "summarize our conversation", "create a summary
    PDF of what we discussed", or similar requests.

    Workflow:
      1. Fetches ALL messages (user + assistant) from the given thread.
      2. Sends the full transcript to the chosen LLM to produce a structured summary.
      3. Builds a formatted A4 PDF with Arabic support (if language is Arabic).
      4. Uploads the PDF to S3 and returns a 24-hour download link.

    Available models (pass short name as `model`):
      - "llama4"  → Meta Llama 4 Maverick 17B (default, best for Arabic + English)
      - "llama3"  → Meta Llama 3.3 70B Instruct
      - "granite" → IBM Granite 13B Instruct
      - "mistral" → Mistral Large

    Args:
        thread_id:  The current thread / conversation ID. Pass the value returned
                    by get_current_thread_id.
        title:      PDF title. Leave blank to auto-generate.
        language:   Language for the summary — "English" (default) or "Arabic".
        model:      LLM short name: llama4, llama3, granite, mistral.
        s3_folder:  S3 prefix to store the PDF under (default: "conversation-summaries").

    Returns:
        A message with the S3 key and a 24-hour download link to the summary PDF.
    """
    _MODEL_IDS = {
        "llama4":  "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
        "llama3":  "meta-llama/llama-3-3-70b-instruct",
        "granite": "ibm/granite-13b-instruct-v2",
        "mistral": "mistralai/mistral-large",
    }

    thread_id = (thread_id or "").strip()
    if not thread_id:
        return "thread_id is required. Call get_current_thread_id first to obtain it."

    model_id = _MODEL_IDS.get((model or "llama4").strip().lower())
    if not model_id:
        return f"Unknown model '{model}'. Choose from: {', '.join(_MODEL_IDS)}."

    # Step 1 — fetch messages
    try:
        messages = _fetch_messages(thread_id)
    except Exception as e:
        return f"Failed to fetch conversation messages: {e}"

    if not messages:
        return "No user or assistant messages found in this thread."

    transcript = _build_transcript(messages)

    # Step 2 — LLM summarisation
    try:
        summary_md = _llm_summarize(transcript, language, model_id)
    except Exception as e:
        return f"LLM summarisation failed: {e}"

    if not summary_md:
        return "LLM returned an empty summary. Try again or use a different model."

    # Step 3 — derive title
    if not title.strip():
        ts_label = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        title = f"Conversation Summary — {ts_label}"

    subtitle = f"{len(messages)} messages · summarised with {model} · thread {thread_id[:12]}…"

    # Step 4 — build PDF
    try:
        pdf_bytes = _build_pdf(title, summary_md, subtitle)
    except Exception as e:
        return f"PDF generation failed: {e}"

    # Step 5 — upload to S3
    safe_tid = re.sub(r"[^\w-]", "_", thread_id)[:40]
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"summary_{safe_tid}_{ts}.pdf"
    folder = (s3_folder or "").strip("/")
    s3_key = f"{folder}/{filename}" if folder else filename

    try:
        ak, sk, bucket, region = _s3_env()
        _s3_put(s3_key, pdf_bytes, "application/pdf", ak, sk, bucket, region)
        link = _s3_presign(s3_key, ak, sk, bucket, region)
    except RuntimeError as e:
        return f"PDF generated ({len(pdf_bytes):,} bytes) but S3 upload failed: {e}"

    size_kb = len(pdf_bytes) / 1024
    return (
        f"Conversation summary PDF created and uploaded to S3.\n\n"
        f"  Thread   : {thread_id}\n"
        f"  Messages : {len(messages)}\n"
        f"  Language : {language}\n"
        f"  Model    : {model}\n"
        f"  File     : {s3_key}\n"
        f"  Size     : {size_kb:.1f} KB\n\n"
        f"[⬇ Download Summary PDF]({link})\n\n"
        f"_Link expires in 24 hours._"
    )
