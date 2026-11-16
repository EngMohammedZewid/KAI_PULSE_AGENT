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

from tools._tooling import tool

# ---------------------------------------------------------------------------
# PDF generation using fpdf2 + arabic-reshaper + python-bidi.
# Supports full Unicode including Arabic (RTL), Latin, numbers and symbols.
# ---------------------------------------------------------------------------

# Theme colours (r, g, b in 0.0–1.0 range)
_BLUE   = (0.102, 0.306, 0.533)
_LGREY  = (0.600, 0.600, 0.600)
_STRIPE = (0.929, 0.949, 0.969)
_BLACK  = (0.0, 0.0, 0.0)
_WHITE  = (1.0, 1.0, 1.0)

_BULLET = "\u2022  "   # U+2022 BULLET, supported by all TrueType fonts

# Regex to strip **bold** / *italic* / __bold__ / _italic_ inline markers
_INLINE_MD_RE = re.compile(r"\*{1,2}([^*\n]+)\*{1,2}|_{1,2}([^_\n]+)_{1,2}")


def _strip_inline_md(text: str) -> str:
    """Remove **bold** / *italic* markers, keeping the inner text."""
    return _INLINE_MD_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)


# ---------------------------------------------------------------------------
# Arabic text helpers
# ---------------------------------------------------------------------------

# Unicode ranges that indicate Arabic / RTL script
_AR_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"
    r"\uFB50-\uFDFF\uFE70-\uFEFF]"
)


def _has_arabic(text: str) -> bool:
    """Return True if text contains Arabic characters."""
    return bool(_AR_RE.search(text))


def _ar_reshape(text: str) -> str:
    """Shape Arabic letter forms only (no BiDi reorder). Leaves logical order."""
    try:
        import arabic_reshaper
        return arabic_reshaper.reshape(text)
    except ImportError:
        return text


def _ar_bidi(text: str) -> str:
    """Apply BiDi visual reordering to a single already-shaped line.
    Always forces RTL base direction so mixed Arabic/Latin lines
    (e.g. '2025 في عام' or 'IBM شركة') are rendered correctly.
    """
    try:
        from bidi.algorithm import get_display
        return get_display(text, base_dir='R')
    except ImportError:
        return text


def _arabic_display(text: str) -> str:
    """Shape Arabic letters and apply the BiDi algorithm for correct rendering."""
    return _ar_bidi(_ar_reshape(text))


def _ar_wrap(text: str, font_name: str, font_size: float, avail_w: float) -> list[str]:
    """
    Word-wrap Arabic shaped text (logical order) into lines that fit avail_w.
    Uses pdfmetrics.stringWidth for accurate per-word measurement.
    Returns a list of shaped logical-order fragments ready for per-line BiDi.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth as sw
    words = text.split(" ")
    lines: list[str] = []
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
            # Accept word even if it alone exceeds avail_w (no infinite loop)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


# Cache so we only download the font once per process
_ARABIC_FONT_CACHE: dict[str, str] = {}


def _get_arabic_font() -> str | None:
    """
    Download the Amiri Regular TrueType font to /tmp and return its path.
    Tries S3 bucket first (most reliable in cloud), then GitHub CDN, then raw GitHub.
    Returns None if all sources fail.
    """
    if "path" in _ARABIC_FONT_CACHE:
        return _ARABIC_FONT_CACHE["path"]

    tmp = os.path.join(tempfile.gettempdir(), "amiri_regular.ttf")
    if os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
        _ARABIC_FONT_CACHE["path"] = tmp
        return tmp

    urls = [
        "https://aws-buc-for-orchestrate.s3.us-east-2.amazonaws.com/fonts/amiri_regular.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/amiri/Amiri-Regular.ttf",
        "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf",
    ]
    for url in urls:
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


def _get_arabic_font_bold() -> str | None:
    """Download Amiri Bold TrueType font. Tries S3 first, then GitHub CDN, then raw GitHub."""
    if "bold" in _ARABIC_FONT_CACHE:
        return _ARABIC_FONT_CACHE["bold"]

    tmp = os.path.join(tempfile.gettempdir(), "amiri_bold.ttf")
    if os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
        _ARABIC_FONT_CACHE["bold"] = tmp
        return tmp

    urls = [
        "https://aws-buc-for-orchestrate.s3.us-east-2.amazonaws.com/fonts/amiri_bold.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/amiri/Amiri-Bold.ttf",
        "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Bold.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Bold.ttf",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "python-pdf/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) > 50_000:
                with open(tmp, "wb") as f:
                    f.write(data)
                _ARABIC_FONT_CACHE["bold"] = tmp
                return tmp
        except Exception:
            continue
    return None


def _image_dims(data: bytes) -> tuple[int, int]:
    """Quick PNG/JPEG width×height extraction (no full decode)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":          # PNG: IHDR is always first chunk
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":                    # JPEG: scan for SOF marker
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h = struct.unpack(">H", data[i + 5: i + 7])[0]
                w = struct.unpack(">H", data[i + 7: i + 9])[0]
                return w, h
            length = struct.unpack(">H", data[i + 2: i + 4])[0]
            i += 2 + length
    return 0, 0


# ---------------------------------------------------------------------------
# Image download helper
# ---------------------------------------------------------------------------

