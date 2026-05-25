# Fix-Rate Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise the Orbit auto-remediation fix rate by applying 8 targeted, independent improvements to the fix pipeline — from direct-patch value matching through to extracting Groovy/XSLT scripts from the iFlow ZIP.

**Architecture:** Each task is a self-contained code change to one or two files. Tasks are ordered by size (smallest first). No task depends on another being merged first. All use existing interfaces — no new external dependencies.

**Tech Stack:** Python 3.13, FastAPI, LangChain, SAP HANA via hdbcli, xml.etree.ElementTree, pytest + pytest-asyncio

---

## File Map

| Task | Files modified |
|---|---|
| 1 | `agents/fix_planner.py` |
| 2 | `db/database.py`, `agents/rca_agent.py` |
| 3 | `agents/rca_agent.py` |
| 4 | `db/database.py`, `agents/orchestrator_agent.py`, `agents/fix_supervisor.py` |
| 5 | `agents/verifier_agent.py` |
| 6 | `agents/orchestrator_agent.py` |
| 7 | `agents/rca_agent.py` |
| 8 | `agents/fix_context.py`, `agents/fix_planner.py`, `agents/rca_agent.py` |

Tests live under `tests/agents/` and `tests/db/`.

---

## Task 1: Direct-Patch Value Normalization

**Problem:** The value-search fallback in `_apply_direct_patch` uses bare `.strip()`. SAP CPI designer emits values with HTML entities (`&amp;`, `&lt;`, `&#x2F;`), CDATA wrappers, and trailing whitespace. These cause value-search to miss the element even though the value is logically identical.

**Files:**
- Modify: `agents/fix_planner.py` — `_apply_direct_patch` static method and its `value_to_props` build loop

- [ ] **Step 1: Write the failing test**

Create `tests/agents/test_fix_planner_normalize.py`:

```python
import pytest
from agents.fix_planner import FixPlanner

_XML_ENTITY = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns:ifl="http://sap.com/xi/mapping/ifl">
  <ifl:property id="ReceiverHTTP_1">
    <ifl:key>Address</ifl:key>
    <ifl:value>https://api.example.com/v1/orders&amp;format=json</ifl:value>
  </ifl:property>
</root>"""

_XML_CDATA = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns:ifl="http://sap.com/xi/mapping/ifl">
  <ifl:property id="ReceiverHTTP_1">
    <ifl:key>Address</ifl:key>
    <ifl:value><![CDATA[https://api.example.com/v1/orders]]></ifl:value>
  </ifl:property>
</root>"""

def test_value_search_matches_html_entity():
    """Value-search must find a property when current_value uses & but XML stores &amp;."""
    fixes = [{
        "component_id":      "WRONG_ID",
        "property_to_change": "Address",
        "current_value":     "https://api.example.com/v1/orders&format=json",
        "correct_value":     "https://api.example.com/v2/orders&format=json",
    }]
    result = FixPlanner._apply_direct_patch(_XML_ENTITY, fixes)
    assert result is not None
    assert "v2/orders" in result

def test_value_search_matches_cdata():
    """Value-search must find a property whose value is wrapped in CDATA."""
    fixes = [{
        "component_id":      "WRONG_ID",
        "property_to_change": "Address",
        "current_value":     "https://api.example.com/v1/orders",
        "correct_value":     "https://api.example.com/v2/orders",
    }]
    result = FixPlanner._apply_direct_patch(_XML_CDATA, fixes)
    assert result is not None
    assert "v2/orders" in result
```

- [ ] **Step 2: Run to confirm tests fail**

```
uv run python -m pytest tests/agents/test_fix_planner_normalize.py -v
```
Expected: FAILED — both tests fail because bare `.strip()` doesn't unescape entities.

- [ ] **Step 3: Add `_normalize_value` helper and wire it in**

In `agents/fix_planner.py`, add this static method immediately before `_apply_direct_patch`:

```python
@staticmethod
def _normalize_value(v: str) -> str:
    """Normalize an iFlow property value for equality comparison.

    SAP CPI designer stores values with HTML entities (&amp; → &),
    CDATA wrappers, and inconsistent whitespace. Normalize both sides
    before comparing so value-search finds matches regardless of encoding.
    """
    import html as _html  # noqa: PLC0415
    v = (v or "").strip()
    # Strip CDATA wrapper: <![CDATA[...]]>
    if v.startswith("<![CDATA[") and v.endswith("]]>"):
        v = v[9:-3]
    # Unescape HTML entities (&amp; → &, &lt; → <, &#x2F; → /, etc.)
    v = _html.unescape(v)
    return v.strip()
```

Then update `_apply_direct_patch` in two places:

**Place 1** — building `value_to_props` (currently line ~287). Change:
```python
_vt = (_val_el.text or "").strip()
if _vt:
    value_to_props.setdefault(_vt, []).append((_key_el, _val_el))
```
To:
```python
_vt = FixPlanner._normalize_value(_val_el.text or "")
if _vt:
    value_to_props.setdefault(_vt, []).append((_key_el, _val_el))
```

**Place 2** — looking up in Strategy 2 (currently line ~361). Change:
```python
matches = value_to_props.get(old_value.strip(), [])
```
To:
```python
matches = value_to_props.get(FixPlanner._normalize_value(old_value), [])
```

- [ ] **Step 4: Run tests — expect PASS**

```
uv run python -m pytest tests/agents/test_fix_planner_normalize.py -v
```
Expected: PASSED (both tests).

- [ ] **Step 5: Commit**

```
git add agents/fix_planner.py tests/agents/test_fix_planner_normalize.py
git commit -m "feat: normalize HTML entities and CDATA in direct-patch value-search"
```

---

## Task 2: Rich Few-Shot Patterns in RCA Prompt

**Problem:** `EM_FIX_PATTERNS` stores successful fixes as plain text in `fix_applied` / `root_cause`. RCA gets them as prose summaries. If the stored row also carried the structured `fixes[]` JSON, the RCA LLM could see a worked example of the exact output format it must produce — improving both format compliance and value accuracy.

**Files:**
- Modify: `db/database.py` — `_migrate_fix_patterns_table`, `upsert_fix_pattern`
- Modify: `agents/rca_agent.py` — `run_rca` prompt assembly for `cross_iflow_text`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_fix_pattern_fixes_json.py`:

```python
import json
import pytest
from unittest.mock import patch, MagicMock

