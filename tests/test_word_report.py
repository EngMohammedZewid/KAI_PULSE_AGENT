"""Word reports embed Markdown images (the charts create_report appends) instead of printing them as text."""
from __future__ import annotations

import io
import struct
import unittest
import zlib
from unittest import mock

from docx import Document
from docx.shared import Pt

from tools import pdf_tool

CHART = "https://quickchart.io/chart?c={type:'bar',data:{labels:['Jan','Feb'],datasets:[{data:[1,2]}]}}"


def png(width: int, height: int) -> bytes:
    """A valid grey PNG of the given size."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def build(content: str, image: bytes | Exception) -> Document:
    fetch = mock.Mock(side_effect=image) if isinstance(image, Exception) else mock.Mock(return_value=image)
    with mock.patch.object(pdf_tool, "_download_image", fetch):
        data = pdf_tool._build_docx("Report", content)
    return Document(io.BytesIO(data))


def texts(doc: Document) -> str:
    return "\n".join(p.text for p in doc.paragraphs)


class WordImageTests(unittest.TestCase):
    def test_chart_is_embedded_with_caption(self):
        doc = build(f"## Charts\n\n![Monthly income]({CHART})\n", png(600, 300))
        self.assertEqual(len(doc.inline_shapes), 1)
        self.assertNotIn("![", texts(doc))
        self.assertNotIn("quickchart", texts(doc))
        self.assertIn("Monthly income", texts(doc))

    def test_tall_image_is_capped(self):
        doc = build(f"![Tall]({CHART})", png(100, 1000))
        shape = doc.inline_shapes[0]
        self.assertLessEqual(shape.height, Pt(160) + 1)
        self.assertAlmostEqual(shape.width / shape.height, 0.1, places=2)  # aspect ratio kept

    def test_failed_download_leaves_a_note_not_the_raw_link(self):
        doc = build(f"Intro\n\n![Chart]({CHART})\n\nOutro", OSError("timed out"))
        self.assertEqual(len(doc.inline_shapes), 0)
        body = texts(doc)
        self.assertIn("[Chart could not be loaded: timed out]", body)
        self.assertNotIn("quickchart", body)
        self.assertIn("Outro", body)

    def test_unsupported_image_bytes_leave_no_empty_paragraph(self):
        doc = build(f"![Chart]({CHART})", b"<svg></svg>")
        self.assertEqual(len(doc.inline_shapes), 0)
        self.assertIn("Chart could not be loaded", texts(doc))
        self.assertFalse(any(p.text == "" and p.alignment is not None for p in doc.paragraphs))


if __name__ == "__main__":
    unittest.main()
