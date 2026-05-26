# Premortem Transcript — SAP CPI Self-Healing Agent
**Date:** 2026-05-26  
**Frame:** It is 6 months from now. The project has failed.

---

## Context Gathered

**What is it?**  
A SAP CPI Self-Healing Agent built by Sierra Digital. It autonomously monitors SAP Cloud Integration for failed iFlow message runs, performs LLM-based root cause analysis, and applies fixes via a get-iflow → update-iflow → deploy-iflow pipeline through 3 MCP servers.

**Who is it for?**  
Sierra Digital's enterprise clients running SAP Cloud Integration (CPI). Managed by Premkumar's team at Sierra Digital.

**What does success look like?**  
The agent autonomously resolves iFlow errors reliably enough that clients stop routing incidents to manual support. Fix rate high enough to be trusted. No false success signals. Safe to run overnight without supervision.

---

## Raw Failure Reasons

1. Fix rate stays too low to earn enterprise trust — confidence scoring measures RCA completeness, not fix correctness
2. SAP API surface changes break the pipeline silently — mock-based tests pass, live behavior changes undetected
3. HANA Cloud outage causes loop crash + double-processing on recovery, corrupting in-flight transactions
4. System can't fix what matters most — Groovy/XSLT/mmap errors are not remediable but this is never surfaced clearly
5. Async deploy creates false "Fix Applied" signals — HTTP 202 ≠ deployment success
6. OAuth token expiry mid-fix leaves iFlows locked in modified-but-undeployed state
7. Zero enforced test coverage — silent DB write failures trigger infinite re-fix loops
8. Settings page is an unguarded kill switch — no threshold floor, no audit log, no access control

---

## Agent Deep-Dives

### Failure #1 — Fix rate stays too low to be trusted

**Story:** The value-search fallback was a patch on a misdiagnosis. The real problem was that LLM RCA produced structurally plausible but semantically wrong fixes for the majority of error types. The +0.10 confidence boost pushed borderline cases (0.82 → 0.92) into auto-fix — but those were the cases that previously routed correctly to manual review. They now deployed in a broken state. The client saw actively broken deployments, not just unresolved alerts. By month 3, they had an informal rule: flag every agent-touched iFlow for manual review before go-live. By month 5, the agent was generating more work, not less. The confidence score measured RCA completeness (all 4 fields present), not fix correctness. A confidently wrong answer scored 0.95.

**Underlying Assumption:** A high RCA confidence score correlates with a correct fix, when it only measures structural completeness of the LLM's output.

**Early Warning Signs:**
- Deploy success rate drops while RCA confidence scores trend upward — the two metrics diverge in logs within the first 2 weeks of the confidence boost going live.
- The same iFlow error type reappears in the incident table on consecutive days — the agent "fixed" it, redeployed, and it broke again.

---

### Failure #2 — SAP API surface change breaks pipeline silently

**Story:** SAP's Q1 quarterly update moved iFlow XML from the `iflowContent` key to `artifactContent`. The `_unwrap_getiflow_response` function checked only the old key; when absent, returned raw JSON. The LLM received outer JSON, not XML, and generated patches against phantom nodes. `update-iflow` accepted the payload (SAP validated the file container, not XML semantics). `deploy-iflow` changed from HTTP 202 to HTTP 200 with `"status":"queued"` — the keyword matcher didn't match, but the orchestrator's LLM-evaluation path overrode and marked success. For 3 weeks, every "Fix Applied" was fiction. A client escalation revealed zero successful deployments since the platform update date.

**Underlying Assumption:** HTTP status codes and keyword-matched strings in MCP tool responses are a reliable, stable proxy for actual SAP platform behavior.

**Early Warning Signs:**
- get-iflow output length dropped sharply in logs — `<XML len=~400>` instead of `<XML len=~8000+>`. A size threshold alert would fire within the first hour.
- fix_applied=True with deploy_success=False across consecutive incidents — detectable with a daily SQL query.

