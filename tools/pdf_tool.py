"""pdf_tool.py
-----------
Unified LLM-powered PDF generation tool for watsonx Orchestrate.

Two modes in a single tool (create_pdf_report):
  1. TOPIC REPORT      — provide topic     → LLM writes a full report → PDF → S3
  2. CONVERSATION SUMMARY — provide thread_id → fetch messages → LLM summarizes → PDF → S3

Language is detected automatically from the `language` parameter (English / Arabic).
"""

from __future__ import annotations

import datetime
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import smtplib
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from tools._safe_fetch import fetch as safe_fetch
from tools._settings import require_bucket
from tools._tooling import tool

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Colours & constants
# ---------------------------------------------------------------------------
_BLUE   = (0.102, 0.306, 0.533)
_LGREY  = (0.600, 0.600, 0.600)
_BLACK  = (0.0, 0.0, 0.0)
_WHITE  = (1.0, 1.0, 1.0)
_STRIPE = (0.929, 0.949, 0.969)
_BULLET = "\u2022  "

_AR_RE        = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_INLINE_MD_RE = re.compile(r"\*{1,2}([^*\n]+)\*{1,2}|_{1,2}([^_\n]+)_{1,2}")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
_INLINE_RUN_RE = re.compile(r"\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|__([^_\n]+)__|_([^_\n]+)_")


