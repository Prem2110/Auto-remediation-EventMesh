"""
main_v2.py
==========
SAP CPI Self-Healing Agent — refactored multi-agent entry point.

This file contains ONLY:
  - FastAPI app setup + CORS middleware
  - Lifespan: create all agents, wire dependencies, start autonomous loop
  - All HTTP endpoints (delegating to the appropriate agent)
  - POST /aem/events  — AEM webhook entry point

All business logic lives in:
  core/     — mcp_manager, validators, constants, state
  agents/   — classifier, observer, rca, fix, verifier, orchestrator
  aem/      — event_bus

To promote this to the live main.py:
  mv main.py main_legacy.py && mv main_v2.py main.py
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─────────────────────────────────────────────
# ENV + LOGGING
# ─────────────────────────────────────────────
load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Import and configure logging to file + console
from utils.logger_config import configure_logging
configure_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CORE IMPORTS
# ─────────────────────────────────────────────
from core.constants import (
    AUTO_DEPLOY_AFTER_FIX,
    AUTO_FIX_ALL_CPI_ERRORS,
    AUTO_FIX_CONFIDENCE,
    BURST_DEDUP_WINDOW_SECONDS,
    FAILED_MESSAGE_FETCH_LIMIT,
    FIX_INTENT_KEYWORDS,
    RUNTIME_ERROR_FETCH_LIMIT,
    SUGGEST_FIX_CONFIDENCE,
)
from core.mcp_manager import MultiMCP
from core.state import FIX_PROGRESS, get_fix_progress

# ─────────────────────────────────────────────
# AGENT IMPORTS
# ─────────────────────────────────────────────
from agents.base import ApprovalRequest, DirectFixRequest, QueryRequest, QueryResponse
from agents.classifier_agent import ClassifierAgent
from agents.fix_agent import FixAgent
from agents.observer_agent import ObserverAgent, SAPErrorFetcher
from agents.orchestrator_agent import OrchestratorAgent
from agents.rca_agent import RCAAgent
from agents.verifier_agent import VerifierAgent

# ─────────────────────────────────────────────
# AEM
# ─────────────────────────────────────────────
from aem.event_bus import AEMEventBus, event_bus
# solace_client removed — Event Mesh delivers via HTTP webhook push

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
from db.database import (
    create_incident,
    get_all_history,
    count_all_incidents,
    get_all_incidents,
    get_stage_counts,
    get_incident_by_id,
    get_incident_by_message_guid,
    get_open_incident_by_signature,
    get_pending_approvals,
    get_escalation_tickets,
    get_escalation_ticket_by_id,
    create_escalation_ticket,
    update_escalation_ticket,
    get_recent_incident_by_group_key,
    get_similar_patterns,
    get_testsuite_log_entries,
    get_xsd_files_by_session,
    create_query_history,
    update_query_history,
    update_incident,
    increment_incident_occurrence,
    ensure_em_schema,
    set_db_source,
)
from storage.storage import upload_multiple_files
from utils.utils import get_hana_timestamp

# ─────────────────────────────────────────────
# GLOBALS — set during lifespan
# ─────────────────────────────────────────────
mcp:          Optional[MultiMCP]          = None
observer:     Optional[ObserverAgent]     = None
orchestrator: Optional[OrchestratorAgent] = None

# ─────────────────────────────────────────────
# EVENT MESH WEBHOOK COUNTER
# ─────────────────────────────────────────────
_WEBHOOK_LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "event_mesh_webhook.log")
os.makedirs(os.path.dirname(_WEBHOOK_LOG_PATH), exist_ok=True)

def _load_webhook_counter() -> int:
    try:
        with open(_WEBHOOK_LOG_PATH, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0

_webhook_counter: int = _load_webhook_counter()

# ─────────────────────────────────────────────
# AGENT WEBHOOK ROLLING COUNTER (7-day window, in-memory)
# ─────────────────────────────────────────────
_AGENT_WEBHOOK_WINDOW = timedelta(days=7)
_agent_webhook_ts: deque[datetime] = deque()


def _record_agent_webhook() -> None:
    """Append now to the rolling deque and drop entries older than 7 days."""
    now = datetime.now(UTC)
    _agent_webhook_ts.append(now)
    cutoff = now - _AGENT_WEBHOOK_WINDOW
    while _agent_webhook_ts and _agent_webhook_ts[0] < cutoff:
        _agent_webhook_ts.popleft()


def _agent_webhook_count() -> int:
    """Return number of agent webhook hits recorded in the last 7 days."""
    cutoff = datetime.now(UTC) - _AGENT_WEBHOOK_WINDOW
    # deque is sorted by insertion time; trim stale from the left then count
    while _agent_webhook_ts and _agent_webhook_ts[0] < cutoff:
        _agent_webhook_ts.popleft()
    return len(_agent_webhook_ts)


# ─────────────────────────────────────────────
# CPI MONITOR BACKGROUND TASK
# ─────────────────────────────────────────────

async def _run_cpi_monitor() -> None:
    """
    Background polling loop: query CPI for FAILED messages every
    CPI_POLL_INTERVAL_SECONDS (default 600) and publish each new failure
    to the Event Mesh topic default/sierra.automation/1/autofix/in.

    All errors are caught and logged — this loop must never crash the app.
    """
    try:
        from cpi_monitor.cpi_poller import poll_failed_messages, _POLL_INTERVAL
        from cpi_monitor.error_publisher import publish_failed_messages
    except Exception as exc:
        logger.error("[CPI_MONITOR] Module import failed — poller disabled: %s", exc)
        return

    while True:
        try:
            messages = await poll_failed_messages()
            if messages:
                logger.info("[CPI_MONITOR] Found %d failed messages", len(messages))
                await publish_failed_messages(messages)
        except Exception as exc:
            logger.error("[CPI_MONITOR] Unhandled poller error: %s", exc)
        await asyncio.sleep(_POLL_INTERVAL)


# ─────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp, observer, orchestrator

    # Create Event Mesh tables on startup (no-op if they already exist)
    ensure_em_schema()

    # Create the MCP infrastructure only if servers are configured
    from core.constants import MCP_SERVERS
    if MCP_SERVERS:
        mcp = MultiMCP()
    else:
        mcp = None
        logger.info("[Startup] MCP servers disabled - skipping MCP initialization")

    # Create all specialist agents (not yet wired)
    _rca      = RCAAgent(mcp)
    _fix      = FixAgent(mcp)
    _verifier = VerifierAgent(mcp)
    observer  = ObserverAgent(mcp)

    # Create orchestrator with all specialist references
    orchestrator = OrchestratorAgent(mcp, _rca, _fix, _verifier)

    # Wire observer ↔ orchestrator (late injection to avoid circular import)
    observer.set_orchestrator(orchestrator)
    orchestrator.set_observer(observer)   # must be set before start() so OData fallback works at boot

    # Wire error_fetcher → fix agent and verifier agent (shared token cache)
    _fix.set_error_fetcher(observer.error_fetcher)
    _verifier.set_error_fetcher(observer.error_fetcher)

    async def _init_background():
        try:
            if mcp:
                logger.info("[Startup] Initialising MCP servers in background…")
                await mcp.connect()
                await mcp.discover_tools()
                await mcp.build_agent()                  # full-toolset shared agent (must be first)
            else:
                logger.info("[Startup] MCP disabled - skipping MCP initialization")

            # Build all specialist agents in parallel — they are independent of each other
            await asyncio.gather(
                observer.build_agent(),
                orchestrator._classifier.build_agent(mcp),
                _rca.build_agent(),
                _fix.build_agent(),
                _verifier.build_agent(),
            )
            await orchestrator.build_agent(observer=observer)  # depends on all above being ready
            logger.info("[Startup] All specialist agents built — orchestrator ready to process messages.")
        except Exception as exc:
            logger.error("[Startup] Agent initialisation failed: %s", exc)
        finally:
            # Always mark ready so /autonomous/status returns True and webhooks are accepted.
            # If MCP discovery partially failed, whatever tools loaded are still usable.
            orchestrator._agents_ready = True
            tool_count = len(mcp.tools) if mcp else 0
            logger.info(
                "[Startup] pipeline_running=True — MCP tools loaded: %d, agents_ready: True",
                tool_count,
            )

    asyncio.create_task(_init_background())
    asyncio.create_task(_run_cpi_monitor())
    logger.info("[CPI_MONITOR] Poller started, interval=%ds", int(os.getenv("CPI_POLL_INTERVAL_SECONDS", "600")))
    logger.info("[Startup] FastAPI ready — agents initialising in background.")
    logger.info("[Startup] Event-driven mode active — waiting for SAP Event Mesh webhooks")
    yield


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────
app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── Smart Monitoring routers ──────────────────
from smart_monitoring import router as _sm_router          # noqa: E402
from smart_monitoring_dashboard import router as _sm_dash  # noqa: E402
app.include_router(_sm_router)
app.include_router(_sm_dash)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _guard() -> None:
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Agents not ready — still initialising.")


def _resolve_incident(incident_ref: str) -> Optional[Dict]:
    return get_incident_by_id(incident_ref) or get_incident_by_message_guid(incident_ref)


def parse_query_request(
    query:   str           = Form(...),
    id:      Optional[str] = Form(None),
    user_id: str           = Form(...),
) -> QueryRequest:
    return QueryRequest(query=query, id=id, user_id=user_id)


def _has_fix_intent(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in FIX_INTENT_KEYWORDS)


# ─────────────────────────────────────────────
# ROOT
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "running", "service": "CPI MCP Servers + Autonomous Ops", "version": "4.0.0"}


# ─────────────────────────────────────────────
# /query — chatbot
# ─────────────────────────────────────────────

@app.post("/query", response_model=QueryResponse)
async def query_endpoint(
    req:   QueryRequest               = Depends(parse_query_request),
    files: Optional[List[UploadFile]] = File(None),
):
    _guard()
    timestamp  = get_hana_timestamp()
    session_id = req.id or str(uuid.uuid4())
    result: Dict[str, Any] = {}

    try:
        if files:
            try:
                await upload_multiple_files(session_id, files, timestamp, req.user_id)
            except Exception as exc:
                logger.warning("File upload failed: %s", exc)

        xsd_files      = get_xsd_files_by_session(session_id)
        enhanced_query = req.query
        if xsd_files:
            xsd_context = "\n\n--- XSD Files Available in This Session ---\n"
            for xsd in xsd_files:
                xsd_context += (
                    f"\nFile: {xsd['file_id']}\n"
                    f"Target Namespace: {xsd['target_namespace']}\n"
                    f"Elements: {xsd['element_count']}, Types: {xsd['type_count']}\n"
                    f"XSD Content:\n```xml\n{xsd['content']}\n```\n"
                )
            enhanced_query = xsd_context + "\n\n" + req.query

        fix_triggered = False
        if _has_fix_intent(req.query):
            pending     = get_all_incidents(status="AWAITING_APPROVAL", limit=5)
            rca_done    = get_all_incidents(status="RCA_COMPLETE", limit=5)
            fix_failed  = get_all_incidents(status="FIX_FAILED", limit=5)
            fix_failed_deploy = get_all_incidents(status="FIX_FAILED_DEPLOY", limit=3)
            fix_failed_update = get_all_incidents(status="FIX_FAILED_UPDATE", limit=3)
            candidates  = pending + rca_done + fix_failed + fix_failed_deploy + fix_failed_update

            matched_incident = None
            for inc in candidates:
                if (inc.get("iflow_id", "").lower() in req.query.lower()
                        or inc.get("incident_id", "") in req.query):
                    matched_incident = inc
                    break
            if not matched_incident and len(candidates) == 1:
                matched_incident = candidates[0]

            if matched_incident and matched_incident.get("proposed_fix"):
                fix_triggered = True
                logger.info("[Query] Fix intent → incident: %s", matched_incident["incident_id"])
                fix_result = await orchestrator._fix.ask_fix_and_deploy(
                    iflow_id=matched_incident["iflow_id"],
                    error_message=matched_incident.get("error_message", ""),
                    proposed_fix=matched_incident.get("proposed_fix", ""),
                    root_cause=matched_incident.get("root_cause", ""),
                    error_type=matched_incident.get("error_type", "UNKNOWN"),
                    affected_component=matched_incident.get("affected_component", ""),
                    user_id=req.user_id,
                    session_id=session_id,
                    timestamp=timestamp,
                )
                final_status = "HUMAN_INITIATED_FIX" if fix_result["success"] else (
                    "FIX_FAILED_DEPLOY" if fix_result.get("failed_stage") == "deploy"
                    else "FIX_FAILED_UPDATE" if fix_result.get("failed_stage") in ("update", "get")
                    else "FIX_FAILED"
                )
                update_incident(matched_incident["incident_id"], {
                    "status":      final_status,
                    "fix_summary": fix_result["summary"],
                    "resolved_at": get_hana_timestamp() if fix_result["success"] else None,
                    "verification_status": "VERIFIED" if fix_result["success"] else "PENDING",
                })
                result = {"answer": fix_result["summary"], "steps": fix_result.get("steps", [])}
            elif candidates:
                fix_triggered = True
                result = {
                    "answer": (
                        "Multiple actionable incidents exist. Please specify the incident_id or "
                        "iFlow ID you want to fix."
                    ),
                    "steps": [],
                }

        if not fix_triggered:
            result = await orchestrator.ask(enhanced_query, req.user_id, session_id, timestamp)

        question = req.query.strip()
        if not req.id:
            create_query_history(session_id, question, result.get("answer") or "Request failed!", timestamp, req.user_id)
        else:
            update_query_history(session_id, question, result.get("answer") or "Request failed!", timestamp)

    except Exception as exc:
        logger.error("query_endpoint error: %s", exc)
        result = {"error": str(exc)}

    return QueryResponse(
        response=result.get("answer") or "Request failed! Try again.",
        id=session_id,
        error=result,
    )


# ─────────────────────────────────────────────
# /fix — direct fix
# ─────────────────────────────────────────────

@app.post("/fix")
async def direct_fix_endpoint(req: DirectFixRequest):
    _guard()
    timestamp  = get_hana_timestamp()
    session_id = f"direct_fix_{uuid.uuid4()}"

    proposed_fix       = req.proposed_fix or ""
    root_cause         = ""
    error_type         = "UNKNOWN"
    affected_component = ""
    confidence         = 0.0

    if not proposed_fix:
        clf            = orchestrator._classifier.classify_error(req.error_message)
        fake_incident  = {
            "incident_id":   str(uuid.uuid4()),
            "iflow_id":      req.iflow_id,
            "error_message": req.error_message,
            "error_type":    clf["error_type"],
            "message_guid":  "",
        }
        rca            = await orchestrator._rca.run_rca(fake_incident)
        proposed_fix       = rca.get("proposed_fix", "")
        root_cause         = rca.get("root_cause", "")
        error_type         = rca.get("error_type", clf["error_type"])
        affected_component = rca.get("affected_component", "")
        confidence         = rca.get("confidence", 0.0)

    if not proposed_fix:
        return {
            "success": False, "fix_applied": False, "deploy_success": False,
            "summary": "Could not determine a proposed fix. Please provide proposed_fix.",
            "rca_confidence": confidence,
        }

    fix_result = await orchestrator._fix.ask_fix_and_deploy(
        iflow_id=req.iflow_id,
        error_message=req.error_message,
        proposed_fix=proposed_fix,
        root_cause=root_cause,
        error_type=error_type,
        affected_component=affected_component,
        user_id=req.user_id,
        session_id=session_id,
        timestamp=timestamp,
    )
    return {
        "iflow_id":       req.iflow_id,
        "fix_applied":    fix_result.get("fix_applied", False),
        "deploy_success": fix_result.get("deploy_success", False),
        "success":        fix_result.get("success", False),
        "summary":        fix_result.get("summary", ""),
        "rca_confidence": confidence,
        "proposed_fix":   proposed_fix,
        "steps_count":    len(fix_result.get("steps", [])),
    }


# ─────────────────────────────────────────────
# HISTORY / TEST SUITE
# ─────────────────────────────────────────────

@app.get("/get_all_history")
async def get_history_endpoint(user_id: Optional[str] = None):
    try:
        return {"history": get_all_history(user_id)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/get_testsuite_logs")
async def get_testsuite_logs(user_id: Optional[str] = None):
    try:
        return {"ts_logs": get_testsuite_log_entries(user_id)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────
# AUTONOMOUS CONTROL
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# AUTO-FIX CONFIGURATION
# ─────────────────────────────────────────────

@app.get("/api/config/auto-fix")
async def get_auto_fix_status():
    try:
        from config.config import Config  # noqa: PLC0415
        enabled   = Config.get_auto_fix_enabled()
        env_value = os.getenv("AUTO_FIX_ENABLED", "false").lower() == "true"
        return {
            "enabled":     enabled,
            "source":      "runtime" if enabled != env_value else "env",
            "env_default": env_value,
            "timestamp":   get_hana_timestamp(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/config/auto-fix")
async def set_auto_fix_status(enabled: bool):
    try:
        from config.config import Config  # noqa: PLC0415
        if Config.set_auto_fix_enabled(enabled):
            return {"success": True, "enabled": enabled,
                    "message": f"Auto-fix {'enabled' if enabled else 'disabled'} successfully",
                    "timestamp": get_hana_timestamp()}
        raise HTTPException(status_code=500, detail="Failed to update configuration")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/config/auto-fix/reset")
async def reset_auto_fix_to_env():
    try:
        from config.config import Config  # noqa: PLC0415
        if Config.reset_auto_fix_to_env():
            env_value = os.getenv("AUTO_FIX_ENABLED", "false").lower() == "true"
            return {"success": True, "enabled": env_value,
                    "message": "Auto-fix reset to .env configuration",
                    "timestamp": get_hana_timestamp()}
        raise HTTPException(status_code=500, detail="Failed to reset configuration")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────
# SAP CPI ERROR INVENTORY
# ─────────────────────────────────────────────

@app.get("/autonomous/cpi/errors")
async def get_cpi_error_inventory(
    message_limit:  int = FAILED_MESSAGE_FETCH_LIMIT,
    artifact_limit: int = RUNTIME_ERROR_FETCH_LIMIT,
):
    _guard()
    try:
        return await observer.error_fetcher.fetch_cpi_error_inventory(
            message_limit=message_limit, artifact_limit=artifact_limit
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/autonomous/cpi/messages/errors")
async def get_cpi_message_errors(limit: int = FAILED_MESSAGE_FETCH_LIMIT):
    _guard()
    try:
        raw_errors = await observer.error_fetcher.fetch_failed_messages(limit=limit)
        normalized = []
        for raw in raw_errors:
            guid    = raw.get("MessageGuid", "")
            details = await observer.error_fetcher.fetch_error_details(guid) if guid else {}
            normalized.append(observer.error_fetcher.normalize(raw, details))
        return {"count": len(normalized), "messages": normalized}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/autonomous/cpi/runtime_artifacts/errors")
async def get_cpi_runtime_artifact_errors(limit: int = RUNTIME_ERROR_FETCH_LIMIT):
    _guard()
    try:
        artifacts = await observer.error_fetcher.fetch_runtime_artifact_errors(limit=limit)
        return {"count": len(artifacts), "artifacts": artifacts}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/autonomous/cpi/runtime_artifacts/{artifact_id}")
async def get_cpi_runtime_artifact_detail(artifact_id: str):
    _guard()
    try:
        detail     = await observer.error_fetcher.fetch_runtime_artifact_detail(artifact_id)
        error_info = await observer.error_fetcher.fetch_runtime_artifact_error_detail(artifact_id)
        if not detail and not error_info:
            raise HTTPException(status_code=404, detail="Runtime artifact not found")
        return {"artifact_id": artifact_id, "detail": detail, "error_information": error_info}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────
# PIPELINE STATUS  (polled by frontend fetchPipelineStatus)
# ─────────────────────────────────────────────

@app.get("/autonomous/status")
async def autonomous_status():
    """Return whether the agent pipeline is ready to process events."""
    agents_ready = bool(orchestrator and orchestrator._agents_ready)
    tool_count   = len(mcp.tools) if mcp else 0
    logger.debug("[Autonomous/status] agents_ready=%s tool_count=%d", agents_ready, tool_count)
    return {
        "running":      agents_ready,
        "agents_ready": agents_ready,
        "tool_count":   tool_count,
    }


@app.post("/autonomous/start")
async def autonomous_start():
    """No-op in event-driven mode — always returns current readiness state."""
    agents_ready = bool(orchestrator and orchestrator._agents_ready)
    return {
        "running": agents_ready,
        "message": "Event-driven pipeline active" if agents_ready else "Agents still initialising",
    }


@app.post("/autonomous/stop")
async def autonomous_stop():
    """No-op in event-driven mode."""
    return {"running": False, "message": "Event-driven mode: stop is not applicable"}


# ─────────────────────────────────────────────
# TOOLS LISTING
# ─────────────────────────────────────────────

@app.get("/autonomous/tools")
async def list_loaded_tools(server: Optional[str] = None):
    _guard()
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for tool in mcp.tools:
        if server and tool.server != server:
            continue
        grouped.setdefault(tool.server, []).append({
            "agent_tool_name": tool.name,
            "mcp_tool_name":   tool.mcp_tool_name,
            "description":     tool.description,
            "fields":          mcp.get_tool_field_names(tool.server, tool.mcp_tool_name),
        })
    if server:
        return {"server": server, "tools": grouped.get(server, []),
                "count": len(grouped.get(server, []))}
    return {
        "servers": grouped,
        "counts":  {n: len(i) for n, i in grouped.items()},
        "total":   sum(len(i) for i in grouped.values()),
    }


# ─────────────────────────────────────────────
# INCIDENTS CRUD
# ─────────────────────────────────────────────

@app.get("/autonomous/incidents")
async def get_incidents(status: Optional[str] = None, limit: int = 50):
    try:
        incidents = get_all_incidents(status=status, limit=limit)
        def _is_sap_guid(value: str) -> bool:
            # SAP artifact GUIDs are 20+ char alphanumeric strings with no spaces,
            # underscores, or hyphens — e.g. "AGeNKJ3Th6DlBzQ2v9X1". Human-readable
            # iFlow names always contain at least one underscore, hyphen, or space.
            import re
            return bool(value) and len(value) >= 18 and bool(re.fullmatch(r"[A-Za-z0-9]{18,}", value))

        for inc in incidents:
            if not inc.get("iflow_name"):
                raw_id = inc.get("iflow_id") or ""
                inc["iflow_name"] = (
                    "" if _is_sap_guid(raw_id) else raw_id
                ) or inc.get("integration_flow_name") or ""
        return {"incidents": incidents, "total": len(incidents)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/autonomous/incidents/{incident_id}")
async def get_incident(incident_id: str):
    try:
        incident = _resolve_incident(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        return incident
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/autonomous/incidents/{incident_id}/view_model")
async def get_incident_view_model(incident_id: str):
    _guard()
    try:
        incident = _resolve_incident(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        return await orchestrator.build_incident_view_model(incident)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/autonomous/incidents/{incident_id}/fix_progress")
async def get_fix_progress_endpoint(incident_id: str):
    progress = get_fix_progress(incident_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="No fix progress found for this incident")
    return progress


@app.post("/autonomous/incidents/{incident_id}/approve")
async def approve_fix(
    incident_id: str,
    req: ApprovalRequest,
    background_tasks: BackgroundTasks,
):
    _guard()
    incident = _resolve_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    resolved_id = incident["incident_id"]
    if incident.get("status") not in ("AWAITING_APPROVAL", "RCA_COMPLETE"):
        raise HTTPException(
            status_code=400,
            detail=f"Incident status '{incident.get('status')}' is not approvable",
        )
    if req.approved:
        update_incident(resolved_id, {"status": "FIX_IN_PROGRESS"})
        background_tasks.add_task(_apply_fix_background, resolved_id, dict(incident))
        return {"status": "fix_started", "incident_id": resolved_id,
                "message_guid": incident.get("message_guid")}
    update_incident(resolved_id, {"status": "REJECTED", "comment": req.comment or "Rejected by user"})
    return {"status": "rejected", "incident_id": resolved_id,
            "message_guid": incident.get("message_guid")}


@app.post("/autonomous/incidents/{incident_id}/generate_fix")
async def generate_fix_for_incident(
    incident_id: str,
    background_tasks: BackgroundTasks,
    sync: bool = False,
):
    _guard()
    incident = _resolve_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.get("status") not in (
        "AWAITING_APPROVAL", "RCA_COMPLETE",
        "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Incident status '{incident.get('status')}' cannot generate a fix right now",
        )
    resolved_id = incident["incident_id"]
    if sync:
        try:
            return await orchestrator.execute_incident_fix(dict(incident), human_approved=True)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
    update_incident(resolved_id, {"status": "FIX_IN_PROGRESS"})
    background_tasks.add_task(_apply_fix_background, resolved_id, dict(incident))
    return {"status": "fix_started", "message": "AI fix flow started in background",
            "incident_id": resolved_id, "message_guid": incident.get("message_guid")}


async def _apply_fix_background(incident_id: str, incident: Dict):
    try:
        await orchestrator.execute_incident_fix(dict(incident), human_approved=True)
    except Exception as exc:
        logger.error("[_apply_fix_background] %s", exc)
        update_incident(incident_id, {"status": "FIX_FAILED", "fix_summary": str(exc)})


@app.post("/autonomous/incidents/{incident_id}/retry_rca")
async def retry_rca(incident_id: str, background_tasks: BackgroundTasks):
    _guard()
    incident = _resolve_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.get("status") in {
        "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED", "REJECTED", "TICKET_CREATED",
    }:
        raise HTTPException(
            status_code=400, detail=f"Status '{incident.get('status')}' cannot be retried"
        )
    resolved_id = incident["incident_id"]
    update_incident(resolved_id, {"status": "RCA_IN_PROGRESS"})
    background_tasks.add_task(_retry_rca_background, resolved_id, dict(incident))
    return {"status": "rca_started", "incident_id": resolved_id}


async def _retry_rca_background(incident_id: str, incident: Dict):
    try:
        rca = await orchestrator._rca.run_rca(incident)
        update_incident(incident_id, {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
        })
        await orchestrator.remediation_gate(dict(incident), rca)
    except Exception as exc:
        logger.error("[_retry_rca_background] %s", exc)
        update_incident(incident_id, {"status": "RCA_FAILED", "root_cause": str(exc)})


@app.get("/autonomous/incidents/{incident_id}/fix_patterns")
async def get_fix_patterns_endpoint(incident_id: str):
    incident = _resolve_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    clf = ClassifierAgent()
    sig     = clf.error_signature(
        incident.get("iflow_id", ""),
        incident.get("error_type", ""),
        incident.get("error_message", ""),
    )
    patterns = get_similar_patterns(sig)
    return {"patterns": patterns, "signature": sig}


@app.get("/autonomous/pending_approvals")
async def list_pending_approvals():
    try:
        pending = get_pending_approvals()
        for inc in pending:
            inc["approval_ref"]      = inc.get("incident_id")
            inc["message_guid_ref"]  = inc.get("message_guid")
        return {"pending": pending}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


_CRITICAL_ERROR_TYPES = frozenset({
    "SSL_ERROR", "BACKEND_ERROR", "SFTP_ERROR", "DUPLICATE_ERROR",
    "PAYLOAD_SIZE_ERROR", "IDOC_ERROR", "RESOURCE_ERROR",
})


def _auto_create_ticket(
    incident: Dict[str, Any],
    fix_result: Dict[str, Any],
    fix_status: str,
    incident_id: str,
) -> None:
    """Create an EM_ESCALATION_TICKETS row after a fix failure, idempotently."""
    try:
        existing = get_escalation_tickets(incident_id=incident_id, limit=1)
        if existing:
            return
        error_type = (incident.get("error_type") or "UNKNOWN").upper()
        priority   = "HIGH" if error_type in _CRITICAL_ERROR_TYPES else "MEDIUM"
        iflow_id   = incident.get("iflow_id") or incident.get("integration_flow_name") or ""
        description = (
            f"iFlow: {iflow_id}\n"
            f"Error: {(incident.get('error_message') or '')[:500]}\n"
            f"Root cause: {(incident.get('root_cause') or '')[:500]}\n"
            f"Proposed fix: {(incident.get('proposed_fix') or '')[:500]}\n"
            f"Fix attempted: {(fix_result.get('summary') or '')[:500]}"
        )
        ticket_data: Dict[str, Any] = {
            "incident_id": incident_id,
            "iflow_id":    iflow_id,
            "error_type":  error_type,
            "title":       f"Fix Failed: {iflow_id} — {error_type}",
            "description": description,
            "priority":    priority,
            "status":      "OPEN",
            "assigned_to": None,
            "created_at":  get_hana_timestamp(),
        }
        ticket_id = create_escalation_ticket(ticket_data)
        update_incident(incident_id, {"ticket_id": ticket_id, "auto_escalated": 1, "status": "TICKET_CREATED"})
        logger.info(
            "[AutoTicket] Created ticket %s for incident %s (status=%s priority=%s)",
            ticket_id, incident_id, fix_status, priority,
        )
    except Exception as exc:
        logger.error("[AutoTicket] Failed to create ticket for incident %s: %s", incident_id, exc)


@app.get("/autonomous/tickets")
async def list_escalation_tickets(status: Optional[str] = None, limit: int = 50):
    try:
        tickets = get_escalation_tickets(status=status, limit=limit)
        return {"tickets": tickets}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.patch("/autonomous/tickets/{ticket_id}")
async def update_ticket_status(ticket_id: str, body: Dict[str, Any]):
    """Transition ticket status: OPEN → IN_PROGRESS → RESOLVED."""
    ticket = get_escalation_ticket_by_id(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    allowed_fields = {"status", "assigned_to", "resolution_notes"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    if "status" in updates:
        valid_transitions: Dict[str, set] = {
            "OPEN":        {"IN_PROGRESS"},
            "IN_PROGRESS": {"RESOLVED", "OPEN"},
            "RESOLVED":    set(),
        }
        current    = (ticket.get("status") or "OPEN").upper()
        new_status = updates["status"].upper()
        if new_status not in valid_transitions.get(current, set()):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status transition: {current} → {new_status}",
            )
        updates["status"] = new_status
        if new_status == "RESOLVED":
            updates["resolved_at"] = get_hana_timestamp()
    try:
        update_escalation_ticket(ticket_id, updates)
        updated = get_escalation_ticket_by_id(ticket_id)
        return {"ticket": updated}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────
# MANUAL TRIGGER + TEST INCIDENT
# ─────────────────────────────────────────────


@app.post("/autonomous/test_incident")
async def inject_test_incident(background_tasks: BackgroundTasks):
    _guard()
    incident_id = str(uuid.uuid4())
    incident    = {
        "incident_id":    incident_id,
        "message_guid":   "TEST-" + incident_id[:8],
        "iflow_id":       "EH8-BPP-Material-UPSERT",
        "sender":         "S4HANA",
        "receiver":       "BPP",
        "status":         "DETECTED",
        "error_type":     "MAPPING_ERROR",
        "error_message":  "MappingException: Field 'NetPrice' does not exist in target structure.",
        "correlation_id": "COR-TEST-001",
        "log_start":      get_hana_timestamp(),
        "log_end":        get_hana_timestamp(),
        "created_at":     get_hana_timestamp(),
        "tags":           ["mapping", "schema"],
    }
    create_incident(incident)

    async def run_pipeline():
        update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
        rca = await orchestrator._rca.run_rca(incident)
        update_incident(incident_id, {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
        })
        await orchestrator.remediation_gate(dict(incident), rca)

    background_tasks.add_task(run_pipeline)
    return {"status": "test_incident_created", "incident_id": incident_id}


# ─────────────────────────────────────────────
# EVENT MESH STATUS
# ─────────────────────────────────────────────

@app.get("/event-mesh/status")
@app.get("/aem/status")
async def event_mesh_status():
    """Return SAP Event Mesh connectivity info and pipeline stage counts."""
    queue_name = os.getenv("EVENT_MESH_QUEUE", "default/sierra.automation/1/autofix/orbit/orchestrator")

    total_incidents      = count_all_incidents()
    stage_counts         = get_stage_counts()
    webhook_events_count = _agent_webhook_count()

    return {
        "event_mesh_queue":      queue_name,
        "delivery_mode":         "webhook_push",
        "webhook_active":        True,
        "event_mesh_enabled":    True,
        "messages_retrieved":    _webhook_counter,
        "webhook_events_count":  webhook_events_count,
        "queue_depth":           0,
        "stage_counts":          stage_counts,
        "total_incidents":       total_incidents,
    }


# ─────────────────────────────────────────────
# EVENT MESH WEBHOOK
# ─────────────────────────────────────────────

@app.post("/event-mesh/events")
@app.post("/aem/events")
async def event_mesh_webhook(event: Dict[str, Any]):
    """
    SAP Event Mesh webhook push endpoint.

    SAP Event Mesh posts messages from queue default/sierra.automation/1/autofix/orbit/orchestrator
    here via HTTPS POST.  The body is JSON — the multimap envelope produced
    by the iFlow's XML-to-JSON converter step.

    The message is handed directly to orchestrator._route_stage() which
    dispatches based on the 'stage' field (defaults to 'observed' for new
    CPI error events).
    """
    global _webhook_counter
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")

    set_db_source("EVENT_MESH")   # inherited by the create_task below
    _webhook_counter += 1
    _ts = get_hana_timestamp()

    # Extract a human-readable identifier from the multimap envelope if present
    try:
        messages = event.get("multimap:Messages", {})
        first_msg = next(iter(messages.values()), [{}])
        first_item = first_msg[0] if isinstance(first_msg, list) else first_msg
        logs_block = first_item.get("MessageProcessingLogs", first_item)
        _guid      = logs_block.get("MessageGuid", "")
        _iflow     = logs_block.get("IntegrationFlowName", "")
    except Exception:
        _guid, _iflow = "", ""

    _log_line = (
        f"[{_ts}] count={_webhook_counter}"
        f" guid={_guid or 'n/a'}"
        f" iflow={_iflow or 'n/a'}\n"
    )
    with open(_WEBHOOK_LOG_PATH, "a", encoding="utf-8") as _lf:
        _lf.write(_log_line)

    asyncio.create_task(orchestrator._route_stage(event))
    logger.info("[EventMesh] Webhook received #%d, dispatched to _route_stage", _webhook_counter)
    return {"status": "accepted", "count": _webhook_counter}


# ─────────────────────────────────────────────
# AGENT-TO-AGENT WEBHOOK ENDPOINTS
#
# Strict linear event-driven pipeline:
#
#   /agents/orchestrator  → classify + create incident → publish observer
#   /agents/observer      → enrich metadata            → publish rca
#   /agents/rca           → root cause analysis        → publish fixer
#   /agents/fixer         → apply fix + deploy         → publish verifier
#   /agents/verifier      → verify fix worked          → (terminal)
#
# Each endpoint returns {"status": "accepted"} immediately and runs the
# agent logic inside asyncio.create_task so the HTTP response is not blocked.
# On failure the incident DB status is set to a FAIL variant; no publish occurs.
# ─────────────────────────────────────────────


# ── Stage 1: Orchestrator ────────────────────────────────────────────────────

async def _run_orchestrator_task(event: Dict[str, Any]) -> None:
    """
    Classify the raw CPI error, apply dedup rules, create a new incident,
    then publish to the observer queue.  Does NOT run RCA, fix, or verify.
    """
    try:
        set_db_source("EVENT_MESH")

        # Normalize the raw AEM/iFlow message envelope
        normalized = orchestrator._normalize_aem_message(event)  # type: ignore[union-attr]
        _error_msg = normalized.get("error_message", "")
        _iflow_id  = normalized.get("iflow_id", "")

        # Rule-based classify, LLM fallback when confidence is low
        clf = orchestrator._classifier.classify_error(_error_msg)  # type: ignore[union-attr]
        if clf.get("confidence", 0.0) < 0.70:
            try:
                llm_clf = await orchestrator._classifier.classify_with_llm(  # type: ignore[union-attr]
                    _error_msg, _iflow_id
                )
                if llm_clf and llm_clf.get("confidence", 0.0) > clf.get("confidence", 0.0):
                    clf = llm_clf
            except Exception as _clf_exc:
                logger.warning("[Agents/orchestrator] LLM classify fallback skipped: %s", _clf_exc)

        normalized.update(clf)

        # Signature dedup — skip when iflow_id is blank/placeholder
        _iflow_for_dedup = normalized.get("iflow_id", "")
        _skip_sig_dedup  = _iflow_for_dedup.lower() in ("", "unknown_iflow", "unknown", "n/a")
        existing_sig = (
            None if _skip_sig_dedup
            else get_open_incident_by_signature(_iflow_for_dedup, normalized.get("error_type", ""))
        )
        if existing_sig:
            increment_incident_occurrence(
                existing_sig["incident_id"],
                message_guid=normalized.get("message_guid") or None,
                last_seen=get_hana_timestamp(),
            )
            logger.info(
                "[Agents/orchestrator] Signature dedup → correlated into incident=%s",
                existing_sig["incident_id"],
            )
            return

        # Burst dedup — absorb rapid repeat errors within the dedup window
        _group_key = orchestrator.incident_group_key(normalized)  # type: ignore[union-attr]
        _recent    = get_recent_incident_by_group_key(_group_key, within_seconds=BURST_DEDUP_WINDOW_SECONDS)
        if _recent:
            increment_incident_occurrence(
                _recent["incident_id"],
                message_guid=normalized.get("message_guid") or None,
                last_seen=get_hana_timestamp(),
            )
            logger.info(
                "[Agents/orchestrator] Burst dedup → absorbed into incident=%s (group=%s)",
                _recent["incident_id"], _group_key,
            )
            return

        # Create new incident with CLASSIFIED status
        incident_id = str(uuid.uuid4())
        create_incident({
            **normalized,
            "incident_id":          incident_id,
            "status":               "CLASSIFIED",
            "created_at":           get_hana_timestamp(),
            "incident_group_key":   _group_key,
            "occurrence_count":     1,
            "last_seen":            get_hana_timestamp(),
            "verification_status":  "UNVERIFIED",
            "consecutive_failures": 0,
            "auto_escalated":       0,
        })
        logger.info(
            "[Agents/orchestrator] Created incident=%s iflow=%s error_type=%s confidence=%.2f",
            incident_id, _iflow_id, clf.get("error_type", ""), clf.get("confidence", 0.0),
        )

        # Hand off to observer — publish to observer queue topic
        await event_bus.publish_to_next(event_bus.make_topic("observer"), {
            "stage": "observer", "incident_id": incident_id,
        })
    except Exception as exc:
        logger.error("[Agents/orchestrator] Task failed: %s", exc)


@app.post("/agents/orchestrator")
async def agent_orchestrator_webhook(request: Request, event: Dict[str, Any]):
    """
    Receives raw CPI error events from SAP Event Mesh.
    Classifies the error, applies dedup rules, creates the incident,
    then publishes to the observer queue.
    """
    body_bytes = await request.body()
    logger.info(f"[Agents/orchestrator] RAW PAYLOAD: {body_bytes.decode('utf-8', errors='replace')[:500]}")
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")
    _record_agent_webhook()
    asyncio.create_task(_run_orchestrator_task(event))
    logger.info("[Agents/orchestrator] Webhook received, dispatched")
    return {"status": "accepted"}


# ── Stage 2: Observer ────────────────────────────────────────────────────────

async def _run_observer_task(event: Dict[str, Any]) -> None:
    """
    Enrich the incident with OData metadata (sender, receiver, log timestamps),
    then publish to the RCA queue.  Does NOT run RCA, fix, or verify.
    """
    incident_id: str = event.get("incident_id", "")
    try:
        set_db_source("EVENT_MESH")
        incident = get_incident_by_id(incident_id)
        if not incident:
            logger.error("[Agents/observer] Incident %s not found in DB", incident_id)
            return

        update_incident(incident_id, {"status": "OBSERVED"})

        # Enrich with MessageProcessingLogs metadata from SAP OData
        guid = incident.get("message_guid", "")
        if guid and observer and observer.error_fetcher:
            try:
                meta = await observer.error_fetcher.fetch_message_metadata(guid)
                enrichments: Dict[str, Any] = {}
                if meta.get("IntegrationFlowName"):
                    enrichments["iflow_id"]  = meta["IntegrationFlowName"]
                for src, dst in (("Sender", "sender"), ("Receiver", "receiver"),
                                 ("LogStart", "log_start"), ("LogEnd", "log_end")):
                    if meta.get(src):
                        enrichments[dst] = meta[src]
                if enrichments:
                    update_incident(incident_id, enrichments)
                    logger.info(
                        "[Agents/observer] Enriched incident=%s fields=%s",
                        incident_id, list(enrichments.keys()),
                    )
            except Exception as _meta_exc:
                logger.warning(
                    "[Agents/observer] OData metadata fetch failed incident=%s: %s",
                    incident_id, _meta_exc,
                )

        # Hand off to RCA — publish to rca queue topic
        await event_bus.publish_to_next(event_bus.make_topic("rca"), {
            "stage": "rca", "incident_id": incident_id,
        })
    except Exception as exc:
        logger.error("[Agents/observer] Task failed incident=%s: %s", incident_id, exc)
        if incident_id:
            try:
                update_incident(incident_id, {"status": "OBS_FAILED", "error_message": str(exc)[:500]})
            except Exception:
                pass


@app.post("/agents/observer")
async def agent_observer_webhook(event: Dict[str, Any]):
    """
    Receives enrichment requests from the observer queue.
    Fetches OData metadata, updates the incident, then publishes to the RCA queue.
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")
    _record_agent_webhook()
    asyncio.create_task(_run_observer_task(event))
    logger.info("[Agents/observer] Webhook received incident=%s, dispatched", event.get("incident_id"))
    return {"status": "accepted"}


