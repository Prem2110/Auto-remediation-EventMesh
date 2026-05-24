# Monitoring: MPL, Tracing, Alerting

For when the task involves monitoring data, MPL analysis, log retention, or alerting setup.

---

## Message Processing Log (MPL)

The MPL is CPI's per-message execution record. It's the primary forensic artifact for incidents.

### MPL fields

| Field | Meaning |
|---|---|
| `MessageGuid` | Unique ID per message processing instance |
| `CorrelationId` | Correlation across iFlow boundaries (you set this) |
| `Status` | `COMPLETED`, `FAILED`, `PROCESSING`, `RETRY`, `DISCARDED` |
| `LogStart` / `LogEnd` | Processing time window |
| `IntegrationFlowName` | Which iFlow ran |
| `SenderName` / `ReceiverName` | Endpoints |
| `LogLevel` | Trace detail level |

Plus per-step entries showing execution trace and (if trace) headers/properties/body at each step.

### Status meanings — subtle points

- `COMPLETED` ≠ "no errors anywhere." A flow that catches and handles an exception completes successfully. Look at custom properties or attachments for sub-failures.
- `FAILED` = unhandled exception. The exception subprocess may have run but itself failed, or it routed to FAILED state deliberately.
- `PROCESSING` indefinitely = stuck. Either a sync call to an unresponsive receiver, or a JMS consumer died mid-processing.
- `RETRY` = scheduled retry (JMS, retry policy). Distinct from `FAILED`.
- `DISCARDED` = message dropped per policy (e.g., expired).

### MPL retention

Default retention is tenant-configured (often 30–90 days for headers, shorter for payloads). Payloads in attachments may be purged before metadata.

Implication: incidents reported late may have only metadata, not payloads. Don't promise payload-based RCA without first checking if it's retained.

### Trace log

Trace log is per-step detail with full headers/properties/body. Off by default in production for cost and compliance reasons.

Enabling trace:
- Per-iFlow toggle in monitoring
- Auto-disabled after a window (default 10 min, configurable)
- Generates substantial storage

When trace is needed for RCA but is off:
- Wait for next occurrence with trace on (if reproducible)
- Or correlate with downstream system logs

---

## Custom logging from iFlows

```groovy
def messageLog = messageLogFactory.getMessageLog(message)
messageLog?.setStringProperty("customField", value)
messageLog?.addAttachmentAsString("name", content, "mime/type")
```

- String properties show in MPL header (queryable)
- Attachments show as files in MPL details (large, retention-limited)

### Logging best practices

- Don't log payloads by default — trace mode does this already.
- Do log classifications, decisions, business keys.
- Never log credentials, tokens, full headers (Authorization leak risk).
- Use consistent property names across iFlows for cross-flow analysis.

---

## Tracing across iFlows

Cross-iFlow correlation requires explicit correlation IDs:

1. Sender sets `X-Correlation-ID` (or similar header).
2. iFlow stores in property, propagates through ProcessDirect calls.
3. Sub-iFlows include in their MPL custom properties.
4. Monitoring tool queries by correlation ID to assemble cross-flow trace.

Without this, correlating "this incident" across the actual chain of iFlows is manual and error-prone.

---

## Alerting

CPI's built-in alerting is limited. Common patterns:

### Pattern: exception subprocess emits to alert iFlow

Exception subprocess → ProcessDirect to central alert iFlow → posts to:
- ITSM (ticket creation)
- Email
- Slack/Teams webhook
- BTP Alert Notification service

Central alert iFlow benefits:
- Single point for formatting and enrichment
- Dedup of related alerts (same correlation ID)
- Rate-limiting to avoid alert storms

### Pattern: scheduled health check iFlow

Periodic iFlow that:
- Polls receiver endpoints (health-check URLs)
- Checks cert expiry windows on keystore
- Checks JMS queue depths
- Emits alerts if any threshold crossed

Catches issues before they cause user-visible failures.

### Pattern: MPL polling for FAILED messages

Periodic iFlow that queries CPI's monitoring OData API for recent FAILED messages, classifies them, and emits alerts. See `references/cpi-apis.md` for the relevant OData endpoints.

---

## Monitoring data quality

When using MPL data as RCA input:

- **Sample size matters**: one failure could be random. Look for patterns across N failures.
- **Time correlation**: cluster failures by timestamp — simultaneous failures usually share root cause.
- **Receiver correlation**: failures concentrated on one receiver point to receiver-side issue.
- **iFlow correlation**: failures across many iFlows point to infrastructure (tenant, CC, network).

---

## Error signatures

| Pattern | Meaning |
|---|---|
| `MPL not found` for ID | MPL purged (retention), or wrong ID |
| `Trace log not available` | Trace was off, or trace data already purged |
| `Attachment not found` | Attachment retention shorter than MPL header retention |
| Per-step status `OK` but message `FAILED` | Failure in exception subprocess after main flow |

---

## Remediation guidance

| Action | Blast radius |
|---|---|
| Enabling trace temporarily with auto-disable timer | `SAFE_AUTO` |
| Adding custom log properties (no PII) | `SAFE_AUTO` |
| Increasing retention | `REVIEW_REQUIRED` (storage cost, compliance) |
| Changing alert routing | `REVIEW_REQUIRED` |