def _inline_to_xml(text: str) -> str:
    """Convert markdown inline bold/italic to ReportLab XML tags, escaping XML special chars."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__([^_\n]+)__",     r"<b>\1</b>", text)
    text = re.sub(r"\*([^*\n]+)\*",     r"<i>\1</i>", text)
    text = re.sub(r"_([^_\n]+)_",       r"<i>\1</i>", text)
    return text


def _parse_md_table(table_lines: List[str]) -> List[List[str]]:
    """Parse markdown table lines into rows of cells, skipping separator rows."""
    rows: List[List[str]] = []
    for line in table_lines:
        if _TABLE_SEP_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if any(cells):
            rows.append(cells)
    return rows


def _add_runs(para: Any, text: str) -> None:
    """Add runs to a python-docx paragraph, honouring **bold** and *italic* markdown."""
    last = 0
    for m in _INLINE_RUN_RE.finditer(text):
        if m.start() > last:
            para.add_run(text[last:m.start()])
        full = m.group(0)
        inner = m.group(1) or m.group(2) or m.group(3) or m.group(4) or ""
        run = para.add_run(inner)
        if full.startswith("**") or full.startswith("__"):
            run.bold = True
        else:
            run.italic = True
        last = m.end()
    if last < len(text):
        para.add_run(text[last:])

_LLM_MODELS: Dict[str, str] = {
    "llama4":   "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
    "llama3":   "meta-llama/llama-3-3-70b-instruct",
    "granite":  "ibm/granite-13b-instruct-v2",
    "mistral":  "mistralai/mistral-large",
    "deepseek": "deepseek-chat",          # DeepSeek-V3 (best for Arabic)
    "deepseek-r1": "deepseek-reasoner",   # DeepSeek-R1 (chain-of-thought)
}


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _get_env(*keys: str) -> Optional[str]:
    for key in keys:
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return None


def _s3_env() -> tuple:
    def g(k: str, d: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_aws_credentials_{k}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_aws-credentials_{k}", "").strip()
            or os.environ.get(k, "").strip()
            or d
        )
    return (
        g("AWS_ACCESS_KEY_ID",     ""),
        g("AWS_SECRET_ACCESS_KEY", ""),
        require_bucket(g("AWS_S3_BUCKET")),
        g("AWS_S3_REGION",         "us-east-2"),
    )


def _watsonx_env() -> tuple:
    def g(k: str, d: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_watsonx_{k}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_watsonx-ai_{k}", "").strip()
            or os.environ.get(k, "").strip()
            or d
        )
    return (
        g("WATSONX_API_KEY",    ""),
        g("WATSONX_PROJECT_ID", "ab0de2fc-5f68-4e0e-8dc8-d1677b6b689b"),
        g("WATSONX_URL",        "https://us-south.ml.cloud.ibm.com"),
    )


def _deepseek_api_key() -> str:
    return (
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        or os.environ.get("WXO_CONNECTION_deepseek_DEEPSEEK_API_KEY", "").strip()
    )


def _wxo_api_env() -> tuple:
    base_url = (
        _get_env("WXO_API_ENDPOINT", "WO_INSTANCE")
        or "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/16cd2449-1e4f-4fc9-a4d8-9d69c3864a61"
    )
    api_key = (
        _get_env("WXO_API_KEY", "WO_API_KEY")
    )
    return base_url, api_key


def _smtp_bool(key: str, default: bool = False) -> bool:
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _smtp_env() -> dict[str, Any]:
    """Read SMTP settings from the environment."""
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_smtp_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_smtp-credentials_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )

    host = g("SMTP_HOST")
    port_raw = g("SMTP_PORT")
    username = g("SMTP_USERNAME")
    password = g("SMTP_PASSWORD")
    from_email = g("SMTP_FROM_EMAIL") or username
    from_name = g("SMTP_FROM_NAME")

    port = 0
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            port = 0

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from_email": from_email,
        "from_name": from_name,
        "use_tls": _smtp_bool("SMTP_USE_TLS", True),
        "use_ssl": _smtp_bool("SMTP_USE_SSL", False),
        "timeout": int(g("SMTP_TIMEOUT", "30") or "30"),
    }


def _sendgrid_env() -> tuple[str, str, str]:
    def find_connection_value(key: str) -> str:
        pattern = re.compile(rf"^WXO_CONNECTION_[^=]+_{re.escape(key)}$")
        for env_key, env_val in os.environ.items():
            if pattern.match(env_key) and (env_val or "").strip():
                return env_val.strip()
        return ""

    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_sendgrid_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_sendgrid-credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_sendgrid_{key}", "").strip()
            or find_connection_value(key)
            or os.environ.get(key, "").strip()
            or default
        )

    return (
        g("SENDGRID_API_KEY"),
        g("SENDGRID_FROM_EMAIL"),
        g("SENDGRID_FROM_NAME", "KAI Pulse"),
    )


def _send_email_via_sendgrid(
    recipient: str,
    subject: str,
    body: str,
    filename: str,
    attachment: bytes,
    content_type: str,
) -> str:
    api_key, from_email, from_name = _sendgrid_env()
    if not api_key or not from_email:
        return ""

    payload = {
        "personalizations": [{"to": [{"email": recipient}], "subject": subject}],
        "from": {"email": from_email, "name": from_name},
        "content": [{"type": "text/plain", "value": body}],
        "attachments": [{
            "content": base64.b64encode(attachment).decode("utf-8"),
            "type": content_type,
            "filename": filename,
            "disposition": "attachment",
        }],
    }

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = getattr(resp, "status", 0) or resp.getcode()
        if 200 <= int(status) < 300:
            return f"Email sent successfully to {recipient} via SendGrid."
        return f"SendGrid returned non-success status: {status}."
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return f"SendGrid send failed (HTTP {e.code}): {err}"
    except Exception as e:
        return f"SendGrid send failed: {e}"


def _send_email_attachment(
    recipient: str,
    subject: str,
    body: str,
    filename: str,
    attachment: bytes,
    content_type: str,
) -> str:
    """Send an email with a single attachment using SMTP settings from the env."""
    recipient = (recipient or "").strip()
    subject = (subject or "").strip() or "Your generated file"
    body = (body or "").strip() or "Please find the generated file attached."

    if not recipient:
        return "email_to is required for email delivery."

    cfg = _smtp_env()
    if not cfg["host"]:
        sg_result = _send_email_via_sendgrid(
            recipient=recipient,
            subject=subject,
            body=body,
            filename=filename,
            attachment=attachment,
            content_type=content_type,
        )
        if sg_result:
            return sg_result
        return (
            "SMTP is not configured and SendGrid is not configured. "
            "Set SMTP_* or SENDGRID_* credentials in the environment."
        )

    msg = EmailMessage()
    sender = cfg["from_email"] or cfg["username"]
    if cfg["from_name"] and sender:
        msg["From"] = f"{cfg['from_name']} <{sender}>"
    else:
        msg["From"] = sender or cfg["username"]
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    maintype, subtype = (content_type.split("/", 1) + ["octet-stream"])[:2]
    msg.add_attachment(attachment, maintype=maintype, subtype=subtype, filename=filename)

    try:
        if cfg["use_ssl"]:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"] or 465, timeout=cfg["timeout"])
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"] or 587, timeout=cfg["timeout"])
        with server:
            server.ehlo()
            if cfg["use_tls"] and not cfg["use_ssl"]:
                server.starttls()
                server.ehlo()
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
    except Exception as e:
        sg_result = _send_email_via_sendgrid(
            recipient=recipient,
            subject=subject,
            body=body,
            filename=filename,
            attachment=attachment,
            content_type=content_type,
        )
        if sg_result:
            return f"SMTP send failed ({e}); fallback {sg_result}"
        return f"Email delivery failed: {e}"

    return f"Email sent successfully to {recipient}."


# ---------------------------------------------------------------------------
# Arabic helpers
# ---------------------------------------------------------------------------

_ARABIC_FONT_CACHE: Dict[str, str] = {}


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
        return get_display(text, base_dir="R")
    except ImportError:
        return text


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


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _font_dirs() -> List[str]:
    """Where the shipped Amiri files are looked for, in order: KAI_FONT_DIR, then the
    project root (the repo checkout, or /app in the Docker image)."""
    dirs = [os.environ.get("KAI_FONT_DIR", "").strip(), _PROJECT_ROOT]
    return [d for d in dirs if d]


def _get_amiri(bold: bool = False) -> Optional[str]:
    """Path to the Amiri TTF used for Arabic. The files shipped with the app come
    first, so Arabic PDFs need no internet; a download is only the last resort."""
    key = "bold" if bold else "path"
    if key in _ARABIC_FONT_CACHE:
        return _ARABIC_FONT_CACHE[key]
    fname = "amiri_bold.ttf" if bold else "amiri_regular.ttf"
    for folder in _font_dirs():
        local = os.path.join(folder, fname)
        if os.path.isfile(local) and os.path.getsize(local) > 50_000:
            _ARABIC_FONT_CACHE[key] = local
            return local
    tmp = os.path.join(tempfile.gettempdir(), fname)
    if os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
        _ARABIC_FONT_CACHE[key] = tmp
        return tmp
    _log.warning("Amiri font %s not found in %s; downloading it (set KAI_FONT_DIR to avoid this)",
                 fname, " or ".join(_font_dirs()))
    variant = "Amiri-Bold" if bold else "Amiri-Regular"
    urls = [  # Google Fonts only (the old Orchestrate bucket is no longer a source)
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
    _log.error("Amiri font %s unavailable: Arabic text in PDFs will not render correctly", fname)
    return None


# ---------------------------------------------------------------------------
# PDF builder (reportlab)
# ---------------------------------------------------------------------------

class _PDF:
    W, H = 595.28, 841.89
    ML, MR, MT, MB = 56.7, 56.7, 56.7, 70.0

    def __init__(self) -> None:
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

    def _fn(self, bold: bool = False, arabic: bool = False) -> str:
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
        if is_ar:  # this canvas path draws plain text: drop **bold** / *italic* markers
            text = _strip_md(text)
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

    def image(self, url: str, caption: str = "") -> None:
        """Download url and embed the image, scaled to fit page width.

        Only from trusted chart/storage hosts (see tools/_safe_fetch.py): the
        URL comes from model-written content."""
        try:
            img_bytes, _ = safe_fetch(url, trusted_only=True, timeout=30)
        except Exception as exc:
            self.paragraph(f"[Chart could not be loaded: {str(exc)[:80]}]",
                           size=8.5, color=(0.5, 0.3, 0.0))
            return
        try:
            from reportlab.lib.utils import ImageReader
            reader = ImageReader(io.BytesIO(img_bytes))
            iw, ih = reader.getSize()
            max_w = self._uw * 0.60          # 60 % of usable page width
            max_h = 160.0                    # max height in points
            scale = min(max_w / max(iw, 1), max_h / max(ih, 1), 1.0)
            dw, dh = iw * scale, ih * scale
            self._check(dh + 20)
            x = self.ML + (self._uw - dw) / 2.0
            y = self._cy - dh
            self._c.drawImage(reader, x, y, width=dw, height=dh,
                              preserveAspectRatio=True, mask="auto")
            self._cy -= dh + 4
        except Exception as exc:
            self.paragraph(f"[Image render error: {str(exc)[:80]}]", size=8.5)
            return
        if caption:
            self.paragraph(caption, size=9, color=_LGREY, space_after=10)
        else:
            self._cy -= 8

    def vspace(self, pts: float) -> None:
        self._cy -= pts

    def rich_paragraph(self, text: str, size: float = 10.0, bold: bool = False,
                       color: tuple = _BLACK, indent: float = 0.0,
                       space_after: float = 5.0) -> None:
        """Render a paragraph with inline **bold** / *italic* via ReportLab Paragraph flowable."""
        if _has_arabic(text):
            # Fall back to existing canvas-based rendering for Arabic
            self.paragraph(text, size=size, bold=bold, color=color,
                           indent=indent, space_after=space_after)
            return
        from reportlab.platypus import Paragraph as _RLPara
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.colors import Color as _RLColor
        xml_text = _inline_to_xml(text)
        style = ParagraphStyle(
            "rp",
            fontName="Helvetica-Bold" if bold else "Helvetica",
            fontSize=size,
            leading=size * 1.4,
            textColor=_RLColor(*color),
            leftIndent=indent,
        )
        p = _RLPara(xml_text, style)
        avail_w = self._uw - indent
        _, h = p.wrapOn(self._c, avail_w, self.H)
        self._check(h + space_after)
        p.drawOn(self._c, self.ML + indent, self._cy - h)
        self._cy -= h + space_after

    def table(self, rows: List[List[str]], space_after: float = 10.0) -> None:
        """Render a parsed markdown table using ReportLab Table flowable.

        Arabic cells get the Amiri font, shaping, right alignment and wrapping
        (Helvetica has no Arabic glyphs). A table whose header is mostly Arabic
        is laid out right to left. **x** markers are removed; a cell that is
        entirely **bold** is drawn in bold.
        """
        if not rows:
            return
        from reportlab.platypus import Table as _RLTable, TableStyle as _RLTableStyle
        from reportlab.lib.colors import HexColor, white
        col_count = max(len(r) for r in rows)
        norm = [r + [""] * (col_count - len(r)) for r in rows]
        header = [c for c in norm[0] if c.strip()]
        rtl = bool(header) and sum(_has_arabic(c) for c in header) * 2 >= len(header)
        if rtl:
            norm = [list(reversed(r)) for r in norm]
        col_w = self._uw / col_count
        size, pad = 9, 6
        cell_cmds: List[tuple] = [("ALIGN", (0, 0), (-1, -1), "RIGHT")] if rtl else []
        cells: List[List[str]] = []
        for i, row in enumerate(norm):
            out = []
            for j, raw in enumerate(row):
                m = re.fullmatch(r"\s*\*\*(.+?)\*\*\s*", raw)
                bold = i == 0 or bool(m)
                text = _strip_md(m.group(1) if m else raw).strip()
                if _has_arabic(text) and self._ar_ok:
                    fn = self._fn(bold=bold, arabic=True)
                    lines = _ar_wrap(_ar_reshape(text), fn, size, col_w - 2 * pad)
                    text = "\n".join(_ar_bidi(ln) for ln in lines)
                    cell_cmds += [("FONTNAME", (j, i), (j, i), fn), ("ALIGN", (j, i), (j, i), "RIGHT")]
                elif bold and i > 0:
                    cell_cmds.append(("FONTNAME", (j, i), (j, i), "Helvetica-Bold"))
                out.append(text)
            cells.append(out)
        norm = cells
        style_cmds = [
            ("BACKGROUND",    (0, 0), (-1, 0),  HexColor("#1A4E88")),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  white),
            ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 9),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [HexColor("#FFFFFF"), HexColor("#EDF2F7")]),
            ("GRID",          (0, 0), (-1, -1), 0.4, HexColor("#AAAAAA")),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ] + cell_cmds  # per-cell commands last, so they override the defaults
        t = _RLTable(norm, colWidths=[col_w] * col_count)
        t.setStyle(_RLTableStyle(style_cmds))
        _, h = t.wrapOn(self._c, self._uw, self.H)
        self._check(h + space_after)
        t.drawOn(self._c, self.ML, self._cy - h)
        self._cy -= h + space_after

    def build(self) -> bytes:
        self._c.setFont("Helvetica", 8)
        self._c.setFillColorRGB(*_LGREY)
        self._c.drawCentredString(self.W / 2, self.MB - 18, str(self._page))
        self._c.save()
        return self._buf.getvalue()


# Regex to find QuickChart URLs anywhere in text (including inside HTML/Markdown)
_CHART_URL_RE = re.compile(
    r'https://quickchart\.io/[^\s"<>)]+',
    re.IGNORECASE,
)


def _extract_chart_urls(messages: List[Dict[str, Any]]) -> List[str]:
    """Return all unique QuickChart URLs found in assistant messages, in order."""
    seen: set = set()
    urls: List[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for url in _CHART_URL_RE.findall(msg.get("content") or ""):
            url = url.rstrip(").,")  # strip accidental trailing punctuation
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _build_pdf(title: str, content: str, subtitle: str = "",
               chart_urls: Optional[List[str]] = None) -> bytes:
    doc = _PDF()
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
    table_buf: List[str] = []

    def flush_table() -> None:
        if table_buf:
            rows = _parse_md_table(list(table_buf))
            if rows:
                doc.table(rows)
            table_buf.clear()

    def flush() -> None:
        if para_buf:
            doc.rich_paragraph(" ".join(para_buf).strip())
            para_buf.clear()

    for raw in lines:
        line = raw.rstrip()

        # Markdown table row
        if line.lstrip().startswith("|"):
            flush()
            table_buf.append(line)
            continue

        # Non-table line — flush any pending table
        flush_table()

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
            text = re.sub(r"^\s*[-*+\d.)]+\s*", "", line).strip()
            if _has_arabic(text):
                doc.bullet_paragraph(_strip_md(text), size=10, space_after=2)
            else:
                doc.rich_paragraph(_BULLET + text, size=10, indent=14, space_after=2)
            continue
        # Markdown image: ![alt](url)
        img_m = re.match(r"!\[([^\]]*)\]\((https?://\S+)\)", line.strip())
        if img_m:
            flush()
            doc.image(img_m.group(2), caption=img_m.group(1))
            continue
        if not line.strip():
            flush()
            doc.vspace(4)
            continue
        para_buf.append(line)

    flush_table()
    flush()

    # Append any chart images extracted from the conversation
    if chart_urls:
        doc.vspace(10)
        doc.hline()
        doc.paragraph("Charts from Conversation", size=13, bold=True,
                      color=_BLUE, space_after=6)
        for i, url in enumerate(chart_urls, 1):
            doc.image(url, caption=f"Chart {i}")
            doc.vspace(6)

    return doc.build()


# ---------------------------------------------------------------------------
# AWS S3 helpers (stdlib-only SigV4)
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, "s3")
    k = _hmac_sha256(k, "aws4_request")
    return k


def _s3_put(s3_key: str, body: bytes, content_type: str,
            ak: str, sk: str, bucket: str, region: str) -> None:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    ad = t.strftime("%Y%m%dT%H%M%SZ")
    ds = t.strftime("%Y%m%d")
    uri = "/" + urllib.parse.quote(s3_key.lstrip("/"), safe="/")
    ph = hashlib.sha256(body).hexdigest()
    ch = f"content-type:{content_type}\nhost:{host}\nx-amz-content-sha256:{ph}\nx-amz-date:{ad}\n"
    sh = "content-type;host;x-amz-content-sha256;x-amz-date"
    cr = "\n".join(["PUT", uri, "", ch, sh, ph])
    cs = f"{ds}/{region}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", ad, cs, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, ds, region), sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={ak}/{cs}, SignedHeaders={sh}, Signature={sig}"
    req = urllib.request.Request(
        f"https://{host}{uri}", method="PUT", data=body,
        headers={"Content-Type": content_type, "x-amz-date": ad,
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
    ad = t.strftime("%Y%m%dT%H%M%SZ")
    ds = t.strftime("%Y%m%d")
    uri = "/" + urllib.parse.quote(s3_key.lstrip("/"), safe="/")
    cs = f"{ds}/{region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{ak}/{cs}",
        "X-Amz-Date": ad,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    cqs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(params.items())
    )
    cr = "\n".join(["GET", uri, cqs, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join(["AWS4-HMAC-SHA256", ad, cs, hashlib.sha256(cr.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, ds, region), sts.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{uri}?{cqs}&X-Amz-Signature={sig}"


# ---------------------------------------------------------------------------
# WXO thread helpers
# ---------------------------------------------------------------------------

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
    from ibm_watsonx_orchestrate.client.client import Client
    from ibm_watsonx_orchestrate.client.credentials import Credentials as WXOCreds
    from ibm_watsonx_orchestrate_clients.threads.threads_client import ThreadsClient

    base_url, api_key = _wxo_api_env()
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
        label = "User" if msg["role"] == "user" else "Assistant"
        content = (msg["content"] or "").strip()
        if content:
            lines.append(f"[{i}] {label}: {content}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _deepseek_call(prompt: str, model_id: str, max_tokens: int = 3000) -> str:
    """Call DeepSeek API (OpenAI-compatible) via stdlib urllib."""
    api_key = _deepseek_api_key()
    if not api_key:
        raise ValueError(
            "DEEPSEEK_API_KEY is not set. "
            "Add it as an environment variable or app_connection."
        )
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return (data["choices"][0]["message"]["content"] or "").strip()


def _llm_call(prompt: str, model_id: str, max_tokens: int = 3000) -> str:
    # Route DeepSeek models to DeepSeek API
    if model_id.startswith("deepseek"):
        return _deepseek_call(prompt, model_id, max_tokens)

    # Default: IBM watsonx.ai
    from ibm_watsonx_ai import Credentials
    from ibm_watsonx_ai.foundation_models import ModelInference

    api_key, project_id, url = _watsonx_env()
    creds = Credentials(url=url, api_key=api_key)
    llm = ModelInference(model_id=model_id, credentials=creds, project_id=project_id)
    result = llm.generate(prompt=prompt, params={"max_new_tokens": max_tokens, "temperature": 0.3})
    return (result["results"][0]["generated_text"] or "").strip()


def _is_arabic_lang(language: str) -> bool:
    return language.strip().lower() in ("arabic", "ar", "عربي", "عربية")


def _summary_prompt(transcript: str, language: str) -> str:
    lang_line = "Write the ENTIRE report in Arabic." if _is_arabic_lang(language) else ""
    return (
        f"You are a professional conversation analyst. Read the conversation below "
        f"and produce a comprehensive, well-structured summary report.\n\n"
        f"{lang_line}\n"
        f"Format using STRICT Markdown rules:\n"
        f"- Use ## for every section heading (always Latin ## symbol, even in Arabic)\n"
        f"- Use - (hyphen space) for every bullet point\n"
        f"- Separate sections with a blank line\n"
        f"- Do NOT include a top-level # heading\n"
        f"- Do NOT use **bold** or *italic* markers\n"
        f"- Do NOT wrap output in code blocks\n\n"
        f"Required sections:\n"
        f"## Overview\n"
        f"## Key Topics Discussed\n"
        f"## Important Outcomes and Answers\n"
        f"## Action Items / Next Steps\n\n"
        f"Conversation:\n{transcript}\n\n"
        f"Begin the summary now:"
    )


def _report_prompt(topic: str, language: str, instructions: str) -> str:
    lang_line = "Write the ENTIRE report in Arabic." if _is_arabic_lang(language) else ""
    extra = f"\nAdditional instructions: {instructions}" if instructions.strip() else ""
    return (
        f"You are a professional report writer. Write a comprehensive, well-structured "
        f"report about the following topic.\n\n"
        f"Topic: {topic}{extra}\n\n"
        f"{lang_line}\n"
        f"Format using STRICT Markdown rules:\n"
        f"- Use ## for every section heading (always Latin ## symbol, even in Arabic)\n"
        f"- Use ### for sub-section headings\n"
        f"- Use - (hyphen space) for every bullet point\n"
        f"- Separate every paragraph with a blank line\n"
        f"- Do NOT include a top-level # heading\n"
        f"- Do NOT use **bold** or *italic* markers\n"
        f"- Do NOT wrap in a code block\n\n"
        f"Begin the report now:"
    )


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def create_pdf_report(
    topic: str = "",
    thread_id: str = "",
    title: str = "",
    language: str = "English",
    model: str = "deepseek",
    instructions: str = "",
    s3_folder: str = "reports",
    content: str = "",
    email_to: str = "",
    email_subject: str = "",
    email_body: str = "",
) -> str:
    """
    Create a professional PDF report and upload it to S3.
    Returns a 24-hour presigned download link.

    If `content` is provided, it is used directly (no LLM call is made).
    Otherwise, an LLM generates the content based on topic or thread_id.

    TWO MODES — provide EITHER topic OR thread_id:

    MODE 1 — TOPIC REPORT (provide topic, leave thread_id blank):
        The LLM writes a full, structured report about the given topic.
        Example: topic="Q1 2025 MENA region sales performance"

    MODE 2 — CONVERSATION SUMMARY (provide thread_id, leave topic blank):
        Fetches ALL messages from the conversation thread, then the LLM
        summarizes them into a structured PDF report.
        Call get_current_thread_id first to obtain the thread_id.

    LANGUAGE:
        Pass language="Arabic" for a fully Arabic report with RTL layout.
        Default is "English". Use ONLY the user's own writing language to decide —
        not the assistant's replies.

    MODELS (model parameter):
        - "llama4"  → Meta Llama 4 Maverick 17B  (default, best for Arabic + English)
        - "llama3"  → Meta Llama 3.3 70B Instruct
        - "granite" → IBM Granite 13B Instruct
        - "mistral"      → Mistral Large
        - "deepseek"     → DeepSeek-V3 (excellent Arabic, requires DEEPSEEK_API_KEY)
        - "deepseek-r1"  → DeepSeek-R1 chain-of-thought (requires DEEPSEEK_API_KEY)

    Args:
        topic:        Report topic. Leave blank when summarizing a conversation.
        thread_id:    Conversation thread ID (from get_current_thread_id).
                      Leave blank when writing a topic report.
        title:        PDF title shown at the top. Auto-generated if blank.
        language:     Output language — "English" (default) or "Arabic".
        model:        LLM short name: deepseek (default), llama4, llama3, granite, mistral.
        instructions: Extra guidance for the LLM (tone, sections to include, etc.).
        s3_folder:    S3 prefix for the uploaded PDF (default: "reports").
        content:      Pre-generated report text. If provided, skips the LLM call entirely.
                      The agent can write the content itself and pass it here.
        email_to:     Optional recipient email address. If provided, the PDF is
                  attached and emailed after generation.
        email_subject: Optional email subject line.
        email_body:   Optional email body text.

    Returns:
        A message with the S3 key and a 24-hour presigned download link.
    """
    topic     = (topic or "").strip()
    thread_id = (thread_id or "").strip()
    content   = (content or "").strip()

    if not topic and not thread_id and not content:
        return (
            "Please provide either:\n"
            "  - topic: the subject you want a report on, OR\n"
            "  - thread_id: to summarize the current conversation (use get_current_thread_id), OR\n"
            "  - content: pre-written report text."
        )

    msg_count = 0
    chart_urls = None

    if thread_id:
        # Fetch messages for chart extraction (and transcript if no content provided)
        try:
            messages = _fetch_messages(thread_id)
        except Exception as e:
            return f"Failed to fetch thread messages: {e}"
        if not messages and not content:
            return "No messages found in this thread."
        msg_count = len(messages)
        chart_urls = _extract_chart_urls(messages)
        mode = "summary"
        default_title = f"Conversation Summary — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        subtitle = f"{msg_count} messages  ·  thread {thread_id[:12]}…"
    else:
        mode = "report"
        default_title = topic[:80] if topic else title[:80]
        subtitle = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # -------------------------------------------------------------------------
    # Generate content via LLM only if not provided by the agent
    # -------------------------------------------------------------------------
    if not content:
        model_id = _LLM_MODELS.get((model or "deepseek").strip().lower())
        if not model_id:
            return f"Unknown model '{model}'. Choose from: {', '.join(_LLM_MODELS)}."
        if thread_id:
            transcript = _build_transcript(messages)
            prompt = _summary_prompt(transcript, language)
        else:
            prompt = _report_prompt(topic, language, instructions)
        try:
            content = _llm_call(prompt, model_id)
        except Exception as e:
            return f"LLM generation failed: {e}"
        if not content:
            return "LLM returned empty content. Try a different model."
        subtitle += f"  ·  model: {model}"

    # -------------------------------------------------------------------------
    # Derive title
    # -------------------------------------------------------------------------
    if not title.strip():
        title = default_title
    if not title:
        title = default_title

    # -------------------------------------------------------------------------
    # Build PDF
    # -------------------------------------------------------------------------
    try:
        pdf_bytes = _build_pdf(title, content, subtitle, chart_urls=chart_urls)
    except Exception as e:
        return f"PDF generation failed: {e}"

    # -------------------------------------------------------------------------
    # Upload to S3
    # -------------------------------------------------------------------------
    safe = re.sub(r"[^\w\s-]", "", title.lower())
    safe = re.sub(r"[\s-]+", "_", safe).strip("_")[:50]
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{mode}_{safe}_{ts}.pdf"
    folder = (s3_folder or "").strip("/")
    s3_key = f"{folder}/{filename}" if folder else filename

    try:
        ak, sk, bucket, region = _s3_env()
        _s3_put(s3_key, pdf_bytes, "application/pdf", ak, sk, bucket, region)
        link = _s3_presign(s3_key, ak, sk, bucket, region)
    except RuntimeError as e:
        return f"PDF generated ({len(pdf_bytes):,} bytes) but S3 upload failed: {e}"

    email_status = ""
    if (email_to or "").strip():
        subject = (email_subject or "").strip() or f"{title or 'PDF Report'}"
        body = (email_body or "").strip() or (
            f"Attached is your generated PDF report: {title or 'Report'}\n\n"
            f"Download link: {link}"
        )
        email_status = _send_email_attachment(
            recipient=email_to,
            subject=subject,
            body=body,
            filename=filename,
            attachment=pdf_bytes,
            content_type="application/pdf",
        )

    size_kb = len(pdf_bytes) / 1024

    if mode == "summary":
        result = (
            f"Conversation summary PDF created and uploaded to S3.\n\n"
            f"  Thread   : {thread_id}\n"
            f"  Messages : {msg_count}\n"
            f"  Language : {language}\n"
            f"  Model    : {model}\n"
            f"  File     : {s3_key}\n"
            f"  Size     : {size_kb:.1f} KB\n\n"
            f"[⬇ Download Summary PDF]({link})\n\n"
            f"_Link expires in 24 hours._"
        )
    else:
        result = (
            f"PDF report generated and uploaded to S3.\n\n"
            f"  Topic    : {topic[:60]}\n"
            f"  Language : {language}\n"
            f"  Model    : {model}\n"
            f"  File     : {s3_key}\n"
            f"  Size     : {size_kb:.1f} KB\n\n"
            f"[⬇ Download PDF Report]({link})\n\n"
            f"_Link expires in 24 hours._"
        )

    if email_status:
        result += f"\n\n{email_status}"
    return result


# ---------------------------------------------------------------------------
# Word (.docx) builder
# ---------------------------------------------------------------------------

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://\S+)\)")


def _download_image(url: str) -> bytes:
    """Fetch an image for embedding: trusted chart/storage hosts only, like the
    PDF builder (the URL comes from model-written content)."""
    body, _ = safe_fetch(url, trusted_only=True, timeout=30)
    return body


def _build_docx(title: str, content: str, subtitle: str = "",
                is_arabic: bool = False) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    # Same proportions as the PDF: at most 60% of the text width, at most ~160pt tall.
    img_max_w = Inches(6.5 * 0.60)
    img_max_h = Pt(160)

    def _add_image(url: str, caption: str) -> None:
        p = None
        try:
            data = _download_image(url)
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            shape = p.add_run().add_picture(io.BytesIO(data), width=img_max_w)
            if shape.height > img_max_h:  # keep tall images from filling the page
                scale = img_max_h / shape.height
                shape.height = int(shape.height * scale)
                shape.width = int(shape.width * scale)
        except Exception as exc:  # download failed or not an image Word can embed
            if p is not None:     # drop the half-built picture paragraph
                p._p.getparent().remove(p._p)
            note = doc.add_paragraph(f"[Chart could not be loaded: {str(exc)[:80]}]")
            for run in note.runs:
                run.font.size = Pt(8.5)
                run.font.color.rgb = RGBColor(0x80, 0x4D, 0x00)
            return
        if caption.strip():
            cap = doc.add_paragraph(caption.strip())
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in cap.runs:
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    def _set_rtl(para):
        pPr = para._p.get_or_add_pPr()
        bidi = OxmlElement("w:bidi")
        pPr.insert(0, bidi)
        para.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    def _strip(text: str) -> str:
        return _INLINE_MD_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)

    doc = Document()
    # Page margins
    for section in doc.sections:
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)

    # Title
    h = doc.add_heading(title, level=0)
    h.runs[0].font.color.rgb = RGBColor(0x1A, 0x4E, 0x88)
    if is_arabic:
        _set_rtl(h)

    # Subtitle / timestamp
    ts = datetime.datetime.utcnow().strftime("%B %d, %Y  %H:%M UTC")
    sub_text = f"{subtitle}\nGenerated: {ts}" if subtitle else f"Generated: {ts}"
    sp = doc.add_paragraph(sub_text)
    for run in sp.runs:
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
    if is_arabic:
        _set_rtl(sp)
    doc.add_paragraph()

    lines = content.replace("\\n", "\n").splitlines()
    para_buf: List[str] = []
    table_buf: List[str] = []

    def flush_table():
        if table_buf:
            rows = _parse_md_table(list(table_buf))
            if rows:
                col_count = max(len(r) for r in rows)
                tbl = doc.add_table(rows=len(rows), cols=col_count)
                try:
                    tbl.style = "Table Grid"
                except Exception:
                    pass
                for i, row_cells in enumerate(rows):
                    row = tbl.rows[i]
                    for j in range(col_count):
                        cell_text = row_cells[j] if j < len(row_cells) else ""
                        cell = row.cells[j]
                        cell.text = ""
                        p = cell.paragraphs[0]
                        _add_runs(p, cell_text)
                        if i == 0:
                            for run in p.runs:
                                run.bold = True
                                run.font.color.rgb = RGBColor(0x1A, 0x4E, 0x88)
            doc.add_paragraph()
            table_buf.clear()

    def flush():
        if para_buf:
            text = " ".join(para_buf).strip()
            p = doc.add_paragraph()
            _add_runs(p, text)
            if is_arabic or _has_arabic(text):
                _set_rtl(p)
            para_buf.clear()

    for raw in lines:
        line = raw.rstrip()

        # Markdown table row
        if line.lstrip().startswith("|"):
            flush()
            table_buf.append(line)
            continue

        # Non-table line — flush any pending table
        flush_table()

        if re.match(r"^## [^#]", line):
            flush()
            h2 = doc.add_heading(_strip(line[3:].strip()), level=1)
            for run in h2.runs:
                run.font.color.rgb = RGBColor(0x1A, 0x4E, 0x88)
            if is_arabic:
                _set_rtl(h2)
            continue

        if re.match(r"^### ", line):
            flush()
            h3 = doc.add_heading(_strip(line[4:].strip()), level=2)
            if is_arabic:
                _set_rtl(h3)
            continue

        if re.fullmatch(r"[-=*]{3,}", line.strip()):
            flush()
            doc.add_paragraph()
            continue

        # Markdown image: ![caption](url), e.g. the charts create_report appends
        img_m = _MD_IMAGE_RE.match(line.strip())
        if img_m:
            flush()
            _add_image(img_m.group(2), img_m.group(1))
            continue

        if re.match(r"^\s*[-*+] ", line) or re.match(r"^\s*\d+[.)]\s", line):
            flush()
            text = re.sub(r"^\s*[-*+\d.)]+\s*", "", line).strip()
            p = doc.add_paragraph(style="List Bullet")
            _add_runs(p, text)
            if is_arabic or _has_arabic(text):
                _set_rtl(p)
            continue

        if not line.strip():
            flush()
            continue

        para_buf.append(line)

    flush_table()
    flush()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tool: create_word_report
# ---------------------------------------------------------------------------

@tool
def create_word_report(
    topic: str = "",
    thread_id: str = "",
    title: str = "",
    language: str = "English",
    model: str = "deepseek",
    instructions: str = "",
    s3_folder: str = "reports",
    content: str = "",
    email_to: str = "",
    email_subject: str = "",
    email_body: str = "",
) -> str:
    """
    Create a professional Word (.docx) report and upload it to S3.
    Returns a 24-hour presigned download link.

    If `content` is provided, it is used directly (no LLM call is made).
    Otherwise, an LLM generates the content based on topic or thread_id.

    TWO MODES — provide EITHER topic OR thread_id (or pass content directly):

    MODE 1 — TOPIC REPORT (provide topic, leave thread_id blank).
    MODE 2 — CONVERSATION SUMMARY (provide thread_id, leave topic blank).
        Call get_current_thread_id first to obtain the thread_id.

    LANGUAGE:
        Pass language="Arabic" for a fully Arabic report with RTL layout.

    Args:
        topic:        Report topic. Leave blank when summarizing a conversation.
        thread_id:    Conversation thread ID (from get_current_thread_id).
        title:        Document title. Auto-generated if blank.
        language:     Output language — "English" (default) or "Arabic".
        model:        LLM short name: deepseek (default), llama4, llama3, granite, mistral.
        instructions: Extra guidance for the LLM.
        s3_folder:    S3 prefix for the uploaded file (default: "reports").
        content:      Pre-generated report text. If provided, skips the LLM call entirely.
                      The agent can write the content itself and pass it here.
        email_to:     Optional recipient email address. If provided, the DOCX is
                  attached and emailed after generation.
        email_subject: Optional email subject line.
        email_body:   Optional email body text.

    Returns:
        A message with the S3 key and a 24-hour presigned download link.
    """
    topic     = (topic or "").strip()
    thread_id = (thread_id or "").strip()
    content   = (content or "").strip()

    if not topic and not thread_id and not content:
        return (
            "Please provide either:\n"
            "  - topic: the subject you want a report on, OR\n"
            "  - thread_id: to summarize the current conversation, OR\n"
            "  - content: pre-written report text."
        )

    is_arabic = _is_arabic_lang(language)
    msg_count = 0
    mode = "report"

    if thread_id:
        try:
            messages = _fetch_messages(thread_id)
        except Exception as e:
            return f"Failed to fetch thread messages: {e}"
        if not messages and not content:
            return f"No messages found in thread '{thread_id}'."
        msg_count = len(messages)
        mode = "summary"
        if not title:
            title = "Conversation Summary"

    if not content:
        model_id = _LLM_MODELS.get((model or "deepseek").strip().lower())
        if not model_id:
            return f"Unknown model '{model}'. Choose from: {', '.join(_LLM_MODELS)}."
        if thread_id:
            transcript = "\n".join(
                f"[{m['role'].upper()}]: {m['content']}"
                for m in messages
                if m.get("content", "").strip()
            )
            prompt = _summary_prompt(transcript, language)
        else:
            prompt = _report_prompt(topic, language, instructions)
        try:
            content = _llm_call(prompt, model_id, max_tokens=4000)
        except Exception as e:
            return f"LLM generation failed: {e}"
        if not content.strip():
            return "LLM returned empty content — please try again."

    if not title:
        title = topic[:80] if topic else "Report"

    try:
        docx_bytes = _build_docx(title, content, is_arabic=is_arabic)
    except Exception as e:
        return f"Word document build failed: {e}"

    safe = re.sub(r"[^\w\s-]", "", title.lower())
    safe = re.sub(r"[\s-]+", "_", safe).strip("_")[:50]
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{mode}_{safe}_{ts}.docx"
    folder = (s3_folder or "").strip("/")
    s3_key = f"{folder}/{filename}" if folder else filename
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    try:
        ak, sk, bucket, region = _s3_env()
        _s3_put(s3_key, docx_bytes, content_type, ak, sk, bucket, region)
        link = _s3_presign(s3_key, ak, sk, bucket, region)
    except RuntimeError as e:
        return f"Word document generated ({len(docx_bytes):,} bytes) but S3 upload failed: {e}"

    email_status = ""
    if (email_to or "").strip():
        subject = (email_subject or "").strip() or f"{title or 'Word Report'}"
        body = (email_body or "").strip() or (
            f"Attached is your generated Word document: {title or 'Report'}\n\n"
            f"Download link: {link}"
        )
        email_status = _send_email_attachment(
            recipient=email_to,
            subject=subject,
            body=body,
            filename=filename,
            attachment=docx_bytes,
            content_type=content_type,
        )

    size_kb = len(docx_bytes) / 1024

    if mode == "summary":
        result = (
            f"Conversation summary Word document created and uploaded to S3.\n\n"
            f"  Thread   : {thread_id}\n"
            f"  Messages : {msg_count}\n"
            f"  Language : {language}\n"
            f"  Model    : {model}\n"
            f"  File     : {s3_key}\n"
            f"  Size     : {size_kb:.1f} KB\n\n"
            f"[⬇ Download Summary (.docx)]({link})\n\n"
            f"_Link expires in 24 hours._"
        )
    else:
        result = (
            f"Word report generated and uploaded to S3.\n\n"
            f"  Topic    : {topic[:60]}\n"
            f"  Language : {language}\n"
            f"  Model    : {model}\n"
            f"  File     : {s3_key}\n"
            f"  Size     : {size_kb:.1f} KB\n\n"
            f"[⬇ Download Word Report (.docx)]({link})\n\n"
            f"_Link expires in 24 hours._"
        )

    if email_status:
        result += f"\n\n{email_status}"
    return result
