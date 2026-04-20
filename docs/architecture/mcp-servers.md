# MCP Servers

The system communicates with SAP Integration Suite exclusively through three MCP (Model Context Protocol) servers. Each server has a distinct responsibility and must never be used outside its designated scope.

---

## Server Routing Table

| Key | Environment Variable | Responsibility |
|---|---|---|
| `integration_suite` | `MCP_INTEGRATION_SUITE_URL` | iFlow CRUD + deploy + unlock |
| `mcp_testing` | `MCP_TESTING_URL` | Test execution, validation, test reports |
| `documentation_mcp` | `MCP_DOCUMENTATION_URL` | SAP standard docs, templates, spec generation |

Server URLs are set in `core/constants.py` (`MCP_SERVERS` dict) and can be overridden by the environment variables above.

---

## integration_suite

Handles all design-time operations against SAP Integration Suite.

**Tools exposed:**

| Tool | Description |
|---|---|
| `get-iflow` | Fetch iFlow XML and metadata by ID |
| `update-iflow` | Upload modified iFlow XML |
| `deploy-iflow` | Trigger deployment of an iFlow |
| `unlock-iflow` | Release a locked iFlow |

**Used by:** `FixAgent`, `RCAAgent` (read-only), `VerifierAgent` (runtime status)

!!! danger "Deploy is Mandatory"
    Every `update-iflow` call **must** be followed by `deploy-iflow`. Never skip the deploy step after a successful update.

---

## mcp_testing

Handles all test and verification operations.

**Tools exposed:**

| Tool | Description |
|---|---|
| `send-test-message` | Inject a test payload into an iFlow HTTP endpoint |
| `retry-failed-message` | Replay a specific failed message by GUID |
| `get-test-report` | Fetch test execution results |
| `check-runtime-status` | Poll iFlow deployment/runtime status |

**Used by:** `VerifierAgent` exclusively

---

## documentation_mcp

Provides access to SAP documentation and template generation.

**Tools exposed:**

| Tool | Description |
|---|---|
| `get-sap-docs` | Retrieve SAP standard documentation |
| `generate-spec` | Generate specification document for an iFlow |
| `get-template` | Fetch iFlow XML template for a given adapter type |

**Used by:** `RCAAgent` (supplementary context), `OrchestratorAgent` (chatbot queries)

---

## Connection Management

The `MultiMCP` class in `core/mcp_manager.py` manages all server connections.

**Lifecycle:**

```python
# Startup (lifespan in main_v2.py)
await multi_mcp.connect()      # Opens StreamableHttpTransport for all 3 servers
await multi_mcp.discover_tools()  # Fetches tool schemas from each server

# Per-request
tool_result = await multi_mcp.execute("get-iflow", {"iflow_id": "..."})

# Shutdown
await multi_mcp.disconnect()
```

**Transport settings** (from `TRANSPORT_OPTIONS` in `core/constants.py`):

| Setting | Value |
|---|---|
| SSL verify | `true` in production |
| Timeout | 60 seconds per call |
| Retries | 1 unlock + 1 retry on lock error |

---

## Agent-to-Server Access Matrix

| Agent | integration_suite | mcp_testing | documentation_mcp |
|---|---|---|---|
| ClassifierAgent | No | No | No |
| RCAAgent | Read only (`get-iflow`, `get_message_logs`) | No | Yes |
| FixAgent | Full (get / update / deploy / unlock) | No | No |
| VerifierAgent | Runtime status only | Full | No |
| OrchestratorAgent | Via FixAgent | Via VerifierAgent | Yes (chatbot) |
| ObserverAgent | No (OData direct) | No | No |

!!! note "RCA Tool Filtering"
    `RCAAgent` receives a filtered tool list that explicitly excludes `update-iflow` and `deploy-iflow`. This prevents accidental modifications during root cause analysis.
