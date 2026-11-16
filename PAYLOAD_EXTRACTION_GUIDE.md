# Complete Guide: Extracting Agent Payloads from watsonx Orchestrate to an External DB

## Overview

This guide covers **three complementary approaches** to capture every request,
response, tool call, run event, and trace that flows through an IBM watsonx
Orchestrate agent, and persist all of it to an external database.

| Approach | Best for | Files involved |
|---|---|---|
| **1 – In-agent tool** | Real-time, per-turn capture inside the agent | `tools/payload_extractor_tool.py` |
| **2 – Pipeline extractor** | Batch / polling from outside the agent | `tools/pipeline_extractor.py` |
| **3 – Observability (Langfuse / Traces API)** | Deep LLM span telemetry | Built into the ADK SDK |

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────┐
│          IBM watsonx Orchestrate (SaaS / DevEd)  │
│                                                  │
│  ┌──────────┐  Chat API   ┌─────────────────┐    │
│  │  User /  │ ──────────► │   Agent Engine  │    │
│  │  App     │ ◄────────── │  (LLM + Tools)  │    │
│  └──────────┘  SSE/JSON   └────────┬────────┘    │
│                                    │             │
│  ┌──────────────────────────────┐  │ tool call   │
│  │  Message Threads API         │  │             │
│  │  /v1/threads/{id}/messages   │◄─┘             │
│  └──────────────────────────────┘                │
│  ┌──────────────────────────────┐                │
│  │  Runs API                    │                │
│  │  /v1/orchestrate/runs        │                │
│  └──────────────────────────────┘                │
│  ┌──────────────────────────────┐                │
│  │  Observability / Traces API  │                │
│  │  (TracesController SDK)      │                │
│  └──────────────────────────────┘                │
└───────────────────┬──────────────────────────────┘
                    │ REST / SDK
                    ▼
        ┌───────────────────────┐
        │  pipeline_extractor   │  ◄── runs on your infra
        │  (or in-agent tool)   │       (cron / Cloud Functions)
        └──────────┬────────────┘
                   │ SQLAlchemy / pymongo
                   ▼
        ┌───────────────────────┐
        │   External Database   │
        │  PostgreSQL / MySQL   │
        │  SQLite / MongoDB     │
        └───────────────────────┘
