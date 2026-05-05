# Configuration

All configuration is loaded from a `.env` file at the project root. Copy `.env.example` to `.env` and fill in the required values.

Legend: **[R]** = Required, **[O]** = Optional (has default)

---

## SAP AI Core

| Variable | R/O | Description |
|---|---|---|
| `AICORE_CLIENT_ID` | R | OAuth2 client ID for AI Core |
| `AICORE_CLIENT_SECRET` | R | OAuth2 client secret for AI Core |
| `AICORE_AUTH_URL` | R | Token endpoint URL |
| `AICORE_BASE_URL` | R | AI Core API base URL |
| `AICORE_RESOURCE_GROUP` | R | Resource group name (e.g. `default`) |
| `LLM_DEPLOYMENT_ID` | R | Deployment ID of the LLM model |

---

## SAP Integration Suite — Design Time

Used to read and write iFlow XML.

| Variable | R/O | Description |
|---|---|---|
| `SAP_DESIGN_TIME_URL` | R | Design-time API base URL |
| `SAP_DESIGN_TIME_TOKEN_URL` | R | OAuth2 token endpoint |
| `SAP_DESIGN_TIME_CLIENT_ID` | R | OAuth2 client ID |
| `SAP_DESIGN_TIME_CLIENT_SECRET` | R | OAuth2 client secret |

---

## SAP Integration Suite — CPI Monitoring

Used to fetch message logs and runtime artifact details.

| Variable | R/O | Description |
|---|---|---|
| `CPI_BASE_URL` | R | CPI monitoring API base URL |
| `CPI_OAUTH_CLIENT_ID` | R | OAuth2 client ID |
| `CPI_OAUTH_CLIENT_SECRET` | R | OAuth2 client secret |
| `CPI_OAUTH_TOKEN_URL` | R | OAuth2 token endpoint |

---

## SAP Integration Suite — Hub (Autonomous Polling)

Used by `ObserverAgent` to poll for failed messages via OData.

| Variable | R/O | Description |
|---|---|---|
| `SAP_HUB_TENANT_URL` | R | Hub tenant base URL |
| `SAP_HUB_TOKEN_URL` | R | OAuth2 token endpoint |
| `SAP_HUB_CLIENT_ID` | R | OAuth2 client ID |
| `SAP_HUB_CLIENT_SECRET` | R | OAuth2 client secret |

---

## SAP Integration Suite — API

| Variable | R/O | Description |
|---|---|---|
| `API_BASE_URL` | R | Integration Suite API base URL |
| `API_OAUTH_CLIENT_ID` | R | OAuth2 client ID |
| `API_OAUTH_CLIENT_SECRET` | R | OAuth2 client secret |
| `API_OAUTH_TOKEN_URL` | R | OAuth2 token endpoint |

---

## SAP HANA Cloud

| Variable | R/O | Description |
|---|---|---|
| `HANA_HOST` | R | HANA Cloud host (omit for SQLite fallback) |
| `HANA_PORT` | R | HANA Cloud port (typically `443`) |
| `HANA_USER` | R | Database user |
| `HANA_PASSWORD` | R | Database password |
| `HANA_SCHEMA` | R | Schema name |
| `HANA_TABLE_QUERY_HISTORY` | O | Table name for query history (default: `MCP_QUERY_HISTORY`) |
| `HANA_TABLE_USER_FILES` | O | Table for file metadata (default: `USER_FILES_METADATA`) |
| `HANA_TABLE_XSD_FILES` | O | Table for XSD files (default: `SAP_IS_XSD_FILES`) |
| `HANA_TABLE_VECTOR` | O | Vector store table (default: `SAP_HELP_DOCS`) |
| `HANA_TABLE_SAP_DOCS` | O | SAP docs table (default: `SAP_HELP_DOCS`) |

---

## AWS S3 / Object Store

| Variable | R/O | Description |
|---|---|---|
| `BUCKET_NAME` | R* | S3 bucket name |
| `REGION` | R* | AWS region |
| `ENDPOINT_URL` | O | Custom S3 endpoint (for SAP Object Store) |
| `OBJECT_STORE_ENDPOINT` | O | Alias for `ENDPOINT_URL` |
| `OBJECT_STORE_ACCESS_KEY` | R* | Access key (single key mode) |
| `OBJECT_STORE_SECRET_KEY` | R* | Secret key (single key mode) |
| `WRITE_ACCESS_KEY_ID` | O | Separate write access key |
| `WRITE_SECRET_ACCESS_KEY` | O | Separate write secret key |
| `READ_ACCESS_KEY_ID` | O | Separate read access key |
| `READ_SECRET_ACCESS_KEY` | O | Separate read secret key |

