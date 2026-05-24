# Connectivity: Cloud Connector, Destinations, Network

For when failure involves connectivity to on-premise systems, Destinations, Cloud Connector, or network-level issues.

---

## SAP BTP Destination Service

CPI uses Destinations from BTP Destination Service to abstract:
- URL (per-environment)
- Authentication type and credentials
- Proxy type (Internet vs OnPremise)
- Additional properties (timeouts, headers)

Destinations referenced by name in iFlows. Same iFlow can use different Destinations per environment.

### Common destination issues

- **Destination not found** → name mismatch (case-sensitive), or destination not deployed to the subaccount, or wrong subaccount selected.
- **Proxy Type wrong** → `Internet` for public endpoints, `OnPremise` for via Cloud Connector. Mixing up causes "connection refused" or "timeout."
- **URL trailing slash** → some endpoints care, some don't. Be consistent.
- **Authentication mismatch** → destination configured for one auth type, iFlow expects another.

### Destination properties for CPI

Useful additional properties:
- `HTML5.DynamicDestination=true` — allows runtime override
- `cloudConnectorLocationId=<id>` — for multi-CC scenarios
- `TrustAll=true` — **never in production**
- `HTTP.timeout=<ms>` — connection timeout override

---

## Cloud Connector

Cloud Connector bridges BTP to on-premise systems. CPI in BTP calls "internal" URLs; Cloud Connector translates to actual on-prem URLs.

### Architecture

1. CC installed in customer network (on-prem or DMZ)
2. CC connects outbound to BTP via persistent tunnel
3. CC exposes specific on-prem resources to specific subaccounts
4. CPI uses Destination with Proxy Type `OnPremise` to route via CC

### Common CC issues

- **CC not running** → all on-prem-bound traffic fails. Check CC dashboard.
- **CC tunnel disconnected** → CC running but not connected to BTP. Usually network/firewall.
- **Subaccount not authorized** → CC exposes resources per subaccount; missing mapping → "not registered."
- **Resource not exposed** → CC running, subaccount authorized, but specific URL not in CC's exposed resources.
- **Location ID mismatch** → for multi-CC setups, destination's `cloudConnectorLocationId` must match a registered CC.
- **CC version mismatch** → some features need recent CC. Check CC version against feature requirements.

### CC location IDs

When multiple CCs serve the same subaccount (e.g., multi-region or DR setup), location ID distinguishes them. Missing or wrong location ID → CC layer picks default (often wrong one).

### CC backend types

CC exposes resources by type:
- HTTP — most common
- RFC — ABAP RFC calls
- LDAP — directory access
- TCP — generic TCP tunneling

Type mismatch → CC refuses the request even if URL/host appear correct.

---

## Principal propagation via CC

For on-prem calls preserving user identity:

1. iFlow receives request with SAML/JWT identity
2. CPI extracts identity, forwards to CC with assertion
3. CC validates and exchanges for on-prem identity (often via STS)
4. On-prem system sees the request as from the actual user

Requires:
- CC configured for principal propagation
- IDP trust between BTP and on-prem (often via STS or SAP NW SSO)
- User identity mapping (cloud user ↔ on-prem user)

See `references/security.md` for failure modes.

---

## Network-level issues

### DNS resolution

`UnknownHostException` for a name that should resolve:
- BTP-side DNS issue (rare, usually transient)
- For on-prem via CC, the hostname is internal — must be in CC's exposed resources, not resolvable from BTP directly
- For Internet destinations with custom DNS, check the destination's DNS configuration

### Firewall / network policy

`Connection refused` / `Connection timeout`:
- Internet destination: check receiver's firewall allows BTP IP ranges
- OnPremise destination: check CC's network policy allows the on-prem target
- Both: check no intermediate proxy blocking

### Proxy configuration

BTP has its own proxy when going to Internet via subaccount-level proxy config. If destination is `Internet` type but the receiver is in a network reachable only via a corporate proxy → connection fails.

---

## TLS specifics

TLS handshake failures (not HTTP-level):
- `SSL handshake aborted` — receiver rejected handshake
- `Protocol version mismatch` — CPI offers TLS 1.2+, receiver needs older (security risk to downgrade)
- `Cipher suite mismatch` — no common cipher between CPI and receiver
- `Certificate validation failed` — see `references/security.md`

---

## Error signatures

| Pattern | Meaning |
|---|---|
| `UnknownHostException` | DNS resolution failed |
| `Connection refused` | Target reachable but refused — service not listening |
| `Connection timeout` | Target unreachable in time — firewall, distance, load |
| `Destination ... not found` | Destination name mismatch or not deployed |
| `Cloud Connector` + `not registered` | CC down or subaccount mapping missing |
| `Cloud Connector` + `not reachable` | CC tunnel disconnected |
| `Backend ... not exposed` | CC running but resource not configured |
| `Principal propagation failed` | Identity chain through CC broken |
| `Proxy authentication required` | Corporate proxy needs auth, not configured |

---

## Diagnostic approach

1. Identify destination type (Internet vs OnPremise) from MPL.
2. If OnPremise: check CC dashboard status before anything else.
3. If Internet: check if receiver itself is reachable (independent of CPI).
4. Check if other iFlows using the same destination also fail (isolate destination vs iFlow).
5. Check timing — simultaneous failure across multiple iFlows = infrastructure; single iFlow = config.

---

## Remediation guidance

| Action | Blast radius |
|---|---|
| Adjusting destination timeout within ceiling | `SAFE_AUTO` |
| Destination URL change | `REVIEW_REQUIRED` |
| Switching destination auth type | `REVIEW_REQUIRED` |
| CC restart | `NEVER_AUTO` (requires CC admin access) |
| Adding to CC exposed resources | `NEVER_AUTO` (manual CC config) |
| Bulk transport | `NEVER_AUTO` |