```

---

## Part 1 – Understanding the Payload Structure

### 1.1 Chat Request (POST `/v1/orchestrate/{agent_id}/chat/completions`)

```json
{
  "messages": [
    {
      "role": "user",
      "content": "What is the weather in Paris?"
    }
  ],
  "stream": false,
  "context": { "session_id": "abc123" },
  "additional_parameters": {}
}
```

### 1.2 Chat Response (`ChatCompletionResponse`)

```json
{
  "id": "uuid-of-completion",
  "object": "chat.completion",
  "created": 1714000000,
  "model": "meta-llama/llama-3-70b-instruct",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "The weather in Paris is 18 °C and sunny."
      },
      "finish_reason": "stop"
    }
  ],
  "thread_id": "uuid-of-thread",
  "usage": {
    "prompt_tokens": 45,
    "completion_tokens": 22,
    "total_tokens": 67
  }
}
```

### 1.3 Run Event (`RunEvent`) – from `/v1/orchestrate/runs/{run_id}/events`

```json
{
  "id": "evt_abc",
  "event": "run.step.completed",
  "data": {
    "id": "step-uuid",
    "type": "tool_calls",
    "step_details": {
      "tool_calls": [
        {
          "id": "call_xyz",
          "type": "function",
          "function": {
            "name": "hello_tool",
            "arguments": "{\"name\": \"Alice\"}",
            "output": "Hello, Alice! Welcome to watsonx Orchestrate."
          }
        }
      ]
    }
  }
}
```

### 1.4 Message Thread (`Message`) – from `/v1/threads/{thread_id}/messages`

```json
{
  "id": "msg-uuid",
  "tenant_id": "tenant-uuid",
  "thread_id": "thread-uuid",
  "role": "assistant",
  "content": [
    { "response_type": "text", "text": "Hello, Alice!" }
  ],
  "additional_properties": {
    "tool_calls": [...],
    "tool_name": "hello_tool",
    "tool_call_id": "call_xyz"
  },
  "step_history": [...],
  "created_on": "2026-05-02T10:00:00Z"
}
```

### 1.5 AssistantRun – from `/v1/orchestrate/runs`

Key fields persisted:

| Field | Description |
|---|---|
| `id` | Run UUID |
| `agent_id` | The agent that handled this run |
| `thread_id` | Linked conversation thread |
| `status` | `completed` / `failed` / `cancelled` / … |
| `trace_id` | Links to an observability trace |
| `usage` | Token counts (`prompt_tokens`, `completion_tokens`) |
| `llm_params` | LLM hyperparameters used |
| `guardrails` | HAP / PII / social-bias guardrail output |
| `result` | Final structured result of the run |
| `created_at` / `completed_at` | Timing data |

---

## Part 2 – Database Schema

The schema is auto-created by `_ensure_table()` in `payload_extractor_tool.py`:

```sql
CREATE TABLE agent_payloads (
    id              VARCHAR(36)  PRIMARY KEY,   -- UUID of this DB record
    agent_id        VARCHAR(255),               -- WxO agent UUID
    thread_id       VARCHAR(36),                -- conversation thread UUID
    run_id          VARCHAR(36),                -- assistant run UUID
    session_id      VARCHAR(255),               -- app-level session
    user_id         VARCHAR(255),               -- end-user identifier
    tenant_id       VARCHAR(255),               -- IBM tenant UUID
    captured_at     TIMESTAMP    NOT NULL,      -- UTC insertion time
    direction       VARCHAR(16)  NOT NULL,      -- request | response | run | step | event | trace
    role            VARCHAR(32),                -- user | assistant | tool
    content_type    VARCHAR(64),                -- text | forms | search | …
    content_text    TEXT,                       -- plain-text content
    tool_name       VARCHAR(255),               -- tool invoked (if any)
    tool_call_id    VARCHAR(255),               -- tool call correlation ID
    tool_calls      TEXT,                       -- JSON array of tool calls
    raw_payload     TEXT         NOT NULL,      -- complete JSON payload
    trace_id        VARCHAR(255),               -- observability trace ID
    status          VARCHAR(64),                -- run/event status string
    token_input     INTEGER,                    -- input token count
    token_output    INTEGER                     -- output token count
);
```

### Recommended indexes

```sql
CREATE INDEX idx_ap_agent_id     ON agent_payloads (agent_id);
CREATE INDEX idx_ap_thread_id    ON agent_payloads (thread_id);
CREATE INDEX idx_ap_run_id       ON agent_payloads (run_id);
CREATE INDEX idx_ap_captured_at  ON agent_payloads (captured_at DESC);
CREATE INDEX idx_ap_direction    ON agent_payloads (direction);
```

---

## Part 3 – Setup & Installation

### 3.1 Install dependencies

```bash
pip install -r requirements.txt
```

### 3.2 Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3.3 Register the capture tool with the ADK

The `capture_agent_payload` tool is already listed in `agents/my_agent.yaml`:

```yaml
tools:
  - hello_tool
  - capture_agent_payload
```

Import and deploy:

```bash
orchestrate tools import -k tools/payload_extractor_tool.py
orchestrate agents import -f agents/my_agent.yaml
orchestrate agents deploy my_agent
```

---

## Part 4 – Approach 1: In-Agent Real-Time Capture

The `capture_agent_payload` tool is an ADK `@tool` the agent can invoke at
the end of its turn. The agent's instructions should direct it to call this
tool after every response.

### Agent instruction example

Add to your agent's `instructions` in `my_agent.yaml`:

```yaml
instructions: |
  You are a helpful assistant.
  After you complete EVERY response, call the capture_agent_payload tool
  with:
    - user_message: the user's original input
    - agent_response: your final answer
    - agent_id: the current agent ID (if known)
    - thread_id: the current thread ID (if known)
  This is mandatory for auditing purposes.
```

### What gets saved

Each call to `capture_agent_payload` writes **two rows**:

| direction | role | content_text |
|---|---|---|
| `request` | `user` | user's original message |
| `response` | `assistant` | agent's reply |

---

## Part 5 – Approach 2: Pipeline Extractor (Batch/Polling)

Run `tools/pipeline_extractor.py` outside the agent to pull all historical
and ongoing data from the watsonx Orchestrate REST API.

### Run once (good for back-fill)

```bash
python tools/pipeline_extractor.py --once --agent-id <your-agent-uuid>
```

### Run as a continuous polling daemon

```bash
python tools/pipeline_extractor.py --agent-id <your-agent-uuid> --interval 120
```

### Run as a cron job (Linux/macOS)

```cron
# Every 5 minutes
*/5 * * * * cd /path/to/project && python tools/pipeline_extractor.py --once >> /var/log/wxo_extractor.log 2>&1
```

### What gets extracted per cycle

| Step | API called | Records created |
|---|---|---|
| List all runs | `GET /v1/orchestrate/runs` | 1 per run |
| Fetch run events | `GET /v1/orchestrate/runs/{id}/events` | N per run |
| List threads | `GET /v1/threads` | — |
| List messages per thread | `GET /v1/threads/{id}/messages` | 1 per message |
| Fetch traces (SDK) | `TracesController.search_traces()` | 1 per trace |

---

## Part 6 – Approach 3: Observability / Langfuse Integration

For deep LLM-level span data (token timing, model inputs/outputs, tool call
latency), configure Langfuse **or** the IBM native telemetry:

### 6.1 IBM Native Telemetry (Developer Edition)

```bash
orchestrate env start --with-ibm-telemetry
# Then browse http://localhost:8765 for the Langfuse-compatible UI
```

### 6.2 Connect to hosted Langfuse (SaaS)

```bash
orchestrate settings observability langfuse configure \
  --url "https://cloud.langfuse.com/api/public/otel" \
  --api-key "sk-lf-YOUR_KEY" \
  --health-uri "https://cloud.langfuse.com" \
  --config-json '{"public_key": "pk-lf-YOUR_PUBLIC_KEY"}'
