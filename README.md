# SAP CPI Self-Healing Agent

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![LangChain](https://img.shields.io/badge/LangChain-0.3%2B-brightgreen.svg)](https://langchain.com/)
[![HANA Cloud](https://img.shields.io/badge/SAP%20HANA-Cloud-blue.svg)](https://www.sap.com/products/technology-platform/hana.html)

An intelligent, autonomous SAP Cloud Platform Integration (CPI) monitoring and self-healing system powered by AI. The system automatically detects failed integration messages via **Solace PubSub+ AEM queue** or direct SAP CPI polling, performs AI-driven Root Cause Analysis through a **multi-agent pipeline**, and applies fixes to iFlows — with no manual intervention required.

---

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Copy and fill environment variables
cp .env.example .env   # then edit .env with your credentials

# 3. (One-time) Build the SAP Notes knowledge base
uv run playwright install chromium
uv run python scrape_sap_docs.py --notes-only --notes-file sap_notes_1.txt
uv run python vectorize_docs.py

# 4. Start the server (development — hot reload)
APP_ENV=development uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# 4. Start the server (production)
uvicorn main:app --host 0.0.0.0 --port 8080
```

API docs: `http://localhost:8080/docs`

---

## Features

### Core Capabilities

- **Autonomous Error Detection** — Receives failed CPI messages in real-time from Solace PubSub+ AEM queue (`FailedLogscapturing_Schedule` iFlow → AEM → self-healing agent). Falls back to direct SAP CPI polling when AEM is disabled
- **SAP Multimap XML Parsing** — Parses the SAP CPI multimap XML format (`<multimap:Messages><multimap:Message1><Error>…</Error></multimap:Message1></multimap:Messages>`). One Solace message may contain N `<Error>` blocks — each is split into an independent incident
- **OData iFlow Name Resolution** — Extracts the MPL ID (GUID) from each `<Error>` block and calls `GET /api/v1/MessageProcessingLogs('{guid}')` to resolve `IntegrationFlowName`, `Sender`, `Receiver`, `LogStart`, `LogEnd`
- **Multi-Agent Pipeline** — Dedicated specialist agents (Observer → Classifier → RCA → Fix → Verifier) coordinated by an OrchestratorAgent; each agent has its own filtered tool set and LLM deployment
- **AI Root Cause Analysis (RCA)** — LangChain agent analyses errors using message logs, actual iFlow configuration (`get-iflow`), top-5 SAP notes from vector store, HANA knowledge base, and rule-based classifier with priority-ordered keyword matching
- **Self-Healing Fix Pipeline** — Downloads iFlow config, applies a reasoned fix, updates and deploys — with automatic unlock handling, 600 s agent timeout, per-stage failure diagnosis, and retry logic
- **Pre-Update XML Validator** — Python validator intercepts every `update-iflow` call before it reaches SAP CPI: checks filepath matches the original, validates XML is well-formed, rejects `ifl:property` at collaboration level, and blocks version attribute changes
- **SFTP Error Routing** — All SFTP-class errors are classified as `SFTP_ERROR` and routed directly to `TICKET_CREATED` — never wastes a fix attempt on errors requiring server-side action
- **Backend vs Adapter Error Split** — HTTP 5xx (`BACKEND_ERROR`) → ticket; HTTP 4xx (`ADAPTER_CONFIG_ERROR`) → auto-fix; HTTP 429 → retry
- **HANA Vector Store Integration** — Cosine similarity search over `SAP_HELP_DOCS` (20,000+ scraped SAP notes, 3072-dim embeddings)
- **Internal Escalation Tickets** — Low-confidence incidents escalated as tickets in HANA
- **Fix Pattern Learning** — Successful fixes stored and reused via error signature matching (scoped to iflow + error type + normalised error fragment)
- **Live Fix Progress** — In-memory step tracker provides granular pipeline progress without HANA polling
- **Locked Artifact Handling** — Automatic unlock via DELETE /checkout, POST /CancelCheckout, MCP unlock tools
- **Recurring Incident Correlation** — Deduplicates by signature and resumes fix flow for recurring failures
- **iFlow Rollback** — Restore iFlow to pre-fix snapshot via `update-iflow` + `deploy-iflow`
- **Dashboard + Observability** — React frontend with Dashboard, Observability, Pipeline, Orchestrator, Agent Cards, Test Suite, Migration Wizard, and PiPo List tabs

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      FastAPI Application ($PORT)                         │
│                                                                           │
│  ┌─────────────┐   ┌──────────────────────┐   ┌────────────────────┐   │
│  │ Chatbot API │   │ Smart Monitoring API │   │  Dashboard API     │   │
│  │ /query /fix │   │ /smart-monitoring/   │   │  /dashboard/       │   │
│  └──────┬──────┘   └──────────┬───────────┘   └─────────┬──────────┘   │
│         └─────────────────────┴──────────────────────────┘              │
│                               │                                           │
│   ┌───────────────────────────▼──────────────────────────────────────┐  │
│   │                      OrchestratorAgent                            │  │
│   │   Routes AEM messages → Classifier → RCA → Fix → Verifier        │  │
│   │   Manages autonomous loop, dedup, escalation, fix progress        │  │
│   └──────┬──────────┬──────────┬──────────┬──────────────────────────┘  │
│          │          │          │          │                               │
│   ┌──────▼──┐ ┌─────▼───┐ ┌───▼────┐ ┌──▼───────┐                     │
│   │Observer │ │Classif- │ │  RCA   │ │  Fix     │ ┌──────────┐        │
│   │Agent    │ │ier Agent│ │ Agent  │ │  Agent   │ │ Verifier │        │
│   │+OData   │ │rule-based│ │LLM +  │ │get/update│ │  Agent   │        │
│   │fetcher  │ │keywords  │ │vector │ │/deploy   │ │test+retry│        │
│   └─────────┘ └─────────┘ └───────┘ └──────────┘ └──────────┘        │
│                               │                                           │
│   ┌───────────────────────────▼──────────────────────────────────────┐  │
│   │                       MultiMCP Manager                            │  │
│   │   Transport layer — connects, discovers, executes MCP tools       │  │
│   └──────┬───────────────────┬────────────────────┬───────────────────┘  │
└──────────┼───────────────────┼────────────────────┼─────────────────────┘
           │                   │                    │
 ┌─────────▼────────┐  ┌───────▼──────┐  ┌─────────▼────────┐
 │ Integration Suite│  │  iFlow Test  │  │  Documentation   │
 │      MCP         │  │     MCP      │  │      MCP         │
 │ get/update/deploy│  │ test/validate│  │  spec/templates  │
 └──────────────────┘  └──────────────┘  └──────────────────┘

Inbound:
┌────────────────────────────────────────────────┐
│  Solace PubSub+ AEM Queue (wss://)             │
│  FailedLogscapturing_Schedule → multimap XML   │
│  Background receiver thread → asyncio.Queue   │
│  → OrchestratorAgent._route_stage()           │
└────────────────────────────────────────────────┘

External Services:
┌──────────────────────┐  ┌───────────────────┐  ┌──────────────────────┐
│    SAP HANA Cloud    │  │   SAP AI Core     │  │       AWS S3         │
│  Incidents · History │  │  LLM deployments  │  │  File uploads        │
│  Fix patterns        │  │  text-embedding   │  │  iFlow examples      │
│  SAP_HELP_DOCS       │  │  -3-large (3072d) │  │  (structural ref)    │
│  Escalation tickets  │  │                   │  │                      │
└──────────────────────┘  └───────────────────┘  └──────────────────────┘
```

### Agent Responsibilities

| Agent | Role | Tool Set |
|---|---|---|
| `OrchestratorAgent` | Coordinates the full pipeline; drains AEM queue; manages dedup, escalation, circuit breaker | All specialist agents as LangChain `@tool` wrappers |
| `ObserverAgent` | Holds `SAPErrorFetcher` for OData metadata calls; manages approval timeouts | CPI OData API via `error_fetcher` |
| `ClassifierAgent` | Rule-based keyword classifier (priority-ordered) | No LLM — pure Python |
| `RCAAgent` | Root cause analysis | `get-iflow`, `get_message_logs` (read-only MCP tools) + vector store |
| `FixAgent` | iFlow fix + deploy pipeline | `get-iflow`, `update-iflow`, `deploy-iflow` |
| `VerifierAgent` | Post-fix test + message replay | `mcp_testing` tools |

### MCP Servers

| Server | Responsibility |
|---|---|
| `integration_suite` | iFlow get / update / deploy / unlock, message logs, iFlow examples |
| `mcp_testing` | Test execution, iFlow validation, test reports |
| `documentation_mcp` | SAP standard docs, spec generation, templates |

---

## Prerequisites

- Python 3.12+
- SAP BTP account with Integration Suite access
- SAP AI Core deployment (LLM + `text-embedding-3-large` embedding model)
- SAP HANA Cloud database instance
- AWS S3 bucket (for file uploads and iFlow example storage)
- The three MCP servers deployed and reachable
- **Solace PubSub+ broker** (AEM) — required if `AEM_ENABLED=true`
- Playwright Chromium browser (for `scrape_sap_docs.py` utility only — not needed for the running app)

---

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd auto-remediation
```

### 2. Install dependencies

Using uv (recommended):
```bash
uv sync
```

Or pip:
```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the project root.

Legend: `[REQUIRED]` must be set or the app will not start / feature will not work. `[OPTIONAL]` has a safe default or enables a non-critical feature.

```env
# ── SAP AI Core ── [REQUIRED] ─────────────────────────────────────────
AICORE_CLIENT_ID=your_client_id
AICORE_CLIENT_SECRET=your_client_secret
AICORE_AUTH_URL=https://your-tenant.authentication.region.hana.ondemand.com
AICORE_BASE_URL=https://api.ai.prod.region.aws.ml.hana.ondemand.com/v2
AICORE_RESOURCE_GROUP=default
LLM_DEPLOYMENT_ID=your_llm_deployment_id
LLM_DEPLOYMENT_ID_RCA=your_rca_deployment_id     # optional — falls back to LLM_DEPLOYMENT_ID
LLM_DEPLOYMENT_ID_FIX=your_fix_deployment_id     # optional — falls back to LLM_DEPLOYMENT_ID

# ── LangSmith Tracing ── [OPTIONAL] ──────────────────────────────────
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=your_langsmith_key
LANGSMITH_PROJECT=your_project_name

# ── SAP Integration Suite — Runtime API ── [REQUIRED] ────────────────
API_BASE_URL=https://your-tenant.it-cpi019.cfapps.region.hana.ondemand.com/api/v1
API_OAUTH_CLIENT_ID=your_client_id
API_OAUTH_CLIENT_SECRET=your_client_secret
API_OAUTH_TOKEN_URL=https://your-tenant.authentication.region.hana.ondemand.com/oauth/token

# ── SAP Integration Suite — Monitor / CPI Runtime ── [REQUIRED] ──────
CPI_BASE_URL=https://your-tenant.it-cpi019-rt.cfapps.region.hana.ondemand.com
CPI_OAUTH_CLIENT_ID=your_client_id
CPI_OAUTH_CLIENT_SECRET=your_client_secret
CPI_OAUTH_TOKEN_URL=https://your-tenant.authentication.region.hana.ondemand.com/oauth/token

# ── SAP Hub — Autonomous Error Polling & OData ── [REQUIRED] ─────────
SAP_HUB_TENANT_URL=https://your-tenant.it-cpi019.cfapps.region.hana.ondemand.com
SAP_HUB_TOKEN_URL=https://your-tenant.authentication.region.hana.ondemand.com/oauth/token
SAP_HUB_CLIENT_ID=your_client_id
SAP_HUB_CLIENT_SECRET=your_client_secret

# ── SAP HANA Cloud ── [REQUIRED] ──────────────────────────────────────
HANA_HOST=your-guid.hna0.prod-region.hanacloud.ondemand.com
HANA_PORT=443
HANA_USER=your_hdi_rt_user
HANA_PASSWORD=your_password
HANA_SCHEMA=your_schema
HANA_TABLE_VECTOR=SAP_HELP_DOCS
HANA_TABLE_SAP_DOCS=SAP_HELP_DOCS

# ── AEM / Solace PubSub+ ── [REQUIRED if AEM_ENABLED=true] ───────────
AEM_ENABLED=true                                 # false = use local in-process queue
AEM_HOST=wss://your-broker.messaging.solace.cloud:443
AEM_VPN=your-message-vpn
AEM_USERNAME=your-solace-user
AEM_PASSWORD=your-solace-password
AEM_OBSERVER_QUEUE=sap.cpi.autofix.observer.out  # queue to consume from
AEM_OBSERVER_TOPIC=sap/cpi/autofix/observer/out  # topic to publish stage events to

# ── AWS S3 Object Store ── [REQUIRED for file uploads & iFlow examples]
BUCKET_NAME=your_bucket_name
REGION=us-east-1
OBJECT_STORE_ENDPOINT=https://s3.amazonaws.com/
OBJECT_STORE_ACCESS_KEY=your_access_key
OBJECT_STORE_SECRET_KEY=your_secret_key
WRITE_ACCESS_KEY_ID=your_write_key_id
WRITE_SECRET_ACCESS_KEY=your_write_secret
READ_ACCESS_KEY_ID=your_read_key_id
READ_SECRET_ACCESS_KEY=your_read_secret

# ── Autonomous Operations ── [OPTIONAL — defaults shown] ─────────────
AUTONOMOUS_ENABLED=true
POLL_INTERVAL_SECONDS=60
AUTO_FIX_CONFIDENCE=0.90
SUGGEST_FIX_CONFIDENCE=0.70
MAX_CONSECUTIVE_FAILURES=5
PENDING_APPROVAL_TIMEOUT_HRS=24
PATTERN_MIN_SUCCESS_COUNT=2
BURST_DEDUP_WINDOW_SECONDS=60

# ── Escalation Tickets ── [OPTIONAL] ─────────────────────────────────
TICKET_DEFAULT_ASSIGNEE=team@example.com

# ── Server & Logging ── [OPTIONAL — defaults shown] ──────────────────
APP_ENV=production          # set to "development" to enable --reload
ENABLE_CONSOLE_LOGS=true

# ── SAP Notes Scraper ── [REQUIRED only for scrape_sap_docs.py] ──────
SAP_USERNAME=your_sap_user@example.com
SAP_PASSWORD=your_sap_password
SAP_NOTE_CONCURRENCY=10
SAP_NOTE_DELAY=0.3

# ── Embeddings ── [REQUIRED only for vectorize_docs.py] ──────────────
EMBEDDING_DEPLOYMENT_ID=your_embedding_deployment_id
EMBEDDING_MODEL_NAME=text-embedding-3-large
VECTOR_DIMENSION=3072
```

### 4. Build the SAP Notes knowledge base (one-time setup)

Install Playwright browsers (only needed for this utility script):
```bash
uv run playwright install chromium
```

Scrape SAP Notes from me.sap.com. Each notes file is a plain text file with one SAP Note URL per line. Split your full list into files of ~500 then run each:
```bash
uv run python scrape_sap_docs.py --notes-only --notes-file sap_notes_1.txt
uv run python scrape_sap_docs.py --notes-only --notes-file sap_notes_2.txt
```

Vectorize all scraped notes (run once after scraping):
```bash
uv run python vectorize_docs.py
```

Both scripts are crash-safe — re-run at any time to resume from where they left off.

### 5. Run the server

Development (hot reload):
```bash
APP_ENV=development uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

Production:
```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

API docs available at:
- Swagger UI: `http://localhost:8080/docs`
- ReDoc: `http://localhost:8080/redoc`

---

## BTP Cloud Foundry Deployment

The project ships with all required CF deployment files.

### Files

| File | Purpose |
|---|---|
| `manifest.yml` | `cf push` configuration — 2 GB memory, 1 instance, `uvicorn main:app --port $PORT` |
| `Procfile` | CF startup command |
| `runtime.txt` | Python 3.12 buildpack selection |
| `.cfignore` | Excludes `.env`, `.venv/`, logs, stale files from push |
| `requirements.txt` | Complete pip dependency list for CF Python buildpack |

### Deploy

```bash
# Log in and target your BTP space
cf login -a https://api.cf.us10-001.hana.ondemand.com
cf target -o <your-org> -s <your-space>

# Set all required environment variables (or use BTP Cockpit → Environment Variables)
cf set-env sap-cpi-self-healing-agent HANA_HOST <value>
cf set-env sap-cpi-self-healing-agent LLM_DEPLOYMENT_ID <value>
# ... (all other vars from the manifest.yml env section)

# Deploy
cf push
```

### Notes

- All secrets must be set as CF environment variables — **never commit `.env`**
- The app listens on `$PORT` which CF injects automatically
- Logs go to stdout and are captured by CF Loggregator (`cf logs sap-cpi-self-healing-agent --recent`)
- Memory: 2 GB minimum — the multi-agent LangChain stack is memory-heavy

---

## Project Structure

```
auto-remediation/
├── main.py                         # FastAPI app, lifespan, all HTTP endpoints
├── smart_monitoring.py             # /smart-monitoring/* router
├── smart_monitoring_dashboard.py   # /dashboard/* router
├── generate_dashboard_pdf.py       # PDF report export
│
├── agents/
│   ├── base.py                     # Shared base classes (StepLogger, TestExecutionTracker)
│   ├── classifier_agent.py         # Rule-based error classifier (no LLM)
│   ├── observer_agent.py           # ObserverAgent + SAPErrorFetcher (OData calls)
│   ├── orchestrator_agent.py       # Top-level coordinator — routes AEM messages through pipeline
│   ├── rca_agent.py                # Root Cause Analysis (LLM + vector store)
│   ├── fix_agent.py                # iFlow fix + deploy pipeline
│   └── verifier_agent.py          # Post-fix testing + message replay
│
├── aem/
│   ├── solace_client.py            # Solace PubSub+ wss:// client (async wrapper + receiver thread)
│   └── event_bus.py                # In-process AEM event bus for webhook path
│
├── core/
│   ├── mcp_manager.py              # MultiMCP — connect, discover, execute, build_agent
│   ├── constants.py                # All config constants, prompts, remediation policies
│   ├── state.py                    # FIX_PROGRESS in-memory store
│   └── validators.py               # iFlow XML validator (pre-update-iflow gate)
│
├── db/
│   └── database.py                 # SAP HANA Cloud abstraction (all table CRUD)
│
├── storage/
│   ├── storage.py                  # File uploads + XSD detection
│   └── object_store.py             # AWS S3 operations
│
├── utils/
│   ├── utils.py                    # HANA timestamp helpers
│   ├── logger_config.py            # Rotating file logger + stdout handler
│   ├── vector_store.py             # HANA SAP_HELP_DOCS cosine similarity search
│   └── xsd_handler.py             # XSD parsing and validation
│
├── frontend/                       # React + Vite frontend app
│   └── src/pages/
│       ├── dashboard/              # KPI cards, charts, recent failures
│       ├── observability/          # Incident list + drill-down
│       ├── pipeline/               # Pipeline status view
│       ├── orchestrator/           # Orchestrator + fix progress
│       ├── agent-cards/            # Agent status cards
│       ├── test-suite/             # iFlow test execution
│       ├── migration-wizard/       # Migration helper
│       └── pipo-list/              # PiPo interface list
│
├── rules/                          # Coding standards
├── scrape_sap_docs.py              # Playwright-based SAP Notes scraper (utility, not deployed)
├── vectorize_docs.py               # SAP_HELP_DOCS vectorization (utility, not deployed)
├── manifest.yml                    # BTP CF deployment manifest
├── Procfile                        # CF startup command
├── runtime.txt                     # Python 3.12 for CF buildpack
├── .cfignore                       # CF push exclusions
├── requirements.txt                # Production pip dependencies
├── pyproject.toml                  # Full dependency spec
└── CLAUDE.md                       # Claude Code project instructions
```

---

## How the Agent Works

### Startup Sequence

```
FastAPI lifespan starts
  │
  ├─ DB schema migration (ensure_*_schema)
  ├─ All agents created and wired (observer ↔ orchestrator, error_fetcher → fix/verifier)
  ├─ Solace client connected + receiver thread started (if AEM_ENABLED=true)
  ├─ Orchestrator loop started immediately (UI shows "Running" from boot)
  │
  └─ _init_background() [async task]:
       ├─ mcp.connect() → discover_tools() → build_agent()
       ├─ All specialist agents build in parallel
       ├─ orchestrator.build_agent(observer=observer)
       └─ AEM webhook subscription registered
            │
            └─ Agent ready — messages that arrived before this point
               were buffered in solace_client._inbound and are now processed
```

### AEM Message Flow

```
SAP CPI FailedLogscapturing_Schedule iFlow
  │  publishes SAP multimap XML to Solace queue
  ▼
Solace receiver thread (daemon)
  │  polls queue every 1 s
  │  get_payload_as_bytes() fallback for binary payloads
  ▼
asyncio.Queue (solace_client._inbound, max 500)
  ▼
OrchestratorAgent._autonomous_loop()
  │  drains up to 20 messages per tick
  ▼
_route_stage(message)
  │
  ├─ raw_body starts with "<"?  →  SAP multimap XML path
  │    ├─ re.findall(<Error>…</Error>)  — N blocks
  │    ├─ extract MPL ID GUID from each block text
  │    ├─ OData: GET /MessageProcessingLogs('{guid}')
  │    │         resolves IntegrationFlowName, Sender, Receiver
  │    └─ process_detected_error(inc) × N
  │
  └─ JSON message  →  _normalize_aem_message()  →  process_detected_error()
```

### RCA Engine

```
Error detected → process_detected_error()
      │
      ├─── ClassifierAgent (rule-based, no LLM)
      │    • Priority-ordered keyword matching
      │    • Returns error_type + confidence
      │
      ├─── RCAAgent (LangChain)
      │    • get_vector_store_notes — top-5 SAP notes from HANA
      │    • get_cross_iflow_patterns — successful past fixes
      │    • get-iflow — reads actual iFlow configuration
      │    • get_message_logs — MPL log (if GUID available)
      │    • Returns root_cause, proposed_fix, confidence
      │
      └─── confidence = max(LLM_confidence, classifier_confidence)
                │
                If proposed_fix empty → FALLBACK_FIX_BY_ERROR_TYPE
```

### Remediation Gate

| Condition | Action |
|---|---|
| `CONNECTIVITY_ERROR` + confidence ≥ 0.70 | Retry failed message |
| confidence ≥ 0.90 (`AUTO_FIX_CONFIDENCE`) | Auto-fix + deploy |
| confidence ≥ 0.70 (`SUGGEST_FIX_CONFIDENCE`) | `PENDING_APPROVAL` |
| confidence < 0.70 | `TICKET_CREATED` — escalation ticket in HANA |

### Fix Pipeline

```
1. Verify iFlow exists          — pre-flight existence check
2. Pre-flight unlock            — cancel any existing checkout
3. Run RCA (if needed)         — LLM + vector store + classifier
4. Re-verify iFlow exists       — double-check after RCA
5. Download iFlow config        — MCP: get-iflow
6. Apply fix                    — LLM determines precise XML change
7. Pre-update validation        — Python validator:
                                   • filepath matches original
                                   • XML is well-formed
                                   • no ifl:property at collaboration root
                                   • no version attributes changed
8. Upload updated iFlow         — MCP: update-iflow (only if validation passes)
9. Deploy iFlow                 — MCP: deploy-iflow (mandatory after update)
10. Fetch deploy errors          — detailed error if deploy fails
11. Replay message               — retry original failed message
12. Persist result               — AUTONOMOUS_INCIDENTS + FIX_PATTERNS
```

### Error Classification

| Priority | Error Type | Default Action | Keywords |
|---|---|---|---|
| 1 | `SFTP_ERROR` | TICKET_CREATED | `sftp`, `jsch`, `no such file`, `permission denied`, `auth fail`, `hostkey`, `quota exceeded` |
| 2 | `AUTH_ERROR` | AUTO_FIX | `unauthorized`, `expired`, `certificate`, `ssl handshake`, `tls`, `saml`, `oauth` |
| 3 | `MAPPING_ERROR` | AUTO_FIX | `mappingexception`, `does not exist in target`, `xslt`, `groovy`, `script` |
| 4 | `DATA_VALIDATION` | AUTO_FIX | `mandatory`, `required field`, `null value`, `schema validation` |
| 5 | `CONNECTIVITY_ERROR` | RETRY | `connection refused`, `connect timed out`, `unreachable`, `socketexception` |
| 6 | `CONNECTIVITY_ERROR` (rate-limit) | RETRY | `429`, `too many requests`, `rate limit` |
| 7 | `BACKEND_ERROR` | TICKET_CREATED | `500`, `502`, `503`, `internal server error`, `bad gateway` |
| 8 | `ADAPTER_CONFIG_ERROR` | AUTO_FIX | `400`, `401`, `403`, `404`, `422`, `bad request`, `not found` |
| 9 | `UNKNOWN_ERROR` | PENDING_APPROVAL | _(fallback)_ |

---

## API Reference

### Chatbot

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `POST` | `/query` | Chat with fix-intent detection (supports file uploads) |
| `POST` | `/fix` | Direct iFlow fix by iflow_id + error message |
| `GET` | `/get_all_history` | Query history for a user |

### Smart Monitoring

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/smart-monitoring/messages` | List failed CPI messages |
| `GET` | `/smart-monitoring/messages/{guid}` | Full detail (6 tabs) for one message |
| `POST` | `/smart-monitoring/messages/{guid}/analyze` | Trigger AI RCA (returns 503 if agents still init) |
| `POST` | `/smart-monitoring/messages/{guid}/apply_fix` | Apply and deploy fix |
| `POST` | `/smart-monitoring/incidents/{id}/retry_fix` | Retry failed fix (up to 3 attempts) |
| `POST` | `/smart-monitoring/incidents/{id}/rollback` | Rollback iFlow to pre-fix snapshot |
| `GET` | `/smart-monitoring/incidents/{id}/fix_status` | Live fix progress (in-memory, no HANA poll) |
| `GET` | `/smart-monitoring/escalations` | List escalation tickets |
| `PATCH` | `/smart-monitoring/escalations/{ticket_id}` | Update ticket status / assignee |

### Dashboard

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/dashboard/kpi-cards` | Top-level KPI metrics |
| `GET` | `/dashboard/error-distribution` | Error type breakdown |
| `GET` | `/dashboard/failures-over-time` | Time-series failure data |
| `GET` | `/dashboard/top-failing-iflows` | Most failing iFlows |
| `GET` | `/dashboard/recent-failures-table` | Recent failures (reads HANA directly — always works at boot) |
| `GET` | `/dashboard/active-incidents-table` | Real-time active incidents |
| `GET` | `/dashboard/health-metrics` | System health indicators |
| `GET` | `/dashboard/sla-metrics` | SLA compliance metrics |

### Autonomous Operations

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/autonomous/start` | Start autonomous monitoring loop |
| `POST` | `/autonomous/stop` | Stop loop |
| `GET` | `/autonomous/status` | Loop status and config |
| `GET` | `/autonomous/incidents` | All incidents (optional status filter) |
| `GET` | `/autonomous/incidents/{id}` | Incident detail by ID or message GUID |
| `GET` | `/autonomous/incidents/{id}/view_model` | Enriched incident view with OData metadata |
| `POST` | `/autonomous/incidents/{id}/approve` | Approve or reject a pending fix |
| `POST` | `/autonomous/incidents/{id}/generate_fix` | Generate and apply fix |
| `POST` | `/autonomous/manual_trigger` | Manual one-shot error polling |
| `POST` | `/autonomous/test_incident` | Inject synthetic test incident |
| `GET` | `/autonomous/tools` | List all loaded MCP tools |
| `GET` | `/autonomous/db_test` | Test HANA connectivity |

### Auto-Fix Configuration

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/config/auto-fix` | Current auto-fix config |
| `POST` | `/api/config/auto-fix` | Enable/disable at runtime |
| `POST` | `/api/config/auto-fix/reset` | Reset to `.env` default |

---

## iFlow XML Validator

Runs as a Python gate inside `MultiMCP.execute()` — every `update-iflow` call passes through before any HTTP request is made.

| Check | What it catches |
|---|---|
| **Filepath match** | LLM submitted a different `.iflw` filename than returned by `get-iflow` |
| **XML well-formedness** | Malformed XML that SAP CPI would reject on upload |
| **Property placement** | `ifl:property` inside `<bpmn2:collaboration>` instead of inside the specific step |
| **Version attributes** | Any `version="…"` attribute changed from the original value |

Validation failures are returned to the LLM as specific actionable messages so it can self-correct in the same run.

---

## Database Schema

All tables are in SAP HANA Cloud. Auto-created/migrated on startup via `ensure_*_schema()`.

### `AUTONOMOUS_INCIDENTS`

One row per detected error. Key columns:

| Column | Description |
|---|---|
| `incident_id` | UUID primary key |
| `message_guid` | SAP CPI MPL ID (GUID) |
| `iflow_id` | Integration flow name (resolved via OData) |
| `status` | Lifecycle status |
| `error_type` | Classified error type |
| `root_cause` | AI-generated root cause |
| `proposed_fix` | AI-generated fix description |
| `rca_confidence` | Confidence score 0.0–1.0 |
| `occurrence_count` | Recurring error counter |
| `incident_group_key` | Deduplication hash |
| `consecutive_failures` | Circuit breaker counter |
| `auto_escalated` | 1 if circuit breaker triggered escalation |

**Incident lifecycle:**

```
DETECTED → RCA_IN_PROGRESS → RCA_COMPLETE → PENDING_APPROVAL → FIX_IN_PROGRESS → AUTO_FIXED
                                                                             → HUMAN_INITIATED_FIX
                                                                             → FIX_FAILED ──────────┐
                                                                             → FIX_FAILED_UPDATE ───┤ retry_fix
                                                                             → FIX_FAILED_DEPLOY ───┤ (up to 3×)
                                                                             → FIX_FAILED_RUNTIME ──┘
                                                                             → ROLLED_BACK
                                                                             → ARTIFACT_MISSING
                                        → TICKET_CREATED
                        → RETRIED
```

### `FIX_PATTERNS`

Stores outcomes of applied fixes. Matched by `error_signature` (`iflow_id + error_type + normalised_error_fragment`) on recurring incidents. Includes `success_rate` and `key_steps` injected into RCA prompts.

### `ESCALATION_TICKETS`

Internal escalation system. Auto-created for low-confidence incidents. Priority derived from `occurrence_count` and `rca_confidence`.

### `SAP_HELP_DOCS`

```sql
CREATE TABLE sap_help_docs (
    ID         INTEGER          PRIMARY KEY,
    VEC_TEXT   NCLOB,           -- chunked SAP Note text
    VEC_META   NCLOB,           -- JSON: title, url, source, note_id
    VEC_VECTOR REAL_VECTOR(3072) -- text-embedding-3-large embedding
);
```

Populated by `scrape_sap_docs.py`, vectorized by `vectorize_docs.py`. Used for semantic RCA context retrieval via cosine similarity.

---

## Security

- All SAP API calls use OAuth 2.0 client credentials — tokens cached until expiry
- Secrets stored in `.env` locally or CF environment variables in production — never committed
- HANA connections use SSL/TLS (`encrypt=True`, `sslValidateCertificate=False`)
- SQL queries use parameterised statements — no string interpolation
- API responses never expose stack traces or internal error details
- `AUTO_FIX_CONFIDENCE=0.90` gates autonomous deployments
- Auto-fix toggled at runtime via API — no service restart required
- S3 uses separate read / write credentials
- Locked artifact detection prevents concurrent edit conflicts

---

## Debugging

```bash
# View live logs (local)
tail -f logs/mcp.log

# View logs on BTP CF
cf logs sap-cpi-self-healing-agent --recent

# Health check
curl http://localhost:8080/

# Autonomous loop status
curl http://localhost:8080/autonomous/status

# List all loaded MCP tools
curl http://localhost:8080/autonomous/tools

# Test HANA connectivity
curl http://localhost:8080/autonomous/db_test

# Fetch current SAP CPI errors (without starting loop)
curl http://localhost:8080/autonomous/cpi/errors

# Inject synthetic test incident
curl -X POST http://localhost:8080/autonomous/test_incident

# Debug autonomous config
curl http://localhost:8080/autonomous/debug

# Enable auto-fix at runtime
curl -X POST http://localhost:8080/api/config/auto-fix \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `iFlow name shows as blank in Observability` | OData metadata call failed — check `SAP_HUB_*` env vars | Verify credentials and tenant URL; check `logs/mcp.log` for OData errors |
| `Solace receiver not connecting` | Wrong `AEM_HOST`, `AEM_VPN`, or credentials | Check Solace broker URL format (`wss://host:443`); verify `AEM_USERNAME`/`AEM_PASSWORD` |
| `recent-failures-table returns empty` | HANA not reachable | Check `HANA_HOST`/`HANA_PASSWORD`; table is read directly — no MCP dependency |
| `503 on /analyze right after boot` | MCP agents still initialising (takes 30–60 s) | Wait for `[Startup] All agents ready` in logs, then retry |
| `iFlow fix applied but deploy fails locked` | iFlow checked out in browser | Use `POST /retry_fix` — automatic unlock is attempted |
| `Solace message payload None` | Binary payload from SAP multimap | Already handled — falls back to `get_payload_as_bytes()` with UTF-8 decode |
| `HANA connection refused` | Wrong host/port or IP not whitelisted | Check HANA Cloud instance → Allowed Connections |
| `LLM deployment not found` | Wrong `LLM_DEPLOYMENT_ID` | Verify deployment ID in SAP AI Core Launchpad |
| `cf push fails — memory exceeded` | Not enough memory in BTP quota | Request quota increase or reduce to `1536M` and monitor |
| `VEC_VECTOR IS NULL after vectorize` | Script interrupted mid-run | Re-run `vectorize_docs.py` — it skips already-vectorized rows |

---

## Further Reading

- [CLAUDE.md](CLAUDE.md) — Claude Code project standards and agent instructions
- [rules/coding-style.md](rules/coding-style.md) — naming conventions and architecture rules
- [rules/security.md](rules/security.md) — security standards for SAP BTP
- [manifest.yml](manifest.yml) — BTP CF deployment configuration
