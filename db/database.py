"""
db/database.py
SAP HANA Cloud database layer (hdbcli).
"""

import os
import json
import uuid
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, UTC

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# TABLE NAMES — SAP Event Mesh app (EM tables only, no legacy AEM tables)
# ─────────────────────────────────────────────────────────────────────────────

_INCIDENTS_TABLE = os.getenv("HANA_TABLE_EM_INCIDENTS",         "EM_AUTONOMOUS_INCIDENTS")
_FIX_TABLE       = os.getenv("HANA_TABLE_EM_FIX_PATTERNS",      "EM_FIX_PATTERNS")
_TICKETS_TABLE   = os.getenv("HANA_TABLE_EM_ESCALATION_TICKETS", "EM_ESCALATION_TICKETS")


def set_db_source(source: str) -> None:
    """No-op — kept for import compatibility. Single EM table set in this app."""


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

def get_connection():
    from hdbcli import dbapi
    host     = os.getenv("HANA_HOST")
    user     = os.getenv("HANA_USER")
    password = os.getenv("HANA_PASSWORD")
    port_raw = os.getenv("HANA_PORT", "443")

    missing = []
    if not host:
        missing.append("HANA_HOST")
    if not user:
        missing.append("HANA_USER")
    if not password:
        missing.append("HANA_PASSWORD")
    if missing:
        raise RuntimeError(f"Missing HANA configuration: {', '.join(missing)}")

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid HANA_PORT value: {port_raw}") from exc

    conn = dbapi.connect(
        address=host,
        port=port,
        user=user,
        password=password,
        encrypt=True,
        sslValidateCertificate=False,
    )
    schema = os.getenv("HANA_SCHEMA", "")
    if schema:
        cur = conn.cursor()
        cur.execute(f'SET SCHEMA "{schema}"')
        cur.close()
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _rows_to_dicts(cursor) -> List[Dict]:
    """Convert hdbcli cursor results using column names from description."""
    if cursor.description is None:
        return []
    cols = [d[0].lower() for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA MIGRATIONS
# ─────────────────────────────────────────────────────────────────────────────

def ensure_autonomous_incident_schema():
    required_columns = {
        "iflow_id":              "NVARCHAR(500)",
        "incident_group_key":    "NVARCHAR(64)",
        "occurrence_count":      "INTEGER",
        "last_seen":             "NVARCHAR(64)",
        "verification_status":   "NVARCHAR(64)",
        "fix_steps":             "NCLOB",
        "field_changes":         "NCLOB",
        "fix_plan_generated_at": "NVARCHAR(64)",
        "retry_count":           "INTEGER",
        "last_failed_stage":     "NVARCHAR(64)",
        "iflow_snapshot_before": "NCLOB",
        "pending_since":         "NVARCHAR(64)",
        "ticket_id":             "NVARCHAR(512)",
        "consecutive_failures":  "INTEGER",
        "auto_escalated":        "INTEGER",
        "source_type":           "NVARCHAR(64)",
        "designtime_artifact_id": "NVARCHAR(500)",
    }
    try:
        conn = get_connection()
        cur  = conn.cursor()
        schema = os.getenv("HANA_SCHEMA", "")
        tbl = _INCIDENTS_TABLE.upper()
        if schema:
            cur.execute(
                "SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME=? AND SCHEMA_NAME=?",
                (tbl, schema),
            )
        else:
            cur.execute("SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME=?", (tbl,))
        existing = {str(r[0]).lower() for r in cur.fetchall()}
        for column_name, column_type in required_columns.items():
            if column_name.lower() not in existing:
                cur.execute(f'ALTER TABLE "{_INCIDENTS_TABLE}" ADD ("{column_name}" {column_type})')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"ensure_autonomous_incident_schema: {e}")


def _get_autonomous_incident_column_lookup() -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    try:
        conn   = get_connection()
        cur    = conn.cursor()
        schema = os.getenv("HANA_SCHEMA", "")
        tbl = _INCIDENTS_TABLE.upper()
        if schema:
            cur.execute(
                "SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME=? AND SCHEMA_NAME=?",
                (tbl, schema),
            )
        else:
            cur.execute("SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME=?", (tbl,))
        for row in cur.fetchall():
            name = str(row[0])
            lookup[name.lower()] = name
        conn.close()
    except Exception as e:
        logger.error(f"_get_autonomous_incident_column_lookup: {e}")
    return lookup