---

### Failure #3 — HANA outage causes double-processing and data corruption

**Story:** HANA entered a 45-minute maintenance window during business hours. The loop hit `HDBException: connection refused`, retried in a tight loop, exhausted its stack, and crashed. CF's health check (HTTP endpoint only) declared the app healthy — loop stayed dead for 4 hours with no alert. When HANA came back, the loop found 23 open incidents including several already manually resolved. With no idempotency key and no processing lock, it re-ran the full fix pipeline on all of them. Three iFlows were redeployed mid-message. Two corrupted in-flight payloads. The client pulled the agent's credentials that afternoon.

**Underlying Assumption:** HANA Cloud would always be available when the loop ran, so incident state transitions never needed to be idempotent or recoverable.

**Early Warning Signs:**
- The incident table had no `processing_at` column — any engineer reading the schema could see concurrent replay was structurally impossible to prevent.
- Integration test re-runs against shared dev database produced duplicate fix attempts, closed as "test harness issues" rather than missing idempotency.

---

### Failure #4 — System can't fix what clients actually break (Groovy/XSLT)

**Story:** In the client's production environment, 65% of iFlow failures originated in Groovy scripts and XSLT transformations. The system classified these correctly, ran full RCA, then stalled at the confidence gate every time — "insufficient confidence to auto-fix." This happened hundreds of times a month. The dashboard showed high incident volume with a 4% resolution rate. The client concluded the agent was malfunctioning, not operating as designed for the wrong problem. The limitation was never surfaced in the UI — only a low confidence score where clients expected action.

**Underlying Assumption:** The errors reachable by the fix engine represented the majority of errors the target client actually experienced.

**Early Warning Signs:**
- During pilot, message log analysis consistently showed Groovy-originated stack traces — the team focused on incidents that resolved, not those that didn't.
- Confidence score distribution skewed heavily below threshold from week one. Nobody asked why the median was so low.

---

### Failure #5 — Async deploy creates false "Fix Applied" history

**Story:** The agent treated HTTP 202 from deploy-iflow as deployment success. Quota exceeded errors, XML schema violations, and dependency conflicts all returned 202 and then failed asynchronously. The agent closed these as fixed. No polling loop, no status check, no reconciliation. Fixed iFlows reappeared as new incidents with identical error signatures 24–48 hours later. Six months in, a client compliance audit cross-referenced the agent's fix history against SAP CPI deployment logs. 23% of "Fix Applied" records had no corresponding STARTED state in SAP. The client concluded the agent was fabricating success records.

**Underlying Assumption:** HTTP 202 from SAP CPI is a reliable proxy for eventual deployment success, rather than merely an acknowledgment that the request was queued.

**Early Warning Signs:**
- iFlows marked "Fix Applied" reappearing as new incidents within 24–48 hours with identical error signatures.
- SAP CPI cockpit showing iFlows in persistent STARTING or ERROR state that the agent's database listed as STARTED.

---

### Failure #6 — OAuth expiry leaves iFlows locked mid-fix

**Story:** During a long overnight session, OAuth tokens (4-hour TTL) expired between update-iflow and deploy-iflow on 3 consecutive fixes. update-iflow had succeeded — iFlows were checked out and modified. deploy-iflow returned 401. The error handler logged the 401 and moved on without rollback or unlock. The pipeline's cleanup logic only handled "locked" errors on entry, not after partial success. By morning, 3 production iFlows were locked in modified-but-undeployed state. Subsequent fix attempts hit the locked state and skipped. The client's integration team spent half a day manually unlocking and reverting.

**Underlying Assumption:** Token refresh failure would only occur before a pipeline started, never between an already-committed write and its mandatory follow-on deploy.

**Early Warning Signs:**
- In staging, deploy-iflow occasionally logged a 401 on first attempt — nobody verified whether the retry was fetching a fresh token or reusing the expired one.
- A small count of "unresolved" incidents after sessions longer than 3.5 hours — treated as noise, not as a locked-state signal.

