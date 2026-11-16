# KAI Pulse Agent — Full Reference

> **Platform:** IBM watsonx Orchestrate (ADK)
> **Spec version:** `v1` — `kind: native`
> **YAML:** `agents/my_agent.yaml`
> **LLM backbone:** GPT-OSS 120B
> **Style:** default

---

## 1. Overview

**KAI Pulse Agent** is a multi-capability conversational AI agent built on the
watsonx Orchestrate Agent Developer Kit (ADK).  
It serves as a **unified productivity hub** that combines:

- Document & report generation (PDF / Word) in English and Arabic
- Financial analysis from bank statements (CSV or PDF)
- Data visualisation through multiple chart types
- File management across AWS S3 and IBM Cloud Object Storage (COS)
- Cognos Analytics integration
- Table and spreadsheet export
- Email delivery via SendGrid
- Web content fetching and session history management

The agent is fully bilingual (English ↔ Arabic) and auto-detects the user's
language from their messages.

---

## 2. Core Instructions Summary

The agent operates under a strict, prioritised rule set embedded in its YAML
`instructions` block. The key behavioural rules are:

### 2.1 File Upload Rule
| File type | Action |
|-----------|--------|
| **CSV / JSON / TXT / Markdown / XML / YAML / PDF** | Content is immediately available. Use it for whatever was requested. Silently back up to S3 via `upload_chat_file_to_s3`. |
| **XLSX / XLS / PNG / JPG / GIF (binary)** | Call `upload_chat_file_to_s3` with `is_base64: true`, then `inspect_excel_content` + `render_chart_from_excel` for spreadsheets. |
| **Multiple files at once** | For CSV bank statements, build a JSON array and call `analyze_bank_statement` with `csv_files`. For other data, process together or separately as requested. |

The agent **never** says "I can't read the file" — file content is always present.

### 2.2 Document / Summary Export Rule
When the user asks to summarise, export, or create any report:
1. Ask: **"PDF or Word (.docx)?"** (unless format already specified).
2. The agent writes the full content itself (no external LLM call).
3. Calls `create_pdf_report` or `create_word_report` with the self-written content.
4. For conversation exports: first calls `get_current_thread_id`.
5. Report content structure: `## Overview`, `## Key Topics Discussed`, `## Important Outcomes`, `## Action Items`.

### 2.3 Excel / CSV Export Rule
Whenever the user requests an Excel, XLSX, CSV, or spreadsheet export:
1. Collect data as a JSON array of objects, CSV string, or Markdown table.
2. Call `export_table_to_file` with `format: "xlsx"` or `"csv"`.
3. Return the download link to the user.

### 2.4 Email Delivery Rule
When the user asks to send a file by email:
1. Collect recipient, subject, and body if missing.
2. Call `send_file_via_sendgrid` with `file_url` (if file is already hosted) or `file_content` + `filename`.
3. Confirm delivery. Never expose credentials.

### 2.5 Bank Statement Financial Analysis Rule
Three upload scenarios are handled:

| Scenario | Trigger | Action |
|----------|---------|--------|
| **A** | Single file + immediate analysis request | Analyze right away |
| **B** | Single file + "add" / "add statement" | Acknowledge receipt, wait for more files |
| **C** | "analyze all" / "start analysis" | Look back through entire conversation, collect all CSV files, build `csv_files` array, then analyze |

After analysis:
- Financial follow-up questions are answered from the stored JSON.
- Charts are rendered via `render_financial_chart`.
- Mortgage affordability via `simulate_mortgage_affordability`.
- For PDF/Word reports: generate all charts first, embed them using `![Title](url)` syntax, then call the report tool.

---

## 3. Tools Reference

The agent exposes **40+ tools** organised into the following categories. Each tool is shown with its full function signature and docstring as extracted from the source file.

---

### 3.1 Utility Tools

#### `hello_tool` — `tools/hello_tool.py`

```python
@tool
def hello_tool(name: str) -> str:
    """Greet a user by name.

    Args:
        name: The name of the person to greet.

    Returns:
        A greeting message.
    """
    return f"Hello, {name}! Welcome to watsonx Orchestrate."
```

---

#### `debug_env_vars` — `tools/debug_env_tool.py`

```python
@tool
def debug_env_vars() -> str:
    """Check which environment variables are available at runtime in IBM Cloud."""
    relevant = {k: v[:6] + "..." for k, v in os.environ.items()
                if any(k.startswith(p) for p in ("AWS", "COS", "WATSONX", "IBM", "WO", "WX"))}
    all_keys = sorted(os.environ.keys())
    return f"Relevant vars: {relevant}\n\nAll var names: {all_keys}"
```

---

#### `get_current_thread_id` — `tools/get_current_thread_id.py`

```python
@tool
def get_current_thread_id(agent_id: str = "") -> str:
    """
    Automatically find and return the current (most recently active) thread ID
    for this agent.

    The tool queries the watsonx Orchestrate Threads API and returns the thread
    that was updated most recently — which is the ongoing conversation.

    Use the returned thread_id with capture_thread_payload to save the full
    conversation to S3.

    Args:
        agent_id: The watsonx Orchestrate agent UUID. Defaults to the
                  WXO_AGENT_ID environment variable (set automatically at runtime).

    Returns:
        A JSON string with the current thread_id, agent_id, last activity
        timestamp, message count, and a list of the most recent threads found.
    """
```

