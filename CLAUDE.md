# SAP CPI Self-Healing Agent — Sierra Digital
## Claude Code Project Standards

> Last updated: 2026-03-30

---

## Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI + Uvicorn |
| AI / LLM | LangChain, SAP AI Core (ChatOpenAI via gen_ai_hub) |
| MCP Protocol | fastmcp `>=2.14.5`, langchain-mcp-adapters |
| Database | SAP HANA Cloud (primary), SQLite (local dev) via hdbcli |
| Object Storage | AWS S3 via boto3 |
| Auth | OAuth 2.0 (SAP) with token caching |
| Async | asyncio / httpx |
| Python | `>=3.13` (see pyproject.toml) |
| Logging | structlog + rotating file handlers |

---

## Project Layout

```
single_client/
├── main.py                       # Core FastAPI app + MultiMCP manager + autonomous loop
├── smart_monitoring.py           # /smart-monitoring/* API router
├── smart_monitoring_dashboard.py # /dashboard/* API router
├── generate_dashboard_pdf.py     # PDF export
├── config/
│   └── config.py                 # Settings loaded from .env
├── db/
│   └── database.py               # HANA / SQLite abstraction (incident CRUD, patterns, history)
├── storage/
│   ├── storage.py                # File upload + XSD detection
│   └── object_store.py           # S3 operations
├── utils/
│   ├── utils.py                  # HANA timestamp helpers
│   ├── logger_config.py          # Rotating file logger setup
│   ├── vector_store.py           # HANA vector search for SAP Notes
│   └── xsd_handler.py            # XSD parsing / validation
├── rules/                        # Development standards (see below)
│   ├── coding-style.md
│   ├── testing.md
│   └── security.md
├── logs/                         # Rotating application logs
├── .env                          # Secrets — NEVER commit
└── requirements.txt / pyproject.toml
```

---

## Code Change Rules

- **Delta edits only** — provide only the changed lines/functions. Never rewrite an entire file unless explicitly asked.
- **No fabricated tool calls** — never invent tool names or API responses; report actual results.
- **No hardcoded secrets** — always read from environment variables or `.env`.
- **No `Any` types** — use Pydantic v2 models or explicit type hints throughout.
- **Async-first** — all I/O operations (SAP API calls, DB queries, S3) must use `async/await`.
- **New dependencies** require explicit user confirmation before adding to `pyproject.toml`.

---

## Three MCP Servers — Routing Rules

| Server key | URL | Responsibility |
|---|---|---|
| `integration_suite` | CF runtime | iFlow get / update / deploy / unlock |
| `mcp_testing` | CF runtime | Test execution, validation, test reports |
| `documentation_mcp` | CF runtime | SAP standard docs, templates, spec generation |

- Never mix server responsibilities unless explicitly required.
- Fix + deploy pipelines must call `get-iflow → update-iflow → deploy-iflow` in strict order.
- Deploy is always mandatory after a successful update; never skip it.

---

## Observability Requirements

- All production logging must use **structured JSON** (structlog or the rotating logger in `utils/logger_config.py`).
- Every log line must include: `timestamp`, `level`, `component`, `correlation_id`, `user_id`.
- **Never** use bare `print()` statements in production code paths.
- Never return stack traces or internal error details to API clients or the LLM.

---

## Testing & Quality

- Minimum **80% line coverage** on all `db/`, `utils/`, and business-logic functions.
- External systems (SAP OData, HANA, S3) must be mocked in unit tests — no real calls.
- Use `pytest` + `pytest-asyncio` for all async functions.

---

## References

- See `@rules/coding-style.md` for naming conventions and architectural patterns.
- See `@rules/testing.md` for test framework and coverage requirements.
- See `@rules/security.md` for auth, secrets, and SAP BTP security standards.
