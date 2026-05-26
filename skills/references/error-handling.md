# Exception Handling Patterns in CPI

For when failure involves the exception subprocess, MPL showing only stack traces, or unclear error routing.

---

## The exception subprocess basics

Every iFlow should have an Exception Subprocess. Without one:
- Unhandled exceptions mark MPL `FAILED` with stack-trace-only context
- No alerting hook fires
- No DLQ write happens

## Standard exception subprocess pattern

A well-designed exception subprocess does these in order:

1. **Capture the original exception** — store `${exception.message}`, `${exception.stacktrace}`, `${property.CamelExceptionCaught}` in properties before anything else. Bad subprocesses lose this by running a Content Modifier that overwrites them.

2. **Capture the failing message body** — set a property to `${in.body}` so it isn't lost if downstream steps modify it.

3. **Classify** — set a `failureCategory` property so downstream alerting routes appropriately.

4. **Persist** — write to Data Store or JMS DLQ with original payload, exception details, correlation ID. This enables retry.

5. **Notify** — emit to alerting channel (typically ProcessDirect to alert iFlow, or write to monitoring queue).

6. **Decide on response** — for sync senders, exception subprocess must produce a response payload. For async, ack/nack the sender.

---

## Common antipatterns

### Bare catch that swallows

```groovy
try {
  // processing
} catch (Exception e) {
  message.setHeader("hadError", "true")
}
return message
```

Groovy step succeeds, iFlow continues, failure invisible in MPL. Receiver gets partial/wrong payload.

**Fix**: re-throw, let exception subprocess handle it.

### Exception subprocess calls same flow

If exception subprocess writes notification via the same receiver type that just failed, notification itself fails → silent failure or loop.

**Fix**: notification path should use a different transport (main = OData to S/4, notification = HTTP to monitoring webhook).

### Catching MessagingException to "recover"

```groovy
try {
  // receiver call
} catch (MessagingException me) {
  // pretend it succeeded
}
```

Worst class of bug: data lost silently. MPL shows green, downstream never got the message.

**Fix**: never catch `MessagingException` to swallow. For recovery, route to retry/DLQ pattern.

### Rethrowing without cause chain

```groovy
} catch (Exception e) {
  throw new RuntimeException("Processing failed")  // original cause lost
}
```

**Fix**: `throw new RuntimeException("Processing failed", e)` to preserve the cause.

---

## The DLQ pattern

For async flows where retry-with-backoff isn't enough:

1. Main iFlow processes; on failure, exception subprocess writes to JMS DLQ with full context.
2. Separate "DLQ consumer" iFlow runs on a schedule, picks up DLQ messages, decides:
   - `TRANSIENT_RECEIVER` + time > backoff → replay
   - `AUTH_FAILURE` + credential rotated → replay
   - `DATA_QUALITY` → escalate via ITSM
   - Otherwise → age out after N retries and create ticket

This pattern is what makes auto-remediation possible. Without explicit DLQ + classification on entry, retries are blind.

---

## Exception subprocess context variables

Available inside the exception subprocess:

| Variable | Contents |
|---|---|
| `${exception.message}` | Top-level exception message |
| `${exception.stacktrace}` | Full stack trace |
| `${property.CamelExceptionCaught}` | The Exception object (Groovy access) |
| `${property.CamelFailureEndpoint}` | URI of failing endpoint |
| `${property.CamelFailureRouteId}` | Route ID where failure occurred |
| `${property.SAP_MessageProcessingLogID}` | MPL ID for cross-reference |

---

## What the MPL gives you

- Step-by-step execution trace
- Headers and properties at each step (if `Log Level = Trace`)
- Exception stack traces on failure
- Final payload at termination

**Trace logging is off by default in production.** "No payload in MPL" ≠ "no payload was processed."

## MPL log level guidance

| Level | Use case |
|---|---|
| `None` | Performance-critical iFlows where MPL itself is overhead |
| `Error` | Default for high-volume prod |
| `Info` | Default for medium-volume prod |
| `Debug` | Temporary diagnostic only — don't leave on |
| `Trace` | Forensic diagnostic; payloads logged, retention/compliance impact |

---

## Custom headers worth setting

Conventions for cross-iFlow correlation:

- `SAP_MessageProcessingLogID` — already set by CPI; surface in all alerts
- `X-Correlation-ID` — set at sender, propagate through all ProcessDirect calls
- `X-Source-System` — for multi-source iFlows, classification depends on this
- `X-Retry-Count` — increment on each DLQ-driven retry; cap based on it

Properties (internal) vs headers (leak to receivers): see core principle #4 in `sap-cpi-reference.md`.

---

## Alert routing

Recommended pattern: exception subprocess → ProcessDirect → central alert iFlow → ITSM ticket.

Centralisation benefits:
- Single point for formatting and enrichment
- Dedup of related alerts (same correlation ID firing multiple times)
- Rate-limiting of alert volume to avoid ticket spam during incidents