# ── Stage 3: RCA ─────────────────────────────────────────────────────────────

async def _run_rca_task(event: Dict[str, Any]) -> None:
    """
    Run root cause analysis on the enriched incident, persist results,
    then publish to the fixer queue.  Does NOT apply the fix or verify.
    """
    incident_id: str = event.get("incident_id", "")
    try:
        set_db_source("EVENT_MESH")
        incident = get_incident_by_id(incident_id)
        if not incident:
            logger.error("[Agents/rca] Incident %s not found in DB", incident_id)
            return

        update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
        rca = await orchestrator._rca.run_rca(dict(incident))  # type: ignore[union-attr]

        update_incident(incident_id, {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
            "error_type":         rca.get("error_type") or incident.get("error_type", ""),
        })
        logger.info(
            "[Agents/rca] RCA complete incident=%s confidence=%.2f",
            incident_id, rca.get("confidence", 0.0),
        )

        # Hand off to fixer — publish to fixer queue topic
        await event_bus.publish_to_next(event_bus.make_topic("fixer"), {
            "stage": "fixer", "incident_id": incident_id,
        })
    except Exception as exc:
        logger.error("[Agents/rca] Task failed incident=%s: %s", incident_id, exc)
        if incident_id:
            try:
                update_incident(incident_id, {"status": "RCA_FAILED", "error_message": str(exc)[:500]})
            except Exception:
                pass


