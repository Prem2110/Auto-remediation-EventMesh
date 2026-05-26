# CPI Adapter Quirks & Gotchas

Per-adapter detail for when the failing step involves a specific adapter type.

---

## HTTP Receiver

**Defaults that bite**
- Timeout: 60s (often too short for downstream SAP systems)
- Retry: 0 (no retry on transient failure)
- Connection pooling: enabled, but pool exhaustion produces `ConnectTimeoutException`, not a pool error

**Common error → cause**
- `503 Service Unavailable` → receiver down or rate-limited. Safe to retry with backoff.
- `502 Bad Gateway` → usually a load balancer in front of the receiver. Investigate the LB layer.
- `401` after period of success → token expiry. The adapter should auto-refresh OAuth but doesn't always.
- `Connection reset` mid-request → receiver killed the connection. Often payload-size related.

**Config patterns**
- For idempotent receivers, retry 3 with exponential backoff.
- For non-idempotent receivers, retry must be 0; rely on JMS DLQ + manual reprocess.
- Always set `Authentication` explicitly; `None` is rarely correct in production.

---

## HTTP Sender

**Defaults that bite**
- CSRF protection: enabled by default for state-changing methods. Callers must fetch token first.
- URL pattern: case-sensitive; trailing slashes matter.
- Auth: Basic is the default; OAuth requires explicit setup.

**Common error → cause**
- `403 Forbidden` from sender → CSRF token missing or expired.
- `404 Not Found` despite correct path → iFlow not deployed, or address path mismatch.

---

## SOAP Receiver

**Defaults that bite**
- WS-Addressing: off
- MTOM: off
- WS-Security: none
- Compression: off

Toggling any of these changes wire format. Coordinate with receiver team before changing.

**Common error → cause**
- `WSDL validation failed` → schema mismatch, often after receiver-side WSDL update. Local WSDL copy doesn't auto-update.
- `MustUnderstand header not understood` → CPI sending header receiver isn't configured for. Usually WS-Addressing or WS-Security misconfiguration.

---

## IDoc Sender/Receiver

**Status codes**

| Code | Meaning |
|---|---|
| `03` | IDoc passed to port OK (sender side success) |
| `12` | Dispatch OK (receiver side success after status 03) |
| `51` | Application document not posted (business error; investigate SAP app log via `WE19`/`BD87`) |
| `52` | Application document posted with warning |
| `53` | Application document posted |
| `56` | IDoc with errors added (usually duplicate) |
| `64` | IDoc ready to be transferred to application |
| `69` | IDoc was edited |

Status 51 means the receiver ACK'd receipt — failure is application-side, not integration-side. Classify as `RECEIVER_APP_ERROR`, NOT `TRANSIENT_RECEIVER`.

---

## OData v2 / v4

**v2 vs v4 differences**
- Error envelope path: v2 = `error.message.value`, v4 = `error.message`
- Batch format: completely different syntax
- `$filter` operators: v4 adds `contains` (replaces v2's `substringof`)
- Metadata endpoint: same path (`$metadata`), different schema
- ETags: v4 makes them more pervasive; missing `If-Match` causes 412

**Auth modes**
- Basic: fine for sandbox, never for prod
- OAuth2 SAML Bearer: most common for S/4HANA Cloud
- OAuth2 Client Credentials: most common for SuccessFactors and similar
- Principal Propagation: for on-premise via Cloud Connector

**Token caching** — credentials cache tokens. After rotation, in-flight tokens remain valid until expiry. Failures appear delayed.

---

## JMS

**Behaviors**
- DLQ messages are NOT auto-retried.
- Tenant queue size limits exist (often 4 GB). When full, sender blocks or fails.
- `Persistent=true` is default; non-persistent loses messages on broker restart.
- Lock duration default: ~10 min. Consumer must finish within window or message redelivers.

**Common error → cause**
- `MaxOccupancy reached` → consumer can't keep up. Investigate consumer, not producer.
- `Queue not found` → typo, or queue deleted. Runtime doesn't auto-create.
- `Lock could not be acquired` → another consumer holds the lock, or previous consumer crashed mid-processing.

---

## SFTP

**Silent breakages**
- Server fingerprint change → looks like auth failure, but is known-hosts mismatch. Check `Strict Host Key Checking`.
- File-locking: CPI uses `.lck` file by default; if previous run crashed, lock can persist.
- `MoveFile` after processing: if move fails, file reprocessed next poll. Idempotency concern.

**Common error → cause**
- `Auth failed: publickey` → key not on server, or wrong key pair.
- `Auth failed: password` → password rotated, or account locked.

---

## ProcessDirect

**Behavior**
- In-process; called iFlow runs in same execution context.
- Unhandled exception in called iFlow propagates to caller as if inline.
- Separate MPL entry per iFlow; correlation requires explicit correlation ID.

**RCA implication**
- Failing iFlow may not be where the bug lives. If failing step is a ProcessDirect call, trace into the called iFlow before classifying.

---

## SuccessFactors

**Session token caching**
- Caches session tokens. After credential rotation, failures may appear N requests later.
- Failing message's timestamp is NOT the cause's timestamp. Look back to most recent credential change.

**Rate limits**
- Per-tenant rate limits enforced by SuccessFactors. `429` responses → rate limiting, not real failure. Back off and retry.

**Operations**
- `Upsert` is idempotent by design; prefer over separate Insert/Update.
- `Query` with `$select` reduces payload; full entity reads are slow.

---

## Ariba

Similar token caching pattern to SuccessFactors.

- Ariba requires realm in URL path. Misconfigured realm → 404 (not auth error).
- ANID (network ID) is different from realm — both required for some calls.

---

## Mail (IMAP/POP3/SMTP)

**Behaviors**
- Provider throttling often silent; partial fetches return success without indication.
- IMAP idle vs poll: idle is more efficient but requires persistent connection.
- SMTP: send is fire-and-forget from CPI's perspective; bounces aren't detected.

**Common error → cause**
- `Authentication failed` → app-specific password required (Gmail, M365 with MFA).
- `Connection refused` → firewall, or OAuth required and not configured.

---

## AMQP

**Prefetch matters**
- Default prefetch: 250. Too high for slow consumers — messages locked but not processed → redelivery storm.
- Tune to: ~2x average processing rate per consumer.

**Topic vs queue**
- Topic = pub/sub, multiple consumers each get a copy.
- Queue = point-to-point, one consumer gets each message.
- Misconfiguration → "missing messages" (sent to queue but expected on topic).

---

## OpenConnectors

**Aggregator layer**
- Provider-specific errors get repackaged. Generic error might hide specific upstream error.
- Check the OpenConnectors element for the true upstream response.

**Quota management**
- OpenConnectors has its own quota tier separate from CPI tenant. Hitting OC quota → cryptic 429 with OC-specific error code.

---

## Mail attachment edge cases

- MIME boundary mismatch → silent partial parse.
- Encoding (base64 vs 7bit) handling differs by client; downstream parsing may need normalization.
