"""bank_statement_tool.py
------------------------
Financial analysis tools for bank statement processing.

Three tools:
  1. analyze_bank_statement      — CSV: direct Python parsing; PDF text: LLM extraction
  2. simulate_mortgage_affordability — Pure-Python DTI-based affordability simulation
  3. render_financial_chart       — QuickChart visualizations (savings, income/spending,
                                   cashflow, categories)

CSV bank statements are parsed directly (no LLM needed).
PDF text statements use DeepSeek for transaction extraction.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from tools._tooling import tool
from tools._chart_spec import records_chart


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _deepseek_api_key() -> str:
    return (
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        or os.environ.get("WXO_CONNECTION_deepseek_DEEPSEEK_API_KEY", "").strip()
    )


def _watsonx_env() -> tuple:
    def g(k: str, d: str = "") -> str:
        return (
            os.environ.get(f"WXO_CONNECTION_watsonx_{k}", "").strip()
            or os.environ.get(f"WXO_CONNECTION_watsonx-ai_{k}", "").strip()
            or os.environ.get(k, "").strip()
            or d
        )
    return (
        g("WATSONX_API_KEY",    ""),
        g("WATSONX_PROJECT_ID", "ab0de2fc-5f68-4e0e-8dc8-d1677b6b689b"),
        g("WATSONX_URL",        "https://us-south.ml.cloud.ibm.com"),
    )


# ---------------------------------------------------------------------------
# LLM routing (DeepSeek or watsonx, same as pdf_tool.py)
# ---------------------------------------------------------------------------

_LLM_MODELS: Dict[str, str] = {
    "deepseek":    "deepseek-chat",
    "deepseek-r1": "deepseek-reasoner",
    "llama4":      "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
    "llama3":      "meta-llama/llama-3-3-70b-instruct",
    "granite":     "ibm/granite-13b-instruct-v2",
    "mistral":     "mistralai/mistral-large",
}


def _deepseek_call(prompt: str, model_id: str, max_tokens: int = 4000) -> str:
    api_key = _deepseek_api_key()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is not set.")
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return (data["choices"][0]["message"]["content"] or "").strip()


def _llm_call(prompt: str, model: str = "deepseek", max_tokens: int = 4000) -> str:
    model_id = _LLM_MODELS.get(model.strip().lower())
    if not model_id:
        raise ValueError(f"Unknown model '{model}'. Choose from: {', '.join(_LLM_MODELS)}.")
    if model_id.startswith("deepseek"):
        return _deepseek_call(prompt, model_id, max_tokens)
    from ibm_watsonx_ai import Credentials
    from ibm_watsonx_ai.foundation_models import ModelInference
    api_key, project_id, url = _watsonx_env()
    creds = Credentials(url=url, api_key=api_key)
    llm = ModelInference(model_id=model_id, credentials=creds, project_id=project_id)
    result = llm.generate(prompt=prompt, params={"max_new_tokens": max_tokens, "temperature": 0.1})
    return (result["results"][0]["generated_text"] or "").strip()


# ---------------------------------------------------------------------------
# QuickChart helper (same pattern as chart_ts_tool.py)
# ---------------------------------------------------------------------------

_COLORS = [
    "#2E6DB4", "#C8A84B", "#E05C2E", "#3DAA6D",
    "#9B59B6", "#1ABC9C", "#E74C3C", "#F39C12",
]
_QUICKCHART = "https://quickchart.io/chart"


@records_chart
def _chart_url(config: dict, width: int = 700, height: int = 420) -> str:
    import copy
    cfg = copy.deepcopy(config)
    opts = cfg.setdefault("options", {})
    pl = opts.setdefault("plugins", {})
    pl.setdefault("title", {}).setdefault("font", {}).setdefault("size", 11)
    pl.setdefault("legend", {}).setdefault("labels", {}).setdefault("font", {}).setdefault("size", 10)
    for sc in opts.get("scales", {}).values():
        sc.setdefault("ticks", {}).setdefault("font", {}).setdefault("size", 9)
        if "title" in sc:
            sc["title"].setdefault("font", {}).setdefault("size", 9)
    body = json.dumps({
        "chart": cfg, "width": width, "height": height, "backgroundColor": "white",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://quickchart.io/chart/create",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["url"]
    except Exception:
        params = urllib.parse.urlencode({
            "c": json.dumps(cfg, separators=(",", ":")),
            "w": width, "h": height, "bkg": "white",
        })
        return f"{_QUICKCHART}?{params}"


def _chart_md(url: str, title: str) -> str:
    return (
        f'<img src="{url}" alt="{title}" style="max-width:65%;display:block" />\n\n'
        f'<a href="{url}">⬇️ Download chart</a>\n\n'
    )


# ---------------------------------------------------------------------------
# CSV bank statement parser
# ---------------------------------------------------------------------------

# Column name aliases — maps canonical role → list of possible CSV header substrings
_COL_ALIASES: Dict[str, List[str]] = {
    "date":        ["date", "trans date", "transaction date", "value date",
                    "posting date", "تاريخ"],
    "description": ["description", "narration", "particulars", "details",
                    "remarks", "memo", "reference", "بيان", "وصف"],
    "debit":       ["debit", "withdrawal", "dr", "amount dr", "payments out",
                    "خصم", "سحب"],
    "credit":      ["credit", "deposit", "cr", "amount cr", "payments in",
                    "إيداع", "ايداع"],
    "amount":      ["amount", "transaction amount", "net amount", "مبلغ"],
    "balance":     ["balance", "running balance", "closing balance",
                    "available balance", "رصيد"],
    "type":        ["type", "transaction type", "dr/cr", "cr/dr", "نوع"],
}


def _alias_in(alias: str, header: str) -> bool:
    # Latin aliases must match whole words (an optional plural "s" allowed):
    # a plain substring test let "cr" match inside "Description", so the
    # description column was read as the credit column and all income was lost.
    if alias.isascii():
        pattern = rf"(?<![a-z]){re.escape(alias)}s?(?![a-z])"
        return re.search(pattern, header) is not None
    return alias in header


def _match_col(header: str, role: str) -> bool:
    h = header.strip().lower()
    return any(_alias_in(alias, h) for alias in _COL_ALIASES.get(role, []))


def _find_col(headers: List[str], role: str) -> Optional[str]:
    aliases = _COL_ALIASES.get(role, [])
    for h in headers:  # an exact header name wins over a partial match
        if h.strip().lower() in aliases:
            return h
    for h in headers:
        if _match_col(h, role):
            return h
    return None


# Keyword → category mapping (checked against description, case-insensitive)
_KW_CATEGORIES: List[tuple] = [
    ("Income",        ["salary", "payroll", "wages", "income", "revenue",
                       "مرتب", "راتب", "أجر", "دخل"]),
    ("Debt Payment",  ["loan", "emi", "installment", "mortgage", "repayment",
                       "قسط", "اقساط", "تسديد", "سداد"]),
    ("Housing",       ["rent", "lease", "utility", "electric", "water",
                       "gas bill", "telecom", "internet", "ايجار", "إيجار"]),
    ("Education",     ["school", "university", "tuition", "fees", "course",
                       "مدرسة", "جامعة", "تعليم", "رسوم"]),
    ("Healthcare",    ["pharmacy", "hospital", "clinic", "medical", "doctor",
                       "صيدلية", "مستشفى", "عيادة", "طبي"]),
    ("Transport",     ["petrol", "fuel", "uber", "careem", "taxi", "metro",
                       "transport", "parking", "بنزين", "وقود", "مواصلات"]),
    ("Food",          ["restaurant", "cafe", "grocery", "supermarket",
                       "food", "eat", "مطعم", "بقالة", "سوبر ماركت"]),
    ("Entertainment", ["cinema", "movie", "netflix", "spotify", "game",
                       "hotel", "travel", "vacation", "ترفيه", "سينما"]),
    ("Savings",       ["saving", "investment", "mutual fund", "ادخار", "استثمار"]),
]


def _categorize(description: str, is_credit: bool) -> str:
    if is_credit:
        desc_lower = description.lower()
        # Salary / income check even on credit side
        for kw in _KW_CATEGORIES[0][1]:
            if kw in desc_lower:
                return "Income"
        return "Income"  # default all credits to Income
    desc_lower = description.lower()
    for category, keywords in _KW_CATEGORIES:
        for kw in keywords:
            if kw in desc_lower:
                return category
    return "Other"


def _merge_csv_blocks(csv_text: str) -> str:
    """
    Merge multiple CSV files that have been concatenated into one string.
    When multiple files are stacked, the header row appears more than once.
    This function keeps the first header and strips all subsequent header
    repetitions, returning a single clean CSV block.
    """
    csv_text = csv_text.replace("\\n", "\n").replace("\\r\n", "\n").strip()
    lines = csv_text.splitlines()
    if not lines:
        return csv_text

    # Detect the header line (first non-empty line)
    header_line = ""
    output_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not header_line:
            header_line = stripped
            output_lines.append(line)
        elif stripped.lower() == header_line.lower():
            # Duplicate header — skip it (marks the start of a new file)
            pass
        else:
            output_lines.append(line)

    return "\n".join(output_lines)


def _parse_csv_statement(csv_text: str, currency: str) -> List[Dict]:
    """
    Parse one or more concatenated CSV bank statements into a normalised
    transaction list. Handles debit/credit split columns and single
    signed-amount columns. Duplicate header rows (from multiple files)
    are stripped automatically.
    """
    csv_text = _merge_csv_blocks(csv_text)

    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    rows = list(reader)

    if not headers or not rows:
        return []

    col_date   = _find_col(headers, "date")
    col_desc   = _find_col(headers, "description")
    col_debit  = _find_col(headers, "debit")
    col_credit = _find_col(headers, "credit")
    col_amount = _find_col(headers, "amount") if not (col_debit or col_credit) else None
    col_type   = _find_col(headers, "type")
    col_bal    = _find_col(headers, "balance")

    transactions: List[Dict] = []
    for row in rows:
        date_val = (row.get(col_date) or "").strip() if col_date else ""
        desc_val = (row.get(col_desc) or "").strip() if col_desc else ""
        bal_val  = _safe_float(row.get(col_bal)) if col_bal else 0.0

        if col_debit and col_credit:
            debit  = _safe_float(row.get(col_debit))
            credit = _safe_float(row.get(col_credit))
            if credit and credit > 0:
                tx_type = "credit"
                amount  = credit
            elif debit and debit > 0:
                tx_type = "debit"
                amount  = debit
            else:
                continue
        elif col_amount:
            raw_amount = _safe_float(row.get(col_amount))
            if raw_amount is None:
                continue
            type_hint = (row.get(col_type) or "").strip().lower() if col_type else ""
            if type_hint in ("cr", "credit", "c"):
                tx_type = "credit"
                amount  = abs(raw_amount)
            elif type_hint in ("dr", "debit", "d"):
                tx_type = "debit"
                amount  = abs(raw_amount)
            else:
                tx_type = "credit" if raw_amount >= 0 else "debit"
                amount  = abs(raw_amount)
        else:
            continue

        category = _categorize(desc_val, tx_type == "credit")

        transactions.append({
            "date":        date_val,
            "description": desc_val,
            "type":        tx_type,
            "amount":      round(amount, 2),
            "balance":     round(bal_val or 0.0, 2),
            "category":    category,
        })

    return transactions


# ---------------------------------------------------------------------------
# Metric calculation helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _month_key(date_str: str) -> str:
    """Return YYYY-MM from various date formats, or original string if unparseable."""
    import datetime
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y",
                "%d %B %Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m")
        except ValueError:
            pass
    # try partial like "2024-01"
    if re.match(r"^\d{4}-\d{2}$", date_str.strip()):
        return date_str.strip()
    return date_str.strip()[:7]


def _calculate_metrics(transactions: List[Dict], currency: str) -> Dict:
    """
    Derive all financial metrics from the raw transaction list.
    Returns a metrics dict ready to be JSON-serialised.
    """
    if not transactions:
        return {}

    # ── Bucket transactions by month ─────────────────────────────────────────
    month_income:      Dict[str, float] = {}
    month_spending:    Dict[str, float] = {}
    month_debt:        Dict[str, float] = {}
    month_balance:     Dict[str, float] = {}
    category_totals:   Dict[str, float] = {}
    monthly_savings:   Dict[str, float] = {}

    INCOME_TYPES    = {"credit", "income", "deposit"}
    DEBT_CATEGORIES = {"debt payment", "debt_payment", "loan", "loan payment"}

    for tx in transactions:
        raw_date = str(tx.get("date") or "")
        mk = _month_key(raw_date) if raw_date else "unknown"

        amount = _safe_float(tx.get("amount")) or 0.0
        balance = _safe_float(tx.get("balance"))
        tx_type = str(tx.get("type") or "").strip().lower()
        category = str(tx.get("category") or "Other").strip()

        # Track last balance per month
        if balance is not None:
            month_balance[mk] = balance

        if tx_type in INCOME_TYPES or category.lower() == "income":
            month_income[mk] = month_income.get(mk, 0.0) + abs(amount)
        else:
            if category.lower() in DEBT_CATEGORIES:
                month_debt[mk] = month_debt.get(mk, 0.0) + abs(amount)
            else:
                month_spending[mk] = month_spending.get(mk, 0.0) + abs(amount)

            # category breakdown (spending only)
            if category.lower() != "income":
                category_totals[category] = category_totals.get(category, 0.0) + abs(amount)

    # ── Align months ─────────────────────────────────────────────────────────
    all_months = sorted(set(
        list(month_income.keys()) +
        list(month_spending.keys()) +
        list(month_debt.keys())
    ))

    for m in all_months:
        inc  = month_income.get(m, 0.0)
        spd  = month_spending.get(m, 0.0)
        debt = month_debt.get(m, 0.0)
        monthly_savings[m] = inc - spd - debt

    # ── Aggregate stats ───────────────────────────────────────────────────────
    incomes   = [month_income.get(m, 0.0) for m in all_months]
    spendings = [month_spending.get(m, 0.0) for m in all_months]
    debts     = [month_debt.get(m, 0.0) for m in all_months]
    savings   = [monthly_savings[m] for m in all_months]
    balances  = [month_balance[m] for m in all_months if m in month_balance]

    n = len(all_months) or 1
    avg_income   = round(sum(incomes) / n, 2)
    avg_spending = round(sum(spendings) / n, 2)
    avg_savings  = round(sum(savings) / n, 2)
    avg_debt     = round(sum(debts) / n, 2)
    avg_balance  = round(sum(balances) / len(balances), 2) if balances else 0.0

    debt_pct = round((avg_debt / avg_income * 100), 1) if avg_income > 0 else 0.0

    positive_cashflow_months = sum(1 for s in savings if s > 0)

    # savings growth %
    first_saving = savings[0] if savings else 0.0
    last_saving  = savings[-1] if savings else 0.0
    if first_saving and first_saving > 0:
        savings_growth_pct = round((last_saving - first_saving) / first_saving * 100, 1)
    else:
        savings_growth_pct = 0.0

    # Top spending category (exclude Debt Payment)
    spd_cats = {
        k: v for k, v in category_totals.items()
        if k.lower() not in DEBT_CATEGORIES
    }
    top_category = max(spd_cats, key=spd_cats.get) if spd_cats else "N/A"

    return {
        "currency": currency,
        "months": all_months,
        "monthly_income": {m: round(month_income.get(m, 0.0), 2) for m in all_months},
        "monthly_spending": {m: round(month_spending.get(m, 0.0), 2) for m in all_months},
        "monthly_debt": {m: round(month_debt.get(m, 0.0), 2) for m in all_months},
        "monthly_savings": {m: round(monthly_savings[m], 2) for m in all_months},
        "monthly_balance": {m: round(month_balance.get(m, 0.0), 2) for m in all_months},
        "category_totals": {k: round(v, 2) for k, v in category_totals.items()},
        "avg_monthly_income": avg_income,
        "avg_monthly_spending": avg_spending,
        "avg_monthly_savings": avg_savings,
        "avg_monthly_debt": avg_debt,
        "avg_balance": avg_balance,
        "debt_obligations_pct": debt_pct,
        "debt_obligations_amount": avg_debt,
        "top_spending_category": top_category,
        "positive_cashflow_months": positive_cashflow_months,
        "total_months": n,
        "savings_growth_pct": savings_growth_pct,
    }


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

def _extraction_prompt(statement_text: str, currency: str) -> str:
    return f"""You are a financial data extraction assistant.

