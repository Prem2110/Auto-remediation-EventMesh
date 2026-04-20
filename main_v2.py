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
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CORE IMPORTS
# ─────────────────────────────────────────────
from core.constants import (
    AUTO_DEPLOY_AFTER_FIX,
    AUTO_FIX_ALL_CPI_ERRORS,
    AUTO_FIX_CONFIDENCE,
    AUTONOMOUS_ENABLED,
    FAILED_MESSAGE_FETCH_LIMIT,
    FIX_INTENT_KEYWORDS,
    POLL_INTERVAL_SECONDS,
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

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
from db.database import (
    create_incident,
    get_all_history,
    get_all_incidents,
    get_incident_by_id,
    get_incident_by_message_guid,
    get_pending_approvals,
    get_similar_patterns,
    get_testsuite_log_entries,
    get_xsd_files_by_session,
    create_query_history,
    update_query_history,
    update_incident,
    ensure_autonomous_incident_schema,
    ensure_escalation_tickets_schema,
    ensure_fix_patterns_schema,
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
# LIFESPAN
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp, observer, orchestrator

    # Synchronous schema setup
    ensure_autonomous_incident_schema()
    ensure_fix_patterns_schema()
    ensure_escalation_tickets_schema()

    # Create the MCP infrastructure
    mcp = MultiMCP()

    # Create all specialist agents (not yet wired)
    _rca      = RCAAgent(mcp)
    _fix      = FixAgent(mcp)
    _verifier = VerifierAgent(mcp)
    observer  = ObserverAgent(mcp)

    # Create orchestrator with all specialist references
    orchestrator = OrchestratorAgent(mcp, _rca, _fix, _verifier)

    # Wire observer → orchestrator (late injection to avoid circular import)
    observer.set_orchestrator(orchestrator)

    # Wire error_fetcher → fix agent (shared token cache)
    _fix.set_error_fetcher(observer.error_fetcher)

    async def _init_background():
        try:
            logger.info("[Startup] Initialising MCP servers in background…")
            await mcp.connect()
            await mcp.discover_tools()
            await mcp.build_agent()                  # full-toolset shared agent
            await _rca.build_agent()                 # RCA-filtered agent
            await _fix.build_agent()                 # fix/deploy-filtered agent
            await _verifier.build_agent()            # test/replay-filtered agent
            if AUTONOMOUS_ENABLED:
                observer.start()
                logger.info("[Startup] Autonomous monitoring auto-started.")
            logger.info("[Startup] All agents ready.")
        except Exception as exc:
            logger.error("[Startup] Agent initialisation failed: %s", exc)

    asyncio.create_task(_init_background())
    logger.info("[Startup] FastAPI ready — agents initialising in background.")
    yield

    if observer:
        observer.stop()


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
    if mcp is None or orchestrator is None:
        raise HTTPException(status_code=503, detail="Agents not ready — MCP still initialising.")


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
            pending  = get_all_incidents(status="AWAITING_APPROVAL", limit=5)
            rca_done = get_all_incidents(status="RCA_COMPLETE", limit=5)
            candidates = pending + rca_done

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

@app.post("/autonomous/start")
async def start_autonomous():
    _guard()
    started = observer.start()
    return {"status": "started" if started else "already_running",
            "poll_interval_seconds": POLL_INTERVAL_SECONDS}


@app.post("/autonomous/stop")
async def stop_autonomous():
    _guard()
    stopped = observer.stop()
    return {"status": "stopped" if stopped else "not_running"}


@app.get("/autonomous/status")
async def autonomous_status():
    _guard()
    return {
        "running":                observer.is_running,
        "poll_interval_seconds":  POLL_INTERVAL_SECONDS,
        "auto_fix_confidence":    AUTO_FIX_CONFIDENCE,
        "suggest_fix_confidence": SUGGEST_FIX_CONFIDENCE,
        "auto_fix_all":           AUTO_FIX_ALL_CPI_ERRORS,
        "auto_deploy":            AUTO_DEPLOY_AFTER_FIX,
    }


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
        for inc in incidents:
            if not inc.get("iflow_name"):
                inc["iflow_name"] = (
                    inc.get("iflow_id") or
                    inc.get("artifact_id") or
                    inc.get("integration_flow_name") or
                    ""
                )
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


# ─────────────────────────────────────────────
# MANUAL TRIGGER + TEST INCIDENT
# ─────────────────────────────────────────────

@app.post("/autonomous/manual_trigger")
async def manual_trigger(background_tasks: BackgroundTasks):
    _guard()

    async def one_shot():
        try:
            raw_errors   = await observer.error_fetcher.fetch_failed_messages(limit=FAILED_MESSAGE_FETCH_LIMIT)
            unique_errors = observer.dedupe_raw_failed_messages(raw_errors)
            for raw in unique_errors:
                guid = raw.get("MessageGuid", "")
                if not guid:
                    continue
                existing = get_incident_by_message_guid(guid)
                if existing and existing.get("status") not in {
                    "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED",
                    "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
                    "REJECTED", "TICKET_CREATED", "ARTIFACT_MISSING",
                }:
                    continue
                error_detail = await observer.error_fetcher.fetch_error_details(guid)
                normalized   = observer.error_fetcher.normalize(raw, error_detail)
                await orchestrator.process_detected_error(normalized)

            runtime_errors = await observer.error_fetcher.fetch_runtime_artifact_errors(
                limit=RUNTIME_ERROR_FETCH_LIMIT
            )
            for norm in runtime_errors:
                await orchestrator.process_detected_error(norm)
        except Exception as exc:
            logger.error("[manual_trigger] %s", exc)

    background_tasks.add_task(one_shot)
    return {"status": "triggered", "message": "One-shot poll started in background"}


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
# AEM WEBHOOK  (Pattern 2 entry point)
# ─────────────────────────────────────────────

@app.post("/aem/events")
async def aem_webhook(event: Dict[str, Any]):
    """
    SAP AEM REST Delivery webhook.
    The AEM queue consumer calls this endpoint with a JSON event body.
    The event is dispatched to all registered in-process handlers.
    """
    topic = event.get("topic", "")
    if not topic:
        raise HTTPException(status_code=400, detail="Event must include a 'topic' field")
    await event_bus.publish(topic, event)
    return {"status": "accepted", "topic": topic}


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
        "autonomous_running": observer.is_running if observer else False,
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
# ENTRYPOINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn  # noqa: PLC0415
    uvicorn.run(
        "main_v2:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        reload_excludes=["*.log", "*.db", "logs/*", "__pycache__"],
    )