@app.post("/agents/rca")
async def agent_rca_webhook(event: Dict[str, Any]):
    """
    Receives RCA requests from the rca queue.
    Runs root cause analysis, persists results, then publishes to the fixer queue.
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")
    _record_agent_webhook()
    asyncio.create_task(_run_rca_task(event))
    logger.info("[Agents/rca] Webhook received incident=%s, dispatched", event.get("incident_id"))
    return {"status": "accepted"}


# ── Stage 4: Fixer ───────────────────────────────────────────────────────────

async def _run_fixer_task(event: Dict[str, Any]) -> None:
    """
    Apply the proposed fix (update iFlow XML, deploy), persist the result,
    then publish to the verifier queue — but ONLY if the fix was successfully applied.
    Does NOT run the verifier inline.
    """
    incident_id: str = event.get("incident_id", "")
    try:
        set_db_source("EVENT_MESH")
        incident = get_incident_by_id(incident_id)
        if not incident:
            logger.error("[Agents/fixer] Incident %s not found in DB", incident_id)
            return

        rca = {
            "root_cause":         incident.get("root_cause", ""),
            "proposed_fix":       incident.get("proposed_fix", ""),
            "confidence":         incident.get("rca_confidence", 0.0),
            "error_type":         incident.get("error_type", ""),
            "affected_component": incident.get("affected_component", ""),
        }

        update_incident(incident_id, {"status": "FIX_IN_PROGRESS"})
        fix_result  = await orchestrator._fix.apply_fix(dict(incident), rca)  # type: ignore[union-attr]
        fix_success = fix_result.get("fix_applied", False) and fix_result.get("deploy_success", False)
        fix_status  = FixAgent.determine_post_fix_status(
            fix_success=fix_success,
            policy={"action": "AUTO_FIX"},
            failed_stage=fix_result.get("failed_stage", ""),
        )
        update_incident(incident_id, {
            "status":      fix_status,
            "fix_summary": fix_result.get("summary", ""),
            "fix_applied": 1 if fix_success else 0,
        })
        logger.info("[Agents/fixer] Fix complete incident=%s status=%s", incident_id, fix_status)

        if not fix_success:
            logger.warning(
                "[Agents/fixer] Fix not applied for incident=%s (stage=%s) — halting pipeline",
                incident_id, fix_result.get("failed_stage", ""),
            )
            _auto_create_ticket(incident, fix_result, fix_status, incident_id)
            return

        # Hand off to verifier — publish to verifier queue topic
        await event_bus.publish_to_next(event_bus.make_topic("verifier"), {
            "stage": "verifier", "incident_id": incident_id,
        })
    except Exception as exc:
        logger.error("[Agents/fixer] Task failed incident=%s: %s", incident_id, exc)
        if incident_id:
            try:
                update_incident(incident_id, {"status": "FIX_FAILED", "error_message": str(exc)[:500]})
                _inc = get_incident_by_id(incident_id)
                if _inc:
                    _auto_create_ticket(_inc, {"summary": str(exc)[:500]}, "FIX_FAILED", incident_id)
            except Exception:
                pass


@app.post("/agents/fixer")
async def agent_fixer_webhook(event: Dict[str, Any]):
    """
    Receives fix requests from the fixer queue.
    Applies the proposed fix, deploys the iFlow, then publishes to the verifier queue.
    """
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")
    _record_agent_webhook()
    asyncio.create_task(_run_fixer_task(event))
    logger.info("[Agents/fixer] Webhook received incident=%s, dispatched", event.get("incident_id"))
    return {"status": "accepted"}


# ── Stage 5: Verifier (terminal) ─────────────────────────────────────────────

async def _run_verifier_task(event: Dict[str, Any]) -> None:
    """
    Verify that the deployed fix actually resolved the error.
    Writes the final status (FIX_VERIFIED or FIX_FAILED_RUNTIME) to the DB.
    Terminal stage — no publish_to_next.
    """
    incident_id: str = event.get("incident_id", "")
    try:
        set_db_source("EVENT_MESH")
        incident = get_incident_by_id(incident_id)
        if not incident:
            logger.error("[Agents/verifier] Incident %s not found in DB", incident_id)
            return

        result      = await orchestrator._verifier.test_iflow_after_fix(dict(incident))  # type: ignore[union-attr]
        test_passed = result.get("test_passed", False) or result.get("success", False)
        final_status = "FIX_VERIFIED" if test_passed else "FIX_FAILED_RUNTIME"
        update_incident(incident_id, {
            "status":              final_status,
            "verification_status": "VERIFIED" if test_passed else "FAILED",
            "fix_summary":         result.get("summary", ""),
            "resolved_at":         get_hana_timestamp() if test_passed else None,
        })
        logger.info(
            "[Agents/verifier] Verification complete incident=%s final_status=%s",
            incident_id, final_status,
        )
    except Exception as exc:
        logger.error("[Agents/verifier] Task failed incident=%s: %s", incident_id, exc)
        if incident_id:
            try:
                update_incident(incident_id, {"status": "FIX_FAILED_RUNTIME", "error_message": str(exc)[:500]})
            except Exception:
                pass


@app.post("/agents/verifier")
async def agent_verifier_webhook(request: Request, event: Dict[str, Any]):
    """
    Receives verification requests from the verifier queue.
    Tests the deployed iFlow and writes the final status to the DB.
    """
    body_bytes = await request.body()
    logger.info("[Agents/verifier] RAW PAYLOAD: %s", body_bytes.decode("utf-8", errors="replace")[:500])

    # Fallback: if FastAPI body-parsing silently produced an empty dict (e.g. AEM
    # delivered with a non-application/json Content-Type), re-parse from raw bytes.
    if not event and body_bytes:
        try:
            event = json.loads(body_bytes)
        except Exception:
            pass

    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not ready")
    incident_id = event.get("incident_id", "")
    _record_agent_webhook()
    asyncio.create_task(_run_verifier_task(event))
    logger.info("[Agents/verifier] Webhook received incident=%s, dispatched", incident_id)
    return {"status": "accepted"}


@app.options("/agents/{agent_name}")
async def agents_options_preflight(agent_name: str):
    """
    Handles OPTIONS preflight from SAP Event Mesh webhook subscription validation.
    CORS middleware only intercepts requests that carry an Origin header; AEM sends
    plain OPTIONS without one, so a dedicated handler is required.
    """
    return JSONResponse(status_code=200, content={"status": "ok"})


# ─────────────────────────────────────────────
# DEBUG
# ─────────────────────────────────────────────

@app.get("/autonomous/db_test")
async def db_test():
    import traceback  # noqa: PLC0415
    test_id = str(uuid.uuid4())
    try:
        create_incident({
            "incident_id":   test_id,
            "message_guid":  "TEST-DB",
            "iflow_id":      "TEST-IFLOW",
            "status":        "DETECTED",
            "error_type":    "MAPPING_ERROR",
            "error_message": "test error",
            "created_at":    get_hana_timestamp(),
            "tags":          [],
        })
        fetched = get_incident_by_id(test_id)
        return {"create": "OK" if fetched else "FAILED", "fetch": fetched,
                "total": len(get_all_incidents())}
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}


@app.get("/autonomous/debug")
async def autonomous_debug():
    results: Dict[str, Any] = {
        "env_vars": {
            "SAP_HUB_TENANT_URL":    os.getenv("SAP_HUB_TENANT_URL", "NOT SET"),
            "SAP_HUB_TOKEN_URL":     os.getenv("SAP_HUB_TOKEN_URL",  "NOT SET"),
            "SAP_HUB_CLIENT_ID":     "SET" if os.getenv("SAP_HUB_CLIENT_ID")     else "NOT SET",
            "SAP_HUB_CLIENT_SECRET": "SET" if os.getenv("SAP_HUB_CLIENT_SECRET") else "NOT SET",
        },
        "webhook_mode": True,
        "auto_fix_all":       AUTO_FIX_ALL_CPI_ERRORS,
        "auto_deploy":        AUTO_DEPLOY_AFTER_FIX,
        "fetch_test":  None,
        "fetch_error": None,
    }
    if observer:
        try:
            errors = await observer.error_fetcher.fetch_failed_messages()
            results["fetch_test"] = f"SUCCESS — {len(errors)} messages"
        except Exception as exc:
            results["fetch_error"] = str(exc)
    return results


@app.get("/autonomous/debug2")
async def autonomous_debug2():
    results: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.post(
                os.getenv("SAP_HUB_TOKEN_URL"),
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     os.getenv("SAP_HUB_CLIENT_ID"),
                    "client_secret": os.getenv("SAP_HUB_CLIENT_SECRET"),
                },
            )
            results["token_status"] = resp.status_code
            if resp.status_code != 200:
                results["token_error"] = resp.text
                return results
            token = resp.json()["access_token"]
            results["token"] = "OK"
    except Exception as exc:
        results["token_exception"] = str(exc)
        return results
    try:
        base   = os.getenv("SAP_HUB_TENANT_URL", "").rstrip("/")
        params = {
            "$filter": "Status eq 'FAILED'", "$orderby": "LogEnd desc",
            "$top": "5", "$format": "json",
        }
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.get(
                f"{base}/api/v1/MessageProcessingLogs",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            results["api_status"]           = resp.status_code
            results["api_response_preview"] = resp.text[:500]
    except Exception as exc:
        results["api_exception"] = str(exc)
    return results


# ─────────────────────────────────────────────
# CPI MONITOR DEBUG / VERIFICATION
# ─────────────────────────────────────────────

@app.get("/cpi-monitor/status")
async def cpi_monitor_status():
    """
    Show the current configuration and dedup-cache state of the CPI monitor.
    Use this first to confirm env vars and destination names are set correctly.
    """
    from cpi_monitor.cpi_poller import (
        _POLL_INTERVAL, _API_BASE_URL, _API_TOKEN_URL,
        _API_CLIENT_ID, _API_CLIENT_SECRET,
    )
    from cpi_monitor.error_publisher import _EM_DESTINATION_NAME, _published, _em_cache

    return {
        "cpi_poll": {
            "auth_mode":           "direct env vars (SAP_HUB_* / API_OAUTH_*)",
            "poll_interval_secs":  _POLL_INTERVAL,
            "api_base_url":        _API_BASE_URL or "NOT SET",
            "api_token_url":       _API_TOKEN_URL or "NOT SET",
            "api_client_id":       "SET" if _API_CLIENT_ID else "NOT SET",
            "api_client_secret":   "SET" if _API_CLIENT_SECRET else "NOT SET",
        },
        "event_mesh_publish": {
            "destination_name":    _EM_DESTINATION_NAME,
            "publish_url":         os.getenv("AEM_REST_URL", "NOT SET"),
            "cached_token":        "present" if _em_cache.get("token") else "not resolved yet",
        },
        "dedup_cache": {
            "tracked_guids":       len(_published),
            "guids":               list(_published.keys()),
        },
        "vcap_services_present":   bool(os.getenv("VCAP_SERVICES")),
    }


@app.post("/cpi-monitor/trigger")
async def cpi_monitor_trigger():
    """
    Manually run one CPI poll + publish cycle immediately.
    Use this to verify the integration without waiting 10 minutes.
    Returns what was found in CPI and what was published to Event Mesh.
    """
    from cpi_monitor.cpi_poller import poll_failed_messages
    from cpi_monitor.error_publisher import publish_failed_messages, _published

    result: Dict[str, Any] = {
        "polled_messages": [],
        "published_count": 0,
        "skipped_duplicates": 0,
        "errors": [],
    }

    try:
        messages = await poll_failed_messages()
        result["polled_messages"] = [
            {
                "MessageGuid":         m.get("MessageGuid"),
                "IntegrationFlowName": m.get("IntegrationFlowName"),
                "Status":              m.get("Status"),
                "LogEnd":              m.get("LogEnd"),
            }
            for m in messages
        ]

        before = set(_published.keys())
        if messages:
            await publish_failed_messages(messages)
        after = set(_published.keys())

        newly_published = after - before
        result["published_count"]    = len(newly_published)
        result["skipped_duplicates"] = len(messages) - len(newly_published)
        result["published_guids"]    = list(newly_published)

    except Exception as exc:
        result["errors"].append(str(exc))
        logger.error("[CPI_MONITOR] /cpi-monitor/trigger error: %s", exc)

    return result


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn  # noqa: PLC0415
    _port = int(os.getenv("PORT", "8080"))   # BTP CF injects $PORT; 8080 for local dev
    _dev  = os.getenv("APP_ENV", "production").lower() == "development"
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=_port,
        reload=_dev,
        reload_excludes=["*.log", "*.db", "logs/*", "__pycache__"] if _dev else [],
        log_level="info",
    )

# run this code using UV run