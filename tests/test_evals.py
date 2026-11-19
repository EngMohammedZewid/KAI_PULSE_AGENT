"""Runs every eval case with the scripted model, so the cases and harness stay valid.

The live run against the real model is `python -m evals.run --live`.
"""
from __future__ import annotations

import unittest

from evals.harness import load_cases, run_case


class ScriptedEvalTests(unittest.TestCase):
    def test_all_cases_pass_scripted(self):
        for case in load_cases():
            with self.subTest(case=case["id"]):
                result = run_case(case, live=False)
                failures = [f"{t.label}: {f}" for t in result.turns for f in t.failures]
                self.assertTrue(result.passed, "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