def _make_conn():
    """In-memory SQLite connection that mimics HANA enough for these tests."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE EM_FIX_PATTERNS (
        pattern_id TEXT PRIMARY KEY, error_signature TEXT, iflow_id TEXT,
        error_type TEXT, root_cause TEXT, fix_applied TEXT, outcome TEXT,
        applied_count INTEGER DEFAULT 0, last_seen TEXT, success_count INTEGER DEFAULT 0,
        replay_success_count INTEGER DEFAULT 0, key_steps TEXT, FIXES_JSON TEXT
    )""")
    conn.commit()
    return conn

def test_upsert_stores_fixes_json(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)

    from db.database import upsert_fix_pattern
    upsert_fix_pattern({
        "error_signature": "sig1",
        "iflow_id":        "iflow_A",
        "error_type":      "HTTP_CALL_FAILED",
        "root_cause":      "wrong address",
        "fix_applied":     "corrected address",
        "outcome":         "SUCCESS",
        "fixes_json":      json.dumps([{
            "component_id": "ReceiverHTTP_1",
            "property_to_change": "Address",
            "current_value": "http://old",
            "correct_value": "https://new",
        }]),
    })

    row = conn.execute("SELECT FIXES_JSON FROM EM_FIX_PATTERNS WHERE error_signature='sig1'").fetchone()
    assert row is not None
    stored = json.loads(row["FIXES_JSON"])
    assert stored[0]["component_id"] == "ReceiverHTTP_1"
```

- [ ] **Step 2: Run to confirm it fails**

```
uv run python -m pytest tests/db/test_fix_pattern_fixes_json.py -v
```
Expected: FAILED — `FIXES_JSON` column doesn't exist yet and `upsert_fix_pattern` doesn't read `fixes_json` from data.

- [ ] **Step 3: Add `FIXES_JSON` column to migration**

In `db/database.py`, `_migrate_fix_patterns_table`, add to `_NEEDED`:

```python
("FIXES_JSON", "fixes_json", "NCLOB"),
```

- [ ] **Step 4: Persist `fixes_json` in `upsert_fix_pattern`**

In `db/database.py`, `upsert_fix_pattern`, after the existing `key_steps_json` build:

```python
fixes_json_str = data.get("fixes_json") or None
```

Then in the INSERT (both branches), add `FIXES_JSON` to the column list and `fixes_json_str` to values:

For the `_has_success_count=True` INSERT branch, change:
```python
f"""INSERT INTO "{_FIX_TABLE}"
   (pattern_id, error_signature, iflow_id, error_type,
    root_cause, fix_applied, outcome, applied_count, last_seen,
    "success_count", "replay_success_count", "key_steps")
   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
(
    str(uuid.uuid4()), sig,
    data.get("iflow_id", ""), data.get("error_type", ""),
    data.get("root_cause", ""), fix, outcome, 1, now,
    1 if outcome == "SUCCESS" else 0,
    1 if replay_success else 0,
    key_steps_json if outcome == "SUCCESS" else None,
),
```
To:
```python
f"""INSERT INTO "{_FIX_TABLE}"
   (pattern_id, error_signature, iflow_id, error_type,
    root_cause, fix_applied, outcome, applied_count, last_seen,
    "success_count", "replay_success_count", "key_steps", "FIXES_JSON")
   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
(
    str(uuid.uuid4()), sig,
    data.get("iflow_id", ""), data.get("error_type", ""),
    data.get("root_cause", ""), fix, outcome, 1, now,
    1 if outcome == "SUCCESS" else 0,
    1 if replay_success else 0,
    key_steps_json if outcome == "SUCCESS" else None,
    fixes_json_str if outcome == "SUCCESS" else None,
),
```

Apply the same `"FIXES_JSON"` addition to the UPDATE and the `_has_success_count=False` branches.

- [ ] **Step 5: Wire `fixes_json` into `upsert_fix_pattern` call in `agents/orchestrator_agent.py`**

Find the `upsert_fix_pattern` call at line ~616. Add `fixes_json` to the data dict:

```python
import json as _json
...
upsert_fix_pattern({
    "error_signature": self._classifier.error_signature(...),
    "iflow_id":   incident["iflow_id"],
    "error_type": rca.get("error_type", ""),
    "root_cause": rca.get("root_cause", ""),
    "fix_applied": _fix_applied_desc,
    "outcome":    _outcome,
    "key_steps":  fix_result.get("steps", []) if fix_result.get("fix_applied") else [],
    "fixes_json": _json.dumps(
        [vars(f) if hasattr(f, "__dict__") else f for f in rca.get("fixes", [])]
    ) if (outcome == "SUCCESS" and rca.get("fixes")) else None,
})
```

- [ ] **Step 6: Inject `fixes_json` as few-shot examples in RCA prompt**

In `agents/rca_agent.py`, find the `cross_iflow_text` assembly block (~lines 514-530). After building `lines`, add:

```python
for _cp in _cross_patterns:
    if _cp.get("iflow_id") != iflow_id:
        _fj = _cp.get("fixes_json") or _cp.get("FIXES_JSON") or ""
        _fex = ""
        if _fj:
            try:
                _fex = f" | fixes_example: {_fj[:300]}"
            except Exception:
                pass
        lines.append(
            f"iFlow: {_cp.get('iflow_id')} | "
            f"root_cause: {(_cp.get('root_cause') or '')[:120]} | "
            f"fix: {(_cp.get('fix_applied') or '')[:150]} | "
            f"success_count: {_cp.get('success_count', 0)}"
            f"{_fex}"
        )
```

- [ ] **Step 7: Run tests — expect PASS**

```
uv run python -m pytest tests/db/test_fix_pattern_fixes_json.py -v
```

- [ ] **Step 8: Commit**

```
git add db/database.py agents/rca_agent.py agents/orchestrator_agent.py tests/db/test_fix_pattern_fixes_json.py
git commit -m "feat: store and inject fixes[] JSON as few-shot examples in RCA prompt"
```

---

## Task 3: RCA Self-Consistency Retry with Component Hint

**Problem:** `rca_agent.py` already detects when `current_value` from RCA is not found in the iFlow XML (lines 783-806) and caps confidence at 0.75. But it doesn't retry — it just logs a warning and lets a wrong fix reach `direct_patch`. Adding one targeted retry when mismatches are detected (injecting the real component IDs) converts "cap + hope" into "retry + correct".

**Files:**
- Modify: `agents/rca_agent.py` — `run_rca` method, after the mismatch detection block

- [ ] **Step 1: Write the failing test**

Create `tests/agents/test_rca_consistency_retry.py`:

```python
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

