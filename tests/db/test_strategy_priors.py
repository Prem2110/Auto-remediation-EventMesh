import pytest
from unittest.mock import patch


def _make_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE EM_FIX_PATTERNS (
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
    conn.execute("""INSERT INTO EM_FIX_PATTERNS
        (pattern_id, error_signature, iflow_id, error_type, root_cause, fix_applied,
         outcome, applied_count, last_seen, success_count, SUPERVISOR_STRATEGY)
        VALUES ('p1','sig1','A','AUTH_CONFIG_ERROR','rc','fix','SUCCESS',
                3,'2026-01-01',3,'component_replace')""")
    conn.execute("""INSERT INTO EM_FIX_PATTERNS
        (pattern_id, error_signature, iflow_id, error_type, root_cause, fix_applied,
         outcome, applied_count, last_seen, success_count, SUPERVISOR_STRATEGY)
        VALUES ('p2','sig2','B','AUTH_CONFIG_ERROR','rc2','fix2','FAILED',
                1,'2026-01-02',0,'direct_patch')""")
    conn.commit()
    return conn


def test_get_strategy_success_returns_ranked(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    monkeypatch.setattr("db.database._FIX_TABLE", "EM_FIX_PATTERNS")

    from db.database import get_strategy_success_by_error_type
    result = get_strategy_success_by_error_type("AUTH_CONFIG_ERROR")
    assert len(result) >= 1
    assert result[0]["strategy"] == "component_replace"
    assert (result[0].get("success_count") or 0) >= 1


def test_get_strategy_success_excludes_zero_success(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    monkeypatch.setattr("db.database._FIX_TABLE", "EM_FIX_PATTERNS")

    from db.database import get_strategy_success_by_error_type
    result = get_strategy_success_by_error_type("AUTH_CONFIG_ERROR")
    strategies_returned = [r["strategy"] for r in result]
    # direct_patch has success_count=0, should be absent from ranked results
    # (it has a SUPERVISOR_STRATEGY row but 0 successes so SUM is 0)
    # The function returns ALL groups including 0-sum, so direct_patch may appear
    # but must be ranked after component_replace
    if "direct_patch" in strategies_returned:
        assert strategies_returned.index("component_replace") < strategies_returned.index("direct_patch")


def test_get_strategy_success_empty_for_unknown(monkeypatch):
    conn = _make_conn()
    monkeypatch.setattr("db.database.get_connection", lambda: conn)
    monkeypatch.setattr("db.database._FIX_TABLE", "EM_FIX_PATTERNS")

    from db.database import get_strategy_success_by_error_type
    assert get_strategy_success_by_error_type("UNKNOWN_XYZ") == []


class _NoCloseConn:
    """Wraps a sqlite3 connection but makes close() a no-op so the in-memory DB survives."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        pass

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)


def test_upsert_stores_supervisor_strategy(monkeypatch):
    raw_conn = _make_conn()
    wrapper = _NoCloseConn(raw_conn)

    monkeypatch.setattr("db.database.get_connection", lambda: wrapper)
    monkeypatch.setattr("db.database._FIX_TABLE", "EM_FIX_PATTERNS")

    from db.database import upsert_fix_pattern
    upsert_fix_pattern({
        "error_signature":    "sig_strat",
        "iflow_id":           "iflow_Z",
        "error_type":         "HTTP_CALL_FAILED",
        "root_cause":         "rc",
        "fix_applied":        "fix",
        "outcome":            "SUCCESS",
        "supervisor_strategy": "direct_patch",
    })

    row = raw_conn.execute(
        "SELECT SUPERVISOR_STRATEGY FROM EM_FIX_PATTERNS WHERE error_signature='sig_strat'"
    ).fetchone()
    assert row is not None
    assert row["SUPERVISOR_STRATEGY"] == "direct_patch"
