# Idempotency Patterns in CPI

For when category is `IDEMPOTENCY_VIOLATION`, or a proposed fix involves retry config.

---

## Why idempotency is central

CPI retries on transient failures — network blips, receiver restarts, timeouts. Without an idempotency strategy, retries produce duplicates. "Duplicate records downstream" is one of the highest-impact failure modes (data corruption, financial impact, manual cleanup).

The question for every receiver call: **"What happens if this exact request runs twice?"** If the answer isn't "second call is a safe no-op," the iFlow needs an idempotency strategy.

---

## Strategies

### Strategy 1: Idempotent Process Call step

CPI's built-in step using an Idempotent Repository (JDBC or in-memory):

- **Key**: header or property uniquely identifying the message (source system ID + operation type)
- **Repository**: JDBC for production (in-memory loses state on restart)
- **Behavior**: if key seen, step short-circuits subsequent processing

Cleanest pattern when the source reliably provides a unique ID.

### Strategy 2: Correlation ID at receiver

If receiver supports idempotency natively (e.g., HTTP receiver with `Idempotency-Key` header):

- Generate correlation ID once at sender side
- Propagate as header to receiver
- Receiver dedupes based on header

Works for HTTP receivers. SOAP receivers usually lack a standard idempotency-key concept.

### Strategy 3: Downstream dedup

When neither above is available, receiver app must dedupe:

- Source provides business key (e.g., `OrderNumber`)
- Receiver app checks "have I seen this before?" and discards duplicates

Pushes burden to application team; requires coordination. The iFlow itself is *not* idempotent — relying on external dedup.

### Strategy 4: Upsert semantics

For data sync iFlows, use upsert instead of insert:
- OData v4 PATCH operation
- SQL `MERGE`
- SuccessFactors `Upsert` operation

Receiver call becomes idempotent by construction.

### Strategy 5: Compare-and-swap with ETag

For OData v4 receivers supporting ETags:
- Read entity, capture ETag
- Send update with `If-Match: <etag>`
- Receiver rejects if ETag doesn't match (entity changed)

Provides idempotency + optimistic concurrency.

---

## Anti-strategies to avoid

### "Set retry to 0 and hope"

CPI may still retry at JMS layer; sender may retry independently; ops may manually retry from MPL. "No retry" is not sufficient idempotency.

### Timestamp-based dedup

"Skip if seen this ID in last 60s" fails when:
- Legitimate updates occur within the window
- Clock skew between systems
- Window expires before a delayed retry arrives

Dedup must be content-based (unique key), not timing-based.

### Scope dedup too narrowly

Deduping by a single field misses cases where the same logical operation has different surface representations (different timestamps, different request IDs). Use a stable business key from the source domain.

---

## Detecting idempotency violations in MPL

Flag when:
- Same message ID appears in MPL multiple times in a short window
- Receiver returns `Duplicate key` / `unique constraint violation`
- Downstream system reports duplicate records

The second case — "successful from CPI's perspective, broken from receiver's perspective" — means MPL is green for the first attempt, failure shows on retry. RCA needs to look at the *pair*, not just the failed one.

---

## Safe remediation for idempotency violations

When classifier identifies `IDEMPOTENCY_VIOLATION`:

- **Never** auto-fix by changing idempotency strategy (`REVIEW_REQUIRED` minimum)
- **Do** suggest appropriate strategy based on receiver type and available unique keys
- **Do** check if duplicate is from legitimate retry (fix is dedup) vs. genuine double-send from source (fix is upstream)

The "legit retry vs upstream double-send" question is often the hardest part of RCA. Evidence:
- Same correlation ID across attempts → legit retry, dedup needed
- Different correlation IDs, same business key → upstream double-send, address at source
- One CPI-side attempt, two records downstream → race condition or downstream bug

---

## Idempotent Repository sizing

The JDBC idempotent repository grows; old entries must be purged. Default TTL is often missing in production configs.

Symptoms:
- Repository table grows unbounded
- Query performance degrades over time
- Eventually, dedup checks themselves time out

Configure TTL based on the legitimate retry window (e.g., 7 days for human-driven retries, 1 day for automated retries).