*Required only if file upload feature is used.

---

## MCP Servers

| Variable | O | Description |
|---|---|---|
| `MCP_INTEGRATION_SUITE_URL` | O | Overrides the `integration_suite` URL from constants |
| `MCP_TESTING_URL` | O | Overrides the `mcp_testing` URL from constants |
| `MCP_DOCUMENTATION_URL` | O | Overrides the `documentation_mcp` URL from constants |

---

## Autonomous Operations

| Variable | R/O | Default | Description |
|---|---|---|---|
| `AUTONOMOUS_ENABLED` | O | `false` | Enable background polling loop |
| `POLL_INTERVAL_SECONDS` | O | `120` | Seconds between SAP polling cycles |
| `AUTO_FIX_CONFIDENCE` | O | `0.90` | Minimum confidence to auto-deploy a fix |
| `SUGGEST_FIX_CONFIDENCE` | O | `0.70` | Minimum confidence to suggest a fix |
| `AUTO_FIX_ALL_CPI_ERRORS` | O | `true` | Apply AUTO_FIX policy to all error types |
| `AUTO_DEPLOY_AFTER_FIX` | O | `true` | Always deploy after successful update |
| `FAILED_MESSAGE_FETCH_LIMIT` | O | `50` | Max failed messages per polling cycle |
| `RUNTIME_ERROR_FETCH_LIMIT` | O | `20` | Max runtime errors per cycle |
| `MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE` | O | `10` | Unique errors to process per cycle |
| `MAX_CONSECUTIVE_FAILURES` | O | `3` | Escalate after N consecutive failures |
| `PENDING_APPROVAL_TIMEOUT_HRS` | O | `24` | Auto-escalate stale approval requests |
| `PATTERN_MIN_SUCCESS_COUNT` | O | `2` | Minimum successes before pattern is trusted |
| `BURST_DEDUP_WINDOW_SECONDS` | O | `60` | Dedup window for the same error signature |

---

## SAP Advanced Event Mesh (AEM)

| Variable | R/O | Default | Description |
|---|---|---|---|
| `AEM_ENABLED` | O | `false` | Enable AEM REST delivery |
| `AEM_REST_URL` | O | — | AEM REST messaging endpoint |
| `AEM_USERNAME` | O | — | AEM credentials |
| `AEM_PASSWORD` | O | — | AEM credentials |
| `AEM_QUEUE_PREFIX` | O | `sap/cpi/remediation` | Topic prefix for published events |

---

## Escalation Tickets

| Variable | R/O | Default | Description |
|---|---|---|---|
| `ESCALATION_EMAIL` | O | — | Email address for ticket notifications |
| `ESCALATION_SYSTEM` | O | `internal` | External ticketing system name |

---

## Server & Logging

| Variable | R/O | Default | Description |
|---|---|---|---|
| `API_HOST` | O | `0.0.0.0` | Uvicorn bind address |
| `API_PORT` | O | `8080` | Uvicorn port |
| `LOG_LEVEL` | O | `DEBUG` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `ENABLE_CONSOLE_LOGS` | O | `false` | Mirror logs to stdout |
| `UPLOAD_ROOT` | O | `user` | S3 key prefix for uploaded files |

---

## LangSmith (Optional Tracing)

| Variable | R/O | Description |
|---|---|---|
| `LANGSMITH_API_KEY` | O | LangSmith API key |
| `LANGSMITH_PROJECT` | O | Project name in LangSmith dashboard |
| `LANGSMITH_TRACING` | O | Set to `true` to enable tracing |

---

## SAP Notes Scraper

Only needed if running `scrape_sap_docs.py`.

| Variable | R/O | Description |
|---|---|---|
| `SAP_USERNAME` | O | SAP support portal username |
| `SAP_PASSWORD` | O | SAP support portal password |

---

## Runtime Config Overrides

Some settings can be changed at runtime without restarting the server. They are persisted in `runtime_config.json`:

```bash
# Toggle auto-fix on/off
curl -X POST http://localhost:8080/autonomous/enable
curl -X POST http://localhost:8080/autonomous/disable

# Reset to .env value
curl -X POST http://localhost:8080/autonomous/reset
```