```

### 6.3 Pull traces programmatically into your DB

The `process_traces()` function in `pipeline_extractor.py` calls the ADK
`TracesController` to search and export spans, then writes them to the DB
under `direction='trace'`.

```python
from tools.pipeline_extractor import process_traces
process_traces(agent_id="your-agent-uuid")
```

Each trace record in the DB contains:
- Full OTLP-style `resourceSpans → scopeSpans → spans` hierarchy (in `raw_payload`)
- `trace_id`, `agent_id`, `captured_at`
- Duration, span counts, error spans in `_trace_summary` (inside `raw_payload`)

---

## Part 7 – Querying the Data

### All conversations for a specific agent (last 7 days)

```sql
SELECT thread_id, direction, role, content_text, captured_at
FROM   agent_payloads
WHERE  agent_id    = 'YOUR_AGENT_UUID'
  AND  captured_at >= NOW() - INTERVAL '7 days'
ORDER  BY captured_at;
```

### Full conversation replay for a thread

```sql
SELECT direction, role, content_text, tool_name, captured_at
FROM   agent_payloads
WHERE  thread_id = 'YOUR_THREAD_UUID'
ORDER  BY captured_at;
```

### Runs that failed

```sql
SELECT run_id, status, content_text AS error, captured_at
FROM   agent_payloads
WHERE  direction = 'run'
  AND  status    = 'failed';
```

### Token usage over time (daily)

```sql
SELECT DATE(captured_at)     AS day,
       SUM(token_input)      AS total_input_tokens,
       SUM(token_output)     AS total_output_tokens
FROM   agent_payloads
WHERE  direction IN ('run', 'response')
GROUP  BY 1
ORDER  BY 1 DESC;
```

### Tool calls frequency

```sql
SELECT tool_name, COUNT(*) AS call_count
FROM   agent_payloads
WHERE  tool_name IS NOT NULL
GROUP  BY tool_name
ORDER  BY call_count DESC;
```

---

## Part 8 – Security Considerations

| Concern | Mitigation |
|---|---|
| Credential management | Store all secrets in `.env` (never in code). Rotate API keys regularly. |
| PII in payloads | Enable `mask_pii: true` in Langfuse config; scrub `content_text` before insert if needed. |
| DB access control | Create a dedicated DB user with `INSERT`-only privileges on `agent_payloads`. |
| Token expiry | IAM tokens expire after ~1 hour. The extractor calls `get_iam_token()` fresh on each cycle. |
| SQL injection | All DB writes use SQLAlchemy parameterised queries — no string interpolation. |
| Transport security | Always use TLS (`postgresql+psycopg2://` with `sslmode=require`). |

---

## Part 9 – File Reference

| File | Purpose |
|---|---|
| `tools/payload_extractor_tool.py` | ADK `@tool` + `save_payload_to_db()` helper |
| `tools/pipeline_extractor.py` | Stand-alone batch extractor & polling daemon |
| `agents/my_agent.yaml` | Agent definition (now includes `capture_agent_payload`) |
| `.env.example` | Environment variable template |
| `requirements.txt` | Python dependencies |

---

## Quick-Start Checklist

- [ ] Copy `.env.example` → `.env` and fill in credentials  
- [ ] `pip install -r requirements.txt`  
- [ ] Create the target database and user  
- [ ] `python tools/pipeline_extractor.py --once` — verify rows appear  
- [ ] `orchestrate tools import -k tools/payload_extractor_tool.py`  
- [ ] `orchestrate agents import -f agents/my_agent.yaml`  
- [ ] Add capture instructions to your agent's system prompt  
- [ ] Schedule `pipeline_extractor.py` as a cron job / Cloud Function  
- [ ] (Optional) Configure Langfuse for LLM-level trace data  
