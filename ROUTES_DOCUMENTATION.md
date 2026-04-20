# API Routes Documentation

This document lists all HTTP API routes exposed by the SAP CPI Self-Healing Agent (`main.py`), including the mounted sub-routers from `smart_monitoring.py` and `smart_monitoring_dashboard.py`.

## Base Configuration

- **Default port**: `8080` (local dev) — overridden by `$PORT` on BTP Cloud Foundry
- **Host**: `0.0.0.0`
- **Frontend API base**: `/api` (configurable via `VITE_API_BASE`)
- **Primary CF URL**: configurable via `VITE_API_PRIMARY`

---

## Root

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Health check — returns `{"status": "running", "version": "4.0.0"}` |

---

## Chatbot (`/query`)

| Route | Method | Description |
|-------|--------|-------------|
| `/query` | POST | Conversational query endpoint. Accepts multipart form with optional file uploads. Detects fix intent and triggers FixAgent automatically. |

### Request (`multipart/form-data`)
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | User's natural-language question or instruction |
| `user_id` | string | Yes | User identifier |
| `id` | string | No | Session ID (generated if omitted) |
| `files` | file[] | No | XSD or attachment files for context |

### Response (`QueryResponse`)
```json
{
  "response": "AI answer text",
  "id": "session-uuid",
  "error": {}
}
```

---

## Direct Fix (`/fix`)

| Route | Method | Description |
|-------|--------|-------------|
| `/fix` | POST | Trigger RCA + fix + deploy for a given iFlow and error message. |

### Request Body
```json
{
  "iflow_id": "EH8-BPP-Material-UPSERT",
  "error_message": "MappingException: ...",
  "user_id": "user",
  "proposed_fix": "(optional) override fix description"
}
```

### Response
```json
{
  "iflow_id": "...",
  "fix_applied": true,
  "deploy_success": true,
  "success": true,
  "summary": "Fix applied and deployed successfully.",
  "rca_confidence": 0.87,
  "proposed_fix": "...",
  "steps_count": 3
}
```

---

## History & Test Logs

| Route | Method | Description |
|-------|--------|-------------|
| `/get_all_history` | GET | Fetch all query history records. Optional `?user_id=` filter. |
| `/get_testsuite_logs` | GET | Fetch test suite log entries. Optional `?user_id=` filter. |

---

## Autonomous Pipeline Control (`/autonomous`)

| Route | Method | Description |
|-------|--------|-------------|
| `/autonomous/start` | POST | Start the orchestrator autonomous loop |
| `/autonomous/stop` | POST | Stop the orchestrator autonomous loop |
| `/autonomous/status` | GET | Get pipeline running state and configuration |
| `/autonomous/tools` | GET | List all MCP tools loaded, grouped by server. Optional `?server=` filter. |
| `/autonomous/manual_trigger` | POST | Pull one message from the AEM queue and process it (background) |
| `/autonomous/test_incident` | POST | Inject a synthetic test incident and run the full pipeline (background) |

### `/autonomous/status` Response
```json
{
  "running": true,
  "poll_interval_seconds": 30,
  "auto_fix_confidence": 0.8,
  "suggest_fix_confidence": 0.6,
  "auto_fix_all": false,
  "auto_deploy": true
}
```

---

## Auto-Fix Runtime Configuration (`/api/config/auto-fix`)

| Route | Method | Description |
|-------|--------|-------------|
| `/api/config/auto-fix` | GET | Get current auto-fix enabled state and source (`env` or `runtime`) |
| `/api/config/auto-fix` | POST | Toggle auto-fix on/off at runtime. Query param: `?enabled=true` |
| `/api/config/auto-fix/reset` | POST | Reset auto-fix setting back to the `.env` value |

---

## SAP CPI Error Inventory (`/autonomous/cpi`)

| Route | Method | Description |
|-------|--------|-------------|
| `/autonomous/cpi/errors` | GET | Full CPI error inventory (messages + runtime artifacts). Params: `?message_limit=` `?artifact_limit=` |
| `/autonomous/cpi/messages/errors` | GET | Fetch failed messages from SAP CPI OData API, normalized. `?limit=` |
| `/autonomous/cpi/runtime_artifacts/errors` | GET | Fetch runtime artifact errors. `?limit=` |
| `/autonomous/cpi/runtime_artifacts/{artifact_id}` | GET | Detail + error info for a specific runtime artifact |

---

## Incidents CRUD (`/autonomous/incidents`)

