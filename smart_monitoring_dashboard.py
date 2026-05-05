"""
Smart Monitoring Dashboard Router — Sierra Digital

New dashboard-focused endpoints for Grafana-style monitoring UI.
These routes complement smart_monitoring.py with aggregated, time-series,
and drill-down data optimized for dashboard visualization.

New Endpoints:
  GET  /dashboard/kpi-cards                           — Top-level KPI metrics
  GET  /dashboard/error-distribution                  — Error type pie/donut chart data
  GET  /dashboard/status-distribution                 — Incident status breakdown
  GET  /dashboard/status-breakdown                    — Detailed status counts for all statuses
  GET  /dashboard/failures-over-time                  — Time-series failure data
  GET  /dashboard/top-failing-iflows                  — Bar chart: top noisy integrations
  GET  /dashboard/sender-receiver-stats               — Sender/receiver failure counts
  GET  /dashboard/active-incidents-table              — Real-time active incidents
  GET  /dashboard/recent-failures-table               — Recent failed messages feed
  GET  /dashboard/fix-progress-tracker                — Operational fix progress widget
  GET  /dashboard/leaderboard/noisy-integrations      — Most failing iFlows
  GET  /dashboard/leaderboard/recurring-incidents     — Most recurring incidents
  GET  /dashboard/leaderboard/longest-open            — Longest unresolved incidents
  GET  /dashboard/drill-down/message/{guid}           — Detailed message drill-down
  GET  /dashboard/drill-down/incident/{id}            — Detailed incident drill-down
  GET  /dashboard/drill-down/iflow/{name}             — iFlow-specific analytics
  GET  /dashboard/health-metrics                      — System health indicators
  GET  /dashboard/sla-metrics                         — SLA compliance metrics
  GET  /dashboard/rca-coverage                        — AI RCA coverage statistics
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from collections import defaultdict
import logging
import asyncio
import os

from db.database import (
    get_all_incidents,
    get_incident_by_id,
    get_incident_by_message_guid,
    count_all_incidents,
)
from utils.utils import get_hana_timestamp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# ─────────────────────────────────────────────────────────────────────────────
# ASYNC WRAPPERS FOR BLOCKING DATABASE CALLS
# ─────────────────────────────────────────────────────────────────────────────

async def _aget_all_incidents(limit: int = 50, status: Optional[str] = None):
    """Async wrapper for get_all_incidents to avoid blocking the event loop."""
    return await asyncio.to_thread(get_all_incidents, status, limit)

async def _acount_all_incidents(status: Optional[str] = None):
    """Async wrapper for count_all_incidents to avoid blocking the event loop."""
    return await asyncio.to_thread(count_all_incidents, status)

async def _aget_incident_by_id(incident_id: str):
    """Async wrapper for get_incident_by_id."""
    return await asyncio.to_thread(get_incident_by_id, incident_id)


# ─────────────────────────────────────────────────────────────────────────────
# DEPENDENCY — lazy import MCP manager (MUST be before routes that use it)
# ─────────────────────────────────────────────────────────────────────────────

class _MCPCompat:
    """Shim: maps legacy mcp_manager interface to new MultiMCP + ObserverAgent architecture."""
    def __init__(self, mcp, observer, orchestrator) -> None:
        self._mcp          = mcp
        self._observer     = observer
        self._orchestrator = orchestrator

    @property
    def error_fetcher(self):
        return self._observer.error_fetcher

    @property
    def _autonomous_running(self) -> bool:
        return self._orchestrator._autonomous_running if self._orchestrator else False


def _get_mcp():
    """Lazy-import the global mcp from main.py at request time."""
    import main as _main  # noqa: PLC0415
    _mcp  = getattr(_main, "mcp_manager", None) or getattr(_main, "mcp", None)
    _obs  = getattr(_main, "observer", None)
    _orch = getattr(_main, "orchestrator", None)
    if _mcp is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    if not hasattr(_mcp, "error_fetcher"):
        return _MCPCompat(_mcp, _obs, _orch)
    return _mcp


# ─────────────────────────────────────────────────────────────────────────────
# ALL-IN-ONE DASHBOARD ENDPOINT
# One HANA connection, one query, all widget data returned together.
# The frontend uses this instead of firing 7 separate requests.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/test")
async def get_test():
    """Simple test endpoint."""
    return {"status": "ok"}


@router.get("/all")
async def get_dashboard_all(mcp=Depends(_get_mcp)):
    """
    Returns all dashboard widget data in a single response.
    Replaces 7 individual endpoints with one HANA roundtrip.
    """
    try:
        all_incidents = await _aget_all_incidents(limit=2000)
        total_incidents = len(all_incidents)

        in_progress = sum(
            1 for i in all_incidents
            if i.get("status") in {"FIX_IN_PROGRESS", "RCA_IN_PROGRESS", "FIX_DEPLOYED"}
        )
        fix_failed = sum(
            1 for i in all_incidents
            if i.get("status") in {
                "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY",
                "FIX_FAILED_RUNTIME", "TICKET_CREATED",
            }
        )
        auto_fixed = sum(
            1 for i in all_incidents
            if i.get("status") in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED"}
        )
        pending_approval = sum(
            1 for i in all_incidents
            if i.get("status") in {"AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW"}
        )

        resolution_times = []
        for inc in all_incidents:
            if inc.get("resolved_at") and inc.get("created_at"):
                created = _parse_sap_timestamp(str(inc.get("created_at")))
                resolved = _parse_sap_timestamp(str(inc.get("resolved_at")))
                if created and resolved:
                    resolution_times.append((resolved - created).total_seconds() / 60)

        rca_complete = sum(
            1 for i in all_incidents
            if i.get("root_cause") and i.get("proposed_fix")
        )

        auto_fix_rate = round(auto_fixed / total_incidents * 100, 1) if total_incidents > 0 else 0.0
        avg_resolution_time = round(sum(resolution_times) / len(resolution_times), 1) if resolution_times else 0.0
        rca_coverage = round(rca_complete / total_incidents * 100, 1) if total_incidents > 0 else 0.0

        status_counts: Dict[str, int] = defaultdict(int)
        error_counts: Dict[str, int] = defaultdict(int)
        iflow_counts: Dict[str, int] = defaultdict(int)
        timeline_buckets: Dict[str, int] = defaultdict(int)
        cutoff = datetime.utcnow() - timedelta(hours=24)

        for inc in all_incidents:
            status = str(inc.get("status") or "UNKNOWN")
            error_type = str(inc.get("error_type") or "UNKNOWN")
            iflow_name = str(inc.get("iflow_id") or inc.get("iflow_name") or "Unknown")

            status_counts[status] += 1
            error_counts[error_type] += 1
            iflow_counts[iflow_name] += 1

            ts = _parse_sap_timestamp(
                str(inc.get("last_seen") or inc.get("log_end") or inc.get("created_at") or "")
            )
            if ts and ts >= cutoff:
                timeline_buckets[_time_bucket(ts, "hour")] += 1

        total_failed_messages = total_incidents
        try:
            total_failed_messages = int(await mcp.error_fetcher.fetch_failed_messages_count())
        except Exception as exc:
            logger.warning(f"[Dashboard] Failed to fetch live failed message count: {exc}")

        aem_enabled = os.getenv("AEM_ENABLED", "false").lower() == "true"
        aem_payload: Dict[str, Any]
        if aem_enabled:
            aem_payload = {
                "queues": {},
                "stage_counts": {},
                "semp_error": None,
            }
        else:
            aem_payload = {"warning": "AEM is disabled in backend configuration."}

        return {
            "kpi": {
                "total_failed_messages": total_failed_messages,
                "total_incidents": total_incidents,
                "in_progress": in_progress,
                "fix_failed": fix_failed,
                "auto_fixed": auto_fixed,
                "pending_approval": pending_approval,
                "auto_fix_rate": auto_fix_rate,
                "avg_resolution_time_minutes": avg_resolution_time,
                "rca_coverage_percent": rca_coverage,
            },
            "status_breakdown": [
                {"status": status, "count": count}
                for status, count in sorted(status_counts.items(), key=lambda x: x[1], reverse=True)
            ],
            "error_distribution": [
                {"error_type": error_type, "count": count}
                for error_type, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True)
            ],
            "top_iflows": [
                {"iflow_name": iflow_name, "failure_count": count}
                for iflow_name, count in sorted(iflow_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            ],
            "timeline": [
                {"time": bucket, "count": count}
                for bucket, count in sorted(timeline_buckets.items())
            ],
            "aem": aem_payload,
            "total_messages_count": total_failed_messages,
            "total_incidents_count": total_incidents,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] /all aggregation error: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load dashboard data")


# ─────────────────────────────────────────────────────────────────────────────
# TIME PARSING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_sap_timestamp(ts: str) -> Optional[datetime]:
    """Parse SAP /Date(ms)/ or ISO timestamp → datetime (UTC, naive)."""
    if not ts:
        return None
    try:
        import re
        if "/Date(" in ts:
            ms = int(re.search(r"\d+", ts).group())
            return datetime.utcfromtimestamp(ms / 1000)
        clean = ts.replace("Z", "").split("+")[0].split(".")[0]
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def _time_bucket(dt: datetime, interval: str = "hour") -> str:
    """Bucket a datetime into time intervals for time-series charts."""
    if interval == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    elif interval == "day":
        return dt.strftime("%Y-%m-%d")
    elif interval == "week":
        # ISO week format
        return f"{dt.year}-W{dt.isocalendar()[1]:02d}"
    elif interval == "month":
        return dt.strftime("%Y-%m")
    return dt.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# KPI CARDS ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/kpi-cards")
async def get_kpi_cards(mcp=Depends(_get_mcp)):
    """
    Top-level KPI metrics for dashboard cards.
    
    Returns:
    - total_failed_messages: Live count from SAP CPI
    - total_incidents: All incidents in DB
    - in_progress: Incidents currently being fixed
    - fix_failed: Failed fix attempts
    - auto_fix_rate: Percentage of auto-resolved incidents
    - avg_resolution_time: Average time to resolve (minutes)
    - rca_coverage: Percentage of incidents with RCA
    """
    try:
        # Fetch total failed messages from SAP
        total_failed_messages = await mcp.error_fetcher.fetch_failed_messages_count()
        
        # Get all incidents
        all_incidents = get_all_incidents(limit=1000)
        total_incidents = len(all_incidents)
        
        # Calculate status counts
        in_progress = sum(
            1 for i in all_incidents
            if i.get("status") in {"FIX_IN_PROGRESS", "RCA_IN_PROGRESS", "FIX_DEPLOYED"}
        )

        fix_failed = sum(
            1 for i in all_incidents
            if i.get("status") in {
                "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY",
                "FIX_FAILED_RUNTIME", "TICKET_CREATED",
            }
        )

        auto_fixed = sum(
            1 for i in all_incidents
            if i.get("status") in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED"}
        )

        pending_approval = sum(
            1 for i in all_incidents
            if i.get("status") in {"AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW"}
        )
        
        # Calculate auto-fix rate
        auto_fix_rate = round(auto_fixed / total_incidents * 100, 1) if total_incidents > 0 else 0.0
        
        # Calculate average resolution time
        resolution_times = []
        for inc in all_incidents:
            if inc.get("resolved_at") and inc.get("created_at"):
                created = _parse_sap_timestamp(str(inc.get("created_at")))
                resolved = _parse_sap_timestamp(str(inc.get("resolved_at")))
                if created and resolved:
                    delta = (resolved - created).total_seconds() / 60  # minutes
                    resolution_times.append(delta)
        
        avg_resolution_time = round(sum(resolution_times) / len(resolution_times), 1) if resolution_times else 0.0
        
        # Calculate RCA coverage
        rca_complete = sum(
            1 for i in all_incidents
            if i.get("root_cause") and i.get("proposed_fix")
        )
        rca_coverage = round(rca_complete / total_incidents * 100, 1) if total_incidents > 0 else 0.0
        
        return {
            "total_failed_messages": total_failed_messages,
            "total_incidents": total_incidents,
            "in_progress": in_progress,
            "fix_failed": fix_failed,
            "auto_fixed": auto_fixed,
            "pending_approval": pending_approval,
            "auto_fix_rate": auto_fix_rate,
            "avg_resolution_time_minutes": avg_resolution_time,
            "rca_coverage_percent": rca_coverage,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] KPI cards error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# ERROR DISTRIBUTION (PIE/DONUT CHART)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/error-distribution")
async def get_error_distribution():
    """
    Error type distribution for pie/donut charts.
    
    Returns counts and percentages for each error type.
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        total = count_all_incidents()
        
        error_types: Dict[str, int] = defaultdict(int)
        for inc in all_incidents:
            et = inc.get("error_type") or "UNKNOWN"
            error_types[et] += 1
        
        # Build chart data
        distribution = [
            {
                "error_type": et,
                "count": count,
                "percentage": round(count / total * 100, 1) if total > 0 else 0.0,
            }
            for et, count in sorted(error_types.items(), key=lambda x: x[1], reverse=True)
        ]
        
        return {
            "total_incidents": total,
            "distribution": distribution,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Error distribution error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# STATUS DISTRIBUTION (PIE/DONUT CHART)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status-distribution")
async def get_status_distribution():
    """
    Incident status distribution for pie/donut charts.
    
    Returns counts and percentages for each status.
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        total = count_all_incidents()
        
        statuses: Dict[str, int] = defaultdict(int)
        for inc in all_incidents:
            status = inc.get("status") or "UNKNOWN"
            statuses[status] += 1
        
        # Build chart data
        distribution = [
            {
                "status": status,
                "count": count,
                "percentage": round(count / total * 100, 1) if total > 0 else 0.0,
            }
            for status, count in sorted(statuses.items(), key=lambda x: x[1], reverse=True)
        ]
        
        return {
            "total_incidents": total,
            "distribution": distribution,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Status distribution error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# STATUS BREAKDOWN - DETAILED COUNTS FOR ALL STATUSES
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status-breakdown")
async def get_status_breakdown():
    """
    Detailed breakdown of incident counts for each status.
    
    Returns counts for all possible incident statuses:
    Analysis:    DETECTED → RCA_IN_PROGRESS → RCA_COMPLETE | RCA_FAILED | RCA_INCONCLUSIVE
    Human gate:  AWAITING_APPROVAL → AWAITING_HUMAN_REVIEW | REJECTED
    Fix pipeline: FIX_IN_PROGRESS → FIX_DEPLOYED → FIX_VERIFIED | FIX_FAILED_*
    Other:       HUMAN_INITIATED_FIX, RETRIED, TICKET_CREATED, ARTIFACT_MISSING,
                 VERIFICATION_UNAVAILABLE
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        total = count_all_incidents()
        
        # Define all possible statuses with descriptions
        status_definitions = {
            # ── Analysis ──
            "DETECTED":               "Error detected, no analysis yet",
            "RCA_IN_PROGRESS":        "AI is analyzing root cause",
            "RCA_COMPLETE":           "Analysis done, fix proposed",
            "RCA_FAILED":             "Root cause analysis failed (exception)",
            "RCA_INCONCLUSIVE":       "Analysis complete but confidence too low to propose a fix",
            # ── Human gate ──
            "AWAITING_APPROVAL":      "Fix proposed — waiting for human approval",
            "AWAITING_HUMAN_REVIEW":  "Escalated — stale approval timeout, requires human diagnosis",
            "REJECTED":               "Human rejected the proposed fix",
            # ── Fix pipeline ──
            "FIX_IN_PROGRESS":        "Fix is being applied right now",
            "FIX_DEPLOYED":           "Fix deployed — awaiting runtime verification",
            # ── Terminal: resolved ──
            "FIX_VERIFIED":           "Fix applied and runtime verified (autonomous)",
            "HUMAN_INITIATED_FIX":    "Fix applied via human-initiated action",
            "RETRIED":                "Transient error resolved by message retry",
            # ── Terminal: failed ──
            "FIX_FAILED":             "Fix attempt failed (unknown stage or exception)",
            "FIX_FAILED_UPDATE":      "Fix failed — iFlow content update step rejected",
            "FIX_FAILED_DEPLOY":      "Fix failed — content updated but deploy step failed",
            "FIX_FAILED_RUNTIME":     "Fix deployed but iFlow still erroring at runtime",
            "TICKET_CREATED":         "Low confidence or escalated — external ticket raised",
            # ── Artifact ──
            "ARTIFACT_MISSING":       "iFlow missing or deleted in SAP CPI (HTTP 404)",
            "VERIFICATION_UNAVAILABLE": "iFlow existence could not be verified (SAP API error)",
        }
        
        # Count incidents by status
        status_counts: Dict[str, int] = defaultdict(int)
        for inc in all_incidents:
            status = inc.get("status") or "UNKNOWN"
            status_counts[status] += 1
        
        # Build detailed breakdown with all statuses
        breakdown = []
        for status, description in status_definitions.items():
            count = status_counts.get(status, 0)
            percentage = round(count / total * 100, 1) if total > 0 else 0.0
            
            breakdown.append({
                "status": status,
                "description": description,
                "count": count,
                "percentage": percentage,
            })
        
        # Add any unknown statuses found in the data
        for status, count in status_counts.items():
            if status not in status_definitions and status != "UNKNOWN":
                percentage = round(count / total * 100, 1) if total > 0 else 0.0
                breakdown.append({
                    "status": status,
                    "description": "Unknown status",
                    "count": count,
                    "percentage": percentage,
                })
        
        # Sort by count descending
        breakdown.sort(key=lambda x: x["count"], reverse=True)
        
        # Calculate category groups
        active_statuses = [
            "DETECTED", "RCA_IN_PROGRESS", "RCA_COMPLETE",
            "AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW", "FIX_IN_PROGRESS", "FIX_DEPLOYED",
        ]
        resolved_statuses = ["FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED"]
        failed_statuses = [
            "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
            "RCA_FAILED", "RCA_INCONCLUSIVE", "REJECTED", "TICKET_CREATED",
            "ARTIFACT_MISSING", "VERIFICATION_UNAVAILABLE",
        ]
        
        active_count = sum(status_counts.get(s, 0) for s in active_statuses)
        resolved_count = sum(status_counts.get(s, 0) for s in resolved_statuses)
        failed_count = sum(status_counts.get(s, 0) for s in failed_statuses)
        
        return {
            "total_incidents": total,
            "breakdown": breakdown,
            "summary": {
                "active": {
                    "count": active_count,
                    "percentage": round(active_count / total * 100, 1) if total > 0 else 0.0,
                    "statuses": active_statuses,
                },
                "resolved": {
                    "count": resolved_count,
                    "percentage": round(resolved_count / total * 100, 1) if total > 0 else 0.0,
                    "statuses": resolved_statuses,
                },
                "failed": {
                    "count": failed_count,
                    "percentage": round(failed_count / total * 100, 1) if total > 0 else 0.0,
                    "statuses": failed_statuses,
                },
            },
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Status breakdown error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# FAILURES OVER TIME (TIME-SERIES CHART)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/failures-over-time")
async def get_failures_over_time(
    interval: str = "hour",
    time_range: str = "24h",
    mcp=Depends(_get_mcp),
):
    """
    Time-series data for failures over time chart.
    
    Parameters:
    - interval: hour, day, week, month
    - time_range: 1h, 24h, 7d, 30d
    
    Returns time-bucketed failure counts.
    """
    try:
        # Parse time range
        now = datetime.utcnow()
        if time_range == "1h":
            cutoff = now - timedelta(hours=1)
        elif time_range == "24h":
            cutoff = now - timedelta(hours=24)
        elif time_range == "7d":
            cutoff = now - timedelta(days=7)
        elif time_range == "30d":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = now - timedelta(hours=24)
        
        # Fetch failed messages
        raw_errors = await mcp.error_fetcher.fetch_failed_messages(limit=500)
        
        # Bucket by time
        time_buckets: Dict[str, int] = defaultdict(int)
        for raw in raw_errors:
            log_end = _parse_sap_timestamp(str(raw.get("LogEnd") or ""))
            if log_end and log_end >= cutoff:
                bucket = _time_bucket(log_end, interval)
                time_buckets[bucket] += 1
        
        # Sort by time
        series = [
            {"time": bucket, "count": count}
            for bucket, count in sorted(time_buckets.items())
        ]
        
        return {
            "interval": interval,
            "time_range": time_range,
            "series": series,
            "total_failures": sum(time_buckets.values()),
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Failures over time error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# TOP FAILING IFLOWS (BAR CHART)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/top-failing-iflows")
async def get_top_failing_iflows(limit: int = 10, mcp=Depends(_get_mcp)):
    """
    Top N failing iFlows for bar chart visualization.
    
    Returns iFlow names with failure counts, sorted descending.
    """
    try:
        raw_errors = await mcp.error_fetcher.fetch_failed_messages(limit=500)
        
        iflow_counts: Dict[str, int] = defaultdict(int)
        for raw in raw_errors:
            iflow = raw.get("IntegrationFlowName", "Unknown")
            iflow_counts[iflow] += 1
        
        # Sort and limit
        top_iflows = [
            {"iflow_name": iflow, "failure_count": count}
            for iflow, count in sorted(iflow_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        ]
        
        return {
            "top_iflows": top_iflows,
            "total_unique_iflows": len(iflow_counts),
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Top failing iFlows error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# SENDER/RECEIVER STATS (BAR CHART)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sender-receiver-stats")
async def get_sender_receiver_stats(limit: int = 10, mcp=Depends(_get_mcp)):
    """
    Sender and receiver failure statistics for bar charts.
    
    Returns top senders and receivers by failure count.
    """
    try:
        raw_errors = await mcp.error_fetcher.fetch_failed_messages(limit=500)
        
        sender_counts: Dict[str, int] = defaultdict(int)
        receiver_counts: Dict[str, int] = defaultdict(int)
        
        for raw in raw_errors:
            sender = raw.get("Sender", "Unknown")
            receiver = raw.get("Receiver", "Unknown")
            sender_counts[sender] += 1
            receiver_counts[receiver] += 1
        
        # Sort and limit
        top_senders = [
            {"sender": sender, "failure_count": count}
            for sender, count in sorted(sender_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        ]
        
        top_receivers = [
            {"receiver": receiver, "failure_count": count}
            for receiver, count in sorted(receiver_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        ]
        
        return {
            "top_senders": top_senders,
            "top_receivers": top_receivers,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Sender/receiver stats error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVE INCIDENTS TABLE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/active-incidents-table")
async def get_active_incidents_table(limit: int = 20):
    """
    Real-time active incidents table data.
    
    Returns incidents that are currently being processed or awaiting action.
    """
    try:
        active_statuses = {
            "DETECTED", "RCA_IN_PROGRESS", "RCA_COMPLETE",
            "AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW", "FIX_IN_PROGRESS", "FIX_DEPLOYED",
        }
        
        all_incidents = get_all_incidents(limit=500)
        active_incidents = [
            {
                "incident_id": inc.get("incident_id"),
                "message_guid": inc.get("message_guid"),
                "iflow_id": inc.get("iflow_id"),
                "error_type": inc.get("error_type"),
                "status": inc.get("status"),
                "created_at": inc.get("created_at"),
                "last_seen": inc.get("last_seen"),
                "occurrence_count": inc.get("occurrence_count", 1),
                "rca_confidence": inc.get("rca_confidence"),
            }
            for inc in all_incidents
            if inc.get("status") in active_statuses
        ][:limit]
        
        return {
            "active_incidents": active_incidents,
            "total_active": len(active_incidents),
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Active incidents table error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/incidents/paginated")
async def get_incidents_paginated(
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
):
    """
    Paginated active incidents for the dashboard table.
    """
    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be >= 1")
    if page_size < 1 or page_size > 500:
        raise HTTPException(status_code=400, detail="Page size must be between 1 and 500")

    try:
        active_statuses = {
            "DETECTED", "RCA_IN_PROGRESS", "RCA_COMPLETE",
            "AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW", "FIX_IN_PROGRESS", "FIX_DEPLOYED",
        }
        
        all_incidents = get_all_incidents(limit=0)
        
        filtered_incidents = [
            {
                "incident_id": inc.get("incident_id"),
                "message_guid": inc.get("message_guid"),
                "iflow_id": inc.get("iflow_id"),
                "error_type": inc.get("error_type"),
                "status": inc.get("status"),
                "created_at": inc.get("created_at"),
                "last_seen": inc.get("last_seen"),
                "occurrence_count": inc.get("occurrence_count", 1),
                "rca_confidence": inc.get("rca_confidence"),
            }
            for inc in all_incidents
            if inc.get("status") in active_statuses
            and (status is None or str(inc.get("status")).upper() == status.upper())
        ]
        
        total_count = len(filtered_incidents)
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1
        
        if page > total_pages and total_count > 0:
            raise HTTPException(status_code=404, detail=f"Page {page} not found. Total pages: {total_pages}")
        
        start_idx = (page - 1) * page_size
        paginated_incidents = filtered_incidents[start_idx : start_idx + page_size]
        
        return {
            "count": len(paginated_incidents),
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
            "incidents": paginated_incidents,
            "timestamp": get_hana_timestamp(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[Dashboard] Paginated incidents error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# RECENT FAILURES TABLE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/recent-failures-table")
async def get_recent_failures_table(limit: int = 20):
    """
    Recent failed messages feed for table display.

    Reads from HANA incidents (no live CPI OData call) so it works
    immediately at startup and shows error previews correctly.
    """
    try:
        rows = get_all_incidents(limit=limit)

        recent_failures = [
            {
                "message_guid":  inc.get("message_guid") or "-",
                "iflow_name":    inc.get("iflow_id") or inc.get("iflow_name") or "-",
                "status":        inc.get("status") or "DETECTED",
                "log_end":       inc.get("last_seen") or inc.get("log_end") or inc.get("created_at"),
                "sender":        inc.get("sender"),
                "receiver":      inc.get("receiver"),
                "error_preview": (inc.get("error_message") or "")[:120],
            }
            for inc in rows
        ]

        return {
            "recent_failures": recent_failures,
            "count": len(recent_failures),
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Recent failures table error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# FIX PROGRESS TRACKER
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/fix-progress-tracker")
async def get_fix_progress_tracker():
    """
    Operational fix progress widget showing current fix operations.
    
    Returns incidents currently in FIX_IN_PROGRESS status with details.
    """
    try:
        all_incidents = get_all_incidents(limit=500)
        in_progress = [
            {
                "incident_id": inc.get("incident_id"),
                "message_guid": inc.get("message_guid"),
                "iflow_id": inc.get("iflow_id"),
                "error_type": inc.get("error_type"),
                "status": inc.get("status"),
                "fix_summary": inc.get("fix_summary"),
                "started_at": inc.get("last_seen"),
            }
            for inc in all_incidents
            if inc.get("status") == "FIX_IN_PROGRESS"
        ]
        
        return {
            "fixes_in_progress": in_progress,
            "count": len(in_progress),
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Fix progress tracker error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# LEADERBOARDS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/leaderboard/noisy-integrations")
async def get_noisy_integrations_leaderboard(limit: int = 10):
    """
    Leaderboard: Most noisy integrations (by occurrence count).
    
    Returns iFlows with highest total occurrence counts.
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        
        iflow_occurrences: Dict[str, int] = defaultdict(int)
        for inc in all_incidents:
            iflow = inc.get("iflow_id", "Unknown")
            iflow_occurrences[iflow] += inc.get("occurrence_count", 1)
        
        leaderboard = [
            {"iflow_id": iflow, "total_occurrences": count}
            for iflow, count in sorted(iflow_occurrences.items(), key=lambda x: x[1], reverse=True)[:limit]
        ]
        
        return {
            "leaderboard": leaderboard,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Noisy integrations leaderboard error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/leaderboard/recurring-incidents")
async def get_recurring_incidents_leaderboard(limit: int = 10):
    """
    Leaderboard: Most recurring incidents (by occurrence count per incident).
    
    Returns individual incidents with highest occurrence counts.
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        
        # Sort by occurrence count
        recurring = sorted(
            all_incidents,
            key=lambda x: x.get("occurrence_count", 1),
            reverse=True
        )[:limit]
        
        leaderboard = [
            {
                "incident_id": inc.get("incident_id"),
                "message_guid": inc.get("message_guid"),
                "iflow_id": inc.get("iflow_id"),
                "error_type": inc.get("error_type"),
                "occurrence_count": inc.get("occurrence_count", 1),
                "status": inc.get("status"),
                "created_at": inc.get("created_at"),
                "last_seen": inc.get("last_seen"),
            }
            for inc in recurring
        ]
        
        return {
            "leaderboard": leaderboard,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Recurring incidents leaderboard error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/leaderboard/longest-open")
async def get_longest_open_leaderboard(limit: int = 10):
    """
    Leaderboard: Longest open/unresolved incidents.
    
    Returns incidents that have been open the longest without resolution.
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        
        # Filter unresolved incidents
        unresolved_statuses = {
            "DETECTED", "RCA_IN_PROGRESS", "RCA_COMPLETE",
            "AWAITING_APPROVAL", "AWAITING_HUMAN_REVIEW", "FIX_IN_PROGRESS", "FIX_DEPLOYED",
            "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
        }
        
        unresolved = [
            inc for inc in all_incidents
            if inc.get("status") in unresolved_statuses
        ]
        
        # Calculate age and sort
        now = datetime.utcnow()
        for inc in unresolved:
            created = _parse_sap_timestamp(str(inc.get("created_at") or ""))
            if created:
                age_hours = (now - created).total_seconds() / 3600
                inc["age_hours"] = round(age_hours, 1)
            else:
                inc["age_hours"] = 0
        
        longest_open = sorted(unresolved, key=lambda x: x.get("age_hours", 0), reverse=True)[:limit]
        
        leaderboard = [
            {
                "incident_id": inc.get("incident_id"),
                "message_guid": inc.get("message_guid"),
                "iflow_id": inc.get("iflow_id"),
                "error_type": inc.get("error_type"),
                "status": inc.get("status"),
                "created_at": inc.get("created_at"),
                "age_hours": inc.get("age_hours"),
            }
            for inc in longest_open
        ]
        
        return {
            "leaderboard": leaderboard,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Longest open leaderboard error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# DRILL-DOWN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/drill-down/message/{message_guid}")
async def drill_down_message(message_guid: str, mcp=Depends(_get_mcp)):
    """
    Detailed drill-down for a specific message GUID.
    
    Returns comprehensive message details, incident info, and related data.
    """
    try:
        # Get incident
        incident = get_incident_by_message_guid(message_guid)
        
        # Fetch raw message
        raw_errors = await mcp.error_fetcher.fetch_failed_messages(limit=200)
        raw = next((r for r in raw_errors if r.get("MessageGuid") == message_guid), {})
        
        # Fetch error details
        error_detail = await mcp.error_fetcher.fetch_error_details(message_guid) or {}
        
        return {
            "message_guid": message_guid,
            "incident": incident,
            "raw_message": raw,
            "error_detail": error_detail,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Message drill-down error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/drill-down/incident/{incident_id}")
async def drill_down_incident(incident_id: str):
    """
    Detailed drill-down for a specific incident ID.
    
    Returns full incident details and history.
    """
    try:
        incident = get_incident_by_id(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        
        return {
            "incident_id": incident_id,
            "incident": incident,
            "timestamp": get_hana_timestamp(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[Dashboard] Incident drill-down error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/drill-down/iflow/{iflow_name}")
async def drill_down_iflow(iflow_name: str, mcp=Depends(_get_mcp)):
    """
    iFlow-specific analytics and drill-down.
    
    Returns all incidents and failures for a specific iFlow.
    """
    try:
        # Get all incidents for this iFlow
        all_incidents = get_all_incidents(limit=1000)
        iflow_incidents = [
            inc for inc in all_incidents
            if inc.get("iflow_id") == iflow_name
        ]
        
        # Get recent failures for this iFlow
        raw_errors = await mcp.error_fetcher.fetch_failed_messages(limit=500)
        iflow_failures = [
            raw for raw in raw_errors
            if raw.get("IntegrationFlowName") == iflow_name
        ]
        
        # Calculate statistics
        total_incidents = len(iflow_incidents)
        total_failures = len(iflow_failures)
        
        error_types = defaultdict(int)
        for inc in iflow_incidents:
            et = inc.get("error_type", "UNKNOWN")
            error_types[et] += 1
        
        status_counts = defaultdict(int)
        for inc in iflow_incidents:
            status = inc.get("status", "UNKNOWN")
            status_counts[status] += 1
        
        return {
            "iflow_name": iflow_name,
            "total_incidents": total_incidents,
            "total_failures": total_failures,
            "error_type_distribution": dict(error_types),
            "status_distribution": dict(status_counts),
            "recent_incidents": iflow_incidents[:20],
            "recent_failures": iflow_failures[:20],
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] iFlow drill-down error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH METRICS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health-metrics")
async def get_health_metrics(mcp=Depends(_get_mcp)):
    """
    System health indicators for operational monitoring.
    
    Returns:
    - autonomous_enabled: Whether autonomous mode is active
    - fix_success_rate: Percentage of successful fixes
    - avg_rca_confidence: Average RCA confidence score
    - system_load: Current processing load indicators
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        
        # Fix success rate
        fix_attempted = sum(
            1 for inc in all_incidents
            if inc.get("status") in {
                "FIX_VERIFIED", "HUMAN_INITIATED_FIX",
                "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
            }
        )
        fix_succeeded = sum(
            1 for inc in all_incidents
            if inc.get("status") in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX"}
        )
        fix_success_rate = round(fix_succeeded / fix_attempted * 100, 1) if fix_attempted > 0 else 0.0
        
        # Average RCA confidence
        confidences = [
            float(inc.get("rca_confidence", 0))
            for inc in all_incidents
            if inc.get("rca_confidence")
        ]
        avg_rca_confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
        
        # System load
        active_fixes = sum(
            1 for inc in all_incidents
            if inc.get("status") == "FIX_IN_PROGRESS"
        )
        
        pending_rca = sum(
            1 for inc in all_incidents
            if inc.get("status") in {"DETECTED", "RCA_IN_PROGRESS"}
        )
        
        return {
            "autonomous_enabled": mcp._autonomous_running,
            "fix_success_rate": fix_success_rate,
            "avg_rca_confidence": avg_rca_confidence,
            "active_fixes": active_fixes,
            "pending_rca": pending_rca,
            "system_status": "healthy" if fix_success_rate > 70 else "degraded",
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] Health metrics error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# SLA METRICS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sla-metrics")
async def get_sla_metrics():
    """
    SLA compliance metrics for operational monitoring.
    
    Returns:
    - incidents_within_sla: Count of incidents resolved within SLA
    - incidents_breached_sla: Count of incidents that breached SLA
    - avg_time_to_resolution: Average resolution time
    - sla_compliance_rate: Percentage of incidents meeting SLA
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        
        # Define SLA threshold (e.g., 4 hours)
        sla_threshold_hours = 4
        
        resolved_incidents = [
            inc for inc in all_incidents
            if inc.get("status") in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED"}
            and inc.get("resolved_at") and inc.get("created_at")
        ]
        
        within_sla = 0
        breached_sla = 0
        resolution_times = []
        
        for inc in resolved_incidents:
            created = _parse_sap_timestamp(str(inc.get("created_at")))
            resolved = _parse_sap_timestamp(str(inc.get("resolved_at")))
            
            if created and resolved:
                resolution_hours = (resolved - created).total_seconds() / 3600
                resolution_times.append(resolution_hours)
                
                if resolution_hours <= sla_threshold_hours:
                    within_sla += 1
                else:
                    breached_sla += 1
        
        total_resolved = len(resolved_incidents)
        sla_compliance_rate = round(within_sla / total_resolved * 100, 1) if total_resolved > 0 else 0.0
        avg_time_to_resolution = round(sum(resolution_times) / len(resolution_times), 2) if resolution_times else 0.0
        
        return {
            "sla_threshold_hours": sla_threshold_hours,
            "incidents_within_sla": within_sla,
            "incidents_breached_sla": breached_sla,
            "total_resolved": total_resolved,
            "sla_compliance_rate": sla_compliance_rate,
            "avg_time_to_resolution_hours": avg_time_to_resolution,
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] SLA metrics error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# RCA COVERAGE
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/rca-coverage")
async def get_rca_coverage():
    """
    AI RCA coverage statistics.
    
    Returns:
    - total_incidents: Total incidents
    - rca_complete: Incidents with completed RCA
    - rca_actionable: Incidents with actionable fixes
    - rca_coverage_rate: Percentage with RCA
    - actionable_rate: Percentage with actionable fixes
    - avg_confidence: Average RCA confidence
    """
    try:
        all_incidents = get_all_incidents(limit=1000)
        total = count_all_incidents()
        
        rca_complete = sum(
            1 for inc in all_incidents
            if inc.get("root_cause") and inc.get("proposed_fix")
        )
        
        rca_actionable = sum(
            1 for inc in all_incidents
            if inc.get("root_cause")
            and inc.get("proposed_fix")
            and float(inc.get("rca_confidence", 0)) >= 0.7
        )
        
        confidences = [
            float(inc.get("rca_confidence", 0))
            for inc in all_incidents
            if inc.get("rca_confidence")
        ]
        
        avg_confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
        rca_coverage_rate = round(rca_complete / total * 100, 1) if total > 0 else 0.0
        actionable_rate = round(rca_actionable / total * 100, 1) if total > 0 else 0.0
        
        # Confidence distribution
        high_confidence = sum(1 for c in confidences if c >= 0.9)
        medium_confidence = sum(1 for c in confidences if 0.7 <= c < 0.9)
        low_confidence = sum(1 for c in confidences if c < 0.7)
        
        return {
            "total_incidents": total,
            "rca_complete": rca_complete,
            "rca_actionable": rca_actionable,
            "rca_coverage_rate": rca_coverage_rate,
            "actionable_rate": actionable_rate,
            "avg_confidence": avg_confidence,
            "confidence_distribution": {
                "high": high_confidence,
                "medium": medium_confidence,
                "low": low_confidence,
            },
            "timestamp": get_hana_timestamp(),
        }
    except Exception as exc:
        logger.error(f"[Dashboard] RCA coverage error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))