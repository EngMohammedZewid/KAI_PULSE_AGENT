"""Number parity: the original Orchestrate bank tool vs the current one.

tests/fixtures/legacy_bank_statement_tool.py is the tool exactly as it ran on
Orchestrate (from the repo's first commit). Both versions analyze the same
synthetic statements in seven common layouts; every metric must match,
except where the old version had the "Description"/"cr" column bug, where the
old result is checked to be the known-wrong one.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

from tools.bank_statement_tool import analyze_bank_statement, simulate_mortgage_affordability

FIXTURES = Path(__file__).parent / "fixtures"
STATEMENTS = FIXTURES / "statements"

METRICS = ("months", "monthly_income", "monthly_spending", "monthly_debt", "monthly_savings",
           "avg_monthly_income", "avg_monthly_spending", "avg_monthly_savings",
           "avg_monthly_debt", "avg_balance", "debt_obligations_pct", "top_spending_category",
           "positive_cashflow_months", "savings_growth_pct", "category_totals",
           "transaction_count")

# Layouts where the old column matching read "Description" as the credit column.
OLD_BUG_LAYOUTS = {"debit_credit_description.csv", "signed_amount_description.csv"}


def load_legacy():
    """Import the Orchestrate-era module without the Orchestrate SDK installed."""
    fake = types.ModuleType("ibm_watsonx_orchestrate.agent_builder.tools")
    fake.tool = lambda f=None, **kw: f if f is not None else (lambda g: g)
    for name in ("ibm_watsonx_orchestrate", "ibm_watsonx_orchestrate.agent_builder"):
        sys.modules.setdefault(name, types.ModuleType(name))
    saved = sys.modules.get("ibm_watsonx_orchestrate.agent_builder.tools")
    sys.modules["ibm_watsonx_orchestrate.agent_builder.tools"] = fake
    try:
        spec = importlib.util.spec_from_file_location(
            "legacy_bank_statement_tool", FIXTURES / "legacy_bank_statement_tool.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop("ibm_watsonx_orchestrate.agent_builder.tools", None)
        else:
            sys.modules["ibm_watsonx_orchestrate.agent_builder.tools"] = saved


def _json(raw: str) -> dict:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = load_legacy()
        cls.layouts = sorted(STATEMENTS.glob("*.csv"))

    def analyze_both(self, path: Path) -> tuple[dict, dict]:
        text = path.read_text(encoding="utf-8")
        old = _json(self.legacy.analyze_bank_statement(csv_content=text))
        new = _json(analyze_bank_statement.func(csv_content=text))
        return old, new

    def test_all_layouts_give_the_same_numbers(self):
        reference = None
        for path in self.layouts:
            with self.subTest(layout=path.name):
                old, new = self.analyze_both(path)
                self.assertTrue(new.get("months"), f"new tool found no transactions in {path.name}")
                if reference is None:
                    reference = new
                for key in METRICS:  # every layout describes the same 12 transactions
                    self.assertEqual(new.get(key), reference.get(key), f"{path.name}: {key}")
                if path.name in OLD_BUG_LAYOUTS:
                    self.assertFalse(old.get("avg_monthly_income"),
                                     "old tool was expected to lose income on this layout")
                else:
                    for key in METRICS:
                        self.assertEqual(old.get(key), new.get(key), f"{path.name}: {key}")

    def test_expected_values(self):
        _, new = self.analyze_both(STATEMENTS / "withdrawals_deposits_narration.csv")
        self.assertEqual(new["avg_monthly_income"], 20666.67)
        self.assertEqual(new["avg_monthly_debt"], 3000.0)
        self.assertEqual(new["avg_monthly_spending"], round((1950 + 920 + 5260) / 3, 2))
        self.assertEqual(new["top_spending_category"], "Education")
        self.assertEqual(new["transaction_count"], 12)

    def test_mortgage_matches_where_analysis_matches(self):
        for path in self.layouts:
            if path.name in OLD_BUG_LAYOUTS:
                continue
            with self.subTest(layout=path.name):
                text = path.read_text(encoding="utf-8")
                old_json = self.legacy.analyze_bank_statement(csv_content=text)
                new_json = analyze_bank_statement.func(csv_content=text)
                self.assertEqual(self.legacy.simulate_mortgage_affordability(analysis_json=old_json),
                                 simulate_mortgage_affordability.func(analysis_json=new_json))


if __name__ == "__main__":
    unittest.main()
