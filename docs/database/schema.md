# Database Schema

**File:** `db/database.py`

The system uses SAP HANA Cloud in production and SQLite for local development. All tables are created automatically on first startup via schema migration functions.

---

## Tables

### `AUTONOMOUS_INCIDENTS`

The core incident tracking table. Every failed message detected by the observer creates one row.

| Column | Type | Description |
|---|---|---|
| `incident_id` | UUID (PK) | Unique incident identifier |
| `iflow_id` | VARCHAR | Integration Flow ID |
| `message_guid` | VARCHAR | SAP CPI message GUID |
| `error_message` | TEXT | Full error message (truncated to 2000 chars) |
| `error_type` | VARCHAR | Classification result (e.g. `MAPPING_ERROR`) |
| `error_signature` | VARCHAR(16) | MD5 hash for deduplication |
| `occurrence_count` | INT | How many times this error recurred |
| `status` | VARCHAR | Current pipeline status |
| `confidence` | FLOAT | RCA confidence score |
| `root_cause` | TEXT | LLM root cause explanation |
| `proposed_fix` | TEXT | LLM proposed fix |
| `affected_component` | VARCHAR | iFlow component name |
| `fix_steps` | JSON | Ordered fix steps from RCA |
| `fix_applied` | BOOLEAN | Whether fix was applied |
| `deploy_success` | BOOLEAN | Whether deploy succeeded |
| `verification_status` | VARCHAR | Result of VerifierAgent |
| `iflow_snapshot_before` | TEXT | Full iFlow XML before any change |
| `retry_count` | INT | Number of fix retry attempts |
| `ticket_id` | VARCHAR | Linked escalation ticket ID |
| `consecutive_failures` | INT | Failures without successful fix |
| `auto_escalated` | BOOLEAN | Whether auto-escalated |
| `created_at` | TIMESTAMP | When incident was created |
| `updated_at` | TIMESTAMP | Last update timestamp |
| `resolved_at` | TIMESTAMP | When incident was resolved |
| `user_id` | VARCHAR | User who triggered (if manual) |

**Incident Status Values:**

| Status | Meaning |
|---|---|
| `DETECTED` | New incident, not yet processed |
| `CLASSIFIED` | Error type identified |
| `RCA_COMPLETE` | Root cause analysis done |
| `FIX_IN_PROGRESS` | Fix pipeline running |
| `AWAITING_APPROVAL` | Human approval needed |
| `FIX_VERIFIED` | Fix deployed and verified |
| `FIX_FAILED` | Fix pipeline failed |
| `DEPLOY_FAILED` | Update succeeded, deploy failed |
| `FIX_TIMEOUT` | Pipeline timed out |
| `RETRIED` | Transient error retried successfully |
| `TICKET_CREATED` | Escalated to ticket system |

---

### `FIX_PATTERNS`

Stores successful fixes for reuse in future incidents.

| Column | Type | Description |
|---|---|---|
| `pattern_id` | UUID (PK) | Unique pattern ID |
| `signature` | VARCHAR(16) | Error signature (MD5) |
| `iflow_id` | VARCHAR | Associated iFlow |
| `error_type` | VARCHAR | Error type |
| `proposed_fix` | TEXT | Fix description |
| `success_count` | INT | Times this fix succeeded |
| `failure_count` | INT | Times this fix failed |
| `success_rate` | FLOAT | success / (success + failure) |
| `key_steps` | JSON | Ordered XML change steps |
| `replay_success_count` | INT | Times message replay succeeded |
| `created_at` | TIMESTAMP | First seen |
| `updated_at` | TIMESTAMP | Last updated |

Patterns are returned by `get_similar_patterns()` to the `RCAAgent` when `success_count >= PATTERN_MIN_SUCCESS_COUNT`.

---

### `ESCALATION_TICKETS`

Tracks incidents that require human intervention.

