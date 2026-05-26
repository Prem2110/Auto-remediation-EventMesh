"""
jobs/itsm_poller.py
===================
Polls ITSM every ITSM_POLL_INTERVAL seconds for tickets that have been closed.

ITSM GET /odata/v4/ticket/Tickets/{ID} response shape (confirmed):
  - "ID"              → ticket identifier
  - "status"          → current status string
  - "resolutionNotes" → how the engineer resolved it (maps to our resolution_notes)
  - "modifiedAt"      → last modified timestamp
  - "modifiedBy"      → who last modified
  - "ticketNumber"    → human-readable ticket number (used in log messages)

Closed status values to watch — update _RESOLVED_STATUSES once confirmed
with ITSM team what value "status" becomes when a ticket is closed.
Currently set to the most common OData ITSM conventions.

When a ticket is resolved:
  1. EM_ESCALATION_TICKETS  → status=RESOLVED, resolution_notes filled
  2. EM_AUTONOMOUS_INCIDENTS → status=HUMAN_RESOLVED, resolved_at filled
  3. EM_FIX_PATTERNS         → new row inserted so agents reuse the fix
  4. pattern_stored=1        → prevents duplicate pattern inserts
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from db.database import (
    get_incident_by_id,
    get_open_itsm_tickets,
    update_escalation_ticket,
    update_incident,
    upsert_fix_pattern,
)
from integrations.itsm_client import get_itsm_ticket, _resolve_itsm_destination

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("ITSM_POLL_INTERVAL", "300"))  # default 5 min

# Confirmed status values from OhZone ITSM: New, Assigned, Pending, Resolved, Cancelled
_RESOLVED_STATUSES = {"resolved", "cancelled"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def poll_itsm_tickets_once(classifier=None) -> None:
    """Single poll cycle — fetches all open tickets and checks their ITSM status."""
    open_tickets = get_open_itsm_tickets()
    if not open_tickets:
        logger.debug("[ITSMPoller] No open tickets to poll")
        return

    # Pre-flight: verify ITSM credentials are available before iterating tickets.
    # Without this guard every ticket triggers an individual credential error.
    creds_ok = await _resolve_itsm_destination()
    if not creds_ok:
        logger.warning(
            "[ITSMPoller] Skipping poll of %d ticket(s) — ITSM credentials unavailable (VCAP_SERVICES not configured)",
            len(open_tickets),
        )
        return

    logger.info("[ITSMPoller] Polling %d open ticket(s) against ITSM", len(open_tickets))

    for ticket in open_tickets:
        itsm_id     = ticket.get("itsm_ticket_id")
        ticket_id   = ticket.get("ticket_id")
        incident_id = ticket.get("incident_id")

        if not itsm_id:
            continue

        try:
            itsm_data = await get_itsm_ticket(itsm_id)
            if not itsm_data:
                logger.info(
                    "[ITSMPoller] itsm_id=%s not found in ITSM — marking STALE to stop polling "
                    "(ticket_id=%s incident_id=%s)",
                    itsm_id, ticket_id, incident_id,
                )
                try:
                    update_escalation_ticket(ticket_id, {"status": "STALE"})
                except Exception as _stale_exc:
                    logger.debug("[ITSMPoller] Could not mark ticket %s as STALE: %s", ticket_id, _stale_exc)
                continue

            itsm_status = str(itsm_data.get("status", "")).lower()
            logger.debug("[ITSMPoller] itsm_id=%s status=%s", itsm_id, itsm_status)

            if itsm_status not in _RESOLVED_STATUSES:
                continue

            # ── Ticket is closed in ITSM ─────────────────────────────────────

            # Confirmed ITSM field names from GET response
            resolution_notes = itsm_data.get("resolutionNotes") or ""
            resolved_by      = itsm_data.get("modifiedBy")      or ""
            resolved_at      = itsm_data.get("modifiedAt")      or _now_iso()
            ticket_number    = itsm_data.get("ticketNumber")     or itsm_id

            logger.info(
                "[ITSMPoller] Ticket %s (%s) closed by %s — syncing to Orbit",
                ticket_number, itsm_id, resolved_by,
            )

            # 1. Update EM_ESCALATION_TICKETS → Orbit ticket list reflects RESOLVED
            update_escalation_ticket(ticket_id, {
                "status":           "RESOLVED",
                "resolution_notes": resolution_notes,
                "resolved_at":      resolved_at,
            })

            # 2. Update EM_AUTONOMOUS_INCIDENTS → Orbit incident list reflects HUMAN_RESOLVED
            if incident_id:
                update_incident(incident_id, {
                    "status":      "HUMAN_RESOLVED",
                    "fix_summary": resolution_notes,
                    "resolved_at": resolved_at,
                })

            # 3. Store human fix into EM_FIX_PATTERNS for agent reuse
            if resolution_notes and incident_id and not ticket.get("pattern_stored"):
                incident = get_incident_by_id(incident_id)
                if incident and classifier:
                    try:
                        error_sig = classifier.error_signature(
                            incident.get("iflow_id",      ""),
                            incident.get("error_type",    ""),
                            incident.get("error_message", ""),
                        )
                        upsert_fix_pattern({
                            "error_signature": error_sig,
                            "iflow_id":        incident.get("iflow_id",   ""),
                            "error_type":      incident.get("error_type", ""),
                            "root_cause":      incident.get("root_cause", resolution_notes),
                            "fix_applied":     resolution_notes,
                            "outcome":         "SUCCESS",
                            "key_steps":       [],
                        }, replay_success=True)

                        # 4. Prevent duplicate pattern inserts on next poll
                        update_escalation_ticket(ticket_id, {"pattern_stored": 1})

                        logger.info(
                            "[ITSMPoller] Human fix stored as pattern for iflow=%s",
                            incident.get("iflow_id"),
                        )
                    except Exception as exc:
                        logger.warning("[ITSMPoller] Failed to store fix pattern: %s", exc)

            logger.info(
                "[ITSMPoller] ticket_id=%s synced — incident=%s now HUMAN_RESOLVED",
                ticket_id, incident_id,
            )

        except Exception as exc:
            logger.error(
                "[ITSMPoller] Error checking ticket %s (itsm_id=%s): %s",
                ticket_id, itsm_id, exc,
            )


async def run_itsm_poller(classifier=None) -> None:
    """Long-running loop. Started as an asyncio task in main.py on startup."""
    logger.info("[ITSMPoller] Started. Poll interval: %ds", POLL_INTERVAL)
    while True:
        try:
            await poll_itsm_tickets_once(classifier)
        except asyncio.CancelledError:
            logger.info("[ITSMPoller] Cancelled — stopping.")
            raise
        except Exception as exc:
            logger.error("[ITSMPoller] Unexpected error in poll cycle: %s", exc)
        try:
            await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("[ITSMPoller] Cancelled during sleep — stopping.")
            raise
