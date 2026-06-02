"""
calm/incident_adapter.py
========================
Translates a CALMException into the normalized incident dict that
orchestrator.process_detected_error() expects.

The output format EXACTLY matches what orchestrator._normalize_event_message()
returns — same keys, same semantics — so the entire downstream pipeline
(Classifier → Observer → RCA → Fix → Verifier) runs with zero changes.

CALM-specific fields (calm_exception_id, calm_alert_id) are passed through
so the feedback job can update CALM after remediation.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from calm.models import CALMException

# Map CALM errorCategory → Orbit error_type
# These are hints only — the Classifier will refine/override via rule engine + LLM
_CATEGORY_MAP: dict[str, str] = {
    "CONNECTIVITY":   "ReceiverNotFoundError",
    "MAPPING":        "MappingError",
    "SECURITY":       "AuthenticationError",
    "CONFIGURATION":  "ConfigurationError",
    "PROCESSING":     "ProcessingError",
    "TIMEOUT":        "TimeoutError",
    "AUTHORIZATION":  "AuthenticationError",
    "INTEGRATION":    "ProcessingError",
}


def calm_exception_to_normalized(exc: "CALMException") -> dict:
    """
    Convert a CALMException to the normalized incident dict.

    This dict is passed directly to orchestrator.process_detected_error()
    which will classify, dedup, create the DB incident, and hand off
    to the observer stage — all unchanged.

    CALM-specific fields are stored in source_type and prefixed keys
    so they don't collide with the standard Orbit schema.
    """
    error_type = _CATEGORY_MAP.get(exc.errorCategory or "", "")
    start_iso  = exc.startTime.isoformat() if exc.startTime else ""
    end_iso    = exc.endTime.isoformat()   if exc.endTime   else ""

    return {
        # ── Standard Orbit incident fields (matches _normalize_event_message output) ──
        "source_type":    "CLOUD_ALM",
        "message_guid":   exc.messageId or exc.id,          # CPI MessageGuid or CALM exception ID
        "iflow_id":       exc.technicalName or "",           # SAP CPI iFlow technical name
        "artifact_id":    "",                                # will be resolved by observer via OData
        "sender":         exc.sender or "",
        "receiver":       exc.receiver or "",
        "status":         "FAILED",
        "log_start":      start_iso,
        "log_end":        end_iso,
        "error_message":  exc.errorMessage or "",
        "correlation_id": exc.correlationId or exc.id,
        "error_type":     error_type,                       # classifier will override this

        # ── CALM back-reference fields ────────────────────────────────────────────
        # Stored via create_incident → used by calm_feedback_job to patch alert status
        "calm_exception_id":    exc.id,
        "calm_alert_id":        exc.alertId or "",
        "calm_managed_object":  exc.managedObjectId or "",
    }