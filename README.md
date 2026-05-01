# Orbit — SAP CPI Auto-Remediation Agent

An event-driven, multi-agent system that automatically detects, diagnoses, and fixes
errors in SAP Integration Suite (CPI) iFlows. When a CPI iFlow fails, the system
detects the failure via a background Python microservice, runs root cause analysis
using an LLM, applies a fix to the iFlow XML, redeploys it, and verifies the fix —
all without human intervention.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [CPI Monitor — Background Poller](#3-cpi-monitor--background-poller)
4. [Agent Roles](#4-agent-roles)
5. [SAP Event Mesh Setup](#5-sap-event-mesh-setup)
6. [Environment Variables](#6-environment-variables)
7. [API Endpoints](#7-api-endpoints)
8. [Local Development Setup](#8-local-development-setup)
9. [Deployment (Cloud Foundry)](#9-deployment-cloud-foundry)
10. [Database](#10-database)
11. [Frontend](#11-frontend)
12. [What's New](#12-whats-new)

---

## 1. Project Overview

**What it does:**

SAP CPI iFlows occasionally fail due to mapping errors, missing fields, endpoint
timeouts, or schema mismatches. Orbit monitors these failures in real time:

- **CPI Monitor** — a background Python microservice (replaces the old error-capturing
  iFlow) that queries CPI OData every `CPI_POLL_INTERVAL_SECONDS` (default 600 s) for
  `FAILED` messages and publishes them to Event Mesh using the `EventMesh` SAP BTP
  Destination. No hardcoded OAuth credentials — all auth goes through the SAP Destination
  service binding.
- **SAP Event Mesh** — routes the published event to the orchestrator webhook, triggering
  the 5-stage AI agent pipeline.

The pipeline determines the root cause using an LLM, edits the iFlow XML to apply the
fix, deploys the updated iFlow back to SAP Integration Suite, and verifies it works —
writing the final outcome to the database.

**Tech stack:**

| Layer | Technology |
|---|---|
| API framework | FastAPI + Uvicorn |
| Agent orchestration | LangChain (tool-calling agents) |
| LLM | SAP AI Core — GPT-4 / Claude Sonnet via OpenAI-compatible API |
| MCP tool protocol | fastmcp `>=2.14.5`, langchain-mcp-adapters |
| Event bus | SAP Event Mesh (`em_automation`, namespace `default/sierra.automation/1`) |
| SAP CPI integration | OData API (OAuth2), Design-time / Runtime REST APIs |
| SAP BTP Destination | `EventMesh` destination — single auth source for all Event Mesh publishing |
| Database | SAP HANA Cloud via hdbcli |
| Object storage | AWS S3 via boto3 |
| Frontend | React + TypeScript (Vite) |
| Python | `>=3.13` |
| Logging | structlog + rotating file handlers |

---

## 2. Architecture

### System Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│  SAP CPI                                                             │
│  iFlow FAILED  ──────────────────────────────────────────────────┐   │
└──────────────────────────────────────────────────────────────────│───┘
                                                                   │
┌──────────────────────────────────────────────────────────────────▼───┐
│  cpi_monitor/ (Python microservice — background asyncio task)        │
│                                                                       │
│  Every CPI_POLL_INTERVAL_SECONDS (default: 600 s):                   │
│  GET /api/v1/MessageProcessingLogs?$filter=Status eq 'FAILED'        │
│        (CPI OData — auth: SAP_HUB_* env vars)                        │
│                │                                                      │
│                ▼                                                      │
│  GET /api/v1/MessageProcessingLogs('{guid}')/ErrorInformation/$value │
│                │                                                      │
│                ▼                                                      │
│  POST to SAP Event Mesh topic:                                        │
│    default/sierra.automation/1/autofix/in                            │
│    auth: EventMesh SAP BTP Destination (resolved via VCAP_SERVICES)  │
└──────────────────────────────────────┬───────────────────────────────┘
                                       │
                                       ▼  SAP Event Mesh (em_automation)
                                       │  delivers to queue
                                       │  default/sierra.automation/1/autofix/orbit/orchestrator
                                       │
┌──────────────────────────────────────▼───────────────────────────────┐
│  5-Stage AI Agent Pipeline                                           │
│                                                                      │
│  POST /agents/orchestrator  ──► classify + create incident           │
│         │  publish_to_next → default/sierra.automation/1/autofix/orbit/observer
│         ▼                                                            │
│  POST /agents/observer      ──► enrich with OData metadata           │
│         │  publish_to_next → default/sierra.automation/1/autofix/orbit/rca
│         ▼                                                            │
│  POST /agents/rca           ──► LLM root cause analysis              │
│         │  publish_to_next → default/sierra.automation/1/autofix/orbit/fixer
│         ▼                                                            │
│  POST /agents/fixer         ──► apply fix + deploy iFlow             │
│         │  publish_to_next → default/sierra.automation/1/autofix/orbit/verifier
│         ▼                                                            │
│  POST /agents/verifier      ──► verify fix  ──► FIX_VERIFIED ✓      │
└──────────────────────────────────────────────────────────────────────┘
```

### 5-Stage Agent Pipeline Detail

```
POST /agents/orchestrator
  • Normalize raw AEM message envelope
  • Classify error (rule-based + LLM fallback)
  • Dedup check (signature + burst window)
  • Create incident in DB  →  status: CLASSIFIED
        │
        │  publish_to_next → default/sierra.automation/1/autofix/orbit/observer
        ▼
POST /agents/observer
  • Fetch OData metadata (sender, receiver, log timestamps)
  • Enrich incident in DB  →  status: OBSERVED
        │
        │  publish_to_next → default/sierra.automation/1/autofix/orbit/rca
        ▼
POST /agents/rca
  • Run LLM root cause analysis (reads iFlow XML via MCP)
  • Update DB: root_cause, proposed_fix, rca_confidence
  • status: RCA_IN_PROGRESS → RCA_COMPLETE
        │
        │  publish_to_next → default/sierra.automation/1/autofix/orbit/fixer
        ▼
POST /agents/fixer
  • Apply fix: get-iflow → update-iflow → deploy-iflow (via MCP)
  • Update DB: fix_summary, fix_applied
  • status: FIX_IN_PROGRESS → FIX_DEPLOYED
  • Halt pipeline on failure — does NOT publish to verifier
        │
        │  publish_to_next → default/sierra.automation/1/autofix/orbit/verifier
        ▼
POST /agents/verifier
  • Check iFlow runtime status
  • Run test payload (HTTP-triggered iFlows only)
  • Write final status:
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

---

## 3. CPI Monitor — Background Poller

### What it does

`cpi_monitor/` is a self-contained microservice module started as a background
`asyncio` task on app startup. It replaces the SAP CPI iFlow that previously captured
failed message logs and sent them to Event Mesh.

```
Startup → asyncio.create_task(_run_cpi_monitor())
               │
               ▼  every CPI_POLL_INTERVAL_SECONDS (default: 600 s)
          poll_failed_messages()
               │  GET /api/v1/MessageProcessingLogs
               │  filter: Status eq 'FAILED'
               │         LogEnd ge datetime'{now - poll_interval}'
               │
               ▼
          For each FAILED message:
          • GET /api/v1/MessageProcessingLogs('{guid}')/ErrorInformation/$value
          • Skip if MessageGuid published within last 30 min (in-memory dedup)
          • Build payload:
            {
              "IflowId":             "<IntegrationFlowName>",
              "MessageGuid":         "<guid>",
              "IntegrationFlowName": "<IntegrationFlowName>",
              "Status":              "FAILED",
              "LogEnd":              "<timestamp>",
              "ErrorMessage":        "<raw error text>"
            }
          • POST to topic default/sierra.automation/1/autofix/in
            auth: EventMesh SAP BTP Destination
            (bearer token resolved at runtime via SAP Destination service — VCAP_SERVICES)
```

### Files

| File | Purpose |
|---|---|
| [cpi_monitor/\_\_init\_\_.py](cpi_monitor/__init__.py) | Empty package marker |
| [cpi_monitor/cpi_poller.py](cpi_monitor/cpi_poller.py) | OData poll loop, OAuth token cache, `get_cpi_client()`, `get_destination_service_creds()` |
| [cpi_monitor/error_publisher.py](cpi_monitor/error_publisher.py) | Error detail fetch, Event Mesh publish via Destination, 30-min dedup |

### Auth

| Operation | Credentials used |
|---|---|
| CPI OData polling | `SAP_HUB_TENANT_URL` + `SAP_HUB_TOKEN_URL` + `SAP_HUB_CLIENT_ID/SECRET` |
| Event Mesh publishing | `EventMesh` SAP BTP Destination resolved via `VCAP_SERVICES` (Destination service binding) |

All Event Mesh authentication goes through the SAP Destination service.
`EVENT_MESH_CLIENT_ID`, `EVENT_MESH_CLIENT_SECRET`, and `EVENT_MESH_TOKEN_URL` are no
longer used anywhere in this codebase.

### SAP BTP Destination — `EventMesh`

Create (or confirm) a Destination in your SAP BTP subaccount → Connectivity → Destinations:

| Field | Value |
|---|---|
| Name | `EventMesh` |
| Type | `HTTP` |
| Authentication | `OAuth2ClientCredentials` |
| URL | `https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com` |
| Client ID | `sb-default-...xbem-service-broker-!b732` |
| Client Secret | `<your-secret>` |
| Token Service URL | `https://<subdomain>.authentication.us10.hana.ondemand.com/oauth/token` |
| Additional Header | `Content-Type: application/json`, `x-qos: 1` |

The microservice looks this destination up at runtime via the SAP Destination service
binding (`VCAP_SERVICES` on CF). The app must have the Destination service instance bound
(see `manifest.yml` → `services: [destination-service]`).

### Startup Log

```
[CPI_MONITOR] Poll interval set to 600s
[CPI_MONITOR] Poller started, interval=600s
```

### Runtime Logs

```
[CPI_MONITOR] Poll complete, found 2 failed messages
[CPI_MONITOR] EventMesh destination token resolved successfully
[CPI_MONITOR] Published MessageGuid=abc-123 iflow=MyIflow
[CPI_MONITOR] Skipping duplicate MessageGuid=abc-123        ← dedup hit

[CPI_MONITOR] EventMesh destination token resolution FAILED - check destination binding
                                                            ← binding missing / misconfigured
```

---

## 4. Agent Roles

### Stage 1 — Orchestrator (`/agents/orchestrator`)

- **Input:** Raw CPI error event JSON (SAP Event Mesh push)
- **Responsibilities:**
  - Normalize the AEM multimap envelope into a flat incident dict
  - Classify the error type using rule-based patterns (zero latency)
  - Fall back to LLM classification when rule confidence < 70%
  - Apply signature dedup — if same iFlow + error type is already open, increment occurrence count and stop
  - Apply burst dedup — absorb rapid repeat events within `BURST_DEDUP_WINDOW_SECONDS`
  - Create a new incident record in HANA with status `CLASSIFIED`
- **Publishes to:** `default/sierra.automation/1/autofix/orbit/observer`
- **On failure:** Logs error; no incident is written to DB

### Stage 2 — Observer (`/agents/observer`)

- **Input:** `{"stage": "observer", "incident_id": "<uuid>"}`
- **Responsibilities:**
  - Read incident from DB
  - Call SAP CPI OData `MessageProcessingLogs('<guid>')` API
  - Enrich incident with: `iflow_id`, `sender`, `receiver`, `log_start`, `log_end`
  - Update DB status to `OBSERVED`
- **Publishes to:** `default/sierra.automation/1/autofix/orbit/rca`
- **On failure:** Sets status `OBS_FAILED`

### Stage 3 — RCA Agent (`/agents/rca`)

- **Input:** `{"stage": "rca", "incident_id": "<uuid>"}`
- **Responsibilities:**
  - Read enriched incident from DB
  - Run an LLM agent with a restricted tool set (`get-iflow`, `get_message_logs`)
  - Read the iFlow XML and CPI message logs to determine root cause
  - Update DB: `root_cause`, `proposed_fix`, `rca_confidence`, `affected_component`
  - Status progression: `RCA_IN_PROGRESS` → `RCA_COMPLETE`
- **Publishes to:** `default/sierra.automation/1/autofix/orbit/fixer`
- **On failure:** Sets status `RCA_FAILED`

### Stage 4 — Fixer Agent (`/agents/fixer`)

- **Input:** `{"stage": "fixer", "incident_id": "<uuid>"}`
- **Responsibilities:**
  - Read incident + RCA results from DB
  - Execute the fix pipeline via MCP: `get-iflow` → `update-iflow` → `deploy-iflow`
  - Evaluate outcome: `fix_applied AND deploy_success`
  - Update DB: `fix_summary`, `fix_applied`, `status`
  - Only publish to verifier if fix was successfully applied and deployed; otherwise halt
- **Publishes to:** `default/sierra.automation/1/autofix/orbit/verifier` (success only)
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

## 5. SAP Event Mesh Setup

### Event Mesh Instance

| Field | Value |
|---|---|
| Instance name | `em_automation` |
| Namespace | `default/sierra.automation/1` |
| Region | `us10` |

### Queues

Create the following 5 queues in the `em_automation` Event Mesh service instance:

| Queue Name | Topic Subscription | Purpose |
|---|---|---|
| `default/sierra.automation/1/autofix/orbit/orchestrator` | `default/sierra.automation/1/autofix/in` | Receives raw iFlow error events from CPI Monitor |
| `default/sierra.automation/1/autofix/orbit/observer` | `default/sierra.automation/1/autofix/orbit/observer` | Receives classified incidents for enrichment |
| `default/sierra.automation/1/autofix/orbit/rca` | `default/sierra.automation/1/autofix/orbit/rca` | Receives enriched incidents for RCA |
| `default/sierra.automation/1/autofix/orbit/fixer` | `default/sierra.automation/1/autofix/orbit/fixer` | Receives RCA-complete incidents for fix |
| `default/sierra.automation/1/autofix/orbit/verifier` | `default/sierra.automation/1/autofix/orbit/verifier` | Receives deployed fixes for verification |

Recommended queue settings: **Access type: Exclusive**, **Message retention: 7 days**

### Webhook Subscriptions

Create one REST delivery webhook per queue:

| Subscription Name | Source Queue | Webhook URL |
|---|---|---|
| `orbit-orchestrator` | `default/sierra.automation/1/autofix/orbit/orchestrator` | `https://<backend-url>/agents/orchestrator` |
| `orbit-observer` | `default/sierra.automation/1/autofix/orbit/observer` | `https://<backend-url>/agents/observer` |
| `orbit-rca` | `default/sierra.automation/1/autofix/orbit/rca` | `https://<backend-url>/agents/rca` |
| `orbit-fixer` | `default/sierra.automation/1/autofix/orbit/fixer` | `https://<backend-url>/agents/fixer` |
| `orbit-verifier` | `default/sierra.automation/1/autofix/orbit/verifier` | `https://<backend-url>/agents/verifier` |

Webhook settings: **Content-Type: application/json**, **Method: POST**

> **Note:** SAP Event Mesh sends an OPTIONS request to each webhook URL during subscription
> creation. The backend handles this automatically via `OPTIONS /agents/{agent_name}` →
> HTTP 200. No additional configuration is needed.

### Inbound Topic

The `default/sierra.automation/1/autofix/in` topic receives error events from the
CPI Monitor microservice. The orchestrator queue subscribes to this topic and delivers
events to `POST /agents/orchestrator`.

---

## 6. Environment Variables

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

### SAP Hub — CPI OData Polling (CPI Monitor + Observer agent)

| Variable | Description |
|---|---|
| `SAP_HUB_TENANT_URL` | CPI tenant base URL (no `/api/v1` suffix) |
| `SAP_HUB_TOKEN_URL` | OAuth2 token endpoint |
| `SAP_HUB_CLIENT_ID` | Client ID |
| `SAP_HUB_CLIENT_SECRET` | Client secret |

### CPI Monitor — Background Poller

| Variable | Default | Description |
|---|---|---|
| `CPI_POLL_INTERVAL_SECONDS` | `600` | How often the poller queries CPI for FAILED messages (seconds). Set to `30` for rapid local testing. |

### SAP Event Mesh

| Variable | Default | Description |
|---|---|---|
| `AEM_ENABLED` | `false` | `true` = publish to SAP Event Mesh REST API via Destination; `false` = in-process fallback only |
| `AEM_REST_URL` | — | Event Mesh REST gateway base URL (`https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com`) |
| `EVENT_MESH_DESTINATION_NAME` | `EventMesh` | SAP BTP Destination name. Resolved at runtime via the Destination service binding (`VCAP_SERVICES`). |
| `EVENT_MESH_QUEUE` | `default/sierra.automation/1/autofix/orbit/orchestrator` | Inbound queue name (shown in `/aem/status`) |

> **Removed:** `EVENT_MESH_TOKEN_URL`, `EVENT_MESH_CLIENT_ID`, and `EVENT_MESH_CLIENT_SECRET`
> are no longer used. All Event Mesh authentication now goes through the SAP Destination
> service. Remove these from any existing `.env` or `cf set-env` configuration.

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

## 7. API Endpoints

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
| `OPTIONS` | `/agents/{name}` | Responds 200 to SAP Event Mesh webhook subscription validation |

### Event Mesh Status

| Method | Path | Description |
|---|---|---|
| `GET` | `/aem/status` | AEM connectivity, queue depth, stage counts, enabled flag |
| `GET` | `/event-mesh/status` | Alias for `/aem/status` |

### CPI Monitor

| Method | Path | Description |
|---|---|---|
| `GET` | `/cpi-monitor/status` | Current poller config: base URL, token state, dedup cache, VCAP presence |
| `POST` | `/cpi-monitor/trigger` | Manually run one poll + publish cycle immediately |

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
| `POST` | `/autonomous/incidents/{id}/approve` | Approve or reject a pending fix |
| `POST` | `/autonomous/incidents/{id}/generate_fix` | Manually trigger fix generation |
| `POST` | `/autonomous/incidents/{id}/retry_rca` | Re-run RCA on an existing incident |
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

### Debug

| Method | Path | Description |
|---|---|---|
| `GET` | `/autonomous/db_test` | HANA connectivity + insert/fetch roundtrip |
| `GET` | `/autonomous/debug` | SAP credential env vars + observer health |
| `GET` | `/autonomous/debug2` | Raw CPI token + OData connectivity test |

---

## 8. Local Development Setup

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
# .env settings:
AEM_ENABLED=false
CPI_POLL_INTERVAL_SECONDS=30    # faster polling for local testing

uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

Trigger one poll cycle immediately (no need to wait):

```bash
curl -X POST http://localhost:8080/cpi-monitor/trigger
```

Check config and auth state:

```bash
curl http://localhost:8080/cpi-monitor/status
```

Test the full pipeline with a synthetic incident:

```bash
curl -X POST http://localhost:8080/autonomous/test_incident \
  -H "Content-Type: application/json" \
  -d '{"iflow_id": "MyTestFlow", "error_message": "Mapping failed: field not found"}'
```

### Run with SAP Event Mesh (`AEM_ENABLED=true`)

Requires the 5 queues and webhook subscriptions configured (see [Section 5](#5-sap-event-mesh-setup)).
The app must have a public HTTPS URL reachable from SAP Event Mesh.

All auth goes through the SAP Destination service. On Cloud Foundry this is handled
automatically via the `destination-service` binding. For local testing, simulate it by
setting `VCAP_SERVICES` manually, or use `cf ssh` tunnelling.

```bash
# .env settings:
AEM_ENABLED=true
AEM_REST_URL=https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com
EVENT_MESH_DESTINATION_NAME=EventMesh

# VCAP_SERVICES must be set for the Destination service lookup to work.
# For local dev, either simulate it or use cf ssh tunnelling.

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

## 9. Deployment (Cloud Foundry)

### manifest.yml

```yaml
applications:
  - name: orbit-is-be
    memory: 1G
    buildpacks:
      - python_buildpack
    command: python -m uvicorn main:app --host 0.0.0.0 --port $PORT
    services:
      - destination-service   # SAP BTP Destination service binding
    env:
      EVENT_MESH_DESTINATION_NAME: EventMesh
      CPI_POLL_INTERVAL_SECONDS: "600"
```

All sensitive credentials are set via `cf set-env` — never commit them.

### Deploy

```bash
cf push
```

### Required `cf set-env` Variables

```bash
# Event Mesh
cf set-env orbit-is-be AEM_ENABLED   "true"
cf set-env orbit-is-be AEM_REST_URL  "https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com"

# SAP CPI (used by CPI Monitor + Observer agent)
cf set-env orbit-is-be SAP_HUB_TENANT_URL    "https://<tenant>.it-cpi<n>.cfapps.<region>.hana.ondemand.com"
cf set-env orbit-is-be SAP_HUB_TOKEN_URL     "https://<tenant>.authentication.<region>.hana.ondemand.com/oauth/token"
cf set-env orbit-is-be SAP_HUB_CLIENT_ID     "<client-id>"
cf set-env orbit-is-be SAP_HUB_CLIENT_SECRET "<client-secret>"

# HANA
cf set-env orbit-is-be HANA_HOST     "<hana-host>"
cf set-env orbit-is-be HANA_USER     "<user>"
cf set-env orbit-is-be HANA_PASSWORD "<password>"
cf set-env orbit-is-be HANA_SCHEMA   "<schema>"

# AI Core
cf set-env orbit-is-be LLM_DEPLOYMENT_ID    "<deployment-id>"
cf set-env orbit-is-be AICORE_CLIENT_ID     "<client-id>"
cf set-env orbit-is-be AICORE_CLIENT_SECRET "<client-secret>"
cf set-env orbit-is-be AICORE_AUTH_URL      "https://<subdomain>.authentication.us10.hana.ondemand.com"
cf set-env orbit-is-be AICORE_BASE_URL      "https://api.ai.prod.us-east-1.aws.ml.hana.ondemand.com/v2"

cf restage orbit-is-be
```

> **Note:** Do **not** set `EVENT_MESH_CLIENT_ID`, `EVENT_MESH_CLIENT_SECRET`, or
> `EVENT_MESH_TOKEN_URL`. These are no longer used. Event Mesh auth is handled entirely
> by the `destination-service` binding.

### After Deployment — Wire Event Mesh Webhooks

Once the backend is live, update all 5 webhook subscriptions in `em_automation` to use
`https://orbit-is-be.cfapps.us10-001.hana.ondemand.com/agents/*`.

### Frontend

```bash
cd frontend
npm run build

cf push nd-orbit-eventmesh-fe \
  --buildpack staticfile_buildpack \
  --memory 256M \
  --path dist
```

**Deployed frontend URL:** `https://nd-orbit-eventmesh-fe.cfapps.us10-001.hana.ondemand.com`

---

## 10. Database

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

── Failure variants (pipeline halts) ──────────────────────────────────
OBS_FAILED              observer threw an exception
RCA_FAILED              RCA agent threw an exception
FIX_FAILED              fixer threw an unhandled exception
FIX_FAILED_UPDATE       SAP CPI rejected the iFlow XML update
FIX_FAILED_DEPLOY       deploy step failed
FIX_FAILED_RUNTIME      post-deploy verification failed

── Special statuses ────────────────────────────────────────────────────
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

## 11. Frontend

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
backend application.

**Deployed frontend URL:** `https://nd-orbit-eventmesh-fe.cfapps.us10-001.hana.ondemand.com`

---

## 12. What's New

### CPI iFlow replaced by Python microservice

The SAP CPI iFlow that previously captured failed messages and published them to Event
Mesh has been replaced by `cpi_monitor/` — a lightweight Python asyncio background task
that starts with the FastAPI app. No iFlow deployment or SAP CPI design-time changes are
needed for the ingestion path.

### SAP Destination Service for all Event Mesh auth

Previously, Event Mesh credentials were passed as plain env vars (`EVENT_MESH_CLIENT_ID`,
`EVENT_MESH_CLIENT_SECRET`, `EVENT_MESH_TOKEN_URL`). These have been removed entirely.
Both `cpi_monitor/error_publisher.py` (inbound publish) and `aem/event_bus.py`
(agent-to-agent publish) now resolve their bearer token exclusively via the SAP
Destination service — the `EventMesh` destination configured in BTP Connectivity and
exposed through the `VCAP_SERVICES` binding.

### New Event Mesh instance — `em_automation`

The pipeline now uses the `em_automation` Event Mesh service instance with namespace
`default/sierra.automation/1`. All topics and queue names follow the pattern
`default/sierra.automation/1/autofix/...`.

### OPTIONS endpoint for SAP Event Mesh webhook validation

SAP Event Mesh sends a plain HTTP OPTIONS request to each webhook URL during subscription
creation to verify the endpoint is reachable. A single route `OPTIONS /agents/{name}`
now handles all five agent paths and returns HTTP 200, satisfying the handshake without
any per-agent configuration.

### Configurable poll interval with startup logging

`CPI_POLL_INTERVAL_SECONDS` is now logged at startup so operators can confirm the
configured value:

```
[CPI_MONITOR] Poll interval set to 600s
[CPI_MONITOR] Poller started, interval=600s
```

After each poll cycle the result count is also logged:

```
[CPI_MONITOR] Poll complete, found 3 failed messages
```

---

## Project Structure

```
auto-remediation - EventMesh/
├── main.py                          # FastAPI app — all HTTP endpoints, lifespan wiring,
│                                    #   starts CPI Monitor background task on startup
├── smart_monitoring.py              # /smart-monitoring/* router
├── smart_monitoring_dashboard.py    # /dashboard/* router
├── generate_dashboard_pdf.py        # PDF export utility
├── manifest.yml                     # CF deployment manifest (EVENT_MESH_DESTINATION_NAME,
│                                    #   CPI_POLL_INTERVAL_SECONDS, destination-service binding)
│
├── cpi_monitor/                     # Background poller — replaces the error-capturing iFlow
│   ├── __init__.py
│   ├── cpi_poller.py                # OData poll, OAuth token cache, get_cpi_client(),
│   │                                #   get_destination_service_creds() shared utility
│   └── error_publisher.py           # Error detail fetch, EventMesh Destination resolution,
│                                    #   REST publish (x-qos:1), 30-min in-memory dedup
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
│   └── event_bus.py                 # SAP Event Mesh publisher — bearer token via SAP
│                                    #   Destination service, publish / publish_to_next /
│                                    #   in-process fallback (AEM_ENABLED=false)
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
