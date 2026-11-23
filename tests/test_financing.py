"""Installment plans, loan capacity and file aggregation: every number computed in code."""
from __future__ import annotations

import datetime as dt
import io
import tempfile
import unittest
import uuid

import openpyxl
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from kai.files import LocalFileStore
from kai.graph import SPECIALIST_TOOLS
from kai.state import KaiState
from kai.tools.aggregate import aggregate_file, aggregate_rows
from kai.tools.financing import (FINANCING_TOOLS, installment_plan, loan_capacity,
                                 monthly_payment, principal_for_payment)
from tools.bank_statement_tool import affordable_range
from tools.export_table_tool import _normalise_data, _to_xlsx_bytes

# Metrics of the two demo personas, as produced by analyze_bank_statement
MARIAM = {"currency": "EGP", "months": ["2026-06"], "avg_monthly_income": 38000.0,
          "avg_monthly_debt": 4500.0, "avg_monthly_savings": 10337.5}
KARIM = {"currency": "EGP", "months": ["2026-06"], "avg_monthly_income": 41666.67,
         "avg_monthly_debt": 12500.0, "avg_monthly_savings": 12416.67}

PORTFOLIO = """Week,Bank Applications,Bank Decision Minutes,Telco Applications
2026-01-05,91,2516.4,232
2026-01-12,87,2325.6,233
2026-02-02,95,2521.7,210
2026-02-09,100,2400.0,240
2026-07-06,180,9.5,520
"""


