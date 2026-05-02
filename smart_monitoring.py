"""
Smart Monitoring Router — Sierra Digital

Backend API powering the Smart Monitoring UI.

Endpoints:
  GET  /smart-monitoring/messages                          — List failed CPI messages (filterable)
  GET  /smart-monitoring/messages/paginated                — List failed CPI messages with pagination (no time_range)
  GET  /smart-monitoring/messages/{guid}                   — Full detail view (all 6 tabs)
  POST /smart-monitoring/messages/{guid}/analyze           — Run AI RCA
  POST /smart-monitoring/messages/{guid}/generate_fix_patch — Generate detailed fix plan
  POST /smart-monitoring/messages/{guid}/apply_fix         — Apply fix via MCP tools
  POST /smart-monitoring/chat                              — Ask AI about a specific error
  GET  /smart-monitoring/stats                             — Dashboard statistics
  GET  /smart-monitoring/incidents                         — List all incidents
  GET  /smart-monitoring/incidents/{incident_id}/fix_status — Poll fix progress
  GET  /smart-monitoring/total-errors                      — Get total count of failed messages
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import logging
import uuid
import json
import re

from db.database import (
    create_incident,
    update_incident,
    get_incident_by_id,
    get_incident_by_message_guid,
    get_all_incidents,
    count_all_incidents,
    get_open_incident_by_signature,
    increment_incident_occurrence,
    get_escalation_tickets,
    get_escalation_ticket_by_id,
    update_escalation_ticket,
    clear_all_em_tables,
)
from utils.utils import get_hana_timestamp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/smart-monitoring", tags=["Smart Monitoring"])


# ─────────────────────────────────────────────────────────────────────────────
# DEPENDENCY — lazy import avoids circular import with main.py
# ─────────────────────────────────────────────────────────────────────────────

class _MCPCompat:
    """
    Compatibility shim: maps the legacy monolithic mcp_manager interface used
    throughout this file to the new split architecture (MultiMCP + ObserverAgent
    + OrchestratorAgent) introduced in the refactored main.py.
    """
    def __init__(self, mcp, observer, orchestrator) -> None:
        self._mcp         = mcp
        self._observer    = observer
        self._orchestrator = orchestrator

    @property
    def error_fetcher(self):
        return self._observer.error_fetcher

    @property
    def llm(self):
        return self._mcp.llm

    @property
    def _autonomous_running(self) -> bool:
        return self._orchestrator._autonomous_running if self._orchestrator else False

    def classify_error(self, error_message: str):
        return self._orchestrator._classifier.classify_error(error_message)

    @staticmethod
    def incident_group_key(incident):
        from agents.orchestrator_agent import OrchestratorAgent  # noqa: PLC0415
        return OrchestratorAgent.incident_group_key(incident)

    async def run_rca(self, incident):
        return await self._orchestrator._rca.run_rca(incident)

    async def execute_incident_fix(self, incident, human_approved: bool = False, deploy_only: bool = False, force: bool = False):
        return await self._orchestrator.execute_incident_fix(
            incident, human_approved=human_approved, deploy_only=deploy_only, force=force
        )

    async def ask(self, query: str, user_id: str, session_id: str, timestamp: str):
        return await self._orchestrator.ask(query, user_id, session_id, timestamp)

    def get_tool_field_names(self, server: str, mcp_tool_name: str):
        return self._mcp.get_tool_field_names(server, mcp_tool_name)

    @property
    def tools(self):
        return self._mcp.tools


def _get_mcp():
    """
    Lazy-import the global mcp from main.py at request time.
    This avoids a circular import at module-load time because smart_monitoring.py
    is imported by main.py.
    """
    import main as _main  # noqa: PLC0415
    _mcp  = getattr(_main, "mcp_manager", None) or getattr(_main, "mcp", None)
    _obs  = getattr(_main, "observer", None)
    _orch = getattr(_main, "orchestrator", None)
    if _mcp is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    # Legacy main_legacy.py already has full interface; new main.py needs the shim.
    if not hasattr(_mcp, "error_fetcher"):
        return _MCPCompat(_mcp, _obs, _orch)
    return _mcp


def _recommended_action(status: str, error_type: str) -> str:
    """Return a one-liner action hint for the given status/error_type pair."""
    import main as _main  # noqa: PLC0415
    status_hints = getattr(_main, "_STATUS_ACTION_HINTS", {})
    error_hints  = getattr(_main, "ACTION_HINTS", {})
    return (
        status_hints.get(status or "")
        or error_hints.get(error_type or "", "No action hint available.")
    )


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    user_id: str = "user"


class GenerateFixPatchRequest(BaseModel):
    user_id: str = "user"


class ApplyFixRequest(BaseModel):
    user_id: str
    proposed_fix: Optional[str] = None  # override RCA proposed_fix


class ChatRequest(BaseModel):
    query: str
    user_id: str
    session_id: Optional[str] = None
    message_guid: Optional[str] = None  # provide context for AI


class RetryFixRequest(BaseModel):
    user_id: str


class ExplainErrorRequest(BaseModel):
    user_id: str = "user"


# ─────────────────────────────────────────────────────────────────────────────
# TIME / FORMATTING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sap_timestamp(ts: str) -> Optional[datetime]:
    """Parse SAP /Date(ms)/ or ISO timestamp → datetime (UTC, naive)."""
    if not ts:
        return None
    try:
        if "/Date(" in ts:
            ms = int(re.search(r"\d+", ts).group())
            return datetime.utcfromtimestamp(ms / 1000)
        clean = ts.replace("Z", "").split("+")[0].split(".")[0]
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def _format_ts(ts: str) -> str:
    """Return human-readable timestamp: 'Jan 07, 2026, 12:30:06'."""
    if not ts:
        return ""
    dt = _parse_sap_timestamp(ts)
    return dt.strftime("%b %d, %Y, %H:%M:%S") if dt else ts


def _relative_time(ts: str) -> str:
    """Return a relative label: 'Today', '10 Days Ago', 'One Month Ago'."""
    if not ts:
        return ""
    dt = _parse_sap_timestamp(ts)
    if not dt:
        return ""
    delta = datetime.utcnow() - dt
    if delta.days < 0:
        return "Just now"
    if delta.days == 0:
        return "Today"
    if delta.days == 1:
        return "Yesterday"
    if delta.days < 30:
        return f"{delta.days} Days Ago"
    months = delta.days // 30
    return "One Month Ago" if months == 1 else f"{months} Months Ago"


def _parse_time_range_cutoff(time_range: Optional[str]) -> Optional[datetime]:
    """Convert a UI time-range label to a UTC cutoff datetime."""
    if not time_range:
        return None
    tr = (time_range or "").lower()
    now = datetime.utcnow()
    mapping = {
        "1 hour":   timedelta(hours=1),
        "24 hour":  timedelta(hours=24),
        "today":    timedelta(hours=24),
        "7 day":    timedelta(days=7),
        "week":     timedelta(days=7),
        "30 day":   timedelta(days=30),
        "month":    timedelta(days=30),
    }
    for key, delta in mapping.items():
        if key in tr:
            return now - delta
    return None


def _extract_duration(raw: Dict) -> str:
    """Compute processing duration string from SAP raw record."""
    try:
        s = _parse_sap_timestamp(str(raw.get("LogStart") or ""))
        e = _parse_sap_timestamp(str(raw.get("LogEnd") or ""))
        if s and e:
            total_ms = int((e - s).total_seconds() * 1000)
            secs, ms = total_ms // 1000, total_ms % 1000
            return f"{secs} sec {ms} ms"
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# FIELD-CHANGE EXTRACTION (for highlighting in UI)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_field_changes(error_message: str, proposed_fix: str) -> List[Dict[str, str]]:
    """
    Extract field rename/change highlights from error message and proposed fix text.
    Returns list of {old_field, new_field} for the UI to highlight in colour.
    """
    changes: List[Dict[str, str]] = []
    seen_old: set = set()

    def _add(old: str, new: str, src: str) -> None:
        if old and new and old.lower() != new.lower() and old not in seen_old:
            seen_old.add(old)
            changes.append({"old_field": old, "new_field": new, "source": src})

    # "Field 'X' does not exist … Did you mean 'Y'?"
    m = re.search(
        r"[Ff]ield\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s+does not exist.*?"
        r"[Dd]id you mean\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?",
        error_message or "",
    )
    if m:
        _add(m.group(1), m.group(2), "error_message")

    # Proposed-fix text: "X renamed to Y" / "replace X with Y"
    for pat in (
        r"['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s+(?:was\s+)?renamed?\s+(?:to\s+)?['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?",
        r"(?:replace|rename)\s+(?:field\s+)?['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s+(?:with|to)\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?",
    ):
        for mm in re.finditer(pat, proposed_fix or "", re.IGNORECASE):
            _add(mm.group(1), mm.group(2), "proposed_fix")

    return changes


# ─────────────────────────────────────────────────────────────────────────────
# FILTER HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _message_matches_filter(
    raw: Dict[str, Any],
    status: Optional[str],
    time_range: Optional[str],
    msg_id: Optional[str],
    artifact: Optional[str],
) -> bool:
    """Return True if a raw SAP message record passes the UI filter criteria."""
    # Status filter
    if status:
        raw_status = str(raw.get("Status") or "FAILED").upper()
        if raw_status != status.upper():
            return False

    # ID / iFlow name filter
    if msg_id:
        guid   = str(raw.get("MessageGuid", "") or "").lower()
        iflow  = str(raw.get("IntegrationFlowName", "") or "").lower()
        if msg_id.lower() not in guid and msg_id.lower() not in iflow:
            return False

    # Artifact filter
    if artifact:
        iflow = str(raw.get("IntegrationFlowName", "") or "").lower()
        if artifact.lower() not in iflow:
            return False

    # Time range filter
    cutoff = _parse_time_range_cutoff(time_range)
    if cutoff:
        log_dt = _parse_sap_timestamp(str(raw.get("LogEnd") or ""))
        if log_dt and log_dt < cutoff:
            return False

    return True


def _message_matches_filter_no_time(
    raw: Dict[str, Any],
    status: Optional[str],
    msg_id: Optional[str],
    artifact: Optional[str],
) -> bool:
    """Return True if a raw SAP message record passes the UI filter criteria (without time_range)."""
    # Status filter
    if status:
        raw_status = str(raw.get("Status") or "FAILED").upper()
        if raw_status != status.upper():
            return False

    # ID / iFlow name filter
    if msg_id:
        guid   = str(raw.get("MessageGuid", "") or "").lower()
        iflow  = str(raw.get("IntegrationFlowName", "") or "").lower()
        if msg_id.lower() not in guid and msg_id.lower() not in iflow:
            return False

    # Artifact filter
    if artifact:
        iflow = str(raw.get("IntegrationFlowName", "") or "").lower()
        if artifact.lower() not in iflow:
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# DB-BASED FILTER HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _incident_matches_filter(
    inc: Dict,
    status: Optional[str],
    time_range: Optional[str],
    msg_id: Optional[str],
    artifact: Optional[str],
) -> bool:
    """Return True if a DB incident passes UI filter criteria."""
    inc_status = (inc.get("status") or "").upper()

    if status:
        s = status.upper()
        # Legacy "FAILED" filter → show all incidents (they all originate from failures)
        if s not in ("FAILED", "ALL", "") and s != inc_status:
            return False

    if msg_id:
        q = msg_id.lower()
        guid   = (inc.get("message_guid") or "").lower()
        iflow  = (inc.get("iflow_id") or "").lower()
        inc_id = (inc.get("incident_id") or "").lower()
        if q not in guid and q not in iflow and q not in inc_id:
            return False

    if artifact:
        iflow = (inc.get("iflow_id") or "").lower()
        if artifact.lower() not in iflow:
            return False

    cutoff = _parse_time_range_cutoff(time_range)
    if cutoff:
        # Prefer last_seen (updated on every correlation) over log_end (frozen at
        # the original AEM event time) so recently active incidents are always visible.
        ts = str(inc.get("last_seen") or inc.get("log_end") or inc.get("created_at") or "")
        dt = _parse_sap_timestamp(ts)
        if dt and dt < cutoff:
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# INCIDENT LIFECYCLE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_incident_for_guid(
    guid: str,
    raw: Dict,
    error_detail: Dict,
    mcp,
    run_rca: bool = True,
) -> Dict:
    """
    Ensure an incident record exists for the given CPI message GUID.
    - If one exists with no actionable RCA, re-runs RCA.
    - If none exists, creates one and (optionally) runs RCA.
    Returns the freshest incident dict from the DB.
    """
    existing = get_incident_by_message_guid(guid)
    if existing:
        # Decide whether to re-run RCA
        re_run = run_rca and existing.get("status") in {"DETECTED", "RCA_FAILED"} or (
            run_rca and not _has_actionable_fix(existing)
        )
        if re_run:
            classification = mcp.classify_error(existing.get("error_message", ""))
            merged = {**existing, **classification}
            update_incident(existing["incident_id"], {
                "status": "RCA_IN_PROGRESS",
                "error_type": classification["error_type"],
            })
            rca = await mcp.run_rca(merged)
            update_incident(existing["incident_id"], {
                "status":             "RCA_COMPLETE",
                "root_cause":         rca.get("root_cause", ""),
                "proposed_fix":       rca.get("proposed_fix", ""),
                "rca_confidence":     rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
                "error_type":         rca.get("error_type") or classification["error_type"],
            })
        return get_incident_by_message_guid(guid) or existing

    # No existing incident — normalise and create one
    normalized = mcp.error_fetcher.normalize(raw, error_detail)
    classification = mcp.classify_error(normalized.get("error_message", ""))
    normalized.update(classification)

    incident_id = str(uuid.uuid4())
    incident = {
        **normalized,
        "incident_id":         incident_id,
        "status":              "DETECTED",
        "created_at":          get_hana_timestamp(),
        "incident_group_key":  mcp.incident_group_key(normalized),
        "occurrence_count":    1,
        "last_seen":           get_hana_timestamp(),
        "verification_status": "UNVERIFIED",
    }
    create_incident(incident)

    if run_rca:
        update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
        try:
            rca = await mcp.run_rca(incident)
            update_incident(incident_id, {
                "status":             "RCA_COMPLETE",
                "root_cause":         rca.get("root_cause", ""),
                "proposed_fix":       rca.get("proposed_fix", ""),
                "rca_confidence":     rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
                "error_type":         rca.get("error_type") or classification["error_type"],
            })
        except Exception as e:
            logger.error(f"[SmartMonitoring] RCA failed for new incident {incident_id}: {e}")
            update_incident(incident_id, {"status": "RCA_FAILED"})

    return get_incident_by_message_guid(guid) or incident


def _has_actionable_fix(incident_or_rca: Dict) -> bool:
    """True when both proposed_fix and root_cause are non-empty strings."""
    return bool(
        (incident_or_rca.get("proposed_fix") or "").strip()
        and (incident_or_rca.get("root_cause") or "").strip()
    )


# ─────────────────────────────────────────────────────────────────────────────
# TAB BUILDERS — each returns data for one UI tab
# ─────────────────────────────────────────────────────────────────────────────

def _tab_error_details(incident: Dict, raw: Optional[Dict] = None) -> Dict:
    raw = raw or {}
    return {
        "error_message":  incident.get("error_message", ""),
        "error_type":     incident.get("error_type", ""),
        "status":         "Failed",
        "log_start":      _format_ts(str(incident.get("log_start") or raw.get("LogStart") or "")),
        "log_end":        _format_ts(str(incident.get("log_end") or raw.get("LogEnd") or "")),
        "last_updated":   _format_ts(
            str(incident.get("log_end") or incident.get("last_seen") or incident.get("created_at") or "")
        ),
        "raw_error_text": incident.get("error_message", ""),
    }


def _tab_ai_recommendation(incident: Dict) -> Dict:
    error_message  = incident.get("error_message", "") or ""
    proposed_fix   = incident.get("proposed_fix", "") or ""
    root_cause     = incident.get("root_cause", "") or ""
    confidence     = float(incident.get("rca_confidence") or 0.0)
    error_type     = incident.get("error_type", "")
    status         = incident.get("status", "")

    field_changes  = _extract_field_changes(error_message, proposed_fix)
    conf_label     = "High" if confidence >= 0.90 else ("Medium" if confidence >= 0.70 else "Low")

    # Show "Generate Fix" whenever RCA data exists and fix plan not yet stored
    _NO_FIX_STATUSES = {
        "AUTO_FIXED", "HUMAN_FIXED", "FIX_VERIFIED", "RETRIED", "SUCCESS",
        "FIX_IN_PROGRESS", "RCA_IN_PROGRESS", "FIX_APPLIED_PENDING_VERIFICATION",
        "TICKET_CREATED", "PENDING_APPROVAL", "AWAITING_APPROVAL",
    }
    has_stored_plan = bool(incident.get("fix_steps"))
    can_fix = bool(
        (root_cause or proposed_fix)
        and status not in _NO_FIX_STATUSES
        and not has_stored_plan
    )

    # Re-hydrate stored fix plan so the frontend can restore its state on reload
    fix_patch = None
    if has_stored_plan:
        try:
            raw_steps = incident.get("fix_steps")
            steps = json.loads(raw_steps) if isinstance(raw_steps, str) else (raw_steps or [])
            raw_fc = incident.get("field_changes")
            stored_fc = json.loads(raw_fc) if isinstance(raw_fc, str) else (raw_fc or field_changes)
            _TERMINAL = {"AUTO_FIXED", "HUMAN_FIXED", "FIX_VERIFIED", "RETRIED",
                         "FIX_IN_PROGRESS", "TICKET_CREATED"}
            fix_patch = {
                "incident_id":       incident.get("incident_id", ""),
                "message_guid":      incident.get("message_guid", ""),
                "iflow_id":          incident.get("iflow_id", ""),
                "error_type":        error_type,
                "summary":           root_cause or proposed_fix,
                "summary_structured": {
                    "diagnosis":     root_cause,
                    "field_changes": stored_fc,
                    "proposed_fix":  proposed_fix,
                },
                "steps":             steps,
                "confidence":        confidence,
                "confidence_label":  conf_label,
                "confidence_display": f"{confidence:.2f} ({conf_label})",
                "affected_component": incident.get("affected_component", ""),
                "ready_to_apply":    True,
                "can_apply":         status not in _TERMINAL,
            }
        except Exception:
            pass

    return {
        "diagnosis":           root_cause,
        "proposed_fix":        proposed_fix,
        "field_changes":       field_changes,
        "confidence":          confidence,
        "confidence_label":    conf_label,
        "confidence_display":  f"{confidence:.2f} ({conf_label})",
        "error_type":          error_type,
        "affected_component":  incident.get("affected_component", ""),
        "can_generate_fix":    can_fix,
        "fix_status":          status,
        "fix_summary":         incident.get("fix_summary", "") or "",
        "fix_patch":           fix_patch,
    }


def _tab_properties(incident: Dict, metadata: Optional[Dict] = None) -> Dict:
    meta = metadata or {}
    return {
        "message": {
            "message_id":       incident.get("message_guid") or meta.get("MessageGuid"),
            "mpl_id":           meta.get("MessageGuid") or incident.get("message_guid"),
            "correlation_id":   incident.get("correlation_id") or meta.get("CorrelationId"),
            "sender":           incident.get("sender") or meta.get("Sender"),
            "receiver":         incident.get("receiver") or meta.get("Receiver"),
            "interface_iflow":  incident.get("iflow_id") or meta.get("IntegrationFlowName"),
            "status":           "Failed",
            "processing_start": _format_ts(str(incident.get("log_start") or meta.get("LogStart") or "")),
            "processing_end":   _format_ts(str(incident.get("log_end") or meta.get("LogEnd") or "")),
        },
        "adapter": {
            "sender_adapter":   meta.get("SenderAdapterType"),
            "receiver_adapter": meta.get("ReceiverAdapterType"),
            "content_type":     meta.get("ContentType"),
            "retry_count":      meta.get("RetryCount"),
        },
        "business_context": {
            "material_id":  meta.get("MaterialId"),
            "plant":        meta.get("Plant"),
            "company_code": meta.get("CompanyCode"),
        },
    }


def _tab_artifact(incident: Dict, metadata: Optional[Dict] = None) -> Dict:
    meta = metadata or {}
    return {
        "name":        incident.get("iflow_id") or meta.get("IntegrationFlowName"),
        "artifact_id": meta.get("IntegrationFlowId") or incident.get("iflow_id"),
        "version":     meta.get("Version"),
        "package":     meta.get("PackageId") or meta.get("PackageName"),
        "deployed_on": _format_ts(str(meta.get("LogEnd") or incident.get("log_end") or "")),
        "deployed_by": meta.get("User") or meta.get("CreatedBy"),
        "runtime_node": meta.get("Node") or meta.get("RuntimeNode"),
        "status":      meta.get("Status") or "FAILED",
    }


def _tab_history(incident: Dict) -> List[Dict]:
    """Build the History tab timeline entries."""
    timeline = []

    def _entry(step: str, ts: str, desc: str, status: str) -> Optional[Dict]:
        if not ts and not desc:
            return None
        return {
            "step":          step,
            "timestamp":     _format_ts(ts),
            "timestamp_raw": ts,
            "description":   desc,
            "status":        status,
        }

    e = _entry(
        "Detected",
        str(incident.get("created_at") or ""),
        "CPI error detected and incident created.",
        "completed",
    )
    if e:
        timeline.append(e)

    inc_status = (incident.get("status") or "").upper()

    if inc_status not in {"DETECTED"}:
        rca_ts = str(incident.get("last_seen") or incident.get("created_at") or "")
        rca_desc = (
            f"Root cause identified: {(incident.get('root_cause') or '')[:150]}"
            if incident.get("root_cause") else "Root cause analysis completed."
        )
        e = _entry("RCA Analysis", rca_ts, rca_desc, "completed")
        if e:
            timeline.append(e)

    if inc_status in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX"}:
        e = _entry(
            "Fix Applied",
            str(incident.get("resolved_at") or incident.get("last_seen") or ""),
            (incident.get("fix_summary") or "Fix applied and iFlow redeployed.")[:200],
            "completed",
        )
        if e:
            timeline.append(e)
    elif inc_status in {"FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME"}:
        stage_label = {
            "FIX_FAILED_UPDATE": "Update Failed",
            "FIX_FAILED_DEPLOY": "Deploy Failed",
            "FIX_FAILED_RUNTIME": "Runtime Failure Post-Fix",
        }.get(inc_status, "Fix Attempted")
        e = _entry(
            stage_label,
            str(incident.get("last_seen") or incident.get("created_at") or ""),
            (incident.get("fix_summary") or "Fix attempt failed.")[:200],
            "failed",
        )
        if e:
            timeline.append(e)
    elif inc_status in {"AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW"}:
        e = _entry(
            "Awaiting Approval",
            str(incident.get("last_seen") or incident.get("created_at") or ""),
            "Fix proposal generated and awaiting human approval.",
            "pending",
        )
        if e:
            timeline.append(e)
    elif inc_status == "FIX_IN_PROGRESS":
        e = _entry(
            "Fix In Progress",
            str(incident.get("last_seen") or incident.get("created_at") or ""),
            "AI is applying the fix and deploying the iFlow.",
            "in_progress",
        )
        if e:
            timeline.append(e)

    occ = int(incident.get("occurrence_count") or 1)
    if occ > 1:
        e = _entry(
            "Recurrence",
            str(incident.get("last_seen") or ""),
            f"Error occurred {occ} times. Last seen: {_format_ts(str(incident.get('last_seen') or ''))}",
            "info",
        )
        if e:
            timeline.append(e)

    return [t for t in timeline if t is not None]


# ─────────────────────────────────────────────────────────────────────────────
# FIX-PLAN GENERATION
# ─────────────────────────────────────────────────────────────────────────────

async def _generate_fix_plan_steps(
    mcp,
    iflow_id: str,
    error_type: str,
    root_cause: str,
    proposed_fix: str,
    affected_component: str,
    error_message: str,
    field_changes: List[Dict],
) -> List[Dict]:
    """
    Ask the LLM (directly, no tool loop) to produce a numbered fix-plan list.
    Falls back to rule-based steps if the LLM call fails.
    """
    fc_hint = ""
    if field_changes:
        fc_hint = "Field changes detected:\n" + "\n".join(
            f"  - '{fc['old_field']}' → '{fc['new_field']}'" for fc in field_changes
        )

    prompt = f"""You are a SAP CPI expert. Generate a numbered, step-by-step fix plan for the issue below.
