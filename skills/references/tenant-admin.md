# Tenant Administration: Deployment, Transport, Configuration

For when the task involves deployment issues, transport between environments, or tenant configuration.

---

## Tenant model

CPI tenants are typically organized as a Dev / Test / Prod triplet (or more environments). Each tenant has independent: artifacts, credentials, keystores, destinations, JMS queues, MPL data.

Transport moves *artifacts* between tenants — not credentials, not config, not JMS state.

This separation is essential and frequently misunderstood. "Deploying to prod" means deploying the iFlow, not the supporting infrastructure that lives in each tenant.

---

## Design-time vs runtime

### Design-time
- Editing iFlows in Integration Suite UI or via API
- Stored in the tenant's design repository (DTR)
- Not yet executable

### Runtime
- Deployment compiles the design artifact into a runtime artifact
- Runtime artifacts execute when messages arrive
- Undeploy stops execution but leaves the design artifact

A common confusion: "I changed the iFlow but the behavior didn't change." Often the iFlow was edited in design but not re-deployed.

---

## Deployment

Deploying an iFlow:
1. Validates artifact (schemas, references, dependencies)
2. Deploys runtime artifact
3. Sender adapters become active (start listening)
4. Existing in-flight messages on old version may continue on old version, depending on adapter

Deployment failures:
- `Validation failed` → artifact has issues; not a runtime concern
- `Resource conflict` → another iFlow listening on same path/queue
- `Credential alias not found` → credential referenced doesn't exist in this tenant

### Cutover behavior

When redeploying a running iFlow:
- New incoming messages use new version immediately
- In-flight messages: typically complete on old version (graceful shutdown)
- JMS consumer: redeploy can cause brief gap; consumers reconnect

---

## Configuration-time parameters

iFlows can declare configurable parameters (URLs, credentials, queue names) that vary per environment. Set at deploy time, not in artifact.

### Pattern: externalize everything env-specific

Anything that differs between dev/test/prod should be a configure-time parameter:
- Endpoint URLs
- Credential aliases
- Queue names
- Feature flags
- Schedule expressions

If hardcoded in the artifact, you've coupled artifact to environment and the same artifact won't deploy cleanly to all environments.

### Common config pitfalls

- **Default value used in prod**: param has a dev-friendly default, deployer forgets to override.
- **Required without enforcement**: param is "optional" with empty default but iFlow expects a value → silent misbehavior in prod.
- **Stale config after artifact update**: artifact adds a new param; existing deployment doesn't get it; default applies (which may be wrong).

---

## Transport (moving artifacts between tenants)

### Direct export/import
- Export artifact to `.zip`, import to target tenant, reconfigure parameters at target.
- Manual, error-prone for large changesets.

### CTS+ (Change and Transport System+)
- SAP's enterprise transport tool. Centralized, auditable. Required in many enterprise SAP shops.

### Content Agent Service
- BTP-native transport. Integrated with BTP role/permission model.

### CI/CD via API
- Public APIs allow programmatic export/import. Combined with Git for source control. Common in modern setups.

### Pitfalls of any transport

- **Tenant-specific references break**: credentials, destinations, queues exist per-tenant. Transported artifact references them by name; if name doesn't exist in target, deployment fails.
- **Version coupling**: artifact transported from dev might depend on artifacts/libraries not yet in target.
- **Partial transport**: dependent artifacts forgotten, transported artifact references missing dependencies.

---

## Roles and permissions

CPI roles in BTP:

| Role | Access |
|---|---|
| `AuthGroup.IntegrationDeveloper` | Design and deploy |
| `AuthGroup.BusinessExpert` | Limited design (mappings, value mappings) |
| `AuthGroup.Administrator` | Tenant config |
| `AuthGroup.Monitoring` | Read MPL |
| `MessageSendUser` | Runtime calls to CPI |

Common issue: developer can deploy in dev but not prod (intentional). Deployment failures in prod with `403` are often role issues, not artifact issues.

---

## Tenant-level configuration

Things configured per-tenant (not per-iFlow):
- MPL retention period
- Default trace duration
- Tenant-wide credentials and keystores
- Destinations (in subaccount, used by tenant)
- JMS queue definitions
- Adapter version availability

Changes to these affect *all* iFlows. Coordinate before changing.

---

## Version management of iFlows

CPI maintains version history per artifact:
- Each save creates a new version
- Deploy is of a specific version
- Old versions can be redeployed (rollback)

For RCA: "what changed when?" is answerable from version history + audit log.

---

## Common deployment-time error signatures

| Pattern | Meaning |
|---|---|
| `Validation failed: schema` | Schema issue in iFlow definition |
| `Resource conflict: address already in use` | Another iFlow on same endpoint |
| `Credential alias '...' not found` | Credential not in target tenant |
| `Destination '...' not found` | Destination not in subaccount |
| `Queue '...' not found` | JMS queue not pre-provisioned |
| `Package locked` | Concurrent edit; release lock |
| `Insufficient privileges` | Role mismatch for action |

---

## DR / Disaster Recovery

For tenants with DR:
- Primary tenant ↔ DR tenant pair
- DR is read-only normally, activated on failover
- Artifact transport keeps both in sync
- JMS state does NOT replicate; in-flight messages may be lost on failover

DR-active iFlows must tolerate replay (idempotency again). The DR scenario is essentially a giant retry.

---

## Remediation guidance

| Action | Blast radius |
|---|---|
| Redeploying an iFlow | `REVIEW_REQUIRED` (always — production change) |
| Configure-time parameter change | `REVIEW_REQUIRED` |
| Creating missing JMS queue | `REVIEW_REQUIRED` (tenant config change) |
| Rolling back to previous version | `REVIEW_REQUIRED` |
| Bulk transport | `NEVER_AUTO` (typically requires CTS+ approval workflow) |
