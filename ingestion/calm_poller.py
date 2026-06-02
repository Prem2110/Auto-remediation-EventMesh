"""
ingestion/calm_poller.py
========================
Long-running async task: polls CALM Exception API every CALM_POLL_INTERVAL_SECONDS
and feeds new FAILED exceptions into the existing agent pipeline via
orchestrator.process_detected_error().

Replaces: main.py::_run_cpi_monitor() + cpi_monitor/error_publisher.py

Checkpoint is persisted in HANA (CALM_POLL_CHECKPOINT table) so the poller
resumes from the correct point after restarts. Lookback is set to
CALM_LOOKBACK_MINUTES (default 15) on first run to handle exceptions that
appeared while the app was down.

Pull mode: CALM pulls from CPI on its own schedule (configured in CALM UI).
Set CALM_POLL_INTERVAL_SECONDS >= CALM pull interval to avoid redundant empty pages.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agents.orchestrator_agent import OrchestratorAgent

logger = logging.getLogger(__name__)

_POLL_INTERVAL  = int(os.getenv("CALM_POLL_INTERVAL_SECONDS", "120"))
_LOOKBACK_MINS  = int(os.getenv("CALM_LOOKBACK_MINUTES",      "15"))
_CHECKPOINT_TABLE = "CALM_POLL_CHECKPOINT"


# ── Checkpoint persistence ────────────────────────────────────────────────────

def _ensure_checkpoint_table() -> None:
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
        cur.execute(check, (_CHECKPOINT_TABLE, schema_q) if schema_q else (_CHECKPOINT_TABLE,))
        if int(cur.fetchone()[0]) == 0:
            cur.execute(f"""CREATE TABLE "{_CHECKPOINT_TABLE}" (
                ID           INTEGER PRIMARY KEY,
                LAST_POLLED  NVARCHAR(64) NOT NULL
            )""")
            logger.info("[CALMPoller] Created checkpoint table %s", _CHECKPOINT_TABLE)

        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("[CALMPoller] _ensure_checkpoint_table: %s", exc)


def _get_checkpoint() -> datetime:
    """Return the last poll timestamp, or now - LOOKBACK_MINS for first run."""
    try:
        from db.database import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute(f'SELECT LAST_POLLED FROM "{_CHECKPOINT_TABLE}" WHERE ID=1')
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
    except Exception as exc:
        logger.warning("[CALMPoller] _get_checkpoint failed: %s", exc)
    return datetime.now(timezone.utc) - timedelta(minutes=_LOOKBACK_MINS)


def _save_checkpoint(ts: datetime) -> None:
    try:
        from db.database import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        ts_str = ts.astimezone(timezone.utc).isoformat()
        cur.execute(
            f'MERGE INTO "{_CHECKPOINT_TABLE}" AS target '
            f'USING (SELECT 1 AS ID, ? AS LAST_POLLED FROM DUMMY) AS source '
            f'ON target.ID = source.ID '
            f'WHEN MATCHED THEN UPDATE SET target.LAST_POLLED = source.LAST_POLLED '
            f'WHEN NOT MATCHED THEN INSERT (ID, LAST_POLLED) VALUES (1, source.LAST_POLLED)',
            (ts_str,),
        )
        conn.commit()
        conn.close()
        logger.debug("[CALMPoller] Checkpoint saved: %s", ts_str)
    except Exception as exc:
        logger.error("[CALMPoller] _save_checkpoint failed: %s", exc)


# ── Main poll loop ────────────────────────────────────────────────────────────

async def poll_calm_once(orchestrator: Optional["OrchestratorAgent"] = None) -> int:
    """
    Single poll cycle. Fetches new FAILED exceptions from CALM and feeds each
    into the agent pipeline via orchestrator.process_detected_error().
    Returns number of new exceptions queued. Never raises.
    """
    from calm.exceptions_api import fetch_failed_exceptions_since
    from calm.incident_adapter import calm_exception_to_normalized
    from ingestion.dedup import is_seen, mark_seen

    since    = _get_checkpoint()
    new_at   = datetime.now(timezone.utc)
    enqueued = 0

    logger.debug("[CALMPoller] Polling exceptions since %s", since.isoformat())

    try:
        async for calm_exc in fetch_failed_exceptions_since(since):
            if is_seen(calm_exc.id):
                logger.debug("[CALMPoller] Already seen: %s", calm_exc.id)
                continue

            mark_seen(calm_exc.id)   # mark BEFORE processing to prevent races

            if orchestrator is None:
                logger.warning("[CALMPoller] Orchestrator not available — exception %s skipped", calm_exc.id)
                continue

            try:
                normalized = calm_exception_to_normalized(calm_exc)
                result     = await orchestrator.process_detected_error(normalized)
                enqueued  += 1
                logger.info(
                    "[CALMPoller] Exception %s → iflow=%s error_type=%s result=%s",
                    calm_exc.id,
                    normalized.get("iflow_id", "?"),
                    normalized.get("error_type", "?"),
                    result,
                )
            except Exception as exc:
                logger.error(
                    "[CALMPoller] process_detected_error failed for exception %s: %s",
                    calm_exc.id, exc,
                )

        _save_checkpoint(new_at)

    except Exception as exc:
        logger.error("[CALMPoller] Poll cycle error (checkpoint NOT advanced): %s", exc)

    return enqueued


async def run_calm_poller(orchestrator: Optional["OrchestratorAgent"] = None) -> None:
    """
    Long-running loop. Started as asyncio task in main.py lifespan.
    Replaces _run_cpi_monitor().
    """
    _ensure_checkpoint_table()
    logger.info(
        "[CALMPoller] Started. Poll interval=%ds, lookback=%dmin, managed_object=%s",
        _POLL_INTERVAL,
        _LOOKBACK_MINS,
        os.getenv("CALM_MANAGED_OBJECT_ID", "(all)"),
    )
    while True:
        try:
            count = await poll_calm_once(orchestrator)
            if count:
                logger.info("[CALMPoller] Cycle complete — %d new exception(s) enqueued", count)
            else:
                logger.debug("[CALMPoller] Cycle complete — no new exceptions")
        except asyncio.CancelledError:
            logger.info("[CALMPoller] Cancelled — stopping.")
            raise
        except Exception as exc:
            logger.error("[CALMPoller] Unexpected error in poll loop: %s", exc)
        try:
            await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("[CALMPoller] Cancelled during sleep — stopping.")
            raise