# Coding Style — SAP CPI Self-Healing Agent

## Naming Conventions

| Target | Convention | Example |
|---|---|---|
| Files | `snake_case` | `smart_monitoring.py`, `vector_store.py` |
| Classes | `PascalCase` | `MultiMCP`, `SAPErrorFetcher`, `VectorStoreRetriever` |
| Functions / methods | `snake_case`, verb-first | `run_rca`, `apply_fix`, `fetch_failed_messages` |
| Pydantic models | `PascalCase` | `AnalyzeRequest`, `ApplyFixRequest` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_RETRIES`, `AUTO_FIX_CONFIDENCE` |
| API routes | `kebab-case` path segments | `/smart-monitoring/messages/{guid}/apply_fix` |
| Environment variables | `UPPER_SNAKE_CASE` | `HANA_HOST`, `POLL_INTERVAL_SECONDS` |

---

## Architecture

### Module Responsibilities

| File | What goes here |
|---|---|
| `main.py` | `MultiMCP` class, FastAPI app lifespan, autonomous loop, `/query`, `/fix`, `/autonomous/*` routes |
| `smart_monitoring.py` | `/smart-monitoring/*` routes — thin controllers; delegate business logic to `MultiMCP` |
| `smart_monitoring_dashboard.py` | `/dashboard/*` read-only aggregate queries — never mutate state |
| `db/database.py` | All HANA / SQLite queries — no business logic, no HTTP calls |
| `config/config.py` | `Settings` class — one `os.getenv()` call per variable, nowhere else |
| `utils/` | Stateless helpers only — no imports from `main.py` (prevents circular imports) |
| `storage/` | File I/O and S3 operations — no direct DB writes |

### Dependency Rule

```
main.py
  └── smart_monitoring.py (lazy import via _get_mcp())
  └── smart_monitoring_dashboard.py
  └── db/database.py
  └── utils/*
  └── storage/*
  └── config/config.py
```

`utils/` and `db/` must **never** import from `main.py`. Use the lazy `_get_mcp()` pattern for any router that needs `mcp_manager`.

---

## FastAPI Patterns

- Use `APIRouter` with `prefix` and `tags` in every router file.
- Pydantic models for all request bodies — no raw `dict` parameters.
- Use `Depends()` for shared dependencies (e.g., `_get_mcp()`).
- Background tasks (`BackgroundTasks`) for fire-and-forget operations (RCA, auto-fix).
- Return explicit `JSONResponse` or typed Pydantic response models — avoid bare `dict` returns where possible.

---

## MCP Tool Calls

- One MCP tool call = one specific action (e.g., `get-iflow`, not a generic `manage-iflow`).
- Fix+Deploy pipeline is always: `get-iflow → (apply fix) → update-iflow → deploy-iflow`. Never skip `deploy-iflow`.
- Check every tool response for success before proceeding to the next step.
- On lock detection (`"locked"` in response): attempt unlock once, then retry. If unlock fails, stop and report.

---

## Async & Concurrency

- All I/O operations (SAP OData, HANA queries, S3) must use `async/await`.
- Use `asyncio.gather()` for independent parallel calls (e.g., fetching error details for multiple messages).
- Rate-limit concurrent SAP calls — use `aiolimiter` or semaphores; never fan out unbounded.
- Background loops (`_autonomous_loop`) must handle all exceptions to avoid crashing the asyncio event loop.

---

## Error Handling

- Never let raw Python exceptions propagate to API clients — catch, log, and re-raise as `HTTPException`.
- Log every exception with full context (tool name, iflow_id, error message) **before** raising.
- Never silently swallow exceptions with bare `except: pass`.
- Return user-friendly messages in tool/agent responses — the LLM will relay these to the end user.
- Never expose stack traces in API responses (`detail=str(exc)` is acceptable; `traceback` is not).

---

## Response Size & LLM Context

- Agent responses going to the LLM must not exceed ~4 000 tokens.
- Dashboard/list endpoints must support pagination (`limit`, `offset`) — never return unbounded result sets.
- RCA prompts must include only the fields the LLM needs — do not dump raw SAP OData payloads.

---

## Logging

```python
# Correct: structured, module-scoped
import logging
logger = logging.getLogger(__name__)
logger.info("[RCA] Starting analysis", extra={"iflow_id": iflow_id, "error_type": error_type})

# Wrong: bare print or unstructured
print("starting RCA")
logger.info("starting RCA for " + iflow_id)
```

- Log at `INFO` for normal operations, `WARNING` for degraded-but-recovering, `ERROR` for failures.
- Always include `[ComponentName]` prefix in the message for easy log filtering.