GOOD_XML = """<?xml version="1.0"?>
<root id="Collab_1">
  <process id="ReceiverHTTP_1">
    <ifl:property xmlns:ifl="http://sap.com/xi/mapping/ifl" id="ReceiverHTTP_1">
      <ifl:key>Address</ifl:key>
      <ifl:value>http://old.example.com</ifl:value>
    </ifl:property>
  </process>
</root>"""

@pytest.mark.asyncio
async def test_rca_retries_on_component_mismatch():
    """When RCA references WRONG_COMPONENT (not in XML), agent should be invoked a second time."""
    first_rca = json.dumps({
        "root_cause": "wrong address",
        "proposed_fix": "fix address",
        "confidence": 0.85,
        "auto_apply": False,
        "error_type": "HTTP_CALL_FAILED",
        "affected_component": "WRONG_COMPONENT",
        "property_to_change": "Address",
        "current_value": "http://nonexistent",
        "correct_value": "http://new.example.com",
        "fixes": [{"component_id": "WRONG_COMPONENT", "property_to_change": "Address",
                   "current_value": "http://nonexistent", "correct_value": "http://new.example.com"}],
    })
    second_rca = json.dumps({
        "root_cause": "wrong address",
        "proposed_fix": "fix address",
        "confidence": 0.88,
        "auto_apply": False,
        "error_type": "HTTP_CALL_FAILED",
        "affected_component": "ReceiverHTTP_1",
        "property_to_change": "Address",
        "current_value": "http://old.example.com",
        "correct_value": "http://new.example.com",
        "fixes": [{"component_id": "ReceiverHTTP_1", "property_to_change": "Address",
                   "current_value": "http://old.example.com", "correct_value": "http://new.example.com"}],
    })

    invoke_count = 0
    async def _fake_invoke(payload, config=None):
        nonlocal invoke_count
        invoke_count += 1
        answer = first_rca if invoke_count == 1 else second_rca
        msg = MagicMock()
        msg.content = answer
        return {"messages": [msg]}

    mock_agent = MagicMock()
    mock_agent.ainvoke = _fake_invoke

    mock_mcp = MagicMock()
    mock_mcp.agent = mock_agent
    mock_mcp.tools = []

    # Patch DB and vector store so no real calls happen
    with patch("agents.rca_agent.get_similar_patterns", return_value=[]), \
         patch("agents.rca_agent.get_vector_store") as mock_vs, \
         patch("db.database.get_patterns_by_error_type", return_value=[]), \
         patch("db.database.log_agent_event"):
        mock_vs.return_value.retrieve_relevant_notes.return_value = []
        mock_vs.return_value.format_notes_for_prompt.return_value = ""

        from agents.rca_agent import RCAAgent
        agent = RCAAgent(mock_mcp)
        agent._agent = mock_agent
        agent._current_error_type = "HTTP_CALL_FAILED"
        agent._current_iflow_id = "TestIflow"

        # Pre-populate iFlow XML into step history so mismatch check fires
        from agents.base import StepLogger, TestExecutionTracker
        with patch.object(agent, "_agent", mock_agent), \
             patch("agents.rca_agent.asyncio.gather", new=AsyncMock(return_value=[
                 [], [], [], (MagicMock(retrieve_relevant_notes=lambda *a, **k: [],
                               format_notes_for_prompt=lambda x: ""), []),
                 GOOD_XML, ""])):
            result = await agent.run_rca({
                "iflow_id": "TestIflow",
                "error_message": "Connection refused",
                "error_type": "HTTP_CALL_FAILED",
                "message_guid": "",
            })

    # After mismatch → retry, second invocation uses correct component
    assert invoke_count == 2
    assert result["affected_component"] == "ReceiverHTTP_1"
```

- [ ] **Step 2: Run to confirm it fails**

```
uv run python -m pytest tests/agents/test_rca_consistency_retry.py -v
```
Expected: FAILED — agent is invoked only once (no retry today).

- [ ] **Step 3: Add retry logic in `run_rca` after the mismatch block**

In `agents/rca_agent.py`, find the block starting at `if _mismatched:` (line ~784). After the `logger.warning` and `final_confidence = min(...)` line, add:

```python
            # ── One targeted retry: inject the real component IDs so the LLM can correct ──
            _real_ids = sorted(
                {f.split("}")[1] if "}" in f else f
                 for f in re.findall(r'id=["\']([^"\']+)["\']', _iflow_xml_raw)}
            )[:30]
            _hint = (
                f"\n\nCORRECTION REQUIRED: your previous answer referenced component IDs that "
                f"do not exist in the iFlow XML: {[m.component_id if hasattr(m, 'component_id') else m.get('component_id', '') for m in _mismatched]}. "
                f"The actual available IDs are: {_real_ids}. "
                f"Re-examine the iFlow XML and return corrected JSON using only these IDs.\n"
            )
            _retry_messages = messages + [
                {"role": "assistant", "content": answer},
                {"role": "user",     "content": _hint},
            ]
            try:
                _retry_result = await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": _retry_messages},
                        config={"callbacks": [logger_cb], "recursion_limit": 6},
                    ),
                    timeout=120.0,
                )
                _retry_msg  = _retry_result["messages"][-1]
                _retry_ans  = _retry_msg.content if hasattr(_retry_msg, "content") else str(_retry_msg)
                _retry_clean = re.sub(r"```(?:json)?|```", "", _retry_ans).strip()
                _retry_rca   = json.loads(_retry_clean)
                # Only accept retry if it fixes the mismatch
                _retry_fixes = get_all_fixes(_retry_rca)
                _still_bad = [
                    f for f in _retry_fixes
                    if (f.current_value if hasattr(f, "current_value") else f.get("current_value", ""))
                    and (f.current_value if hasattr(f, "current_value") else f.get("current_value", ""))
                    not in _iflow_xml_raw
                ]
                if not _still_bad:
                    logger.info(
                        "[RCA] Consistency retry succeeded — mismatch resolved for iflow=%s",
                        iflow_id,
                    )
                    rca      = _retry_rca
                    all_fixes = _retry_fixes
                    final_confidence = min(
                        float(rca.get("confidence", final_confidence)), 0.95
                    )
                else:
                    logger.warning(
                        "[RCA] Consistency retry did not resolve mismatch for iflow=%s — "
                        "keeping capped confidence=%.2f", iflow_id, final_confidence,
                    )
            except Exception as _retry_exc:
                logger.warning(
                    "[RCA] Consistency retry failed for iflow=%s: %s", iflow_id, _retry_exc
                )
