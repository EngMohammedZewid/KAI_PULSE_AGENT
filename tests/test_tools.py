"""Phase 1 checks: every tool loads as a LangChain tool and the offline ones work.

Run from the repo root:  python -m unittest discover -s tests -v
Tests that would call S3, QuickChart, SendGrid, Cognos or an LLM are left out.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from langchain_core.tools import BaseTool

from tools.bank_statement_tool import (
    _find_col,
    analyze_bank_statement,
    simulate_mortgage_affordability,
)
from tools.cognos_analytics_tools import cognos_render_widget_iframe
from tools.hello_tool import hello_tool
from tools.registry import all_tools, load_tools

REPO = Path(__file__).resolve().parents[1]

STATEMENT_CSV = (
    "Date,Description,Debit,Credit,Balance\n"
    "2026-01-01,Salary,,20000,20000\n"
    "2026-01-05,Supermarket,1500,,18500\n"
    "2026-01-10,Loan installment,3000,,15500\n"
    "2026-02-01,Salary,,20000,35500\n"
    "2026-02-07,Restaurant,800,,34700\n"
    "2026-02-10,Loan installment,3000,,31700\n"
    "2026-03-01,Salary,,22000,53700\n"
    "2026-03-03,School fees,5000,,48700\n"
    "2026-03-10,Loan installment,3000,,45700\n"
)


class RegistryTests(unittest.TestCase):
    def test_all_tools_load_with_unique_names(self):
        tools = all_tools()
        names = [t.name for t in tools]
        self.assertGreaterEqual(len(tools), 36)
        self.assertEqual(len(names), len(set(names)))
        for t in tools:
            self.assertIsInstance(t, BaseTool)
            self.assertTrue(t.description.strip(), f"{t.name} has no description")

    def test_specialist_groups_present(self):
        groups = load_tools()
        for g in ("finance", "documents", "data", "cognos"):
            self.assertIn(g, groups)
        self.assertEqual(
            {t.name for t in groups["finance"]},
            {"analyze_bank_statement", "simulate_mortgage_affordability",
             "render_financial_chart"},
        )

    def test_args_documented_for_parsed_tools(self):
        # Tools whose docstring parsed per argument expose the description in the schema.
        schema = simulate_mortgage_affordability.args
        self.assertIn("max_dti", schema)
        self.assertTrue(schema["max_dti"].get("description"))


class BankStatementTests(unittest.TestCase):
    def test_column_detection(self):
        cases = {
            ("Date", "Description", "Debit", "Credit", "Balance"):
                ("Description", "Debit", "Credit"),
            ("Transaction Date", "Narration", "Withdrawals", "Deposits", "Running Balance"):
                ("Narration", "Withdrawals", "Deposits"),
        }
        for headers, (desc, debit, credit) in cases.items():
            h = list(headers)
            self.assertEqual(_find_col(h, "description"), desc)
            self.assertEqual(_find_col(h, "debit"), debit)
            # Regression: "cr" used to match inside "Description".
            self.assertEqual(_find_col(h, "credit"), credit)

    def test_arabic_headers(self):
        h = ["تاريخ", "بيان", "خصم", "إيداع", "رصيد"]
        self.assertEqual(_find_col(h, "credit"), "إيداع")
        self.assertEqual(_find_col(h, "debit"), "خصم")

    def test_metrics(self):
        d = json.loads(analyze_bank_statement.invoke({"csv_content": STATEMENT_CSV}))
        self.assertEqual(d["months"], ["2026-01", "2026-02", "2026-03"])
        self.assertAlmostEqual(d["avg_monthly_income"], 20666.67, places=2)
        self.assertAlmostEqual(d["avg_monthly_spending"], 2433.33, places=2)
        self.assertAlmostEqual(d["avg_monthly_debt"], 3000.0, places=2)
        self.assertAlmostEqual(d["avg_monthly_savings"], 15233.33, places=2)
        self.assertEqual(d["debt_obligations_pct"], 14.5)
        self.assertEqual(d["top_spending_category"], "Education")
        self.assertEqual(d["positive_cashflow_months"], 3)
        self.assertEqual(d["savings_growth_pct"], -9.7)

    def test_multi_file_merge_matches_single_file(self):
        lines = STATEMENT_CSV.strip().split("\n")
        header, rows = lines[0], lines[1:]
        files = ["\n".join([header] + rows[:3]), "\n".join([header] + rows[3:])]
        merged = json.loads(analyze_bank_statement.invoke({"csv_files": json.dumps(files)}))
        single = json.loads(analyze_bank_statement.invoke({"csv_content": STATEMENT_CSV}))
        self.assertEqual(merged["avg_monthly_income"], single["avg_monthly_income"])
        self.assertEqual(merged["transaction_count"], single["transaction_count"])

    def test_mortgage_range(self):
        analysis = analyze_bank_statement.invoke({"csv_content": STATEMENT_CSV})
        out = simulate_mortgage_affordability.invoke({"analysis_json": analysis})
        # available = 20,666.67 x 40% - 3,000 = 5,266.67
        # max = min(95% of available, 90% of savings) = 5,003; min = 75% of max = 3,753
        self.assertIn("EGP 3,753", out)
        self.assertIn("EGP 5,003", out)


class SimpleToolTests(unittest.TestCase):
    def test_hello(self):
        self.assertIn("Hello, Ada!", hello_tool.invoke({"name": "Ada"}))

    def test_cognos_iframe(self):
        html = cognos_render_widget_iframe.invoke({"embed_url": "https://example.com/d"})
        self.assertTrue(html.startswith("<iframe"))
        self.assertIn('src="https://example.com/d"', html)


class NoSecretsTests(unittest.TestCase):
    """Guards against credentials creeping back into the code."""

    PATTERNS = [
        re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS access key id
        re.compile(r"\bsk-[A-Za-z0-9]{24,}"),                  # DeepSeek / OpenAI style
        re.compile(r"""(?:KEY|SECRET|PASSWORD|TOKEN)["']?\s*,\s*["'][A-Za-z0-9/+_\-]{24,}["']"""),
        # A long literal used as an env fallback; UUIDs (project / agent ids) are config, not secrets.
        re.compile(r"""^\s*or\s+["'](?![0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}["'])[A-Za-z0-9_\-]{30,}["']\s*$""", re.M),
    ]

    def test_no_hardcoded_secrets(self):
        offenders = []
        for path in list(REPO.glob("tools/*.py")) + list(REPO.glob("*.py")) + list(REPO.glob("*.sh")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for p in self.PATTERNS:
                if p.search(text):
                    offenders.append(f"{path.name}: {p.pattern[:30]}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
