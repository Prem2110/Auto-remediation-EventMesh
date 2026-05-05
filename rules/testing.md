# Testing — SAP CPI Self-Healing Agent

## Framework & Tools

| Tool | Purpose |
|---|---|
| `pytest` | Test runner |
| `pytest-asyncio` | Async test support |
| `pytest-mock` / `unittest.mock` | Mock SAP OData, HANA, S3 |
| `httpx.AsyncClient` | FastAPI route testing |
| `pytest-cov` | Coverage reporting |

---

## Coverage Requirements

- Minimum **80% line coverage** on:
  - `db/database.py` — all CRUD functions
  - `utils/` — all helper utilities
  - RCA and fix pipeline logic in `main.py` (`run_rca`, `apply_fix`, `remediation_gate`)
- Dashboard routes (`smart_monitoring_dashboard.py`) require 60% minimum.

---

## What to Mock

**Always mock** — never call real external systems in tests:

| System | Mock target |
|---|---|
| SAP Integration Suite OData | `httpx.AsyncClient.get/post` |
| SAP AI Core / LLM | `gen_ai_hub.proxy.langchain.openai.ChatOpenAI` |
| HANA database | `hdbcli.dbapi.connect` |
| S3 / Object Store | `boto3.client` |
| MCP tool calls | `fastmcp.client.Client.call_tool` |
| Vector store | `utils.vector_store.VectorStoreRetriever.retrieve_relevant_notes` |

---

## Test Structure

```
tests/
├── test_rca.py               # run_rca(), classify_error(), confidence scoring
├── test_fix_pipeline.py      # ask_fix_and_deploy(), apply_fix(), evaluate_fix_result()
├── test_autonomous_loop.py   # process_detected_error(), remediation_gate()
├── test_smart_monitoring.py  # /smart-monitoring/* route tests
├── test_dashboard.py         # /dashboard/* route tests
├── test_database.py          # HANA / SQLite CRUD functions
├── test_vector_store.py      # VectorStoreRetriever (mocked HANA)
└── conftest.py               # Shared fixtures (app client, mock MCP, mock DB)
```

---

## Async Tests

```python
import pytest
import pytest_asyncio

@pytest.mark.asyncio
async def test_run_rca_mapping_error(mock_mcp_manager):
    result = await mock_mcp_manager.run_rca(
        iflow_id="test_iflow",
        error_message="Field 'Material' does not exist",
        error_type="MAPPING_ERROR",
    )
    assert result["error_type"] == "MAPPING_ERROR"
    assert result["rca_confidence"] >= 0.5
    assert result["proposed_fix"] is not None
```

---

## Docstring Validation

- Every public function exposed as an API endpoint or called by the autonomous loop must have a non-empty docstring.
- Test that Pydantic models reject invalid input (missing required fields, wrong types).

---

## Running Tests

```bash
# Run all tests with coverage
pytest --cov=. --cov-report=term-missing tests/

# Run a specific module
pytest tests/test_fix_pipeline.py -v

# Run async tests
pytest -m asyncio tests/
```