```

- [ ] **Step 4: Run tests — expect PASS**

```
uv run python -m pytest tests/agents/test_rca_consistency_retry.py -v
```

- [ ] **Step 5: Commit**

```
git add agents/rca_agent.py tests/agents/test_rca_consistency_retry.py
git commit -m "feat: retry RCA with component hint when current_value mismatch detected"
```

---

## Task 4: Strategy Ordering by Historical Priors

**Problem:** `_STRATEGY_ORDER` in `fix_supervisor.py` is a fixed list: `["direct_patch", "component_replace", "structured", "free_xml"]`. For some error types (e.g. AUTH_CONFIG_ERROR), `component_replace` succeeds 90% of the time but the supervisor still tries `direct_patch` first and burns an attempt on it. The `SUPERVISOR_STRATEGY` column already exists in `EM_FIX_PATTERNS` (via migration) but nothing writes to or reads from it yet.

**Files:**
- Modify: `db/database.py` — `upsert_fix_pattern` (write `supervisor_strategy`), new query `get_strategy_success_by_error_type`
- Modify: `agents/orchestrator_agent.py` — pass `supervisor_strategy` to `upsert_fix_pattern`
- Modify: `agents/fix_supervisor.py` — query priors and reorder strategies before first attempt

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_strategy_priors.py`:

```python
import pytest
from unittest.mock import patch, MagicMock

def _make_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE EM_FIX_PATTERNS (
        pattern_id TEXT PRIMARY KEY, error_signature TEXT, iflow_id TEXT,
        error_type TEXT, root_cause TEXT, fix_applied TEXT, outcome TEXT,
        applied_count INTEGER DEFAULT 0, last_seen TEXT, success_count INTEGER DEFAULT 0,
        replay_success_count INTEGER DEFAULT 0, key_steps TEXT, FIXES_JSON TEXT,
        SUPERVISOR_STRATEGY TEXT
    )""")
    conn.execute("""INSERT INTO EM_FIX_PATTERNS
        (pattern_id, error_signature, iflow_id, error_type, root_cause, fix_applied, outcome,
         applied_count, last_seen, success_count, SUPERVISOR_STRATEGY)
        VALUES ('p1','sig1','A','AUTH_CONFIG_ERROR','rc','fix','SUCCESS',3,'2026-01-01',3,'component_replace')""")
    conn.execute("""INSERT INTO EM_FIX_PATTERNS
        (pattern_id, error_signature, iflow_id, error_type, root_cause, fix_applied, outcome,
         applied_count, last_seen, success_count, SUPERVISOR_STRATEGY)
        VALUES ('p2','sig2','B','AUTH_CONFIG_ERROR','rc2','fix2','FAILED',1,'2026-01-02',0,'direct_patch')""")
    conn.commit()
    return conn

def test_get_strategy_success_returns_ranked(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    from db.database import get_strategy_success_by_error_type
    result = get_strategy_success_by_error_type("AUTH_CONFIG_ERROR")
    assert result[0]["strategy"] == "component_replace"
    assert result[0]["success_count"] >= 1

def test_get_strategy_success_empty_for_unknown(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    from db.database import get_strategy_success_by_error_type
    assert get_strategy_success_by_error_type("UNKNOWN_XYZ") == []
```

- [ ] **Step 2: Run to confirm it fails**

```
uv run python -m pytest tests/db/test_strategy_priors.py -v
```
Expected: FAILED — `get_strategy_success_by_error_type` doesn't exist yet.

- [ ] **Step 3: Add `get_strategy_success_by_error_type` to `db/database.py`**

Append after `get_patterns_by_error_type`:

```python
def get_strategy_success_by_error_type(error_type: str) -> List[Dict]:
    """
    Return per-strategy success counts for a given error_type, ranked by
    success_count DESC.  Used by FixSupervisor to reorder strategy attempts.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f"""SELECT "SUPERVISOR_STRATEGY" AS strategy,
                       SUM("success_count")  AS success_count,
                       COUNT(*)              AS total_patterns
                FROM "{_FIX_TABLE}"
               WHERE error_type=?
                 AND "SUPERVISOR_STRATEGY" IS NOT NULL
                 AND "SUPERVISOR_STRATEGY" != ''
               GROUP BY "SUPERVISOR_STRATEGY"
               ORDER BY SUM("success_count") DESC""",
            (error_type,),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        return rows
    except Exception as exc:
        logger.error("get_strategy_success_by_error_type: %s", exc)
        return []
```

- [ ] **Step 4: Write `supervisor_strategy` in `upsert_fix_pattern`**

In `upsert_fix_pattern`, after `fixes_json_str = ...`, add:

```python
supervisor_strategy = data.get("supervisor_strategy") or None
```

In the INSERT (success_count branch):
```python
f"""INSERT INTO "{_FIX_TABLE}"
   (pattern_id, error_signature, iflow_id, error_type,
    root_cause, fix_applied, outcome, applied_count, last_seen,
    "success_count", "replay_success_count", "key_steps", "FIXES_JSON", "SUPERVISOR_STRATEGY")
   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
(
    str(uuid.uuid4()), sig,
    data.get("iflow_id", ""), data.get("error_type", ""),
    data.get("root_cause", ""), fix, outcome, 1, now,
    1 if outcome == "SUCCESS" else 0,
    1 if replay_success else 0,
    key_steps_json if outcome == "SUCCESS" else None,
    fixes_json_str if outcome == "SUCCESS" else None,
    supervisor_strategy,
),
```