---

### 3.2 Session & Payload Tools

#### `capture_thread_payload` — `tools/capture_thread_payload.py`

```python
@tool
def capture_thread_payload(
    thread_id: str,
    agent_id: str = "",
    session_label: str = "",
) -> str:
    """
    Capture the full conversation history of the current thread and save it as
    a JSON file to S3.

    This tool fetches EVERY user and assistant message in the thread from the
    beginning of the conversation, not just the current turn. Use it at any
    point during or after a conversation to create a complete payload snapshot.

    The JSON file is uploaded to S3 under:
      thread-payloads/{YYYYMMDD}/{thread_id}.json

    Args:
        thread_id:     The current thread ID (conversation ID). Required.
        agent_id:      The watsonx Orchestrate agent UUID. Falls back to the
                       WXO_AGENT_ID environment variable if not provided.
        session_label: Optional human-readable label embedded in the JSON for
                       easy identification (e.g. "sales_demo", "support_case_123").

    Returns:
        A message with the S3 key, message count, and a 24-hour download link.
    """
```

---

#### `sync_agent_session_messages_to_db` — `tools/session_conversation_sync_tool.py`

```python
@tool
def sync_agent_session_messages_to_db(
    target_agent_name: str = "",
    target_agent_id: str = "",
    include_existing_history: bool = True,
) -> str:
    """
    Fetch user/assistant messages for a target agent and save them in DB.

    The tool stores each message in agent_payloads with:
    - direction = thread_message
    - agent_id = target agent id
    - thread_id = original thread id
    - session_id = "<agent_id>:<thread_id>" as the conversation key
    - tool_call_id = source message id (used to avoid duplicate inserts)

    Args:
        target_agent_name: Name/display name of the agent whose sessions should be synced.
        target_agent_id: UUID of the target agent. If set, this takes precedence.
        include_existing_history: When true, sync all available thread history for the target agent.

    Returns:
        JSON string with sync summary and conversation grouping.
    """
```

---

#### `summarize_session_history` — `tools/session_summary_tools.py`

```python
@tool
def summarize_session_history(
    history_json_path: str = "session_history_payload.json",
    recent_turns: int = 10,
    output_text_path: str = "session_history_summary.txt",
) -> str:
    """
    Generate a plain-text summary from session chat history.

    Args:
        history_json_path: Path to session history JSON file.
        recent_turns: Number of latest turns to include in detail.
        output_text_path: Output path for the generated summary text file.

    Returns:
        A status message including output file path.
    """
```

---

#### `export_summary_pdf` — `tools/session_summary_tools.py`

```python
@tool
def export_summary_pdf(
    summary_text_path: str = "session_history_summary.txt",
    output_pdf_path: str = "session_history_summary.pdf",
) -> str:
    """
    Export a text summary file to a PDF document.

    Args:
        summary_text_path: Path to the plain-text summary file.
        output_pdf_path: Path where the PDF should be written.

    Returns:
        A status message including PDF output path.
    """
```

---

#### `summarize_session_history_with_gemini` — `tools/session_summary_tools.py`

```python
@tool
def summarize_session_history_with_gemini(
    history_json_path: str = "session_history_payload.json",
    recent_turns: int = 20,
    model_id: str = "meta-llama/llama-3-3-70b-instruct",
    temperature: float = 0.1,
    max_new_tokens: int = 1024,
) -> str:
    """
    Backward-compatible alias retained for existing agent definitions.
    Routes to summarize_session_history_with_llama (Llama 3.3 70B via watsonx.ai).

    Args:
        history_json_path: Path to session history JSON file.
        recent_turns: Number of latest turns to summarize (0 means all).
        model_id: watsonx.ai model ID.
        temperature: Sampling temperature.
        max_new_tokens: Maximum tokens in the generated summary.

    Returns:
        A concise LLM-generated summary of the conversation.
    """
```

---

### 3.3 Translation Tools

#### `translate_to_arabic` — `tools/translate_to_arabic_openapi.yaml`

Defined as an OpenAPI tool spec. Translates provided text to Arabic via an external translation service endpoint. Parameters are resolved from the YAML spec at runtime by the WXO agent framework.

---

### 3.4 Cognos Analytics Tools

#### `cognos_list_content` — `tools/cognos_analytics_tools.py`

```python
@tool
def cognos_list_content(
    search: str = "",
    limit: int = 20,
    path: str = "/api/v1/content",
) -> str:
    """
    List Cognos content entries through Cognos REST API.

    Args:
        search: Optional free-text filter/query supported by your Cognos API gateway.
        limit: Maximum number of items to request.
        path: API path for content listing. Defaults to /api/v1/content.

    Returns:
        JSON string with content entries and metadata.
    """
```

---

#### `cognos_generate_embed_url` — `tools/cognos_analytics_tools.py`