Do NOT apply any changes — produce only a human-readable review plan.

Issue:
- iFlow: {iflow_id}
- Error Type: {error_type}
- Root Cause: {root_cause}
- Error Message: {error_message}
- Proposed Fix: {proposed_fix}
- Affected Component: {affected_component}
{fc_hint}

Return ONLY valid JSON — no markdown fences, no extra text:
{{
  "steps": [
    {{
      "step_number": 1,
      "title": "<short imperative action>",
      "description": "<1-2 sentence detailed instruction referencing exact field/artifact names>",
      "sub_steps": ["<optional bullet>"],
      "note": null
    }}
  ]
}}

Requirements:
- 4 to 7 steps
- Steps should flow: Open iFlow → Locate issue → Apply fix → Validate → Redeploy
- Reference exact field names, artifact names, and SAP UI paths
- sub_steps may be empty list []
"""

    try:
        # Call the LLM directly (no agent tool-loop needed for planning)
        from langchain_core.messages import HumanMessage  # noqa: PLC0415

        response = await mcp.llm.ainvoke([HumanMessage(content=prompt)])
        answer = response.content if hasattr(response, "content") else str(response)
        clean = re.sub(r"```(?:json)?|```", "", answer).strip()
        parsed = json.loads(clean)
        steps = parsed.get("steps", [])
        if steps and isinstance(steps, list):
            return steps
    except Exception as e:
        logger.warning(f"[SmartMonitoring] LLM fix-plan generation failed ({e}), using fallback")

    return _rule_based_fix_steps(iflow_id, error_type, proposed_fix, field_changes, affected_component)


def _rule_based_fix_steps(
    iflow_id: str,
    error_type: str,
    proposed_fix: str,
    field_changes: List[Dict],
    affected_component: str,
) -> List[Dict]:
    """Deterministic fallback fix-plan when the LLM is unavailable."""
    n = 1

    def _step(title: str, desc: str, sub: Optional[List[str]] = None, note: Optional[str] = None) -> Dict:
        nonlocal n
        s = {
            "step_number": n,
            "title":       title,
            "description": desc,
            "sub_steps":   sub or [],
            "note":        note,
        }
        n += 1
        return s

    steps = []

    if error_type == "MAPPING_ERROR":
        steps.append(_step(
            f"Open iFlow '{iflow_id}' in Integration Flow Designer",
            f"Navigate to SAP Integration Suite → Design → Integrations and APIs. "
            f"Open the iFlow '{iflow_id}'.",
        ))
        steps.append(_step(
            "Locate the Message Mapping step",
            f"Find the Message Mapping step within the iFlow (search for 'mapping' or "
            f"'Message Mapping' in the step list).",
            note="You may need to expand the iFlow canvas to find all mapping artifacts."
        ))
        if field_changes:
            for fc in field_changes:
                steps.append(_step(
                    f"Search for field: {fc['old_field']}",
                    f"In the mapping editor, search for all occurrences of '{fc['old_field']}'.",
                    sub=[
                        f"Replace the target field mapping from **{fc['old_field']}** to **{fc['new_field']}**",
                        f"If **{fc['old_field']}** is referenced in expressions or calculations, "
                        f"update the output to target **{fc['new_field']}**",
                    ],
                ))
        else:
            steps.append(_step(
                "Correct the mapping",
                proposed_fix or "Identify and correct the invalid field references in the mapping editor.",
            ))
        steps.append(_step(
            "Refresh target metadata (XSD/EDMX)",
            "Re-import or update the target structure (XSD/EDMX) to the latest version.",
            sub=[
                "Re-import / update target XSD/EDMX (latest version)",
                "Rebuild/validate mapping to ensure all fields exist and types match",
            ],
            note="Always use the latest version of the target structure to avoid stale references."
        ))
        steps.append(_step(
            f"Validate and redeploy iFlow '{iflow_id}'",
            "Save all changes, run iFlow validation, and deploy to the runtime.",
            sub=["Click 'Deploy' in the Integration Flow Designer toolbar"],
            note="Monitor deploy status in Operations → Monitor → Integrations."
        ))

    elif error_type == "AUTH_ERROR":
        steps.append(_step(
            "Inspect the receiver adapter security settings",
            f"Open iFlow '{iflow_id}' and locate the receiver adapter step that uses credentials.",
        ))
        steps.append(_step(
            "Check Security Material in SAP Integration Suite",
            "Navigate to Monitor → Manage Security → Security Material. "
            "Verify the credential alias referenced by the iFlow exists and is not expired.",
        ))
        steps.append(_step(
            "Refresh or recreate credentials",
            "Update the OAuth token, basic auth password, or certificate in Security Material.",
            note="Coordinate with the target system team for new credentials if required."
        ))
        steps.append(_step(
            f"Redeploy iFlow '{iflow_id}'",
            "After updating the credential, redeploy the iFlow to pick up the changes.",
        ))

    elif error_type == "CONNECTIVITY_ERROR":
        steps.append(_step(
            "Verify the receiver endpoint",
            "Confirm the receiver endpoint URL/host is reachable from SAP BTP.",
            sub=[
                "Check firewall rules and network ACLs",
                "Validate the SAP BTP destination configuration",
            ],
        ))
        steps.append(_step(
            f"Open iFlow '{iflow_id}' and inspect receiver adapter",
            "Verify the host, port, and path in the HTTP/SOAP receiver adapter are correct.",
        ))
        steps.append(_step(
            "Add or tune a retry policy",
            "In the receiver adapter, set retry count ≥ 3 with an interval of 10 seconds.",
        ))
        steps.append(_step(
            f"Redeploy iFlow '{iflow_id}'",
            "Save changes and redeploy the iFlow.",
        ))

    elif error_type == "DATA_VALIDATION":
        steps.append(_step(
            f"Open iFlow '{iflow_id}'",
            "Navigate to the iFlow in SAP Integration Suite Designer.",
        ))
        steps.append(_step(
            "Add an input-validation step",
            "Insert a Content Modifier or Groovy script before the mapping step "
            "to validate mandatory fields and reject invalid payloads early.",
        ))
        steps.append(_step(
            "Add dead-letter / exception handling",
            "Ensure invalid payloads are routed to an exception subprocess rather than failing silently.",
        ))
        steps.append(_step(
            f"Redeploy iFlow '{iflow_id}'",
            "Save and deploy the updated iFlow.",
        ))

    else:
        # Generic
        steps = [
            _step(
                f"Open iFlow '{iflow_id}' in Integration Suite",
                "Navigate to SAP Integration Suite → Design and open the iFlow.",
            ),
            _step(
                "Apply the proposed fix",
                proposed_fix or "Apply the fix described by the AI diagnosis.",
            ),
            _step(
                "Validate and redeploy",
                f"Validate the iFlow and deploy '{iflow_id}' to the runtime.",
                note="Monitor deployment status in the Operations section."
            ),
        ]

    return steps


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/messages")
async def list_messages(
    status: Optional[str] = None,
    time_range: Optional[str] = "Last 24 Hours",
    type: Optional[str] = None,
    id: Optional[str] = None,
    time: Optional[str] = None,
    artifacts: Optional[str] = None,
    limit: int = 50,
):
    """
    Fetch incidents from the local DB — no SAP CPI OData API calls.
    All incidents originate from the AEM queue via the autonomous pipeline.
    """
    incidents = get_all_incidents(limit=limit * 3)

    messages: List[Dict] = []
    for inc in incidents:
        if not _incident_matches_filter(inc, status, time_range, id, artifacts):
            continue

        inc_status = (inc.get("status") or "DETECTED").upper()
        iflow_name = inc.get("iflow_id") or ""
        log_end    = str(inc.get("log_end") or inc.get("last_seen") or inc.get("created_at") or "")
        log_start  = str(inc.get("log_start") or inc.get("created_at") or "")

        messages.append({
            "message_guid":    inc.get("message_guid") or inc.get("incident_id"),
            "iflow_name":      iflow_name,
            "iflow_display":   iflow_name.replace("-", " – ").replace("_", " "),
            "status":          inc_status,
            "status_label":    inc_status.replace("_", " ").title(),
            "log_start":       _format_ts(log_start),
            "log_end":         _format_ts(log_end),
            "relative_time":   _relative_time(log_end),
            "duration":        "",
            "sender":          inc.get("sender") or "",
            "receiver":        inc.get("receiver") or "",
            "error_type":      inc.get("error_type") or "",
            "has_rca":         bool(inc.get("root_cause")),
            "incident_id":     inc.get("incident_id"),
            "incident_status": inc_status,
        })

        if len(messages) >= limit:
            break

    return {
        "count":            len(messages),
        "messages":         messages,
        "filters_applied":  {
            "status":     status,
            "time_range": time_range,
            "type":       type,
            "id":         id,
            "artifacts":  artifacts,
        },
    }


@router.get("/messages/paginated")
async def list_messages_paginated(
    status: Optional[str] = None,
    type: Optional[str] = None,
    id: Optional[str] = None,
    artifacts: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
):
    """
    Fetch incidents from the local DB with pagination — no SAP CPI OData API calls.
    """
    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be >= 1")
    if page_size < 1 or page_size > 500:
        raise HTTPException(status_code=400, detail="Page size must be between 1 and 500")

    incidents = get_all_incidents(limit=0)

    filtered_messages: List[Dict] = []
    for inc in incidents:
        if not _incident_matches_filter(inc, status, None, id, artifacts):
            continue

        inc_status = (inc.get("status") or "DETECTED").upper()
        iflow_name = inc.get("iflow_id") or ""
        log_end    = str(inc.get("log_end") or inc.get("last_seen") or inc.get("created_at") or "")
        log_start  = str(inc.get("log_start") or inc.get("created_at") or "")

        filtered_messages.append({
            "message_guid":    inc.get("message_guid") or inc.get("incident_id"),
            "iflow_name":      iflow_name,
            "iflow_display":   iflow_name.replace("-", " – ").replace("_", " "),
            "status":          inc_status,
            "status_label":    inc_status.replace("_", " ").title(),
            "log_start":       _format_ts(log_start),
            "log_end":         _format_ts(log_end),
            "relative_time":   _relative_time(log_end),
            "duration":        "",
            "sender":          inc.get("sender") or "",
            "receiver":        inc.get("receiver") or "",
            "error_type":      inc.get("error_type") or "",
            "has_rca":         bool(inc.get("root_cause")),
            "incident_id":     inc.get("incident_id"),
            "incident_status": inc_status,
        })

    total_count = len(filtered_messages)
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

    if page > total_pages and total_count > 0:
        raise HTTPException(status_code=404, detail=f"Page {page} not found. Total pages: {total_pages}")

    start_idx = (page - 1) * page_size
    paginated_messages = filtered_messages[start_idx: start_idx + page_size]

    return {
        "count":            len(paginated_messages),
        "total_count":      total_count,
        "page":             page,
        "page_size":        page_size,
        "total_pages":      total_pages,
        "has_next":         page < total_pages,
        "has_previous":     page > 1,
        "messages":         paginated_messages,
        "filters_applied":  {
            "status":     status,
            "type":       type,
            "id":         id,
            "artifacts":  artifacts,
        },
    }


@router.get("/messages/{message_guid}")
async def get_message_detail(
    message_guid: str,
    run_rca: bool = True,
    mcp=Depends(_get_mcp),
):
    """
    Return the full detail view-model for the Smart Monitoring right panel.
    Covers all 6 tabs: Error Details, AI Recommendations, Properties, Artifact,
    Attachments, History.

    If no incident exists in the DB for this GUID it is created automatically.
    If RCA has not yet run (and run_rca=true) it is triggered synchronously so
    the AI diagnosis is immediately available for the UI.
    """
    # ── 1. DB-first: fetch incident by message_guid ───────────────────────
    incident = get_incident_by_message_guid(message_guid)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incident for GUID {message_guid!r} not found in DB")

    # ── 2. Optionally trigger RCA if not yet done ─────────────────────────
    if run_rca and incident.get("status") in {"DETECTED", "RCA_FAILED"} and not incident.get("root_cause"):
        try:
            await _ensure_incident_for_guid(message_guid, {}, {}, mcp, run_rca=True)
            incident = get_incident_by_message_guid(message_guid) or incident
        except Exception as exc:
            logger.warning(f"[SM] RCA trigger skipped for {message_guid}: {exc}")

    # ── 3. Build response from DB ─────────────────────────────────────────
    iflow_id = incident.get("iflow_id") or message_guid
    log_end  = str(incident.get("log_end") or incident.get("last_seen") or incident.get("created_at") or "")

    return {
        "message_guid":     message_guid,
        "iflow_id":         iflow_id,
        "iflow_display":    iflow_id.replace("-", " – ").replace("_", " "),
        "status":           "Failed",
        "status_label":     "Failed",
        "last_updated":     _format_ts(log_end),
        "relative_time":    _relative_time(log_end),
        "incident_id":      incident.get("incident_id"),
        "incident_status":  incident.get("status"),

        # ── Tabs ──────────────────────────────────────────────────────────
        "error_details":    _tab_error_details(incident),
        "ai_recommendation": _tab_ai_recommendation(incident),
        "properties":       _tab_properties(incident),
        "artifact":         _tab_artifact(incident),
        "attachments":      [],
        "history":          _tab_history(incident),
    }


@router.post("/messages/{message_guid}/analyze")
async def analyze_message(
    message_guid: str,
    req: AnalyzeRequest,
    mcp=Depends(_get_mcp),
):
    """
    Run AI root-cause analysis for a specific CPI message.
    Creates or refreshes the incident record.
    Returns the AI Recommendations tab data (diagnosis, fix, confidence, field changes).
    """
    # Use DB incident as primary source; fall back to CPI only if not in DB
    existing = get_incident_by_message_guid(message_guid)
    if existing:
        normalized = {
            "message_guid": message_guid,
            "iflow_id":     existing.get("iflow_id", ""),
            "error_message": existing.get("error_message", ""),
            "error_type":   existing.get("error_type", ""),
            "sender":       existing.get("sender", ""),
            "receiver":     existing.get("receiver", ""),
            "log_start":    existing.get("log_start", ""),
            "log_end":      existing.get("log_end", ""),
        }
        classification = mcp.classify_error(normalized.get("error_message", ""))
        normalized.update(classification)
    else:
        # Fallback: fetch from CPI if not in DB
        raw: Dict = {}
        try:
            raw_errors = await mcp.error_fetcher.fetch_failed_messages(limit=200)
            raw = next((r for r in raw_errors if r.get("MessageGuid") == message_guid), {})
        except Exception:
            pass
        error_detail: Dict = {}
        try:
            error_detail = await mcp.error_fetcher.fetch_error_details(message_guid) or {}
        except Exception:
            pass
        normalized     = mcp.error_fetcher.normalize(raw, error_detail)
        classification = mcp.classify_error(normalized.get("error_message", ""))
        normalized.update(classification)
    if not existing:
        incident_id = str(uuid.uuid4())
        incident = {
            **normalized,
            "incident_id":         incident_id,
            "status":              "DETECTED",
            "created_at":          get_hana_timestamp(),
            "incident_group_key":  mcp.incident_group_key(normalized),
            "occurrence_count":    1,
            "last_seen":           get_hana_timestamp(),
            "verification_status": "UNVERIFIED",
        }
        create_incident(incident)
    else:
        incident_id = existing["incident_id"]
        incident    = existing
        update_incident(incident_id, {
            "error_type":    normalized.get("error_type", incident.get("error_type")),
            "error_message": normalized.get("error_message", incident.get("error_message")),
            "last_seen":     get_hana_timestamp(),
        })
        incident = {**incident, **{
            "error_type":    normalized.get("error_type", incident.get("error_type")),
            "error_message": normalized.get("error_message", incident.get("error_message")),
        }}

    # Run RCA
    update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
    try:
        rca = await mcp.run_rca(incident)
    except RuntimeError as exc:
        # Agent not ready (MCP still connecting) → 503 so caller knows to retry
        logger.warning(f"[SM] RCA blocked — agent not ready for {message_guid}: {exc}")
        update_incident(incident_id, {"status": "RCA_FAILED"})
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error(f"[SM] RCA failed for {message_guid}: {exc}")
        update_incident(incident_id, {"status": "RCA_FAILED"})
        raise HTTPException(status_code=500, detail=f"RCA failed: {exc}")

    update_incident(incident_id, {
        "status":             "RCA_COMPLETE",
        "root_cause":         rca.get("root_cause", ""),
        "proposed_fix":       rca.get("proposed_fix", ""),
        "rca_confidence":     rca.get("confidence", 0.0),
        "affected_component": rca.get("affected_component", ""),
        "error_type":         rca.get("error_type") or classification["error_type"],
    })

    refreshed      = get_incident_by_message_guid(message_guid) or incident
    confidence     = float(rca.get("confidence", 0.0))
    conf_label     = "High" if confidence >= 0.90 else ("Medium" if confidence >= 0.70 else "Low")
    field_changes  = _extract_field_changes(
        refreshed.get("error_message", ""),
        rca.get("proposed_fix", ""),
    )

    return {
        "incident_id":         incident_id,
        "message_guid":        message_guid,
        "status":              "RCA_COMPLETE",
        "diagnosis":           rca.get("root_cause", ""),
        "proposed_fix":        rca.get("proposed_fix", ""),
        "field_changes":       field_changes,
        "confidence":          confidence,
        "confidence_display":  f"{confidence:.2f} ({conf_label})",
        "confidence_label":    conf_label,
        "error_type":          rca.get("error_type", ""),
        "affected_component":  rca.get("affected_component", ""),
        "can_generate_fix":    True,
    }


@router.post("/messages/{message_guid}/explain_error")
async def explain_error(
    message_guid: str,
    req: ExplainErrorRequest,
    mcp=Depends(_get_mcp),
):
    """
    Lightweight LLM call that explains the error in plain English.
    Returns a structured breakdown: category, summary, likely causes, recommended actions.
    Does NOT run full RCA — useful as a quick first-look before triggering /analyze.
    """
    incident = get_incident_by_message_guid(message_guid)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incident for GUID {message_guid!r} not found")

    error_message = (incident.get("error_message") or "").strip()
    error_type    = (incident.get("error_type")    or "UNKNOWN").strip()
    iflow_id      = (incident.get("iflow_id")      or "").strip()

    prompt = f"""You are an SAP CPI integration expert. Analyze the following error and explain it clearly.