Apply the same for the UPDATE query and the `_has_success_count=False` branches.

- [ ] **Step 5: Pass `supervisor_strategy` from `orchestrator_agent.py`**

In the `upsert_fix_pattern` call at ~line 616:

```python
upsert_fix_pattern({
    ...
    "supervisor_strategy": fix_result.get("strategy_used", ""),
})
```

- [ ] **Step 6: Reorder strategies in `fix_supervisor.py`**

At the top of the `supervise` method, before the `for attempt in range(...)` loop, add:

```python
        # Query historical strategy priors and reorder for this error_type.
        # Falls back to _STRATEGY_ORDER if DB is unavailable or has no data.
        _ordered_strategies = list(_STRATEGY_ORDER)
        try:
            from db.database import get_strategy_success_by_error_type  # noqa: PLC0415
            _priors = get_strategy_success_by_error_type(ctx.error_type)
            if _priors:
                _prior_rank = [r["strategy"] for r in _priors if r.get("success_count", 0) > 0]
                _fallback   = [s for s in _STRATEGY_ORDER if s not in _prior_rank]
                _ordered_strategies = _prior_rank + _fallback
                logger.info(
                    "[Supervisor] Strategy order from priors for error_type=%s: %s",
                    ctx.error_type, _ordered_strategies,
                )
        except Exception as _pe:
            logger.debug("[Supervisor] Could not fetch strategy priors: %s", _pe)
```

Then change the loop to use `_ordered_strategies` instead of `_STRATEGY_ORDER` in `_next_strategy` calls. Update `_next_strategy` to accept an optional `order` param, or pass `_ordered_strategies` as a closure by making it `nonlocal`:

Replace the `_next_strategy` calls at lines ~233 and ~265:
```python
next_strat = self._next_strategy(strat_name, tried_strategies)
```
With:
```python
next_strat = self._next_strategy(strat_name, tried_strategies, _ordered_strategies)
```

Update the `_next_strategy` static method signature:
```python
@staticmethod
def _next_strategy(current: str, tried: set, order: Optional[List[str]] = None) -> Optional[str]:
    for s in (order or _STRATEGY_ORDER):
        if s not in tried:
            return s
    return None
```

- [ ] **Step 7: Run tests — expect PASS**

```
uv run python -m pytest tests/db/test_strategy_priors.py -v
```

- [ ] **Step 8: Commit**

```
git add db/database.py agents/orchestrator_agent.py agents/fix_supervisor.py tests/db/test_strategy_priors.py
git commit -m "feat: reorder fix strategies by per-error-type historical success rate"
```

---

## Task 5: Verifier Post-Deploy Message Log Re-Check

**Problem:** The verifier only checks iFlow runtime status and optionally sends a test payload. It never calls `get_message_logs` for the original `message_guid` to confirm the original error signature is gone. If the error reoccurs immediately (e.g. the target system is still down), the verifier marks `FIX_VERIFIED` while the iFlow is already failing again.

**Files:**
- Modify: `agents/verifier_agent.py` — add `get-message-logs` to tool set + update system prompt

- [ ] **Step 1: Write the failing test**

Create `tests/agents/test_verifier_log_check.py`:

```python
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

def test_verifier_includes_message_log_tool():
    """After build_agent, the verifier's tool list must include a message-log tool."""
    mock_mcp = MagicMock()
    log_tool = MagicMock()
    log_tool.server = "integration_suite"
    log_tool.name = "get_message_logs"
    log_tool.mcp_tool_name = "get_message_logs"
    log_tool.description = "Retrieve message processing logs"

    test_tool = MagicMock()
    test_tool.server = "mcp_testing"
    test_tool.name = "test_iflow_with_payload"
    test_tool.mcp_tool_name = "test_iflow_with_payload"
    test_tool.description = "Test iFlow"

    mock_mcp.tools = [log_tool, test_tool]
    mock_mcp.build_agent = AsyncMock()

    from agents.verifier_agent import VerifierAgent
    verifier = VerifierAgent(mock_mcp)
    asyncio.run(verifier.build_agent())

    tool_names = [str(t) for t in mock_mcp.build_agent.call_args[1].get("tools", [])]
    assert any("message_log" in n.lower() or "message-log" in n.lower() for n in tool_names), \
        "get_message_logs must be in verifier tool set"
```

- [ ] **Step 2: Run to confirm it fails**

```
uv run python -m pytest tests/agents/test_verifier_log_check.py -v
```
Expected: FAILED — no message-log tool is included today.

- [ ] **Step 3: Add message-log tool to verifier in `verifier_agent.py`**

In `build_agent`, after the block that adds `get_iflow_tool`, add:

```python
        # Add get_message_logs (read-only) so verifier can confirm the original
        # error signature is absent from the latest run after deploy.
        get_logs_tool = next(
            (t for t in self._mcp.tools
             if any(kw in f"{t.name} {t.mcp_tool_name}".lower()
                    for kw in ("message_log", "message-log"))),
            None,
        )
        if get_logs_tool:
            all_tools.append(get_logs_tool)
```

- [ ] **Step 4: Update system prompt to instruct log re-check**

In the verifier system prompt, after step 4 (`call test_iflow_with_payload`), add step 5:

```python
system_prompt = """You are an SAP CPI post-fix verification agent.
...
5. (Optional) If a message_guid is provided: call get_message_logs for that GUID.
   - If the latest log entry still shows the original error type: set test_passed=false
     and include "Original error persists in message log" in summary.
   - If the log is absent or shows success: no action needed.
...
"""
```

Also update the `test_iflow_after_fix` prompt (lines ~250-270) to add:

```python
f"""
5. If message_guid is available ('{incident.get("message_guid", "")}'):
   Call get_message_logs for that GUID. If the latest run still shows the original
   error — set test_passed=false: "Original error still present in message log."
"""
```

- [ ] **Step 5: Run tests — expect PASS**

```
uv run python -m pytest tests/agents/test_verifier_log_check.py -v
```

- [ ] **Step 6: Commit**

```
git add agents/verifier_agent.py tests/agents/test_verifier_log_check.py
git commit -m "feat: verifier re-checks message log after deploy to confirm error is gone"
```

---

