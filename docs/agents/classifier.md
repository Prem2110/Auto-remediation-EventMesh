# ClassifierAgent

**File:** `agents/classifier_agent.py`

Rule-based error classifier. No LLM, no MCP, no I/O — purely synchronous string analysis.

---

## Error Types

| Error Type | Typical Cause |
|---|---|
| `SFTP_ERROR` | SFTP connection failure, missing directory |
| `AUTH_ERROR` | Expired credential alias, token failure |
| `MAPPING_ERROR` | Target field renamed, missing XSD element |
| `DATA_VALIDATION` | Missing mandatory field, format mismatch |
| `CONNECTIVITY_ERROR` | Network timeout, host unreachable |
| `ADAPTER_CONFIG_ERROR` | Receiver URL incorrect, HTTP 4xx from adapter |
| `BACKEND_ERROR` | Backend service returning HTTP 500 |
| `UNKNOWN_ERROR` | No pattern matched |

---

## Key Methods

### `classify_error(error_message) -> dict`

Returns:

```python
{
    "error_type": "MAPPING_ERROR",
    "confidence": 0.85,
    "tags": ["xpath", "transformation"]
}
```

Classification is pattern-based: keyword and regex matching against the error message string. Each match contributes to a confidence score. The highest-scoring type wins.

### `error_signature(iflow_id, error_type, error_message) -> str`

Returns a 16-character MD5 hex string from the combination of the three inputs. Used by `OrchestratorAgent` for burst deduplication.

```python
signature = md5(f"{iflow_id}:{error_type}:{error_message[:200]}".encode()).hexdigest()[:16]
```

### `fallback_root_cause(error_type, error_message) -> str`

Returns a human-readable root cause string based on error type. Used by `RCAAgent` when LLM confidence is lower than the classifier's confidence.

---

## Design Rationale

The classifier is intentionally lightweight:

- It runs before RCA, which is expensive (LLM call + vector search)
- A fast pre-classification lets the Orchestrator route RETRY and TICKET cases without ever invoking the LLM
- The confidence score from classification is compared with the LLM's RCA confidence — the higher score wins

---

## No External Dependencies

This agent has zero external dependencies. It can be unit-tested without mocking anything.
