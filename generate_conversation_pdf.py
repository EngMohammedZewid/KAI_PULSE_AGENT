"""
Generate a summary PDF of the CBE Exchange Agent conversation
from session_history_payload.json
"""
import json
import re
from datetime import datetime
from collections import Counter

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ── Register Arabic-capable fonts ─────────────────────────────────────────────
pdfmetrics.registerFont(TTFont("Arial", "C:/Windows/Fonts/arial.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Bold", "C:/Windows/Fonts/arialbd.ttf"))

# ── Colours ────────────────────────────────────────────────────────────────────
BLUE_DARK  = HexColor("#1B3A6B")
BLUE_MID   = HexColor("#2E6DB4")
BLUE_LIGHT = HexColor("#D6E4F7")
GOLD       = HexColor("#C8A84B")
GREY_BG    = HexColor("#F5F5F5")
GREY_TEXT  = HexColor("#555555")

# ── Helpers ────────────────────────────────────────────────────────────────────
def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def is_arabic(text: str) -> bool:
    arabic_chars = sum(1 for c in text if "\u0600" <= c <= "\u06FF")
    return arabic_chars > len(text) * 0.3

def detect_topic(user: str, assistant: str) -> str:
    combined = (user + " " + assistant).lower()
    if any(w in combined for w in ["usd", "sar", "riyal", "ريال"]):
        return "USD → SAR Conversion"
    if any(w in combined for w in ["usd", "egp", "دولار", "مصري", "pound"]):
        return "USD ↔ EGP Conversion"
    if any(w in combined for w in ["rate", "rates", "سعر", "أسعار", "exchange"]):
        return "Exchange Rate Inquiry"
    if any(w in combined for w in ["hi", "hello", "مرحبا", "thanks", "شكر"]):
        return "Greeting / Thanks"
    return "General Inquiry"

def summarize_turns(history):
    """Produce topic-grouped summary bullets."""
    topics = [detect_topic(t.get("user_input",""), t.get("assistant_response","")) for t in history]
    topic_counts = Counter(topics)
    
    summary_lines = []
    for topic, count in topic_counts.most_common():
        summary_lines.append((topic, count))
    return summary_lines, topics