def _fetch_png(url: str) -> bytes:
    """Download an image from URL and return raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()



class _PDFReportlab:
    """A4 PDF writer using reportlab with full Unicode (Arabic RTL) support."""

    W  = 595.28
    H  = 841.89
    ML = 56.7
    MR = 56.7
    MT = 56.7
    MB = 70.0

    def __init__(self, doc_title: str = "") -> None:
        try:
            from reportlab.pdfgen import canvas as _rl_canvas
        except ImportError:
            # Runtime pip-install fallback (works on Code Engine / Cloud Functions)
            try:
                import subprocess, sys
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "reportlab>=4.0", "-q"],
                    timeout=120,
                )
                from reportlab.pdfgen import canvas as _rl_canvas
            except Exception as _e:
                raise ImportError(f"reportlab not available: {_e}") from _e
        self._buf = io.BytesIO()
        self._c = _rl_canvas.Canvas(self._buf, pagesize=(self.W, self.H))
        self._uw = self.W - self.ML - self.MR
        self._cy = self.H - self.MT
        self._page_num = 1

        # Register Amiri TrueType font for Unicode / Arabic support
        self._ar_ok = False
        fp_reg = _get_arabic_font()
        if fp_reg:
            try:
                from reportlab.pdfbase import pdfmetrics
                from reportlab.pdfbase.ttfonts import TTFont
                pdfmetrics.registerFont(TTFont("Amiri", fp_reg))
                fp_bold = _get_arabic_font_bold()
                if fp_bold:
                    pdfmetrics.registerFont(TTFont("Amiri-Bold", fp_bold))
                else:
                    pdfmetrics.registerFont(TTFont("Amiri-Bold", fp_reg))
                self._ar_ok = True
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _font_name(self, bold: bool = False, mono: bool = False,
                   arabic: bool = False) -> str:
        if mono:
            return "Courier"
        if arabic and self._ar_ok:
            return "Amiri-Bold" if bold else "Amiri"
        return "Helvetica-Bold" if bold else "Helvetica"

    def _set_font(self, size: float, bold: bool = False,
                  mono: bool = False, arabic: bool = False) -> None:
        self._c.setFont(self._font_name(bold=bold, mono=mono, arabic=arabic), size)

    def _new_page(self) -> None:
        """Draw footer on current page and start a fresh page."""
        self._c.setFont("Helvetica", 8)
        self._c.setFillColorRGB(*_LGREY)
        self._c.drawCentredString(self.W / 2, self.MB - 18, str(self._page_num))
        self._c.showPage()
        self._page_num += 1
        self._cy = self.H - self.MT

    def _check(self, need: float = 20.0) -> None:
        if self._cy - need < self.MB:
            self._new_page()

    def _prepare_text(self, text: str) -> tuple[str, bool]:
        """Apply Arabic shaping+BiDi if needed. Returns (display_text, is_arabic)."""
        is_ar = _has_arabic(text)
        if is_ar:
            if self._ar_ok:
                text = _arabic_display(text)
            else:
                text = f"[Arabic — font unavailable: {text[:60]}]"
                is_ar = False
        return text, is_ar

    def _string_width(self, text: str, font: str, size: float) -> float:
        try:
            from reportlab.pdfbase.pdfmetrics import stringWidth
            return stringWidth(text, font, size)
        except Exception:
            return len(text) * size * 0.5  # rough fallback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def text_line(self, text: str, size: float, font: str = "Fr",
                  color: tuple = _BLACK, indent: float = 0.0,
                  leading: float = 1.4) -> None:
        bold = font == "Fb"
        mono = font == "Fc"
        text, is_ar = self._prepare_text(text)
        line_h = size * leading
        self._check(line_h)
        fname = self._font_name(bold=bold, mono=mono, arabic=is_ar)
        self._c.setFont(fname, size)
        self._c.setFillColorRGB(*color)
        y = self._cy - size
        x = self.ML + indent
        avail_w = self._uw - indent
        if is_ar:
            self._c.drawRightString(x + avail_w, y, text)
        else:
            self._c.drawString(x, y, text)
        self._cy -= line_h

    def paragraph(self, text: str, size: float = 10.0,
                  bold: bool = False, mono: bool = False,
                  color: tuple = _BLACK, indent: float = 0.0,
                  space_after: float = 5.0) -> None:
        from reportlab.lib.utils import simpleSplit
        is_ar = _has_arabic(text)
        fname = self._font_name(bold=bold, mono=mono, arabic=is_ar)
        avail_w = self._uw - indent
        line_h = size * 1.4
        if is_ar and self._ar_ok:
            # reshape (letter forms, logical order) → word-wrap by font width
            # → BiDi per line.  Never apply BiDi before wrapping.
            shaped = _ar_reshape(text)
            raw_lines = _ar_wrap(shaped, fname, size, avail_w)
            lines = [_ar_bidi(ln) for ln in raw_lines]
        else:
            lines = simpleSplit(text, fname, size, avail_w) or [""]
        for ln in lines:
            self._check(line_h)
            self._c.setFont(fname, size)
            self._c.setFillColorRGB(*color)
            y = self._cy - size
            x = self.ML + indent
            if is_ar:
                self._c.drawRightString(x + avail_w, y, ln)
            else:
                self._c.drawString(x, y, ln)
            self._cy -= line_h
        self._cy -= space_after

    def bullet_paragraph(self, text: str, size: float = 10.0,
                         space_after: float = 2.0) -> None:
        """Render an Arabic bullet item: • on the right, text indented from right."""
        from reportlab.pdfbase.pdfmetrics import stringWidth as sw
        fname = self._font_name(arabic=True)
        bullet_str = "\u2022 "  # • + space
        bullet_w = sw(bullet_str, "Helvetica", size) + 4.0
        avail_w = self._uw - bullet_w
        line_h = size * 1.4
        right_edge = self.ML + self._uw  # rightmost x

        shaped = _ar_reshape(text)
        raw_lines = _ar_wrap(shaped, fname, size, avail_w)
        visual_lines = [_ar_bidi(ln) for ln in raw_lines]

        for i, ln in enumerate(visual_lines):
            self._check(line_h)
            y = self._cy - size
            if i == 0:
                # Draw bullet on the far right
                self._c.setFont("Helvetica", size)
                self._c.setFillColorRGB(*_BLACK)
                self._c.drawString(right_edge - bullet_w + 2, y, bullet_str)
            # Draw Arabic text indented from right by bullet width
            self._c.setFont(fname, size)
            self._c.setFillColorRGB(*_BLACK)
            self._c.drawRightString(right_edge - bullet_w, y, ln)
            self._cy -= line_h
        self._cy -= space_after

    def vspace(self, pts: float) -> None:
        self._cy -= pts
        self._check(0)

    def hline(self, space: float = 6.0) -> None:
        self._check(space * 2 + 2)
        self._cy -= space
        self._c.setStrokeColorRGB(*_BLUE)
        self._c.setLineWidth(0.8)
        self._c.line(self.ML, self._cy, self.W - self.MR, self._cy)
        self._cy -= space

    def table(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        nc = max(len(r) for r in rows)
        if not nc:
            return
        cw = self._uw / nc
        rh, pad, fs = 18.0, 4.0, 9.0
        self._check(len(rows) * rh + 14)

        for ri, row in enumerate(rows):
            y = self._cy - rh
            if ri == 0:
                fill_color, tc, bold = _BLUE, _WHITE, True
            elif ri % 2 == 0:
                fill_color, tc, bold = _STRIPE, _BLACK, False
            else:
                fill_color, tc, bold = _WHITE, _BLACK, False

            # Row background
            self._c.setFillColorRGB(*fill_color)
            self._c.rect(self.ML, y, self._uw, rh, fill=1, stroke=0)

            # Cell borders
            self._c.setStrokeColorRGB(0.8, 0.8, 0.8)
            self._c.setLineWidth(0.4)
            for ci in range(nc):
                self._c.rect(self.ML + ci * cw, y, cw, rh, fill=0, stroke=1)

            # Cell text
            for ci, cell in enumerate(row[:nc]):
                ct = str(cell)
                ct, is_ar = self._prepare_text(ct)
                fname = self._font_name(bold=bold, arabic=is_ar)
                self._c.setFont(fname, fs)
                self._c.setFillColorRGB(*tc)
                cx = self.ML + ci * cw
                ty = y + pad + 2
                if is_ar:
                    self._c.drawRightString(cx + cw - pad, ty, ct)
                elif ri == 0:
                    self._c.drawCentredString(cx + cw / 2, ty, ct)
                else:
                    self._c.drawString(cx + pad, ty, ct)

            self._cy -= rh
        self._cy -= 10

    def image(self, url: str, caption: str = "") -> None:
        """Download an image from url and embed it, scaled to fit page width."""
        try:
            img_bytes = _fetch_png(url)
        except Exception as exc:
            self.paragraph(
                f"[Chart image could not be loaded: {exc}]"[:120],
                size=8.5, color=(0.5, 0.3, 0.0),
            )
            return

        w_px, h_px = _image_dims(img_bytes)
        if w_px > 0 and h_px > 0:
            scale = min(self._uw / w_px, 260.0 / h_px, 1.0)
            draw_w, draw_h = w_px * scale, h_px * scale
        else:
            draw_w, draw_h = self._uw, 200.0

        self._check(draw_h + 20)
        try:
            from reportlab.lib.utils import ImageReader
            img_reader = ImageReader(io.BytesIO(img_bytes))
            x = self.ML + (self._uw - draw_w) / 2.0
            y = self._cy - draw_h
            self._c.drawImage(img_reader, x, y, width=draw_w, height=draw_h,
                               preserveAspectRatio=True, mask="auto")
            self._cy -= draw_h + 4
        except Exception as exc:
            self.paragraph(f"[Image embed error: {exc}]", size=8.5)
            return

        if caption:
            self.paragraph(caption, size=9, color=_LGREY, space_after=10)
        else:
            self._cy -= 8

    def build(self) -> bytes:
        # Footer on last page
        self._c.setFont("Helvetica", 8)
        self._c.setFillColorRGB(*_LGREY)
        self._c.drawCentredString(self.W / 2, self.MB - 18, str(self._page_num))
        self._c.save()
        return self._buf.getvalue()


# ---------------------------------------------------------------------------
# Pillow-based fallback PDF (image-based, works without reportlab)
# ---------------------------------------------------------------------------

class _PDFPillow:
    """A4 PDF fallback using PIL/Pillow — renders pages as 150-DPI raster images.

    Used automatically when reportlab is not available in the runtime.
    Fully supports Unicode and Arabic text via the downloaded Amiri TTF font.
    Note: produces image-based pages (no text selection), but always works.
    """

    W_PT = 595.28
    H_PT = 841.89
    DPI  = 150
    ML   = 56.7
    MR   = 56.7
    MT   = 56.7
    MB   = 70.0

    def __init__(self, doc_title: str = "") -> None:
        from PIL import Image, ImageDraw  # raises ImportError if Pillow absent
        self._pages: list = []
        self._page_num = 0
        self._cy = 0.0
        self._new_page()

    # ---- coordinate helpers ----

    def _px(self, pt: float) -> int:
        return int(pt / 72.0 * self.DPI)

    def _to_pt(self, px: float) -> float:
        return px / self.DPI * 72.0

    @property
    def _uw(self) -> float:
        return self.W_PT - self.ML - self.MR

    @property
    def _draw(self):
        return self._pages[-1][1]

    @property
    def _img(self):
        return self._pages[-1][0]

    # ---- page management ----

    def _new_page(self) -> None:
        from PIL import Image, ImageDraw
        w = self._px(self.W_PT)
        h = self._px(self.H_PT)
        img = Image.new("RGB", (w, h), (255, 255, 255))
        self._pages.append((img, ImageDraw.Draw(img)))
        self._page_num += 1
        self._cy = self.MT

    def _check(self, need: float = 20.0) -> None:
        if self._cy + need > self.H_PT - self.MB:
            self._new_page()

    # ---- font helpers ----

    def _get_font(self, size_pt: float, bold: bool = False,
                  arabic: bool = False, mono: bool = False):
        from PIL import ImageFont
        size_px = max(8, int(size_pt / 72.0 * self.DPI))
        fp = (_get_arabic_font_bold() if bold else None) or _get_arabic_font()
        if fp:
            try:
                return ImageFont.truetype(fp, size_px)
            except Exception:
                pass
        try:
            return ImageFont.load_default(size=size_px)
        except TypeError:
            return ImageFont.load_default()

    def _text_w(self, text: str, font) -> float:
        """Text width in points."""
        try:
            w_px = self._draw.textlength(text, font=font)
        except Exception:
            try:
                w_px = self._draw.textsize(text, font=font)[0]
            except Exception:
                w_px = len(text) * getattr(font, "size", 10) * 0.55
        return self._to_pt(w_px)

    def _draw_str(self, text: str, x_pt: float, y_pt: float,
                  font, rgb: tuple, right: bool = False) -> None:
        if right:
            x_pt -= self._text_w(text, font)
        try:
            self._draw.text((self._px(x_pt), self._px(y_pt)), text,
                            font=font, fill=rgb, anchor="lt")
        except TypeError:
            self._draw.text((self._px(x_pt), self._px(y_pt)), text,
                            font=font, fill=rgb)

    def _wrap(self, text: str, font, max_w_pt: float) -> list:
        words = text.split()
        if not words:
            return [""]
        lines, cur = [], words[0]
        for w in words[1:]:
            candidate = cur + " " + w
            if self._text_w(candidate, font) <= max_w_pt:
                cur = candidate
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    def _prepare_text(self, text: str) -> tuple:
        is_ar = _has_arabic(text)
        if is_ar:
            text = _arabic_display(text)
        return text, is_ar

    # ---- public API (same signatures as _PDFReportlab) ----

    def text_line(self, text: str, size: float, font: str = "Fr",
                  color: tuple = _BLACK, indent: float = 0.0,
                  leading: float = 1.4) -> None:
        bold = font == "Fb"
        mono = font == "Fc"
        text, is_ar = self._prepare_text(text)
        line_h = size * leading
        self._check(line_h)
        f = self._get_font(size, bold=bold, arabic=is_ar, mono=mono)
        rgb = tuple(int(c * 255) for c in color)
        avail_w = self._uw - indent
        x = self.ML + indent + (avail_w if is_ar else 0)
        self._draw_str(text, x, self._cy, f, rgb, right=is_ar)
        self._cy += line_h

    def paragraph(self, text: str, size: float = 10.0,
                  bold: bool = False, mono: bool = False,
                  color: tuple = _BLACK, indent: float = 0.0,
                  space_after: float = 5.0) -> None:
        is_ar = _has_arabic(text)
        f = self._get_font(size, bold=bold, arabic=is_ar, mono=mono)
        avail_w = self._uw - indent
        line_h = size * 1.4
        rgb = tuple(int(c * 255) for c in color)
        if is_ar:
            # reshape (logical order) → wrap by pixel width → BiDi per line
            shaped = _ar_reshape(text)
            raw_lines = self._wrap(shaped, f, avail_w)
            lines = [_ar_bidi(ln) for ln in raw_lines]
        else:
            lines = self._wrap(text, f, avail_w)
        for ln in lines:
            self._check(line_h)
            x = self.ML + indent + (avail_w if is_ar else 0)
            self._draw_str(ln, x, self._cy, f, rgb, right=is_ar)
            self._cy += line_h
        self._cy += space_after

    def bullet_paragraph(self, text: str, size: float = 10.0,
                         space_after: float = 2.0) -> None:
        """Render an Arabic bullet item: • on the right, text indented from right."""
        f = self._get_font(size, arabic=True)
        bullet_str = "\u2022 "
        bullet_w = self._text_w(bullet_str, self._get_font(size)) + 4.0
        avail_w = self._uw - bullet_w
        line_h = size * 1.4
        rgb = tuple(int(c * 255) for c in _BLACK)
        right_edge = self.ML + self._uw

        shaped = _ar_reshape(text)
        raw_lines = self._wrap(shaped, f, avail_w)
        visual_lines = [_ar_bidi(ln) for ln in raw_lines]

        for i, ln in enumerate(visual_lines):
            self._check(line_h)
            if i == 0:
                bf = self._get_font(size)
                self._draw_str(bullet_str, right_edge - bullet_w + 2, self._cy, bf, rgb)
            self._draw_str(ln, right_edge - bullet_w, self._cy, f, rgb, right=True)
            self._cy += line_h
        self._cy += space_after

    def vspace(self, pts: float) -> None:
        self._cy += pts

    def hline(self, space: float = 6.0) -> None:
        self._cy += space
        self._check(2)
        y_px = self._px(self._cy)
        rgb = tuple(int(c * 255) for c in _BLUE)
        self._draw.line(
            [(self._px(self.ML), y_px), (self._px(self.W_PT - self.MR), y_px)],
            fill=rgb, width=max(1, self._px(0.8)),
        )
        self._cy += space

    def table(self, rows: list) -> None:
        if not rows:
            return
        nc = max(len(r) for r in rows)
        if not nc:
            return
        cw = self._uw / nc
        rh, pad, fs = 18.0, 4.0, 9.0
        self._check(len(rows) * rh + 14)

        for ri, row in enumerate(rows):
            if ri == 0:
                fill_c, tc, bold = _BLUE, _WHITE, True
            elif ri % 2 == 0:
                fill_c, tc, bold = _STRIPE, _BLACK, False
            else:
                fill_c, tc, bold = _WHITE, _BLACK, False

            fill_rgb  = tuple(int(c * 255) for c in fill_c)
            tc_rgb    = tuple(int(c * 255) for c in tc)
            border_rgb = (200, 200, 200)
            x1 = self._px(self.ML)
            y1 = self._px(self._cy)
            x2 = self._px(self.ML + self._uw)
            y2 = self._px(self._cy + rh)
            self._draw.rectangle([(x1, y1), (x2, y2)], fill=fill_rgb, outline=border_rgb)
            for ci in range(1, nc):
                cx = self._px(self.ML + ci * cw)
                self._draw.line([(cx, y1), (cx, y2)], fill=border_rgb, width=1)

            for ci, cell in enumerate(row[:nc]):
                ct = str(cell)
                ct, is_ar = self._prepare_text(ct)
                f = self._get_font(fs, bold=bold, arabic=is_ar)
                cell_l = self.ML + ci * cw
                ty = self._cy + pad
                if is_ar:
                    self._draw_str(ct, cell_l + cw - pad, ty, f, tc_rgb, right=True)
                elif ri == 0:
                    tw = self._text_w(ct, f)
                    self._draw_str(ct, cell_l + (cw - tw) / 2, ty, f, tc_rgb)
                else:
                    self._draw_str(ct, cell_l + pad, ty, f, tc_rgb)

            self._cy += rh
        self._cy += 10

    def image(self, url: str, caption: str = "") -> None:
        try:
            img_bytes = _fetch_png(url)
        except Exception as exc:
            self.paragraph(f"[Chart image could not be loaded: {exc}]"[:120],
                           size=8.5, color=(0.5, 0.3, 0.0))
            return
        try:
            from PIL import Image as _PI
            chart = _PI.open(io.BytesIO(img_bytes)).convert("RGB")
        except Exception as exc:
            self.paragraph(f"[Image embed error: {exc}]", size=8.5)
            return

        sw, sh = chart.size
        scale = min(self._px(self._uw) / sw, self._px(260.0) / sh, 1.0)
        nw, nh = int(sw * scale), int(sh * scale)
        resample = getattr(_PI, "LANCZOS", getattr(_PI, "ANTIALIAS", _PI.BICUBIC))
        chart = chart.resize((nw, nh), resample)

        nh_pt = self._to_pt(nh)
        self._check(nh_pt + 20)
        xoff = self._px(self.ML + (self._uw - self._to_pt(nw)) / 2)
        self._img.paste(chart, (xoff, self._px(self._cy)))
        self._cy += nh_pt + 4

        if caption:
            self.paragraph(caption, size=9, color=_LGREY, space_after=10)
        else:
            self._cy += 8

    def build(self) -> bytes:
        buf = io.BytesIO()
        # Stamp page numbers
        for i, (img, draw) in enumerate(self._pages):
            pg = str(i + 1)
            f = self._get_font(8)
            try:
                tw = draw.textlength(pg, font=f)
            except Exception:
                tw = 10
            x_px = (img.width - int(tw)) // 2
            y_px = self._px(self.H_PT - self.MB + 10)
            lgrey = tuple(int(c * 255) for c in _LGREY)
            try:
                draw.text((x_px, y_px), pg, font=f, fill=lgrey, anchor="lt")
            except TypeError:
                draw.text((x_px, y_px), pg, font=f, fill=lgrey)

        first = self._pages[0][0]
        rest  = [p[0] for p in self._pages[1:]]
        if rest:
            first.save(buf, "PDF", save_all=True, append_images=rest,
                       resolution=self.DPI)
        else:
            first.save(buf, "PDF", resolution=self.DPI)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Factory — choose the best available backend automatically
# ---------------------------------------------------------------------------

def _PDF(doc_title: str = ""):
    """Return a _PDFReportlab or _PDFPillow instance, whichever works first."""
    try:
        return _PDFReportlab(doc_title)
    except ImportError:
        pass
    try:
        return _PDFPillow(doc_title)
    except ImportError:
        raise ImportError(
            "PDF generation requires 'reportlab' or 'Pillow'. "
            "Neither library is available in the current Python environment."
        )


def _build_pdf(title: str, content: str, subtitle: str = "") -> bytes:
    """Parse lightweight markdown content and render to A4 PDF bytes."""
    doc = _PDF(doc_title=title)

    doc.vspace(20)
    doc.paragraph(title, size=22, bold=True, color=_BLUE, space_after=6)
    if subtitle:
        doc.paragraph(subtitle, size=11, color=_LGREY, space_after=4)
    ts = datetime.datetime.utcnow().strftime("%B %d, %Y  %H:%M UTC")
    doc.paragraph(f"Generated: {ts}", size=9, color=_LGREY, space_after=10)
    doc.hline()
    doc.vspace(10)

    lines = content.replace("\\n", "\n").splitlines()
    para_buf: list[str] = []
    in_code = False
    code_buf: list[str] = []
    table_rows: list[list[str]] = []

    def flush_para() -> None:
        if para_buf:
            doc.paragraph(_strip_inline_md(" ".join(para_buf).strip()))
            para_buf.clear()

    def flush_table() -> None:
        if table_rows:
            doc.table(table_rows)
            table_rows.clear()

    for raw in lines:
        line = raw.rstrip()

        if line.startswith("```"):
            if in_code:
                in_code = False
                if code_buf:
                    for cl in code_buf:
                        doc.paragraph(cl, size=9, mono=True,
                                      color=(0.2, 0.2, 0.2), space_after=0)
                    code_buf.clear()
                    doc.vspace(6)
            else:
                flush_para()
                flush_table()
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue

        if line.startswith("|"):
            flush_para()
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-+:?|:?=+:?", c) for c in cells if c):
                continue
            table_rows.append(cells)
            continue
        else:
            flush_table()

        if re.match(r"^# [^#]", line):
            flush_para()
            doc.vspace(8)
            doc.paragraph(_strip_inline_md(line[2:].strip()), size=16, bold=True,
                          color=_BLUE, space_after=4)
            continue
        if re.match(r"^## [^#]", line):
            flush_para()
            doc.vspace(6)
            doc.paragraph(_strip_inline_md(line[3:].strip()), size=13, bold=True,
                          color=_BLUE, space_after=3)
            continue
        if re.match(r"^### ", line):
            flush_para()
            doc.vspace(4)
            doc.paragraph(_strip_inline_md(line[4:].strip()), size=11, bold=True, space_after=3)
            continue
        if re.fullmatch(r"[-=*]{3,}", line.strip()):
            flush_para()
            doc.hline()
            continue
        if re.match(r"^\s*[-*+] ", line) or re.match(r"^\s*\d+[.)]\s", line):
            flush_para()
            text = _strip_inline_md(re.sub(r"^\s*[-*+\d.)]+\s*", "", line).strip())
            if _has_arabic(text):
                # For Arabic bullets: the • prefix confuses BiDi (it's a neutral
                # char that gets placed at the wrong end of the visual line).
                # Use a dedicated bullet_paragraph that draws • on the right side.
                doc.bullet_paragraph(text, size=10, space_after=2)
            else:
                doc.paragraph(_BULLET + text, size=10, indent=14, space_after=2)
            continue
        if not line.strip():
            flush_para()
            doc.vspace(4)
            continue
        # Markdown image: ![alt text](url)
        img_m = re.match(r"!\[([^\]]*)\]\((\S+)\)", line.strip())
        if img_m:
            flush_para()
            flush_table()
            doc.image(img_m.group(2), caption=img_m.group(1))
            continue
        # Plain markdown link [text](url) that looks like a chart URL — embed as image
        link_m = re.match(r"\[([^\]]*)\]\((https://quickchart\.io/[^)]+)\)", line.strip())
        if link_m:
            flush_para()
            flush_table()
            doc.image(link_m.group(2), caption=link_m.group(1))
            continue
        # Bare QuickChart URL on its own line
        bare_m = re.match(r"(https://quickchart\.io/\S+)", line.strip())
        if bare_m:
            flush_para()
            flush_table()
            doc.image(bare_m.group(1))
            continue
        para_buf.append(_strip_inline_md(line))

    flush_para()
    flush_table()
    return doc.build()


# ---------------------------------------------------------------------------
# AWS SigV4 helpers (stdlib-only, shared pattern)
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, "s3")
    k = _hmac_sha256(k, "aws4_request")
    return k


def _s3_put_bytes(
    file_key: str,
    body: bytes,
    content_type: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
) -> None:
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    canonical_uri = "/" + urllib.parse.quote(file_key.lstrip("/"), safe="/")
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
    cr = "\n".join(["PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(cr.encode()).hexdigest(),
    ])
    sig = hmac.new(_signing_key(secret_key, date_stamp, region), sts.encode(), hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}"
    )
    req = urllib.request.Request(
        f"https://{host}{canonical_uri}",
        method="PUT", data=body,
        headers={
            "Content-Type": content_type,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": auth,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120):
            pass
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"S3 PUT failed ({e.code}): {e.read().decode('utf-8', errors='replace')}") from e


def _s3_presign_get(
    file_key: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
    expires: int = 86400,  # 24 hours
) -> str:
    """Return a pre-signed S3 GET URL valid for `expires` seconds."""
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")
    canonical_uri = "/" + urllib.parse.quote(file_key.lstrip("/"), safe="/")
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    credential = f"{access_key}/{credential_scope}"

    qs_params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(qs_params.items())
    )
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    cr = "\n".join(["GET", canonical_uri, canonical_qs, canonical_headers, signed_headers, "UNSIGNED-PAYLOAD"])
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(cr.encode()).hexdigest(),
    ])
    sig = hmac.new(_signing_key(secret_key, date_stamp, region), sts.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_qs}&X-Amz-Signature={sig}"


def _s3_env() -> tuple[str, str, str, str]:
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_aws_credentials_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_aws-credentials_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )
    ak     = g("AWS_ACCESS_KEY_ID",     "")
    sk     = g("AWS_SECRET_ACCESS_KEY", "")
    bucket = g("AWS_S3_BUCKET",         "aws-buc-for-orchestrate")
    region = g("AWS_S3_REGION",         "us-east-2")
    return ak, sk, bucket, region


def _wxo_env() -> tuple[str, str, str]:
    def g(key: str, default: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_watsonx_{key}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_watsonx-ai_{key}", "").strip()
            or os.environ.get(key, "").strip()
            or default
        )
    api_key    = g("WATSONX_API_KEY",    "")
    project_id = g("WATSONX_PROJECT_ID", "ab0de2fc-5f68-4e0e-8dc8-d1677b6b689b")
    url        = g("WATSONX_URL",        "https://us-south.ml.cloud.ibm.com")
    return api_key, project_id, url


def _llm_generate(prompt: str, model: str) -> str:
    """Call watsonx.ai ModelInference and return the generated text."""
    from ibm_watsonx_ai import Credentials
    from ibm_watsonx_ai.foundation_models import ModelInference

    api_key, project_id, url = _wxo_env()
    creds = Credentials(url=url, api_key=api_key)
    llm = ModelInference(model_id=model, credentials=creds, project_id=project_id)
    result = llm.generate(prompt=prompt, params={"max_new_tokens": 3000, "temperature": 0.4})
    return result["results"][0]["generated_text"].strip()


_LLM_MODELS = {
    "llama4":   "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
    "llama3":   "meta-llama/llama-3-3-70b-instruct",
    "granite":  "ibm/granite-13b-instruct-v2",
    "mistral":  "mistralai/mistral-large",
}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def pdf_report_tool(
    title: str,
    content: str,
    filename: str = "",
    subtitle: str = "",
    s3_folder: str = "reports",
    chart_urls: str = "",
) -> str:
    """
    Generate a formatted PDF report and upload it to S3.
    Returns a download link valid for 24 hours.

    Use this when the user wants to create a PDF document, report, or summary.

    IMPORTANT — to include charts in the PDF:
        Pass the QuickChart URL(s) in the chart_urls parameter.
        Use the URL exactly as returned by render_pie_chart / render_bar_chart /
        render_time_series etc.  Multiple URLs can be separated by newlines or commas.
        Charts are appended at the end of the report in the order given.

        You can ALSO embed a chart inline inside the content string using the
        markdown image syntax:  ![Chart title](https://quickchart.io/chart?...)

    Content supports simple markdown formatting:
        # Heading 1
        ## Heading 2
        ### Heading 3
        - bullet item
        | Col1 | Col2 |    (tables — first row is the header)
        |------|------|
        | val  | val  |
        ---             (horizontal divider)
        ```
        code block
        ```

    Args:
        title:      The main title shown at the top of the PDF.
        content:    The body text. Use markdown headings, bullets, and tables
                    to structure the report. Separate paragraphs with blank lines.
        filename:   Filename to use in S3, e.g. "monthly_report.pdf".
                    Leave blank to auto-generate from the title.
        subtitle:   Optional subtitle line below the title.
        s3_folder:  S3 folder prefix to store the PDF under (default: "reports").
                    Set to "" to upload at the bucket root.
        chart_urls: One or more QuickChart image URLs to embed in the PDF.
                    Separate multiple URLs with a newline or comma.
                    Copy the URL exactly from the chart tool output.

    Returns:
        A message with the S3 key and a 24-hour download link.
    """
    # --- Input validation ---
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        return "Please provide a title for the PDF report."
    if not content:
        return "Please provide the content (body text) for the PDF report."

    # --- Derive filename ---
    if not filename.strip():
        safe = re.sub(r"[^\w\s-]", "", title.lower())
        safe = re.sub(r"[\s-]+", "_", safe).strip("_")[:50]
        ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{safe}_{ts}.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    s3_key = f"{s3_folder.strip('/')}/{filename}" if s3_folder.strip("/") else filename

    # --- Append chart_urls to content as embedded images ---
    urls_to_append: list[str] = []
    if chart_urls.strip():
        # Accept newline- or comma-separated URLs; also accept raw markdown like
        # ![title](url) or [title](url) — extract just the URL from each token
        for raw in re.split(r"[,\n]+", chart_urls):
            raw = raw.strip()
            if not raw:
                continue
            # Strip markdown wrapper if the LLM pasted "![alt](url)" or "[alt](url)"
            m = re.search(r"\(?(https?://[^\s)]+)\)?", raw)
            if m:
                urls_to_append.append(m.group(1))
    if urls_to_append:
        chart_section = "\n\n## Charts\n\n"
        for u in urls_to_append:
            chart_section += f"![Chart]({u})\n\n"
        content = content + chart_section

    # --- Build PDF ---
    try:
        pdf_bytes = _build_pdf(title, content, subtitle)
    except Exception as e:
        return f"Failed to generate PDF: {e}"

    # --- Upload to S3 ---
    try:
        ak, sk, bucket, region = _s3_env()
        _s3_put_bytes(s3_key, pdf_bytes, "application/pdf", ak, sk, bucket, region)
        download_url = _s3_presign_get(s3_key, ak, sk, bucket, region, expires=86400)
    except RuntimeError as e:
        return f"PDF generated ({len(pdf_bytes):,} bytes) but S3 upload failed: {e}"

    size_kb = len(pdf_bytes) / 1024
    return (
        f"PDF report created and uploaded to S3.\n\n"
        f"  File   : {s3_key}\n"
        f"  Size   : {size_kb:.1f} KB\n\n"
        f"[⬇ Download PDF]({download_url})\n\n"
        f"_Link expires in 24 hours. Use list_s3_files to find it later._"
    )


@tool
def generate_pdf_report_with_llm(
    topic: str,
    instructions: str = "",
    title: str = "",
    filename: str = "",
    subtitle: str = "",
    s3_folder: str = "reports",
    model: str = "llama4",
) -> str:
    """
    Use an LLM to write the content of a PDF report, then generate and upload
    the PDF to S3 in one step.

    Use this when the user asks you to "write a report about …", "create a PDF
    on …", or any similar request where the content should be AI-generated.

    Available LLM models (pass the short name as the `model` parameter):
        - "llama4"   → Meta Llama 4 Maverick 17B (default, best for Arabic + English)
        - "llama3"   → Meta Llama 3.3 70B Instruct
        - "granite"  → IBM Granite 13B Instruct
        - "mistral"  → Mistral Large

    Args:
        topic:        What the report should be about. Be as specific as possible.
                      E.g. "Q1 2025 sales performance for the MENA region".
        instructions: Optional extra guidance for the LLM, such as language,
                      tone, sections to include, length, etc.
                      E.g. "Write in Arabic. Include an executive summary,
                      key findings, and recommendations. Use bullet points."
        title:        Report title shown at the top of the PDF.
                      If blank, the LLM will choose a suitable title.
        filename:     Filename for the S3 object, e.g. "q1_sales_report.pdf".
                      Leave blank to auto-generate from the title.
        subtitle:     Optional subtitle line below the title.
        s3_folder:    S3 prefix / virtual folder (default: "reports").
        model:        Short model name — one of: llama4, llama3, granite, mistral.
                      Defaults to "llama4".

    Returns:
        A message with the S3 key and a 24-hour download link to the generated PDF.
    """
    topic = (topic or "").strip()
    if not topic:
        return "Please provide a topic for the report."

    model_id = _LLM_MODELS.get((model or "llama4").strip().lower())
    if not model_id:
        opts = ", ".join(f'"{k}"' for k in _LLM_MODELS)
        return f"Unknown model '{model}'. Choose one of: {opts}."

    # Build prompt
    is_arabic = bool(instructions and any(
        w in instructions.lower() for w in ["arabic", "عربي", "بالعربي", "ar"]
    ))

    prompt = (
        f"You are a professional report writer. Write a comprehensive, well-structured "
        f"report about the following topic.\n\n"
        f"Topic: {topic}\n"
    )
    if instructions:
        prompt += f"Additional instructions: {instructions}\n"
    prompt += (
        f"\n{'Write the ENTIRE report in Arabic (RTL). ' if is_arabic else ''}"
        f"Format the report using STRICT Markdown rules — follow these EXACTLY:\n"
        f"- Use ## for every section heading (even in Arabic — always use the Latin ## symbol)\n"
        f"- Use ### for sub-section headings\n"
        f"- Use - (hyphen + space) for every bullet point (even in Arabic)\n"
        f"- For numbered lists use: 1. 2. 3. (Latin digits and dot)\n"
        f"- Use | Col1 | Col2 | style tables when presenting tabular data\n"
        f"- Separate every paragraph from the next with a blank line\n"
        f"- Do NOT include a top-level # heading — the title will be added automatically\n"
        f"- Do NOT use bold (**) or italic (*) markers — plain text only\n"
        f"- Do NOT wrap the output in a code block or add any prefix/suffix\n"
        f"\nBegin the report now:"
    )

    # Generate content
    try:
        content = _llm_generate(prompt, model_id)
    except Exception as e:
        return f"LLM generation failed: {e}"

    if not content:
        return "LLM returned empty content. Try a different model or refine the topic."

    # Derive title if not provided
    if not title.strip():
        title_prompt = (
            f"Give a concise professional title (max 10 words) for a report about: {topic}. "
            f"Return ONLY the title, nothing else."
        )
        try:
            title = _llm_generate(title_prompt, model_id).strip('"').strip()
        except Exception:
            title = topic[:80]

    # Derive filename
    if not filename.strip():
        safe = re.sub(r"[^\w\s-]", "", title.lower())
        safe = re.sub(r"[\s-]+", "_", safe).strip("_")[:50]
        ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{safe}_{ts}.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    s3_key = f"{s3_folder.strip('/')}/{filename}" if s3_folder.strip("/") else filename

    # Build PDF
    try:
        pdf_bytes = _build_pdf(title, content, subtitle)
    except Exception as e:
        return f"PDF generation failed: {e}"

    # Upload to S3
    try:
        ak, sk, bucket, region = _s3_env()
        _s3_put_bytes(s3_key, pdf_bytes, "application/pdf", ak, sk, bucket, region)
        download_url = _s3_presign_get(s3_key, ak, sk, bucket, region, expires=86400)
    except RuntimeError as e:
        return f"PDF generated ({len(pdf_bytes):,} bytes) but S3 upload failed: {e}"

    size_kb = len(pdf_bytes) / 1024
    model_label = next((k for k, v in _LLM_MODELS.items() if v == model_id), model_id)
    return (
        f"PDF report generated by {model_label} and uploaded to S3.\n\n"
        f"  File   : {s3_key}\n"
        f"  Size   : {size_kb:.1f} KB\n\n"
        f"[⬇ Download PDF]({download_url})\n\n"
        f"_Link expires in 24 hours. Use list_s3_files to find it later._"
    )

