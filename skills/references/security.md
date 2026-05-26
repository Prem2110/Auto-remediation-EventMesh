# Security: Credentials, OAuth, Keystores, Certificates

For when failure involves auth, certificate validation, key material, or principal propagation.

---

## Credential types in CPI

| Type | Used for |
|---|---|
| User Credentials | Basic auth (username/password) |
| OAuth2 Credentials | OAuth2 client credentials, SAML bearer, etc. |
| OAuth2 SAML Bearer Assertion | SuccessFactors, S/4HANA Cloud |
| OAuth2 Authorization Code | User-context flows (rare in iFlows) |
| Known Hosts | SSH/SFTP server fingerprints |
| Secure Parameter | Single secret values (API keys) |
| PGP Public/Private Keyring | Message-level encryption |
| Keystore Entries | TLS certs, signing keys |

Credentials referenced by alias in iFlows; rotation happens in the credential, not the iFlow.

---

## OAuth2 patterns

### Client credentials grant

Most common for server-to-server. Token requested with client_id/client_secret, valid for N minutes (provider-dependent).

**Token caching**: CPI caches the token until expiry. After credential rotation:
- Old tokens remain valid until their TTL
- New requests get new tokens
- Failures appear *delayed* — N minutes/hours after rotation, when cached tokens start expiring

This is the #1 reason credential-rotation failures don't surface immediately.

### SAML bearer assertion grant

Used when caller has a SAML assertion (often from BTP destination service or IDP). The assertion is exchanged for an OAuth token at the provider.

Failure modes:
- Assertion expired (typically short-lived, e.g., 10 minutes)
- Assertion signed with wrong key
- Audience/issuer mismatch
- Clock skew between CPI and provider

### JWT bearer

Similar to SAML bearer but with JWT format. Same failure modes.

---

## Keystore management

CPI tenant has a keystore for TLS material. Common operations:

- Import server certificate (for TLS to receiver)
- Import CA / intermediate certs
- Generate/import client key pair (for mutual TLS)
- Import signing/encryption keys (for message-level security)

### Certificate validation chain

When CPI connects to a receiver via TLS:
1. Receiver presents its cert
2. CPI verifies signature chain to a trusted root in the CPI keystore
3. CPI checks hostname matches cert CN/SAN
4. CPI checks cert not expired

Failure at any step → `PKIX path validation failed` or `unable to find valid certification path`.

### Certificate expiry — the silent killer

Expiry timeline:
- T-30 days: cert reaching end of life (often unnoticed)
- T-0: expiry — all connections to that receiver fail simultaneously
- T+seconds: massive ticket spike

**Prevention**: monitoring iFlow that checks cert expiry dates in keystore, alerts at T-30, T-14, T-7.

**Detection**: classifier should recognize "all messages to receiver X failing simultaneously starting at time T" as likely cert expiry on either CPI side or receiver side.

### Self-signed certificates

`unable to find valid certification path` for a known receiver — likely self-signed cert not in CPI trust store. Verify provenance via independent channel (don't blindly import what the receiver presents over the failing connection itself).

---

## Principal propagation

For on-premise calls via Cloud Connector with user-context propagation:

1. CPI receives request with user identity (SAML or JWT)
2. CPI forwards via Cloud Connector with identity
3. Cloud Connector exchanges for on-prem identity
4. On-prem system processes as that user

Failure modes:
- IDP not configured on both sides
- User exists in cloud but not on-prem (or vice versa)
- SAML signing key changed without coordination
- Clock skew (assertions short-lived)

Error: `principal propagation failed` or `no identity`.

---

## Common security antipatterns

### TrustAll on TLS

Disables cert validation entirely. May appear in dev to bypass cert issues; if it survives to prod, it's a MITM vulnerability.

**Fix**: never `TrustAll` in production. Import the correct certs.

### Hostname verification disabled

Cert validates but hostname doesn't match cert CN/SAN. Toggling hostname verification off "to make it work" is a vulnerability.

**Fix**: get correct cert with matching hostname, or correct DNS so hostname matches.

### Credentials in headers as plaintext

Setting `Authorization: Basic <base64>` via a Content Modifier with hardcoded value. Anyone with read access to the iFlow sees the credential.

**Fix**: use credential alias + adapter auth config, not Content Modifier headers.

### Secrets in script literals

```groovy
def apiKey = "abc123def456"
```

Visible to anyone with iFlow read access. Also: doesn't rotate.

**Fix**: SecureStore API to fetch from credential alias.

### Long-lived tokens stored as properties

```groovy
def token = obtainToken()
message.setProperty("authToken", token)
```

If property persists across iFlow boundaries via JMS persistence, token is now in MPL/storage indefinitely. Compliance issue.

**Fix**: tokens stay in-memory only; never persist; never log.

---

## Message-level security

For B2B / EDI scenarios, message-level security layers on top of transport security:

- **PGP/GPG**: file encryption (common for SFTP scenarios)
- **XML Digital Signature**: signed XML payloads
- **XML Encryption**: encrypted XML payloads
- **WS-Security**: SOAP-level signing/encryption

Common issues:
- Key rotation without partner coordination → all messages fail
- Algorithm mismatch (e.g., SHA-1 deprecated, partner moved to SHA-256)
- Canonicalization differences in XML signing

---

## Audit logging

Security-relevant events are auditable at the tenant level:
- Credential changes
- Keystore changes
- iFlow deployment/undeployment
- Role assignments

When unexplained behavior change correlates with a tenant config change, audit log is the place to look.

---

## Error signatures

| Pattern | Meaning |
|---|---|
| `401 Unauthorized` (after period of success) | Token expired / credential rotated |
| `403 Forbidden` | Auth succeeded, authorization failed (scope/role) |
| `PKIX path validation failed` | Cert chain doesn't validate |
| `unable to find valid certification path` | Missing trust anchor (CA) in keystore |
| `Certificate expired` | Receiver or local cert past expiry |
| `Hostname verification failed` | Cert valid but hostname mismatch |
| `Principal propagation failed` | Identity chain broken |
| `Signature verification failed` | Message-level signature invalid |
| `Decryption failed` | Wrong key for encrypted message |

---

## Remediation guidance

| Action | Blast radius |
|---|---|
| Credential rotation | `REVIEW_REQUIRED` |
| CA chain cert import (after independent verification) | `SAFE_AUTO` |
| New server cert import | `REVIEW_REQUIRED` |
| Token cache refresh after confirmed rotation | `SAFE_AUTO` |
| Disabling any validation | `NEVER_AUTO` |
