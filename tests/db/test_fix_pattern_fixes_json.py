import json
import sqlite3
import pytest
from unittest.mock import patch


class _NoCloseConn:
    """Wraps a sqlite3 connection and no-ops close() so the test can inspect after upsert."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def close(self):
        pass


def _make_conn():
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.execute("""CREATE TABLE EM_FIX_PATTERNS (
        pattern_id TEXT PRIMARY KEY,
        error_signature TEXT,
        iflow_id TEXT,
        error_type TEXT,
        root_cause TEXT,
        fix_applied TEXT,
        outcome TEXT,
        applied_count INTEGER DEFAULT 0,
        last_seen TEXT,
        success_count INTEGER DEFAULT 0,
        replay_success_count INTEGER DEFAULT 0,
        key_steps TEXT,
        FIXES_JSON TEXT,
        SUPERVISOR_STRATEGY TEXT
    )""")
    raw.commit()
    return _NoCloseConn(raw)


def test_upsert_stores_fixes_json_on_success(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    monkeypatch.setattr("db.database._FIX_TABLE", "EM_FIX_PATTERNS")

    from db.database import upsert_fix_pattern
    fixes = [{"component_id": "ReceiverHTTP_1", "property_to_change": "Address",
               "current_value": "http://old", "correct_value": "https://new"}]
    upsert_fix_pattern({
        "error_signature": "sig1",
        "iflow_id":        "iflow_A",
        "error_type":      "HTTP_CALL_FAILED",
        "root_cause":      "wrong address",
        "fix_applied":     "corrected address",
        "outcome":         "SUCCESS",
        "fixes_json":      json.dumps(fixes),
    })

    row = conn._conn.execute(
        "SELECT FIXES_JSON FROM EM_FIX_PATTERNS WHERE error_signature='sig1'"
    ).fetchone()
    assert row is not None
    stored = json.loads(row["FIXES_JSON"])
    assert stored[0]["component_id"] == "ReceiverHTTP_1"


def test_upsert_does_not_store_fixes_json_on_failure(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    monkeypatch.setattr("db.database._FIX_TABLE", "EM_FIX_PATTERNS")

    from db.database import upsert_fix_pattern
    upsert_fix_pattern({
        "error_signature": "sig2",
        "iflow_id":        "iflow_B",
        "error_type":      "HTTP_CALL_FAILED",
        "root_cause":      "wrong address",
        "fix_applied":     "corrected address",
        "outcome":         "FAILED",
        "fixes_json":      json.dumps([{"component_id": "x"}]),
    })

    row = conn._conn.execute(
        "SELECT FIXES_JSON FROM EM_FIX_PATTERNS WHERE error_signature='sig2'"
    ).fetchone()
    assert row is not None
    assert row["FIXES_JSON"] is None


def test_upsert_updates_fixes_json_on_second_success(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    monkeypatch.setattr("db.database._FIX_TABLE", "EM_FIX_PATTERNS")

    from db.database import upsert_fix_pattern
    fixes_v1 = [{"component_id": "A", "property_to_change": "Address",
                  "current_value": "http://old", "correct_value": "https://v1"}]
    fixes_v2 = [{"component_id": "A", "property_to_change": "Address",
                  "current_value": "http://old", "correct_value": "https://v2"}]

    upsert_fix_pattern({
        "error_signature": "sig3", "iflow_id": "iflow_C", "error_type": "HTTP_CALL_FAILED",
        "root_cause": "rc", "fix_applied": "fix", "outcome": "SUCCESS",
        "fixes_json": json.dumps(fixes_v1),
    })
    upsert_fix_pattern({
        "error_signature": "sig3", "iflow_id": "iflow_C", "error_type": "HTTP_CALL_FAILED",
        "root_cause": "rc", "fix_applied": "fix", "outcome": "SUCCESS",
        "fixes_json": json.dumps(fixes_v2),
    })

    row = conn._conn.execute(
        "SELECT FIXES_JSON, applied_count FROM EM_FIX_PATTERNS WHERE error_signature='sig3'"
    ).fetchone()
    assert row["applied_count"] == 2
    stored = json.loads(row["FIXES_JSON"])
    assert stored[0]["correct_value"] == "https://v2"
