# Orbit — SAP CPI Auto-Remediation Agent

An event-driven, multi-agent system that automatically detects, diagnoses, and fixes
errors in SAP Integration Suite (CPI) iFlows. When a CPI iFlow fails, the system
receives the error event via SAP Event Mesh, runs root cause analysis using an LLM,
applies a fix to the iFlow XML, redeploys it, and verifies the fix — all without
human intervention.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Agent Roles](#3-agent-roles)
4. [SAP Event Mesh Setup](#4-sap-event-mesh-setup)
5. [Environment Variables](#5-environment-variables)
6. [API Endpoints](#6-api-endpoints)
7. [Local Development Setup](#7-local-development-setup)
8. [Deployment (Cloud Foundry)](#8-deployment-cloud-foundry)
9. [Database](#9-database)
10. [Frontend](#10-frontend)

---

## 1. Project Overview

**What it does:**

SAP CPI iFlows occasionally fail due to mapping errors, missing fields, endpoint
timeouts, or schema mismatches. Orbit monitors these failures in real time, determines
the root cause using an LLM, edits the iFlow XML to apply the fix, deploys the updated
iFlow back to SAP Integration Suite, and verifies it works — writing the final outcome
to the database.

**Tech stack:**

| Layer | Technology |
|---|---|
| API framework | FastAPI + Uvicorn |
| Agent orchestration | LangChain (tool-calling agents) |
| LLM | SAP AI Core — GPT-5 / Claude Sonnet via OpenAI-compatible API |
| MCP tool protocol | fastmcp `>=2.14.5`, langchain-mcp-adapters |
| Event bus | SAP Event Mesh (REST publishing, OAuth2, x-qos 0/1) |
| SAP CPI integration | OData API (OAuth2), Design-time / Runtime REST APIs |
| Database | SAP HANA Cloud via hdbcli |
| Object storage | AWS S3 via boto3 |
| Frontend | React + TypeScript (Vite) |
| Python | `>=3.13` |
| Logging | structlog + rotating file handlers |

---

## 2. Architecture

### Event-Driven Pipeline

A CPI iFlow error triggers an event that flows through five agents in strict linear
order. Each agent webhook returns `{"status": "accepted"}` immediately and processes
in a background task. Only on success does it publish to the next agent's topic.

```
SAP CPI iFlow → error occurs
        │
        │  publishes event to topic: cpi/evt/02/autofix/in
        ▼
┌──────────────────────────────────────────────────────────────┐
│  Queue: cpi/evt/02/autofix/orbit/orchestrator                │
│  Subscribes to topic: cpi/evt/02/autofix/in                  │
└──────────────────────┬───────────────────────────────────────┘
                       │ webhook push
                       ▼
             POST /agents/orchestrator
             • Normalize raw AEM message envelope
             • Classify error (rule-based + LLM fallback)
             • Dedup check (signature + burst window)
             • Create incident in DB  →  status: CLASSIFIED
                       │
                       │  publish_to_next → cpi/evt/02/autofix/agent/orbit/observer
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Queue: cpi/evt/02/autofix/orbit/observer                    │
│  Subscribes to topic: cpi/evt/02/autofix/agent/orbit/observer│
└──────────────────────┬───────────────────────────────────────┘
                       │ webhook push
                       ▼
             POST /agents/observer
             • Fetch OData metadata (sender, receiver, log timestamps)
             • Enrich incident in DB  →  status: OBSERVED
                       │
                       │  publish_to_next → cpi/evt/02/autofix/agent/orbit/rca
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Queue: cpi/evt/02/autofix/orbit/rca                         │
│  Subscribes to topic: cpi/evt/02/autofix/agent/orbit/rca     │
└──────────────────────┬───────────────────────────────────────┘
                       │ webhook push
                       ▼
             POST /agents/rca
             • Run LLM root cause analysis (reads iFlow XML via MCP)
             • Update DB: root_cause, proposed_fix, rca_confidence
             • status: RCA_IN_PROGRESS → RCA_COMPLETE
                       │
                       │  publish_to_next → cpi/evt/02/autofix/agent/orbit/fixer
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Queue: cpi/evt/02/autofix/orbit/fixer                       │
│  Subscribes to topic: cpi/evt/02/autofix/agent/orbit/fixer   │
└──────────────────────┬───────────────────────────────────────┘
                       │ webhook push
                       ▼
             POST /agents/fixer
             • Apply fix: get-iflow → update-iflow → deploy-iflow (via MCP)
             • Update DB: fix_summary, fix_applied
             • status: FIX_IN_PROGRESS → FIX_DEPLOYED
             • Halt pipeline on failure — does NOT publish to verifier
                       │
                       │  publish_to_next → cpi/evt/02/autofix/agent/orbit/verifier
                       ▼
┌──────────────────────────────────────────────────────────────┐
│  Queue: cpi/evt/02/autofix/orbit/verifier                    │
│  Subscribes to topic: cpi/evt/02/autofix/agent/orbit/verifier│
└──────────────────────┬───────────────────────────────────────┘
                       │ webhook push
                       ▼
             POST /agents/verifier
             • Check iFlow runtime status
             • Run test payload (HTTP-triggered iFlows only)
             • Write final status to DB:
               ✓  FIX_VERIFIED        — fix confirmed working
               ✗  FIX_FAILED_RUNTIME  — iFlow still failing after deploy
             ✓  Terminal — no further publish
```

### Failure Handling

Each background task wraps the agent call in `try/except`. On any exception:
- The incident DB status is set to the appropriate `*_FAILED` variant
- `publish_to_next()` is **not** called — the pipeline halts cleanly at that stage
- The failure is logged as `[Agents/<name>] Task failed incident=<id>: <exc>`

### In-Process Fallback (`AEM_ENABLED=false`)

When `AEM_ENABLED=false` (local dev), `publish_to_next()` dispatches to in-process
handlers registered via `event_bus.subscribe()` instead of making HTTP calls to SAP
Event Mesh. The same five-stage pipeline runs end-to-end with zero external dependencies.

### Backward-Compatible Entry Points

The original `/aem/events` and `/event-mesh/events` webhooks are preserved unchanged.
They call `orchestrator._route_stage()` which runs the full pipeline inline — useful
as a fallback or for testing without the full queue topology.

---

## 3. Agent Roles

### Stage 1 — Orchestrator (`/agents/orchestrator`)

- **Input:** Raw CPI error event JSON (SAP Event Mesh push from iFlow)
- **Responsibilities:**
  - Normalize the AEM multimap envelope into a flat incident dict
  - Classify the error type using rule-based patterns (zero latency)
  - Fall back to LLM classification when rule confidence < 70%
  - Apply signature dedup — if same iFlow + error type is already open, increment occurrence count and stop
  - Apply burst dedup — absorb rapid repeat events within `BURST_DEDUP_WINDOW_SECONDS`
  - Create a new incident record in HANA with status `CLASSIFIED`
- **Publishes to:** `cpi/evt/02/autofix/agent/orbit/observer`
- **On failure:** Logs error; no incident is written to DB

### Stage 2 — Observer (`/agents/observer`)

- **Input:** `{"stage": "observer", "incident_id": "<uuid>"}`
- **Responsibilities:**
  - Read incident from DB
  - Call SAP CPI OData `MessageProcessingLogs('<guid>')` API
  - Enrich incident with: `iflow_id`, `sender`, `receiver`, `log_start`, `log_end`
  - Update DB status to `OBSERVED`
- **Publishes to:** `cpi/evt/02/autofix/agent/orbit/rca`
- **On failure:** Sets status `OBS_FAILED`

### Stage 3 — RCA Agent (`/agents/rca`)

- **Input:** `{"stage": "rca", "incident_id": "<uuid>"}`
- **Responsibilities:**
  - Read enriched incident from DB
  - Run an LLM agent with a restricted tool set (`get-iflow`, `get_message_logs`)
  - Read the iFlow XML and CPI message logs to determine root cause
  - Update DB: `root_cause`, `proposed_fix`, `rca_confidence`, `affected_component`
  - Status progression: `RCA_IN_PROGRESS` → `RCA_COMPLETE`
- **Publishes to:** `cpi/evt/02/autofix/agent/orbit/fixer`
- **On failure:** Sets status `RCA_FAILED`

### Stage 4 — Fixer Agent (`/agents/fixer`)

- **Input:** `{"stage": "fixer", "incident_id": "<uuid>"}`
- **Responsibilities:**
  - Read incident + RCA results from DB
  - Execute the fix pipeline via MCP: `get-iflow` → `update-iflow` → `deploy-iflow`
  - Evaluate outcome: `fix_applied AND deploy_success`
  - Update DB: `fix_summary`, `fix_applied`, `status`
  - Only publish to verifier if fix was successfully applied and deployed; otherwise halt
- **Publishes to:** `cpi/evt/02/autofix/agent/orbit/verifier` (success only)
- **On failure:** Sets status `FIX_FAILED`

### Stage 5 — Verifier Agent (`/agents/verifier`) — Terminal

- **Input:** `{"stage": "verifier", "incident_id": "<uuid>"}`
- **Responsibilities:**
  - Read incident from DB
  - Check iFlow runtime status via `check_iflow_runtime_status` tool
  - For HTTP-triggered iFlows: execute a test payload via `test_iflow_with_payload`
  - For non-HTTP iFlows: runtime `Started` state is sufficient confirmation
  - Write final DB status: `FIX_VERIFIED` or `FIX_FAILED_RUNTIME`
- **Publishes to:** Nothing — this is the terminal stage
- **On failure:** Sets status `FIX_FAILED_RUNTIME`

---

## 4. SAP Event Mesh Setup

### Queues

Create the following 5 queues in your SAP Event Mesh service instance:

| Queue Name | Topic Subscription | Purpose |
|---|---|---|
| `cpi/evt/02/autofix/orbit/orchestrator` | `cpi/evt/02/autofix/in` | Receives raw iFlow error events |
| `cpi/evt/02/autofix/orbit/observer` | `cpi/evt/02/autofix/agent/orbit/observer` | Receives classified incidents for enrichment |
| `cpi/evt/02/autofix/orbit/rca` | `cpi/evt/02/autofix/agent/orbit/rca` | Receives enriched incidents for RCA |
| `cpi/evt/02/autofix/orbit/fixer` | `cpi/evt/02/autofix/agent/orbit/fixer` | Receives RCA-complete incidents for fix |
| `cpi/evt/02/autofix/orbit/verifier` | `cpi/evt/02/autofix/agent/orbit/verifier` | Receives deployed fixes for verification |

Recommended queue settings: **Access type: Exclusive**, **Message retention: 7 days**

### Webhook Subscriptions

Create one REST delivery webhook per queue:

| Subscription Name | Source Queue | Webhook URL |
|---|---|---|
| `orbit-orchestrator` | `cpi/evt/02/autofix/orbit/orchestrator` | `https://<backend-url>/agents/orchestrator` |
| `orbit-observer` | `cpi/evt/02/autofix/orbit/observer` | `https://<backend-url>/agents/observer` |
| `orbit-rca` | `cpi/evt/02/autofix/orbit/rca` | `https://<backend-url>/agents/rca` |
| `orbit-fixer` | `cpi/evt/02/autofix/orbit/fixer` | `https://<backend-url>/agents/fixer` |
| `orbit-verifier` | `cpi/evt/02/autofix/orbit/verifier` | `https://<backend-url>/agents/verifier` |

Webhook settings: **Content-Type: application/json**, **Method: POST**

### SAP CPI iFlow Configuration

Configure the CPI iFlow's outbound adapter to publish error events to the
Event Mesh topic `cpi/evt/02/autofix/in` using the AMQP 1.0 adapter.
Store the Event Mesh AMQP credentials in the iFlow's credential store entry
`EventMesh_CPIEVT`.

AMQP connection details (from `.env`):
- Host: `EVENT_MESH_AMQP_HOST`
- Port: `EVENT_MESH_AMQP_PORT` (443)
- Path: `EVENT_MESH_AMQP_PATH` (`/protocols/amqp10ws`)

---

## 5. Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env`.**

### SAP AI Core (LLM)

| Variable | Description |
|---|---|
| `AICORE_CLIENT_ID` | OAuth2 client ID from SAP AI Core service key |
| `AICORE_CLIENT_SECRET` | OAuth2 client secret |
| `AICORE_AUTH_URL` | Token URL: `https://<subdomain>.authentication.<region>.hana.ondemand.com` |
| `AICORE_BASE_URL` | AI Core API base: `https://api.ai.prod.<region>.aws.ml.hana.ondemand.com/v2` |
| `AICORE_RESOURCE_GROUP` | Resource group (default: `default`) |
| `LLM_DEPLOYMENT_ID` | Default LLM deployment ID |
| `LLM_DEPLOYMENT_ID_RCA` | Per-agent override for RCA (optional; falls back to `LLM_DEPLOYMENT_ID`) |
| `LLM_DEPLOYMENT_ID_FIX` | Per-agent override for Fixer (optional) |
| `EMBEDDING_DEPLOYMENT_ID` | Embedding model deployment ID |
| `EMBEDDING_MODEL_NAME` | Embedding model name (e.g. `text-embedding-3-large`) |
| `VECTOR_DIMENSION` | Embedding vector size (e.g. `3072`) |

### SAP Integration Suite — Runtime API

| Variable | Description |
|---|---|
| `API_BASE_URL` | CPI API base: `https://<tenant>.it-cpi<n>.cfapps.<region>.hana.ondemand.com/api/v1` |
| `API_OAUTH_CLIENT_ID` | Client ID for CPI API |
| `API_OAUTH_CLIENT_SECRET` | Client secret |
| `API_OAUTH_TOKEN_URL` | Token endpoint URL |

### SAP Integration Suite — CPI Runtime Monitor

| Variable | Description |
|---|---|
| `CPI_BASE_URL` | CPI runtime base: `https://<tenant>.it-cpi<n>-rt.cfapps.<region>.hana.ondemand.com` |
| `CPI_OAUTH_CLIENT_ID` | Client ID for CPI runtime |
| `CPI_OAUTH_CLIENT_SECRET` | Client secret |
| `CPI_OAUTH_TOKEN_URL` | Token endpoint URL |

### SAP Integration Suite — Design Time

| Variable | Description |
|---|---|
| `SAP_DESIGN_TIME_URL` | Design-time base URL |
| `SAP_DESIGN_TIME_TOKEN_URL` | Token endpoint |
| `SAP_DESIGN_TIME_CLIENT_ID` | Client ID |
| `SAP_DESIGN_TIME_CLIENT_SECRET` | Client secret |

### SAP Hub — Autonomous Error Polling

| Variable | Description |
|---|---|
| `SAP_HUB_TENANT_URL` | SAP CPI tenant URL for OData polling |
| `SAP_HUB_TOKEN_URL` | Token endpoint |
| `SAP_HUB_CLIENT_ID` | Client ID |
| `SAP_HUB_CLIENT_SECRET` | Client secret |

### MCP Servers

| Variable | Description |
|---|---|
| `MCP_INTEGRATION_SUITE_URL` | Integration Suite MCP server URL (iFlow get/update/deploy tools) |
| `MCP_TESTING_URL` | Testing MCP server URL (test execution, validation) |
| `MCP_DOCUMENTATION_URL` | Documentation MCP server URL (SAP docs, templates) |

### SAP HANA Cloud

| Variable | Default | Description |
|---|---|---|
| `HANA_HOST` | — | HANA host: `<uuid>.hna0.prod-<region>.hanacloud.ondemand.com` |
| `HANA_PORT` | `443` | HANA port |
| `HANA_USER` | — | Database user |
| `HANA_PASSWORD` | — | Database password |
| `HANA_SCHEMA` | — | Schema name |
| `HANA_TABLE_EM_INCIDENTS` | `EM_AUTONOMOUS_INCIDENTS` | Incidents table |
| `HANA_TABLE_EM_FIX_PATTERNS` | `EM_FIX_PATTERNS` | Fix patterns table |
| `HANA_TABLE_EM_ESCALATION_TICKETS` | `EM_ESCALATION_TICKETS` | Escalation tickets table |

### SAP Event Mesh

| Variable | Default | Description |
|---|---|---|
| `AEM_ENABLED` | `false` | `true` = publish to SAP Event Mesh; `false` = in-process only |
| `AEM_REST_URL` | — | Event Mesh REST gateway base URL (from service key `httprest` entry) |
| `EVENT_MESH_TOKEN_URL` | — | OAuth2 token endpoint (from service key `uaa` section) |
| `EVENT_MESH_CLIENT_ID` | — | OAuth2 client ID |
| `EVENT_MESH_CLIENT_SECRET` | — | OAuth2 client secret |
| `EVENT_MESH_QUEUE` | `cpi/evt/02/autofix` | Inbound queue name |
| `AEM_OBSERVER_QUEUE` | `cpi/evt/02/autofix` | Observer queue alias |
| `EVENT_MESH_AMQP_HOST` | — | AMQP host (used by CPI iFlow adapter only) |
| `EVENT_MESH_AMQP_PORT` | `443` | AMQP port |
| `EVENT_MESH_AMQP_PATH` | `/protocols/amqp10ws` | AMQP WebSocket path |

### AWS S3 Object Store

| Variable | Description |
|---|---|
| `BUCKET_NAME` | S3 bucket name |
| `REGION` | AWS region (e.g. `us-east-1`) |
| `ENDPOINT_URL` | S3 endpoint |
| `OBJECT_STORE_ACCESS_KEY` | Access key |
| `OBJECT_STORE_SECRET_KEY` | Secret key |
| `WRITE_ACCESS_KEY_ID` | Write-only access key |
| `WRITE_SECRET_ACCESS_KEY` | Write-only secret key |

### Autonomous Operations — Feature Flags

| Variable | Default | Description |
|---|---|---|
| `AUTONOMOUS_ENABLED` | `true` | Enable the orchestrator polling loop at startup |
| `AUTO_FIX_CONFIDENCE` | `0.90` | Min RCA confidence to auto-apply a fix |
| `SUGGEST_FIX_CONFIDENCE` | `0.70` | Min confidence to suggest (not apply) a fix |
| `USE_REAL_FIXES` | `true` | Actually deploy; `false` = dry-run (no iFlow changes) |
| `POLL_INTERVAL_SECONDS` | `60` | Interval between autonomous CPI polling cycles |
| `FAILED_MESSAGES_PAGE_SIZE` | `400` | Page size for CPI failed message fetch |
| `FAILED_MESSAGES_MAX_TOTAL` | `50000` | Max messages to fetch per cycle |
| `MAX_CONSECUTIVE_FAILURES` | `5` | Circuit breaker: escalate after N consecutive failures |
| `PENDING_APPROVAL_TIMEOUT_HRS` | `24` | Hours before pending approval auto-escalates |
| `PATTERN_MIN_SUCCESS_COUNT` | `2` | Min successful fixes before a pattern is trusted |
| `BURST_DEDUP_WINDOW_SECONDS` | `60` | Window for absorbing rapid repeat events |

### Server & Logging

| Variable | Default | Description |
|---|---|---|
| `API_HOST` | `0.0.0.0` | Server bind address |
| `API_PORT` | `8080` | Server port |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ENABLE_CONSOLE_LOGS` | `true` | Mirror structured logs to stdout |
| `WEB_SEARCH_ENABLED` | `false` | Enable DuckDuckGo search tool for agents |
| `UPLOAD_ROOT` | `user` | Root prefix for S3 file uploads |

---

## 6. API Endpoints

### Agent Webhook Endpoints

Event-driven pipeline entry points called by SAP Event Mesh webhooks.
All return `{"status": "accepted"}` immediately; work runs in a background task.

| Method | Path | Description |
|---|---|---|
| `POST` | `/agents/orchestrator` | Classify error, dedup check, create incident → publish to observer |
| `POST` | `/agents/observer` | Enrich with OData metadata → publish to rca |
| `POST` | `/agents/rca` | Run root cause analysis → publish to fixer |
| `POST` | `/agents/fixer` | Apply fix + deploy iFlow → publish to verifier (success only) |
| `POST` | `/agents/verifier` | Verify fix, write final status — terminal stage |

### Legacy Event Mesh Webhooks (backward-compatible)

| Method | Path | Description |
|---|---|---|
| `POST` | `/aem/events` | Full inline pipeline via `_route_stage()` |
| `POST` | `/event-mesh/events` | Alias for `/aem/events` |

### Event Mesh Status

| Method | Path | Description |
|---|---|---|
| `GET` | `/aem/status` | AEM connectivity, queue depth, stage counts, enabled flag |
| `GET` | `/event-mesh/status` | Alias for `/aem/status` |

### Autonomous Pipeline

| Method | Path | Description |
|---|---|---|
| `POST` | `/autonomous/start` | Start the autonomous CPI polling loop |
| `POST` | `/autonomous/stop` | Stop the autonomous polling loop |
| `GET` | `/autonomous/status` | Pipeline running state + agent states |
| `GET` | `/autonomous/tools` | MCP tool list per agent |
| `GET` | `/autonomous/incidents` | List incidents (params: `status`, `limit`) |
| `GET` | `/autonomous/incidents/{id}` | Single incident detail |
| `GET` | `/autonomous/incidents/{id}/view_model` | Rich UI view model |
| `GET` | `/autonomous/incidents/{id}/fix_progress` | Live fix progress (SSE) |
| `POST` | `/autonomous/incidents/{id}/approve` | Approve or reject a pending fix |
| `POST` | `/autonomous/incidents/{id}/generate_fix` | Manually trigger fix generation |
| `POST` | `/autonomous/incidents/{id}/retry_rca` | Re-run RCA on an existing incident |
| `GET` | `/autonomous/incidents/{id}/fix_patterns` | Similar patterns from knowledge base |
| `GET` | `/autonomous/pending_approvals` | Incidents awaiting human approval |
| `GET` | `/autonomous/tickets` | Escalation tickets |
| `POST` | `/autonomous/manual_trigger` | Push a raw error through the pipeline manually |
| `POST` | `/autonomous/test_incident` | Create a synthetic test incident |

### CPI Error Fetch

| Method | Path | Description |
|---|---|---|
| `GET` | `/autonomous/cpi/errors` | All CPI failed messages |
| `GET` | `/autonomous/cpi/messages/errors` | Message processing log errors |
| `GET` | `/autonomous/cpi/runtime_artifacts/errors` | Runtime artifact errors |
| `GET` | `/autonomous/cpi/runtime_artifacts/{id}` | Single runtime artifact detail |

### Configuration

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config/auto-fix` | Get current auto-fix enabled state |
| `POST` | `/api/config/auto-fix` | Enable or disable auto-fix at runtime |
| `POST` | `/api/config/auto-fix/reset` | Reset to the `.env` value |

### Chatbot & General

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Health check |
| `POST` | `/query` | General chatbot query (full MCP agent) |
| `POST` | `/fix` | Direct fix request |
| `GET` | `/get_all_history` | Chatbot query history for a user |
| `GET` | `/get_testsuite_logs` | Test suite run logs |
| `POST` | `/webhook` | Generic inbound webhook |

### Debug

| Method | Path | Description |
|---|---|---|
| `GET` | `/autonomous/db_test` | HANA connectivity + insert/fetch roundtrip |
| `GET` | `/autonomous/debug` | SAP credential env vars + observer health |
| `GET` | `/autonomous/debug2` | Agent state dump |

---

## 7. Local Development Setup

### Prerequisites

- Python `>=3.13`
- Access to SAP HANA Cloud instance
- Node.js `>=18` (frontend only)
- (Optional) Access to SAP AI Core + MCP servers for full agent functionality

### Backend Setup

```bash
# 1. Clone the repository
git clone <repo-url>
cd "auto-remediation - EventMesh"

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — fill in HANA, SAP CPI, and AI Core credentials at minimum
```

### Run in In-Process Mode (`AEM_ENABLED=false`)

Recommended for local development. No SAP Event Mesh connectivity required.
The five agents call each other in-process via the in-memory event bus.

```bash
# Set in .env:
AEM_ENABLED=false
AUTONOMOUS_ENABLED=false   # disable polling unless you want it

uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

Test the pipeline end-to-end with a synthetic incident:

```bash
curl -X POST http://localhost:8080/autonomous/test_incident \
  -H "Content-Type: application/json" \
  -d '{"iflow_id": "MyTestFlow", "error_message": "Mapping failed: field not found"}'
```

Check the resulting incident:

```bash
curl http://localhost:8080/autonomous/incidents?limit=5
```

### Run with SAP Event Mesh (`AEM_ENABLED=true`)

Requires the 5 queues and webhook subscriptions configured (see [Section 4](#4-sap-event-mesh-setup)).
The app must have a public HTTPS URL reachable from SAP Event Mesh — use ngrok for local testing.

```bash
# Set in .env:
AEM_ENABLED=true
AEM_REST_URL=https://<em-service-host>
EVENT_MESH_TOKEN_URL=https://<subdomain>.authentication.<region>.hana.ondemand.com/oauth/token
EVENT_MESH_CLIENT_ID=<client-id>
EVENT_MESH_CLIENT_SECRET=<client-secret>

uvicorn main:app --host 0.0.0.0 --port 8080
```

### Frontend Setup

```bash
cd frontend
npm install
npm run dev        # starts dev server at http://localhost:5173
```

Create `frontend/.env.local`:

```
VITE_API_BASE=http://localhost:8080/api
VITE_API_PRIMARY=http://localhost:8080
```

---

## 8. Deployment (Cloud Foundry)

### Backend

```bash
cf push nd-orbit-eventmesh-be \
  --buildpack python_buildpack \
  --memory 2G \
  --disk 1G \
  --start-command "uvicorn main:app --host 0.0.0.0 --port 8080"
```

Set all environment variables:

```bash
# Event Mesh
cf set-env nd-orbit-eventmesh-be AEM_ENABLED             "true"
cf set-env nd-orbit-eventmesh-be AEM_REST_URL             "https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com"
cf set-env nd-orbit-eventmesh-be EVENT_MESH_TOKEN_URL     "https://<subdomain>.authentication.us10.hana.ondemand.com/oauth/token"
cf set-env nd-orbit-eventmesh-be EVENT_MESH_CLIENT_ID     "<client-id>"
cf set-env nd-orbit-eventmesh-be EVENT_MESH_CLIENT_SECRET "<client-secret>"

# HANA
cf set-env nd-orbit-eventmesh-be HANA_HOST     "<hana-host>"
cf set-env nd-orbit-eventmesh-be HANA_USER     "<user>"
cf set-env nd-orbit-eventmesh-be HANA_PASSWORD "<password>"
cf set-env nd-orbit-eventmesh-be HANA_SCHEMA   "<schema>"

# AI Core
cf set-env nd-orbit-eventmesh-be LLM_DEPLOYMENT_ID   "<deployment-id>"
cf set-env nd-orbit-eventmesh-be AICORE_CLIENT_ID     "<client-id>"
cf set-env nd-orbit-eventmesh-be AICORE_CLIENT_SECRET "<client-secret>"
cf set-env nd-orbit-eventmesh-be AICORE_AUTH_URL      "https://<subdomain>.authentication.us10.hana.ondemand.com"
cf set-env nd-orbit-eventmesh-be AICORE_BASE_URL      "https://api.ai.prod.us-east-1.aws.ml.hana.ondemand.com/v2"

# SAP CPI
cf set-env nd-orbit-eventmesh-be API_BASE_URL            "https://<tenant>.it-cpi<n>.cfapps.<region>.hana.ondemand.com/api/v1"
cf set-env nd-orbit-eventmesh-be API_OAUTH_CLIENT_ID     "<client-id>"
cf set-env nd-orbit-eventmesh-be API_OAUTH_CLIENT_SECRET "<client-secret>"
cf set-env nd-orbit-eventmesh-be API_OAUTH_TOKEN_URL     "https://<tenant>.authentication.<region>.hana.ondemand.com/oauth/token"

cf restage nd-orbit-eventmesh-be
```

**Deployed backend URL:** `https://nd-orbit-eventmesh-be.cfapps.us10-001.hana.ondemand.com`

### Frontend

```bash
cd frontend
npm run build      # outputs production build to dist/

cf push nd-orbit-eventmesh-fe \
  --buildpack staticfile_buildpack \
  --memory 256M \
  --path dist
```

**Deployed frontend URL:** `https://nd-orbit-eventmesh-fe.cfapps.us10-001.hana.ondemand.com`

### After Deployment — Wire Event Mesh Webhooks

Once the backend is live, update all 5 webhook subscriptions in SAP Event Mesh to use
`https://nd-orbit-eventmesh-be.cfapps.us10-001.hana.ondemand.com/agents/*`.

---

## 9. Database

### Technology

SAP HANA Cloud accessed via `hdbcli`. Schema and all tables are auto-created on
startup by `db/database.py → ensure_em_schema()`. No manual migration scripts needed.
Connections use TLS (`encrypt=True`) on port 443.

### Tables

| Table | Env var (default) | Purpose |
|---|---|---|
| `EM_AUTONOMOUS_INCIDENTS` | `HANA_TABLE_EM_INCIDENTS` | One row per incident; tracks status, RCA, fix, verification |
| `EM_FIX_PATTERNS` | `HANA_TABLE_EM_FIX_PATTERNS` | Successful fix signatures for pattern-matching |
| `EM_ESCALATION_TICKETS` | `HANA_TABLE_EM_ESCALATION_TICKETS` | External tickets created on escalation |

### Incident Status Flow

```
CLASSIFIED              orchestrator created the incident
    │
    ▼
OBSERVED                observer enriched it with OData metadata
    │
    ▼
RCA_IN_PROGRESS         RCA agent is running
    │
    ▼
RCA_COMPLETE            root_cause + proposed_fix stored
    │
    ▼
FIX_IN_PROGRESS         fixer is applying the change
    │
    ▼
FIX_DEPLOYED            iFlow redeployed successfully
    │
    ├──► FIX_VERIFIED          verifier confirmed the fix works       ✓
    └──► FIX_FAILED_RUNTIME    verifier found the iFlow still failing ✗

── Failure variants (pipeline halts) ──────────────────────────────
OBS_FAILED              observer threw an exception
RCA_FAILED              RCA agent threw an exception
FIX_FAILED              fixer threw an unhandled exception
FIX_FAILED_UPDATE       SAP CPI rejected the iFlow XML update
FIX_FAILED_DEPLOY       deploy step failed
FIX_FAILED_RUNTIME      post-deploy verification failed

── Special statuses ────────────────────────────────────────────────
ARTIFACT_MISSING           iFlow not found in SAP CPI design time
AWAITING_APPROVAL          human must approve before fix is applied
TICKET_CREATED             escalated; ticket raised in external system
BURST_DEDUPED              absorbed as duplicate within dedup window
CIRCUIT_BREAKER_ESCALATED  too many consecutive failures; auto-escalated
```

### Key Incident Fields

| Field | Type | Description |
|---|---|---|
| `incident_id` | UUID | Primary key |
| `iflow_id` | string | SAP CPI iFlow name |
| `message_guid` | string | CPI `MessageProcessingLog` GUID |
| `error_type` | string | Classified error category |
| `error_message` | text | Raw error text from CPI |
| `root_cause` | text | LLM root cause explanation |
| `proposed_fix` | text | LLM fix description |
| `rca_confidence` | float | RCA confidence score (0.0–1.0) |
| `fix_summary` | text | Fix + deploy outcome summary |
| `status` | string | Current pipeline status (see flow above) |
| `verification_status` | string | `UNVERIFIED` / `VERIFIED` / `FAILED` |
| `sender` | string | CPI sender system (filled by observer) |
| `receiver` | string | CPI receiver system (filled by observer) |
| `log_start` | timestamp | Message processing start (filled by observer) |
| `log_end` | timestamp | Message processing end (filled by observer) |
| `created_at` | timestamp | Incident creation time |
| `resolved_at` | timestamp | Time of successful verification |
| `occurrence_count` | int | Number of times this error has been seen |
| `consecutive_failures` | int | Consecutive fix attempt failures (circuit breaker) |

---

## 10. Frontend

### Location

Source: [`frontend/`](frontend/) — React + TypeScript, bundled with Vite.

Uses `@tanstack/react-query` for polling, CSS Modules for styling (dark theme,
`#111827` background, no Tailwind).

### Key Pages & Tabs

| Page | Tab | Description |
|---|---|---|
| Observability | **Event Mesh** | Live SVG pipeline diagram with animated glowing nodes, 6 stats cards (total, retrieved, queue depth, stage counts), auto-scrolling event log. Polls `/aem/status` and `/autonomous/incidents` every 3 s. |
| Observability | **Messages** | Paginated CPI failed messages with AI analysis, explain-error, and fix-patch actions |
| Observability | **Agent Monitor** | Real-time agent running state, tool distribution per agent, autonomous loop toggle |
| Observability | **AEM Status** | Event Mesh connectivity status, enabled flag, queue depth |
| Dashboard | — | Incident summary cards, fix rate, recent activity |
| Incidents | — | Full incident list with status filter, detail drawer, approve/reject UI |
| Chat | — | General chatbot backed by the full MCP agent |

### Backend Connection

The frontend reads `VITE_API_BASE` (defaults to `/api`) for the backend base URL.
In production on Cloud Foundry, an nginx reverse-proxy rule forwards `/api/*` to the
backend application, so no hard-coded URLs are needed in the built artefact.

**Deployed frontend URL:** `https://nd-orbit-eventmesh-fe.cfapps.us10-001.hana.ondemand.com`

---

## Project Structure

```
auto-remediation - EventMesh/
├── main.py                          # FastAPI app — all HTTP endpoints, lifespan wiring
├── smart_monitoring.py              # /smart-monitoring/* router
├── smart_monitoring_dashboard.py    # /dashboard/* router
├── generate_dashboard_pdf.py        # PDF export utility
│
├── agents/
│   ├── base.py                      # StepLogger, TestExecutionTracker base classes
│   ├── classifier_agent.py          # Rule-based + LLM error classifier
│   ├── observer_agent.py            # SAP CPI OData polling + metadata enrichment
│   ├── rca_agent.py                 # LLM-based root cause analysis
│   ├── fix_agent.py                 # iFlow fix generation + deploy pipeline
│   ├── verifier_agent.py            # Post-fix test execution + runtime check
│   └── orchestrator_agent.py        # Pipeline coordinator, dedup logic, chatbot
│
├── aem/
│   └── event_bus.py                 # SAP Event Mesh publisher (OAuth2 token cache,
│                                    #   publish / publish_to_next / in-process fallback)
│
├── core/
│   ├── constants.py                 # Tuning constants, prompt templates, error rules
│   ├── mcp_manager.py               # MultiMCP — connects to 3 MCP servers
│   ├── state.py                     # In-memory fix progress tracking
│   ├── validators.py                # iFlow XML validation helpers
│   └── xml_patcher.py               # Structured XML patch operations
│
├── config/
│   └── config.py                    # Settings class + runtime auto-fix override
│
├── db/
│   └── database.py                  # HANA Cloud CRUD, auto-schema, dedup queries
│
├── storage/
│   ├── storage.py                   # File upload + XSD detection
│   └── object_store.py              # AWS S3 operations
│
├── utils/
│   ├── utils.py                     # HANA timestamp helpers
│   ├── logger_config.py             # Rotating file + console logger setup
│   ├── vector_store.py              # HANA vector search for SAP Notes
│   └── xsd_handler.py               # XSD parsing + validation
│
├── frontend/                        # React + TypeScript frontend (Vite)
│   ├── src/pages/observability/     # EventMeshFlow, AgentMonitor, AEM status tabs
│   └── src/services/api.ts          # Typed API client for all backend endpoints
│
├── logs/                            # Rotating application logs (auto-created)
├── .env                             # Secrets — NEVER commit
├── .env.example                     # Template with placeholder values
├── requirements.txt                 # Python production dependencies
└── CLAUDE.md                        # AI assistant coding standards for this project
```
