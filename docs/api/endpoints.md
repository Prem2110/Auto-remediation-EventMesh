# Core API Endpoints

Base URL: `http://<host>:8080`

Interactive docs: `/docs` (Swagger UI), `/redoc` (ReDoc)

---

## General Query

### `POST /query`

Natural-language interface. Detects "fix" intent and routes to the full fix pipeline. Otherwise delegates to a chatbot agent with all tools.

**Request:**

```json
{
  "query": "Why is iflow OrderProcessing failing?",
  "id": "optional-incident-id",
  "user_id": "user123"
}
```

**Response:**

```json
{
  "response": "The iflow is failing due to...",
  "id": "incident-uuid",
  "error": null
}
```

---

## Direct Fix

### `POST /fix`

Trigger a fix for a specific incident by ID or message GUID.

**Request:**

```json
{
  "query": "Fix incident abc123",
  "id": "abc123",
  "user_id": "user123"
}
```

### `POST /execute-fix`

Direct fix with explicit parameters (no RCA needed if proposed fix is provided).

**Request:**

```json
{
  "iflow_id": "OrderProcessing",
  "error_message": "XPath evaluation failed: ...",
  "proposed_fix": "Update XPath expression in MessageMapping_1",
  "user_id": "user123"
}
```

---

## Fix Progress

### `GET /fix-progress/{incident_id}`

Poll real-time fix progress from in-memory `FIX_PROGRESS` state.

**Response:**

```json
{
  "incident_id": "abc123",
  "step": "DEPLOYING",
  "message": "Deploying updated iFlow to SAP CPI...",
  "pct": 75,
  "status": "in_progress"
}
```

Progress percentages:

| Step | % |
|---|---|
| RCA_RUNNING | 20 |
| FIX_GENERATING | 40 |
| UPDATING | 60 |
| DEPLOYING | 75 |
| VERIFYING | 90 |
| COMPLETE | 100 |

---

## Autonomous Mode

### `GET /autonomous/status`

```json
{
  "autonomous_enabled": true,
  "poll_interval_seconds": 120,
  "auto_fix_confidence": 0.9,
  "loop_running": true
}
```

### `POST /autonomous/enable`

Enable autonomous polling and auto-fix. Persisted to `runtime_config.json`.

### `POST /autonomous/disable`

Disable autonomous loop without restarting.

### `POST /autonomous/reset`

Reset to the value specified in `.env`.

---

## Incident Management

### `GET /incidents`

List all incidents with optional filters.

**Query params:** `status`, `error_type`, `iflow_id`, `limit`, `offset`

### `GET /incidents/{incident_id}`

Get full incident detail including RCA result, fix steps, and verification status.

### `POST /incidents/{incident_id}/approve`

Approve an `AWAITING_APPROVAL` incident for auto-fix.

**Request:**

```json
{
  "approved": true,
  "comment": "Reviewed and approved"
}
```

### `POST /incidents/bulk-approve`

Approve multiple incidents at once.

**Request:**

```json
{
  "incident_ids": ["abc123", "def456"],
  "approved": true,
  "user_id": "admin"
}
```

---

## AEM Webhook

### `POST /aem/events`

Receive incoming AEM events. Used when AEM is configured to push events back to this service.

**Request:** AEM event JSON payload (topic-specific structure)

---

## Health

### `GET /health`

```json
{
  "status": "ok",
  "mcp_connected": true,
  "db_connected": true
}
```

---

## Metrics

### `GET /metrics`

Prometheus-format metrics (if `prometheus-client` is installed and configured).