---

### Failure #7 — Zero test coverage makes every refactor a gamble

**Story:** A HANA schema refactor changed the physical column naming convention. `update_incident(incident_id, {"status": "AUTO_FIXED"})` silently skipped the write when the column lookup returned None — no exception raised. The logger emitted a warning nobody was watching. On the next loop iteration, the incident was still `FIX_IN_PROGRESS` and picked up again. The loop began re-fixing the same 5 iFlows in parallel. Within 12 hours, SAP AI Core token consumption triggered rate limiting (429s). All remediation capability went down for 48 hours across the shared tenant. No tests existed for `update_incident` verifying persistence. The CLAUDE.md 80% coverage standard was never enforced by CI.

**Underlying Assumption:** Because update_incident never raised an exception, it had successfully written to the database.

**Early Warning Signs:**
- Structured logs showing `update_incident: skipping unknown column 'status'` warnings after the schema migration — no alert wired to this pattern.
- FIX_IN_PROGRESS incident count plateaued and grew instead of resolving on the dashboard.

---

### Failure #8 — Settings page is an unguarded kill switch

**Story:** A junior IT admin at the client lowered the auto-fix confidence threshold from 0.90 to 0.50, thinking he was tuning sensitivity like a spam filter. No warning, no role check, no confirmation dialog, no audit entry. The system accepted it silently. Within 48 hours, the agent was deploying fixes previously routed to manual review. Weak RCA swapped credential aliases on 3 iFlows. Wrong endpoint URLs substituted on 2 more from low-confidence web search results. All 5 deployed successfully from a technical standpoint. Business process failures surfaced 2 days later. The incident response team needed to reconstruct who changed the threshold, when, and which fixes ran under degraded confidence. None of that data existed.

**Underlying Assumption:** Settings page access would be self-regulating because only technical staff would use it.

**Early Warning Signs:**
- UAT sign-off never tested what happened when the confidence threshold was set below a safe floor — no validation rule existed.
- The first production deploy had no change log for `_runtime_cfg` values — "what were the settings when this fix ran?" had no answer from day one.

---

## Synthesis

**Most Likely Failure:** Failure #5 — Async deploy false success. Already a known open gap (in project memory as MCP opportunity #4). Until post-deploy status polling is implemented, the fix history is untrustworthy and any client audit will expose the discrepancy.

**Most Dangerous Failure:** Failure #8 — Settings page without guardrails. A single misconfigured confidence threshold causes the agent to auto-deploy incorrect fixes to production iFlows, corrupting live business transactions. No audit trail. Contract-terminating scenario.

**Hidden Assumption (across all 8 failures):** "If the pipeline completes without throwing an exception, the fix was applied correctly." This underlies 5 of 8 failures: SAP API surface change, async deploy, silent DB write failure, OAuth expiry mid-fix, and confidence score mismatch. The system verifies that each step didn't crash — it does not verify that the intended outcome was achieved.

**Revised Plan:**
1. Add post-deploy status polling — poll every 10s up to 3 min, only mark "Fix Applied" on STARTED state
2. Add settings page minimum floor (0.75) with server-side validation + audit log persisted to DB
3. Add incident idempotency lock (`processing_at` column) to prevent double-processing after HANA outages
4. Add pre-emptive unlock-iflow at fix start (already in project memory as MCP opportunity #2 — one-liner)
5. Show "Outside remediation scope — manual review required" for Groovy/XSLT errors instead of generic low-confidence

**Pre-Delivery Checklist:**
- [ ] Post-deploy polling implemented and verified against real SAP CPI deployment
- [ ] Settings: minimum threshold floor enforced server-side, change audit log persisted
- [ ] Incident idempotency: processing lock column added, loop eligibility query updated
- [ ] Dashboard distinguishes "outside scope" from "low confidence" for Groovy/XSLT errors
- [ ] HANA outage test: simulate, verify clean recovery without double-processing