## Task 6: Auto-Retry on FIX_FAILED_RUNTIME

**Problem:** When the verifier writes `FIX_FAILED_RUNTIME`, the pipeline halts and a ticket is created. If the fix actually worked but exposed a *new* downstream error (different error type / different affected component), that new error should be fed back into the pipeline rather than escalated as a manual fix.

**Files:**
- Modify: `agents/orchestrator_agent.py` — in `execute_incident_fix`, after verifier returns `FIX_FAILED_RUNTIME`, check if runtime error differs from the original; if so, re-create the incident.

- [ ] **Step 1: Write the failing test**

Create `tests/agents/test_fix_failed_runtime_retry.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_new_error_after_fix_creates_new_incident():
    """If verifier shows a *different* error, orchestrator must create a new incident."""
    ...
    # This is a smoke test — asserts create_incident is called with the new error_type
    created_incidents = []

    with patch("agents.orchestrator_agent.create_incident", side_effect=lambda d: created_incidents.append(d) or "new-id"), \
         patch("agents.orchestrator_agent.update_incident"), \
         patch("agents.orchestrator_agent.get_incident", return_value={
             "incident_id": "orig-id",
             "iflow_id":    "iflow_A",
             "error_type":  "HTTP_CALL_FAILED",
             "error_message": "Connection refused to /v1/orders",
             "message_guid": "guid1",
         }):
        from agents.orchestrator_agent import _maybe_requeue_runtime_failure
        await _maybe_requeue_runtime_failure(
            original_incident={"incident_id": "orig-id", "iflow_id": "iflow_A",
                                "error_type": "HTTP_CALL_FAILED", "error_message": "Connection refused"},
            runtime_error_type="AUTH_CONFIG_ERROR",
            runtime_error_msg="401 Unauthorized — credential alias 'prod_cred' not found",
        )

    assert len(created_incidents) == 1
    assert created_incidents[0]["error_type"] == "AUTH_CONFIG_ERROR"
```

- [ ] **Step 2: Run to confirm it fails**

```
uv run python -m pytest tests/agents/test_fix_failed_runtime_retry.py -v
```
Expected: FAILED — `_maybe_requeue_runtime_failure` doesn't exist.

- [ ] **Step 3: Add `_maybe_requeue_runtime_failure` helper to `orchestrator_agent.py`**

Add this module-level async function near the other helper functions (~line 70):

```python
async def _maybe_requeue_runtime_failure(
    original_incident: Dict,
    runtime_error_type: str,
    runtime_error_msg: str,
) -> bool:
    """
    If the post-deploy runtime error differs from the original error, create a new
    incident so the pipeline can attempt a fix for the newly-exposed failure.
    Returns True if a new incident was queued, False otherwise.
    """
    orig_type = (original_incident.get("error_type") or "").strip()
    if not runtime_error_type or runtime_error_type == orig_type:
        return False
    if not runtime_error_msg:
        return False
    logger.info(
        "[OrchestratorAgent] FIX_FAILED_RUNTIME — original=%s, new=%s for iflow=%s — "
        "re-queuing as new incident",
        orig_type, runtime_error_type, original_incident.get("iflow_id", ""),
    )
    try:
        from db.database import create_incident  # noqa: PLC0415
        create_incident({
            "iflow_id":      original_incident.get("iflow_id", ""),
            "error_type":    runtime_error_type,
            "error_message": runtime_error_msg[:2000],
            "message_guid":  original_incident.get("message_guid", ""),
            "parent_incident_id": original_incident.get("incident_id", ""),
            "status":        "CLASSIFIED",
        })
        return True
    except Exception as exc:
        logger.warning("[OrchestratorAgent] _maybe_requeue_runtime_failure: %s", exc)
        return False
```

- [ ] **Step 4: Call it after `FIX_FAILED_RUNTIME` in `execute_incident_fix`**

Find where the verifier result is processed in `orchestrator_agent.py`. After the status is set to `FIX_FAILED_RUNTIME`, add:

```python
# Check if the runtime failure exposes a new, different error — if so, requeue.
_runtime_err_type = (verifier_result.get("error_type") or "").strip()
_runtime_err_msg  = (verifier_result.get("summary") or "").strip()
if _runtime_err_type and _runtime_err_type != incident.get("error_type", ""):
    await _maybe_requeue_runtime_failure(
        original_incident=incident,
        runtime_error_type=_runtime_err_type,
        runtime_error_msg=_runtime_err_msg,
    )
```

- [ ] **Step 5: Run tests — expect PASS**

```
uv run python -m pytest tests/agents/test_fix_failed_runtime_retry.py -v
```

- [ ] **Step 6: Commit**

```
git add agents/orchestrator_agent.py tests/agents/test_fix_failed_runtime_retry.py
git commit -m "feat: re-queue as new incident when FIX_FAILED_RUNTIME exposes different error type"
```

---

## Task 7: Robust RCA JSON Parsing

**Problem:** `rca_agent.py` parses the LLM response with `json.loads()` then falls back to a greedy `re.search(r"\{.*\}", answer, re.DOTALL)`. The regex grabs the first `{`-to-last-`}` span, which fails when the LLM wraps the JSON in markdown code fences with a preceding code block, or emits extra text after the closing brace. Replacing the fallback with a smarter extractor eliminates parse failures silently producing empty `rca = {}`.

**Files:**
- Modify: `agents/rca_agent.py` — JSON parse block inside the `for attempt in range(3)` loop (~line 680)

- [ ] **Step 1: Write the failing test**

Create `tests/agents/test_rca_json_parse.py`:

```python
from agents.rca_agent import _extract_rca_json

def test_extracts_from_markdown_fence():
    raw = '```json\n{"root_cause": "x", "confidence": 0.9}\n```\nSome trailing text.'
    result = _extract_rca_json(raw)
    assert result["confidence"] == 0.9

def test_extracts_last_json_object_when_multiple():
    """When the LLM emits reasoning text + JSON, pick the last complete JSON object."""
    raw = 'Here is my analysis. {"partial": true} and then {"root_cause": "y", "confidence": 0.8}'
    result = _extract_rca_json(raw)
    assert result["confidence"] == 0.8

def test_returns_empty_dict_on_garbage():
    result = _extract_rca_json("No JSON here at all.")
    assert result == {}

def test_plain_json_passthrough():
    raw = '{"root_cause": "z", "confidence": 0.7}'
    result = _extract_rca_json(raw)
    assert result["confidence"] == 0.7
```

