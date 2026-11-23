"""Financing tools: installment plans and loan capacity, priced in code.

Both read the stored statement analysis and compare offers against the same
affordable range as the affordability tool (tools.bank_statement_tool.
affordable_range), so every number the user sees is computed here, never by
the model. Payments use the standard annuity formula; a 0% rate means equal
installments (price / months).
"""
from __future__ import annotations

from typing import Annotated, Optional

from langgraph.prebuilt import InjectedState

from tools._tooling import tool
from tools.bank_statement_tool import affordable_range

DISCLAIMER = ("Indicative only, based on the statement analysis. Final approval depends on "
              "credit history, collateral and the lender's policy.")
DISCLAIMER_AR = ("هذه الأرقام تقديرية بناءً على تحليل كشف الحساب. الموافقة النهائية تعتمد على "
                 "السجل الائتماني والضمانات وسياسة الجهة المقرضة.")


def monthly_payment(principal: float, months: int, annual_rate_pct: float = 0.0) -> float:
    """Annuity payment; equal installments when the rate is 0."""
    if months <= 0:
        raise ValueError("months must be positive")
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal / months
    return principal * r / (1 - (1 + r) ** -months)


def principal_for_payment(payment: float, months: int, annual_rate_pct: float = 0.0) -> float:
    """Largest principal a monthly payment can repay over `months`."""
    if months <= 0:
        raise ValueError("months must be positive")
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return payment * months
    return payment * (1 - (1 + r) ** -months) / r


def _tenors(raw: str, default: str) -> list[int]:
    out = []
    for part in str(raw or default).replace("،", ",").split(","):
        part = part.strip()
        if part:
            n = int(float(part))
            if not 1 <= n <= 360:
                raise ValueError(f"tenor {n} must be between 1 and 360 months")
            if n not in out:
                out.append(n)
    if not out:
        raise ValueError("give at least one tenor in months, e.g. '12,18,24'")
    return sorted(out)


def _range(state: dict, max_dti: float) -> tuple[Optional[dict], str, str]:
    analysis = state.get("analysis")
    if not analysis:
        return None, "", "Error: no analysis yet. Run analyze_statements first."
    m = analysis["metrics"]
    income = float(m.get("avg_monthly_income") or 0)
    if income <= 0:
        return None, "", "Error: the analysis has no income, so affordability can't be estimated."
    if not 1 <= max_dti <= 100:
        return None, "", "Error: max_dti must be a percentage between 1 and 100."
    rng = affordable_range(income, float(m.get("avg_monthly_debt") or 0),
                           float(m.get("avg_monthly_savings") or 0), max_dti)
    return rng, str(m.get("currency") or "EGP"), ""


def _money(cur: str, v: float) -> str:
    return f"{cur} {v:,.0f}"


