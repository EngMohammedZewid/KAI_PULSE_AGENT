"""Arabic PDFs use the Amiri files shipped with the app, with no internet access."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from tools import pdf_tool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def no_network(*args, **kwargs):
    raise AssertionError("the font loader tried to use the network")


class ArabicFontTests(unittest.TestCase):
    def setUp(self):
        saved = dict(pdf_tool._ARABIC_FONT_CACHE)
        pdf_tool._ARABIC_FONT_CACHE.clear()
        self.addCleanup(lambda: (pdf_tool._ARABIC_FONT_CACHE.clear(), pdf_tool._ARABIC_FONT_CACHE.update(saved)))
        patcher = mock.patch.object(pdf_tool.urllib.request, "urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shipped_fonts_are_used_offline(self):
        with mock.patch.dict(os.environ, {"KAI_FONT_DIR": ""}):
            self.assertEqual(pdf_tool._get_amiri(), os.path.join(ROOT, "amiri_regular.ttf"))
            self.assertEqual(pdf_tool._get_amiri(bold=True), os.path.join(ROOT, "amiri_bold.ttf"))

    def test_font_dir_setting_comes_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "amiri_regular.ttf")
            with open(path, "wb") as f:
                f.write(b"\0" * 60_000)
            with mock.patch.dict(os.environ, {"KAI_FONT_DIR": tmp}):
                self.assertEqual(pdf_tool._get_amiri(), path)

    def test_arabic_pdf_embeds_amiri_offline(self):
        with mock.patch.dict(os.environ, {"KAI_FONT_DIR": ""}):
            pdf = pdf_tool._build_pdf("تقرير مالي", "## الملخص\n\nمتوسط الدخل الشهري 20,667 جنيه\n\n- الادخار جيد")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"Amiri", pdf)


class ArabicTableTests(unittest.TestCase):
    """Arabic in PDF tables uses Amiri and shaping; markdown markers never show."""

    def table(self, rows):
        from reportlab import platypus

        seen = {}
        real_table, real_style = platypus.Table, platypus.TableStyle

        def table_spy(data, **kw):
            seen["data"] = data
            return real_table(data, **kw)

        def style_spy(cmds):
            seen["cmds"] = cmds
            return real_style(cmds)

        with mock.patch.dict(os.environ, {"KAI_FONT_DIR": ""}), \
             mock.patch.object(platypus, "Table", table_spy), mock.patch.object(platypus, "TableStyle", style_spy):
            doc = pdf_tool._PDF()
            self.assertTrue(doc._ar_ok, "Amiri should load from the shipped files")
            doc.table(rows)
            self.assertTrue(doc.build().startswith(b"%PDF"))
        fonts = {(c[1], c[3]) for c in seen["cmds"] if c[0] == "FONTNAME" and c[1] == c[2]}
        return seen["data"], fonts, seen["cmds"]

    def test_arabic_table_uses_amiri_and_runs_right_to_left(self):
        data, fonts, cmds = self.table([["الشهر", "الدخل"], ["يناير", "20,000"], ["**الإجمالي**", "**62,000**"]])
        self.assertIn(("ALIGN", (0, 0), (-1, -1), "RIGHT"), cmds)
        # columns reversed: the first column ("month") is drawn on the right
        self.assertEqual(data[1][0], "20,000")
        self.assertEqual(data[1][1], pdf_tool._ar_bidi(pdf_tool._ar_reshape("يناير")))
        self.assertIn(((1, 0), "Amiri-Bold"), fonts)   # Arabic header cell
        self.assertIn(((1, 1), "Amiri"), fonts)        # Arabic body cell
        self.assertIn(((1, 2), "Amiri-Bold"), fonts)   # **bold** Arabic cell
        self.assertIn(((0, 2), "Helvetica-Bold"), fonts)  # **bold** number
        self.assertFalse(any("*" in cell for row in data for cell in row))

    def test_english_table_keeps_its_order_and_bolds_total(self):
        data, fonts, cmds = self.table([["Month", "Income"], ["Jan", "20,000"], ["**Total**", "62,000"]])
        self.assertEqual(data[0], ["Month", "Income"])
        self.assertEqual(data[2][0], "Total")
        self.assertIn(((0, 2), "Helvetica-Bold"), fonts)
        self.assertNotIn(("ALIGN", (0, 0), (-1, -1), "RIGHT"), cmds)

    def test_long_arabic_cell_wraps_inside_its_column(self):
        long = "نسبة الدين إلى الدخل منخفضة جدا مقارنة بالحد الأقصى المسموح به لدى معظم البنوك المحلية"
        data, _, _ = self.table([["البند", "الملاحظة"], ["الدين", long]])
        self.assertGreater(data[1][0].count("\n"), 0)

    def test_arabic_paragraph_drops_bold_markers(self):
        shaped = []
        with mock.patch.dict(os.environ, {"KAI_FONT_DIR": ""}), \
             mock.patch.object(pdf_tool, "_ar_reshape", side_effect=lambda t: shaped.append(t) or t):
            pdf_tool._PDF().paragraph("متوسط الدخل **20,667 جنيه** شهريا")
        self.assertEqual(shaped, ["متوسط الدخل 20,667 جنيه شهريا"])


if __name__ == "__main__":
    unittest.main()
