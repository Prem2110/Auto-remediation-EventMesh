# Prerequisites

Before installing the SAP CPI Self-Healing Agent, ensure the following are available.

---

## Runtime

| Requirement | Version | Notes |
|---|---|---|
| Python | `>=3.13` | Managed via `uv` (recommended) or `pip` |
| uv | latest | Fast Python package manager |
| Playwright | latest | Required only for `scrape_sap_docs.py` |

---

## SAP Services

| Service | Required | Purpose |
|---|---|---|
| SAP Business Technology Platform (BTP) | Yes | Hosting environment |
| SAP Integration Suite (Cloud Integration) | Yes | iFlow runtime; source of failed messages |
| SAP AI Core | Yes | LLM inference (GPT-4o via `gen_ai_hub`) |
| SAP HANA Cloud | Yes | Incident storage, vector store, fix patterns |
| SAP Advanced Event Mesh (AEM) | No | Event streaming; in-process fallback used if disabled |

### SAP AI Core Setup

You need an active AI Core deployment with:

- A deployed LLM (e.g., GPT-4o via AI Core resource group)
- Client credentials: `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET`, `AICORE_AUTH_URL`
- Resource group name: `AICORE_RESOURCE_GROUP`
- Deployment ID: `LLM_DEPLOYMENT_ID`

### SAP Integration Suite Setup

Three OAuth 2.0 client credential pairs are required:

| Pair | Purpose | Variables |
|---|---|---|
| Design-time | Read/write iFlow XML | `SAP_DESIGN_TIME_*` |
| Runtime / Hub | Fetch failed messages (OData) | `SAP_HUB_*` |
| CPI Monitoring | Message logs and runtime artifacts | `CPI_*` |

### SAP HANA Cloud Setup

- HANA Cloud instance with schema write access
- Tables are auto-created on first startup via schema migration functions
- Required variables: `HANA_HOST`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD`, `HANA_SCHEMA`

---

## AWS S3

Required only if using the file upload feature.

- S3-compatible bucket (AWS or SAP Object Store Service)
- Separate read/write credentials are supported:
  - `WRITE_ACCESS_KEY_ID` / `WRITE_SECRET_ACCESS_KEY`
  - `READ_ACCESS_KEY_ID` / `READ_SECRET_ACCESS_KEY`

---

## MCP Servers

Three MCP servers must be running and reachable. These are separate services (typically deployed on SAP Cloud Foundry):

| Server | Responsibility |
|---|---|
| `integration_suite` | iFlow get / update / deploy / unlock |
| `mcp_testing` | Test execution, validation, test reports |
| `documentation_mcp` | SAP standard docs, templates, spec generation |

Set their URLs via environment variables or directly in `core/constants.py`.

---

## Optional

| Tool | Purpose |
|---|---|
| LangSmith account | LLM call tracing and debugging |
| Prometheus-compatible scraper | Metrics collection from `/metrics` endpoint |
