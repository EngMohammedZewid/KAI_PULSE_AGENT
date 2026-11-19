"""Command line runner:

    python -m evals.run [--live] [--case ID ...] [--report PATH]

Exits 1 if any case fails. Live mode uses kai.models.get_chat_model(), so set
KAI_MODEL_PROVIDER and its credentials first.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from evals.harness import dumps, load_cases, report_json, run_case


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--live", action="store_true", help="use the real chat model")
    parser.add_argument("--case", action="append", default=[], help="run only these case ids")
    parser.add_argument("--report", type=Path, help="write a JSON report here")
    args = parser.parse_args(argv)

    cases = [c for c in load_cases() if not args.case or c["id"] in args.case]
    factory = None
    if args.live:
        from kai.models import get_chat_model

        factory = get_chat_model

    results = []
    for case in cases:
        r = run_case(case, live=args.live, model_factory=factory)
        results.append(r)
        status = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
        print(f"{status}  {r.id}" + (f"  ({r.skipped})" if r.skipped else ""))
        for t in r.turns:
            mark = "  ok " if not t.failures else "  !! "
            print(f"{mark} {t.label}  route={t.route or '-'} tools={','.join(t.tools) or '-'} "
                  f"llm={t.llm_calls} {t.seconds:.1f}s")
            for f in t.failures:
                print(f"       - {f}")

    report = report_json(results, "live" if args.live else "scripted")
    print(f"\n{report['passed']} passed, {report['failed']} failed, {report['skipped']} skipped")
    path = args.report or Path("evals/reports") / f"{datetime.now():%Y%m%d-%H%M%S}-{report['mode']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(report), encoding="utf-8")
    print(f"report: {path}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
