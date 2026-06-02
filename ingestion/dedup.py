"""
ingestion/dedup.py
==================
HANA-backed deduplication for CALM exception IDs.

Prevents the same CALM exception from being processed twice when both
the polling loop and a webhook trigger arrive for the same event.

Table: CALM_DEDUP_SEEN
  EXCEPTION_ID — primary key (CALM exception ID string)
  SEEN_AT      — ISO timestamp when first seen
"""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEDUP_TABLE = "CALM_DEDUP_SEEN"


def ensure_dedup_schema() -> None:
    """Create CALM_DEDUP_SEEN table if it doesn't exist. Called once at startup."""
    try:
        from db.database import get_connection
        schema_q = os.getenv("HANA_SCHEMA", "")
        conn = get_connection()
        cur  = conn.cursor()

        check = (
            "SELECT COUNT(*) FROM SYS.TABLES WHERE TABLE_NAME=? AND SCHEMA_NAME=?"
            if schema_q else
            "SELECT COUNT(*) FROM SYS.TABLES WHERE TABLE_NAME=?"
        )
        cur.execute(check, (_DEDUP_TABLE, schema_q) if schema_q else (_DEDUP_TABLE,))
        if int(cur.fetchone()[0]) == 0:
            cur.execute(f"""CREATE TABLE "{_DEDUP_TABLE}" (
                EXCEPTION_ID  NVARCHAR(256) PRIMARY KEY,
                SEEN_AT       NVARCHAR(64)  NOT NULL
            )""")
            logger.info("[DEDUP] Created table %s", _DEDUP_TABLE)

        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("[DEDUP] ensure_dedup_schema: %s", exc)


def is_seen(exception_id: str) -> bool:
    """Return True if this CALM exception ID has already been processed."""
    try:
        from db.database import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{_DEDUP_TABLE}" WHERE EXCEPTION_ID=?', (exception_id,))
        result = int(cur.fetchone()[0]) > 0
        conn.close()
        return result
    except Exception as exc:
        logger.error("[DEDUP] is_seen check failed: %s", exc)
        return False  # fail open: process rather than miss


def mark_seen(exception_id: str) -> None:
    """Record a CALM exception ID as processed."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        from db.database import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        # HANA: use MERGE INTO for upsert (no INSERT OR IGNORE)
        cur.execute(
            f'MERGE INTO "{_DEDUP_TABLE}" AS target '
            f'USING (SELECT ? AS EXCEPTION_ID, ? AS SEEN_AT FROM DUMMY) AS source '
            f'ON target.EXCEPTION_ID = source.EXCEPTION_ID '
            f'WHEN NOT MATCHED THEN INSERT (EXCEPTION_ID, SEEN_AT) VALUES (source.EXCEPTION_ID, source.SEEN_AT)',
            (exception_id, now),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("[DEDUP] mark_seen failed for %s: %s", exception_id, exc)


def filter_new(exception_ids: list[str]) -> list[str]:
    """
    Bulk check: return only IDs not yet in CALM_DEDUP_SEEN.
    Minimises DB round-trips vs calling is_seen() one at a time.
    """
    if not exception_ids:
        return []
    try:
        from db.database import get_connection
        placeholders = ",".join("?" * len(exception_ids))
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(
            f'SELECT EXCEPTION_ID FROM "{_DEDUP_TABLE}" WHERE EXCEPTION_ID IN ({placeholders})',
            exception_ids,
        )
        seen = {row[0] for row in cur.fetchall()}
        conn.close()
        return [eid for eid in exception_ids if eid not in seen]
    except Exception as exc:
        logger.error("[DEDUP] filter_new failed: %s", exc)
        return exception_ids  # fail open