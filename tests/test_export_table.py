"""Excel exports store numbers as numbers, so users can sum and format them."""
from __future__ import annotations

import io
import unittest

import openpyxl

from tools.export_table_tool import _excel_value, _normalise_data, _to_xlsx_bytes


class ExcelValueTests(unittest.TestCase):
    def test_numbers_become_numbers(self):
        cases = {
            "20,000": (20000, "#,##0"),
            "1234567.891": (1234567.891, "#,##0.000"),
            "450.5": (450.5, None),
            "-3,000": (-3000, "#,##0"),
            "(1,200)": (-1200, "#,##0"),
            "14.5%": (0.145, "0.0%"),
            "40%": (0.4, "0%"),
            "٢٠٬٠٠٠": (20000, "#,##0"),       # Arabic-Indic digits and separator
            "**62,000**": (62000, "#,##0"),   # markdown bold total
            "0": (0, None),
            "0.75": (0.75, None),
            "2026": (2026, "#,##0"),
        }
        for raw, (value, fmt) in cases.items():
            with self.subTest(raw=raw):
                got, got_fmt = _excel_value(raw)
                self.assertIsInstance(got, (int, float))
                self.assertAlmostEqual(got, value)
                self.assertEqual(got_fmt, fmt)

    def test_number_lookalikes_stay_text(self):
        for raw in ["007", "01012345678", "1234567890123456", "Jan", "2026-01-05", "EGP 20,000",
                    "1,23", "12,34,567", "", "N/A", "الإجمالي"]:
            with self.subTest(raw=raw):
                got, fmt = _excel_value(raw)
                self.assertIsInstance(got, str)
                self.assertIsNone(fmt)

    def test_bold_text_loses_markers(self):
        self.assertEqual(_excel_value("**Total**"), ("Total", None))

    def test_non_text_values_pass_through(self):
        self.assertEqual(_excel_value(12.5), (12.5, None))
        self.assertEqual(_excel_value(None), (None, None))


class WorkbookTests(unittest.TestCase):
    def test_markdown_table_exports_summable_numbers(self):
        rows = _normalise_data("| Month | Income | Share |\n|---|---|---|\n"
                               "| Jan | 20,000 | 32.3% |\n| Mar | 22,000 | 35.5% |\n"
                               "| **Total** | **62,000** | 100% |")
        ws = openpyxl.load_workbook(io.BytesIO(_to_xlsx_bytes(rows))).active
        self.assertEqual([c.value for c in ws[2]], ["Jan", 20000, 0.323])
        self.assertEqual(ws["B4"].value, 62000)
        self.assertEqual(ws["A4"].value, "Total")
        self.assertEqual(ws["B2"].number_format, "#,##0")
        self.assertEqual(ws["C2"].number_format, "0.0%")
        self.assertEqual({ws.cell(r, 2).data_type for r in range(2, 5)}, {"n"})

    def test_json_input_is_unchanged(self):
        rows = _normalise_data('[{"Code": "007", "Amount": 1500.25}]')
        ws = openpyxl.load_workbook(io.BytesIO(_to_xlsx_bytes(rows))).active
        self.assertEqual(ws["A2"].value, "007")
        self.assertEqual(ws["B2"].value, 1500.25)


if __name__ == "__main__":
    unittest.main()