```python
@tool
def cognos_generate_embed_url(
    object_id: str,
    perspective: str = "dashboard",
    hide_navigation: bool = True,
    extra_query_json: str = "",
) -> str:
    """
    Build a Cognos embed URL for a dashboard/report object.

    Args:
        object_id: Cognos object identifier (for example, dashboard/report id).
        perspective: Cognos perspective, typically 'dashboard' or 'report'.
        hide_navigation: If true, app navigation bars are hidden for widget-style embedding.
        extra_query_json: Optional JSON object for additional query params.

    Returns:
        A fully-qualified embed URL.
    """
```

---

#### `cognos_render_widget_iframe` — `tools/cognos_analytics_tools.py`

```python
@tool
def cognos_render_widget_iframe(
    embed_url: str,
    width: str = "100%",
    height: str = "640",
    title: str = "Cognos Analytics Widget",
) -> str:
    """
    Render embeddable iframe HTML for a Cognos widget URL.

    Args:
        embed_url: Fully-qualified Cognos embed URL.
        width: Iframe width (for example 100% or 1200).
        height: Iframe height in pixels/string.
        title: Iframe title attribute for accessibility.

    Returns:
        HTML iframe string.
    """
```

---

#### `cognos_get_widget_bundle` — `tools/cognos_analytics_tools.py`

```python
@tool
def cognos_get_widget_bundle(
    object_id: str,
    perspective: str = "dashboard",
    width: str = "100%",
    height: str = "640",
    title: str = "Cognos Analytics Widget",
) -> str:
    """
    Convenience tool: generate both embed URL and iframe HTML for a Cognos object.

    Args:
        object_id: Cognos dashboard/report object id.
        perspective: 'dashboard' or 'report'.
        width: Iframe width.
        height: Iframe height.
        title: Iframe title.

    Returns:
        JSON string containing embed_url and iframe_html.
    """
```

---

### 3.5 Chart & Visualisation Tools