def build_pdf(input_file="session_history_payload.json",
              output_file="CBE_Exchange_Agent_Conversation_Summary.pdf"):

    with open(input_file, encoding="utf-8") as f:
        data = json.load(f)

    history     = data.get("history", [])
    agent_name  = data.get("agent_name", "CBE Exchange Agent")
    agent_id    = data.get("agent_id", "")
    total_turns = data.get("total_turns", len(history))
    generated   = data.get("generated_at", "")
    try:
        gen_dt = datetime.fromisoformat(generated).strftime("%B %d, %Y  %H:%M UTC")
    except Exception:
        gen_dt = generated

    # ── Styles ─────────────────────────────────────────────────────────────────
    styles = getSampleStyleSheet()

    def S(name, **kw):
        defaults = dict(fontName="Arial", fontSize=10, leading=14, textColor=black)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    title_style     = S("Title",     fontName="Arial-Bold", fontSize=22,
                        textColor=white, alignment=TA_CENTER, leading=28)
    subtitle_style  = S("Subtitle",  fontName="Arial", fontSize=11,
                        textColor=BLUE_LIGHT, alignment=TA_CENTER)
    meta_style      = S("Meta",      fontName="Arial", fontSize=9,
                        textColor=GREY_TEXT, alignment=TA_CENTER)
    section_style   = S("Section",   fontName="Arial-Bold", fontSize=13,
                        textColor=BLUE_DARK, spaceBefore=12, spaceAfter=4)
    body_style      = S("Body",      fontSize=10, leading=15, spaceAfter=4)
    bullet_style    = S("Bullet",    fontSize=10, leading=15,
                        leftIndent=16, spaceAfter=3)
    label_user      = S("LabelUser", fontName="Arial-Bold", fontSize=9,
                        textColor=BLUE_MID)
    label_asst      = S("LabelAsst", fontName="Arial-Bold", fontSize=9,
                        textColor=GREY_TEXT)
    turn_text       = S("TurnText",  fontSize=9, leading=13, spaceAfter=2)
    footer_style    = S("Footer",    fontSize=8, textColor=GREY_TEXT,
                        alignment=TA_CENTER)

    # ── Document ────────────────────────────────────────────────────────────────
    doc = SimpleDocTemplate(
        output_file,
        pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm,  bottomMargin=2*cm,
        title="CBE Exchange Agent – Conversation Summary",
        author="watsonx Orchestrate ADK"
    )

    story = []

    # ── Cover banner ───────────────────────────────────────────────────────────
    banner_data = [[Paragraph("CBE Exchange Agent", title_style)],
                   [Paragraph("Conversation Summary Report", subtitle_style)],
                   [Spacer(1, 6)],
                   [Paragraph(f"Generated: {gen_dt}", meta_style)],
                   [Paragraph(f"Agent ID: {agent_id}", meta_style)]]
    banner_table = Table(banner_data, colWidths=[17*cm])
    banner_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), BLUE_DARK),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [BLUE_DARK]),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING",   (0,0), (-1,-1), 16),
        ("RIGHTPADDING",  (0,0), (-1,-1), 16),
        ("ROUNDEDCORNERS",(0,0), (-1,-1), [6]),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 0.5*cm))

    # ── Key Stats ──────────────────────────────────────────────────────────────
    story.append(Paragraph("Session Overview", section_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=8))

    topic_summary, turn_topics = summarize_turns(history)

    # Count languages
    arabic_turns = sum(1 for t in history if is_arabic(t.get("user_input","")))
    english_turns = total_turns - arabic_turns

    stats_data = [
        ["Total Conversation Turns", str(total_turns)],
        ["Arabic Queries",           str(arabic_turns)],
        ["English Queries",          str(english_turns)],
        ["Most Frequent Topic",      topic_summary[0][0] if topic_summary else "—"],
        ["Unique Topics Covered",    str(len(topic_summary))],
    ]
    stats_table = Table(
        [[Paragraph(k, S("sk", fontName="Arial-Bold", fontSize=10, textColor=BLUE_DARK)),
          Paragraph(v, S("sv", fontSize=10, textColor=black, alignment=TA_RIGHT))]
         for k, v in stats_data],
        colWidths=[10*cm, 7*cm]
    )
    stats_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), GREY_BG),
        ("ROWBACKGROUNDS",(0,0), (-1,-1), [white, GREY_BG]),
        ("GRID",          (0,0), (-1,-1), 0.3, HexColor("#CCCCCC")),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ]))
    story.append(stats_table)
    story.append(Spacer(1, 0.4*cm))

    # ── Topic Distribution ─────────────────────────────────────────────────────
    story.append(Paragraph("Topic Distribution", section_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=8))

    topic_rows = [
        [Paragraph("Topic", S("th", fontName="Arial-Bold", fontSize=10, textColor=white)),
         Paragraph("Turns", S("th2", fontName="Arial-Bold", fontSize=10, textColor=white, alignment=TA_CENTER))]
    ]
    for topic, count in topic_summary:
        bar = "█" * count
        topic_rows.append([
            Paragraph(topic, body_style),
            Paragraph(f"{count}  {bar}", S("bar", fontSize=9, textColor=BLUE_MID))
        ])

    topic_table = Table(topic_rows, colWidths=[11*cm, 6*cm])
    topic_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), BLUE_MID),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [white, GREY_BG]),
        ("GRID",          (0,0), (-1,-1), 0.3, HexColor("#CCCCCC")),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ]))
    story.append(topic_table)
    story.append(Spacer(1, 0.4*cm))

    # ── Key Highlights ─────────────────────────────────────────────────────────
    story.append(Paragraph("Key Highlights", section_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=8))

    highlights = [
        "The agent handled bilingual queries in both <b>English and Arabic</b>, responding in the user's language.",
        "Exchange rates sourced from the <b>Central Bank of Egypt (CBE)</b> were quoted consistently.",
        "Currencies covered: <b>USD, EUR, GBP, CHF, JPY, SAR, KWD, AED, CNY</b>.",
        "Conversion queries included <b>USD→EGP</b>, <b>USD→SAR</b>, and <b>EGP→USD</b>.",
        "The agent performed <b>multi-step cross-currency calculations</b> (e.g., USD→EGP→SAR).",
        f"A total of <b>{arabic_turns} Arabic</b> and <b>{english_turns} English</b> turns were recorded across the session.",
    ]
    for h in highlights:
        story.append(Paragraph(f"• {h}", bullet_style))
    story.append(Spacer(1, 0.4*cm))

    # ── Sample Exchange Rates (last captured) ──────────────────────────────────
    # Extract last known rates from last assistant response
    last_response = strip_html(data.get("last_turn", {}).get("assistant_response", ""))
    rates_lines = [l.strip() for l in last_response.split("\n") if "|" in l and "---" not in l]

    if rates_lines:
        story.append(Paragraph("Latest Exchange Rates (Last Captured)", section_style))
        story.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=8))
        story.append(Paragraph(
            "Rates sourced from Central Bank of Egypt – as captured at end of session:",
            S("note", fontSize=9, textColor=GREY_TEXT, spaceAfter=6)
        ))

        rate_rows = []
        for line in rates_lines:
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells:
                rate_rows.append([Paragraph(c, S(f"rc{i}", fontSize=9)) for i, c in enumerate(cells)])

        if rate_rows:
            col_count = max(len(r) for r in rate_rows)
            col_w = 17*cm / col_count
            rate_table = Table(rate_rows, colWidths=[col_w]*col_count)
            rate_table.setStyle(TableStyle([
                ("ROWBACKGROUNDS", (0,0), (-1,-1), [white, GREY_BG]),
                ("GRID",          (0,0), (-1,-1), 0.3, HexColor("#CCCCCC")),
                ("TOPPADDING",    (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                ("LEFTPADDING",   (0,0), (-1,-1), 6),
                ("RIGHTPADDING",  (0,0), (-1,-1), 6),
            ]))
            story.append(rate_table)
            story.append(Spacer(1, 0.4*cm))

    # ── Full Conversation Log ──────────────────────────────────────────────────
    story.append(Paragraph("Full Conversation Log", section_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=GOLD, spaceAfter=8))

    for i, turn in enumerate(history, 1):
        user_in = turn.get("user_input", "").strip()
        asst_out = strip_html(turn.get("assistant_response", ""))
        # Truncate very long assistant responses for readability
        if len(asst_out) > 800:
            asst_out = asst_out[:800] + "\n[... truncated ...]"

        block = []
        block.append(Paragraph(
            f"Turn {i}",
            S(f"tn{i}", fontName="Arial-Bold", fontSize=9, textColor=white)
        ))
        if user_in:
            block.append(Paragraph("User:", label_user))
            block.append(Paragraph(user_in, turn_text))
        if asst_out:
            block.append(Paragraph("Assistant:", label_asst))
            block.append(Paragraph(asst_out.replace("\n", "<br/>"), turn_text))

        turn_data = [[item] for item in block]
        turn_table = Table(turn_data, colWidths=[17*cm])
        turn_table.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (0,0), BLUE_MID),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [BLUE_LIGHT]),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ]))
        story.append(KeepTogether(turn_table))
        story.append(Spacer(1, 0.2*cm))

    # ── Footer note ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#CCCCCC")))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        f"Report generated by watsonx Orchestrate ADK  |  {gen_dt}  |  Agent: {agent_id}",
        footer_style
    ))

    # ── Build ──────────────────────────────────────────────────────────────────
    doc.build(story)
    print(f"✅  PDF saved → {output_file}")


if __name__ == "__main__":
    build_pdf()
