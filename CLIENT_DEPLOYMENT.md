# Orbit — Client Deployment Guide

Step-by-step instructions for onboarding a new client. No code changes required.
All client-specific configuration lives in environment variables.

---

## Prerequisites

The client's SAP BTP subaccount must have the following services provisioned before you begin:

| Service | Plan | Used for |
|---|---|---|
| SAP AI Core | Standard | LLM (GPT-4 / Claude) |
| SAP Event Mesh | Default | Agent-to-agent pipeline messaging |
| SAP HANA Cloud | — | Incident and fix-pattern store |
| SAP BTP Destination Service | Lite | EventMesh bearer-token resolution |
| SAP Integration Suite | — | iFlow monitoring + fix + deploy |
| AWS S3 (or compatible) | — | XSD / document uploads |

---

## Step 1 — Deploy the Three MCP Servers

Each client gets their own instances of the three MCP apps (separate CF pushes in the client's subaccount).

| App | Env var to note |
|---|---|
| Integration Suite MCP | `MCP_INTEGRATION_SUITE_URL` |
| Testing MCP | `MCP_TESTING_URL` |
| Documentation MCP | `MCP_DOCUMENTATION_URL` |

After pushing each app, note the CF route (e.g. `https://is-mcp-<client>.cfapps.<region>.hana.ondemand.com/mcp`). You will use these URLs in Step 6.

---

## Step 2 — Create the SAP BTP Destination `EventMesh`

Go to **BTP Cockpit → Connectivity → Destinations → New Destination**.

| Field | Value |
|---|---|
| Name | `EventMesh` *(must match `EVENT_MESH_DESTINATION_NAME` env var)* |
| Type | `HTTP` |
| Authentication | `OAuth2ClientCredentials` |
| URL | `https://enterprise-messaging-pubsub.cfapps.<region>.hana.ondemand.com` |
| Client ID | From the Event Mesh service key (`uaa.clientid`) |
| Client Secret | From the Event Mesh service key (`uaa.clientsecret`) |
| Token Service URL | From the Event Mesh service key (`uaa.url` + `/oauth/token`) |

This destination is the single credential source for all Event Mesh publishing.  
The app resolves it at runtime via the Destination service binding — no credentials in `.env`.

---

## Step 3 — Set Up SAP Event Mesh

### 3a — Note the namespace

From the Event Mesh service key, note the namespace.  
Example: `default/acme.corp/1`

All queue names and topics are built from this namespace. You will use it as:
```
EM_QUEUE_PREFIX = default/acme.corp/1/autofix/orbit
EM_INGEST_TOPIC = default/acme.corp/1/autofix/in
```

### 3b — Create 5 queues

In the Event Mesh management UI, create the following queues.  
Replace `<ns>` with the client's namespace (e.g. `default/acme.corp/1`).

| Queue Name | Topic Subscription | Purpose |
|---|---|---|
| `<ns>/autofix/orbit/orchestrator` | `<ns>/autofix/in` | Raw CPI error ingress from CPI Monitor |
| `<ns>/autofix/orbit/observer` | `<ns>/autofix/orbit/observer` | OData enrichment |
| `<ns>/autofix/orbit/rca` | `<ns>/autofix/orbit/rca` | LLM root cause analysis |
| `<ns>/autofix/orbit/fixer` | `<ns>/autofix/orbit/fixer` | Fix + deploy |
| `<ns>/autofix/orbit/verifier` | `<ns>/autofix/orbit/verifier` | Post-fix verification |

Recommended settings per queue: **Access type: Exclusive**, **Retention: 7 days**.

### 3c — Create 5 webhook subscriptions

For each queue create one REST delivery webhook.  
`<backend-url>` = the CF route of the Orbit backend app (created in Step 5).

| Subscription Name | Source Queue | Webhook URL |
|---|---|---|
| `orbit-orchestrator` | `<ns>/autofix/orbit/orchestrator` | `https://<backend-url>/agents/orchestrator` |
| `orbit-observer` | `<ns>/autofix/orbit/observer` | `https://<backend-url>/agents/observer` |
| `orbit-rca` | `<ns>/autofix/orbit/rca` | `https://<backend-url>/agents/rca` |
| `orbit-fixer` | `<ns>/autofix/orbit/fixer` | `https://<backend-url>/agents/fixer` |
| `orbit-verifier` | `<ns>/autofix/orbit/verifier` | `https://<backend-url>/agents/verifier` |

Webhook settings: **Method: POST**, **Content-Type: application/json**.

> SAP Event Mesh sends an OPTIONS preflight to each URL during subscription creation.
> The backend handles it automatically — no extra configuration needed.

---

## Step 4 — Initialize the HANA Schema

Connect to the client's HANA Cloud instance and create the schema:

```sql
CREATE SCHEMA "<HANA_SCHEMA>";
```

The application creates all required tables automatically on first startup
(`db/database.py` → `initialize_database()`). No manual DDL is needed beyond the schema.

---

## Step 5 — Prepare `manifest.yml`

Copy and edit `manifest.yml` for the client. Set the `name` to match the client's app name and bind the correct service instances.

```yaml
---
applications:
  - name: orbit-<client-name>-be
    memory: 1G
    buildpacks:
      - python_buildpack
    command: uv run uvicorn main:app --host 0.0.0.0 --port $PORT
    services:
      - <client-destination-service-instance>
    env:
      EVENT_MESH_DESTINATION_NAME: EventMesh
      CPI_POLL_INTERVAL_SECONDS: "600"
      # All secrets are set via cf set-env (see Step 6)
```

> The `services` list must include the Destination service instance bound to this subaccount.
> Check the instance name with `cf services`.

---

## Step 6 — Set Environment Variables

Copy `.env.example` to a scratch file. Fill in all values for this client.
Then set them on CF — **never commit secrets to the manifest**:

```bash
cf target -o <org> -s <space>
cf set-env orbit-<client-name>-be MCP_INTEGRATION_SUITE_URL "https://..."
cf set-env orbit-<client-name>-be MCP_TESTING_URL            "https://..."
cf set-env orbit-<client-name>-be MCP_DOCUMENTATION_URL      "https://..."

cf set-env orbit-<client-name>-be AICORE_CLIENT_ID     "..."
cf set-env orbit-<client-name>-be AICORE_CLIENT_SECRET  "..."
cf set-env orbit-<client-name>-be AICORE_AUTH_URL       "https://..."
cf set-env orbit-<client-name>-be AICORE_BASE_URL       "https://..."
cf set-env orbit-<client-name>-be LLM_DEPLOYMENT_ID     "..."

cf set-env orbit-<client-name>-be SAP_HUB_TENANT_URL    "https://..."
cf set-env orbit-<client-name>-be SAP_HUB_TOKEN_URL     "https://..."
cf set-env orbit-<client-name>-be SAP_HUB_CLIENT_ID     "..."
cf set-env orbit-<client-name>-be SAP_HUB_CLIENT_SECRET "..."

cf set-env orbit-<client-name>-be API_BASE_URL           "https://..."
cf set-env orbit-<client-name>-be API_OAUTH_CLIENT_ID    "..."
cf set-env orbit-<client-name>-be API_OAUTH_CLIENT_SECRET "..."
cf set-env orbit-<client-name>-be API_OAUTH_TOKEN_URL    "https://..."

cf set-env orbit-<client-name>-be CPI_BASE_URL           "https://..."
cf set-env orbit-<client-name>-be CPI_OAUTH_CLIENT_ID    "..."
cf set-env orbit-<client-name>-be CPI_OAUTH_CLIENT_SECRET "..."
cf set-env orbit-<client-name>-be CPI_OAUTH_TOKEN_URL    "https://..."

cf set-env orbit-<client-name>-be HANA_HOST     "<uuid>.hna0.prod-<region>.hanacloud.ondemand.com"
cf set-env orbit-<client-name>-be HANA_PORT     "443"
cf set-env orbit-<client-name>-be HANA_USER     "..."
cf set-env orbit-<client-name>-be HANA_PASSWORD "..."
cf set-env orbit-<client-name>-be HANA_SCHEMA   "..."

cf set-env orbit-<client-name>-be BUCKET_NAME            "..."
cf set-env orbit-<client-name>-be REGION                 "us-east-1"
cf set-env orbit-<client-name>-be ENDPOINT_URL           "https://s3.amazonaws.com"
cf set-env orbit-<client-name>-be OBJECT_STORE_ACCESS_KEY "..."
cf set-env orbit-<client-name>-be OBJECT_STORE_SECRET_KEY "..."

# Event Mesh
cf set-env orbit-<client-name>-be EM_ENABLED    "true"
cf set-env orbit-<client-name>-be EM_REST_URL   "https://enterprise-messaging-pubsub.cfapps.<region>.hana.ondemand.com"
cf set-env orbit-<client-name>-be EM_QUEUE_PREFIX "default/<client-namespace>/1/autofix/orbit"
cf set-env orbit-<client-name>-be EM_INGEST_TOPIC "default/<client-namespace>/1/autofix/in"

# Pipeline behaviour
cf set-env orbit-<client-name>-be AUTO_FIX_ALL_CPI_ERRORS "false"
cf set-env orbit-<client-name>-be AUTO_DEPLOY_AFTER_FIX   "true"
cf set-env orbit-<client-name>-be AUTO_FIX_CONFIDENCE     "0.90"
cf set-env orbit-<client-name>-be SUGGEST_FIX_CONFIDENCE  "0.70"

# ITSM
cf set-env orbit-<client-name>-be ITSM_REQUESTER_ID "<client-sap-user-uuid>"
cf set-env orbit-<client-name>-be CPI_ENVIRONMENT   "production"
```

---

## Step 7 — Push the App

```bash
cf push orbit-<client-name>-be -f manifest.yml
```

The app will:
1. Start the FastAPI server on `$PORT`
2. Initialize HANA tables (first run only)
3. Start the CPI Monitor background poller
4. Load MCP tool servers (takes ~30–60 s on first boot)

Watch startup logs:
```bash
cf logs orbit-<client-name>-be --recent
```

Expected output on healthy startup:
```
[CPI_MONITOR] Poll interval set to 600s
[CPI_MONITOR] Poller started, interval=600s
[MCP] integration_suite: connected
[MCP] mcp_testing: connected
[MCP] documentation_mcp: connected
```

---

## Step 8 — Initial Load (First Deployment Only)

After the app is running, backfill all existing CPI failures so the pipeline processes them.
Without this step the agent only sees new errors going forward.

```bash
# Preview what would be published (no messages sent)
cf run-task orbit-<client-name>-be \
  "uv run python scripts/initial_load.py --dry-run" \
  --name initial-load-preview

# Publish ALL failures with no time restriction
cf run-task orbit-<client-name>-be \
  "uv run python scripts/initial_load.py" \
  --name initial-load

# Watch the task logs
cf logs orbit-<client-name>-be --recent
```

Flags:

| Flag | Default | Effect |
|---|---|---|
| *(none)* | **all time** | Fetch every FAILED message in the tenant — no date filter |
| `--days-back N` | — | Narrow to the last N days only (optional, not recommended for first load) |
| `--limit N` | no cap | Stop after N messages (smoke-test only) |
| `--dry-run` | off | Print what would be published; send nothing |

The script paginates through CPI OData (`$top=100 $skip=N`) until **every** FAILED message is collected, fetches the full error detail for each, and publishes to `EM_INGEST_TOPIC`. The orchestrator webhook picks up each message and runs the full 5-stage pipeline.

> Run this script only once per client. After the initial load, the CPI Monitor polling loop (`CPI_POLL_INTERVAL_SECONDS`) handles all new failures automatically.

---

## Step 9 — Verify the Deployment

### 9a — Health check
```
GET https://<backend-url>/
```
Expected: `{"status": "running", "service": "CPI MCP Servers + Autonomous Ops", "version": "4.0.0"}`

### 9b — Event Mesh connectivity
```
GET https://<backend-url>/event-mesh/status
```
Expected:
```json
{
  "EM_ENABLED": true,
  "receiver_connected": true,
  "queue_depth": 0
}
```

### 9c — Pipeline smoke test

Inject a synthetic incident and watch it flow through all 5 stages:
```
POST https://<backend-url>/autonomous/test_incident
```
Then poll:
```
GET https://<backend-url>/autonomous/incidents/<incident_id>
```
Status should progress:
```
CLASSIFIED → OBSERVED → RCA_IN_PROGRESS → RCA_COMPLETE → FIX_IN_PROGRESS → FIX_VERIFIED
```

### 9d — Debug endpoints (if something is wrong)

| Endpoint | Checks |
|---|---|
| `GET /autonomous/debug` | Env var presence, CPI message fetch test |
| `GET /autonomous/debug2` | OAuth token + CPI API connectivity probe |
| `GET /cpi-monitor/status` | CPI Monitor config and dedup-cache state |
| `GET /autonomous/db_test` | HANA read/write round-trip |

---

## Common Issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `EM_QUEUE_PREFIX is not set` on startup | `EM_QUEUE_PREFIX` env var missing | `cf set-env ... EM_QUEUE_PREFIX "default/<ns>/1/autofix/orbit"` |
| `/event-mesh/status` shows `receiver_connected: false` | Destination `EventMesh` misconfigured or service not bound | Re-check Step 2 and `manifest.yml services` |
| Webhook OPTIONS failing in Event Mesh UI | App not yet deployed or wrong URL | Push app first, then create webhooks |
| `[MCP] integration_suite: connect failed` | Wrong `MCP_INTEGRATION_SUITE_URL` | Verify the MCP app CF route and re-set the env var |
| HANA `schema not found` error | Schema not created | Run `CREATE SCHEMA "<HANA_SCHEMA>"` in HANA |
| Incidents stay at `CLASSIFIED` | Observer webhook not triggering | Verify the `orbit-observer` webhook subscription in Event Mesh UI |
| `EventMesh destination token resolution FAILED` | Destination service not bound or Destination misconfigured | Check `cf services` and re-check Step 2 |
| `initial_load.py` — `EM_INGEST_TOPIC is not set` | Missing env var | `cf set-env ... EM_INGEST_TOPIC "default/<ns>/1/autofix/in"`, then re-run the task |
| `initial_load.py` — messages published but no incidents created | Orchestrator webhook not active | Verify the `orbit-orchestrator` webhook subscription in Event Mesh UI |
| `initial_load.py` — `HTTP 401` when publishing | Destination token expired or wrong destination name | Re-check `EVENT_MESH_DESTINATION_NAME` and Destination config in Step 2 |

---

## Environment Variable Reference

Complete list of every variable the application reads, what it does, and which component uses it.
Variables marked **Required** will cause a startup crash or silent failure if missing.

### MCP Servers

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `MCP_INTEGRATION_SUITE_URL` | ✅ Required | `core/constants.py` | CF route of the Integration Suite MCP app — iFlow get / update / deploy |
| `MCP_TESTING_URL` | ✅ Required | `core/constants.py` | CF route of the Testing MCP app — test execution and validation |
| `MCP_DOCUMENTATION_URL` | ✅ Required | `core/constants.py` | CF route of the Documentation MCP app — SAP docs and spec generation |

### SAP AI Core

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `AICORE_CLIENT_ID` | ✅ Required | `cpi_monitor/cpi_poller.py`, `core/mcp_manager.py` | OAuth client ID for AI Core service |
| `AICORE_CLIENT_SECRET` | ✅ Required | `cpi_monitor/cpi_poller.py`, `core/mcp_manager.py` | OAuth client secret for AI Core service |
| `AICORE_AUTH_URL` | ✅ Required | `cpi_monitor/cpi_poller.py`, `core/mcp_manager.py` | Token URL for AI Core OAuth (`https://<subdomain>.authentication.<region>.hana.ondemand.com`) |
| `AICORE_BASE_URL` | ✅ Required | `cpi_monitor/cpi_poller.py`, `core/mcp_manager.py` | AI Core API base URL |
| `AICORE_RESOURCE_GROUP` | Optional | `core/mcp_manager.py` | AI Core resource group (default: `default`) |
| `LLM_DEPLOYMENT_ID` | ✅ Required | `core/mcp_manager.py` | Default LLM deployment ID — used by all agents unless overridden |
| `LLM_DEPLOYMENT_ID_RCA` | Optional | `agents/rca_agent.py`, `agents/classifier_agent.py` | LLM deployment for RCA and classification — falls back to `LLM_DEPLOYMENT_ID` |
| `LLM_DEPLOYMENT_ID_FIX` | Optional | `agents/fix_planner.py`, `agents/fix_generator.py` | LLM deployment for fix generation — falls back to `LLM_DEPLOYMENT_ID` |

### SAP Integration Suite — Design-Time API

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `API_BASE_URL` | ✅ Required | `cpi_monitor/cpi_poller.py` (fallback) | CPI design-time base URL (`/api/v1`) — iFlow get, update, deploy |
| `API_OAUTH_CLIENT_ID` | ✅ Required | `cpi_monitor/cpi_poller.py` (fallback) | OAuth client ID for design-time API |
| `API_OAUTH_CLIENT_SECRET` | ✅ Required | `cpi_monitor/cpi_poller.py` (fallback) | OAuth client secret for design-time API |
| `API_OAUTH_TOKEN_URL` | ✅ Required | `cpi_monitor/cpi_poller.py` (fallback) | Token URL for design-time API OAuth |

### SAP Integration Suite — Runtime / Monitoring API

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `CPI_BASE_URL` | ✅ Required | Observer, Fix agents | CPI runtime base URL — message logs, error details |
| `CPI_OAUTH_CLIENT_ID` | ✅ Required | Observer, Fix agents | OAuth client ID for runtime API |
| `CPI_OAUTH_CLIENT_SECRET` | ✅ Required | Observer, Fix agents | OAuth client secret for runtime API |
| `CPI_OAUTH_TOKEN_URL` | ✅ Required | Observer, Fix agents | Token URL for runtime API OAuth |

### SAP Hub — CPI Error Polling

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `SAP_HUB_TENANT_URL` | ✅ Required | `cpi_monitor/cpi_poller.py`, `agents/observer_agent.py`, `main.py` | CPI tenant URL used by the poller to fetch failed messages |
| `SAP_HUB_TOKEN_URL` | ✅ Required | `cpi_monitor/cpi_poller.py`, `agents/observer_agent.py` | Token URL for CPI Hub OAuth |
| `SAP_HUB_CLIENT_ID` | ✅ Required | `cpi_monitor/cpi_poller.py`, `agents/observer_agent.py` | OAuth client ID for CPI Hub |
| `SAP_HUB_CLIENT_SECRET` | ✅ Required | `cpi_monitor/cpi_poller.py`, `agents/observer_agent.py` | OAuth client secret for CPI Hub |

### SAP HANA Cloud

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `HANA_HOST` | ✅ Required | `db/database.py`, `config/config.py` | HANA Cloud hostname |
| `HANA_ADDRESS` | Optional | `utils/vector_store.py`, `vectorize_docs.py` | HANA hostname alias — falls back to `HANA_HOST` if not set |
| `HANA_PORT` | Optional | `db/database.py` | HANA port (default: `443`) |
| `HANA_USER` | ✅ Required | `db/database.py` | HANA runtime user |
| `HANA_PASSWORD` | ✅ Required | `db/database.py` | HANA runtime user password |
| `HANA_SCHEMA` | ✅ Required | `db/database.py` | HANA schema name |
| `HANA_TABLE_QUERY_HISTORY` | Optional | `config/config.py` | Table for MCP query history (default: `MCP_QUERY_HISTORY`) |
| `HANA_TABLE_USER_FILES` | Optional | `config/config.py` | Table for uploaded file metadata (default: `USER_FILES_METADATA`) |
| `HANA_TABLE_XSD_FILES` | Optional | `config/config.py` | Table for XSD schema files (default: `SAP_IS_XSD_FILES`) |
| `HANA_TABLE_VECTOR` | Optional | `utils/vector_store.py` | Table for SAP Notes vector embeddings (default: `SAP_HELP_DOCS`) |
| `HANA_TABLE_SAP_DOCS` | Optional | `vectorize_docs.py`, `scrape_sap_docs.py` | Table used by scrape/vectorize scripts (default: `SAP_HELP_DOCS`) |
| `HANA_TABLE_EM_INCIDENTS` | Optional | `db/database.py` | Incident store table (default: `EM_AUTONOMOUS_INCIDENTS`) |
| `HANA_TABLE_EM_FIX_PATTERNS` | Optional | `db/database.py` | Fix pattern store table (default: `EM_FIX_PATTERNS`) |
| `HANA_TABLE_EM_ESCALATION_TICKETS` | Optional | `db/database.py` | Escalation ticket table (default: `EM_ESCALATION_TICKETS`) |

### AWS S3 Object Store

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `BUCKET_NAME` | ✅ Required | `storage/object_store.py` | S3 bucket name |
| `REGION` | Optional | `storage/object_store.py` | AWS region (default: `us-east-1`) |
| `HOST` | Optional | `storage/object_store.py` | S3 hostname used to build endpoint URL (default: `s3.amazonaws.com`) |
| `WRITE_ACCESS_KEY_ID` | ✅ Required | `storage/object_store.py` | AWS access key for write (upload) operations |
| `WRITE_SECRET_ACCESS_KEY` | ✅ Required | `storage/object_store.py` | AWS secret key for write operations |
| `READ_ACCESS_KEY_ID` | ✅ Required | `storage/object_store.py` | AWS access key for read (download/list) operations |
| `READ_SECRET_ACCESS_KEY` | ✅ Required | `storage/object_store.py` | AWS secret key for read operations |

> **Note:** `OBJECT_STORE_ACCESS_KEY`, `OBJECT_STORE_SECRET_KEY`, `ENDPOINT_URL`, and `OBJECT_STORE_ENDPOINT` are **not read by any code** — do not rely on them.

### SAP Event Mesh

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `EM_ENABLED` | ✅ Required | `event_mesh/event_bus.py`, `core/constants.py` | Master switch — `true` enables real Event Mesh; `false` = in-process only |
| `EM_REST_URL` | ✅ Required | `event_mesh/event_bus.py`, `cpi_monitor/error_publisher.py` | Event Mesh REST endpoint base URL (from service key, `httprest` protocol) |
| `EM_QUEUE_PREFIX` | ✅ Required | `event_mesh/event_bus.py` | Topic namespace prefix — all agent-to-agent topics are built from this. **App crashes if missing.** Example: `default/acme.corp/1/autofix/orbit` |
| `EM_INGEST_TOPIC` | ✅ Required | `cpi_monitor/error_publisher.py`, `scripts/initial_load.py` | Topic the CPI Monitor publishes new errors to. Example: `default/acme.corp/1/autofix/in` |
| `EVENT_MESH_DESTINATION_NAME` | Optional | `event_mesh/event_bus.py`, `cpi_monitor/error_publisher.py` | BTP Destination name holding the Event Mesh OAuth token (default: `EventMesh`) |
| `EVENT_MESH_QUEUE` | Optional | `main.py` | Orchestrator queue name — used only in `/event-mesh/status` display |

### Pipeline Behaviour

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `CPI_POLL_INTERVAL_SECONDS` | Optional | `cpi_monitor/cpi_poller.py` | Seconds between CPI failed-message polls (default: `600`) |
| `AUTO_FIX_ALL_CPI_ERRORS` | Optional | `core/constants.py`, `agents/orchestrator_agent.py` | `true` = auto-fix all error types; `false` = respect per-type policy (default: `true`) |
| `AUTO_FIX_CONFIDENCE` | Optional | `core/constants.py`, `agents/orchestrator_agent.py` | Minimum confidence score for fully automatic fix without approval (default: `0.90`) |
| `SUGGEST_FIX_CONFIDENCE` | Optional | `core/constants.py`, `agents/orchestrator_agent.py` | Minimum confidence for suggesting a fix; below this creates a ticket (default: `0.70`) |
| `AUTO_DEPLOY_AFTER_FIX` | Optional | `core/constants.py`, `main.py` | `true` = automatically deploy the iFlow after a successful fix update (default: `true`) |
| `MAX_CONSECUTIVE_FAILURES` | Optional | `core/constants.py`, `agents/orchestrator_agent.py` | Consecutive failures for the same iFlow before auto-escalating to a ticket (default: `5`) |
| `PENDING_APPROVAL_TIMEOUT_HRS` | Optional | `core/constants.py`, `agents/observer_agent.py` | Hours an incident can stay `AWAITING_APPROVAL` before auto-escalating (default: `24`) |
| `PATTERN_MIN_SUCCESS_COUNT` | Optional | `core/constants.py`, `agents/orchestrator_agent.py` | Minimum successful fixes a pattern needs before it is reused (default: `2`) |
| `BURST_DEDUP_WINDOW_SECONDS` | Optional | `core/constants.py`, `agents/orchestrator_agent.py` | Window in seconds to absorb duplicate errors for the same iFlow (default: `60`) |
| `WEB_SEARCH_ENABLED` | Optional | `agents/rca_agent.py`, `agents/fix_generator.py` | `true` = enable web search tool during RCA and fix generation (default: `false`) |

### ITSM Integration

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `ITSM_REQUESTER_ID` | ✅ Required | `agents/orchestrator_agent.py`, `integrations/itsm_client.py`, `main.py` | SAP user UUID stamped as `requester_ID` on all auto-created escalation tickets |

### Server & Logging

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `ENABLE_CONSOLE_LOGS` | Optional | `utils/logger_config.py` | `true` = write logs to console in addition to rotating file (default: `true`) |
| `UPLOAD_ROOT` | Optional | `config/config.py`, `storage/storage.py` | Root prefix for file uploads in S3 (default: `user`) |

> **Note:** `LOG_LEVEL`, `API_HOST`, and `API_PORT` are present in `.env.example` for reference but are **not currently read by any code path** in the running application.

### Variables in Manifest That Are Not Read by the Code

These variables appear in some deployments but have **no effect** — the code does not call `os.getenv` for them:

| Variable | Notes |
|---|---|
| `CPI_BASIC_AUTH_USERNAME` / `CPI_BASIC_AUTH_PASSWORD` / `CPI_AUTH_METHOD` | No basic-auth code path exists — all CPI auth uses OAuth |
| `SAP_DESIGN_TIME_URL` / `SAP_DESIGN_TIME_TOKEN_URL` / `SAP_DESIGN_TIME_CLIENT_ID` / `SAP_DESIGN_TIME_CLIENT_SECRET` | Design-time auth uses `API_OAUTH_*` vars — these are duplicates with a different name that the code never reads |
| `OBJECT_STORE_ACCESS_KEY` / `OBJECT_STORE_SECRET_KEY` | Code reads `WRITE_ACCESS_KEY_ID` and `READ_ACCESS_KEY_ID` instead |
| `ENDPOINT_URL` / `OBJECT_STORE_ENDPOINT` | Code builds the endpoint from `HOST` |
| `USE_REAL_FIXES` / `FAILED_MESSAGES_PAGE_SIZE` / `FAILED_MESSAGES_MAX_TOTAL` | No `os.getenv` for these anywhere in the codebase |
| `AEM_HOST` | Not read — Event Mesh endpoint comes from `EM_REST_URL` |
| `WEB_SEARCH_EcfNABLED` | Typo — code reads `WEB_SEARCH_ENABLED` |

---

## Checklist

- [ ] Step 1 — Three MCP servers deployed and URLs noted
- [ ] Step 2 — BTP Destination `EventMesh` created
- [ ] Step 3 — Event Mesh: 5 queues with correct topic subscriptions
- [ ] Step 3 — Event Mesh: 5 webhook subscriptions pointing to the backend URL
- [ ] Step 4 — HANA schema created
- [ ] Step 5 — `manifest.yml` updated with client app name and Destination service instance
- [ ] Step 6 — All env vars set via `cf set-env`
- [ ] Step 7 — `cf push` succeeded, startup logs clean
- [ ] Step 8 — Initial load: `--dry-run` previewed, then full run completed (no `--days-back`)
- [ ] Step 9a — `GET /` returns `running`
- [ ] Step 9b — `GET /event-mesh/status` shows `receiver_connected: true`
- [ ] Step 9c — `POST /autonomous/test_incident` reaches `FIX_VERIFIED`