| Route | Method | Description |
|-------|--------|-------------|
| `/autonomous/incidents` | GET | List incidents. Params: `?status=` `?limit=` (default 50) |
| `/autonomous/incidents/{incident_id}` | GET | Get a single incident by ID or message GUID |
| `/autonomous/incidents/{incident_id}/view_model` | GET | Build enriched view model for UI display |
| `/autonomous/incidents/{incident_id}/fix_progress` | GET | Poll live fix progress (from in-memory `FIX_PROGRESS` store) |
| `/autonomous/incidents/{incident_id}/approve` | POST | Approve or reject a pending fix |
| `/autonomous/incidents/{incident_id}/generate_fix` | POST | Re-trigger fix flow. `?sync=true` for synchronous execution |
| `/autonomous/incidents/{incident_id}/retry_rca` | POST | Re-run RCA for an incident |
| `/autonomous/incidents/{incident_id}/fix_patterns` | GET | Return similar fix patterns from knowledge base |

### Approve/Reject Body
```json
{
  "approved": true,
  "comment": "optional rejection reason"
}
```

---

## Approvals & Tickets

| Route | Method | Description |
|-------|--------|-------------|
| `/autonomous/pending_approvals` | GET | List all incidents in `AWAITING_APPROVAL` status |
| `/autonomous/tickets` | GET | List escalation tickets. Params: `?status=` `?limit=` |

---

## AEM (Advanced Event Mesh)

| Route | Method | Description |
|-------|--------|-------------|
| `/aem/status` | GET | AEM connectivity, SEMP queue depth, Solace counters, and pipeline stage counts |
| `/aem/events` | POST | Webhook entry point for Solace REST Delivery Point (RDP). Accepts raw event JSON and dispatches to orchestrator pipeline. |

### `/aem/status` Response
```json
{
  "aem_enabled": true,
  "receiver_connected": true,
  "queue_name": "sap.cpi.autofix.observer.out",
  "queue_depth": 3,
  "semp_error": null,
  "stage_counts": { "DETECTED": 2, "RCA_COMPLETE": 1 },
  "total_incidents": 42,
  "messages_retrieved": 10,
  "messages_published": 8,
  "messages_dropped": 0
}
```

---

## Debug Endpoints

| Route | Method | Description |
|-------|--------|-------------|
| `/autonomous/db_test` | GET | Create + fetch a test incident to verify HANA/SQLite connectivity |
| `/autonomous/debug` | GET | Dump env var presence, observer state, and a live CPI message fetch test |
| `/autonomous/debug2` | GET | Step-by-step OAuth token + CPI API connectivity probe |

---

## Smart Monitoring Router (`/smart-monitoring`)

Mounted from `smart_monitoring.py`.

| Route | Method | Description | Refresh |
|-------|--------|-------------|---------|
| `/smart-monitoring/messages` | GET | List all failed CPI messages. Params: `?time_range=` `?status=` `?iflow_name=` | 60s |
| `/smart-monitoring/messages/paginated` | GET | Paginated message list (no time_range). Params: `?page=` `?page_size=` | On-demand |
| `/smart-monitoring/messages/{guid}` | GET | Full detail view — all 6 tabs (Error, AI, Properties, Artifact, Attachments, History) | On-demand |
| `/smart-monitoring/messages/{guid}/analyze` | POST | Run AI RCA on a message | On-demand |
| `/smart-monitoring/messages/{guid}/explain_error` | POST | AI natural-language error explanation | On-demand |
| `/smart-monitoring/messages/{guid}/generate_fix_patch` | POST | Generate detailed multi-step fix plan | On-demand |
| `/smart-monitoring/messages/{guid}/apply_fix` | POST | Apply fix via MCP tools (get → update → deploy iFlow) | On-demand |
| `/smart-monitoring/chat` | POST | Chat with AI about a specific incident | On-demand |
| `/smart-monitoring/stats` | GET | Dashboard-level statistics | On-demand |
| `/smart-monitoring/incidents` | GET | List all incidents from DB | On-demand |
| `/smart-monitoring/incidents/{incident_id}/fix_status` | GET | Poll fix application status | 5s (during fix) |
| `/smart-monitoring/total-errors` | GET | Count of failed messages from SAP CPI | On-demand |

### Request Bodies

**analyze / explain_error / generate_fix_patch**:
```json
{ "user_id": "user" }
```

**apply_fix**:
```json
{
  "user_id": "user",
  "proposed_fix": "(optional) override fix text"
}
```

**chat**:
```json
{
  "query": "Why is this iFlow failing?",
  "user_id": "user",
  "message_guid": "(optional) pin to a specific message",
  "session_id": "(optional) continue a session"
}
```

---

## Dashboard Router (`/dashboard`)

Mounted from `smart_monitoring_dashboard.py`.

