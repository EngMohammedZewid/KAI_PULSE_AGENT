from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_ai import Credentials
from ibm_watsonx_ai.foundation_models import ModelInference
from ibm_watsonx_ai.metanames import GenTextParamsMetaNames as GenParams
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Arabic rendering helpers
# ---------------------------------------------------------------------------

_AR_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"
    r"\uFB50-\uFDFF\uFE70-\uFEFF]"
)


def _has_arabic(text: str) -> bool:
    return bool(_AR_RE.search(text))


def _arabic_display(text: str) -> str:
    """Shape Arabic letters and apply BiDi algorithm for correct rendering."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text), base_dir='R')
    except ImportError:
        return text


_ARABIC_FONT_CACHE: dict[str, str] = {}


def _get_amiri_font() -> str | None:
    """Download Amiri Regular TTF to /tmp and return its path (cached).
    Tries S3 first (reliable in IBM cloud), then jsDelivr CDN, then raw GitHub."""
    if "path" in _ARABIC_FONT_CACHE:
        return _ARABIC_FONT_CACHE["path"]
    tmp = os.path.join(tempfile.gettempdir(), "amiri_regular.ttf")
    if os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
        _ARABIC_FONT_CACHE["path"] = tmp
        return tmp
    for url in [
        "https://aws-buc-for-orchestrate.s3.us-east-2.amazonaws.com/fonts/amiri_regular.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/amiri/Amiri-Regular.ttf",
        "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf",
    ]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "python-pdf/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) > 50_000:
                with open(tmp, "wb") as f:
                    f.write(data)
                _ARABIC_FONT_CACHE["path"] = tmp
                return tmp
        except Exception:
            continue
    return None


def _register_amiri() -> bool:
    """Register Amiri font in reportlab. Returns True if successful."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        fp = _get_amiri_font()
        if not fp:
            return False
        pdfmetrics.registerFont(TTFont("Amiri", fp))
        return True
    except Exception:
        return False


def _load_history(path: str) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"History file not found: {path}")

    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("History file must contain a JSON object.")
    if not isinstance(data.get("history"), list):
        raise ValueError("History JSON must include a 'history' list.")
    return data


