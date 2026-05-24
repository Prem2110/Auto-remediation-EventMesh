# CPI Public APIs (OData)

For when the task involves CPI's management APIs — querying MPL, deploying iFlows, managing artifacts programmatically.

CPI exposes OData APIs for tenant management and monitoring. These are the APIs Orbit and similar tools use to query MPL data, retry messages, manage artifacts, etc.

---

## Base URL pattern

Per-tenant base URL:
```
https://<tenant-host>/api/v1/
```

`<tenant-host>` is the tenant's runtime host (may differ from the design-time/web UI host on some BTP regions).

Auth: OAuth2 client credentials with a service key from the tenant's process integration runtime.

---

## Major API categories

### Monitoring / MPL queries

```
GET /MessageProcessingLogs
GET /MessageProcessingLogs('<guid>')
GET /MessageProcessingLogs?$filter=Status eq 'FAILED'
```

Key query patterns:

- `$filter=Status eq 'FAILED'` — failed messages
- `$filter=Status eq 'FAILED' and LogStart gt datetime'2025-01-01T00:00:00'` — time-bounded
- `$filter=IntegrationFlowName eq 'MyFlow'` — per iFlow
- `$filter=substringof('CorrelationId123', CorrelationId)` — by correlation
- `$expand=AdapterAttributes,CustomHeaderProperties` — include extras

Pagination: `$top=N&$skip=M` (default page size limits apply).

Performance: filter narrowly. Wide queries against full MPL history are slow and may time out.

### MPL details

```
GET /MessageProcessingLogs('<guid>')/Attachments
GET /MessageProcessingLogs('<guid>')/ErrorInformation/$value
GET /MessageProcessingLogs('<guid>')/RunSteps
```

`RunSteps` gives per-step timing and status — the step-by-step trace.

### Retry / reprocess

```
POST /MessageProcessingLogs('<guid>')/Retry
```

Reprocesses the failed message. Behavior depends on iFlow design:
- If iFlow re-reads from a Data Store / JMS, retry triggers re-fetch
- If iFlow's sender is sync, retry replays from saved payload

Not all messages are retryable. Check `Retryable` flag in MPL.

### Artifact management

```
GET /IntegrationPackages
GET /IntegrationPackages('<id>')/IntegrationDesigntimeArtifacts
GET /IntegrationDesigntimeArtifacts(Id='<id>',Version='active')
POST /IntegrationDesigntimeArtifacts(...)/$value  -- deploy
DELETE /IntegrationRuntimeArtifacts('<id>')       -- undeploy
```

These are the APIs CI/CD pipelines call.

### Adapter management

```
GET /IntegrationAdapterDesigntimeArtifacts
GET /IntegrationRuntimeArtifacts
```

Runtime artifacts = deployed. Useful for "what's actually running."

### Credential / configuration management

Limited public API surface. Most credential ops require UI or specific BTP CLI commands.

```
GET /CredentialsOfUser   -- list (not the secrets)
POST /CredentialsOfUser  -- create/update
```

You can't read back secret values via API. Updates work, reads don't.

---

## Common patterns for Orbit-style tools

### Poll for FAILED messages

```
GET /MessageProcessingLogs?$filter=Status eq 'FAILED' and LogEnd gt datetime'<last-checked>'&$orderby=LogEnd desc&$top=100
```

Then for each result, fetch attachments and error info for RCA.

### Classify and ticket

After fetching MPL details:
1. Apply classifier (error signature map in `sap-cpi-reference.md`)
2. Determine remediation (blast-radius rules)
3. If `SAFE_AUTO`: call Retry endpoint after applying fix
4. If `REVIEW_REQUIRED`: create ITSM ticket with full context

### Bulk reprocess

For a known recovery scenario (e.g., receiver came back online):
```
GET /MessageProcessingLogs?$filter=Status eq 'FAILED' and substringof('503', ErrorInformation/lastErrorModelStepID)
```
Iterate results, call Retry on each.

**Caveat**: bulk retry can hammer the receiver. Throttle, and confirm the receiver is actually ready first.

---

## Common API error signatures

| HTTP code + body | Meaning |
|---|---|
| `401` | Service key expired or wrong scope |
| `403` | Auth OK but lacking permission for that operation |
| `404` MPL not found | Message ID wrong or retention expired |
| `409` | Concurrent modification (deploy while someone else deploying) |
| `429` | Rate limit on management API itself |
| `500 Internal Server Error` | Often transient; retry with backoff |
| `Service key invalid scope` | Service key lacks required scope; recreate with right roles |

---

## Service key roles

When creating the service key for API access, required scopes vary by operation:

| Scope | Required for |
|---|---|
| `ESBMessaging.send` | Calling iFlow endpoints |
| `WebToolingWorkspace.Read` | Reading artifacts |
| `WebToolingWorkspace.Write` | Modifying artifacts |
| `WorkspacePackagesReadDeveloper` | Read packages |
| `AuthGroup.Monitoring` | Read MPL |
| `AuthGroup.Administrator` | Admin ops |

Missing scope = 403. Common debugging: service key created with default scopes, then tries an operation needing more.

---

## Rate limits on management APIs

The management APIs themselves have rate limits (separate from runtime tenant limits). Heavy polling (e.g., MPL every second) will hit them.

Mitigation:
- Use delta queries (`LogEnd gt <last-checked>`) to fetch only new entries
- Backoff on 429
- Cache where possible (artifact metadata changes rarely)

---

## API versioning

CPI has v1 and v2 of various APIs. v2 generally better (more efficient, more features), but not all endpoints have v2. Mixing v1 and v2 calls usually works but check response shapes carefully.

---

## Useful endpoints summary

```
# Monitoring
GET /MessageProcessingLogs
GET /MessageProcessingLogs('<id>')
GET /MessageProcessingLogs('<id>')/RunSteps
GET /MessageProcessingLogs('<id>')/Attachments
POST /MessageProcessingLogs('<id>')/Retry

# Artifacts
GET /IntegrationPackages
GET /IntegrationDesigntimeArtifacts
GET /IntegrationRuntimeArtifacts

# JMS / Data Store
GET /JmsBrokers
GET /DataStores
GET /DataStores('<name>')/Entries

# Tenant info
GET /TenantStatuses
GET /MplTraceEvents
```

---

## Remediation guidance

| Action | Blast radius |
|---|---|
| Calling Retry endpoint after confirmed transient recovery | `SAFE_AUTO` |
| Bulk Retry | `REVIEW_REQUIRED` (volume + downstream impact) |
| Programmatic deploy via API | `REVIEW_REQUIRED` (production change) |
| Reading MPL data for analysis | Always safe; read-only |