| Column | Type | Description |
|---|---|---|
| `ticket_id` | UUID (PK) | Ticket ID |
| `incident_id` | UUID (FK) | Linked incident |
| `iflow_id` | VARCHAR | Affected iFlow |
| `error_type` | VARCHAR | Error type |
| `title` | VARCHAR | Short ticket title |
| `description` | TEXT | Full description |
| `priority` | VARCHAR | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `status` | VARCHAR | `OPEN`, `IN_PROGRESS`, `RESOLVED`, `CLOSED` |
| `assigned_to` | VARCHAR | Assignee |
| `resolution_notes` | TEXT | How it was resolved |
| `created_at` | TIMESTAMP | Created |
| `updated_at` | TIMESTAMP | Last updated |
| `resolved_at` | TIMESTAMP | Resolved |

---

### `MCP_QUERY_HISTORY`

Stores user queries and responses for audit and analytics.

| Column | Type | Description |
|---|---|---|
| `session_id` | UUID (PK) | Session identifier |
| `user_id` | VARCHAR | User who submitted the query |
| `query` | TEXT | User's query text |
| `response` | TEXT | Agent response |
| `timestamp` | TIMESTAMP | When the query was submitted |

---

### `USER_FILES_METADATA`

Metadata for files uploaded via the storage API.

| Column | Type | Description |
|---|---|---|
| `file_id` | UUID (PK) | Generated file ID |
| `file_name` | VARCHAR | Original filename |
| `mime_type` | VARCHAR | Detected MIME type |
| `size` | BIGINT | File size in bytes |
| `s3_key` | VARCHAR | S3 object key |
| `uploaded_by` | VARCHAR | User ID |
| `created_at` | TIMESTAMP | Upload timestamp |

---

### `SAP_IS_XSD_FILES`

Stores XSD file content linked to iFlows.

| Column | Type | Description |
|---|---|---|
| `xsd_id` | UUID (PK) | XSD file ID |
| `xsd_name` | VARCHAR | File name |
| `xsd_content` | TEXT | Full XSD content |
| `iflow_id` | VARCHAR | Associated iFlow |
| `uploaded_at` | TIMESTAMP | Upload timestamp |

---

### `SAP_HELP_DOCS`

Vector store for semantic SAP Notes search.

| Column | Type | Description |
|---|---|---|
| `doc_id` | UUID (PK) | Document ID |
| `title` | VARCHAR | SAP Note title |
| `content` | TEXT | Full document content |
| `embedding` | REAL_VECTOR(3072) | text-embedding-3-large embedding |
| `source_url` | VARCHAR | Original SAP Notes URL |
| `created_at` | TIMESTAMP | Vectorized at |

---

### `TEST_SUITE_LOGS`

Execution history for test suites run by `VerifierAgent`.

| Column | Type | Description |
|---|---|---|
| `test_suite_id` | UUID (PK) | Test suite ID |
| `user` | VARCHAR | Who triggered the test |
| `prompt` | TEXT | Query that triggered the test |
| `timestamp` | TIMESTAMP | Execution start |
| `status` | VARCHAR | `RUNNING`, `PASSED`, `FAILED` |
| `executions` | JSON | Array of test execution results |

---

## Key Database Functions

| Function | Description |
|---|---|
| `create_incident(payload)` | Insert new incident; return incident_id |
| `update_incident(id, **kwargs)` | Update any incident fields |
| `get_incident_by_id(id)` | Fetch full incident record |
| `get_incident_by_signature(sig, within_seconds)` | Dedup lookup |
| `get_incidents(status, limit, offset)` | Paginated incident list |
| `get_similar_patterns(signature, error_type)` | Pattern lookup for RCA |
| `upsert_fix_pattern(...)` | Insert or update FIX_PATTERNS |
| `create_escalation_ticket(...)` | Create ticket and link to incident |
| `get_escalation_tickets(status)` | List tickets |
| `create_query_history(...)` | Log user query |
| `get_all_history(user_id, limit)` | Paginated query history |
| `addTestSuiteLog(...)` | Log test suite execution |
| `update_test_suite_executions(id, results)` | Update test results |

---

## Connection Management

Each function in `db/database.py` opens and closes its own HANA connection. There is no connection pooling. For high-throughput scenarios, a connection pool can be added via `hdbcli`'s `ConnectionPool`.

```python
conn = get_connection()
try:
    cursor = conn.cursor()
    cursor.execute(sql, params)
    conn.commit()
finally:
    conn.close()
```