All chart tools use [QuickChart.io](https://quickchart.io) as the rendering backend and return a hosted image URL + Markdown embed string.

#### `render_pie_chart` — `tools/chart_analytics_tools.py`

```python
@tool
def render_pie_chart(
    labels_json: str,
    values_json: str,
    title: str = "Pie Chart",
    donut: str = "false",
) -> str:
    """
    Generate a pie or donut chart and return it as a Markdown image.

    Args:
        labels_json: JSON array of slice labels. Example: ["USD","EUR","GBP"]
        values_json: JSON array of numeric values matching labels. Example: [50,30,20]
        title: Chart title shown at the top.
        donut: Set to "true" for a donut chart (hollow centre). Default "false".

    Returns:
        Markdown image string that renders as an inline chart.
    """
```

---

#### `render_time_series` — `tools/chart_analytics_tools.py`

```python
@tool
def render_time_series(
    series_json: str,
    title: str = "Time Series",
    x_label: str = "Date",
    y_label: str = "Value",
    area: str = "false",
) -> str:
    """
    Generate a time-series line or area chart and return it as a Markdown image.

    Args:
        series_json: JSON array of series objects. Each object must have:
                     "name" (string), "x" (array of labels), "y" (array of numbers).
                     Example: [{"name":"USD Buy","x":["Jan","Feb","Mar"],"y":[52.1,53.4,53.9]}]
        title:   Chart title.
        x_label: X-axis label.
        y_label: Y-axis label.
        area:    Set to "true" to fill the area under each line. Default "false".

    Returns:
        Markdown image string that renders as an inline chart.
    """
```

---

#### `render_bar_chart` — `tools/chart_analytics_tools.py`

```python
@tool
def render_bar_chart(
    categories_json: str,
    series_json: str,
    title: str = "Bar Chart",
    x_label: str = "",
    y_label: str = "",
    stacked: str = "false",
    horizontal: str = "false",
) -> str:
    """
    Generate a grouped or stacked bar chart and return it as a Markdown image.

    Args:
        categories_json: JSON array of category labels. Example: ["Jan","Feb","Mar"]
        series_json:     JSON array of series objects, each with:
                         "name" (string) and "values" (array of numbers).
                         Example: [{"name":"USD","values":[53.1,53.4,53.9]}]
        title:      Chart title.
        x_label:    X-axis label.
        y_label:    Y-axis label.
        stacked:    Set to "true" to stack bars. Default "false".
        horizontal: Set to "true" for horizontal bars. Default "false".

    Returns:
        Markdown image string that renders as an inline chart.
    """
```

---

#### `render_chart_from_csv` — `tools/chart_csv_tool.py`

```python
@tool
def render_chart_from_csv(
    x_column: str,
    y_columns: str,
    csv_content: str = "",
    file_path: str = "",
    title: str = "Chart",
    area: str = "false",
    aggregate: str = "sum",
    group_by: str = "month",
    chart_type: str = "line",
) -> str:
    """
    Draw a chart from CSV data. Accepts either pasted CSV text OR a file path.
    Provide one of csv_content or file_path — csv_content takes priority.

    The first row must be a header row with column names.
    Date columns are automatically grouped by period and values are aggregated.

    Args:
        x_column:    Header of the column to use as X-axis (dates or categories).
        y_columns:   Comma-separated column headers to plot as data series.
        csv_content: Paste the full CSV text here (header row + data rows).
        file_path:   Full path to a CSV file accessible from the server.
        title:       Chart title.
        area:        Set to "true" to fill under lines. Default "false".
        aggregate:   How to combine values per group: "sum" or "avg". Default "sum".
        group_by:    Time grouping: "month", "quarter", "year", or "none". Default "month".
        chart_type:  "line" or "bar". Default "line".

    Returns:
        Markdown image string that renders as an inline chart in the chat.
    """
```

---

#### `inspect_excel_content` — `tools/chart_excel_ts_tool.py`

```python
@tool
def inspect_excel_content(
    file_path: str,
    sheet_name: str = "",
    max_sample_rows: int = 5,
) -> str:
    """
    Inspect an Excel (.xlsx) file: return sheet names, column headers,
    row count, and a sample of data rows.

    Accepts a local file path OR an https:// URL (e.g. an S3 presigned link).
    Use this BEFORE render_chart_from_excel to discover the exact column names.

    Args:
        file_path:       Full local path to the .xlsx file, OR an https:// URL.
        sheet_name:      Sheet to inspect. Leave empty to use the first sheet.
        max_sample_rows: Number of sample data rows to show (default 5, max 20).

    Returns:
        Sheet names, column headers, row count, and sample rows.
    """
```

---

#### `render_chart_from_excel` — `tools/chart_excel_ts_tool.py`

```python
@tool
def render_chart_from_excel(
    file_path: str,
    x_column: str,
    y_columns: str,
    sheet_name: str = "",
    title: str = "Chart from Excel",
    area: str = "false",
    aggregate: str = "sum",
    group_by: str = "month",
) -> str:
    """
    Read an Excel (.xlsx) file and draw a time-series line chart from its data.

    The first row must be a header row. Date columns are automatically grouped
    by the chosen period (month/quarter/year) and y-values are aggregated.

    Args:
        file_path:   Full path or https:// URL to the Excel file.
        x_column:    Header of the column to use as X-axis (should contain dates).
        y_columns:   Comma-separated column headers to plot as data series.
        sheet_name:  Name of the sheet to read. Leave empty to use the first sheet.
        title:       Chart title.
        area:        Set to "true" to fill the area under each line. Default "false".
        aggregate:   How to combine values in each group: "sum" or "avg". Default "sum".
        group_by:    Time period to group date values: "month", "quarter", "year", or "none".

    Returns:
        Markdown image string that renders as an inline chart in the chat.
    """
```

---

#### `list_s3_files` — `tools/chart_s3_tool.py`

```python
@tool
def list_s3_files(prefix: str = "") -> str:
    """
    List files stored in the AWS S3 bucket configured for this agent.
    Use this to discover available file names before calling render_chart_from_s3
    or inspect_s3_file.

    Args:
        prefix: Optional filter prefix. E.g. "sales/" to list only files
                in a folder named 'sales'. Leave blank to list all files.

    Returns:
        A numbered list of file names and their sizes.
    """
```

---

#### `inspect_s3_file` — `tools/chart_s3_tool.py`

```python
@tool
def inspect_s3_file(file_key: str) -> str:
    """
    Download a CSV file from the AWS S3 bucket and inspect its structure:
    column names, row count, and a 3-row sample.

    Use this before render_chart_from_s3 to confirm exact column names.

    Args:
        file_key: The exact file name (key) in the S3 bucket.
                  Use list_s3_files to find available files.

    Returns:
        Column names, row count, and sample rows from the file.
    """
```

---

#### `render_chart_from_s3` — `tools/chart_s3_tool.py`

```python
@tool
def render_chart_from_s3(
    file_key: str,
    x_column: str,
    y_columns: str,
    title: str = "Chart",
    area: str = "false",
    aggregate: str = "sum",
    group_by: str = "month",
    chart_type: str = "line",
) -> str:
    """
    Download a CSV file from AWS S3 and draw a chart from it.

    Args:
        file_key:   The exact file name (key) in the S3 bucket.
        x_column:   Column name to use as the X-axis (dates or categories).
        y_columns:  Comma-separated column names to plot as data series.
        title:      Chart title displayed above the chart.
        area:       "true" to fill area under lines (area chart). Default "false".
        aggregate:  How to combine values per group: "sum" or "avg". Default "sum".
        group_by:   Time grouping: "month", "quarter", "year", or "none". Default "month".
        chart_type: "line" or "bar". Default "line".

    Returns:
        Markdown image that renders as an inline chart, plus a download link.
    """
```

---

#### `list_cos_files` — `tools/chart_cos_tool.py`

```python
@tool
def list_cos_files(prefix: str = "") -> str:
    """
    List files stored in the IBM Cloud Object Storage bucket.

    Args:
        prefix: Optional filter prefix. Leave blank to list all files.

    Returns:
        A numbered list of file names and their sizes.
    """
```

---

#### `inspect_cos_file` — `tools/chart_cos_tool.py`

```python
@tool
def inspect_cos_file(file_key: str) -> str:
    """
    Download a CSV file from IBM Cloud Object Storage and inspect its structure:
    column names, row count, and a 3-row sample.

    Args:
        file_key: The exact file name (key) in the bucket.

    Returns:
        Column names, row count, and sample rows from the file.
    """
```

---

#### `render_chart_from_cos` — `tools/chart_cos_tool.py`

```python
@tool
def render_chart_from_cos(
    file_key: str,
    x_column: str,
    y_columns: str,
    title: str = "Chart",
    area: str = "false",
    aggregate: str = "sum",
    group_by: str = "month",
    chart_type: str = "line",
) -> str:
    """
    Download a CSV file from IBM Cloud Object Storage and draw a chart from it.

    Args:
        file_key:   The exact file name (key) in the COS bucket.
        x_column:   Column name to use as the X-axis (dates or categories).
        y_columns:  Comma-separated column names to plot as data series.
        title:      Chart title displayed above the chart.
        area:       "true" to fill area under lines. Default "false".
        aggregate:  "sum" or "avg". Default "sum".
        group_by:   "month", "quarter", "year", or "none". Default "month".
        chart_type: "line" or "bar". Default "line".

    Returns:
        Markdown image that renders as an inline chart, plus a download link.
    """
```

---

### 3.6 File Storage Tools

#### `upload_file_to_s3` — `tools/chart_s3_tool.py`

```python
@tool
def upload_file_to_s3(
    file_key: str,
    content: str,
) -> str:
    """
    Upload text content (CSV, JSON, TXT, etc.) as a file to the AWS S3 bucket.

    How to use:
      1. Paste your CSV/text content into the 'content' parameter.
      2. Provide a filename as 'file_key' (e.g. "sales_data.csv").
      3. The file will be created (or overwritten) in the S3 bucket.

    Args:
        file_key: The filename to store in S3. Can include a folder path:
                  "reports/sales_2025.csv".
        content:  The full text content to upload (CSV rows, JSON, etc.).

    Returns:
        Confirmation message with the file location, or an error description.
    """
```

---

#### `upload_chat_file_to_s3` — `tools/file_upload_s3_tool.py`

```python
@tool
def upload_chat_file_to_s3(
    filename: str,
    file_content: str,
    s3_folder: str = "uploads",
    is_base64: bool = False,
) -> str:
    """
    Upload a file shared in the chat to the AWS S3 bucket and return a
    24-hour presigned download link.

    Call this tool AUTOMATICALLY whenever the user shares or uploads any
    file in the conversation — do not wait for explicit instructions.

    Supports plain text files (CSV, JSON, TXT, Markdown, YAML, XML) and
    base64-encoded binary files (PDF, XLSX, PNG, JPG, etc.).

    Args:
        filename:     Original filename including extension, e.g. "sales.csv".
        file_content: The file contents as a UTF-8 string for text files, or
                      as a base64-encoded string for binary files.
        s3_folder:    S3 prefix / virtual folder to store the file in.
                      Defaults to "uploads".
        is_base64:    Set True if file_content is base64-encoded (binary files).

    Returns:
        A confirmation message with the S3 key and a 24-hour download link.
    """
```

---

### 3.7 Web Fetch Tool

#### `fetch_text_from_url` — `tools/fetch_text_from_url_tool.py`

```python
@tool
def fetch_text_from_url(
    url: str,
    max_chars: int = 100000,
) -> str:
    """
    Fetch the text content of a file from a URL (e.g. an S3 presigned download link)
    and return it as a plain-text string.

    Use this after `upload_chat_file_to_s3` to retrieve the uploaded CSV, JSON, or
    TXT content so it can be passed to analysis and charting tools.

    Args:
        url:       The URL to fetch. Typically the presigned download link returned
                   by `upload_chat_file_to_s3`.
        max_chars: Maximum number of characters to return. Defaults to 100 000.

    Returns:
        The text content of the file as a plain string, or an error message.
    """
```

---

### 3.8 Document Generation Tools

#### `pdf_report_tool` — `tools/pdf_report_tool.py`

```python
@tool
def pdf_report_tool(
    title: str,
    content: str,
    filename: str = "",
    subtitle: str = "",
    s3_folder: str = "reports",
    chart_urls: str = "",
) -> str:
    """
    Generate a formatted PDF report and upload it to S3.
    Returns a download link valid for 24 hours.

    Content supports simple markdown formatting:
        # Heading 1 / ## Heading 2 / ### Heading 3
        - bullet item
        | Col1 | Col2 |   (pipe tables)
        ---              (horizontal divider)
        ``` code block ```
        ![alt](url)      (embedded chart image)

    Args:
        title:      The main title shown at the top of the PDF.
        content:    The body text in markdown-like syntax.
        filename:   Filename to use in S3, e.g. "monthly_report.pdf".
        subtitle:   Optional subtitle line below the title.
        s3_folder:  S3 folder prefix (default: "reports").
        chart_urls: One or more QuickChart image URLs to append. Separate with newline or comma.

    Returns:
        A message with the S3 key and a 24-hour download link.
    """
```

#### `create_pdf_report` / `create_word_report` — `tools/pdf_report_tool.py`

```python
@tool
def create_pdf_report(
    content: str,
    title: str = "Report",
    topic: str = "",
    thread_id: str = "",
    language: str = "English",
    filename: str = "",
    s3_folder: str = "reports",
) -> str:
    """
    Create a PDF report from LLM-written content and upload it to S3.

    The agent writes the full content before calling this tool.
    Content uses ## headings, - bullets, and ![alt](url) for chart embeds.

    Args:
        content:   Full report body in markdown-like syntax.
        title:     Report title shown at the top.
        topic:     Optional topic description (used for filename if filename is empty).
        thread_id: Thread ID for conversation summaries (used for filename).
        language:  "English" (default) or "Arabic" — controls RTL rendering.
        filename:  Optional explicit S3 filename.
        s3_folder: S3 folder prefix. Default "reports".

    Returns:
        Download link (valid 24 hours) and S3 key.
    """

@tool
def create_word_report(
    content: str,
    title: str = "Report",
    topic: str = "",
    thread_id: str = "",
    language: str = "English",
    filename: str = "",
    s3_folder: str = "reports",
) -> str:
    """
    Create a Word (.docx) report from LLM-written content and upload it to S3.
    Same parameters and content format as create_pdf_report.
    """
```

**PDF rendering pipeline:**
1. Preferred: `reportlab` (vector PDF, text-selectable, full Arabic RTL).
2. Fallback: `Pillow` (raster image-based PDF, 150 DPI, always works).
3. Arabic font: **Amiri** (downloaded from S3 → GitHub CDN → raw GitHub at runtime, cached in `/tmp`).
4. Rendered output is uploaded to S3; a 24-hour pre-signed URL is returned.

---

### 3.9 Email Delivery Tool

#### `send_file_via_sendgrid` — `tools/sendgrid_email_tool.py`

```python
@tool
def send_file_via_sendgrid(
    to_email: str,
    subject: str,
    body: str,
    filename: str = "",
    file_content: str = "",
    is_base64: bool = False,
    file_url: str = "",
) -> str:
    """
    Send a file to a user via SendGrid email API.

    Provide either:
      - file_url (tool downloads and attaches it), OR
      - file_content (+ filename), where content is plain text or base64.

    Args:
        to_email:     Recipient email address.
        subject:      Email subject.
        body:         Email body text.
        filename:     Attachment filename. Required with file_content; optional with file_url.
        file_content: Raw text or base64 string for attachment bytes.
        is_base64:    Set True when file_content is base64-encoded.
        file_url:     Optional URL to fetch and attach.

    Returns:
        Success/failure status including SendGrid response code.
    """
```

---

### 3.10 Table / Spreadsheet Export Tool

#### `export_table_to_file` — `tools/export_table_tool.py`

```python
@tool
def export_table_to_file(
    data: str,
    file_format: str = "xlsx",
    filename: str = "",
    sheet_name: str = "Data",
    s3_folder: str = "exports",
) -> str:
    """Export tabular data to an Excel (XLSX) or CSV file and return a download link.

    Args:
        data: The table data to export. Accepted formats:
              - A JSON array of objects: '[{"Name":"Alice","Age":30}, ...]'
              - A CSV string: 'Name,Age\\nAlice,30\\n...'
              - A Markdown pipe table: '| Name | Age |\\n|------|-----|\\n| Alice | 30 |'
        file_format: Output format — "xlsx" (default) or "csv".
        filename: Optional base filename (without extension). A timestamp is appended.
        sheet_name: Sheet name for XLSX files (default: "Data").
        s3_folder: S3 folder/prefix for the uploaded file (default: "exports").

    Returns:
        A presigned S3 download URL valid for 24 hours, or an error message.
    """
```

---

### 3.11 Financial Analysis Tools

#### `analyze_bank_statement` — `tools/bank_statement_tool.py`

```python
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
      - csv_files:      JSON array of CSV strings — one entry per uploaded file.
                        Example: '["date,desc,debit,credit\\n...", "..."]'
                        The tool merges them automatically, removing duplicate header rows.
      - csv_content:    Single CSV text of the bank statement.
                        The first row must be a header row with column names.
      - statement_text: Raw text of a PDF bank statement. An LLM will extract transactions.

    Calculates:
      - Monthly income, spending, savings, debt obligations, and running balance
      - Average monthly income, spending, and savings
      - Top spending category (Food, Education, Transport, etc.)
      - Debt-to-income ratio and existing debt payment amount
      - Positive cash flow month count and savings growth percentage

    Args:
        csv_content:    Full CSV text. Required column roles auto-detected:
                          Date / Description / Debit / Credit (or Amount+Type) / Balance
        csv_files:      JSON array of CSV strings for multi-file upload.
                        When provided, csv_content is ignored.
        statement_text: Raw text from a PDF bank statement (LLM extraction path).
        currency:       Currency label (default: "EGP").
        language:       "English" or "Arabic" for summary text.
        model:          LLM for PDF extraction: "deepseek" (default), "llama4",
                        "llama3", "granite", "mistral".

    Returns:
        A JSON string with all computed metrics and a human-readable summary.
        Pass this string to simulate_mortgage_affordability or render_financial_chart.
    """
```

**Column alias detection** — the CSV parser auto-detects columns by fuzzy matching:

| Role | Accepted header variants |
|------|--------------------------|
| Date | `date`, `trans date`, `transaction date`, `value date`, `تاريخ` |
| Description | `description`, `narration`, `particulars`, `details`, `بيان`, `وصف` |
| Debit | `debit`, `withdrawal`, `dr`, `amount dr`, `خصم`, `سحب` |
| Credit | `credit`, `deposit`, `cr`, `amount cr`, `إيداع` |
| Amount | `amount`, `transaction amount`, `net amount`, `مبلغ` |
| Balance | `balance`, `running balance`, `closing balance`, `رصيد` |
| Type | `type`, `transaction type`, `dr/cr`, `نوع` |

**Spending categories** auto-assigned by keyword match:
`Income`, `Debt Payment`, `Housing`, `Education`, `Healthcare`, `Transport`, `Food`, `Entertainment`, `Savings`, `Other`

---

#### `simulate_mortgage_affordability` — `tools/bank_statement_tool.py`

```python
@tool
def simulate_mortgage_affordability(
    analysis_json: str,
    currency: str = "EGP",
    max_dti: float = 40.0,
) -> str:
    """
    Simulate mortgage affordability based on a bank statement analysis.

    Uses a Debt-to-Income (DTI) model:
      - Maximum allowed total debt = avg_income × (max_dti / 100)
      - Available for new mortgage  = max_total_debt − existing_debt_payments
      - Safe range = 75%–95% of available, capped at 90% of avg monthly savings

    Args:
        analysis_json: JSON string returned by analyze_bank_statement.
        currency:      Currency label for display (default: "EGP").
        max_dti:       Maximum allowed DTI ratio as a percentage (default: 40.0).
                       Banks typically use 35–45%.

    Returns:
        A human-readable affordability assessment with:
        - Recommended monthly installment range (min / max)
        - Cash flow health indicator
        - Income consistency note
        - Step-by-step calculation breakdown
        - Key assumptions / disclaimer
    """
```

---

#### `render_financial_chart` — `tools/bank_statement_tool.py`

```python
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
      - "cashflow"           — Bar chart with green/red bars (positive/negative)
      - "categories"         — Doughnut chart: spending breakdown by category

    Args:
        analysis_json: JSON string returned by analyze_bank_statement.
        chart_type:    One of: "savings", "income_vs_spending", "cashflow", "categories".
        title:         Optional custom chart title.

    Returns:
        Markdown image string that renders as an inline chart in the chat,
        plus a download link.
    """
```

**Category colour map used by the `categories` doughnut chart:**

```python
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
```

---

### Financial Metrics Computed by `analyze_bank_statement`

| Metric | Description |
|--------|-------------|
| `avg_monthly_income` | Total income ÷ number of months |
| `avg_monthly_spending` | Total spending ÷ months |
| `avg_monthly_savings` | Total savings ÷ months |
| `avg_monthly_debt` | Total debt payments ÷ months |
| `avg_balance` | Average of month-end balances |
| `debt_obligations_pct` | avg_debt / avg_income × 100 |
| `top_spending_category` | Category with highest total spend (excluding Debt Payment) |
| `positive_cashflow_months` | Count of months where savings > 0 |
| `savings_growth_pct` | (last_month_savings − first_month_savings) / first × 100 |
| `monthly_income` | Dict: month → income amount |
| `monthly_spending` | Dict: month → spending amount |
| `monthly_savings` | Dict: month → savings amount |
| `category_totals` | Dict: category → total amount spent |

---

## 4. Environment Variables

| Variable | Purpose |
|----------|---------|
| `WO_INSTANCE` / `WXO_API_ENDPOINT` | WXO instance API base URL |
| `WO_API_KEY` / `WXO_API_KEY` | WXO authentication key |
| `WXO_AGENT_NAME` | Target agent name (resolved to UUID at runtime) |
| `WXO_AGENT_ID` | Target agent UUID |
| `WATSONX_API_KEY` | IBM watsonx.ai API key |
| `WATSONX_PROJECT_ID` | watsonx.ai project ID |
| `WATSONX_URL` | watsonx.ai endpoint URL |
| `WATSONX_MODEL` | Default model ID (e.g. `meta-llama/llama-4-maverick-17b-128e-instruct-fp8`) |
| `AWS_ACCESS_KEY_ID` | AWS S3 access key |
| `AWS_SECRET_ACCESS_KEY` | AWS S3 secret key |
| `AWS_S3_BUCKET` | S3 bucket for file uploads/storage |
| `AWS_S3_REGION` | S3 bucket region |
| `COS_API_KEY` | IBM Cloud Object Storage API key |
| `COS_INSTANCE_CRN` | COS instance CRN |
| `COS_BUCKET` | COS bucket name |
| `COS_ENDPOINT` | COS endpoint URL |
| `SENDGRID_API_KEY` | SendGrid API key for email delivery |
| `SENDGRID_FROM_EMAIL` | Sender email address |
| `SENDGRID_FROM_NAME` | Sender display name (`KAI Pulse Agent`) |
| `SMTP_HOST` / `SMTP_PORT` etc. | Optional SMTP credentials for report email delivery |
| `DB_TYPE` | Local DB type (`sqlite`) |
| `DB_SQLITE_PATH` | Path to the SQLite session history DB |

---

## 5. LLM Routing

The agent uses multiple LLM backends depending on the tool:

| Model key | Model ID | Used by |
|-----------|----------|---------|
| `llama4` | `meta-llama/llama-4-maverick-17b-128e-instruct-fp8` | Default WXO agent LLM, report generation |
| `llama3` | `meta-llama/llama-3-3-70b-instruct` | watsonx fallback |
| `granite` | `ibm/granite-13b-instruct-v2` | watsonx fallback |
| `mistral` | `mistralai/mistral-large` | watsonx fallback |
| `deepseek` | `deepseek-chat` | Bank statement extraction (primary) |
| `deepseek-r1` | `deepseek-reasoner` | Advanced reasoning tasks |

---

## 6. Data Flow Diagrams

### 6.1 Bank Statement Analysis

```
User uploads CSV
      │
      ▼
 File Upload Rule  ──────► upload_chat_file_to_s3 (silent backup)
      │
      ▼
 Scenario detection (A / B / C)
      │
      ├── A: analyze_bank_statement(csv_content=...)
      ├── B: Acknowledge, wait for more files
      └── C: Collect all prior CSVs → analyze_bank_statement(csv_files=[...])
                  │
                  ▼
          Returns analysis_json
                  │
         ┌────────┼────────────┐
         ▼        ▼            ▼
  render_financial_chart   simulate_mortgage_affordability
  (savings / income_vs_spending   (max loan, monthly repayment,
   / cashflow / categories)        debt-to-income ratio)
         │
         ▼
  create_pdf_report (with embedded chart URLs)
         │
         ▼
  send_file_via_sendgrid (optional)
```

### 6.2 Report Generation

```
User: "Create a PDF report about X"
      │
      ▼
 Agent asks: "PDF or Word?"
      │
      ▼
 Agent writes full content (LLM-generated markdown)
      │
      ▼
 create_pdf_report  or  create_word_report
      │  ├── _build_pdf() → ReportLab vector PDF (primary)
      │  └── _build_pdf() → Pillow raster PDF (fallback)
      │
      ▼
 Upload to S3  →  pre-signed 24h URL returned to user
```

### 6.3 Chart from S3/COS/CSV

```
User: "Chart the sales data from S3"
      │
      ▼
 list_s3_files  →  user picks file
      │
      ▼
 inspect_s3_file  →  shows headers
      │
      ▼
 render_chart_from_s3(key, x_column, y_columns, chart_type, ...)
      │
      ▼
 QuickChart.io renders image  →  hosted URL returned
```

---

## 7. Bilingual (Arabic) Support

The agent and its report/PDF tools have deep Arabic RTL support:

- **Language detection**: examines user messages for Arabic script characters.
- **`create_pdf_report` with `language: "Arabic"`**: writes content in Arabic, renders RTL using the **Amiri** TrueType font.
- **Amiri font acquisition**: downloaded from S3 bucket first, then GitHub CDN, then raw GitHub — cached in `/tmp` for the process lifetime.
- **Text rendering pipeline**: `arabic_reshaper` (letter form shaping) → `python-bidi` (visual reorder) → drawn right-aligned in PDF.
- **Bullet lists in Arabic**: rendered with bullet `•` on the right side.
- **`analyze_bank_statement` with `language: "Arabic"`**: all metric labels and chart titles are in Arabic.
- **`translate_to_arabic`**: standalone translation tool via OpenAPI service.

---

## 8. Security Notes

- API keys and credentials are read from environment variables at runtime — never hard-coded in tool logic (defaults are only development fallbacks).
- S3 file access uses AWS Signature V4 (SigV4) signed requests implemented in pure Python stdlib — no boto3 dependency.
- All S3 download links are pre-signed with a **24-hour expiry**.
- SendGrid credentials are never surfaced to the user in conversation.
- The agent does not generate or reveal secrets in its responses.

---

## 9. Tool Count Summary

| Category | Tool count |
|----------|-----------|
| Utility | 3 |
| Session & Payload | 6 |
| Translation | 1 |
| Cognos Analytics | 4 |
| Chart primitives | 3 |
| CSV charts | 4 |
| Excel charts | 2 |
| S3 charts | 3 |
| COS charts | 3 |
| File storage | 2 |
| Web fetch | 1 |
| Document generation | 3 |
| Email delivery | 1 |
| Table/spreadsheet export | 1 |
| Financial analysis | 3 |
| **Total** | **40** |
