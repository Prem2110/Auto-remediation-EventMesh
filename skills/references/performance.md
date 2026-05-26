# Performance: Throughput, Memory, Concurrency

For when failure mode is OOM, slow processing, backpressure, or tenant resource exhaustion.

---

## Tenant resource model

CPI tenants have:
- Worker threads (parallel iFlow executions, tenant-tier-dependent)
- Memory per worker (typically 1–4 GB, tier-dependent)
- Storage for MPL, attachments, data store, JMS
- Network bandwidth (rarely the bottleneck in practice)

Single iFlow execution gets one worker + the memory budget for that worker. Multi-MB payloads compete with other concurrent executions for memory.

---

## Memory: avoiding OOM

### Streaming vs in-memory

Forced in-memory:
- Groovy script reads `message.getBody(String)`
- XML mapping (most cases)
- Content modifier setting body
- Splitter without streaming enabled

Streaming:
- General Splitter with `Streaming=true`
- StAX-based XML processing in scripts
- Pass-through routes with no body inspection

### Symptoms of OOM

- `Java heap space` errors
- `GC overhead limit exceeded`
- Tenant-wide slowdown (one OOM affects shared workers)
- Worker recycles (BTP restarts the worker; in-flight messages may fail)

### Memory remediation

- Enable splitter streaming for large payloads
- Use StAX/SAX in scripts instead of DOM
- Don't store payloads in properties unnecessarily (properties persist in exchange)
- Don't `getBody(String)` then `getBody(byte[])` — converts twice, doubles memory
- Process large payloads in chunks via JMS-based decoupling

---

## Throughput

### Throughput limits

Per iFlow:
- Sequential receiver call → bounded by receiver latency
- Parallel splitter → bounded by worker concurrency + receiver concurrency tolerance
- Aggregator → bounded by completion condition

Per tenant:
- Total active executions = tenant worker count
- New messages queue if all workers busy

### Identifying throughput bottlenecks

Look at MPL timings step-by-step:
- Long total duration with short step durations → queuing (waiting for worker)
- Long single step duration → that step is the bottleneck
- Variable durations on receiver call step → receiver-side variability

### Backpressure (JMS-specific)

When producer is faster than consumer:
- JMS queue grows
- Eventually hits `MaxOccupancy` → producer iFlow fails with `JMS queue full`
- Or grows until tenant storage limit, then everything fails

Detection: monitor JMS queue depths via tenant monitoring.

Remediation hierarchy:
1. Speed up the consumer iFlow (optimize, parallelize)
2. Add more parallel consumers (if message processing is independent)
3. Apply backpressure at producer (rate limit, fail-fast)
4. Increase queue size (last resort — defers the problem)

---

## Concurrency

### Where concurrency comes from

- Multiple messages arriving simultaneously to the same iFlow
- General Splitter (parallel sub-message processing)
- JMS multi-consumer
- Multicast

### Concurrency-related bugs

- **Static state in Groovy** (see `references/scripting.md`)
- **Shared properties across parallel branches** in Multicast
- **Race conditions on external resources** (DB rows, files)
- **Token cache contention** — multiple threads trying to refresh same token simultaneously

### Symptoms of concurrency bugs

- Tests pass, prod fails randomly
- Failure rate correlates with load
- Same input produces different outputs across runs

---

## Adapter-specific performance

### HTTP receiver
- Connection pool size: default usually adequate; tune if many short-lived calls
- Keep-Alive: enabled by default — disable only if receiver requires
- Compression: can reduce bandwidth but adds CPU; benchmark for your payloads

### JMS
- Prefetch: balance between throughput and message redelivery on consumer crash
- Lock duration: long enough for slowest legitimate processing, short enough to recover quickly from crashes

### OData
- Use `$select` to limit returned fields
- Use `$top` and `$skip` for pagination instead of fetching all
- Batch requests reduce round-trips

### SFTP
- File polling interval: too short = wasted polls; too long = latency
- Number of parallel transfers: bounded by server-side limits

---

## Timeouts — getting them right

Layer your timeouts from shortest to longest:

1. **Single receiver call timeout**: should fail before the next layer
2. **Step-level timeout** (rare in CPI but exists for some patterns)
3. **iFlow-level timeout** (if configured)
4. **Sender's own timeout** (caller of CPI gives up)

Common misconfiguration: receiver timeout 5 min, sender timeout 30 sec → sender disconnects, CPI continues processing for 5 min; eventual completion has no caller.

---

## Rate limiting and quotas

### Tenant-level

CPI tenants have per-message and per-second limits depending on tier. Hitting these:
- New messages queued briefly
- Eventually, sender adapters return throttling errors

### Receiver-level

Many SaaS receivers (SuccessFactors, Ariba, S/4HANA Cloud) have their own rate limits:
- `429 Too Many Requests` from receiver, often with `Retry-After` header
- Some adapters don't respect `Retry-After`; explicit retry logic in iFlow may be needed

---

## Error signatures

| Pattern | Meaning |
|---|---|
| `OutOfMemoryError` / `Java heap space` | Memory exhaustion |
| `GC overhead limit exceeded` | Heap thrashing |
| `JMS queue full` / `MaxOccupancy` | Backpressure from slow consumer |
| `429 Too Many Requests` | Rate limit at receiver |
| `Maximum number of messages exceeded` | Tenant-level throttling |
| `Lock could not be acquired` | Concurrency contention |
| `Timeout: ... after Xms` | Step-level or receiver-level timeout |
| Worker recycle in MPL gaps | OOM or runtime issue forced worker restart |

---

## Remediation guidance

| Action | Blast radius |
|---|---|
| Increasing timeout within ceiling | `SAFE_AUTO` |
| Adding splitter streaming | `REVIEW_REQUIRED` (behavior change) |
| Increasing parallelism | `REVIEW_REQUIRED` (concurrency risk) |
| Adding rate limiting | `REVIEW_REQUIRED` |
| Reducing log/trace level for perf | `REVIEW_REQUIRED` (debuggability tradeoff) |
