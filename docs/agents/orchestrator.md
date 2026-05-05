# OrchestratorAgent

**File:** `agents/orchestrator_agent.py`

The top-level coordinator. All incidents flow through the Orchestrator, which routes them through the classification → RCA → remediation pipeline and decides what action to take.

---

## Responsibilities

- Receive incidents from `ObserverAgent` or direct HTTP requests
- Deduplication and correlation of repeated errors
- Route incidents through classification → RCA → remediation
- Enforce the remediation gate (AUTO_FIX / RETRY / APPROVAL / TICKET)
- Expose chatbot interface (`ask()`) for general queries

---

## Key Methods

### `process_detected_error(payload)`

Main entry point for autonomous incident processing.

```
1. Dedup check — compute error_signature; if seen within BURST_DEDUP_WINDOW_SECONDS, skip
2. Create/update incident in HANA (status: DETECTED)
3. ClassifierAgent.classify_error() → {error_type, confidence, tags}
4. Update incident (status: CLASSIFIED)
5. Publish AEM event: sap/cpi/remediation/classified/{incident_id}
6. RCAAgent.run_rca() → {root_cause, proposed_fix, confidence, ...}
7. Update incident (status: RCA_COMPLETE)
8. Publish AEM event: sap/cpi/remediation/rca/{incident_id}
9. remediation_gate(error_type, confidence)
```

### `remediation_gate(error_type, confidence)`

Decides the remediation action based on error type and RCA confidence.

| Condition | Action |
|---|---|
| Policy = AUTO_FIX **and** confidence ≥ `AUTO_FIX_CONFIDENCE` | `execute_incident_fix()` |
| Policy = RETRY | `VerifierAgent.retry_failed_message()` |
| Policy = APPROVAL **or** confidence < `SUGGEST_FIX_CONFIDENCE` | Set status `AWAITING_APPROVAL` |
| Policy = TICKET_CREATED | `create_escalation_ticket()` |

### `execute_incident_fix(incident_id, user_id)`

Human-triggered or approved fix pipeline:

```
1. Pre-flight: FixAgent.verify_iflow_exists()
2. Snapshot current iFlow XML (stored in iflow_snapshot_before)
3. RCA if not already done
4. FixAgent.ask_fix_and_deploy()
5. VerifierAgent.test_iflow_after_fix()
6. Update HANA: status → FIX_VERIFIED or FIX_FAILED
7. Upsert FIX_PATTERNS on success
```

### `ask(query, user_id)`

General chatbot interface. Detects "fix" intent keywords and routes to `execute_incident_fix()` if matched. Otherwise delegates to a full-toolset LangChain agent.

---

## Remediation Policies

Defined in `core/constants.py` under `REMEDIATION_POLICIES`:

| Error Type | Action | Replay After Fix |
|---|---|---|
| `MAPPING_ERROR` | AUTO_FIX | Yes |
| `DATA_VALIDATION` | AUTO_FIX | Yes |
| `AUTH_ERROR` | AUTO_FIX | Yes |
| `CONNECTIVITY_ERROR` | RETRY | Yes |
| `ADAPTER_CONFIG_ERROR` | AUTO_FIX | Yes |
| `BACKEND_ERROR` | TICKET_CREATED | No |
| `SFTP_ERROR` | TICKET_CREATED | No |
| `UNKNOWN_ERROR` | APPROVAL | No |

---

## Deduplication

The Orchestrator uses `ClassifierAgent.error_signature()` to compute a 16-character MD5 key from `(iflow_id, error_type, error_message)`. Incidents with the same signature within `BURST_DEDUP_WINDOW_SECONDS` are skipped, incrementing `occurrence_count` on the existing record instead.

---

## AEM Events Published

| Topic | Trigger |
|---|---|
| `sap/cpi/remediation/observed/{incident_id}` | New incident detected |
| `sap/cpi/remediation/classified/{incident_id}` | Classification complete |
| `sap/cpi/remediation/rca/{incident_id}` | RCA complete |
| `sap/cpi/remediation/fix/{incident_id}` | Fix applied |
| `sap/cpi/remediation/verified/{incident_id}` | Verification complete |

---

## Dependencies

- `ClassifierAgent` — injected at construction
- `RCAAgent` — injected at construction
- `FixAgent` — injected at construction
- `VerifierAgent` — injected at construction
- `ObserverAgent` — calls `set_orchestrator(self)` during wiring
- `db.database` — all HANA persistence
- `core.state.FIX_PROGRESS` — in-memory progress tracker
- `aem.event_bus.AEMEventBus` — event publishing
