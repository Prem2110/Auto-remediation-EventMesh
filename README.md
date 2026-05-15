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
13. [ITSM Integration](#13-itsm-integration)

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

### In-Process Fallback (`EM_ENABLED=false`)

When `EM_ENABLED=false` (local dev), `publish_to_next()` dispatches to in-process
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
  - Run the fix pipeline via `FixSupervisor`, which rotates through four strategies in priority order:
    1. **`direct_patch`** — applies RCA-specified property changes directly to iFlow XML (no LLM, no network call); supports multiple simultaneous property changes from a single RCA pass
    2. **`component_replace`** — swaps the broken component with a validated reference iFlow component
    3. **`structured`** — applies structured operation list (change_type / field / target_component) via XML patcher
    4. **`free_xml`** — full LLM agent loop: `get-iflow` → fix XML → `validate_iflow_xml` → `update-iflow` → `deploy-iflow`
  - Strategies are retried up to 3 times; non-recoverable stages (`locked`, `timeout`, `validation_blocked`, etc.) halt immediately without retry
  - For `free_xml`: `validate_iflow_xml` detects structural regressions and returns a `FATAL:` response; the agent stops immediately instead of burning its full tool budget
  - Update DB: `fix_summary`, `fix_applied`, `status`
  - Only publish to verifier if fix was successfully applied and deployed; otherwise halt
- **Publishes to:** `default/sierra.automation/1/autofix/orbit/verifier` (success only)
- **On failure:** Sets status `FIX_FAILED` / `FIX_FAILED_UPDATE` / `FIX_FAILED_DEPLOY`

#### Fix Pipeline Sub-components

| Module | Role |
|---|---|
| `fix_supervisor.py` | Orchestrates strategy rotation and retry logic |
| `fix_planner.py` | Selects strategy; runs direct_patch / component_replace / structured diagnosis |
| `fix_generator.py` | Runs the free-XML LLM agent; scales timeout by XML size (120 s / 300 s) |
| `fix_validator.py` | Pre-validates patched XML before sending to SAP CPI |
| `fix_applier.py` | Calls SAP MCP tools; evaluates LLM tool-call history for free-XML mode |
| `fix_context.py` | Frozen dataclass — shared contract across all sub-components |
| `fix_component_replacer.py` | Fetches reference iFlow and merges the target component |
| `fix_xml_analyst.py` | Static XML component-map summary injected into the LLM prompt |

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

### How message routing works

Each queue requires **two independent subscriptions** — both must be present for the
pipeline to function:

| Subscription type | What it does | Where to configure |
|---|---|---|
| **Queue Topic Subscription** | Routes published EM topic messages *into* the queue | EM admin → Queues → click queue → **Queue Subscriptions** tab |
| **Webhook Subscription** | Pushes messages *out of* the queue to your HTTP endpoint | EM admin → **Webhooks** tab |

> If the Queue Topic Subscription is missing, SAP Event Mesh accepts the publish with
> HTTP 204 but the message never enters the queue — so the webhook never fires and
> incidents stay permanently at `CLASSIFIED`.

### Queues

Create the following 5 queues in the `em_automation` Event Mesh service instance.
Recommended settings: **Access type: Non-Exclusive**, **Message retention: 7 days**.

| Queue Name | Purpose |
|---|---|
| `default/sierra.automation/1/autofix/orbit/orchestrator` | Receives raw iFlow error events from CPI Monitor |
| `default/sierra.automation/1/autofix/orbit/observer` | Receives classified incidents for enrichment |
| `default/sierra.automation/1/autofix/orbit/rca` | Receives enriched incidents for RCA |
| `default/sierra.automation/1/autofix/orbit/fixer` | Receives RCA-complete incidents for fix |
| `default/sierra.automation/1/autofix/orbit/verifier` | Receives deployed fixes for verification |

### Queue Topic Subscriptions

For each queue, go to **EM admin → Queues → click the queue name → Queue Subscriptions tab → + Add**
and add the exact topic path shown below:

| Queue | Topic subscription to add |
|---|---|
| `default/sierra.automation/1/autofix/orbit/orchestrator` | `default/sierra.automation/1/autofix/in` |
| `default/sierra.automation/1/autofix/orbit/observer` | `default/sierra.automation/1/autofix/orbit/observer` |
| `default/sierra.automation/1/autofix/orbit/rca` | `default/sierra.automation/1/autofix/orbit/rca` |
| `default/sierra.automation/1/autofix/orbit/fixer` | `default/sierra.automation/1/autofix/orbit/fixer` |
| `default/sierra.automation/1/autofix/orbit/verifier` | `default/sierra.automation/1/autofix/orbit/verifier` |

### Webhook Subscriptions

Create one REST delivery webhook per queue in **EM admin → Webhooks tab**:

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
| `VECTOR_DIMENSION` | Embedding vector size (e.g. `3072`) |

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
| `EM_ENABLED` | `false` | `true` = publish to SAP Event Mesh REST API via Destination; `false` = in-process fallback only |
| `EM_REST_URL` | — | Event Mesh REST gateway base URL (`https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com`) |
| `EM_QUEUE_PREFIX` | — | Topic prefix for agent-to-agent routing (e.g. `default/sierra.automation/1/autofix/orbit`). Stage names are appended automatically. |
| `EM_INGEST_TOPIC` | — | Inbound topic the CPI Monitor publishes to (e.g. `default/sierra.automation/1/autofix/in`). The orchestrator queue subscribes here. |
| `EVENT_MESH_DESTINATION_NAME` | `EventMesh` | SAP BTP Destination name. Resolved at runtime via the Destination service binding (`VCAP_SERVICES`). |

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
| `HANA_TABLE_EM_SETTINGS` | `EM_RUNTIME_SETTINGS` | Runtime settings table |
| `HANA_TABLE_EM_AGENT_EVENTS` | `EM_AGENT_EVENT_LOG` | Agent execution event log (tokens, timing) |
| `HANA_TABLE_VECTOR` | `SAP_HELP_DOCS` | HANA vector store table for SAP Notes retrieval |

### AWS S3 Object Store

| Variable | Description |
|---|---|
| `BUCKET_NAME` | S3 bucket name |
| `REGION` | AWS region (e.g. `us-east-1`) |
| `HOST` | S3 hostname used to build the endpoint URL (default: `s3.amazonaws.com`) |
| `WRITE_ACCESS_KEY_ID` | AWS access key for write (upload) operations |
| `WRITE_SECRET_ACCESS_KEY` | AWS secret key for write operations |
| `READ_ACCESS_KEY_ID` | AWS access key for read (download/list) operations |
| `READ_SECRET_ACCESS_KEY` | AWS secret key for read operations |

### Autonomous Operations — Feature Flags

| Variable | Default | Description |
|---|---|---|
| `AUTO_FIX_CONFIDENCE` | `0.90` | Min RCA confidence to auto-apply a fix |
| `SUGGEST_FIX_CONFIDENCE` | `0.70` | Min confidence to suggest (not apply) a fix |
| `AUTO_FIX_ALL_CPI_ERRORS` | `true` | `true` = auto-fix every fixable error regardless of per-type policy |
| `AUTO_DEPLOY_AFTER_FIX` | `true` | Automatically deploy the iFlow after a successful fix update |
| `MAX_CONSECUTIVE_FAILURES` | `5` | Circuit breaker: escalate after N consecutive failures |
| `PENDING_APPROVAL_TIMEOUT_HRS` | `24` | Hours before pending approval auto-escalates |
| `PATTERN_MIN_SUCCESS_COUNT` | `2` | Min successful fixes before a pattern is trusted |
| `BURST_DEDUP_WINDOW_SECONDS` | `60` | Window for absorbing rapid repeat events |
| `FAILED_MESSAGE_FETCH_LIMIT` | `100` | Max failed messages fetched per polling cycle |

### Server & Logging

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | Server port (used by CF and local `__main__` entrypoint) |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ENABLE_CONSOLE_LOGS` | `true` | Mirror structured logs to stdout |
| `WEB_SEARCH_ENABLED` | `false` | Enable DuckDuckGo search tool for agents |
| `UPLOAD_ROOT` | `user` | Root prefix for S3 file uploads |
| `TICKET_DEFAULT_ASSIGNEE` | — | Default assignee written to new escalation tickets |
| `PENDING_DEPLOY_SWEEP_INTERVAL_SECONDS` | `300` | Seconds between background sweeps that retry pending deploys |

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
| `GET` | `/event-mesh/status` | AEM connectivity, queue depth, stage counts, enabled flag |

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
| `POST` | `/autonomous/test_incident` | Create a synthetic test incident (iFlow and error are hardcoded) |

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

### Run in In-Process Mode (`EM_ENABLED=false`)

Recommended for local development. No SAP Event Mesh connectivity required.
The five agents call each other in-process via the in-memory event bus.

```bash
# .env settings:
EM_ENABLED=false
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
curl -X POST http://localhost:8080/autonomous/test_incident
```

> **Note:** This endpoint takes no request body. The iFlow ID and error message are
> hardcoded in the server for smoke-testing purposes.

### Run with SAP Event Mesh (`EM_ENABLED=true`)

Requires the 5 queues and webhook subscriptions configured (see [Section 5](#5-sap-event-mesh-setup)).
The app must have a public HTTPS URL reachable from SAP Event Mesh.

All auth goes through the SAP Destination service. On Cloud Foundry this is handled
automatically via the `destination-service` binding. For local testing, simulate it by
setting `VCAP_SERVICES` manually, or use `cf ssh` tunnelling.

```bash
# .env settings:
EM_ENABLED=true
EM_REST_URL=https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com
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
cf set-env orbit-is-be EM_ENABLED        "true"
cf set-env orbit-is-be EM_REST_URL       "https://enterprise-messaging-pubsub.cfapps.us10.hana.ondemand.com"
cf set-env orbit-is-be EM_QUEUE_PREFIX   "default/<namespace>/1/autofix/orbit"
cf set-env orbit-is-be EM_INGEST_TOPIC   "default/<namespace>/1/autofix/in"

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
| `EM_AGENT_EVENT_LOG` | `HANA_TABLE_EM_AGENT_EVENTS` | One row per agent LLM invocation — correlation ID, activity, tokens |

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
    ├──► AUTO_FIXED            fix applied via smart-monitoring path  ✓
    └──► FIX_FAILED_RUNTIME    verifier found the iFlow still failing ✗

── Failure variants (pipeline halts) ──────────────────────────────────
OBS_FAILED              observer threw an exception
RCA_FAILED              RCA agent threw an exception
FIX_FAILED              fixer threw an unhandled exception
FIX_FAILED_UPDATE       SAP CPI rejected the update (incl. structural
                          validation block — validation_blocked stage)
FIX_FAILED_DEPLOY       deploy step failed
FIX_FAILED_RUNTIME      post-deploy verification failed

── Special statuses ────────────────────────────────────────────────────
ARTIFACT_MISSING              iFlow not found in SAP CPI design time
AWAITING_APPROVAL             human must approve before fix is applied
FIX_APPLIED_PENDING_VERIFY    XML uploaded but deploy not confirmed yet
TICKET_CREATED                escalated; ticket raised in external system
BURST_DEDUPED                 absorbed as duplicate within dedup window
CIRCUIT_BREAKER_ESCALATED     too many consecutive failures; auto-escalated
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
| Observability | **Event Mesh** | Live SVG pipeline diagram with animated glowing nodes, 6 stats cards (total, retrieved, queue depth, stage counts), auto-scrolling event log. Polls `/event-mesh/status` and `/autonomous/incidents` every 3 s. |
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

### Agent execution event log — token tracking per pipeline run

Every LLM invocation in the autonomous pipeline now writes a row to `EM_AGENT_EVENT_LOG`.
The table is auto-created by `ensure_em_schema()` on startup alongside the other EM tables.

**Schema:**

| Column | Type | Description |
|---|---|---|
| `EVENT_ID` | `NVARCHAR(100)` PK | UUID generated per invocation |
| `CORRELATION_ID` | `NVARCHAR(200)` | SAP message GUID (`message_guid`) — ties all agent rows back to the originating CPI failure |
| `ACTIVITY_NAME` | `NVARCHAR(100)` | Agent that fired: `rca_agent`, `fix_planner`, `fix_generator` |
| `TIMESTAMP` | `NVARCHAR(64)` | ISO-8601 UTC at invocation start |
| `TOKENS_IN` | `INTEGER` | Prompt tokens consumed |
| `TOKENS_OUT` | `INTEGER` | Completion tokens returned |

**Coverage:**

| Agent | Source file | Logged on |
|---|---|---|
| RCA | `agents/rca_agent.py` | Success (real tokens), timeout, exception |
| Fix Planner | `agents/fix_planner.py` | Success (real tokens), timeout, exception |
| Fix Generator | `agents/fix_generator.py` | Success (real tokens), all 5 early-return sentinels (TIMEOUT, RECURSION_LIMIT, VALIDATION_BLOCKED × 2, ERROR) |

Timeout and error rows are logged with `TOKENS_IN=0, TOKENS_OUT=0` so pipeline failures
are still visible in the log.

Token counts are extracted by `extract_token_counts()` in `agents/base.py`, which reads
`usage_metadata` (LangChain ≥ 0.2) and falls back to `response_metadata["token_usage"]`
for older model wrappers.

To query total tokens consumed by a pipeline run:

```sql
SELECT ACTIVITY_NAME, SUM(TOKENS_IN) AS prompt, SUM(TOKENS_OUT) AS completion
FROM "EM_AGENT_EVENT_LOG"
WHERE CORRELATION_ID = '<message_guid>'
GROUP BY ACTIVITY_NAME;
```

---

### All classifier error types now have Settings page entries

8 error types recognised by the classifier were previously missing from
`REMEDIATION_POLICIES`, so they never appeared in the Settings page and had no
explicit remediation action. They are now included with the following defaults:

| Error Type | Default Action | Replay After Fix |
|---|---|---|
| `ODATA_ERROR` | `AUTO_FIX` | Yes |
| `GROOVY_ERROR` | `AUTO_FIX` | Yes |
| `SCRIPT_ERROR` | `AUTO_FIX` | Yes |
| `SOAP_ERROR` | `AUTO_FIX` | Yes |
| `ROUTING_ERROR` | `AUTO_FIX` | Yes |
| `PROPERTY_ERROR` | `AUTO_FIX` | Yes |
| `IDOC_ERROR` | `TICKET_CREATED` | No |
| `RESOURCE_ERROR` | `TICKET_CREATED` | No |

**Existing deployments** — the runtime config now merges the schema defaults with any
stored policy on startup. New error types are automatically added to your saved
policy without resetting your existing customisations.

---

### Non-fixable error types short-circuit to TICKET_CREATED at orchestrator stage

Error types that cannot be resolved by modifying iFlow XML (e.g. `BACKEND_ERROR`,
`SFTP_ERROR`, `SSL_ERROR`, `CONNECTIVITY_ERROR`) now skip the full
observer → RCA → fixer → verifier pipeline entirely. The orchestrator creates an
external ticket immediately after incident creation, provided:

1. `is_iflow_fixable(error_type)` returns `False`
2. Classifier confidence is ≥ 0.80
3. The `REMEDIATION_POLICIES` setting for that error type has `action: TICKET_CREATED`

The policy check reads `_runtime_cfg.get("REMEDIATION_POLICIES")` at runtime so any
override configured in the Settings page is always respected.

### Event Mesh topic URL encoding fix

`event_mesh/event_bus.py` was calling `quote(topic, safe="")` which encoded forward
slashes as `%2F` in the REST publish URL. SAP Event Mesh requires real slashes in the
topic path segment. Changed to `quote(topic, safe="/")` in both `_publish_rest` and
`publish_to_next`.

### Pipeline tab UI — 3-dot loader and per-stage progress popup

- In-progress incidents (`CLASSIFIED`, `OBSERVED`, `RCA_IN_PROGRESS`, `RCA_COMPLETE`,
  `FIX_IN_PROGRESS`) now show an animated 3-dot bouncing loader next to the status chip.
- Clicking any row opens a modal showing all 4 pipeline stages (Observer, RCA, Fixer,
  Verifier) with animated state indicators (Done / Running / Waiting / Failed), plus
  root cause and proposed fix when available.

### Fix pipeline refactored into a supervised multi-strategy engine

The fixer agent now delegates entirely to `FixSupervisor`, which owns a four-strategy
rotation (`direct_patch → component_replace → structured → free_xml`) with up to three
attempts. Each attempt is independent — the original iFlow XML is restored before retry
so a bad attempt never poisons the next one. Non-recoverable stages
(`locked`, `timeout`, `no_tool_calls`, `validation_blocked`) stop rotation immediately
without consuming further attempts.

### Multi-fix DirectPatch — atomic multi-property patching without LLM

The RCA agent now outputs a `fixes` list in addition to the legacy scalar
`property_to_change` / `correct_value` fields. `FixPlanner._apply_direct_patch()` applies
all entries in a single XML parse pass: it builds an `id → element` map once (O(1)
lookup), iterates every fix, patches `ifl:property` child elements or falls back to plain
XML attributes, and returns `None` only if zero patches were applied — triggering fallback
to the next strategy. This means a single RCA pass can now fix multiple misconfigured
properties in one atomic operation.

### Structural validation early-bail — stop agent loops on unresolvable errors

When the free-XML LLM agent calls `validate_iflow_xml` and the errors contain structural
regressions it introduced (e.g. removed a CBR default route, broke `exclusiveGateway`
routing), the tool now returns `FATAL: <error>` instead of `ERRORS: <error>`. The agent's
system prompt instructs it to stop immediately and return `failed_stage="validation_blocked"`.
All three exit paths in `FixGenerator` (`TimeoutError`, `GraphRecursionError`, normal
completion) check for a `FATAL:` step in the tool-call history and return the
`__VALIDATION_BLOCKED__` sentinel instead of the normal `__TIMEOUT__` / `__RECURSION_LIMIT__`
sentinels. The supervisor treats `validation_blocked` as non-recoverable — preventing the
agent from burning its full 300-second budget retrying an error it cannot fix.

### Free-XML agent timeout now scales with iFlow XML size

Previously the free-XML LLM agent always ran with a fixed 600 s timeout. The timeout is
now set at invocation time based on the pre-fetched iFlow XML size:

- XML < 30,000 chars → **120 s**
- XML ≥ 30,000 chars → **300 s**

### iFlow collaboration key whitelist — eliminates false-positive validator errors

`core/validators.py` now includes `ALLOWED_COLLABORATION_KEYS` — a frozenset of 16 known
valid global iFlow property keys (`namespaceMapping`, `httpSessionHandling`, `corsEnabled`,
etc.) that appear inside `<bpmn2:collaboration>` extensionElements. The XML diff validator
no longer flags these as unexpected new keys after a fix is applied.

### Frontend fix summary — clean human-readable result display

The Observability page "Fix Applied" banner previously rendered raw JSON including
`__TOOLS_INVOKED__=...` metadata. A `cleanFixSummary()` helper now extracts the `summary`
field from any embedded JSON response before display, falling back to the text before the
first `{` if no JSON is present.

### Smart monitoring background fix — proper status tracking and error isolation

`_run_fix_background()` in `smart_monitoring.py` now:
- Initialises `result = None` before the execution block to prevent `UnboundLocalError`
- Separates the execution `try/except` from the DB-write `try/except` so a DB failure
  cannot mask the fix outcome
- Explicitly sets status `AUTO_FIXED` on success (previously only failure was persisted)
- Uses `result.get("success")` (the authoritative field) rather than ANDing
  `fix_applied AND deploy_success`

### `determine_post_fix_status` — `retry_result=None` now correctly reaches `FIX_VERIFIED`

When `post_fix_policy.action == "RETRY"` but no retry was attempted (e.g. the call
comes from `main.py` which does not run a test retry), `retry_result` is `None`. The
method previously fell through to `FIX_DEPLOYED`; it now returns `FIX_VERIFIED` for the
`None` case.

### CPI iFlow replaced by Python microservice

The SAP CPI iFlow that previously captured failed messages and published them to Event
Mesh has been replaced by `cpi_monitor/` — a lightweight Python asyncio background task
that starts with the FastAPI app. No iFlow deployment or SAP CPI design-time changes are
needed for the ingestion path.

### SAP Destination Service for all Event Mesh auth

Previously, Event Mesh credentials were passed as plain env vars (`EVENT_MESH_CLIENT_ID`,
`EVENT_MESH_CLIENT_SECRET`, `EVENT_MESH_TOKEN_URL`). These have been removed entirely.
Both `cpi_monitor/error_publisher.py` (inbound publish) and `event_mesh/event_bus.py`
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

## 13. ITSM Integration

Orbit integrates with an in-house ITSM system (OhZone) via its OData v4 REST API,
accessed through an SAP BTP Destination named **`ITSM-sierra`**.  The integration
covers three flows:

1. **Orbit → ITSM** — auto-creates a ticket when escalation is triggered
2. **Orbit UI → ITSM** — operator status updates are immediately PATCHed to ITSM
3. **ITSM → Orbit** — a background poller detects resolved tickets and syncs status back

---

### 13.1 SAP BTP Destination Setup

The ITSM API is not called directly.  All calls are routed through the SAP BTP
Destination service so credentials never touch application code.

**Steps:**

1. In SAP BTP Cockpit → your subaccount → **Destinations**, create a destination:

   | Field | Value |
   |---|---|
   | Name | `ITSM-sierra` |
   | Type | `HTTP` |
   | URL | Base URL of the ITSM OData service |
   | Authentication | `OAuth2ClientCredentials` |
   | Client ID | ITSM OAuth client ID |
   | Client Secret | ITSM OAuth client secret |
   | Token Service URL | `https://<itsm-oauth-host>/oauth/token` |

2. Bind the **Destination** service instance to the Orbit CF application so
   `VCAP_SERVICES` is populated automatically.

3. Set `ITSM_REQUESTER_ID` to the SAP user UUID that owns the tickets
   (see [Environment Variables](#6-environment-variables)).

---

### 13.2 Authentication Flow

`integrations/itsm_client.py` resolves credentials in three steps on every cache
miss:

```
Step 1  POST <destination-service-token-url>/oauth/token
        grant_type=client_credentials  →  dest_token

Step 2  GET  <destination-uri>/destination-configuration/v1/destinations/ITSM-sierra
        Authorization: Bearer <dest_token>
        →  {destinationConfiguration: {URL}, authTokens: [{value, expires_in}]}

Step 3  Cache base_url + itsm_token until (now + expires_in - 60s)
        Subsequent calls use the cached values without re-resolving.
```

Cache is stored in the module-level `_itsm_cache` dict.  A 401 or 403 response
from any ITSM call invalidates the cache immediately and forces re-resolution on
the next request.

---

### 13.3 When Tickets Are Created

The orchestrator creates an ITSM ticket in two situations:

| Trigger | Condition | `ticket_source` value |
|---|---|---|
| **Non-fixable error** | `is_iflow_fixable(error_type)` returns `False` AND `REMEDIATION_POLICIES[error_type].action == TICKET_CREATED` | `CLASSIFIER_ESCALATION` |
| **Fix failed** | Fixer or verifier reports failure after exhausting retries | `FIX_FAILED_ESCALATION` |

The orchestrator calls `create_itsm_ticket()` which posts to:

```
POST /odata/v4/ticket/Tickets
```

**Mandatory fields sent:**

| ITSM Field | Source |
|---|---|
| `title` | `"<error_type> in <iflow_id>"` |
| `type_code` | `"incident"` |
| `source` | `"Orbit Monitor app"` |
| `requester_ID` | `ITSM_REQUESTER_ID` env var |
| `description` | LLM-generated root cause + proposed fix |
| `priority` / `severity_code` | LLM-assigned (default `medium`) |
| `environment` | `CPI_ENVIRONMENT` env var |

On success ITSM returns `{ID: <uuid>, ticketNumber: "TCK-YYYY-NNNNN"}`.  Both
values are written to `EM_ESCALATION_TICKETS.itsm_ticket_id` so the poller and UI
can reference them.

---

### 13.4 Orbit UI → ITSM (Operator Updates)

When an operator changes ticket status in the Orbit UI, the
`PATCH /tickets/{ticket_id}` endpoint in `main.py` calls
`patch_itsm_ticket()` **immediately** (fire-and-forget `asyncio.create_task`) so
the change is reflected in ITSM without waiting for the next poll cycle.

**Status mapping:**

| Orbit status | ITSM `status` string |
|---|---|
| `IN_PROGRESS` | `Assigned` |
| `RESOLVED` | `Resolved` |

The PATCH request:

```
PATCH /odata/v4/ticket/Tickets/{itsm_ticket_id}
Authorization: Bearer <token>
Content-Type:  application/json
Accept:        application/json
If-Match:      *          ← required by OData v4 for unconditional PATCH

{"status": "Resolved", "resolutionNotes": "<operator comment>"}
```

> **Note:** `If-Match: *` is mandatory for OData v4 PATCH.  Without it the server
> returns HTTP 400.

---

### 13.5 ITSM → Orbit (Background Poller)

`jobs/itsm_poller.py` runs as a long-lived `asyncio` task started in `main.py`
on application startup.

**Poll cycle (`poll_itsm_tickets_once`):**

```
1. db.get_open_itsm_tickets()
   → all EM_ESCALATION_TICKETS where status NOT IN ('RESOLVED','CANCELLED')
     AND itsm_ticket_id IS NOT NULL

2. For each ticket:
   GET /odata/v4/ticket/Tickets/{itsm_ticket_id}
   → read "status" field

3. If status ∈ {"resolved", "cancelled"}:
   a. UPDATE EM_ESCALATION_TICKETS → status=RESOLVED, resolution_notes, resolved_at
   b. UPDATE EM_AUTONOMOUS_INCIDENTS → status=HUMAN_RESOLVED, fix_summary, resolved_at
   c. If resolution_notes present AND pattern not yet stored:
      → classifier.error_signature() → upsert_fix_pattern()  (agents reuse this fix)
      → UPDATE EM_ESCALATION_TICKETS pattern_stored=1        (prevents duplicate insert)

4. If ticket ID returns 404:
   → WARNING log with ticket_id + incident_id — ticket may be stale test data
     or was manually deleted from ITSM
```

**Timing:**

| Variable | Default | Description |
|---|---|---|
| `ITSM_POLL_INTERVAL` | `300` s | Seconds between full poll cycles |

**ITSM GET response fields used:**

| ITSM field | Mapped to |
|---|---|
| `status` | Compared against `{"resolved", "cancelled"}` |
| `resolutionNotes` | `resolution_notes` / `fix_summary` |
| `modifiedBy` | `resolved_by` (logged only) |
| `modifiedAt` | `resolved_at` timestamp |
| `ticketNumber` | Human-readable ID in log messages |

---

### 13.6 Database Table — `EM_ESCALATION_TICKETS`

| Column | Type | Description |
|---|---|---|
| `ticket_id` | `NVARCHAR(100)` PK | Internal Orbit ticket UUID |
| `incident_id` | `NVARCHAR(100)` | FK → `EM_AUTONOMOUS_INCIDENTS` |
| `iflow_id` | `NVARCHAR(200)` | SAP CPI iFlow identifier |
| `error_type` | `NVARCHAR(100)` | Classifier output (e.g. `ODATA_ERROR`) |
| `title` | `NVARCHAR(500)` | Ticket title sent to ITSM |
| `description` | `NCLOB` | Full ticket description |
| `priority` | `NVARCHAR(20)` | `low` / `medium` / `high` / `critical` |
| `status` | `NVARCHAR(20)` | `OPEN` → `IN_PROGRESS` → `RESOLVED` |
| `assigned_to` | `NVARCHAR(200)` | Assignee (optional) |
| `resolution_notes` | `NCLOB` | Engineer's resolution notes from ITSM |
| `created_at` | `NVARCHAR(64)` | ISO-8601 UTC |
| `updated_at` | `NVARCHAR(64)` | ISO-8601 UTC |
| `resolved_at` | `NVARCHAR(64)` | ISO-8601 UTC (set by poller) |
| `ticket_source` | `NVARCHAR(50)` | `CLASSIFIER_ESCALATION` or `FIX_FAILED_ESCALATION` |
| `itsm_ticket_id` | `NVARCHAR(200)` | UUID returned by ITSM on creation |
| `itsm_ticket_number` | `NVARCHAR(100)` | Human-readable ticket number (e.g. `TCK-2026-00045`) |
| `message_guid` | `NVARCHAR(200)` | CPI failed-message GUID |
| `human_fix_summary` | `NCLOB` | Summary of how engineer resolved it |
| `human_fix_steps` | `NCLOB` | Step-by-step resolution detail from engineer |
| `human_resolved_at` | `NVARCHAR(64)` | ISO-8601 UTC — when engineer marked it resolved |
| `pattern_stored` | `INTEGER` | `1` once fix is written to `EM_FIX_PATTERNS` |

---

### 13.7 Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ITSM_REQUESTER_ID` | ✅ | — | SAP user UUID stamped as `requester_ID` on every auto-created ticket |
| `ITSM_POLL_INTERVAL` | ✗ | `300` | Seconds between poller cycles |
| `CPI_ENVIRONMENT` | ✗ | `production` | Environment tag written to `environment` field on ticket |

The ITSM base URL and bearer token are **not** set via env vars — they are resolved
at runtime from the `ITSM-sierra` SAP BTP Destination bound to the app via
`VCAP_SERVICES`.

---

### 13.8 Debug & Health Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/itsm/status` | Token cache state, open ticket count, destination config |
| `GET` | `/itsm/verify` | 7-step destination diagnostic (connectivity, token, endpoint reachability) |
| `GET` | `/itsm/tickets` | Fetch live tickets from ITSM OData API |
| `POST` | `/itsm/test-ticket` | Fire a test ticket creation (smoke test) |
| `POST` | `/itsm/poll` | Manually trigger one poll cycle and return resolved count |

---

### 13.9 Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `PATCH` returns HTTP 400 | Missing `If-Match: *` header | Already fixed — header is now always sent |
| Ticket created in Orbit but not in ITSM | `ITSM_REQUESTER_ID` not set or `ITSM-sierra` destination misconfigured | Check `/itsm/status` endpoint and CF env vars |
| Status update from UI not reaching ITSM | `itsm_ticket_id` not stored in DB (ticket was created before ITSM was wired up) | Warning logged: `Skipping PATCH — ticket has no itsm_ticket_id stored in DB` |
| Poller logs `404` for every ticket | Test/demo `ITSM-TKT-XXXX` IDs in DB from development data | IDs are fake — delete those rows from `EM_ESCALATION_TICKETS` or ignore; real IDs are UUIDs |
| 401 / 403 on ITSM call | Cached token expired between cache expiry check and API call | Cache is invalidated automatically on 401/403 — next call will re-resolve |

---

## Project Structure

```
auto-remediation - EventMesh/
├── main.py                          # FastAPI app — all HTTP endpoints, lifespan wiring,
│                                    #   starts CPI Monitor background task on startup
├── smart_monitoring.py              # /smart-monitoring/* router
├── smart_monitoring_dashboard.py    # /dashboard/* router
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
│   ├── base.py                      # StepLogger, TestExecutionTracker, extract_token_counts
│   ├── classifier_agent.py          # Rule-based + LLM error classifier
│   ├── observer_agent.py            # SAP CPI OData polling + metadata enrichment
│   ├── rca_agent.py                 # LLM root cause analysis; outputs fixes[] list + scalar fields
│   ├── fix_agent.py                 # Fix pipeline coordinator (ask_fix_and_deploy, apply_fix)
│   ├── fix_supervisor.py            # Strategy rotation (direct_patch→replace→structured→free_xml)
│   ├── fix_planner.py               # Strategy selection; direct_patch; component_replace; structured
│   ├── fix_generator.py             # Free-XML LLM agent; structural FATAL: early-bail; timeout scaling
│   ├── fix_validator.py             # Pre-validate patched XML before SAP CPI update
│   ├── fix_applier.py               # Execute MCP calls; evaluate LLM tool-call history
│   ├── fix_context.py               # Frozen FixContext dataclass — shared pipeline contract
│   ├── fix_component_replacer.py    # Reference iFlow fetch + component merge
│   ├── fix_xml_analyst.py           # Static XML component map injected into LLM prompts
│   ├── verifier_agent.py            # Post-fix test execution + runtime check
│   └── orchestrator_agent.py        # Pipeline coordinator, dedup logic, chatbot
│
├── event_mesh/
│   └── event_bus.py                 # SAP Event Mesh publisher — bearer token via SAP
│                                    #   Destination service, publish / publish_to_next /
│                                    #   in-process fallback (EM_ENABLED=false)
│
├── integrations/
│   └── itsm_client.py               # ITSM OData client — create/get/patch tickets via
│                                    #   SAP BTP Destination (ITSM-sierra), token caching
│
├── jobs/
│   └── itsm_poller.py               # Background asyncio task — polls ITSM for resolved
│                                    #   tickets and syncs status back to Orbit DB
│
├── core/
│   ├── constants.py                 # Tuning constants, prompt templates, error rules
│   ├── runtime_config.py            # Live settings store — loaded from DB, in-memory overrides
│   ├── mcp_manager.py               # MultiMCP — connects to 3 MCP servers
│   ├── state.py                     # In-memory fix progress tracking
│   ├── validators.py                # iFlow XML validation helpers
│   └── xml_patcher.py               # Structured XML patch operations
│
├── config/
│   └── config.py                    # Settings class + runtime auto-fix override
│
├── db/
│   └── database.py                  # HANA Cloud CRUD, auto-schema, dedup queries, log_agent_event()
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