- [ ] **Step 2: Run to confirm it fails**

```
uv run python -m pytest tests/agents/test_rca_json_parse.py -v
```
Expected: FAILED — `_extract_rca_json` doesn't exist.

- [ ] **Step 3: Add `_extract_rca_json` to `rca_agent.py`**

Add this module-level function after the imports:

```python
def _extract_rca_json(answer: str) -> Dict[str, Any]:
    """
    Robustly extract the RCA JSON object from an LLM response.

    Strategy:
    1. Strip markdown code fences.
    2. Try json.loads on the cleaned string.
    3. Find all top-level JSON objects in the string and return the last
       (most likely the final answer, not a tool-call artifact).
    4. Return {} on total failure.
    """
    import json as _json  # noqa: PLC0415
    clean = re.sub(r"```(?:json)?", "", answer).replace("```", "").strip()

    # Direct parse
    try:
        return _json.loads(clean)
    except Exception:
        pass

    # Find the last balanced JSON object in the string
    last_obj: Dict[str, Any] = {}
    depth = 0
    start = -1
    for i, ch in enumerate(clean):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = clean[start:i + 1]
                try:
                    parsed = _json.loads(candidate)
                    if isinstance(parsed, dict):
                        last_obj = parsed
                except Exception:
                    pass
    return last_obj
```

- [ ] **Step 4: Replace inline parse logic with `_extract_rca_json`**

In `run_rca`, inside the `for attempt in range(3)` loop, replace:
```python
try:
    clean = re.sub(r"```(?:json)?|```", "", answer).strip()
    rca   = json.loads(clean)
except Exception:
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        rca = json.loads(match.group(0)) if match else {}
    except Exception:
        rca = {}
```
With:
```python
rca = _extract_rca_json(answer)
```

- [ ] **Step 5: Run tests — expect PASS**

```
uv run python -m pytest tests/agents/test_rca_json_parse.py -v
```

- [ ] **Step 6: Commit**

```
git add agents/rca_agent.py tests/agents/test_rca_json_parse.py
git commit -m "feat: replace brittle regex JSON fallback with balanced-brace extractor in RCA"
```

---

## Task 8: Extract .xsl / .groovy / .mmap from get-iflow ZIP

**Problem:** The get-iflow MCP tool returns a multi-file concatenated string with `---begin-of-file---` / `---end-of-file---` markers. `fix_planner.py` extracts only the `.iflw` file. For MAPPING_ERROR and SCRIPT_ERROR, the root cause is in `.xsl`, `.groovy`, or `.mmap` files that are currently invisible to the RCA agent and fixer. This is the single largest fixable coverage gap.

**Approach:**
1. Add `_extract_secondary_files(raw_get_iflow_output) -> Dict[str, str]` to `fix_planner.py` that returns a `{filepath: content}` dict for all non-`.iflw` files.
2. Surface these via `FixContext.secondary_files` (add field).
3. Inject them into the RCA prompt for MAPPING_ERROR / SCRIPT_ERROR.
4. For SCRIPT_ERROR with a Groovy fix, include the `.groovy` file in the `update-iflow` files list.

**Files:**
- Modify: `agents/fix_context.py` — add `secondary_files: Dict[str, str]`
- Modify: `agents/fix_planner.py` — add `_extract_secondary_files`, call it in `plan()`
- Modify: `agents/rca_agent.py` — inject secondary files for MAPPING_ERROR / SCRIPT_ERROR
- Modify: `agents/fix_applier.py` — include secondary files in `update-iflow` call when present

- [ ] **Step 1: Write the failing test**

Create `tests/agents/test_extract_secondary_files.py`:

```python
from agents.fix_planner import FixPlanner

_MULTI_FILE = """
src/main/resources/scenarioflows/integrationflow/MyIflow.iflw
---begin-of-file---
<?xml version="1.0"?>
<root><ifl:property/></root>
---end-of-file---

src/main/resources/script/TransformOrder.groovy
---begin-of-file---
import com.sap.gateway.ip.core.customdev.util.Message
def Message processData(Message message) { return message }
---end-of-file---

src/main/resources/mapping/OrderMapping.xsl
---begin-of-file---
<?xml version="1.0"?><xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"/>
---end-of-file---
"""

def test_extracts_groovy_and_xsl():
    result = FixPlanner._extract_secondary_files(_MULTI_FILE)
    assert any("groovy" in k for k in result)
    assert any("xsl" in k for k in result)
    assert "processData" in list(result.values())[0] or "processData" in list(result.values())[1]

def test_excludes_iflw():
    result = FixPlanner._extract_secondary_files(_MULTI_FILE)
    assert not any(".iflw" in k for k in result)

def test_returns_empty_for_plain_xml():
    result = FixPlanner._extract_secondary_files("<?xml version='1.0'?><root/>")
    assert result == {}
```

- [ ] **Step 2: Run to confirm it fails**

```
uv run python -m pytest tests/agents/test_extract_secondary_files.py -v
```
Expected: FAILED — `_extract_secondary_files` doesn't exist.

- [ ] **Step 3: Add `_extract_secondary_files` to `fix_planner.py`**

Add this static method to `FixPlanner`, alongside `_slice_xml`:

```python
@staticmethod
def _extract_secondary_files(raw: str) -> Dict[str, str]:
    """
    Parse a multi-file get-iflow output and return non-iflw file contents.

    Returns {filepath: content} for every file block whose path ends with
    .groovy, .xsl, .xslt, or .mmap.  Returns {} for plain-XML input.
    """
    if not raw or "---begin-of-file---" not in raw:
        return {}
    result: Dict[str, str] = {}
    _EXTENSIONS = (".groovy", ".xsl", ".xslt", ".mmap")
    parts = re.split(r"\n---begin-of-file---\n", raw)
    for i, part in enumerate(parts):
        if i == 0:
            continue  # first chunk is just the header / filepath list
        # The filepath is the last non-empty line before this part's marker
        prev_chunk = parts[i - 1]
        filepath_candidate = ""
        for line in reversed(prev_chunk.strip().splitlines()):
            line = line.strip()
            if line:
                filepath_candidate = line
                break
        if not any(filepath_candidate.endswith(ext) for ext in _EXTENSIONS):
            continue
        content_end = part.find("\n---end-of-file---")
        content = part[:content_end].strip() if content_end >= 0 else part.strip()
        if content:
            result[filepath_candidate] = content
    return result
```

