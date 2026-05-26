# Smart Monitoring API

**Router prefix:** `/smart-monitoring`  
**File:** `smart_monitoring.py`

The Smart Monitoring API provides a fine-grained view into SAP CPI failed messages, with AI-powered analysis, fix, and escalation capabilities per message.

---

## Messages

### `GET /smart-monitoring/messages`

List failed CPI messages with optional filtering.

**Query params:** `status`, `iflow_id`, `time_range`, `limit`, `offset`

### `GET /smart-monitoring/messages/paginated`

Paginated list of failed messages with cursor-based navigation.

**Query params:** `page`, `page_size`, `status`, `iflow_id`, `time_range`

### `GET /smart-monitoring/messages/{message_guid}`

Full detail view for a single failed message. Returns tabbed data:

| Tab | Content |
|---|---|
| `overview` | Basic message info, status, timestamps |
| `error` | Full error message and stack |
| `logs` | Processing log entries |
| `payload` | Message payload (if available) |
| `iflow` | Current iFlow configuration summary |
| `history` | Previous incidents for the same iFlow |

### `GET /smart-monitoring/total-errors`

```json
{ "total": 142 }
```

---

## AI Analysis

### `POST /smart-monitoring/messages/{message_guid}/analyze`

Run RCA on a specific failed message. Triggers `RCAAgent.run_rca()`.

**Request:**
```json
{ "user_id": "user123" }
```

**Response:**
```json
{
  "root_cause": "XPath expression references field 'OrderId' but schema defines 'orderId'",
  "proposed_fix": "Update XPath in MessageMapping_1 to use 'orderId'",
  "confidence": 0.92,
  "affected_component": "MessageMapping_1"
}
```

### `POST /smart-monitoring/messages/{message_guid}/explain_error`

Return a plain-English explanation of the error for a given message — no fix generated.

**Request:**
```json
{ "user_id": "user123" }
```

### `POST /smart-monitoring/messages/{message_guid}/generate_fix_patch`

Generate a detailed fix plan without applying it. Returns step-by-step instructions and expected XML change.

**Request:**
```json
{ "user_id": "user123" }
```

### `POST /smart-monitoring/chat`

Ask the AI a free-form question about a specific error or incident.

**Request:**
```json
{ "message_guid": "abc123", "query": "What does this error mean?", "user_id": "user123" }
```

---

## Fix Application

### `POST /smart-monitoring/messages/{message_guid}/apply_fix`

Apply a fix to the iFlow and deploy. Runs the full `OrchestratorAgent.execute_incident_fix()` pipeline.

**Request:**
```json
{ "user_id": "user123", "proposed_fix": "Optional override for RCA-proposed fix" }
```

**Response:**
```json
{
  "incident_id": "ORBCPI-20260520-000001",
  "fix_applied": true,
  "deploy_success": true,
  "summary": "Updated XPath expression in MessageMapping_1 and deployed successfully"
}
```

---

## Incidents

### `GET /smart-monitoring/incidents`

List all tracked incidents persisted in HANA.

**Query params:** `status`, `error_type`, `iflow_id`, `limit`, `offset`

### `GET /smart-monitoring/incidents/{incident_id}/fix_status`

Poll fix progress for an incident.

```json
{ "incident_id": "ORBCPI-20260520-000001", "status": "FIX_VERIFIED", "step": "COMPLETE", "pct": 100 }
```

### `POST /smart-monitoring/incidents/{incident_id}/retry_fix`

Retry a failed fix for an existing incident.

### `POST /smart-monitoring/incidents/{incident_id}/resolve`

Manually mark an incident as resolved.

**Request:**
```json
{ "resolution_note": "Fixed manually via iFlow editor", "user_id": "user123" }
```

### `POST /smart-monitoring/incidents/{incident_id}/rollback`

Roll back a deployed fix and restore the previous iFlow version.

---

## Statistics

### `GET /smart-monitoring/stats`

Summary statistics for the monitoring dashboard.

```json
{
  "total_failed": 142,
  "auto_fixed": 98,
  "awaiting_approval": 12,
  "fix_success_rate": 0.87
}
```

---

## Escalations

### `GET /smart-monitoring/escalations`

List escalation tickets. Query params: `status`, `incident_id`, `limit`.

### `GET /smart-monitoring/escalations/{ticket_id}`

Get detail for a single escalation ticket.

### `PATCH /smart-monitoring/escalations/{ticket_id}`

Update fields on an escalation ticket (e.g. status, resolution notes).

---

## Admin

### `DELETE /smart-monitoring/admin/clear-all-data`

Wipe all incidents and monitoring data from the database. **Destructive — dev/test environments only.**
