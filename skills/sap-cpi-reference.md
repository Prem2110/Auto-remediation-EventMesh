# SAP Cloud Integration (CPI) — Complete Reference

This skill encodes design patterns, antipatterns, error signatures, and remediation heuristics for all major areas of SAP Cloud Integration. Written for LLM agents (classifier, RCA, fix-generator) and human reference.

---

## How to navigate

Quick-lookup tables and core principles are below. Load reference files on demand based on the failure context:

| If the task involves... | Load |
|---|---|
| A specific adapter's behavior or config | `references/adapters.md` |
| Exception subprocess, DLQ, error routing | `references/error-handling.md` |
| Groovy or JavaScript script steps | `references/scripting.md` |
| Idempotency, retries, dedup | `references/idempotency.md` |
| Message mapping, XSLT, value mapping | `references/mappings.md` |
| Splitter, aggregator, gather, multicast | `references/routing-and-flow-control.md` |
| Credentials, OAuth, keystores, certs | `references/security.md` |
| Cloud Connector, Destinations, network | `references/connectivity.md` |
| Monitoring, MPL, alerting, tracing | `references/monitoring.md` |
| Performance, throughput, memory | `references/performance.md` |
| Transport, CTS+, tenant admin, deploy | `references/tenant-admin.md` |
| OData APIs, public APIs of CPI itself | `references/cpi-apis.md` |

When multiple areas overlap (common), load multiple reference files. The error-signature map below tells you which.

## Agent usage guide

