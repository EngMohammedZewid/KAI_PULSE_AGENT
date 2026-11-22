"""Report layout: one localized date line, localized charts heading, charts drawn with Chart.js 4."""
from __future__ import annotations

import datetime
import io
import json
import unittest
from unittest import mock

from pypdf import PdfReader

import tools.pdf_tool as pdf_tool
from kai.tools.documents import with_charts
from tests.test_tools import STATEMENT_CSV
from tools import chart_analytics_tools
from tools._chart_spec import capture, records_chart, to_spec
from tools.bank_statement_tool import analyze_bank_statement, render_financial_chart

WHEN = datetime.datetime(2026, 11, 22, 8, 29, tzinfo=datetime.timezone.utc)


class DateLineTests(unittest.TestCase):
    def test_line_is_in_the_report_language(self):
        self.assertEqual(pdf_tool._generated_line(False, WHEN), "Generated: November 22, 2026  08:29 UTC")
        self.assertEqual(pdf_tool._generated_line(True, WHEN), "تاريخ الإنشاء: 22 نوفمبر 2026، 08:29 UTC")

    def _report_text(self, language: str) -> str:
        saved = {}
        with mock.patch.object(pdf_tool, "_s3_env", return_value=("a", "s", "b", "r")), \
             mock.patch.object(pdf_tool, "_s3_put", side_effect=lambda key, body, *a, **k: saved.update(pdf=body)), \
             mock.patch.object(pdf_tool, "_s3_presign", return_value="https://example.test/r.pdf"):
            out = pdf_tool.create_pdf_report.func(content="## Summary\n\nAll good.", title="Report",
                                                  topic="Report", language=language)
        self.assertIn("Download", out)
        return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(saved["pdf"])).pages)

    def test_pdf_has_a_single_date_line(self):
        text = self._report_text("English")
        self.assertEqual(text.count("Generated:"), 1)
        self.assertEqual(text.count("UTC"), 1, "no second timestamp as a subtitle")

    def test_arabic_pdf_has_no_english_date_line(self):
        text = self._report_text("Arabic")
        self.assertNotIn("Generated", text)
        self.assertEqual(text.count("UTC"), 1)


class ChartsHeadingTests(unittest.TestCase):
    CHART = [{"title": "Savings", "url": "https://quickchart.io/chart/render/a", "source": "s"}]

    def test_heading_follows_language(self):
        self.assertIn("## Charts\n", with_charts("Body", self.CHART))
        self.assertIn("## الرسوم البيانية\n", with_charts("Body", self.CHART, "Arabic"))
        self.assertEqual(with_charts("Body ![x](https://quickchart.io/chart/render/a)", self.CHART, "Arabic"),
                         "Body ![x](https://quickchart.io/chart/render/a)", "already embedded: unchanged")


class ChartVersionTests(unittest.TestCase):
    def test_quickchart_is_asked_for_chart_js_4(self):
        sent = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data))
            return Resp(b'{"url": "https://quickchart.io/chart/render/x"}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            chart_analytics_tools._chart_url({"type": "bar", "data": {"labels": [], "datasets": []}})
        self.assertEqual(sent["version"], "4")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            fallback = chart_analytics_tools._chart_url({"type": "bar", "data": {"labels": [], "datasets": []}})
        self.assertIn("v=4", fallback)

    def test_arabic_titles_are_hidden_in_the_png_only(self):
        drawn = []
        helper = records_chart(lambda config, *a, **k: drawn.append(config) or "https://quickchart.io/x")
        config = {"type": "bar", "data": {"labels": ["Jan"], "datasets": [{"data": [1]}]},
                  "options": {"plugins": {"title": {"display": True, "text": "الادخار الشهري"}},
                              "scales": {"x": {"title": {"display": True, "text": "Month"}},
                                         "y": {"title": {"display": True, "text": "المبلغ"}}}}}
        with capture() as found:
            helper(config)
        png = drawn[0]["options"]
        self.assertFalse(png["plugins"]["title"]["display"])
        self.assertTrue(png["scales"]["x"]["title"]["display"], "English axis title stays")
        self.assertFalse(png["scales"]["y"]["title"]["display"])
        self.assertIs(found[0][1], config, "the original, with its Arabic title, is what gets recorded")
        self.assertTrue(config["options"]["plugins"]["title"]["display"])
        self.assertEqual(to_spec(config)["title"], "الادخار الشهري")

    def test_financial_value_axes_start_at_zero(self):
        analysis = analyze_bank_statement.func(csv_files=json.dumps([STATEMENT_CSV]))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), capture() as found:
            for kind in ("savings", "income_vs_spending", "cashflow"):
                render_financial_chart.func(analysis_json=analysis, chart_type=kind)
        self.assertEqual(len(found), 3)
        for _, config in found:
            self.assertTrue(config["options"]["scales"]["y"]["beginAtZero"])


if __name__ == "__main__":
    unittest.main()
