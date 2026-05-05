# Smart Monitoring API

**Router prefix:** `/smart-monitoring`  
**File:** `smart_monitoring.py`

The Smart Monitoring API provides a fine-grained view into SAP CPI failed messages, with AI-powered analysis and fix capabilities per message.

---

## Message Listing

### `GET /smart-monitoring/messages`

List failed CPI messages with optional filtering.

**Query params:**

| Param | Type | Description |
|---|---|---|
| `status` | string | Filter by message status |
| `iflow_id` | string | Filter by integration flow ID |
| `from_date` | string | ISO 8601 start date |
| `to_date` | string | ISO 8601 end date |
| `limit` | int | Page size (default 50) |
| `offset` | int | Pagination offset |

**Response:**

```json
{
  "messages": [
    {
      "message_guid": "abc123",
      "iflow_id": "OrderProcessing",
      "status": "FAILED",
      "error_message": "...",
      "timestamp": "2026-04-09T10:00:00Z"
    }
  ],
  "total": 142
}
```

### `GET /smart-monitoring/messages/{guid}`

Full detail view for a single failed message. Returns 6 tabs of information:

| Tab | Content |
|---|---|
| `overview` | Basic message info, status, timestamps |
| `error` | Full error message and stack |
| `logs` | Processing log entries |
| `payload` | Message payload (if available) |
| `iflow` | Current iFlow configuration summary |
| `history` | Previous incidents for same iFlow |

### `GET /smart-monitoring/total-errors`

```json
{ "total": 142 }
```

---

## AI Analysis

### `POST /smart-monitoring/messages/{guid}/analyze`

Run RCA on a specific failed message. Triggers `RCAAgent.run_rca()`.

**Request:**

```json
{
  "user_id": "user123"
}
```

**Response:**

```json
{
  "root_cause": "XPath expression references field 'OrderId' but schema defines 'orderId'",
  "proposed_fix": "Update XPath in MessageMapping_1 to use 'orderId'",
  "confidence": 0.92,
  "affected_component": "MessageMapping_1",
  "key_steps": ["..."]
}
```

### `POST /smart-monitoring/messages/{guid}/generate_fix_patch`

Generate a detailed fix plan without applying it. Returns step-by-step instructions and the expected XML change.

**Request:**

```json
{
  "user_id": "user123",
  "include_xml_diff": true
}
```

### `POST /smart-monitoring/chat`

Ask the AI a question about a specific error.

**Request:**

```json
{
  "message_guid": "abc123",
  "query": "What does this error mean?",
  "user_id": "user123"
}
```

---

## Fix Application

### `POST /smart-monitoring/messages/{guid}/apply_fix`

Apply a fix to the iFlow and deploy. Runs the full `OrchestratorAgent.execute_incident_fix()` pipeline.

**Request:**

```json
{
  "user_id": "user123",
  "proposed_fix": "Optional override for proposed fix from RCA"
}
```

**Response:**

```json
{
  "incident_id": "uuid",
  "fix_applied": true,
  "deploy_success": true,
  "summary": "Updated XPath expression in MessageMapping_1 and deployed successfully"
}
```

---

## Statistics

### `GET /smart-monitoring/stats`

Dashboard statistics.

**Response:**

```json
{
  "total_failed": 142,
  "auto_fixed": 98,
  "awaiting_approval": 12,
  "escalated": 8,
  "fix_success_rate": 0.87,
  "top_error_types": [
    {"type": "MAPPING_ERROR", "count": 45},
    {"type": "AUTH_ERROR", "count": 23}
  ]
}
```

---

## Incidents (via Smart Monitoring)

### `GET /smart-monitoring/incidents`

List all tracked incidents (persisted in HANA).

**Query params:** `status`, `error_type`, `iflow_id`, `limit`, `offset`

### `GET /smart-monitoring/incidents/{incident_id}/fix_status`

Poll fix progress for an incident.

```json
{
  "incident_id": "uuid",
  "status": "FIX_VERIFIED",
  "step": "COMPLETE",
  "pct": 100
}
```

---

## MCP Access Pattern

`smart_monitoring.py` uses a lazy import of `main_v2` to access the initialized `MultiMCP` and agents:

```python
# Avoids circular import at module load time
from main_v2 import multi_mcp, orchestrator  # imported at request time inside endpoint
```

This is intentional and necessary due to Python module load order constraints.
