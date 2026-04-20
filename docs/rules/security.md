# Security

Security standards for the SAP CPI Self-Healing Agent.

---

## Secrets & Configuration

- **No hardcoded credentials** — all secrets (SAP OAuth, HANA password, S3 keys, AI Core client secret) must come from `.env` via `os.getenv()` or `config/config.py`.
- `.env` is in `.gitignore` and must **never** be committed. Use `.env.example` as the pattern for documenting required variables.
- On SAP BTP Cloud Foundry, bind secrets via VCAP_SERVICES / user-provided service instances — do not pass them as plain CF environment variables in `manifest.yml`.

---

## SAP OAuth Token Handling

- Tokens are fetched via OAuth 2.0 client credentials flow (`client_id` + `client_secret`).
- Cache tokens in memory until `expires_in - 30s` to avoid unnecessary token requests.
- **Never** log the full token value — log only the first 8 characters for traceability.
- **Never** store tokens in the database or S3.

---

## API Authentication

- The FastAPI server currently trusts the `user_id` field in request bodies (internal use only).
- If exposed externally, add JWT validation middleware using `sap-xssec` (for BTP) before any endpoint processes the request.
- Always forward the requesting user's identity when calling back-end SAP systems — never substitute a shared technical user for user-facing fix operations.

---

## Input Validation

- All request bodies use Pydantic v2 models — never accept raw `dict` or untyped JSON.
- `guid`, `iflow_id`, `user_id`, and similar path/body parameters must be validated before use in SQL queries or API calls.
- SQL queries to HANA must use parameterised statements (`cur.execute(query, params)`) — never use f-string or `.format()` string interpolation for SQL.
- File uploads: validate MIME type and file size before processing; reject non-XML/non-XSD files when XSD handling is expected.

---

## Error Responses

- **Never** return Python stack traces, internal exception messages, or database error details to API clients.
- Use `HTTPException(status_code=..., detail="<user-friendly message>")` for all client-facing errors.
- Log the full exception (with context) server-side before raising.

---

## MCP Server Communication

- MCP server URLs are stored in `MCP_SERVERS` in `core/constants.py` — do not hardcode them in router files.
- Transport `verify=True` for production MCP endpoints; `verify=False` is only acceptable for internal CF-to-CF calls where the cert chain is not importable.
- Set explicit timeouts (60s) on all MCP HTTP transports — never use `timeout=None`.

---

## Autonomous Loop Safety

- The autonomous fix+deploy loop must only run when `AUTONOMOUS_ENABLED=true` is explicitly set.
- `AUTO_FIX_CONFIDENCE` (default 0.90) gates autonomous deployment — do not lower this threshold without explicit review.
- Every autonomous action (retry, fix, deploy) must be written to `AUTONOMOUS_INCIDENTS` with actor `"autonomous"` **before** the action executes.
- Failures in the autonomous loop must not crash the asyncio event loop — catch all exceptions and continue the loop.

---

## Data Protection

- Do not log full message payloads or customer data from SAP Integration Suite error logs.
- Truncate `error_message` to 2,000 characters when storing in the database.
- S3 object keys must not contain PII — use UUIDs or session IDs as key prefixes.