iFlow: {iflow_id}
Error Type: {error_type}
Error Message: {error_message}

Return ONLY valid JSON — no markdown fences, no extra text:
{{
  "error_category": "<one of: HTTP_ERROR, MAPPING_ERROR, CONNECTIVITY_ERROR, AUTH_ERROR, DATA_ERROR, TIMEOUT_ERROR, CONFIG_ERROR, RUNTIME_ERROR, UNKNOWN>",
  "category_label": "<human-readable category, e.g. HTTP Backend Error>",
  "summary": "<1–2 sentence plain-English summary of what went wrong>",
  "what_happened": "<2–3 sentence technical explanation of the root failure, referencing the adapter/step if visible>",
  "likely_causes": ["<cause 1>", "<cause 2>", "<cause 3>"],
  "recommended_actions": ["<action 1>", "<action 2>", "<action 3>"]
}}"""

    try:
        from langchain_core.messages import HumanMessage  # noqa: PLC0415
        response = await mcp.llm.ainvoke([HumanMessage(content=prompt)])
        answer   = response.content if hasattr(response, "content") else str(response)
        clean    = re.sub(r"```(?:json)?|```", "", answer).strip()
        parsed   = json.loads(clean)
        return {
            "error_category":       parsed.get("error_category", error_type),
            "category_label":       parsed.get("category_label", error_type.replace("_", " ").title()),
            "summary":              parsed.get("summary", ""),
            "what_happened":        parsed.get("what_happened", ""),
            "likely_causes":        parsed.get("likely_causes", []),
            "recommended_actions":  parsed.get("recommended_actions", []),
        }
    except Exception as exc:
        logger.warning("[SM] explain_error failed for %s: %s", message_guid, exc)
        raise HTTPException(status_code=500, detail=f"Error explanation failed: {exc}")


@router.post("/messages/{message_guid}/generate_fix_patch")
async def generate_fix_patch(
    message_guid: str,
    req: GenerateFixPatchRequest,
    mcp=Depends(_get_mcp),
):
    """
    Generate a detailed, human-readable fix patch plan.
    Does NOT apply the fix — returns a structured plan for user review.
    Maps to the 'Generate Fix Patch' button in the AI Recommendations tab.
    """
    incident = get_incident_by_message_guid(message_guid)
    if not incident:
        raise HTTPException(
            status_code=404,
            detail="No incident found for this message. Call /analyze first."
        )

    # Run RCA if not yet actionable
    if not _has_actionable_fix(incident):
        update_incident(incident["incident_id"], {"status": "RCA_IN_PROGRESS"})
        rca = await mcp.run_rca(incident)
        update_incident(incident["incident_id"], {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
            "error_type":         rca.get("error_type", incident.get("error_type", "")),
        })
        incident = get_incident_by_message_guid(message_guid) or incident

    iflow_id           = incident.get("iflow_id", "")
    error_type         = incident.get("error_type", "UNKNOWN_ERROR")
    root_cause         = incident.get("root_cause", "") or ""
    proposed_fix       = incident.get("proposed_fix", "") or ""
    affected_component = incident.get("affected_component", "") or ""
    confidence         = float(incident.get("rca_confidence") or 0.0)
    error_message      = incident.get("error_message", "") or ""

    field_changes = _extract_field_changes(error_message, proposed_fix)

    # Generate step-by-step plan via LLM
    fix_steps = await _generate_fix_plan_steps(
        mcp=mcp,
        iflow_id=iflow_id,
        error_type=error_type,
        root_cause=root_cause,
        proposed_fix=proposed_fix,
        affected_component=affected_component,
        error_message=error_message,
        field_changes=field_changes,
    )

    # Persist fix plan to DB so the table always has the latest generated result
    from utils.utils import get_hana_timestamp
    update_incident(incident["incident_id"], {
        "fix_steps":             json.dumps(fix_steps),
        "field_changes":         json.dumps(field_changes),
        "fix_plan_generated_at": get_hana_timestamp(),
        "status":                "AWAITING_APPROVAL",
    })

    # Build summary section (matches the screenshot layout)
    summary_parts: List[str] = []
    if root_cause:
        summary_parts.append(root_cause)
    if field_changes:
        for fc in field_changes:
            summary_parts.append(
                f"**{fc['old_field']}** was renamed/removed and replaced by **{fc['new_field']}**. "
                f"The mapping still references {fc['old_field']}, causing runtime failure."
            )

    conf_label = "High" if confidence >= 0.90 else ("Medium" if confidence >= 0.70 else "Low")

    can_apply = incident.get("status") in {
        "RCA_COMPLETE", "AWAITING_APPROVAL", "DETECTED", "RCA_FAILED",
        "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
    }

    return {
        "incident_id":   incident["incident_id"],
        "message_guid":  message_guid,
        "iflow_id":      iflow_id,
        "error_type":    error_type,

        # ── Summary section ───────────────────────────────────────────────
        "summary":             " ".join(summary_parts) if summary_parts else (root_cause or proposed_fix),
        "summary_structured": {
            "diagnosis":     root_cause,
            "field_changes": field_changes,
            "proposed_fix":  proposed_fix,
        },

        # ── Steps section (Fix Plan) ──────────────────────────────────────
        "steps": fix_steps,

        # ── Metadata ─────────────────────────────────────────────────────
        "confidence":          confidence,
        "confidence_label":    conf_label,
        "confidence_display":  f"{confidence:.2f} ({conf_label})",
        "affected_component":  affected_component,

        # ── Readiness flags ───────────────────────────────────────────────
        "ready_to_apply": bool(proposed_fix.strip() and iflow_id.strip()),
        "can_apply":      can_apply,
    }


@router.post("/messages/{message_guid}/apply_fix")
async def apply_fix(
    message_guid: str,
    req: ApplyFixRequest,
    background_tasks: BackgroundTasks,
    sync: bool = False,
    force: bool = False,
    mcp=Depends(_get_mcp),
):
    """
    Apply the AI-generated fix to the iFlow via MCP tools (get-iflow → update-iflow → deploy-iflow).

    By default runs in the background (sync=false).
    Pass ?sync=true to block until the fix completes (useful for testing).
    Pass ?force=true to skip the pre-flight "iFlow already started" guard and apply the fix anyway.

    Maps to the 'Fix Patch' / 'Apply Fix' button in the fix patch view.
    """
    incident = get_incident_by_message_guid(message_guid)
    if not incident:
        raise HTTPException(            status_code=404,
            detail="No incident found for this message. Run /analyze first."
        )

    incident_id = incident["incident_id"]

    # Build RCA context from incident (allow proposed_fix override via request)
    rca = {
        "root_cause":         incident.get("root_cause", "") or "",
        "proposed_fix":       req.proposed_fix or incident.get("proposed_fix", "") or "",
        "confidence":         float(incident.get("rca_confidence") or 0.0),
        "auto_apply":         True,
        "error_type":         incident.get("error_type", ""),
        "affected_component": incident.get("affected_component", "") or "",
    }

    if not _has_actionable_fix(rca):
        raise HTTPException(
            status_code=400,
            detail="No actionable fix available. Call /analyze first."
        )

    # Guard: reject concurrent fix attempts on the same incident
    _IN_PROGRESS_STATUSES = {"FIX_IN_PROGRESS", "RCA_IN_PROGRESS", "FIX_APPLIED_PENDING_VERIFICATION"}
    if incident.get("status") in _IN_PROGRESS_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A fix is already in progress for incident {incident_id} "
                f"(status: {incident.get('status')}). "
                f"Poll GET /smart-monitoring/incidents/{incident_id}/fix_status for live progress."
            )
        )

    if sync:
        update_incident(incident_id, {"status": "FIX_IN_PROGRESS"})
        try:
            fix_result = await mcp.execute_incident_fix(dict(incident), human_approved=True, force=force)
            return {
                "incident_id":      incident_id,
                "message_guid":     message_guid,
                "iflow_id":         incident.get("iflow_id"),
                "success":          fix_result.get("success", False),
                "fix_applied":      fix_result.get("fix_applied", False),
                "deploy_success":   fix_result.get("deploy_success", False),
                "failed_stage":     fix_result.get("failed_stage"),
                "summary":          fix_result.get("summary", ""),
                "technical_details": fix_result.get("technical_details", ""),
                "status":           fix_result.get("status"),
            }
        except Exception as exc:
            logger.error(f"[SM] apply_fix sync error for {message_guid}: {exc}")
            update_incident(incident_id, {
                "status":            "FIX_FAILED",
                "fix_summary":       str(exc),
                "last_failed_stage": "agent",
            })
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Background execution (default) ───────────────────────────────────
    update_incident(incident_id, {"status": "FIX_IN_PROGRESS"})

    async def _run_fix_background() -> None:
        try:
            result = await mcp.execute_incident_fix(dict(incident), human_approved=True, force=force)
            if not result.get("success"):
                update_incident(incident_id, {
                    "last_failed_stage": result.get("failed_stage", "unknown"),
                })
        except Exception as exc:
            logger.error(f"[SM] background apply_fix error for {message_guid}: {exc}")
            update_incident(incident_id, {
                "status":            "FIX_FAILED",
                "fix_summary":       str(exc),
                "last_failed_stage": "agent",
            })

    background_tasks.add_task(_run_fix_background)

    return {
        "incident_id":  incident_id,
        "message_guid": message_guid,
        "iflow_id":     incident.get("iflow_id"),
        "status":       "FIX_IN_PROGRESS",
        "message":      (
            "Fix is being applied in the background. "
            f"Poll GET /smart-monitoring/incidents/{incident_id}/fix_status for progress."
        ),
    }


_MAX_RETRY_ATTEMPTS = 3


@router.post("/incidents/{incident_id}/retry_fix")
async def retry_fix(
    incident_id: str,
    req: RetryFixRequest,
    background_tasks: BackgroundTasks,
    mcp=Depends(_get_mcp),
):
    """
    Retry a previously failed fix.

    Guards:
    - Incident must exist and have status FIX_FAILED.
    - Blocked if locked stage is detected (user must clear the lock first).
    - Capped at _MAX_RETRY_ATTEMPTS (default 3) to prevent infinite loops.

    Re-runs execute_incident_fix in the background with the same RCA context.
    Poll GET /smart-monitoring/incidents/{incident_id}/fix_status for progress.
    """
    incident = get_incident_by_id(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    status = incident.get("status", "")
    _retryable = {"FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME"}
    if status not in _retryable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot retry — current status is '{status}'. "
                f"Only failed incidents ({', '.join(sorted(_retryable))}) can be retried."
            ),
        )

    retry_count = int(incident.get("retry_count") or 0)
    if retry_count >= _MAX_RETRY_ATTEMPTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Maximum retry attempts ({_MAX_RETRY_ATTEMPTS}) reached. "
                "Please review the iFlow manually or raise a support ticket."
            ),
        )

    last_failed_stage = incident.get("last_failed_stage") or "unknown"
    if last_failed_stage == "locked":
        raise HTTPException(
            status_code=409,
            detail=(
                "The iFlow is still locked by an active edit session in SAP Integration Suite. "
                "Please close the edit session (Cancel Checkout) and then retry."
            ),
        )

    # ── Decide retry strategy based on what failed ──────────────────────────
    # deploy stage (timeout/infra): update already applied — skip get+update, just redeploy
    # deploy_validation: iFlow content itself was rejected by SAP CPI build — must re-fix from scratch
    deploy_only = last_failed_stage == "deploy"

    # For update/agent failures with low confidence: re-run RCA first to get
    # a fresh fix angle before retrying the full pipeline
    rca_confidence = float(incident.get("rca_confidence") or 0.0)
    needs_fresh_rca = (
        not deploy_only
        and rca_confidence < 0.70
        and last_failed_stage in ("update", "agent", "unknown")
    )

    new_retry_count = retry_count + 1
    update_incident(incident_id, {
        "status":      "FIX_IN_PROGRESS",
        "retry_count": new_retry_count,
    })

    async def _run_retry_background() -> None:
        working_incident = dict(incident)
        try:
            if needs_fresh_rca:
                logger.info(
                    f"[SM] retry_fix: low confidence ({rca_confidence:.2f}) on attempt "
                    f"{new_retry_count} — re-running RCA for {incident_id}"
                )
                update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
                fresh_rca = await mcp.run_rca(working_incident)
                update_incident(incident_id, {
                    "status":            "RCA_COMPLETE",
                    "root_cause":        fresh_rca.get("root_cause", ""),
                    "proposed_fix":      fresh_rca.get("proposed_fix", ""),
                    "rca_confidence":    fresh_rca.get("confidence", 0.0),
                    "affected_component": fresh_rca.get("affected_component", ""),
                })
                working_incident.update({
                    "root_cause":        fresh_rca.get("root_cause", ""),
                    "proposed_fix":      fresh_rca.get("proposed_fix", ""),
                    "rca_confidence":    fresh_rca.get("confidence", 0.0),
                    "affected_component": fresh_rca.get("affected_component", ""),
                })
                update_incident(incident_id, {"status": "FIX_IN_PROGRESS"})

            result = await mcp.execute_incident_fix(
                working_incident,
                human_approved=True,
                deploy_only=deploy_only,
            )
            if not result.get("success"):
                update_incident(incident_id, {
                    "last_failed_stage": result.get("failed_stage", "unknown"),
                })
        except Exception as exc:
            logger.error(f"[SM] retry_fix error for incident {incident_id} (attempt {new_retry_count}): {exc}")
            update_incident(incident_id, {
                "status":            "FIX_FAILED",
                "fix_summary":       str(exc),
                "last_failed_stage": "agent",
            })

    background_tasks.add_task(_run_retry_background)

    retry_strategy = (
        "deploy-only (update already applied)"
        if deploy_only
        else ("re-run RCA then full fix (low confidence)" if needs_fresh_rca else "full fix pipeline")
    )

    return {
        "incident_id":       incident_id,
        "iflow_id":          incident.get("iflow_id"),
        "status":            "FIX_IN_PROGRESS",
        "retry_attempt":     new_retry_count,
        "max_retries":       _MAX_RETRY_ATTEMPTS,
        "last_failed_stage": last_failed_stage,
        "retry_strategy":    retry_strategy,
        "message": (
            f"Retry attempt {new_retry_count}/{_MAX_RETRY_ATTEMPTS} started in the background. "
            f"Poll GET /smart-monitoring/incidents/{incident_id}/fix_status for progress."
        ),
    }


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str):
    """
    Manually mark an incident as FIX_VERIFIED.

    Use when the fix is confirmed working outside the agent
    (e.g. deploy timed out but iFlow is actually running fine).
    Sets status → FIX_VERIFIED, verification_status → MANUALLY_VERIFIED, resolved_at → now.
    """
    incident = get_incident_by_id(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    resolvable = {
        "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME", "FIX_FAILED",
        "FIX_FAILED_UPDATE", "VERIFICATION_UNAVAILABLE",
    }
    current = incident.get("status", "")
    if current not in resolvable:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resolve — status is '{current}'. "
                   f"Only failed/unverified incidents can be manually resolved.",
        )

    now = get_hana_timestamp()
    update_incident(incident_id, {
        "status":               "FIX_VERIFIED",
        "verification_status":  "MANUALLY_VERIFIED",
        "resolved_at":          now,
        "fix_summary":          (incident.get("fix_summary") or "") + " [Manually marked as resolved]",
    })
    return {
        "incident_id":  incident_id,
        "iflow_id":     incident.get("iflow_id"),
        "status":       "FIX_VERIFIED",
        "resolved_at":  now,
        "message":      "Incident marked as resolved. iFlow is considered working.",
    }


@router.post("/incidents/{incident_id}/rollback")
async def rollback_fix(
    incident_id: str,
    background_tasks: BackgroundTasks,
    mcp=Depends(_get_mcp),
):
    """
    Roll back the iFlow to its state before the fix was applied.
    Requires that a snapshot was captured (iflow_snapshot_before is set).
    Re-uploads the original content via update-iflow then deploys.
    """
    incident = get_incident_by_id(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    snapshot = incident.get("iflow_snapshot_before")
    if not snapshot:
        raise HTTPException(
            status_code=400,
            detail="No pre-fix snapshot available for this incident. Rollback is not possible.",
        )

    iflow_id = incident.get("iflow_id", "")
    if not iflow_id:
        raise HTTPException(status_code=400, detail="Incident has no iflow_id.")

    update_incident(incident_id, {"status": "FIX_IN_PROGRESS"})

    async def _run_rollback() -> None:
        try:
            prompt = (
                f"ROLLBACK — restore iFlow '{iflow_id}' to its pre-fix state.\n"
                f"Call update-iflow with iFlow ID '{iflow_id}' and the following original content:\n"
                f"{snapshot}\n"
                f"Then call deploy-iflow with iFlow ID '{iflow_id}'.\n"
                f"Return ONLY valid JSON:\n"
                f'{{"fix_applied": true, "deploy_success": true/false, "summary": "<one sentence>"}}'
            )
            result = await mcp.agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": 8},
            )
            final_msg = result["messages"][-1]
            answer = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            success = "deploy_success\": true" in answer or "deployed successfully" in answer.lower()
            update_incident(incident_id, {
                "status":      "ROLLED_BACK" if success else "FIX_FAILED",
                "fix_summary": f"Rollback {'succeeded' if success else 'failed'}: {answer[:300]}",
            })
        except Exception as exc:
            logger.error(f"[SM] rollback error for {incident_id}: {exc}")
            update_incident(incident_id, {
                "status":      "FIX_FAILED",
                "fix_summary": f"Rollback failed: {exc}",
            })

    background_tasks.add_task(_run_rollback)
    return {
        "incident_id": incident_id,
        "iflow_id":    iflow_id,
        "status":      "FIX_IN_PROGRESS",
        "message":     (
            f"Rollback started in the background. "
            f"Poll GET /smart-monitoring/incidents/{incident_id}/fix_status for progress."
        ),
    }


@router.post("/chat")
async def smart_monitoring_chat(
    req: ChatRequest,
    mcp=Depends(_get_mcp),
):
    """
    AI chat endpoint scoped to a specific CPI error.
    Maps to the 'Ask your queries here' input at the bottom of the fix patch view.
    Injects incident context (iFlow ID, error message, root cause, proposed fix)
    into every conversation so the AI can give precise, contextual answers.
    """
    timestamp  = get_hana_timestamp()
    session_id = req.session_id or f"sm_chat_{uuid.uuid4()}"

    # Build context prefix from the linked incident
    context_prefix = ""
    if req.message_guid:
        incident = get_incident_by_message_guid(req.message_guid)
        if incident:
            context_prefix = (
                f"[CONTEXT]\n"
                f"iFlow: {incident.get('iflow_id', 'N/A')}\n"
                f"Error Type: {incident.get('error_type', 'N/A')}\n"
                f"Error: {incident.get('error_message', '') or ''}\n"
                f"Root Cause: {incident.get('root_cause', '') or ''}\n"
                f"Proposed Fix: {incident.get('proposed_fix', '') or ''}\n\n"
                f"[USER QUESTION]\n"
            )

    enhanced_query = context_prefix + req.query

    try:
        result = await mcp.ask(enhanced_query, req.user_id, session_id, timestamp)
        return {
            "answer":       result.get("answer", ""),
            "session_id":   session_id,
            "message_guid": req.message_guid,
        }
    except Exception as exc:
        logger.error(f"[SM] chat error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/stats")
async def get_stats(mcp=Depends(_get_mcp)):
    """
    Aggregate statistics for the Smart Monitoring dashboard header.
    """
    try:
        all_incidents = get_all_incidents(limit=500)
        total    = len(all_incidents)
        resolved = sum(1 for i in all_incidents if i.get("status") in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED"})
        pending  = sum(1 for i in all_incidents if i.get("status") in {"AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW"})
        failed   = sum(
            1 for i in all_incidents
            if i.get("status") in {
                "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME", "TICKET_CREATED",
            }
        )
        active   = sum(
            1 for i in all_incidents
            if i.get("status") in {"DETECTED", "RCA_IN_PROGRESS", "RCA_COMPLETE", "FIX_IN_PROGRESS", "FIX_DEPLOYED"}
        )

        error_types: Dict[str, int] = {}
        for inc in all_incidents:
            et = inc.get("error_type") or "UNKNOWN"
            error_types[et] = error_types.get(et, 0) + 1

        return {
            "total_incidents":        total,
            "auto_fixed":             resolved,
            "pending_approval":       pending,
            "fix_failed":             failed,
            "in_progress":            active,
            "auto_fix_rate":          round(resolved / total * 100, 1) if total > 0 else 0.0,
            "error_type_distribution": error_types,
            "autonomous_running":     mcp._autonomous_running,
        }
    except Exception as exc:
        logger.error(f"[SM] stats error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/incidents")
async def list_sm_incidents(
    status: Optional[str] = None,
    limit: int = 50,
):
    """
    List all autonomous incidents.
    Convenience alias scoped to the Smart Monitoring UI
    (same data as /autonomous/incidents).
    """
    try:
        incidents = get_all_incidents(status=status, limit=limit)
        return {"incidents": incidents, "total": len(incidents)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


_TERMINAL_STATUSES = frozenset({
    "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED",
    "FIX_DEPLOYED",  # deployed but not yet verified — still a terminal pipeline state
    "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
    "REJECTED", "TICKET_CREATED", "ARTIFACT_MISSING", "ARTIFACT_NOT_FOUND", "VERIFICATION_UNAVAILABLE",
})


def _get_fix_progress(incident_id: str) -> dict:
    """Read live pipeline progress from the in-memory store in main.py (no DB hit)."""
    try:
        import main as _main  # noqa: PLC0415
        return getattr(_main, "FIX_PROGRESS", {}).get(incident_id, {})
    except Exception:
        return {}


@router.get("/incidents/{incident_id}/fix_status")
async def get_fix_status(incident_id: str):
    """
    Lightweight polling endpoint: returns current fix status for an incident.

    Fast path: reads from an in-memory progress dict — no HANA round-trip while
    the fix pipeline is running.  Only hits the DB once the status is terminal
    (AUTO_FIXED / HUMAN_FIXED / FIX_FAILED / …) so the final persisted data is
    returned when the fix is done.

    Response fields:
      status         — current incident status
      current_step   — human-readable pipeline step currently executing
      step_index     — e.g. 2
      total_steps    — e.g. 4
      steps_done     — list of completed step labels
      fix_summary    — populated once fix is complete
      resolved_at    — populated once fix is complete
    """
    progress = _get_fix_progress(incident_id)
    in_progress_status = progress.get("status", "")

    # ── Terminal: hit DB once to return the fully-persisted final result ──────
    if not progress or in_progress_status in _TERMINAL_STATUSES:
        incident = get_incident_by_id(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        final_progress = progress or {}
        response = {
            "incident_id":         incident_id,
            "status":              incident.get("status"),
            "current_step":        final_progress.get("current_step", "Complete"),
            "step_index":          final_progress.get("step_index", final_progress.get("total_steps", 1)),
            "total_steps":         final_progress.get("total_steps", 1),
            "steps_done":          final_progress.get("steps_done", []),
            "fix_summary":         incident.get("fix_summary"),
            "last_failed_stage":   incident.get("last_failed_stage"),
            "technical_details":   final_progress.get("technical_details"),
            "resolved_at":         incident.get("resolved_at"),
            "verification_status": incident.get("verification_status"),
            "root_cause":          incident.get("root_cause"),
            "proposed_fix":        incident.get("proposed_fix"),
            "rca_confidence":      incident.get("rca_confidence"),
            "iflow_id":            incident.get("iflow_id"),
            "recommended_action":  _recommended_action(
                incident.get("status", ""), incident.get("error_type", "")
            ),
        }
        
        # Log the complete fix status response for terminal states
        logger.info(
            "[FIX_STATUS_RESPONSE] incident=%s status=%s iflow=%s | "
            "root_cause=%s | proposed_fix=%s | fix_summary=%s",
            incident_id,
            response.get("status"),
            response.get("iflow_id"),
            response.get("root_cause"),
            response.get("proposed_fix"),
            response.get("fix_summary"),
        )
        
        return response

    # ── In-progress: return from memory — no HANA connection needed ──────────
    return {
        "incident_id":         incident_id,
        "status":              in_progress_status,
        "current_step":        progress.get("current_step", "Processing…"),
        "step_index":          progress.get("step_index", 1),
        "total_steps":         progress.get("total_steps", 4),
        "steps_done":          progress.get("steps_done", []),
        "fix_summary":         None,
        "last_failed_stage":   progress.get("failed_stage"),
        "technical_details":   progress.get("technical_details"),
        "resolved_at":         None,
        "verification_status": None,
        # Context fields populated as soon as the pipeline starts
        "iflow_id":            progress.get("iflow_id"),
        "error_type":          progress.get("error_type"),
        "root_cause":          progress.get("root_cause"),
        "proposed_fix":        progress.get("proposed_fix"),
        "rca_confidence":      progress.get("rca_confidence"),
        "recommended_action":  _recommended_action(
            in_progress_status, progress.get("error_type", "")
        ),
    }


@router.get("/total-errors")
async def get_total_errors():
    """
    Get the total count of incidents from the local DB — no SAP CPI API call.
    """
    count = count_all_incidents()
    return {"total_failed_messages": count, "status": "success"}


# ─────────────────────────────────────────────────────────────────────────────
# ESCALATION TICKETS
# ─────────────────────────────────────────────────────────────────────────────

class EscalationTicketUpdate(BaseModel):
    status: Optional[str] = None          # OPEN | IN_PROGRESS | RESOLVED | CLOSED
    assigned_to: Optional[str] = None
    resolution_notes: Optional[str] = None


@router.get("/escalations")
async def list_escalation_tickets(
    status: Optional[str] = None,
    incident_id: Optional[str] = None,
    limit: int = 50,
):
    """List internal escalation tickets, optionally filtered by status or incident."""
    tickets = get_escalation_tickets(status=status, incident_id=incident_id, limit=limit)
    return {"tickets": tickets, "count": len(tickets)}


@router.get("/escalations/{ticket_id}")
async def get_escalation_ticket(ticket_id: str):
    """Get a single escalation ticket by ID."""
    ticket = get_escalation_ticket_by_id(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Escalation ticket not found")
    return ticket


@router.patch("/escalations/{ticket_id}")
async def patch_escalation_ticket(ticket_id: str, body: EscalationTicketUpdate):
    """Update status, assignee, or resolution notes on an escalation ticket."""
    ticket = get_escalation_ticket_by_id(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Escalation ticket not found")

    updates: Dict[str, Any] = {}
    if body.status is not None:
        allowed = {"OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"}
        if body.status not in allowed:
            raise HTTPException(status_code=400, detail=f"status must be one of {allowed}")
        updates["status"] = body.status
        if body.status in ("RESOLVED", "CLOSED"):
            updates["resolved_at"] = datetime.utcnow().isoformat()
    if body.assigned_to is not None:
        updates["assigned_to"] = body.assigned_to
    if body.resolution_notes is not None:
        updates["resolution_notes"] = body.resolution_notes

    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    update_escalation_ticket(ticket_id, updates)
    return {"ticket_id": ticket_id, "updated": list(updates.keys())}


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/admin/clear-all-data")
async def admin_clear_all_data():
    """
    DELETE all rows from EM_AUTONOMOUS_INCIDENTS, EM_FIX_PATTERNS,
    and EM_ESCALATION_TICKETS.  Returns row counts per table.
    """
    try:
        deleted = clear_all_em_tables()
        logger.warning(
            "[Admin] All EM tables cleared — incidents=%d fix_patterns=%d tickets=%d",
            deleted.get("incidents", 0),
            deleted.get("fix_patterns", 0),
            deleted.get("tickets", 0),
        )
        return {
            "status": "cleared",
            "deleted": deleted,
            "total": sum(deleted.values()),
        }
    except Exception as exc:
        logger.error("[Admin] clear_all_em_tables failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))