# RCAAgent

**File:** `agents/rca_agent.py`

LLM-powered root cause analysis. Combines vector store search, cross-iFlow pattern lookup, and selective MCP tool calls to produce a structured diagnosis.

---

## Key Method

### `run_rca(incident) -> dict`

Returns:

```python
{
    "root_cause": str,
    "proposed_fix": str,
    "confidence": float,        # 0.0 – 1.0
    "affected_component": str,
    "key_steps": list[str]      # Ordered remediation steps
}
```

If LLM confidence < classifier confidence, the result falls back to `ClassifierAgent.fallback_root_cause()`.

---

## Tool Sequence

The LLM is instructed to call tools in this order (max 6 calls total):

```
1. get_vector_store_notes(query)     ← FIRST — retrieve relevant SAP Notes
2. get_cross_iflow_patterns(sig)     ← look for proven fixes in pattern DB
3. get-iflow(iflow_id)               ← read current XML config (ONCE)
4. get_message_logs(message_guid)    ← inspect payload if GUID provided (AT MOST ONCE)
```

**Excluded tools:** `update-iflow`, `deploy-iflow`, `unlock-iflow` — the tool list is filtered to read-only before building the RCA agent. This makes it structurally impossible for RCA to modify or deploy anything.

---

## LLM Prompt Structure

The RCA prompt instructs the LLM to:

1. Retrieve relevant SAP Notes first
2. Check cross-iFlow pattern history for proven fixes
3. Inspect the current iFlow XML
4. Optionally check message logs for the specific failed payload
5. Return a strictly structured JSON response

Required JSON output:

```json
{
  "root_cause": "Detailed explanation of why the error occurred",
  "proposed_fix": "Step-by-step fix instructions for the FixAgent",
  "confidence": 0.88,
  "affected_component": "MessageMapping_1",
  "key_steps": [
    "Update XPath expression in MessageMapping_1",
    "Verify target field name matches XSD schema"
  ]
}
```

---

## Vector Store Integration

`utils/vector_store.py` provides `retrieve_relevant_notes(query, top_k=5)`.

- Embeds the query using `text-embedding-3-large` (3072 dimensions)
- Searches `SAP_HELP_DOCS` table in HANA using cosine similarity
- Returns top-K SAP Notes as formatted text for the LLM prompt

The vector store is populated by `scrape_sap_docs.py` + `vectorize_docs.py`.

---

## Cross-iFlow Pattern Lookup

`db.database.get_similar_patterns(signature, error_type)` fetches patterns from `FIX_PATTERNS` table where:

- Same error type, OR
- Same error signature from a previous incident

Patterns with `success_count >= PATTERN_MIN_SUCCESS_COUNT` are included in the RCA context. This allows the system to reuse a proven fix without re-running the full LLM analysis.

---

## Confidence Arbitration

```
rca_confidence = LLM output confidence
classifier_confidence = ClassifierAgent.classify_error().confidence

if rca_confidence >= classifier_confidence:
    use RCA result
else:
    use ClassifierAgent.fallback_root_cause()
    confidence = classifier_confidence
```

---

## Error Guidance

`core/constants.py` defines `ERROR_TYPE_FIX_GUIDANCE` — a dict of per-error-type hints that are injected into the RCA prompt. For example:

- `MAPPING_ERROR` → "Check XPath expressions and field name alignment with the XSD schema"
- `AUTH_ERROR` → "Verify credential alias name in Security Material and check expiry"
