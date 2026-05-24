# Groovy / JS Scripting in CPI

For when failure involves a Script step, or RCA points to a Groovy/JS bug.

---

## CPI's Groovy runtime — what's different

CPI runs Groovy in a sandbox with Camel's `Message` and `Exchange` objects:

```groovy
import com.sap.gateway.ip.core.customdev.util.Message
def Message processData(Message message) {
    // ...
    return message
}
```

Differences from standalone Groovy:
- **No filesystem access** — `java.io.File` operations fail in production tenants.
- **No arbitrary network calls** — must go through CPI adapters.
- **No `System.exit`, no spawning threads** — scripts run in shared executors.
- **Limited classpath** — most Java standard library available, but third-party libraries beyond CPI's bundles are not.

---

## The big pitfalls

### Static state across invocations

```groovy
class Helper {
    static int counter = 0
}
```

Shared across all parallel executions in the tenant. Under concurrency → nondeterministic behavior. Classic "works in dev, fails randomly in prod under load" bug.

**Rule**: no `static` mutable state. Each invocation must be completely isolated.

### Reading body without considering streaming

```groovy
def body = message.getBody(String)
```

For 100 MB payload, loads everything into memory → OOM risk. CPI tenants have per-execution memory limits.

**Fix**: for large payloads, use `InputStream` or `Reader` and process incrementally. Or use a Splitter with streaming.

### Header vs property confusion

```groovy
message.setHeader("internal_correlation", uuid)  // WRONG — leaks to receiver
message.setProperty("internal_correlation", uuid)  // CORRECT — stays in exchange
```

\#1 source of "why is internal data at the receiver" tickets.

### Null-handling on optional headers

```groovy
def custId = message.getHeader("CustomerID", String).trim()
```

If header is missing → NPE, classified as `SCRIPT_BUG`. The failure point looks like a script bug; the root cause is upstream (sender didn't set header).

**Fix**:
```groovy
def custId = message.getHeader("CustomerID", String)?.trim() ?: ""
```

Then decide explicitly whether empty is acceptable, or whether the iFlow should fail-fast.

### Silent JSON parsing failures

```groovy
import groovy.json.JsonSlurper
def parsed = new JsonSlurper().parseText(message.getBody(String))
def value = parsed.field1.field2.field3
```

Any intermediate null → NPE deep in chain. Stack trace shows line but not missing field.

**Fix**: validate parsed structure with explicit null checks, or use a JSON schema validation step upstream.

### Regex DoS

```groovy
def matched = body =~ /(a+)+$/
```

Catastrophic backtracking on malicious input → CPU pegged, execution times out. Treat regex inputs as untrusted; bound complexity.

### SimpleDateFormat across threads

`SimpleDateFormat` is not thread-safe. Static-scoped instances across iFlow executions cause parsing errors under concurrency.

**Fix**: instantiate fresh in each call, or use `java.time` (thread-safe).

### Secrets in logs

```groovy
messageLog.setStringProperty("Headers", message.getHeaders().toString())
```

Dumps everything including `Authorization`. Even temporarily, this leaks credentials into MPL retained storage.

**Fix**: explicit allow-list of headers to log.

---

## JavaScript in CPI

JavaScript steps use Nashorn (older CPI) or GraalVM JS (newer). Most Groovy patterns translate, but:

- Nashorn deprecated; new scripts should use GraalVM JS when available.
- `console.log` doesn't go anywhere useful in MPL; use the message logger.
- Promise/async patterns don't work — script must be synchronous.

---

## Logging from scripts

```groovy
def messageLog = messageLogFactory.getMessageLog(message)
if (messageLog != null) {
    messageLog.setStringProperty("MyDebugInfo", someValue)
    messageLog.addAttachmentAsString("payload-before-mapping", body, "text/xml")
}
```

Writes to MPL where it's visible in monitoring. Use sparingly in production — attachments consume tenant storage within the MPL retention window.

---

## Calling external resources

For HTTP from a script, use `URLConnection` — but consider whether this should be an HTTP receiver step instead (better visibility in MPL, retry handling, credential management).

Never hardcode endpoints or credentials in scripts. Use the SecureStore API:

```groovy
def service = ITApiFactory.getApi(SecureStoreService.class, null)
def cred = service.getUserCredential("my_credential_alias")
def user = cred.getUsername()
def pwd = cred.getPassword().toString()
```

This pulls from the tenant's secure credential store; aliases are rotatable without code changes.

---

## Defensive script template

```groovy
import com.sap.gateway.ip.core.customdev.util.Message
import groovy.json.JsonSlurper

def Message processData(Message message) {
    def messageLog = messageLogFactory.getMessageLog(message)

    try {
        // 1. Read inputs defensively
        def body = message.getBody(String)
        if (!body) {
            throw new IllegalStateException("Empty body received")
        }

        def correlationId = message.getHeader("X-Correlation-ID", String) ?: "no-corr-id"

        // 2. Do the work
        def parsed = new JsonSlurper().parseText(body)
        // validation, transformation...

        // 3. Set outputs explicitly
        message.setBody(transformedBody)
        message.setProperty("processedCorrelation", correlationId)

        return message
    } catch (Exception e) {
        // Surface context, re-throw — never swallow
        messageLog?.setStringProperty("ScriptError", e.message)
        messageLog?.addAttachmentAsString("FailedPayload", message.getBody(String) ?: "", "text/plain")
        throw e
    }
}
```

Pattern: defensive reads, explicit writes, log-and-rethrow on error.

---

## Testing scripts locally

CPI doesn't have great local test tooling for scripts. Practical approach:

- Mock the `Message` interface with a Groovy class providing `setBody`/`getBody`/`setHeader` etc.
- Run unit tests with Groovy CLI or Spock framework.
- Don't test CPI-specific APIs (`messageLogFactory`, `ITApiFactory`) locally — stub them.

---

## Performance tips

- Cache compiled XSLT/XPath — use `static final` for the compiler, not for results.
- For large XML, prefer StAX (streaming) over DOM.
- Avoid `body.text` then re-serializing — process the stream once.