def ensure_fix_patterns_schema():
    """Add missing columns to fix_patterns if absent."""
    required = {
        "success_count":        "INTEGER",
        "replay_success_count": "INTEGER",
        "key_steps":            "NVARCHAR(2000)",  # JSON array of step descriptions from successful fix
    }
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE TABLE_NAME=?",
            (_FIX_TABLE.upper(),),
        )
        existing = {str(r[0]).lower() for r in cur.fetchall()}
        for col, typ in required.items():
            if col.lower() not in existing:
                default = " DEFAULT 0" if "INTEGER" in typ else ""
                cur.execute(f'ALTER TABLE "{_FIX_TABLE}" ADD ("{col}" {typ}{default})')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"ensure_fix_patterns_schema: {e}")



def ensure_em_schema():
    """Create the three Event Mesh–specific tables if they don't exist."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        schema_q = os.getenv("HANA_SCHEMA", "")

        # ── EM_AUTONOMOUS_INCIDENTS ──────────────────────────────────────────
        check = (
            f"SELECT COUNT(*) FROM SYS.TABLES WHERE TABLE_NAME=? AND SCHEMA_NAME=?"
            if schema_q else
            "SELECT COUNT(*) FROM SYS.TABLES WHERE TABLE_NAME=?"
        )
        cur.execute(check, (_INCIDENTS_TABLE.upper(), schema_q) if schema_q else (_INCIDENTS_TABLE.upper(),))
        if int(cur.fetchone()[0]) == 0:
            cur.execute(f"""CREATE TABLE "{_INCIDENTS_TABLE}" (
                incident_id            NVARCHAR(100),
                message_guid           NVARCHAR(200),
                iflow_id               NVARCHAR(500),
                sender                 NVARCHAR(200),
                receiver               NVARCHAR(200),
                status                 NVARCHAR(50) DEFAULT 'DETECTED',
                error_type             NVARCHAR(100),
                error_message          NCLOB,
                root_cause             NCLOB,
                proposed_fix           NCLOB,
                rca_confidence         DECIMAL(5,4),
                affected_component     NVARCHAR(200),
                fix_summary            NCLOB,
                comment                NCLOB,
                correlation_id         NVARCHAR(200),
                log_start              NVARCHAR(64),
                log_end                NVARCHAR(64),
                created_at             NVARCHAR(64),
                resolved_at            NVARCHAR(64),
                tags                   NCLOB,
                incident_group_key     NVARCHAR(64),
                occurrence_count       INTEGER DEFAULT 1,
                last_seen              NVARCHAR(64),
                verification_status    NVARCHAR(64),
                source_type            NVARCHAR(64),
                fix_steps              NCLOB,
                field_changes          NCLOB,
                fix_plan_generated_at  NVARCHAR(64),
                retry_count            INTEGER,
                last_failed_stage      NVARCHAR(64),
                iflow_snapshot_before  NCLOB,
                pending_since          NVARCHAR(64),
                ticket_id              NVARCHAR(512),
                consecutive_failures   INTEGER DEFAULT 0,
                auto_escalated         INTEGER DEFAULT 0,
                integration_flow_name  NVARCHAR(500),
                artifact_id            NVARCHAR(500),
                designtime_artifact_id NVARCHAR(500)
            )""")
            logger.info("[DB] Created table %s", _INCIDENTS_TABLE)

        # ── EM_FIX_PATTERNS ──────────────────────────────────────────────────
        cur.execute(check, (_FIX_TABLE.upper(), schema_q) if schema_q else (_FIX_TABLE.upper(),))
        if int(cur.fetchone()[0]) == 0:
            cur.execute(f"""CREATE TABLE "{_FIX_TABLE}" (
                pattern_id            NVARCHAR(100) PRIMARY KEY,
                error_signature       NVARCHAR(500),
                iflow_id              NVARCHAR(200),
                error_type            NVARCHAR(100),
                root_cause            NCLOB,
                fix_applied           NCLOB,
                outcome               NVARCHAR(50),
                applied_count         INTEGER DEFAULT 0,
                last_seen             NVARCHAR(64),
                success_count         INTEGER DEFAULT 0,
                replay_success_count  INTEGER DEFAULT 0,
                key_steps             NVARCHAR(2000)
            )""")
            logger.info("[DB] Created table %s", _FIX_TABLE)

        # ── EM_ESCALATION_TICKETS ────────────────────────────────────────────
        cur.execute(check, (_TICKETS_TABLE.upper(), schema_q) if schema_q else (_TICKETS_TABLE.upper(),))
        if int(cur.fetchone()[0]) == 0:
            cur.execute(f"""CREATE TABLE "{_TICKETS_TABLE}" (
                ticket_id         NVARCHAR(100) PRIMARY KEY,
                incident_id       NVARCHAR(100),
                iflow_id          NVARCHAR(200),
                error_type        NVARCHAR(100),
                title             NVARCHAR(500),
                description       NCLOB,
                priority          NVARCHAR(20),
                status            NVARCHAR(20) DEFAULT 'OPEN',
                assigned_to       NVARCHAR(200),
                resolution_notes  NCLOB,
                created_at        NVARCHAR(64),
                updated_at        NVARCHAR(64),
                resolved_at       NVARCHAR(64)
            )""")
            logger.info("[DB] Created table %s", _TICKETS_TABLE)

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"ensure_em_schema: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# QUERY HISTORY
# ─────────────────────────────────────────────────────────────────────────────

def get_all_history(user_id: Optional[str] = None) -> List[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        if user_id:
            cur.execute(
                "SELECT * FROM query_history WHERE user_id = ? ORDER BY timestamp DESC",
                (user_id,),
            )
        else:
            cur.execute("SELECT * FROM query_history ORDER BY timestamp DESC")
        rows = _rows_to_dicts(cur)
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_all_history: {e}")
        return []


def create_query_history(session_id, question, answer, timestamp, user_id):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO query_history (session_id, question, answer, timestamp, user_id) VALUES (?,?,?,?,?)",
            (session_id, question, answer, timestamp, user_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"create_query_history: {e}")


def update_query_history(session_id, question, answer, timestamp):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE query_history SET question=?, answer=?, timestamp=? WHERE session_id=?",
            (question, answer, timestamp, session_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"update_query_history: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# FILE METADATA
# ─────────────────────────────────────────────────────────────────────────────

def insert_file_metadata(data: Dict):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO uploaded_files
               (file_id, session_id, file_name, file_type, file_size, s3_key, timestamp, user_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                data.get("file_id", str(uuid.uuid4())),
                data.get("session_id"),
                data.get("file_name"),
                data.get("file_type"),
                data.get("file_size"),
                data.get("s3_key"),
                data.get("timestamp"),
                data.get("user_id"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"insert_file_metadata: {e}")


def insert_xsd_metadata(data: Dict):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO xsd_files
               (file_id, session_id, target_namespace, element_count, type_count, content, timestamp, user_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                data.get("file_id", str(uuid.uuid4())),
                data.get("session_id"),
                data.get("target_namespace"),
                data.get("element_count", 0),
                data.get("type_count", 0),
                data.get("content"),
                data.get("timestamp"),
                data.get("user_id"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"insert_xsd_metadata: {e}")


def get_xsd_files_by_session(session_id: str) -> List[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("SELECT * FROM xsd_files WHERE session_id = ?", (session_id,))
        rows = _rows_to_dicts(cur)
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_xsd_files_by_session: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# TEST SUITE
# ─────────────────────────────────────────────────────────────────────────────

def addTestSuiteLog(data: Dict):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO test_suite_logs
               (test_suite_id, user_id, prompt, timestamp, status, executions)
               VALUES (?,?,?,?,?,?)""",
            (
                data["test_suite_id"],
                data.get("user"),
                data.get("prompt"),
                data.get("timestamp"),
                data.get("status"),
                json.dumps(data.get("executions", [])),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"addTestSuiteLog (table may not exist): {e}")