- [ ] **Step 4: Add `secondary_files` field to `FixContext`**

In `agents/fix_context.py`:

```python
from typing import Dict, List

@dataclass(frozen=True)
class FixContext:
    ...
    secondary_files: Dict[str, str] = field(default_factory=dict)
```

- [ ] **Step 5: Populate `secondary_files` in `fix_planner.plan()`**

In `FixPlanner.plan()`, after the `.iflw` extraction block that sets `ctx.original_xml`, add:

```python
        # Extract secondary files (.groovy, .xsl, .mmap) from the same ZIP output.
        # These are only available when original_xml was derived from a concatenated package.
        _secondary = self._extract_secondary_files(ctx.original_xml or "")
        if not _secondary and "---begin-of-file---" in (ctx.sliced_xml or ""):
            _secondary = self._extract_secondary_files(ctx.sliced_xml)
        if _secondary:
            logger.info(
                "[FixPlanner] Extracted %d secondary file(s) for iflow=%s: %s",
                len(_secondary), ctx.iflow_id, list(_secondary.keys()),
            )
            ctx = replace(ctx, secondary_files=_secondary)
```

Note: this requires the `get-iflow` call to pass the full concatenated output (not just the `.iflw` content). Verify that `_fetch_iflow_xml` in `rca_agent.py` returns the raw output before `.iflw` extraction — it does (it returns `str(r.get("output", "")).strip()`).

The extraction for the `.iflw` XML happens inside `plan()` after receiving the original_xml, so secondary_files must be extracted from the *raw* output, not the already-extracted `.iflw`. Add a `raw_get_iflow_output` field to `FixContext` or extract before the iflw split:

Simplest approach: in `plan()`, before the `_extract_iflow_file` call, capture secondary files from the raw input:
```python
        _secondary = self._extract_secondary_files(ctx.original_xml or "")
        if _secondary:
            ctx = replace(ctx, secondary_files=_secondary)
        # ... then proceed with .iflw extraction
```

- [ ] **Step 6: Inject secondary files into RCA prompt for MAPPING_ERROR / SCRIPT_ERROR**

In `rca_agent.py`, in the `run_rca` method, after the `_iflow_section` and `_logs_section` are built, add:

```python
        _secondary_section = ""
        _error_type_upper = (error_type or "").upper()
        if _error_type_upper in ("MAPPING_ERROR", "SCRIPT_ERROR") and iflow_xml:
            from agents.fix_planner import FixPlanner as _FP  # noqa: PLC0415
            _sec = _FP._extract_secondary_files(iflow_xml)
            if _sec:
                _parts = []
                for _fp, _fc in _sec.items():
                    _parts.append(f"=== {_fp} ===\n{_fc[:5000]}")
                _secondary_section = "\n".join(_parts)
                logger.info(
                    "[RCA] Injecting %d secondary file(s) into prompt for error_type=%s iflow=%s",
                    len(_sec), error_type, iflow_id,
                )
```

Then include `_secondary_section` in the prompt string where applicable (after `_iflow_section`):
```python
        _secondary_block = (
            f"\n=== Script / Mapping files (pre-fetched) ===\n{_secondary_section}\n"
            if _secondary_section else ""
        )
```

Add `{_secondary_block}` into both prompt templates.

- [ ] **Step 7: Include secondary files in `update-iflow` for SCRIPT_ERROR**

In `agents/fix_applier.py`, in `_apply_structured` where `update-iflow` is called, after building the main file entry, add secondary files if present:

```python
        _files = [{"filepath": ctx.original_filepath, "content": patched_xml}]
        for _sfp, _sfc in (ctx.secondary_files or {}).items():
            _files.append({"filepath": _sfp, "content": _sfc})

        update_result = await self._mcp.execute_integration_tool(
            "update-iflow",
            {"id": ctx.iflow_id, "files": _files, "autoDeploy": False},
        )
```

- [ ] **Step 8: Run tests — expect PASS**

```
uv run python -m pytest tests/agents/test_extract_secondary_files.py -v
```

- [ ] **Step 9: Run full test suite**

```
uv run python -m pytest tests/ -v --tb=short
```

- [ ] **Step 10: Commit**

```
git add agents/fix_context.py agents/fix_planner.py agents/rca_agent.py agents/fix_applier.py tests/agents/test_extract_secondary_files.py
git commit -m "feat: extract .groovy/.xsl/.mmap from get-iflow ZIP to enable MAPPING_ERROR and SCRIPT_ERROR fixes"
```

---

## Self-Review

### Spec coverage
| Improvement | Task | Covered |
|---|---|---|
| Direct-patch value normalization | Task 1 | ✓ |
| Rich few-shot RCA patterns | Task 2 | ✓ |
| RCA self-consistency retry | Task 3 | ✓ |
| Strategy ordering by priors | Task 4 | ✓ |
| Verifier log re-check | Task 5 | ✓ |
| Auto-retry on FIX_FAILED_RUNTIME | Task 6 | ✓ |
| Structured RCA JSON parsing | Task 7 | ✓ |
| Extract .xsl/.groovy/.mmap | Task 8 | ✓ |

### Placeholder scan
- No TBD / TODO / "implement later" in code blocks.
- All file paths exact.
- All test commands exact.

### Type consistency
- `FixContext.secondary_files: Dict[str, str]` — matches usage in fix_applier (iterates `ctx.secondary_files.items()`). ✓
- `get_strategy_success_by_error_type` returns `List[Dict]` — matches supervisor usage (iterates `_priors`). ✓
- `_extract_rca_json` returns `Dict[str, Any]` — replaces the `rca = json.loads(...)` assignment. ✓
- `_normalize_value` returns `str` — used as dict key in `value_to_props`. ✓

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-23-fix-rate-improvements.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks

**2. Inline Execution** — execute tasks in this session using executing-plans with checkpoints

Which approach?
