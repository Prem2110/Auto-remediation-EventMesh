# VerifierAgent

**File:** `agents/verifier_agent.py`

Verifies that a deployed fix actually resolves the original error. Uses `mcp_testing` server exclusively — no write access to `integration_suite`.

---

## Key Methods

### `retry_failed_message(incident) -> dict`

Replays the original failed message by GUID. Used when the remediation policy is RETRY (transient errors like `CONNECTIVITY_ERROR`).

```python
result = await multi_mcp.execute("retry-failed-message", {
    "message_guid": incident["message_guid"]
})
```

Returns: `{"success": bool, "message": str}`

### `test_iflow_after_fix(incident) -> dict`

Sends a test payload to the iFlow HTTP endpoint and checks the response.

```python
result = await multi_mcp.execute("send-test-message", {
    "iflow_id": incident["iflow_id"],
    "payload": test_payload
})
```

Returns: `{"success": bool, "response_code": int, "response_body": str}`

### `check_iflow_runtime_status(iflow_id) -> dict`

Polls the CPI runtime artifact status to confirm the iFlow is in `STARTED` state after deployment.

```python
result = await multi_mcp.execute("check-runtime-status", {
    "iflow_id": iflow_id
})
```

Returns: `{"status": "STARTED" | "ERROR" | "STOPPING" | "DEPLOYING", "message": str}`

---

## Verification Strategy

The Orchestrator calls methods in this order after a successful fix:

```
1. check_iflow_runtime_status()
   → Must be STARTED before proceeding

2a. If incident has message_guid:
    retry_failed_message()

2b. If no GUID (e.g., adapter config fix):
    test_iflow_after_fix()

3. Return combined result
```

---

## TestExecutionTracker (`agents/base.py`)

The `TestExecutionTracker` class correlates test payloads with message IDs and logs them to `TEST_SUITE_LOGS` in HANA:

```python
tracker = TestExecutionTracker()
tracker.record_test(iflow_id, payload, message_id)
tracker.mark_success(message_id, response)
tracker.mark_failure(message_id, error)
```

This provides an audit trail of all test executions per incident.

---

## MCP Access

| Tool | Server | Used by |
|---|---|---|
| `retry-failed-message` | `mcp_testing` | `retry_failed_message()` |
| `send-test-message` | `mcp_testing` | `test_iflow_after_fix()` |
| `check-runtime-status` | `mcp_testing` | `check_iflow_runtime_status()` |
| `get-test-report` | `mcp_testing` | Post-verification report |

The VerifierAgent has no access to `update-iflow` or `deploy-iflow`.
