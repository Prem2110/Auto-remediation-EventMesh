# Autonomous Loop

The autonomous loop runs in the background and continuously monitors SAP Integration Suite for failed messages.

---

## How It Works

```mermaid
sequenceDiagram
    participant Loop as ObserverAgent (Loop)
    participant SAP as SAP Integration Suite (OData)
    participant Orch as OrchestratorAgent
    participant HANA as SAP HANA

    loop Every POLL_INTERVAL_SECONDS
        Loop->>SAP: fetch_failed_messages(limit=50)
        SAP-->>Loop: [error1, error2, ...]
        loop For each unique error
            Loop->>Orch: process_detected_error(error)
            Orch->>HANA: create_incident()
            Orch->>Orch: classify → rca → gate
            alt AUTO_FIX
                Orch->>Orch: execute_incident_fix()
                Orch->>HANA: update_incident(FIX_VERIFIED)
            else TICKET
                Orch->>HANA: create_escalation_ticket()
            end
        end
    end
```

---

## Enabling the Loop

Set in `.env`:

```env
AUTONOMOUS_ENABLED=true
POLL_INTERVAL_SECONDS=120
```

The loop starts automatically during server startup (FastAPI lifespan). It can be toggled at runtime without restart:

```bash
curl -X POST http://localhost:8080/autonomous/enable
curl -X POST http://localhost:8080/autonomous/disable
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `AUTONOMOUS_ENABLED` | `false` | Master switch |
| `POLL_INTERVAL_SECONDS` | `120` | Seconds between polling cycles |
| `FAILED_MESSAGE_FETCH_LIMIT` | `50` | Max messages fetched per cycle |
| `MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE` | `10` | Max unique errors to process per cycle |
| `AUTO_FIX_CONFIDENCE` | `0.90` | Min confidence to auto-deploy a fix |
| `SUGGEST_FIX_CONFIDENCE` | `0.70` | Min confidence to suggest (not auto-deploy) |
| `AUTO_FIX_ALL_CPI_ERRORS` | `true` | Apply AUTO_FIX to all error types |
| `AUTO_DEPLOY_AFTER_FIX` | `true` | Always deploy after successful update |
| `BURST_DEDUP_WINDOW_SECONDS` | `60` | Cross-cycle dedup window |
| `MAX_CONSECUTIVE_FAILURES` | `3` | Escalate after N consecutive fix failures |
| `PENDING_APPROVAL_TIMEOUT_HRS` | `24` | Auto-escalate stale AWAITING_APPROVAL incidents |

---

## Safety Properties

### Exception Safety

All exceptions inside the polling loop are caught and logged. The loop itself never crashes, regardless of SAP API errors, HANA errors, or LLM failures:

```python
while not self._stop_flag:
    try:
        ...
    except Exception as e:
        logger.error("autonomous_loop_error", error=str(e), component="observer")
    await asyncio.sleep(POLL_INTERVAL_SECONDS)
```

### Confidence Gate

Auto-deployment only occurs when the LLM's RCA confidence meets or exceeds `AUTO_FIX_CONFIDENCE` (default 0.90). Lower-confidence incidents are queued for human approval.

### Consecutive Failure Escalation

If the same iFlow fails N times (`MAX_CONSECUTIVE_FAILURES`) without a successful fix, the incident is automatically escalated to a ticket with priority `HIGH`. This prevents the loop from endlessly retrying unfixable errors.

### Burst Deduplication

Within a single polling cycle, errors with the same signature are processed only once. Across cycles, `BURST_DEDUP_WINDOW_SECONDS` prevents the same error from being reprocessed too soon.

---

## Monitoring the Loop

### Check loop status

```bash
curl http://localhost:8080/autonomous/status
```

```json
{
  "autonomous_enabled": true,
  "loop_running": true,
  "poll_interval_seconds": 120,
  "auto_fix_confidence": 0.9,
  "last_poll_timestamp": "2026-04-09T10:00:00Z",
  "errors_processed_last_cycle": 3
}
```

### View recent incidents

```bash
curl "http://localhost:8080/incidents?status=FIX_VERIFIED&limit=10"
```

### View pending approvals

```bash
curl "http://localhost:8080/incidents?status=AWAITING_APPROVAL"
```

---

## AEM Events

The autonomous loop publishes lifecycle events to the AEM event bus at each stage:

| Topic | When |
|---|---|
| `sap/cpi/remediation/observed/{id}` | New incident detected |
| `sap/cpi/remediation/classified/{id}` | Classification complete |
| `sap/cpi/remediation/rca/{id}` | RCA complete |
| `sap/cpi/remediation/fix/{id}` | Fix applied |
| `sap/cpi/remediation/verified/{id}` | Verification result |

When `AEM_ENABLED=false` (default), these events are delivered in-process only. See [Event Bus](../aem/event-bus.md) for details.