**Classifier** — Use the [Error signature → category map](#error-signature--category-map) and [Adapter quirks](#adapter-quirks-quick-reference). Signatures are pattern-match-friendly.

**RCA** — Use the [Antipattern → symptom](#antipattern--symptom-table) table, then drill into the relevant reference file. Cross-reference symptoms — many incidents have multiple contributing causes (e.g., a credential rotation that exposes a missing retry config).

**Fix generation** — Read [Safe vs. unsafe remediations](#safe-vs-unsafe-remediations) before proposing any change. Only `SAFE_AUTO` fixes apply without approval.

**All agents** — Surface uncertainty (`confidence: low`, fix flagged for review) over guessing. Wrong fixes that look right are worse than no fix. Always prefer escalation over a confident-but-wrong action.

---

## Core CPI design principles

Most failures violate one of these. When an iFlow doesn't follow them, that violation is usually the root cause.

1. **Idempotency at every external boundary.** Receiver calls that mutate state (HTTP POST/PUT, IDoc send, OData create) must be safe to retry. CPI *will* retry on transient failures. See `references/idempotency.md`.

2. **Persistence before processing.** For async flows: receive → persist (JMS/Data Store) → process → ack. Transformation before persistence loses messages on crash.

3. **Explicit exception handling.** Every iFlow needs an Exception Subprocess that captures context, persists the failed message, and emits to alerting. See `references/error-handling.md`.

4. **Headers for routing, properties for state.** Headers leak to receivers; properties don't. Internal state in headers is a top cause of data-leak findings.

5. **One iFlow, one responsibility.** Mega-iFlows with many branches are hard to monitor and remediate.

6. **Externalize environment-specific values.** Hardcoded URLs, credential aliases, or queue names are the #1 reason "works in dev, breaks in prod." Use configure-time parameters.

7. **Validate inputs at the boundary.** Schema-validate at the sender adapter or immediately after. Invalid payloads should fail fast with a descriptive error, not deep in a mapping step.

8. **Log enough for forensic RCA, not so much you breach compliance.** Sensitive fields (PII, credentials, tokens) must never appear in MPL attachments or trace logs. See `references/security.md`.

---

## Error signature → category map

Fast classification. Match substrings, not whole messages — text varies by CPI version.

| Signature pattern | Category | Typical root cause | Reference |
|---|---|---|---|
| `MessagingException` + `503` / `Service Unavailable` | `TRANSIENT_RECEIVER` | Receiver down; safe to retry | adapters |
| `MessagingException` + `401` / `403` | `AUTH_FAILURE` | Credential rotation, expired token, missing scope | security |
| `MessagingException` + `timeout` / `SocketTimeoutException` | `TRANSIENT_RECEIVER` | Receiver slow; check timeout before retry | adapters, performance |
| `Conversion Exception` / `MappingException` | `DATA_QUALITY` | Source payload doesn't match expected schema | mappings |
| `XPathExpressionException` | `MAPPING_BUG` | XPath references node not in payload | mappings |
| `XSLT transformation error` | `MAPPING_BUG` | XSLT logic error or wrong namespace | mappings |
| `groovy.lang.MissingPropertyException` | `SCRIPT_BUG` | Groovy script assumes header/property not set | scripting |
| `groovy.lang.MissingMethodException` | `SCRIPT_BUG` | Method called on null or wrong type | scripting |
| `Duplicate key` / `unique constraint` from receiver | `IDEMPOTENCY_VIOLATION` | Retry without dedup, or upstream double-send | idempotency |
| `JMS queue full` / `MaxOccupancy` | `BACKPRESSURE` | Consumer slower than producer | performance, routing-and-flow-control |
| `Certificate expired` / `PKIX path validation failed` | `CERT_EXPIRY` | Receiver-side cert or keystore entry expired | security |
| `unable to find valid certification path` | `CERT_TRUST` | Missing CA cert in CPI keystore | security |
| `Connection refused` / `UnknownHostException` | `NETWORK` | DNS, firewall, destination misconfig | connectivity |
| `Cloud Connector` + `unreachable` / `not registered` | `CONNECTIVITY_CC` | Cloud Connector down or location ID wrong | connectivity |
| `Camel Exception` + `splitter` | `SPLITTER_FAILURE` | Single bad sub-message; check `SplitComplete=false` | routing-and-flow-control |
| `Camel Exception` + `aggregator` + `timeout` | `AGGREGATOR_TIMEOUT` | Completion condition never met | routing-and-flow-control |
| `OutOfMemoryError` / `Java heap space` | `MEMORY` | Large payload not streamed, or memory leak in script | performance, scripting |
| `Destination not found` / `DestinationException` | `DESTINATION_CONFIG` | Destination name typo or not deployed | connectivity |
| `Lock could not be acquired` (JMS) | `CONCURRENCY` | Multiple consumers, exclusive resource | performance, routing-and-flow-control |
| `Maximum number of messages exceeded` | `RATE_LIMIT` | Tenant or receiver-side throttling | performance, monitoring |
| `Deployment failed` + `validation` | `DESIGN_TIME` | iFlow doesn't pass validation; not a runtime issue | tenant-admin |
| `MPL not found` / `principal propagation failed` | `IDENTITY_PROPAGATION` | SAML/JWT identity chain broken | security, connectivity |

When nothing matches: `UNCATEGORIZED` — surface the raw exception. **Do not invent a category to look confident.**

---

## Antipattern → symptom table

Reverse lookup: given a symptom, find the likely design flaw.

| Symptom | Likely antipattern | Reference |
|---|---|---|
| Duplicate records downstream | Missing idempotency key, or retry-without-dedup | idempotency |
| Messages stuck in `PROCESSING` indefinitely | Sync call without timeout, or JMS consumer crashed | adapters, monitoring |
| Mapping works in dev, fails in prod | Hardcoded env-specific values | mappings, tenant-admin |
| Headers visible to receiver that shouldn't be | Internal state in headers, not properties | scripting |
| Exception subprocess fires, original error lost | Subprocess overwrites `CamelExceptionCaught` | error-handling |
| Large payloads cause OOM | No streaming; whole payload in memory | performance, scripting |
| Random failures only under load | Shared state across parallel executions | scripting, performance |
| Works 99%, fails on edge cases | Mapping assumes optional fields always present | mappings |
| Credential change broke flow days later | Token cache; sessions outlive credential rotation | security |
| Cert renewal broke flow at midnight UTC | Old cert chain still cached; or new cert not propagated | security |
| Cloud Connector "works yesterday, fails today" | Subaccount config drift, or CC restarted | connectivity |
| Throughput degrades over time | JMS queue backlog growing; consumer too slow | performance |
| Splitter processes 99 of 100 messages | One sub-message fails, blocks aggregator | routing-and-flow-control |
| Aggregator never completes | Completion condition never met (count mismatch) | routing-and-flow-control |
| Trace logs work in dev, not in prod | Trace disabled by default in prod | monitoring |
| Deployment succeeds, runtime uses old version | Cache not refreshed; or wrong version deployed | tenant-admin |
| MPL retention shorter than expected | Tenant retention policy, or attachments truncated | monitoring |

---

## Safe vs. unsafe remediations

**Wrong classification here can cause production incidents.** When in doubt, escalate to `REVIEW_REQUIRED`.

### `SAFE_AUTO` — apply without approval

Reversible, bounded, no semantic change:

- Increasing receiver adapter timeout within configured ceiling (e.g., ≤ 5 min)
- Increasing JMS retry count within ceiling (e.g., ≤ 5)
- Adding a null-check in Groovy when the script was the failure point AND the null path has a defined default
- Externalizing a hardcoded value into a configure-time parameter (no logic change)
- Re-processing a failed message from MPL when root cause is `TRANSIENT_RECEIVER` and receiver is now healthy
- Refreshing OAuth token cache after confirmed credential rotation
- Importing a missing intermediate/root CA after verifying provenance via independent channel

### `REVIEW_REQUIRED` — surface to human

Changes semantics or has broader blast radius:

- Any mapping change (message mapping, XSLT, value mapping)
- Adding/removing a router branch
- Changing idempotency strategy
- Changing exception subprocess behavior
- Changing receiver authentication (auth mode, credential alias)
- Anything touching persistence (Data Store, JMS queue config)
- Cloud Connector / Destination changes
- Keystore changes (cert install, key rotation)

### `NEVER_AUTO` — propose only, never execute

- Disabling exception handling to "see the real error"
- Removing idempotency checks
- Catching and swallowing exceptions in Groovy
- Changing endpoint URLs in production
- Bypassing certificate validation (`TrustAll`, hostname verification off)
- Any change in production tenant without validation in dev/QA
- Changes that affect compliance scope (PII handling, audit logging)

The cost of unnecessary human review is one ITSM ticket. The cost of a wrong auto-fix is a production incident.

---

## Adapter quirks quick reference

Most-frequent adapter gotchas. Full detail in `references/adapters.md`.

- **HTTP receiver**: default timeout 1 min, default retry 0. "Intermittent failure" usually = "first-time failure, no retry configured."
- **SOAP receiver**: WS-Addressing, MTOM, WS-Security all off by default; toggling changes wire format.
- **IDoc**: status 51 = receiver got it, app-layer failed (not integration failure). Do not classify as `TRANSIENT_RECEIVER`.
- **OData v2 vs v4**: error envelope path differs (`error.message.value` vs `error.message`); `$filter` operators differ.
- **JMS**: DLQ messages are NOT auto-retried. Explicit reprocessing required.
- **SFTP**: server fingerprint changes look like generic auth failure. Check known-hosts.
- **ProcessDirect**: in-process; failures propagate as inline. Failing step may be in a different iFlow.
- **SuccessFactors / Ariba**: session token caching means auth failures appear N requests after credential rotation. Look at rotation timing, not just the failing request.
- **Mail (IMAP/POP3)**: provider-side throttling silent; partial fetches return success.
- **AMQP**: prefetch settings affect throughput dramatically; default 250 is too high for slow consumers.
- **OpenConnectors**: aggregator layer; provider-specific errors get repackaged. Check the OpenConnectors element for the true upstream error.

---

## Output format

When agents produce a classification or fix proposal:

```json
{
  "category": "<from error-signature map>",
  "subcategory": "<optional, more specific>",
  "confidence": "high|medium|low",
  "root_cause_hypothesis": "<one-sentence summary>",
  "evidence": ["<MPL snippet or config reference>"],
  "affected_areas": ["adapter|mapping|script|security|..."],
  "remediation": {
    "action": "<concrete change>",
    "blast_radius": "SAFE_AUTO|REVIEW_REQUIRED|NEVER_AUTO",
    "rollback": "<how to undo>",
    "validation_steps": ["<how to verify the fix worked>"]
  },
  "uncertainty_notes": "<what you're not sure about>",
  "related_references": ["adapters", "security"]
}
```

Empty `uncertainty_notes` on a low-confidence classification is a red flag — the agent is overconfident.

---

## Scope

This skill covers SAP CPI specifically. It does NOT cover:

- SAP PI/PO (on-premise predecessor) — different runtime, different patterns
- SAP API Management (separate Integration Suite capability)
- SAP Event Mesh (covered only at the integration boundary)
- SAP BTP infrastructure outside CPI (Cloud Foundry app deployment, HANA admin)
- Underlying Apache Camel internals beyond what CPI exposes

If the task is clearly one of these, say so and suggest the appropriate area rather than forcing CPI patterns to fit.