def _build_summary_text(data: Dict[str, Any], recent_turns: int) -> str:
    history: List[Dict[str, Any]] = data.get("history", [])
    total_turns = len(history)

    user_messages = [str(t.get("user_input") or "").strip() for t in history]
    assistant_messages = [str(t.get("assistant_response") or "").strip() for t in history]

    non_empty_user = [m for m in user_messages if m]
    non_empty_assistant = [m for m in assistant_messages if m]

    user_word_count = sum(len(m.split()) for m in non_empty_user)
    assistant_word_count = sum(len(m.split()) for m in non_empty_assistant)

    topic_tokens = []
    for msg in non_empty_user:
        for token in msg.lower().split():
            token = token.strip(".,!?;:\\n\\r()[]{}\"'")
            if len(token) >= 4:
                topic_tokens.append(token)
    common_topics: List[Tuple[str, int]] = Counter(topic_tokens).most_common(8)

    recent = history[-recent_turns:] if recent_turns > 0 else history

    lines: List[str] = []
    lines.append("Session Chat History Summary")
    lines.append("=" * 28)
    lines.append(f"Generated at: {datetime.utcnow().isoformat()}Z")
    lines.append(f"Total turns: {total_turns}")
    lines.append(f"Total user words: {user_word_count}")
    lines.append(f"Total assistant words: {assistant_word_count}")
    lines.append("")

    if common_topics:
        lines.append("Frequent user keywords:")
        for token, count in common_topics:
            lines.append(f"- {token}: {count}")
        lines.append("")

    lines.append(f"Recent turns shown: {len(recent)}")
    lines.append("-" * 20)
    for idx, turn in enumerate(recent, start=max(1, total_turns - len(recent) + 1)):
        user_input = str(turn.get("user_input") or "(none)").strip()
        assistant_response = str(turn.get("assistant_response") or "(none)").strip()
        lines.append(f"Turn {idx}")
        lines.append(f"User: {user_input}")
        lines.append(f"Assistant: {assistant_response}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _write_pdf(summary_text: str, output_pdf_path: str) -> str:
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase import pdfmetrics

    output = Path(output_pdf_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(output), pagesize=A4)
    width, height = A4
    margin_l = 40
    margin_r = 40
    usable_w = width - margin_l - margin_r
    y = height - 50
    line_height = 14
    font_size = 10

    ar_ok = _register_amiri()

    def _draw_visual_line(display_text: str, arabic: bool) -> None:
        """Draw a single already-shaped/BiDi line, advancing y."""
        nonlocal y
        if arabic and ar_ok:
            c.setFont("Amiri", font_size)
            c.drawRightString(width - margin_r, y, display_text)
        else:
            c.setFont("Helvetica", font_size)
            c.drawString(margin_l, y, display_text)
        y -= line_height
        if y < 40:
            c.showPage()
            y = height - 50

    def _render_paragraph(text: str) -> None:
        """Wrap and render one logical paragraph with correct per-line BiDi."""
        arabic = _has_arabic(text)
        if arabic and ar_ok:
            # reshape → split in logical order → BiDi per line
            try:
                import arabic_reshaper
                shaped = arabic_reshaper.reshape(text)
            except ImportError:
                shaped = text
            raw_lines = simpleSplit(shaped, "Amiri", font_size, usable_w) or [""]
            try:
                from bidi.algorithm import get_display
                display_lines = [get_display(ln, base_dir='R') for ln in raw_lines]
            except ImportError:
                display_lines = raw_lines
        else:
            display_lines = simpleSplit(text, "Helvetica", font_size, usable_w) or [""]
            arabic = False
        for ln in display_lines:
            _draw_visual_line(ln, arabic)

    for paragraph in summary_text.split("\n"):
        if not paragraph.strip():
            y -= line_height  # blank line spacing
            if y < 40:
                c.showPage()
                y = height - 50
            continue
        _render_paragraph(paragraph)

    c.save()
    return str(output)


def _build_transcript_for_llm(data: Dict[str, Any], recent_turns: int) -> str:
    history: List[Dict[str, Any]] = data.get("history", [])
    selected = history[-recent_turns:] if recent_turns > 0 else history

    lines: List[str] = []
    for idx, turn in enumerate(selected, start=1):
        user_input = str(turn.get("user_input") or "").strip()
        assistant_response = str(turn.get("assistant_response") or "").strip()
        lines.append(f"Turn {idx}")
        lines.append(f"User: {user_input}")
        lines.append(f"Assistant: {assistant_response}")
        lines.append("")

    return "\n".join(lines).strip()


@tool
def summarize_session_history(
    history_json_path: str = "session_history_payload.json",
    recent_turns: int = 10,
    output_text_path: str = "session_history_summary.txt",
) -> str:
    """
    Generate a plain-text summary from session chat history.

    Args:
        history_json_path: Path to session history JSON file.
        recent_turns: Number of latest turns to include in detail.
        output_text_path: Output path for the generated summary text file.

    Returns:
        A status message including output file path.
    """
    data = _load_history(history_json_path)
    summary_text = _build_summary_text(data, max(0, recent_turns))

    output = Path(output_text_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(summary_text, encoding="utf-8")

    return f"Summary generated successfully: {output}"


@tool
def export_summary_pdf(
    summary_text_path: str = "session_history_summary.txt",
    output_pdf_path: str = "session_history_summary.pdf",
) -> str:
    """
    Export a text summary file to a PDF document.

    Args:
        summary_text_path: Path to the plain-text summary file.
        output_pdf_path: Path where the PDF should be written.

    Returns:
        A status message including PDF output path.
    """
    summary_path = Path(summary_text_path)
    if not summary_path.exists():
        raise FileNotFoundError(f"Summary text file not found: {summary_text_path}")

    summary_text = summary_path.read_text(encoding="utf-8")
    pdf_path = _write_pdf(summary_text, output_pdf_path)
    return f"PDF exported successfully: {pdf_path}"




@tool
def summarize_session_history_with_llama(
    history_json_path: str = "session_history_payload.json",
    recent_turns: int = 20,
    model_id: str = "meta-llama/llama-3-3-70b-instruct",
    temperature: float = 0.1,
    max_new_tokens: int = 1024,
) -> str:
    """
    Summarize user and assistant chat turns using Llama 3.3 70B via IBM watsonx.ai.

    Args:
        history_json_path: Path to session history JSON file.
        recent_turns: Number of latest turns to summarize (0 means all).
        model_id: watsonx.ai model ID (default: meta-llama/llama-3-3-70b-instruct).
        temperature: Sampling temperature (lower = more factual).
        max_new_tokens: Maximum tokens in the generated summary.

    Returns:
        A concise summary generated by Llama 3.3 70B.
    """
    api_key = (
        os.getenv("WATSONX_APIKEY", "").strip()
        or os.getenv("WATSONX_API_KEY", "").strip()
    )
    project_id = (
        os.getenv("WATSONX_PROJECT_ID", "").strip()
        or "ab0de2fc-5f68-4e0e-8dc8-d1677b6b689b"
    )
    url = os.getenv("WATSONX_URL", "https://us-south.ml.cloud.ibm.com").strip()

    try:
        data = _load_history(history_json_path)
    except FileNotFoundError:
        return (
            f"History file not found: '{history_json_path}'. "
            "Use the capture_thread_payload tool to save the current thread to S3, "
            "then download it and re-run this tool with the correct path."
        )
    transcript = _build_transcript_for_llm(data, max(0, recent_turns))
    if not transcript:
        return "No conversation turns found to summarize."

    credentials = Credentials(url=url, api_key=api_key)

    model = ModelInference(
        model_id=model_id,
        credentials=credentials,
        project_id=project_id,
        params={
            GenParams.TEMPERATURE: temperature,
            GenParams.MAX_NEW_TOKENS: max_new_tokens,
            GenParams.STOP_SEQUENCES: [],
        },
    )

    prompt = (
        "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
        "You are a summarization assistant. Read the conversation between a user and an assistant, "
        "then return a helpful, concise summary. "
        "Include: 1) main topics discussed, 2) important outcomes or answers, 3) any pending actions.\n"
        "<|eot_id|><|start_header_id|>user<|end_header_id|>\n"
        f"Conversation:\n{transcript}\n"
        "<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n"
    )

    response = model.generate_text(prompt=prompt)
    summary = (response or "").strip()
    if not summary:
        return "Llama returned an empty summary."
    return summary


@tool
def summarize_session_history_with_gemini(
    history_json_path: str = "session_history_payload.json",
    recent_turns: int = 20,
    model_id: str = "meta-llama/llama-3-3-70b-instruct",
    temperature: float = 0.1,
    max_new_tokens: int = 1024,
) -> str:
    """
    Backward-compatible alias retained for existing agent definitions.
    """
    return summarize_session_history_with_llama(
        history_json_path=history_json_path,
        recent_turns=recent_turns,
        model_id=model_id,
        temperature=temperature,
        max_new_tokens=max_new_tokens,
    )
