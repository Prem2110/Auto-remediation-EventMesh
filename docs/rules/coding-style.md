# Coding Style

Standards for the SAP CPI Self-Healing Agent codebase.

---

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

## Module Responsibilities

| File | What goes here |
|---|---|
| `main_v2.py` | FastAPI app lifespan, autonomous loop, `/query`, `/fix`, `/autonomous/*` routes, agent wiring |
| `smart_monitoring.py` | `/smart-monitoring/*` routes — thin controllers; delegate business logic to agents |
| `smart_monitoring_dashboard.py` | `/dashboard/*` read-only aggregate queries — never mutate state |
| `db/database.py` | All HANA / SQLite queries — no business logic, no HTTP calls |
| `config/config.py` | `Settings` class — one `os.getenv()` call per variable, nowhere else |
| `utils/` | Stateless helpers only — no imports from `main_v2.py` (prevents circular imports) |
| `storage/` | File I/O and S3 operations — may import `db/` and `utils/` only |
| `agents/` | All business logic — import `core/`, `db/`, `utils/` only |
| `core/` | Infrastructure (MCP, constants, state, validators) — no imports from `agents/` |

### Dependency Rule

```
main_v2.py
  └── agents/
        └── core/
        └── db/
        └── utils/
  └── smart_monitoring.py (lazy import)
  └── smart_monitoring_dashboard.py
  └── db/database.py
  └── utils/*
  └── storage/*
  └── config/config.py
```

`utils/` and `db/` must **never** import from `main_v2.py`. Use lazy imports for any router that needs agents.

---

## FastAPI Patterns

- Use `APIRouter` with `prefix` and `tags` in every router file.
- Pydantic models for all request bodies — no raw `dict` parameters.
- Use `Depends()` for shared dependencies.
- Background tasks (`BackgroundTasks`) for fire-and-forget operations (RCA, auto-fix).
- Return explicit `JSONResponse` or typed Pydantic response models — avoid bare `dict` returns.

---

## MCP Tool Calls

- One MCP tool call = one specific action (e.g., `get-iflow`, not a generic `manage-iflow`).
- Fix+Deploy pipeline is always: `get-iflow → (apply fix) → update-iflow → deploy-iflow`. Never skip `deploy-iflow`.
- Check every tool response for success before proceeding to the next step.
- On lock detection (`"locked"` in response): attempt unlock once, then retry. If unlock fails, stop and report.

---

## Async & Concurrency

- All I/O operations (SAP OData, HANA queries, S3) must use `async/await`.
- Use `asyncio.gather()` for independent parallel calls.
- Rate-limit concurrent SAP calls — use `aiolimiter` or semaphores; never fan out unbounded.
- Background loops must handle all exceptions to avoid crashing the asyncio event loop.

---

## Error Handling

- Never let raw Python exceptions propagate to API clients — catch, log, and re-raise as `HTTPException`.
- Log every exception with full context (tool name, iflow_id, error message) **before** raising.
- Never silently swallow exceptions with bare `except: pass`.
- Return user-friendly messages in tool/agent responses.
- Never expose stack traces in API responses.

---

## Response Size & LLM Context

- Agent responses going to the LLM must not exceed ~4,000 tokens.
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
- Every production log line must include: `timestamp`, `level`, `component`, `correlation_id`, `user_id`.
- Never use bare `print()` statements in production code paths.