class FormulaTests(unittest.TestCase):
    def test_affordable_range_matches_the_demo_figures(self):
        def r(m, dti):
            x = affordable_range(m["avg_monthly_income"], m["avg_monthly_debt"], m["avg_monthly_savings"], dti)
            return x["low"], x["high"]

        self.assertEqual(r(MARIAM, 40), (6978, 9304))
        self.assertEqual(r(MARIAM, 50), (6978, 9304), "capped by savings")
        self.assertEqual(r(KARIM, 40), (2969, 3958))
        self.assertEqual(r(KARIM, 50), (5938, 7917))

    def test_payments(self):
        self.assertEqual(monthly_payment(60000, 12), 5000)
        self.assertAlmostEqual(monthly_payment(100000, 12, 12), 8884.88, places=2)
        p = principal_for_payment(9304, 60, 24)
        self.assertAlmostEqual(monthly_payment(p, 60, 24), 9304, places=6)
        self.assertTrue(320_000 < p < 330_000)
        self.assertEqual(principal_for_payment(1000, 10), 10000)


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = LocalFileStore(tmp.name)
        builder = StateGraph(KaiState)
        builder.add_node("tools", ToolNode([*FINANCING_TOOLS, aggregate_file]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())
        self.cfg = {"configurable": {"thread_id": uuid.uuid4().hex, "file_store": self.store}}

    def with_analysis(self, metrics):
        self.graph.update_state(self.cfg, {"analysis": {"file_ids": ["f_1"], "metrics": metrics}})

    def call(self, name, **args):
        msg = AIMessage("", tool_calls=[{"name": name, "args": args, "id": "c_" + uuid.uuid4().hex[:6]}])
        return str(self.graph.invoke({"messages": [msg]}, self.cfg)["messages"][-1].content)


class InstallmentTests(ToolTestCase):
    def test_karim_counter_offer(self):
        self.with_analysis(KARIM)
        out = self.call("installment_plan", price=60000, tenors="12,18,24", product="phone")
        self.assertIn("EGP 2,969 – EGP 3,958", out)
        self.assertIn("| 12 | EGP 5,000 | EGP 60,000 | ❌ above range |", out)
        self.assertIn("| 18 | EGP 3,333 |", out)
        self.assertIn("| 24 | EGP 2,500 |", out)
        self.assertIn("Recommended: 18 months at EGP 3,333", out)
        # 60,000 - 3,958 x 12 = 12,504: the down payment comes from the tool, not the model
        self.assertIn("To keep 12 months: a down payment of about EGP 12,504", out)
        self.assertNotIn("To keep 18 months", out)
        self.assertIn("الموافقة النهائية", out)

    def test_mariam_fits(self):
        self.with_analysis(MARIAM)
        out = self.call("installment_plan", price=85000, tenors="24")
        self.assertIn("| 24 | EGP 3,542 | EGP 85,000 | ✅ fits |", out)

    def test_nothing_fits_suggests_down_payment_or_term(self):
        self.with_analysis(KARIM)
        out = self.call("installment_plan", price=60000, tenors="6,12")
        self.assertIn("No requested term fits", out)
        self.assertIn("down payment of about EGP 12,504 over 12 months", out)
        self.assertIn("a term of 16 months", out)

    def test_what_if_dti(self):
        self.with_analysis(KARIM)
        self.assertIn("EGP 5,938 – EGP 7,917", self.call("installment_plan", price=60000, max_dti=50))

    def test_bad_input_and_missing_analysis(self):
        self.assertIn("Run analyze_statements first", self.call("installment_plan", price=1000))
        self.with_analysis(KARIM)
        self.assertIn("Error", self.call("installment_plan", price=1000, down_payment=1000))
        self.assertIn("Error", self.call("installment_plan", price=1000, tenors="0"))


class LoanTests(ToolTestCase):
    def test_mariam_loan_at_24_percent(self):
        self.with_analysis(MARIAM)
        out = self.call("loan_capacity", tenors="60", annual_rate=24)
        expected = principal_for_payment(9304, 60, 24)
        self.assertIn(f"| 60 | EGP 9,304 | EGP {expected:,.0f} |", out)

    def test_installment_above_range_is_flagged(self):
        self.with_analysis(KARIM)
        out = self.call("loan_capacity", tenors="48", annual_rate=24, monthly_installment=5000)
        self.assertIn("above the affordable range", out)


class AggregateTests(ToolTestCase):
    ROWS = [{"d": "2026-01-05", "v": "10", "c": "Web"}, {"d": "2026-01-20", "v": "1,000", "c": "Store"},
            {"d": "05/02/2026", "v": "5", "c": "Web"}, {"d": "oops", "v": "7", "c": "Web"}]

    def test_aggregate_rows(self):
        res, skipped = aggregate_rows(self.ROWS, "d", ["v"], "month", "sum")
        self.assertEqual(res, [("2026-01", [1010.0]), ("2026-02", [5.0])])
        self.assertEqual(skipped, 1)
        self.assertEqual(aggregate_rows(self.ROWS, "d", ["v"], "month", "avg")[0][0], ("2026-01", [505.0]))
        self.assertEqual(aggregate_rows(self.ROWS, "c", ["v"], "none", "count")[0],
                         [("Store", [1.0]), ("Web", [3.0])])
        only_feb, _ = aggregate_rows(self.ROWS, "d", ["v"], "month", "sum", start=dt.date(2026, 2, 1))
        self.assertEqual(only_feb, [("2026-02", [5.0])])
        self.assertEqual(aggregate_rows(self.ROWS, "d", ["v"], "week", "sum")[0][0][0], "2026-W02")

    def test_aggregate_file_table_exports_as_numbers(self):
        ref = self.store.put("portfolio.csv", PORTFOLIO.encode())
        self.graph.update_state(self.cfg, {"files": [ref]})
        out = self.call("aggregate_file", file_id=ref["id"], group_column="Week",
                        value_columns="Bank Applications, Telco Applications")
        self.assertIn("| 2026-01 | 178 | 465 |", out)
        self.assertIn("| 2026-07 | 180 | 520 |", out)
        self.assertIn("| Total | 553 | 1,435 |", out)
        table = out.split("\n\n")[1]
        ws = openpyxl.load_workbook(io.BytesIO(_to_xlsx_bytes(_normalise_data(table)))).active
        self.assertEqual([c.value for c in ws[2]], ["2026-01", 178, 465], "numbers, not text")
        self.assertEqual([c.value for c in ws[ws.max_row]], ["Total", 553, 1435])
        avg = self.call("aggregate_file", file_id=ref["id"], group_column="Week",
                        value_columns="Bank Decision Minutes", aggregate="avg")
        self.assertIn("| 2026-01 | 2,421 |", avg)
        self.assertNotIn("Total", avg)

    def test_unknown_column_lists_the_real_ones(self):
        ref = self.store.put("portfolio.csv", PORTFOLIO.encode())
        self.graph.update_state(self.cfg, {"files": [ref]})
        out = self.call("aggregate_file", file_id=ref["id"], group_column="Week", value_columns="Nope")
        self.assertIn("unknown column(s) Nope", out)
        self.assertIn("Telco Applications", out)


class WiringTests(unittest.TestCase):
    def test_specialists_have_the_new_tools(self):
        names = {k: {t.name for t in v} for k, v in SPECIALIST_TOOLS.items()}
        self.assertTrue({"installment_plan", "loan_capacity"} <= names["finance"])
        self.assertIn("aggregate_file", names["data"])
        self.assertIn("aggregate_file", names["documents"])

    def test_model_never_sees_state_or_config(self):
        for t in [*FINANCING_TOOLS, aggregate_file]:
            visible = set(t.tool_call_schema.model_json_schema().get("properties", {}))
            self.assertFalse(visible & {"state", "config"}, t.name)


if __name__ == "__main__":
    unittest.main()
