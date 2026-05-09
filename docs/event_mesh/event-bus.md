# Event Mesh Event Bus

**File:** `event_mesh/event_bus.py`  
**Class:** `EventBus`

Publishes fix lifecycle events. Supports two modes: in-process only (default) and SAP Event Mesh REST delivery.

---

## Modes

### Mode 1: In-Process (Default)

When `EM_ENABLED=false` (the default), all events are delivered directly to registered Python async handlers within the same process. No external dependencies.

```python
bus = EventBus()
bus.subscribe("sap/cpi/remediation/verified", my_handler)
await bus.publish("sap/cpi/remediation/verified/incident-123", event_data)
# → my_handler(event_data) is called directly
```

### Mode 2: SAP Event Mesh

When `EM_ENABLED=true`, events are POSTed to the SAP Event Mesh REST endpoint **in addition to** calling local handlers.

```
POST {EM_REST_URL}
Authorization: Basic {EM_USERNAME}:{EM_PASSWORD}
Content-Type: application/json

{ "topic": "sap/cpi/remediation/verified/incident-123", "data": {...} }
```

---

## Topic Scheme

All topics follow this pattern:

```
{EM_QUEUE_PREFIX}/{stage}/{incident_id}
```

Default prefix: `sap/cpi/remediation`

| Topic | Published When |
|---|---|
| `sap/cpi/remediation/observed/{id}` | New incident detected by ObserverAgent |
| `sap/cpi/remediation/classified/{id}` | Classification complete |
| `sap/cpi/remediation/rca/{id}` | RCA complete |
| `sap/cpi/remediation/fix/{id}` | Fix applied (success or failure) |
| `sap/cpi/remediation/verified/{id}` | Post-fix verification result |

---

## API

### `subscribe(topic_prefix, handler)`

Register an async callable to be called when an event matches `topic_prefix`.

```python
async def on_fix(event: dict):
    print(f"Fix applied: {event['incident_id']}")

bus.subscribe("sap/cpi/remediation/fix", on_fix)
```

### `publish(topic, event)`

Publish an event. Calls all matching subscribers and (if enabled) posts to SAP Event Mesh.

```python
await bus.publish(
    topic=f"sap/cpi/remediation/fix/{incident_id}",
    event={
        "incident_id": incident_id,
        "fix_applied": True,
        "deploy_success": True,
        "timestamp": "2026-04-09T10:00:00Z"
    }
)
```

---

## Subscription Registration (Server Startup)

In `main_v2.py` lifespan:

```python
from event_mesh.event_bus import EventBus

bus = EventBus()

# Example: notify a dashboard on RCA completion
bus.subscribe("sap/cpi/remediation/rca", dashboard_handler)

# Example: send Slack notification on fix completion
bus.subscribe("sap/cpi/remediation/fix", slack_notifier)
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `EM_ENABLED` | `false` | Enable REST delivery to SAP Event Mesh |
| `EM_REST_URL` | — | SAP Event Mesh REST endpoint URL |
| `EM_USERNAME` | — | Basic auth username |
| `EM_PASSWORD` | — | Basic auth password |
| `EM_QUEUE_PREFIX` | `sap/cpi/remediation` | Topic prefix |

---

## Inbound Events

The `/event-mesh/events` endpoint receives events pushed by SAP Event Mesh:

```
POST /event-mesh/events
Content-Type: application/json

{
  "topic": "...",
  "data": {...}
}
```

This allows external systems to trigger actions in the self-healing agent (e.g., SAP Integration Suite pushing error notifications directly rather than waiting for the polling cycle).