@tool
def installment_plan(
    price: float,
    state: Annotated[dict, InjectedState],
    down_payment: float = 0.0,
    tenors: str = "12,18,24",
    annual_rate: float = 0.0,
    max_dti: float = 40.0,
    product: str = "",
) -> str:
    """Price a purchase in installments (e.g. a phone or device) and check each term against the customer's affordable range from the stored analysis.

    Args:
        price: Full price of the product, in the statement currency.
        down_payment: Amount paid upfront. Default 0.
        tenors: Comma-separated terms in months to compare, e.g. "12,18,24".
        annual_rate: Annual interest or markup in percent. 0 means equal interest-free installments.
        max_dti: Debt-to-income cap in percent used for the affordable range. Default 40.
        product: Optional product name for the reply.
    """
    rng, cur, err = _range(state, max_dti)
    if err:
        return err
    try:
        terms = _tenors(tenors, "12,18,24")
    except ValueError as e:
        return f"Error: {e}"
    if price <= 0 or down_payment < 0 or down_payment >= price:
        return "Error: price must be positive and the down payment must be less than the price."
    if not 0 <= annual_rate <= 100:
        return "Error: annual_rate must be between 0 and 100."

    financed = price - down_payment
    low, high = rng["low"], rng["high"]
    rows, fits = [], []
    for n in terms:
        pay = monthly_payment(financed, n, annual_rate)
        ok = pay <= high
        if ok:
            fits.append((n, pay))
        rows.append(f"| {n} | {_money(cur, pay)} | {_money(cur, pay * n + down_payment)} | "
                    f"{'✅ fits' if ok else '❌ above range'} |")

    name = product or "the purchase"
    lines = [
        f"Installment plan for {name}: price {_money(cur, price)}, down payment {_money(cur, down_payment)}, "
        f"financed {_money(cur, financed)}, rate {annual_rate:g}% a year.",
        f"Affordable new installment at {max_dti:g}% DTI: {_money(cur, low)} – {_money(cur, high)} a month.",
        "",
        "| Months | Monthly installment | Total paid | Verdict |",
        "| --- | --- | --- | --- |",
        *rows,
        "",
    ]
    for n in terms:
        if monthly_payment(financed, n, annual_rate) > high:
            need = price - principal_for_payment(high, n, annual_rate)
            lines.append(f"To keep {n} months: a down payment of about {_money(cur, need)} "
                         f"brings the installment to {_money(cur, high)}.")
    if fits:
        n, pay = fits[0]
        lines.append(f"Recommended: {n} months at {_money(cur, pay)}, the shortest term within the range.")
    else:
        longest = terms[-1]
        need_down = max(price - principal_for_payment(high, longest, annual_rate), 0)
        min_n = next((k for k in range(longest + 1, 121)
                      if monthly_payment(financed, k, annual_rate) <= high), None)
        options = [f"a down payment of about {_money(cur, need_down)} over {longest} months"]
        if min_n:
            options.append(f"a term of {min_n} months")
        lines.append("No requested term fits. Options: " + ", or ".join(options) + ".")
    lines += ["", DISCLAIMER, DISCLAIMER_AR]
    return "\n".join(lines)


@tool
def loan_capacity(
    state: Annotated[dict, InjectedState],
    tenors: str = "36,48,60",
    annual_rate: float = 0.0,
    max_dti: float = 40.0,
    monthly_installment: float = 0.0,
) -> str:
    """Largest personal loan the customer can repay for each term, from the stored analysis and the lender's interest rate.

    Args:
        tenors: Comma-separated loan terms in months, e.g. "36,48,60".
        annual_rate: Annual interest rate in percent, e.g. 24. 0 means interest-free.
        max_dti: Debt-to-income cap in percent used for the affordable range. Default 40.
        monthly_installment: Installment to size the loan with. 0 uses the top of the affordable range.
    """
    rng, cur, err = _range(state, max_dti)
    if err:
        return err
    try:
        terms = _tenors(tenors, "36,48,60")
    except ValueError as e:
        return f"Error: {e}"
    if not 0 <= annual_rate <= 100 or monthly_installment < 0:
        return "Error: annual_rate must be between 0 and 100 and monthly_installment can't be negative."

    low, high = rng["low"], rng["high"]
    pay = monthly_installment or high
    rows = [f"| {n} | {_money(cur, pay)} | {_money(cur, principal_for_payment(pay, n, annual_rate))} | "
            f"{_money(cur, principal_for_payment(low, n, annual_rate))} |" for n in terms]
    lines = [
        f"Loan capacity at {annual_rate:g}% a year, {max_dti:g}% DTI cap.",
        f"Affordable new installment: {_money(cur, low)} – {_money(cur, high)} a month.",
        "",
        "| Months | Installment | Maximum loan | Conservative loan (low end) |",
        "| --- | --- | --- | --- |",
        *rows,
    ]
    if monthly_installment and monthly_installment > high:
        lines += ["", f"Note: {_money(cur, monthly_installment)} is above the affordable range; "
                      "the maximum loan amounts are not recommended."]
    lines += ["", DISCLAIMER, DISCLAIMER_AR]
    return "\n".join(lines)


FINANCING_TOOLS = [installment_plan, loan_capacity]