| Route | Method | Description | Refresh |
|-------|--------|-------------|---------|
| `/dashboard/kpi-cards` | GET | Top-level KPI cards (total incidents, auto-fix rate, avg resolution time, etc.) | 60s |
| `/dashboard/error-distribution` | GET | Error type breakdown for pie/donut chart | 60s |
| `/dashboard/status-distribution` | GET | Incident status breakdown (summary) | 60s |
| `/dashboard/status-breakdown` | GET | Detailed counts for all status values | 60s |
| `/dashboard/failures-over-time` | GET | Time-series failure data for line chart | 60s |
| `/dashboard/top-failing-iflows` | GET | Top noisy integrations for bar chart | 60s |
| `/dashboard/sender-receiver-stats` | GET | Sender / receiver failure counts | On-demand |
| `/dashboard/active-incidents-table` | GET | Real-time active incidents table | 60s |
| `/dashboard/recent-failures-table` | GET | Recent failed messages feed | 60s |
| `/dashboard/fix-progress-tracker` | GET | Operational fix progress widget | On-demand |
| `/dashboard/leaderboard/noisy-integrations` | GET | Most failing iFlows leaderboard | On-demand |
| `/dashboard/leaderboard/recurring-incidents` | GET | Most recurring incidents | On-demand |
| `/dashboard/leaderboard/longest-open` | GET | Longest unresolved incidents | On-demand |
| `/dashboard/drill-down/message/{guid}` | GET | Detailed drill-down for a specific message GUID | On-demand |
| `/dashboard/drill-down/incident/{id}` | GET | Detailed drill-down for an incident ID | On-demand |
| `/dashboard/drill-down/iflow/{name}` | GET | iFlow-specific analytics | On-demand |
| `/dashboard/health-metrics` | GET | System health indicators | On-demand |
| `/dashboard/sla-metrics` | GET | SLA compliance metrics | On-demand |
| `/dashboard/rca-coverage` | GET | AI RCA coverage statistics | On-demand |

---

## Common Data Structures

### Incident Object
```typescript
{
  incident_id:          string;
  message_guid:         string;
  iflow_id:             string;
  iflow_name?:          string;
  sender?:              string;
  receiver?:            string;
  status:               string;
  error_type:           string;
  error_message?:       string;
  root_cause?:          string;
  proposed_fix?:        string;
  rca_confidence?:      number;
  affected_component?:  string;
  fix_summary?:         string;
  occurrence_count?:    number;
  created_at:           string;
  updated_at?:          string;
  resolved_at?:         string;
}
```

### Pipeline Status (`/autonomous/status`)
```typescript
{
  running:                boolean;
  poll_interval_seconds:  number;
  auto_fix_confidence:    number;
  suggest_fix_confidence: number;
  auto_fix_all:           boolean;
  auto_deploy:            boolean;
}
```

### AEM Status (`/aem/status`)
```typescript
{
  aem_enabled:        boolean;
  receiver_connected: boolean;
  queue_name:         string;
  queue_depth:        number | null;
  semp_error:         string | null;
  stage_counts:       Record<string, number>;
  total_incidents:    number;
  messages_retrieved: number;
  messages_published: number;
  messages_dropped:   number;
}
```

---

## Incident Status Values

| Status | Meaning |
|--------|---------|
| `DETECTED` | New failure detected by observer |
| `CLASSIFIED` | Error type identified by classifier |
| `RCA_IN_PROGRESS` | Root cause analysis running |
| `RCA_COMPLETE` | Root cause identified |
| `RCA_FAILED` | Root cause analysis failed |
| `AWAITING_APPROVAL` | Fix suggested, waiting for human approval |
| `FIX_IN_PROGRESS` | Fix is being applied |
| `FIX_APPLIED_PENDING_VERIFICATION` | Fix deployed, awaiting test verification |
| `FIX_VERIFIED` | Fix confirmed by test suite |
| `AUTO_FIXED` | Successfully auto-remediated |
| `HUMAN_INITIATED_FIX` | Fix applied via `/fix` or chat |
| `FIX_FAILED` | Fix failed (general) |
| `FIX_FAILED_UPDATE` | Fix failed at iFlow update step |
| `FIX_FAILED_DEPLOY` | Fix failed at deploy step |
| `FIX_FAILED_RUNTIME` | Fix failed at runtime verification |
| `HUMAN_FIXED` | Manually resolved outside the pipeline |
| `PENDING_APPROVAL` | Awaiting manual approval (UI label) |
| `TICKET_CREATED` | Escalated to ticketing system |
| `PIPELINE_ERROR` | Internal pipeline error |
| `REJECTED` | Fix rejected by human reviewer |
| `RETRIED` | Message successfully retried |

---

## Notes

- All POST endpoints accept and return `application/json` unless noted as `multipart/form-data`.
- Routes that depend on agents return HTTP `503` with `"Agents not ready"` while startup MCP init is in progress (first ~30–60 s after boot).
- `incident_id` path parameters also accept `message_guid` — both are resolved by `_resolve_incident()`.
- Auto-refresh intervals listed are the defaults used by the React frontend (React Query).
- On BTP Cloud Foundry `$PORT` is injected at runtime; the app reads it automatically. Never hardcode port `8080` in frontend config for CF deployments.
