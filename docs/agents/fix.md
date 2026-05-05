# FixAgent

**File:** `agents/fix_agent.py`

LLM-driven fix and deploy pipeline. The most critical agent — it modifies live iFlow XML and deploys to SAP CPI.

---

## Key Methods

### `ask_fix_and_deploy(incident) -> dict`

Returns:

```python
{
    "fix_applied": bool,
    "deploy_success": bool,
    "update_response": str,
    "deploy_response": str,
    "summary": str
}
```

### `verify_iflow_exists(iflow_id) -> bool`

Pre-flight check via `get-iflow`. Returns `False` if the iFlow is not found, preventing the pipeline from running against a non-existent artifact.

### `get_deploy_error_details(iflow_id) -> str`

Fetches SAP deployment logs after a failed `deploy-iflow` call. Used for detailed failure diagnosis.

---

## Fix Execution Steps

The LLM is given `FIX_AND_DEPLOY_PROMPT_TEMPLATE` from `core/constants.py` and instructed to execute these steps in strict order:

```
Step 1: get-iflow(iflow_id)
        → Analyse full XML thoroughly

Step 2: (if message_guid provided)
        get_message_logs(message_guid)
        → Inspect actual failed payload

Step 3: Determine minimal XML change
        → Apply ONLY what the proposed_fix requires
        → Do not change versions, IDs, or structure

Step 4: update-iflow(iflow_id, modified_xml, filepath)
        → filepath MUST match exactly what get-iflow returned

Step 5: deploy-iflow(iflow_id)
        → Always deploy after a successful update
```

---

## Pre-Update Validation

Before calling `update-iflow`, the agent runs the XML through `core/validators.py`:

```python
errors = validate_iflow_xml(modified_xml)
if errors:
    return {"fix_applied": False, "summary": f"Validation failed: {errors}"}
```

The validator checks 7 structural rules. See [SAP CPI XML Patterns](../rules/iflow-xml-patterns.md) for the full rule list.

---

## Lock Handling

If `update-iflow` returns a response containing `"is locked"`:

```
1. Call unlock-iflow(iflow_id)
2. Retry update-iflow once
3. If still locked → stop; return fix_applied=False
```

This is handled automatically by the LLM using prompt instructions and tool response inspection.

---

## Timeout Diagnosis

If the LLM agent times out (default 600s):

```python
status = await multi_mcp.execute("check-runtime-status", {"iflow_id": iflow_id})
# Determines which step (update or deploy) the agent reached before timeout
```

The result is logged and included in the incident record.

---

## Prompt Context

The `FIX_AND_DEPLOY_PROMPT_TEMPLATE` is built with these variables:

| Variable | Source |
|---|---|
| `iflow_id` | Incident record |
| `error_type` | ClassifierAgent output |
| `message_guid` | Incident record (may be empty) |
| `error_message` | Original SAP error |
| `root_cause` | RCAAgent output |
| `proposed_fix` | RCAAgent output |
| `affected_component` | RCAAgent output |
| `pattern_history` | Matched FIX_PATTERNS from HANA |

---

## Groovy Script Reference

For fixes involving Groovy scripts, `core/constants.py` provides:

- `GROOVY_STRIPE_HTTP_ADAPTER` — reference implementation for HTTP adapter
- `GROOVY_WOOCOMMERCE_HTTP_ADAPTER` — reference implementation for e-commerce adapter

These are injected into the fix prompt when the error type or affected component suggests a Groovy script issue.

---

## CPI Editing Rules

The FixAgent prompt includes `CPI_IFLOW_MESSAGE_MAPPING_RULES` and `CPI_IFLOW_GROOVY_RULES` from `core/constants.py`, and `CPI_IFLOW_XML_PATTERNS` from `rules/sap_cpi_iflow_xml_patterns.md`.

See [SAP CPI XML Patterns](../rules/iflow-xml-patterns.md) for the complete constraints the LLM must follow.
