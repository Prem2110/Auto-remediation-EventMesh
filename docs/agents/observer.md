# ObserverAgent

**File:** `agents/observer_agent.py`

Autonomously polls SAP Integration Suite for failed messages and feeds them into the fix pipeline.

---

## Classes

### `SAPErrorFetcher`

Handles OAuth2 token management and OData REST calls to SAP.

**Token caching:** The access token is cached and automatically refreshed 30 seconds before expiry. Tokens are never logged in full.

**Key methods:**

| Method | Description |
|---|---|
| `fetch_failed_messages(limit)` | OData call to fetch failed CPI messages |
| `fetch_runtime_artifact_detail(iflow_id)` | Detailed error info for a specific iFlow runtime artifact |
| `_get_token()` | OAuth2 client credentials flow; cached internally |

### `ObserverAgent`

Wraps the polling loop and coordinates with the orchestrator.

**Key methods:**

| Method | Description |
|---|---|
| `start()` | Begin the autonomous monitoring loop (async infinite loop) |
| `stop()` | Set stop flag; loop exits cleanly after current iteration |
| `set_orchestrator(orchestrator)` | Inject `OrchestratorAgent` reference |
| `set_error_fetcher(fetcher)` | Inject `SAPErrorFetcher` reference |

---

## Polling Loop

```python
while not self._stop_flag:
    try:
        errors = await fetcher.fetch_failed_messages(limit=FAILED_MESSAGE_FETCH_LIMIT)
        for error in errors:
            signature = classifier.error_signature(...)
            if signature not in seen_this_cycle:
                await orchestrator.process_detected_error(error)
    except Exception as e:
        log.error("polling_error", error=str(e))  # Never crashes the loop
    await asyncio.sleep(POLL_INTERVAL_SECONDS)
```

Key safety properties:
- All exceptions are caught; the loop never crashes the asyncio event loop
- Per-cycle deduplication prevents the same error being submitted twice in one cycle
- The `BURST_DEDUP_WINDOW_SECONDS` check in `OrchestratorAgent` provides cross-cycle dedup

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `AUTONOMOUS_ENABLED` | `false` | Must be `true` for the loop to start |
| `POLL_INTERVAL_SECONDS` | `120` | Seconds between polling cycles |
| `FAILED_MESSAGE_FETCH_LIMIT` | `50` | Max messages fetched per cycle |
| `MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE` | `10` | Unique errors processed per cycle |
| `BURST_DEDUP_WINDOW_SECONDS` | `60` | Cross-cycle dedup window |

---

## SAP OData API

The fetcher calls the SAP Integration Suite OData API using the Hub tenant credentials (`SAP_HUB_*`). The endpoint path is configured in `core/constants.py`.

Error payloads are normalized to a standard dict before being passed to `OrchestratorAgent`:

```python
{
    "iflow_id": str,
    "message_guid": str,
    "error_message": str,
    "error_timestamp": str,
    "integration_artifact": dict,
}
```

---

## Lifecycle (Server Startup)

```python
# In main_v2.py lifespan
observer = ObserverAgent()
orchestrator = OrchestratorAgent(...)
observer.set_orchestrator(orchestrator)

if AUTONOMOUS_ENABLED:
    asyncio.create_task(observer.start())

# On shutdown
observer.stop()
```
