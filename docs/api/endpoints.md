# Core API Endpoints

**Base URL:** `http://<host>:8080`  
**Interactive docs:** `/docs` (Swagger UI), `/redoc` (ReDoc)

---

## Root

### `GET /`

Health check. Returns service name, version, and status.

---

## Query & Fix

### `POST /query`

Natural-language interface. Detects "fix" intent and routes to the full fix pipeline; otherwise delegates to a chatbot agent with all tools.

**Request:**
```json
{ "query": "Why is iflow OrderProcessing failing?", "id": "optional-incident-id", "user_id": "user123" }
```

**Response:**
```json
{ "response": "The iflow is failing due to...", "id": "ORBCPI-20260520-000001", "error": null }
```

### `POST /fix`

Trigger a fix for a specific incident by ID or message GUID.

**Request:**
```json
{ "query": "Fix incident ORBCPI-20260520-000001", "id": "ORBCPI-20260520-000001", "user_id": "user123" }
```

---

## History & Logs

### `GET /get_all_history`

Return all past fix history records from the database.

### `GET /get_testsuite_logs`

Return stored test-suite execution logs.

---

## Auto-Fix Config

### `GET /api/config/auto-fix`

Return the current auto-fix runtime configuration.

### `POST /api/config/auto-fix`

Update auto-fix runtime configuration fields.

### `POST /api/config/auto-fix/reset`

Reset auto-fix configuration to defaults.

---

## Settings

### `GET /settings`

Return all runtime settings as a flat key-value map.

### `PATCH /settings`

Update one or more settings keys.

### `DELETE /settings/{key}`

Delete a single settings key.

---

## Autonomous Mode

### `GET /autonomous/status`

Return the current state of the autonomous loop.

```json
{ "autonomous_enabled": true, "poll_interval_seconds": 120, "loop_running": true }
```

### `POST /autonomous/start`

Start the autonomous polling loop.

### `POST /autonomous/stop`

Stop the autonomous polling loop.

### `GET /autonomous/auto-fix`

Return the current auto-fix toggle state.

### `POST /autonomous/auto-fix/toggle`

Toggle auto-fix on or off.

### `GET /autonomous/tools`

List all MCP tools available to the autonomous agents.

### `GET /autonomous/db_test`

Smoke-test the database connection.

### `GET /autonomous/debug`

Return internal agent debug state (step 1).

### `GET /autonomous/debug2`

Return internal agent debug state (step 2).

---

## Autonomous — CPI Errors

### `GET /autonomous/cpi/errors`

Fetch current FAILED messages from SAP CPI.

### `GET /autonomous/cpi/messages/errors`

Alias for `/autonomous/cpi/errors` with message-level detail.

### `GET /autonomous/cpi/runtime_artifacts/errors`

Fetch runtime artifact errors from SAP CPI.

### `GET /autonomous/cpi/runtime_artifacts/{artifact_id}`

Fetch detail for a specific runtime artifact by ID.

---

## Autonomous — Incidents

### `GET /autonomous/incidents`

List all incidents. Query params: `status`, `limit`, `offset`.

### `GET /autonomous/incidents/{incident_id}`

Get full detail for a single incident.

### `GET /autonomous/incidents/{incident_id}/view_model`

Get the UI view model for an incident (structured for the frontend).

### `GET /autonomous/incidents/{incident_id}/fix_progress`

Poll real-time fix progress from in-memory state.

```json
{ "incident_id": "ORBCPI-20260520-000001", "step": "DEPLOYING", "pct": 75, "status": "in_progress" }
```

### `POST /autonomous/incidents/{incident_id}/approve`

Approve or reject an `AWAITING_APPROVAL` incident.

**Request:**
```json
{ "approved": true, "comment": "Reviewed and approved", "user_id": "user123" }
```

### `POST /autonomous/incidents/{incident_id}/generate_fix`

Generate a fix plan for an incident without applying it.

### `POST /autonomous/incidents/{incident_id}/retry_rca`

Re-run RCA for an incident that previously failed analysis.

### `GET /autonomous/incidents/{incident_id}/fix_patterns`

Return historical fix patterns matched to this incident's error type.

### `GET /autonomous/pending_approvals`

List all incidents in `AWAITING_APPROVAL` or `AWAITING_HUMAN_REVIEW` state.

### `POST /autonomous/test_incident`

Inject a synthetic test incident into the pipeline (dev/QA use).

---

## Autonomous — Tickets

### `GET /autonomous/tickets`

List all escalation tickets. Query params: `status`, `limit`.

### `PATCH /autonomous/tickets/{ticket_id}`

Update fields on an escalation ticket.

### `POST /autonomous/tickets/{ticket_id}/retry-itsm`

Retry pushing a failed ITSM ticket creation.

---

## Event Mesh

### `GET /event-mesh/status`

Return Event Mesh connectivity and queue status.

### `POST /event-mesh/events`

Receive incoming SAP Event Mesh webhook events. Accepts the topic-specific JSON payload pushed by SAP AEM.

---

## Direct Agent Invocation

These endpoints invoke individual agents directly, bypassing the orchestration pipeline. Intended for debugging and testing.

### `POST /agents/orchestrator`

Run the Orchestrator agent for a given incident.

### `POST /agents/observer`

Run the Observer agent to classify a raw CPI error.

### `POST /agents/rca`

Run the RCA agent against an incident.

### `POST /agents/fixer`

Run the Fixer agent to generate and apply a fix.

### `POST /agents/verifier`

Run the Verifier agent to check a deployed fix.

---

## CPI Monitor

### `GET /cpi-monitor/status`

Return CPI monitor poll state (last run, next run, errors found).

### `POST /cpi-monitor/trigger`

Manually trigger a CPI monitor poll cycle.

---

## ITSM

### `GET /itsm/status`

Return ITSM integration configuration and connectivity status.

### `GET /itsm/verify`

Verify ITSM credentials and connectivity end-to-end.

### `GET /itsm/tickets`

List all ITSM tickets created by the system. Query params: `status`, `limit`.

### `POST /itsm/test-ticket`

Create a test ITSM ticket to verify the integration is working.

### `POST /itsm/poll`

Manually trigger an ITSM sync poll.