def update_test_suite_executions(test_suite_id: str, executions: List):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE test_suite_logs SET executions=? WHERE test_suite_id=?",
            (json.dumps(executions), test_suite_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"update_test_suite_executions (table may not exist): {e}")


def updateTestSuiteStatus(test_suite_id: Optional[str], status: str):
    if not test_suite_id:
        return
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE test_suite_logs SET status=? WHERE test_suite_id=?",
            (status, test_suite_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"updateTestSuiteStatus (table may not exist): {e}")


def get_testsuite_log_entries(user_id: Optional[str] = None) -> List[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        if user_id:
            cur.execute(
                "SELECT * FROM test_suite_logs WHERE user_id = ? ORDER BY timestamp DESC",
                (user_id,),
            )
        else:
            cur.execute("SELECT * FROM test_suite_logs ORDER BY timestamp DESC")
        rows = []
        for d in _rows_to_dicts(cur):
            if isinstance(d.get("executions"), str):
                try:
                    d["executions"] = json.loads(d["executions"])
                except Exception:
                    pass
            rows.append(d)
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_testsuite_log_entries: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# AUTONOMOUS INCIDENTS
# ─────────────────────────────────────────────────────────────────────────────

def create_incident(incident: Dict):
    try:
        conn = get_connection()
        cur  = conn.cursor()
        column_lookup = _get_autonomous_incident_column_lookup()
        payload = {
            "incident_id":       incident.get("incident_id"),
            "message_guid":      incident.get("message_guid"),
            "iflow_id":          incident.get("iflow_id"),
            "sender":            incident.get("sender"),
            "receiver":          incident.get("receiver"),
            "status":            incident.get("status", "DETECTED"),
            "error_type":        incident.get("error_type"),
            "error_message":     incident.get("error_message"),
            "root_cause":        incident.get("root_cause"),
            "proposed_fix":      incident.get("proposed_fix"),
            "rca_confidence":    incident.get("rca_confidence"),
            "affected_component":incident.get("affected_component"),
            "fix_summary":       incident.get("fix_summary"),
            "comment":           incident.get("comment"),
            "correlation_id":    incident.get("correlation_id"),
            "log_start":         incident.get("log_start"),
            "log_end":           incident.get("log_end"),
            "created_at":        incident.get("created_at"),
            "resolved_at":       incident.get("resolved_at"),
            "tags":              json.dumps(incident.get("tags", [])),
            "incident_group_key":incident.get("incident_group_key"),
            "occurrence_count":  incident.get("occurrence_count", 1),
            "last_seen":         incident.get("last_seen") or incident.get("created_at"),
            "verification_status":incident.get("verification_status"),
            "source_type":       incident.get("source_type"),
            "designtime_artifact_id": incident.get("designtime_artifact_id", ""),
        }
        columns, values = [], []
        for logical_name, value in payload.items():
            actual_name = column_lookup.get(logical_name.lower())
            if not actual_name:
                continue
            columns.append(_quote_identifier(actual_name))
            values.append(value)
        placeholders = ",".join("?" for _ in values)
        cur.execute(
            f'INSERT INTO "{_INCIDENTS_TABLE}" ({",".join(columns)}) VALUES ({placeholders})',
            values,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"create_incident: {e}")
        raise


def update_incident(incident_id: str, updates: Dict):
    if not updates:
        return
    try:
        conn          = get_connection()
        cur           = conn.cursor()
        column_lookup = _get_autonomous_incident_column_lookup()
        assignments, values = [], []
        for logical_name, value in updates.items():
            actual_name = column_lookup.get(logical_name.lower())
            if not actual_name:
                logger.warning(f"update_incident: skipping unknown column '{logical_name}'")
                continue
            assignments.append(f"{_quote_identifier(actual_name)}=?")
            values.append(value)
        if not assignments:
            conn.close()
            return
        incident_id_col = _quote_identifier(column_lookup.get("incident_id", "INCIDENT_ID"))
        values.append(incident_id)
        cur.execute(
            f'UPDATE "{_INCIDENTS_TABLE}" SET {", ".join(assignments)} WHERE {incident_id_col}=?',
            values,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"update_incident: {e}")
        raise


def get_all_incidents(status: Optional[str] = None, limit: int = 50) -> List[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        src = f'SELECT * FROM "{_INCIDENTS_TABLE}"'
        if status:
            if limit and limit > 0:
                cur.execute(f"{src} WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
            else:
                cur.execute(f"{src} WHERE status=? ORDER BY created_at DESC", (status,))
        else:
            if limit and limit > 0:
                cur.execute(f"{src} ORDER BY created_at DESC LIMIT ?", (limit,))
            else:
                cur.execute(f"{src} ORDER BY created_at DESC")
        rows = []
        for d in _rows_to_dicts(cur):
            if isinstance(d.get("tags"), str):
                try:
                    d["tags"] = json.loads(d["tags"])
                except Exception:
                    pass
            rows.append(d)
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_all_incidents: {e}")
        return []


def get_stage_counts() -> dict:
    """
    Return pipeline stage counts using a single GROUP BY query —
    never fetches row data, safe on large tables.
    """
    stage_map = {
        "DETECTED":          "observed",
        "RCA_IN_PROGRESS":   "classified",
        "RCA_COMPLETE":      "rca",
        "FIX_IN_PROGRESS":   "fix",
        "FIX_DEPLOYED":      "fix",
        "AWAITING_APPROVAL": "fix",
        "FIX_VERIFIED":      "verified",
        "HUMAN_INITIATED_FIX": "verified",
        "RETRIED":           "verified",
    }
    counts = {"observed": 0, "classified": 0, "rca": 0, "fix": 0, "verified": 0}
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(f'SELECT status, COUNT(*) FROM "{_INCIDENTS_TABLE}" GROUP BY status')
        for row in cur.fetchall():
            s, n = (row[0] or "").upper(), int(row[1] or 0)
            stage = stage_map.get(s)
            if stage:
                counts[stage] += n
        conn.close()
    except Exception as e:
        logger.error(f"get_stage_counts: {e}")
    return counts


def count_all_incidents(status: Optional[str] = None) -> int:
    """Return the exact row count from autonomous_incidents (no LIMIT)."""
    try:
        conn = get_connection()
        cur  = conn.cursor()
        if status:
            cur.execute(f'SELECT COUNT(*) FROM "{_INCIDENTS_TABLE}" WHERE status=?', (status,))
        else:
            cur.execute(f'SELECT COUNT(*) FROM "{_INCIDENTS_TABLE}"')
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"count_all_incidents: {e}")
        return 0


def _normalize_incident_dict(d: Dict) -> Dict:
    if isinstance(d.get("tags"), str):
        try:
            d["tags"] = json.loads(d["tags"])
        except Exception:
            pass
    if d.get("occurrence_count") is None:
        d["occurrence_count"] = 1
    if d.get("last_seen") is None:
        d["last_seen"] = d.get("created_at")
    return d


def _dedupe_incidents(incidents: List[Dict]) -> List[Dict]:
    deduped: List[Dict] = []
    seen_keys: set = set()
    for incident in incidents:
        key = (
            incident.get("message_guid")
            or f"{incident.get('iflow_id','')}::{incident.get('error_type','')}::{incident.get('status','')}"
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(incident)
    return deduped


def get_incident_by_id(incident_id: str) -> Optional[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f'SELECT * FROM "{_INCIDENTS_TABLE}" WHERE incident_id=?',
            (incident_id,),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        if not rows:
            return None
        return _normalize_incident_dict(rows[0])
    except Exception as e:
        logger.error(f"get_incident_by_id: {e}")
        return None


def get_incident_by_message_guid(message_guid: str) -> Optional[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f'SELECT * FROM "{_INCIDENTS_TABLE}" WHERE message_guid=? ORDER BY created_at DESC LIMIT 1',
            (message_guid,),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        if not rows:
            return None
        return _normalize_incident_dict(rows[0])
    except Exception as e:
        logger.error(f"get_incident_by_message_guid: {e}")
        return None


def get_open_incident_by_signature(iflow_id: str, error_type: str) -> Optional[Dict]:
    terminal_statuses = (
        "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED",
        "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
        "REJECTED", "ARTIFACT_MISSING", "VERIFICATION_UNAVAILABLE",
    )
    placeholders = ",".join("?" for _ in terminal_statuses)
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f"""SELECT * FROM "{_INCIDENTS_TABLE}"
                WHERE iflow_id=?
                  AND error_type=?
                  AND status NOT IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1""",
            (iflow_id, error_type, *terminal_statuses),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        if not rows:
            return None
        return _normalize_incident_dict(rows[0])
    except Exception as e:
        logger.error(f"get_open_incident_by_signature: {e}")
        return None


def increment_incident_occurrence(incident_id: str, message_guid: Optional[str] = None, last_seen: Optional[str] = None):
    try:
        incident = get_incident_by_id(incident_id)
        if not incident:
            return
        _now = last_seen or datetime.now(UTC).isoformat()
        updates = {
            "occurrence_count": int(incident.get("occurrence_count") or 1) + 1,
            "last_seen":        _now,
            "log_end":          _now,   # keep log_end fresh so time filters don't hide active incidents
        }
        if message_guid:
            updates["message_guid"] = message_guid
        update_incident(incident_id, updates)
    except Exception as e:
        logger.error(f"increment_incident_occurrence: {e}")


def get_pending_approvals() -> List[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f'SELECT * FROM "{_INCIDENTS_TABLE}" WHERE status=? ORDER BY created_at DESC LIMIT 250',
            ("AWAITING_APPROVAL",),
        )
        rows = [_normalize_incident_dict(d) for d in _rows_to_dicts(cur)]
        conn.close()
        return _dedupe_incidents(rows)
    except Exception as e:
        logger.error(f"get_pending_approvals: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# FIX PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

def upsert_fix_pattern(data: Dict, replay_success: bool = False):
    try:
        import json as _json
        conn = get_connection()
        cur  = conn.cursor()
        sig  = data.get("error_signature", "")
        fix  = str(data.get("fix_applied", "") or "")
        now  = datetime.now(UTC).isoformat()

        # Summarise the key steps from a successful fix for future RCA context
        raw_steps  = data.get("key_steps") or []
        key_steps_json = _json.dumps(
            [str(s)[:200] for s in raw_steps if s][:8]  # at most 8 steps, 200 chars each
        ) if raw_steps else None

        cur.execute(
            f'SELECT pattern_id, applied_count, fix_applied, "success_count", "replay_success_count", "key_steps" FROM "{_FIX_TABLE}" WHERE error_signature=?',
            (sig,),
        )
        rows = _rows_to_dicts(cur)
        existing = next((r for r in rows if str(r.get("fix_applied", "") or "") == fix), None)

        outcome = data.get("outcome", "")
        if existing:
            new_success = (existing.get("success_count") or 0) + (1 if outcome == "SUCCESS" else 0)
            new_replay  = (existing.get("replay_success_count") or 0) + (1 if replay_success else 0)
            # Only overwrite key_steps when this fix succeeded (preserve last successful steps)
            new_steps   = key_steps_json if (outcome == "SUCCESS" and key_steps_json) else existing.get("key_steps")
            cur.execute(
                f"""UPDATE "{_FIX_TABLE}"
                   SET applied_count=?, outcome=?, last_seen=?,
                       "success_count"=?, "replay_success_count"=?, "key_steps"=?
                   WHERE pattern_id=?""",
                (existing["applied_count"] + 1, outcome, now, new_success, new_replay, new_steps, existing["pattern_id"]),
            )
        else:
            cur.execute(
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
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"upsert_fix_pattern: {e}")


def get_recent_incident_by_group_key(group_key: str, within_seconds: int = 60) -> Optional[Dict]:
    """Return the most recent non-terminal incident with this group_key created within `within_seconds`."""
    terminal = (
        "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED",
        "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
        "REJECTED", "TICKET_CREATED", "ARTIFACT_MISSING", "VERIFICATION_UNAVAILABLE",
    )
    try:
        from datetime import timedelta
        cutoff       = (datetime.now(UTC) - timedelta(seconds=within_seconds)).isoformat()
        conn         = get_connection()
        cur          = conn.cursor()
        placeholders = ",".join("?" for _ in terminal)
        cur.execute(
            f"""SELECT * FROM "{_INCIDENTS_TABLE}"
               WHERE incident_group_key=?
                 AND status NOT IN ({placeholders})
                 AND created_at >= ?
               ORDER BY created_at DESC LIMIT 1""",
            (group_key, *terminal, cutoff),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        return rows[0] if rows else None
    except Exception as e:
        logger.error(f"get_recent_incident_by_group_key: {e}")
        return None


def get_similar_patterns(error_signature: str) -> List[Dict]:
    """Return successful patterns ranked by success rate, then recency, then applied_count.

    Ranking priority:
      1. success_rate  (success_count / applied_count) — most reliable fix first
      2. last_seen     (DESC) — favour recent patterns over stale ones
      3. applied_count (DESC) — tie-break by usage frequency
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f"""SELECT * FROM "{_FIX_TABLE}"
               WHERE error_signature=? AND outcome='SUCCESS'
               ORDER BY
                 CAST(COALESCE("success_count", 0) AS REAL) /
                   NULLIF(applied_count, 0) DESC,
                 last_seen DESC,
                 applied_count DESC
               LIMIT 5""",
            (error_signature,),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_similar_patterns: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ESCALATION TICKETS
# ─────────────────────────────────────────────────────────────────────────────

def create_escalation_ticket(data: Dict) -> str:
    """Insert a new escalation ticket and return its ticket_id."""
    ticket_id = data.get("ticket_id") or str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f"""INSERT INTO "{_TICKETS_TABLE}"
               (ticket_id, incident_id, iflow_id, error_type, title,
                description, priority, status, assigned_to,
                resolution_notes, created_at, updated_at, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ticket_id,
                data.get("incident_id"),
                data.get("iflow_id"),
                data.get("error_type"),
                data.get("title"),
                data.get("description"),
                data.get("priority", "MEDIUM"),
                data.get("status", "OPEN"),
                data.get("assigned_to"),
                data.get("resolution_notes"),
                data.get("created_at", now),
                now,
                data.get("resolved_at"),
            ),
        )
        conn.commit()
        conn.close()
        logger.info(f"[EscalationTicket] Created ticket {ticket_id} for incident {data.get('incident_id')}")
    except Exception as e:
        logger.error(f"create_escalation_ticket: {e}")
    return ticket_id


def get_escalation_tickets(
    status: Optional[str] = None,
    incident_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    try:
        conn       = get_connection()
        cur        = conn.cursor()
        conditions: List[str] = []
        params:     List[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if incident_id:
            conditions.append("incident_id=?")
            params.append(incident_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(
            f'SELECT * FROM "{_TICKETS_TABLE}" {where} ORDER BY created_at DESC LIMIT ?',
            (*params, limit),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"get_escalation_tickets: {e}")
        return []


def get_escalation_ticket_by_id(ticket_id: str) -> Optional[Dict]:
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f'SELECT * FROM "{_TICKETS_TABLE}" WHERE ticket_id=?',
            (ticket_id,),
        )
        rows = _rows_to_dicts(cur)
        conn.close()
        return rows[0] if rows else None
    except Exception as e:
        logger.error(f"get_escalation_ticket_by_id: {e}")
        return None


def update_escalation_ticket(ticket_id: str, updates: Dict):
    if not updates:
        return
    updates["updated_at"] = datetime.now(UTC).isoformat()
    try:
        conn = get_connection()
        cur  = conn.cursor()
        assignments = ", ".join(f'"{k}"=?' for k in updates)
        values      = list(updates.values()) + [ticket_id]
        cur.execute(
            f'UPDATE "{_TICKETS_TABLE}" SET {assignments} WHERE "ticket_id"=?',
            values,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"update_escalation_ticket: {e}")
