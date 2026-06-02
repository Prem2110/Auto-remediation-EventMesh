"""
jobs/calm_feedback_job.py
=========================
Long-running job: polls for remediated CLOUD_ALM incidents and updates the
originating CALM alert status so Cloud ALM reflects the remediation outcome.

This closes the feedback loop: CALM shows RESOLVED for auto-fixed incidents
and IN_PROGRESS for escalated ones.

Poll interval: CALM_FEEDBACK_INTERVAL_SECONDS (default 60s)
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_FEEDBACK_INTERVAL = int(os.getenv("CALM_FEEDBACK_INTERVAL_SECONDS", "60"))

# Final statuses that trigger a CALM alert update
_RESOLVED_STATUSES  = {"RESOLVED", "AUTO_FIXED", "FIX_VERIFIED"}
_ESCALATED_STATUSES = {"TICKET_CREATED", "ESCALATED", "FIX_FAILED", "FIX_FAILED_RUNTIME"}


async def _send_feedback(incident: dict) -> None:
    """Send CALM alert status update for a single remediated incident."""
    from calm.alerts_api import patch_alert_status
    from calm.models import CALMAlertStatus
    from db.database import update_incident

    # CALM back-reference stored as calm_alert_id column (added by DB migration)
    # or in tags JSON as fallback
    alert_id = incident.get("calm_alert_id", "")
    if not alert_id:
        # Fallback: try tags JSON
        import json
        try:
            tags = json.loads(incident.get("tags") or "{}")
            alert_id = tags.get("calm_alert_id", "")
        except Exception:
            pass

    if not alert_id:
        # No alert to update — mark feedback sent anyway to avoid repeated attempts
        update_incident(incident["incident_id"], {"calm_feedback_sent": 1})
        return

    iflow_id    = incident.get("iflow_id", "unknown")
    fix_summary = incident.get("fix_summary", "")[:500]
    status      = incident.get("status", "")

    if status in _RESOLVED_STATUSES:
        calm_status = CALMAlertStatus.RESOLVED
        comment = (
            f"[SAP Orbit Auto-Remediation] iFlow '{iflow_id}' was automatically remediated. "
            f"Fix: {fix_summary or 'See incident details.'}"
        )
    else:
        calm_status = CALMAlertStatus.IN_PROGRESS
        comment = (
            f"[SAP Orbit] iFlow '{iflow_id}' could not be auto-remediated (status={status}). "
            f"Escalated to engineering team. {fix_summary or ''}"
        )

    success = await patch_alert_status(
        alert_id,
        status=calm_status,
        comment=comment,
    )

    if success:
        update_incident(incident["incident_id"], {"calm_feedback_sent": 1})
        logger.info(
            "[CALMFeedback] Alert %s → %s for incident %s",
            alert_id, calm_status, incident["incident_id"],
        )
    else:
        logger.warning(
            "[CALMFeedback] Failed to update alert %s for incident %s — will retry",
            alert_id, incident["incident_id"],
        )


async def run_calm_feedback_job() -> None:
    """
    Long-running loop. Started as asyncio task in main.py lifespan.
    """
    logger.info("[CALMFeedback] Started. Interval=%ds", _FEEDBACK_INTERVAL)
    while True:
        try:
            from db.database import get_all_incidents

            # Fetch CLOUD_ALM incidents that are done but haven't sent feedback
            all_incidents = get_all_incidents(limit=200).get("incidents", [])
            pending = [
                inc for inc in all_incidents
                if inc.get("source_type") == "CLOUD_ALM"
                and inc.get("status") in (_RESOLVED_STATUSES | _ESCALATED_STATUSES)
                and not inc.get("calm_feedback_sent")
            ]

            if pending:
                logger.info("[CALMFeedback] Sending feedback for %d incident(s)", len(pending))
                for incident in pending:
                    try:
                        await _send_feedback(incident)
                    except Exception as exc:
                        logger.error(
                            "[CALMFeedback] Failed for incident %s: %s",
                            incident.get("incident_id"), exc,
                        )

        except asyncio.CancelledError:
            logger.info("[CALMFeedback] Cancelled — stopping.")
            raise
        except Exception as exc:
            logger.error("[CALMFeedback] Unexpected error: %s", exc)

        try:
            await asyncio.sleep(_FEEDBACK_INTERVAL)
        except asyncio.CancelledError:
            logger.info("[CALMFeedback] Cancelled during sleep — stopping.")
            raise