Extract ALL transactions from the bank statement text below.
Return ONLY a valid JSON array — no markdown fences, no explanation, no extra text.

Each element must have exactly these keys:
  "date"        – transaction date as a string (keep original format from statement)
  "description" – short description of the transaction
  "type"        – "credit" if money came IN, "debit" if money went OUT
  "amount"      – numeric amount (always positive, no currency symbols)
  "balance"     – running balance after this transaction (numeric, 0 if not shown)
  "category"    – one of: Income, Housing, Food, Transport, Education, Healthcare,
                  Entertainment, Debt Payment, Savings, Other

Rules:
- Salary / payroll / employer transfers → type=credit, category=Income
- Loan repayments / EMI / installments → type=debit, category=Debt Payment
- Rent / utilities → category=Housing
- Supermarket / groceries / restaurants → category=Food
- School fees / university / courses → category=Education
- Petrol / taxi / transport cards → category=Transport
- Investments / transfers to savings account → category=Savings
- Everything else → category=Other
- Do NOT include opening/closing balance summary rows — only actual transactions.

Currency used in this statement: {currency}

Bank statement text:
\"\"\"
{statement_text}
\"\"\"

JSON array output:"""


# ---------------------------------------------------------------------------
# Tool 1: analyze_bank_statement
# ---------------------------------------------------------------------------

@tool
def analyze_bank_statement(
    csv_content: str = "",
    csv_files: str = "",
    statement_text: str = "",
    currency: str = "EGP",
    language: str = "English",
    model: str = "deepseek",
) -> str:
    """
    Analyze a bank statement to extract financial metrics for mortgage readiness assessment.

    Supports three input modes (provide one):
      - csv_files:    JSON array of CSV strings — one entry per uploaded file.
                      Use this when the user uploads MULTIPLE CSV files at once.
                      Example: '["date,desc,debit,credit\\n...", "date,desc,debit,credit\\n..."]'
                      The tool merges them automatically, removing duplicate header rows.
      - csv_content:  Single CSV text of the bank statement (for one file or pre-merged text).
                      The first row must be a header row with column names.
      - statement_text: Raw text of a PDF bank statement. An LLM will extract transactions.

    Calculates:
      - Monthly income, spending, savings, debt obligations, and running balance
      - Average monthly income, spending, and savings
      - Top spending category (Food, Education, Transport, etc.)
      - Debt-to-income ratio and existing debt payment amount
      - Positive cash flow month count and savings growth percentage

    The returned JSON string must be stored and passed to simulate_mortgage_affordability
    and render_financial_chart for all follow-up questions.

    Args:
        csv_content:    Full CSV text of the bank statement. Pass the entire CSV content
                        including the header row. Required column roles (detected
                        automatically by common header names):
                          - Date column  (e.g. "Date", "Transaction Date", "Value Date")
                          - Description  (e.g. "Description", "Narration", "Particulars")
                          - Debit/Credit (e.g. separate "Debit"/"Credit" columns, OR
                            a single "Amount" column with a "Type"/"Dr/Cr" column or sign)
                          - Balance      (e.g. "Balance", "Running Balance") — optional
        csv_files:      JSON array of CSV strings for multi-file upload. Each element is
                        the full text of one CSV file (including its header row).
                        When provided, csv_content is ignored.
        statement_text: Raw text content of a PDF bank statement. Used when CSV is not
                        available. An LLM call will extract transactions from the text.
        currency:       Currency label shown in the statement (default: "EGP").
        language:       Language for the summary text — "English" or "Arabic".
        model:          LLM model for PDF extraction only (ignored for CSV):
                        "deepseek" (default), "llama4", "llama3", "granite", "mistral".

    Returns:
        A JSON string with all computed metrics and a human-readable summary.
        Pass this string to simulate_mortgage_affordability or render_financial_chart.
    """
    csv_files_raw  = (csv_files or "").strip()
    csv_content    = (csv_content or "").strip()
    statement_text = (statement_text or "").strip()

    # ── Resolve csv_files → csv_content by merging all files ─────────────────
    if csv_files_raw:
        try:
            files_list = json.loads(csv_files_raw)
            if not isinstance(files_list, list):
                files_list = [csv_files_raw]
        except (json.JSONDecodeError, ValueError):
            # Not valid JSON — treat the whole string as one CSV block
            files_list = [csv_files_raw]

        # Normalise each block and stack them; _merge_csv_blocks will strip dup headers
        merged_parts: List[str] = []
        for block in files_list:
            block = (block or "").replace("\\n", "\n").replace("\\r", "").strip()
            if block:
                merged_parts.append(block)

        if not merged_parts:
            return "Error: csv_files was provided but all file contents are empty."

        csv_content = "\n".join(merged_parts)

    if not csv_content and not statement_text:
        return (
            "Error: Please provide csv_files (JSON array of CSV texts), "
            "csv_content (single CSV text), or statement_text (PDF text)."
        )

    # ── Route: CSV → direct parse; PDF text → LLM extraction ─────────────────
    if csv_content:
        # Normalise escaped newlines that agents sometimes produce
        csv_content = csv_content.replace("\\n", "\n").replace("\\r", "")
        transactions = _parse_csv_statement(csv_content, currency)
        if not transactions:
            return (
                "Could not parse any transactions from the CSV. "
                "Please verify:\n"
                "  1. The first row is a header row with column names.\n"
                "  2. There are columns for date, description, and debit/credit amounts.\n"
                "  3. The CSV uses comma or semicolon delimiters."
            )
        source = "csv"
    else:
        # PDF text → LLM extraction
        if len(statement_text) < 50:
            return (
                "Error: The provided text is too short to be a valid bank statement. "
                "Please paste the full statement text."
            )
        prompt = _extraction_prompt(statement_text, currency)
        try:
            raw_json = _llm_call(prompt, model=model, max_tokens=6000)
        except Exception as e:
            return f"LLM extraction failed: {e}"

        raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json.strip())
        raw_json = re.sub(r"\s*```$", "", raw_json.strip())
        bracket_match = re.search(r"\[.*\]", raw_json, re.DOTALL)
        if bracket_match:
            raw_json = bracket_match.group(0)

        try:
            transactions: List[Dict] = json.loads(raw_json)
        except json.JSONDecodeError as e:
            return (
                f"Could not parse transaction data from the statement (JSON error: {e}). "
                "Please ensure the statement text contains readable transaction rows."
            )
        if not isinstance(transactions, list) or len(transactions) == 0:
            return (
                "No transactions were extracted from the statement. "
                "Please verify the statement text contains transaction data."
            )
        source = "pdf"

    # ── Metric calculation ─────────────────────────────────────────────────────
    metrics = _calculate_metrics(transactions, currency)
    if not metrics:
        return "Could not calculate metrics — no valid transactions found after parsing."

    # ── Human-readable summary ─────────────────────────────────────────────────
    cur = currency
    n = metrics["total_months"]
    pos = metrics["positive_cashflow_months"]
    growth = metrics["savings_growth_pct"]
    top_cat = metrics["top_spending_category"]
    debt_pct = metrics["debt_obligations_pct"]

    def fmt(v: float) -> str:
        return f"{cur} {v:,.0f}"

    # Build per-month rows for the breakdown table
    month_rows = []
    for m in metrics.get("months", []):
        inc = metrics["monthly_income"].get(m, 0.0)
        spd = metrics["monthly_spending"].get(m, 0.0)
        dbt = metrics["monthly_debt"].get(m, 0.0)
        sav = metrics["monthly_savings"].get(m, 0.0)
        month_rows.append(
            f"  {m:<10} Income: {fmt(inc):<16} Spending: {fmt(spd):<16} "
            f"Debt: {fmt(dbt):<14} Savings: {fmt(sav)}"
        )

    total_income   = sum(metrics["monthly_income"].values())
    total_spending = sum(metrics["monthly_spending"].values())
    total_savings  = sum(metrics["monthly_savings"].values())

    summary_lines = [
        f"## Financial Analysis Summary ({n}-month statement)",
        "",
        f"- **Average monthly income**: {fmt(metrics['avg_monthly_income'])}",
        f"- **Average monthly spending**: {fmt(metrics['avg_monthly_spending'])}",
        f"- **Average monthly savings**: {fmt(metrics['avg_monthly_savings'])}",
        f"- **Average balance**: {fmt(metrics['avg_balance'])}",
        f"- **Largest spending category**: {top_cat}",
        f"- **Debt obligations**: {debt_pct}% of income "
        f"({fmt(metrics['debt_obligations_amount'])}/month)",
        f"- **Positive cash flow months**: {pos} out of {n}",
        f"- **Savings growth**: {'+' if growth >= 0 else ''}{growth}% over {n} months",
        "",
        "### Monthly Breakdown",
    ] + month_rows + [
        "",
        "### How Averages Are Calculated",
        f"  Average monthly income   = Total income / Months",
        f"                           = {fmt(total_income)} / {n} = {fmt(metrics['avg_monthly_income'])}",
        f"  Average monthly spending = Total spending / Months",
        f"                           = {fmt(total_spending)} / {n} = {fmt(metrics['avg_monthly_spending'])}",
        f"  Average monthly savings  = Total savings / Months",
        f"                           = {fmt(total_savings)} / {n} = {fmt(metrics['avg_monthly_savings'])}",
        f"  Debt-to-Income ratio     = Avg debt / Avg income x 100",
        f"                           = {fmt(metrics['debt_obligations_amount'])} / {fmt(metrics['avg_monthly_income'])} x 100 = {debt_pct}%",
    ]

    metrics["summary"] = "\n".join(summary_lines)
    metrics["transaction_count"] = len(transactions)
    metrics["source"] = source

    return json.dumps(metrics, ensure_ascii=False, indent=2)



# ---------------------------------------------------------------------------
# Tool 2: simulate_mortgage_affordability
# ---------------------------------------------------------------------------

@tool
def simulate_mortgage_affordability(
    analysis_json: str,
    currency: str = "EGP",
    max_dti: float = 40.0,
) -> str:
    """
    Simulate mortgage affordability based on a bank statement analysis.

    Uses a Debt-to-Income (DTI) model:
      - Maximum allowed total debt payments = avg_income × (max_dti / 100)
      - Available for new mortgage = max_total_debt − existing_debt_payments
      - Safe range = 75%–95% of available amount, capped at average monthly savings

    Call analyze_bank_statement first, then pass its output directly as analysis_json.

    Args:
        analysis_json: JSON string returned by analyze_bank_statement.
        currency:      Currency label for display (default: "EGP").
        max_dti:       Maximum allowed Debt-to-Income ratio as a percentage (default: 40.0).
                       Banks typically use 35–45%. Lowering this gives a more conservative result.

    Returns:
        A human-readable affordability assessment with the recommended installment range.
    """
    analysis_json = (analysis_json or "").strip()
    if not analysis_json:
        return "Error: analysis_json is empty. Call analyze_bank_statement first."

    try:
        data = json.loads(analysis_json)
    except json.JSONDecodeError as e:
        return f"Error: Could not parse analysis_json ({e}). Pass the raw output of analyze_bank_statement."

    avg_income   = float(data.get("avg_monthly_income", 0))
    avg_spending = float(data.get("avg_monthly_spending", 0))
    avg_savings  = float(data.get("avg_monthly_savings", 0))
    avg_debt     = float(data.get("debt_obligations_amount", 0))
    debt_pct     = float(data.get("debt_obligations_pct", 0))
    n_months     = int(data.get("total_months", 6))
    pos_months   = int(data.get("positive_cashflow_months", 0))
    cur          = currency

    if avg_income <= 0:
        return (
            "Cannot simulate affordability: average monthly income is zero or missing. "
            "Please provide a statement with at least 3 months of income transactions."
        )

    def fmt(v: float) -> str:
        return f"{cur} {v:,.0f}"

    # ── Core DTI calculation ──────────────────────────────────────────────────
    max_total_debt   = avg_income * (max_dti / 100)
    available        = max(max_total_debt - avg_debt, 0.0)

    # Conservative caps
    safe_max = min(available * 0.95, avg_savings * 0.90) if avg_savings > 0 else available * 0.80
    safe_min = safe_max * 0.75
    safe_max = round(safe_max)
    safe_min = round(safe_min)

    # ── Affordability verdict ─────────────────────────────────────────────────
    if safe_max <= 0:
        verdict = (
            "⚠️ Based on your current income and debt obligations, "
            "taking on a new mortgage at this time appears financially challenging. "
            "We recommend reducing existing debt obligations before applying."
        )
    elif safe_min <= 0:
        safe_min = 1
        verdict = (
            f"A mortgage installment of up to {fmt(safe_max)}/month may be feasible, "
            "though your current obligations leave limited margin. "
            "We recommend maintaining at least 3 months of emergency savings."
        )
    else:
        verdict = (
            f"Based on your income and spending profile, a mortgage installment "
            f"between **{fmt(safe_min)}** and **{fmt(safe_max)}** per month "
            f"appears affordable."
        )

    # ── Cash flow health check ────────────────────────────────────────────────
    cash_flow_note = ""
    if pos_months == n_months:
        cash_flow_note = f"✅ Excellent: positive cash flow in all {n_months} months."
    elif pos_months >= n_months * 0.75:
        cash_flow_note = f"✅ Good: positive cash flow in {pos_months} of {n_months} months."
    else:
        cash_flow_note = (
            f"⚠️ Caution: positive cash flow in only {pos_months} of {n_months} months. "
            "This may raise concerns for lenders."
        )

    # ── Income consistency note ───────────────────────────────────────────────
    monthly_incomes = list(data.get("monthly_income", {}).values())
    if len(monthly_incomes) >= 2:
        min_inc = min(monthly_incomes)
        max_inc = max(monthly_incomes)
        variance_pct = round((max_inc - min_inc) / avg_income * 100, 1) if avg_income else 0
        if variance_pct < 10:
            income_note = "✅ Income is highly consistent (< 10% variance)."
        elif variance_pct < 25:
            income_note = f"⚠️ Moderate income variation ({variance_pct}% variance between months)."
        else:
            income_note = f"⚠️ High income variation ({variance_pct}% variance) — lenders may scrutinise this."
    else:
        income_note = ""

    lines = [
        "## Mortgage Affordability Simulation",
        "",
        "### Your Financial Profile",
        f"- Average monthly income:   {fmt(avg_income)}",
        f"- Average monthly spending: {fmt(avg_spending)}",
        f"- Average monthly savings:  {fmt(avg_savings)}",
        f"- Existing debt payments:   {fmt(avg_debt)} ({debt_pct}% of income)",
        f"- DTI limit applied:        {max_dti}%",
        "",
        "### Affordability Result",
        verdict,
        "",
        "### Health Indicators",
        cash_flow_note,
    ]
    if income_note:
        lines.append(income_note)

    # ── Plain-text formula breakdown ─────────────────────────────────────────
    avail_display = round(max(max_total_debt - avg_debt, 0.0))
    max_total_debt_display = round(max_total_debt)

    lines += [
        "",
        "### Calculation Breakdown",
        f"  Step 1  Max allowed debt  = Avg income × DTI limit",
        f"          {fmt(avg_income)} × {max_dti}% = {fmt(max_total_debt_display)}",
        "",
        f"  Step 2  Available for mortgage = Max allowed debt − Existing debt",
        f"          {fmt(max_total_debt_display)} − {fmt(avg_debt)} = {fmt(avail_display)}",
        "",
        f"  Step 3  Recommended monthly installment range",
        f"          Low  = 75% of available = {fmt(safe_min)}",
        f"          High = 90-95% of available, capped at 90% of avg savings = {fmt(safe_max)}",
        "",
        "### Key Assumptions",
        f"- DTI limit: {max_dti}% (typical bank range: 35–45%)",
        "- This is an indicative estimate only. Final approval depends on bank policy,",
        "  collateral, credit history, and property valuation.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 3: render_financial_chart
# ---------------------------------------------------------------------------

_CHART_TYPES = ("savings", "income_vs_spending", "cashflow", "categories")

_CATEGORY_COLORS = {
    "Income":        "#3DAA6D",
    "Housing":       "#2E6DB4",
    "Food":          "#C8A84B",
    "Transport":     "#E05C2E",
    "Education":     "#9B59B6",
    "Healthcare":    "#1ABC9C",
    "Entertainment": "#E74C3C",
    "Debt Payment":  "#E84393",
    "Savings":       "#27AE60",
    "Other":         "#95A5A6",
}


@tool
def render_financial_chart(
    analysis_json: str,
    chart_type: str = "savings",
    title: str = "",
) -> str:
    """
    Render a financial chart from bank statement analysis data.

    Available chart types:
      - "savings"            — Line chart: monthly savings trend over time
      - "income_vs_spending" — Grouped bar: income vs spending per month
      - "cashflow"           — Bar chart with green (positive) / red (negative) bars
      - "categories"         — Doughnut chart: spending breakdown by category

    Call analyze_bank_statement first, then pass its output directly as analysis_json.

    Args:
        analysis_json: JSON string returned by analyze_bank_statement.
        chart_type:    One of: "savings", "income_vs_spending", "cashflow", "categories".
                       Default: "savings".
        title:         Optional chart title. A sensible default is used if blank.

    Returns:
        Markdown image string that renders as an inline chart in the chat,
        plus a download link.
    """
    analysis_json = (analysis_json or "").strip()
    if not analysis_json:
        return "Error: analysis_json is empty. Call analyze_bank_statement first."

    try:
        data = json.loads(analysis_json)
    except json.JSONDecodeError as e:
        return f"Error: Could not parse analysis_json ({e})."

    cur          = data.get("currency", "EGP")
    months       = data.get("months", [])
    m_income     = data.get("monthly_income", {})
    m_spending   = data.get("monthly_spending", {})
    m_savings    = data.get("monthly_savings", {})
    cat_totals   = data.get("category_totals", {})

    ct = chart_type.strip().lower()
    if ct not in _CHART_TYPES:
        return (
            f"Unknown chart_type '{chart_type}'. "
            f"Choose one of: {', '.join(_CHART_TYPES)}."
        )

    if not months:
        return "No monthly data found in analysis. Please run analyze_bank_statement first."

    # ── Savings trend ─────────────────────────────────────────────────────────
    if ct == "savings":
        chart_title = title or "Monthly Savings Trend"
        savings_vals = [round(m_savings.get(m, 0.0)) for m in months]
        config = {
            "type": "line",
            "data": {
                "labels": months,
                "datasets": [{
                    "label": f"Monthly Savings ({cur})",
                    "data": savings_vals,
                    "borderColor": "#3DAA6D",
                    "backgroundColor": "#3DAA6D33",
                    "fill": True,
                    "tension": 0.3,
                    "pointRadius": 5,
                    "pointBackgroundColor": "#3DAA6D",
                }],
            },
            "options": {
                "plugins": {
                    "title": {"display": True, "text": chart_title},
                    "legend": {"position": "bottom"},
                },
                "scales": {
                    "x": {"title": {"display": True, "text": "Month"}},
                    "y": {"title": {"display": True, "text": f"Amount ({cur})"}},
                },
            },
        }

    # ── Income vs Spending ────────────────────────────────────────────────────
    elif ct == "income_vs_spending":
        chart_title = title or "Income vs Spending by Month"
        config = {
            "type": "bar",
            "data": {
                "labels": months,
                "datasets": [
                    {
                        "label": f"Income ({cur})",
                        "data": [round(m_income.get(m, 0.0)) for m in months],
                        "backgroundColor": "#2E6DB4",
                        "borderColor": "#2E6DB4",
                        "borderWidth": 1,
                    },
                    {
                        "label": f"Spending ({cur})",
                        "data": [round(m_spending.get(m, 0.0)) for m in months],
                        "backgroundColor": "#E05C2E",
                        "borderColor": "#E05C2E",
                        "borderWidth": 1,
                    },
                ],
            },
            "options": {
                "plugins": {
                    "title": {"display": True, "text": chart_title},
                    "legend": {"position": "bottom"},
                },
                "scales": {
                    "x": {"title": {"display": True, "text": "Month"}},
                    "y": {"title": {"display": True, "text": f"Amount ({cur})"}},
                },
            },
        }

    # ── Cash flow ─────────────────────────────────────────────────────────────
    elif ct == "cashflow":
        chart_title = title or "Monthly Cash Flow"
        cashflow_vals = [round(m_savings.get(m, 0.0)) for m in months]
        bg_colors = [
            "#3DAA6D" if v >= 0 else "#E74C3C"
            for v in cashflow_vals
        ]
        config = {
            "type": "bar",
            "data": {
                "labels": months,
                "datasets": [{
                    "label": f"Cash Flow ({cur})",
                    "data": cashflow_vals,
                    "backgroundColor": bg_colors,
                    "borderColor": bg_colors,
                    "borderWidth": 1,
                }],
            },
            "options": {
                "plugins": {
                    "title": {"display": True, "text": chart_title},
                    "legend": {"display": False},
                },
                "scales": {
                    "x": {"title": {"display": True, "text": "Month"}},
                    "y": {
                        "title": {"display": True, "text": f"Net Cash Flow ({cur})"},
                        "grid": {"color": "#DDDDDD"},
                    },
                },
            },
        }

    # ── Spending categories ───────────────────────────────────────────────────
    else:  # categories
        chart_title = title or "Spending by Category"
        # Exclude Income from the pie
        spd_cats = {
            k: v for k, v in cat_totals.items()
            if k.lower() not in ("income",) and v > 0
        }
        if not spd_cats:
            return "No spending category data available."
        labels = list(spd_cats.keys())
        values = [round(spd_cats[k]) for k in labels]
        bg_colors = [
            _CATEGORY_COLORS.get(k, _COLORS[i % len(_COLORS)])
            for i, k in enumerate(labels)
        ]
        config = {
            "type": "doughnut",
            "data": {
                "labels": labels,
                "datasets": [{
                    "data": values,
                    "backgroundColor": bg_colors,
                    "borderWidth": 2,
                    "borderColor": "#FFFFFF",
                }],
            },
            "options": {
                "plugins": {
                    "title": {"display": True, "text": chart_title},
                    "legend": {"position": "right"},
                },
                "cutout": "50%",
            },
        }

    try:
        url = _chart_url(config)
    except Exception as e:
        return f"Chart generation failed: {e}"

    return _chart_md(url, chart_title)
