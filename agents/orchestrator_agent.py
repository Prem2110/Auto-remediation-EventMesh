"""
agents/orchestrator_agent.py
=============================
OrchestratorAgent — routes detected CPI errors through the full pipeline and
exposes the general chatbot interface (ask).

Pipeline:
  process_detected_error()
    → dedup / correlation check
    → run_rca (via RCAAgent)
    → remediation_gate()
        AUTO_FIX → apply_fix (via FixAgent) → verify (via VerifierAgent)
        RETRY    → retry_failed_message (via VerifierAgent)
        APPROVAL → set AWAITING_APPROVAL
        TICKET   → _create_external_ticket

  execute_incident_fix()
    → called by /execute-fix endpoint (human-triggered or approved)
    → iFlow existence pre-check → snapshot → RCA (if needed) → fix → verify

  ask()
    → general chatbot interface backed by the full MCP agent

Exports:
  OrchestratorAgent
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid

from typing import Any, Dict, List, Optional

from agents.base import StepLogger, TestExecutionTracker
from agents.classifier_agent import ClassifierAgent
from agents.fix_agent import FixAgent
from agents.rca_agent import RCAAgent
from agents.verifier_agent import VerifierAgent
from aem.event_bus import event_bus
from core.constants import (
    ACTION_HINTS,
    AUTO_FIX_ALL_CPI_ERRORS,
    AUTO_FIX_CONFIDENCE,
    BURST_DEDUP_WINDOW_SECONDS,
    CPI_IFLOW_GROOVY_RULES,
    MAX_CONSECUTIVE_FAILURES,
    PATTERN_MIN_SUCCESS_COUNT,
    REMEDIATION_POLICIES,
    SAP_DOC_TEMPLATE,
    SERVER_ROUTING_GUIDE,
    SUGGEST_FIX_CONFIDENCE,
    TRANSIENT_ERROR_MARKERS,
    _STATUS_ACTION_HINTS,
)
from core.state import FIX_PROGRESS, RUNTIME_FLAGS, cleanup_fix_progress
from db.database import (
    create_escalation_ticket,
    create_incident,
    get_incident_by_id,
    get_incident_by_message_guid,
    get_open_incident_by_signature,
    get_recent_incident_by_group_key,
    get_similar_patterns,
    increment_incident_occurrence,
    update_incident,
    upsert_fix_pattern,
)
from utils.utils import get_hana_timestamp

logger = logging.getLogger(__name__)

_SAP_TENANT          = os.getenv("SAP_HUB_TENANT_URL", "")
_TICKET_ASSIGNEE     = os.getenv("TICKET_DEFAULT_ASSIGNEE", "")

# AEM — pipeline uses HTTP webhook push; local queue buffers during agent init


class OrchestratorAgent:
    """
    Top-level coordinator that holds references to all specialist agents and
    routes each incident through the appropriate pipeline.
    """

    def __init__(self, mcp, rca: RCAAgent, fix: FixAgent, verifier: VerifierAgent):
        self._mcp        = mcp
        self._rca        = rca
        self._fix        = fix
        self._verifier   = verifier
        self._classifier = ClassifierAgent()
        self._observer   = None
        self._agent      = None
        self._agents_ready: bool = False  # set True after all specialist agents finish build_agent()
        # Per-iFlow mutex: prevents two concurrent fix pipelines writing to the same SAP CPI artifact
        self._iflow_fix_locks: Dict[str, asyncio.Lock] = {}

    def set_observer(self, observer) -> None:
        """Inject the ObserverAgent reference for the orchestrator LangChain agent."""
        self._observer = observer

    async def build_agent(self, observer=None) -> None:
        """
        Build the orchestrator LangChain agent.

        Each specialist agent is wrapped as a @tool so the orchestrator LLM
        can call them in sequence: Observer → Classifier → RCA → Fix → Verifier.
        """
        from langchain_core.tools import tool as _tool  # noqa: PLC0415
        import json as _json  # noqa: PLC0415

        if observer is not None:
            self._observer = observer

        _orch      = self
        _obs       = self._observer
        _clf       = self._classifier
        _rca       = self._rca
        _fix       = self._fix
        _verifier  = self._verifier

        def _ainvoke_or_method(agent_obj, method_name: str):
            """Return the agent's _agent.ainvoke if available, else fall back to the method."""
            lc_agent = getattr(agent_obj, "_agent", None)
            if lc_agent is not None:
                return lc_agent
            return None

        async def _call_agent(lc_agent, content: str) -> str:
            result    = await lc_agent.ainvoke(
                {"messages": [{"role": "user", "content": content}]}
            )
            final_msg = result["messages"][-1]
            return final_msg.content if hasattr(final_msg, "content") else str(final_msg)

        @_tool
        async def run_observer(task: str) -> str:
            """
            Consume the next failed message from the AEM observer queue and mark it for analysis.
            Pass a plain-text instruction describing what to monitor or check.
            """
            lc = _ainvoke_or_method(_obs, "")
            if lc:
                return await _call_agent(lc, task)
            return "WARNING: Observer agent not available."

        @_tool
        async def run_classifier(incident_json: str) -> str:
            """
            Classify a SAP CPI error. Pass the incident as a JSON string with
            keys: iflow_id, error_message, error_type (optional).
            Returns: {"error_type": "...", "confidence": 0.0, "tags": [...]}
            """
            lc = _ainvoke_or_method(_clf, "")
            if lc:
                return await _call_agent(lc, incident_json)
            # Fallback: rule-based
            try:
                inc = _json.loads(incident_json) if isinstance(incident_json, str) else {}
            except Exception:
                inc = {"error_message": incident_json}
            return str(_clf.classify_error(inc.get("error_message", "")))

        @_tool
        async def run_rca(incident_json: str) -> str:
            """
            Perform Root Cause Analysis on a SAP CPI incident.
            Pass the incident as a JSON string with keys:
            iflow_id, error_message, error_type, message_guid (optional).
            Returns JSON: {root_cause, proposed_fix, confidence, error_type, affected_component}
            """
            lc = _ainvoke_or_method(_rca, "")
            if lc:
                return await _call_agent(lc, incident_json)
            try:
                incident = _json.loads(incident_json) if isinstance(incident_json, str) else {}
            except Exception:
                incident = {"error_message": incident_json}
            result = await _rca.run_rca(incident)
            return _json.dumps(result)

        @_tool
        async def run_fix(rca_json: str) -> str:
            """
            Apply a fix and deploy the iFlow.
            Pass a JSON string with keys:
            iflow_id, error_message, proposed_fix, root_cause, error_type, affected_component.
            Returns JSON: {fix_applied, deploy_success, success, summary}
            """
            lc = _ainvoke_or_method(_fix, "")
            if lc:
                return await _call_agent(lc, rca_json)
            try:
                ctx = _json.loads(rca_json) if isinstance(rca_json, str) else {}
            except Exception:
                ctx = {}
            result = await _fix.apply_fix(ctx, ctx, lambda *a, **kw: None)
            return _json.dumps(result)

        @_tool
        async def run_verifier(fix_json: str) -> str:
            """
            Verify a deployed fix by checking runtime status and replaying the failed message.
            Pass a JSON string with keys:
            iflow_id, message_guid (optional), error_type, proposed_fix.
            Returns JSON: {test_passed, http_status, summary}
            """
            lc = _ainvoke_or_method(_verifier, "")
            if lc:
                return await _call_agent(lc, fix_json)
            try:
                incident = _json.loads(fix_json) if isinstance(fix_json, str) else {}
            except Exception:
                incident = {}
            result = await _verifier.test_iflow_after_fix(incident)
            return _json.dumps(result)

        system_prompt = """You orchestrate the SAP CPI self-healing pipeline.

Always follow this sequence for a new failure:
1. run_observer  — detect the latest failed messages
2. run_classifier — determine error_type and confidence
3. run_rca       — root cause analysis → proposed_fix
4. run_fix       — apply fix and deploy the iFlow
5. run_verifier  — confirm the fix worked

Rules:
- Pass the output of each step as input to the next.
- Stop and escalate if any agent returns failure or confidence < 0.6.
- For BACKEND_ERROR or SFTP_ERROR: skip run_fix, go straight to escalation.
- Always report the final status as JSON.
"""
        self._agent = await self._mcp.build_agent(
            tools=[run_observer, run_classifier, run_rca, run_fix, run_verifier],
            system_prompt=system_prompt,
        )
        self._agents_ready = True
        logger.info("[Orchestrator] LangChain agent ready (5 specialist @tool wrappers).")

    def _get_iflow_fix_lock(self, iflow_id: str) -> asyncio.Lock:
        if iflow_id not in self._iflow_fix_locks:
            self._iflow_fix_locks[iflow_id] = asyncio.Lock()
        return self._iflow_fix_locks[iflow_id]

    # ────────────────────────────────────────────
    # ROUTING / POLICY HELPERS
    # ────────────────────────────────────────────

    @staticmethod
    def is_transient_error(error_message: str) -> bool:
        msg = (error_message or "").lower()
        return any(m in msg for m in TRANSIENT_ERROR_MARKERS)

    def get_remediation_policy(self, incident: Dict, rca: Dict) -> Dict[str, Any]:
        error_type = rca.get("error_type") or incident.get("error_type") or "UNKNOWN_ERROR"
        policy     = dict(REMEDIATION_POLICIES.get(error_type, REMEDIATION_POLICIES["UNKNOWN_ERROR"]))
        if error_type in {"BACKEND_ERROR", "CONNECTIVITY_ERROR"} and self.is_transient_error(
            incident.get("error_message", "")
        ):
            policy["action"] = "RETRY"
        return policy

    @staticmethod
    def incident_group_key(incident: Dict) -> str:
        iflow_id   = incident.get("iflow_id", "")
        error_type = incident.get("error_type", "")
        affected   = incident.get("affected_component", "") or incident.get("receiver", "") or "unknown"
        return hashlib.md5(f"{iflow_id}|{error_type}|{affected}".encode()).hexdigest()[:20]

    @staticmethod
    def has_actionable_fix(rca: Dict) -> bool:
        return bool((rca.get("proposed_fix") or "").strip() and (rca.get("root_cause") or "").strip())

    def should_auto_fix(
        self, incident: Dict, rca: Dict, policy: Dict[str, Any], confidence: float
    ) -> bool:
        if not self.has_actionable_fix(rca):
            return False
        if not RUNTIME_FLAGS["auto_fix_enabled"]:
            return False
        if policy["action"] in {"AUTO_FIX", "RETRY"}:
            threshold = SUGGEST_FIX_CONFIDENCE if RUNTIME_FLAGS["auto_fix_enabled"] else AUTO_FIX_CONFIDENCE
            return confidence >= threshold
        return False

    # ────────────────────────────────────────────
    # PROGRESS TRACKING
    # ────────────────────────────────────────────

    def _set_progress(
        self,
        incident_id: str,
        step: str,
        step_index: int,
        total_steps: int,
        status: str = "FIX_IN_PROGRESS",
        **context: object,
    ) -> None:
        cleanup_fix_progress()
        entry = FIX_PROGRESS.get(incident_id, {"steps_done": [], "started_at": get_hana_timestamp()})
        if step_index > 1 and entry.get("current_step"):
            entry["steps_done"].append(entry["current_step"])
        entry.update({
            "status":       status,
            "current_step": step,
            "step_index":   step_index,
            "total_steps":  total_steps,
            "updated_at":   get_hana_timestamp(),
            "_updated_epoch": time.time(),
        })
        for k, v in context.items():
            if v is not None or k not in entry:
                entry[k] = v
        FIX_PROGRESS[incident_id] = entry

    # ────────────────────────────────────────────
    # TICKET CREATION
    # ────────────────────────────────────────────

    async def _create_external_ticket(
        self, incident: Dict, rca: Dict
    ) -> Optional[str]:
        try:
            occurrence = incident.get("occurrence_count", 1)
            confidence = rca.get("confidence", 0.0)
            priority   = (
                "CRITICAL" if occurrence >= 5 or confidence < 0.3
                else "HIGH"   if occurrence >= 3 or confidence < 0.5
                else "MEDIUM"
            )
            ticket_data = {
                "incident_id": incident.get("incident_id"),
                "iflow_id":    incident.get("iflow_id"),
                "error_type":  incident.get("error_type"),
                "title": (
                    f"[SAP CPI] Auto-remediation escalation: "
                    f"{incident.get('iflow_id', 'unknown')} — "
                    f"{incident.get('error_type', 'UNKNOWN_ERROR')}"
                ),
                "description": (
                    f"iFlow: {incident.get('iflow_id')}\n"
                    f"Error: {incident.get('error_message', '')[:500]}\n"
                    f"Root cause: {rca.get('root_cause', '')}\n"
                    f"Proposed fix: {rca.get('proposed_fix', '')}\n"
                    f"Incident ID: {incident.get('incident_id')}\n"
                    f"Occurrence count: {occurrence}\n"
                    f"RCA confidence: {confidence}"
                ),
                "priority":    priority,
                "status":      "OPEN",
                "assigned_to": _TICKET_ASSIGNEE or None,
            }
            ticket_id = create_escalation_ticket(ticket_data)
            logger.info(
                "[EscalationTicket] Ticket %s created for incident %s",
                ticket_id, incident.get("incident_id"),
            )
            return ticket_id
        except Exception as exc:
            logger.error("[EscalationTicket] Failed to create ticket: %s", exc)
            return None

    # ────────────────────────────────────────────
    # REMEDIATION GATE
    # ────────────────────────────────────────────

    async def remediation_gate(self, incident: Dict, rca: Dict) -> str:
        confidence = rca.get("confidence", 0.0)
        policy     = self.get_remediation_policy(incident, rca)

        # ── Non-fixable error type gate ───────────────────────────────────────
        # If the error type is definitively NOT fixable via iFlow XML and we have
        # high confidence in that classification, skip RCA/fix and escalate directly.
        _gate_error_type = rca.get("error_type") or incident.get("error_type") or "UNKNOWN_ERROR"
        _is_fixable = self._classifier.is_iflow_fixable(_gate_error_type)
        if not _is_fixable and confidence >= 0.80:
            _reason = self._classifier.fallback_root_cause(_gate_error_type, incident.get("error_message", ""))
            logger.info(
                "[Gate] NON-FIXABLE error type '%s' (confidence=%.2f) — escalating to ticket: %s",
                _gate_error_type, confidence, incident.get("iflow_id", ""),
            )
            update_incident(incident["incident_id"], {"status": "RCA_INCONCLUSIVE"})
            ticket_id = await self._create_external_ticket(incident, rca)
            update_incident(incident["incident_id"], {
                "status":    "TICKET_CREATED",
                "ticket_id": ticket_id,
                "fix_summary": (
                    f"Error type '{_gate_error_type}' cannot be resolved by modifying iFlow XML. "
                    f"Root cause: {_reason[:300]}"
                ),
            })
            return "TICKET_CREATED"

        # Classifier confidence floor
        clf_confidence = self._classifier.classify_error(
            incident.get("error_message", "")
        ).get("confidence", 0.0)
        if clf_confidence > confidence:
            logger.info("[Gate] Confidence floor: LLM=%.2f → classifier=%.2f", confidence, clf_confidence)
            confidence        = clf_confidence
            rca["confidence"] = confidence

        logger.info(
            "[GATE_ENTRY] iflow=%s error_type=%s confidence=%.2f policy=%s "
            "auto_fix_all=%s has_actionable_fix=%s",
            incident.get("iflow_id", ""), rca.get("error_type", ""), confidence,
            policy["action"], AUTO_FIX_ALL_CPI_ERRORS, self.has_actionable_fix(rca),
        )

        if policy["action"] == "RETRY" and confidence >= SUGGEST_FIX_CONFIDENCE:
            logger.info("[Gate] POLICY RETRY (%.2f) → %s", confidence, incident["iflow_id"])
            retry_result = await self._verifier.retry_failed_message(incident)
            if retry_result["success"]:
                update_incident(incident["incident_id"], {
                    "status":      "RETRIED",
                    "fix_summary": retry_result["summary"],
                    "resolved_at": get_hana_timestamp(),
                })
                return "RETRIED"
            logger.info("[Gate] Retry unavailable/failed — escalating to iFlow fix: %s", incident["iflow_id"])

        # Auto-fix path: either policy says AUTO_FIX, or auto_fix_enabled runtime flag is set
        effective_auto_fix = (
            RUNTIME_FLAGS["auto_fix_enabled"]
            and self.has_actionable_fix(rca)
            and rca.get("error_type", "UNKNOWN_ERROR") != "UNKNOWN_ERROR"
        )
        if self.should_auto_fix(incident, rca, policy, confidence) or effective_auto_fix:
            _iflow_id = incident.get("iflow_id", "")
            _lock = self._get_iflow_fix_lock(_iflow_id) if _iflow_id else None
            if _lock:
                try:
                    await asyncio.wait_for(_lock.acquire(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[Gate] iFlow '%s' fix already in progress — deferring incident %s to AWAITING_APPROVAL",
                        _iflow_id, incident.get("incident_id", ""),
                    )
                    update_incident(incident["incident_id"], {
                        "status":        "AWAITING_APPROVAL",
                        "pending_since": get_hana_timestamp(),
                    })
                    return "AWAITING_APPROVAL"
            try:
                logger.info("[Gate] AUTO-FIX (%.2f) → %s", confidence, incident["iflow_id"])
                fix_result   = await self._fix.apply_fix(incident, rca)
                fix_summary  = fix_result.get("summary", "")
                retry_result = None
                if fix_result["success"] and policy.get("replay_after_fix"):
                    retry_result = await self._verifier.retry_failed_message(incident)
                    if retry_result.get("summary"):
                        fix_summary = f"{fix_summary}\nRetry: {retry_result['summary']}"
                final_status = self._fix.determine_post_fix_status(
                    fix_result["success"], policy,
                    retry_result=retry_result, human_approved=False,
                    failed_stage=fix_result.get("failed_stage", ""),
                )
                _resolved = final_status in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX"}
                update_incident(incident["incident_id"], {
                    "status":               final_status,
                    "fix_summary":          fix_summary,
                    "resolved_at":          get_hana_timestamp() if _resolved else None,
                    "verification_status":  "VERIFIED" if _resolved else "PENDING",
                })
                # ── Dispatch verification directly — no Solace round-trip ─────────
                asyncio.create_task(self._handle_fix({
                    "incident_id":    incident.get("incident_id", ""),
                    "iflow_id":       incident.get("iflow_id"),
                    "fix_applied":    fix_result.get("fix_applied"),
                    "deploy_success": fix_result.get("deploy_success"),
                    "success":        fix_result.get("success"),
                    "summary":        fix_summary[:300],
                }))
                # Store what was actually done (agent summary) alongside the RCA proposed_fix.
                # This makes future pattern reuse grounded in the executed change, not just the diagnosis.
                _fix_applied_desc = (
                    fix_result.get("summary") or rca.get("proposed_fix", "")
                ).strip()[:1000]
                upsert_fix_pattern({
                    "error_signature": self._classifier.error_signature(
                        incident["iflow_id"], rca.get("error_type", ""), incident.get("error_message", "")
                    ),
                    "iflow_id":   incident["iflow_id"],
                    "error_type": rca.get("error_type", ""),
                    "root_cause": rca.get("root_cause", ""),
                    "fix_applied": _fix_applied_desc,
                    "outcome":    "SUCCESS" if fix_result["success"] else "FAILED",
                    "key_steps":  fix_result.get("steps", []) if fix_result["success"] else [],
                })
                return final_status
            finally:
                if _lock and _lock.locked():
                    _lock.release()

        if confidence >= SUGGEST_FIX_CONFIDENCE:
            logger.info("[Gate] MEDIUM (%.2f) → awaiting approval: %s", confidence, incident["iflow_id"])
            update_incident(incident["incident_id"], {
                "status":        "AWAITING_APPROVAL",
                "pending_since": get_hana_timestamp(),
            })
            return "AWAITING_APPROVAL"

        logger.info("[Gate] LOW (%.2f) → inconclusive, creating ticket: %s", confidence, incident["iflow_id"])
        update_incident(incident["incident_id"], {"status": "RCA_INCONCLUSIVE"})
        ticket_id = await self._create_external_ticket(incident, rca)
        update_incident(incident["incident_id"], {
            "status":    "TICKET_CREATED",
            "ticket_id": ticket_id,
        })
        return "TICKET_CREATED"

    # ────────────────────────────────────────────
    # RESUME CORRELATED INCIDENT
    # ────────────────────────────────────────────

    async def resume_correlated_incident(
        self, incident: Dict, latest_data: Dict
    ) -> str:
        incident_id    = incident.get("incident_id", "")
        current_status = str(incident.get("status", "")).upper()
        latest_guid    = (
            latest_data.get("message_guid")
            or latest_data.get("MessageGuid")
            or incident.get("message_guid")
        )
        refresh = {
            "message_guid":   latest_guid,
            "error_message":  latest_data.get("error_message", incident.get("error_message", "")),
            "correlation_id": latest_data.get("correlation_id", incident.get("correlation_id", "")),
            "last_seen":      get_hana_timestamp(),
        }
        update_incident(incident_id, refresh)
        merged = {**incident, **latest_data, **refresh}

        if current_status in {"RCA_IN_PROGRESS", "FIX_IN_PROGRESS"}:
            logger.info("[Autonomous] Existing incident still in progress: %s", incident_id)
            return current_status

        if current_status in {"TICKET_CREATED", "AWAITING_APPROVAL"}:
            logger.info(
                "[Autonomous] Absorbed into existing %s incident %s — occurrence_count updated",
                current_status, incident_id,
            )
            return current_status

        rca = {
            "root_cause":         merged.get("root_cause", ""),
            "proposed_fix":       merged.get("proposed_fix", ""),
            "confidence":         merged.get("rca_confidence", 0.0),
            "auto_apply":         True,
            "error_type":         merged.get("error_type", ""),
            "affected_component": merged.get("affected_component", ""),
        }
        if current_status in {"DETECTED", "RCA_FAILED"} or not self.has_actionable_fix(rca):
            logger.info("[Autonomous] Re-running RCA for recurring incident: %s", incident_id)
            update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
            rca = await self._rca.run_rca(merged)
            update_incident(incident_id, {
                "status":             "RCA_COMPLETE",
                "root_cause":         rca.get("root_cause", ""),
                "proposed_fix":       rca.get("proposed_fix", ""),
                "rca_confidence":     rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
            })
        final_status = await self.remediation_gate(dict(merged), rca)
        logger.info("[Autonomous] Recurring incident %s → %s", incident_id, final_status)
        return final_status

    # ────────────────────────────────────────────
    # EVENT-DRIVEN STAGE HANDLERS
    # ────────────────────────────────────────────

    async def on_classified_event(self, event: Dict[str, Any]) -> None:
        """
        Triggered by 'classified' event.
        Runs RCA and emits 'rca' to continue the pipeline.
        """
        incident_id = event.get("incident_id", "")
        if not incident_id:
            return
        incident = get_incident_by_id(incident_id)
        if not incident:
            logger.error("[AEM:classified] Incident %s not found in DB", incident_id)
            return
        update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
        rca = await self._rca.run_rca(dict(incident))
        update_incident(incident_id, {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
        })
        asyncio.create_task(self.on_rca_event({
            "incident_id":        incident_id,
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "confidence":         rca.get("confidence", 0.0),
            "error_type":         rca.get("error_type", ""),
            "affected_component": rca.get("affected_component", ""),
        }))
        logger.info("[Orchestrator] incident=%s RCA complete, dispatching remediation directly", incident_id)

    async def on_rca_event(self, event: Dict[str, Any]) -> None:
        """
        Triggered by 'rca' event.
        Calls remediation_gate which applies the fix and emits 'fix' + 'verified'.
        """
        incident_id = event.get("incident_id", "")
        if not incident_id:
            return
        incident = get_incident_by_id(incident_id)
        if not incident:
            logger.error("[AEM:rca] Incident %s not found in DB", incident_id)
            return
        rca = {
            "root_cause":         incident.get("root_cause", ""),
            "proposed_fix":       incident.get("proposed_fix", ""),
            "confidence":         incident.get("rca_confidence", 0.0),
            "error_type":         incident.get("error_type", ""),
            "affected_component": incident.get("affected_component", ""),
        }
        await self.remediation_gate(dict(incident), rca)
        logger.info("[AEM:rca] incident=%s remediation_gate complete", incident_id)

    # ────────────────────────────────────────────
    # PROCESS DETECTED ERROR  (called by ObserverAgent)
    # ────────────────────────────────────────────

    async def process_detected_error(self, normalized_error: Dict[str, Any]) -> str:
        normalized  = dict(normalized_error)
        _error_msg  = normalized.get("error_message", "")
        _iflow_id   = normalized.get("iflow_id", "")

        # ── Step 1: rule-based classifier (zero latency, zero cost) ──────────
        clf = self._classifier.classify_error(_error_msg)

        # ── Step 2: LLM fallback when rule-based is uncertain ────────────────
        # Trigger when: UNKNOWN_ERROR or confidence < 0.70
        if clf.get("confidence", 0.0) < 0.70:
            try:
                llm_clf = await self._classifier.classify_with_llm(_error_msg, _iflow_id)
                if llm_clf and llm_clf.get("confidence", 0.0) > clf.get("confidence", 0.0):
                    logger.info(
                        "[Classifier] LLM upgrade: %s(%.2f) → %s(%.2f) for iflow=%s",
                        clf.get("error_type"), clf.get("confidence", 0.0),
                        llm_clf.get("error_type"), llm_clf.get("confidence", 0.0),
                        _iflow_id,
                    )
                    clf = llm_clf
            except Exception as _clf_exc:
                logger.warning("[Classifier] LLM fallback error: %s — keeping rule-based result.", _clf_exc)

        normalized.update(clf)
        logger.info(
            "[ERROR_DETECTED] iflow=%s error_type=%s confidence=%.2f fixable=%s guid=%s source=%s | error=%.250s",
            _iflow_id, clf.get("error_type", ""),
            clf.get("confidence", 0.0), clf.get("is_iflow_fixable", True),
            normalized.get("message_guid", ""),
            normalized.get("source_type", ""), _error_msg,
        )

        # ── Existing open incident for same signature ─────────────────────────
        # Skip signature dedup when iflow_id is blank or a placeholder — without
        # a real iFlow name, every unknown error would collapse into one row.
        _iflow_for_dedup = normalized.get("iflow_id", "")
        _skip_sig_dedup  = _iflow_for_dedup.lower() in ("", "unknown_iflow", "unknown", "n/a")
        existing_sig = (
            None if _skip_sig_dedup
            else get_open_incident_by_signature(_iflow_for_dedup, normalized.get("error_type", ""))
        )
        if existing_sig:
            consec = int(existing_sig.get("consecutive_failures") or 0)
            if consec >= MAX_CONSECUTIVE_FAILURES and not existing_sig.get("auto_escalated"):
                logger.warning(
                    "[Autonomous] Circuit breaker: %d consecutive failures for %s — escalating.",
                    consec, existing_sig.get("iflow_id"),
                )
                rca_for_ticket = {
                    "root_cause":   existing_sig.get("root_cause", ""),
                    "proposed_fix": existing_sig.get("proposed_fix", ""),
                    "confidence":   existing_sig.get("rca_confidence", 0.0),
                    "error_type":   existing_sig.get("error_type", ""),
                }
                ticket_id = await self._create_external_ticket(dict(existing_sig), rca_for_ticket)
                update_incident(existing_sig["incident_id"], {
                    "status":        "TICKET_CREATED",
                    "auto_escalated": 1,
                    "ticket_id":     ticket_id,
                })
                return "CIRCUIT_BREAKER_ESCALATED"

            if existing_sig.get("auto_escalated"):
                logger.info("[Autonomous] Skipping auto-escalated incident: %s", existing_sig.get("incident_id"))
                return "SKIPPED_AUTO_ESCALATED"

            increment_incident_occurrence(
                existing_sig.get("incident_id"),
                message_guid=normalized.get("message_guid") or None,
                last_seen=get_hana_timestamp(),
            )
            logger.info(
                "[Autonomous] Correlated duplicate: iflow=%s type=%s existing=%s",
                normalized.get("iflow_id", ""), normalized.get("error_type", ""),
                existing_sig.get("incident_id"),
            )
            return await self.resume_correlated_incident(dict(existing_sig), normalized)

        # ── Burst deduplication ───────────────────────────────────────────────
        _group_key = self.incident_group_key(normalized)
        _recent    = get_recent_incident_by_group_key(
            _group_key, within_seconds=BURST_DEDUP_WINDOW_SECONDS
        )
        if _recent:
            increment_incident_occurrence(
                _recent["incident_id"],
                message_guid=normalized.get("message_guid") or None,
                last_seen=get_hana_timestamp(),
            )
            logger.info(
                "[Autonomous] Burst dedup: absorbed into %s (group=%s)",
                _recent["incident_id"], _group_key,
            )
            return "BURST_DEDUPED"

        # ── New incident ──────────────────────────────────────────────────────
        incident_id = str(uuid.uuid4())
        incident    = {
            **normalized,
            "incident_id":        incident_id,
            "status":             "DETECTED",
            "created_at":         get_hana_timestamp(),
            "incident_group_key": _group_key,
            "occurrence_count":   1,
            "last_seen":          get_hana_timestamp(),
            "verification_status": "UNVERIFIED",
            "consecutive_failures": 0,
            "auto_escalated":     0,
        }
        _iflow_display = normalized.get("iflow_id", "")
        _artifact_id   = normalized.get("artifact_id", "")  # technical ID direct from CPI message log
        _dt_id = ""
        if _artifact_id:
            # Technical artifact ID already known — no name-to-ID OData lookup needed
            _dt_id = _artifact_id
            logger.debug("[Autonomous] Using direct artifact_id='%s' for '%s'", _dt_id, _iflow_display)
        elif _iflow_display and self._observer and self._observer.error_fetcher:
            _dt_id = await self._observer.error_fetcher.fetch_designtime_artifact_id(
                _iflow_display
            )
        incident["designtime_artifact_id"] = _dt_id

        # ── Existence check at detection time ────────────────────────────────
        _resolve_id = _dt_id or _iflow_display
        if _resolve_id and self._fix:
            _existence = await self._fix.verify_iflow_exists(_resolve_id)
            if not _existence["exists"] and _existence.get("verified"):
                incident["status"]              = "ARTIFACT_MISSING"
                incident["verification_status"] = "ARTIFACT_NOT_FOUND"
                incident["fix_summary"]         = (
                    f"iFlow '{_resolve_id}' does not exist in SAP CPI — "
                    f"detected at incident creation. {_existence['message']}"
                )
                incident["resolved_at"] = get_hana_timestamp()
                create_incident(incident)
                logger.warning(
                    "[Autonomous] ARTIFACT_MISSING at detection: incident=%s iflow=%s",
                    incident_id, _resolve_id,
                )
                return "ARTIFACT_MISSING"

        create_incident(incident)
        logger.info("[Autonomous] New incident: %s | %s | %s",
                    incident_id, normalized.get("iflow_id", ""), normalized.get("error_type", ""))

        # ── Dispatch directly — no Solace round-trip needed for in-process stages
        asyncio.create_task(self.on_classified_event({
            "incident_id": incident_id,
            "error_type":  clf.get("error_type"),
            "confidence":  clf.get("confidence"),
            "tags":        clf.get("tags", []),
        }))
        return "QUEUED"

    # ────────────────────────────────────────────
    # EXECUTE INCIDENT FIX  (human/approved trigger)
    # ────────────────────────────────────────────

    async def execute_incident_fix(
        self, incident: Dict, human_approved: bool = False, deploy_only: bool = False, force: bool = False
    ) -> Dict[str, Any]:
        incident_id      = incident.get("incident_id", "")
        working_incident = dict(incident)
        iflow_id         = (
            working_incident.get("designtime_artifact_id")
            or working_incident.get("iflow_id", "")
        )

        # ── Pre-flight: verify iFlow exists ──────────────────────────────────
        if iflow_id:
            existence = await self._fix.verify_iflow_exists(iflow_id)
            if not existence["exists"]:
                confirmed  = existence.get("verified", False)
                fix_summary = (
                    f"Cannot fix — iFlow '{iflow_id}' does not exist in SAP CPI. "
                    f"{'The artifact has been deleted.' if confirmed else 'Existence could not be confirmed — fix blocked.'} "
                    f"{existence['message']}"
                )
                update_incident(incident_id, {
                    "status":               "ARTIFACT_MISSING",
                    "fix_summary":          fix_summary,
                    "resolved_at":          get_hana_timestamp(),
                    "verification_status":  "ARTIFACT_NOT_FOUND",
                })
                return {
                    "incident_id":    incident_id,
                    "iflow_id":       iflow_id,
                    "status":         "ARTIFACT_MISSING",
                    "success":        False,
                    "fix_applied":    False,
                    "deploy_success": False,
                    "failed_stage":   "verification",
                    "technical_details": existence["message"],
                    "summary":        fix_summary,
                    "root_cause":     working_incident.get("root_cause"),
                    "proposed_fix":   working_incident.get("proposed_fix"),
                    "confidence":     working_incident.get("rca_confidence"),
                    "incident":       get_incident_by_id(incident_id) or working_incident,
                }

        # ── Pre-flight: check iFlow runtime health ───────────────────────────
        # If the iFlow is already in Started/healthy state before we touch it,
        # a human has likely fixed it manually. Skip the AI fix to avoid
        # modifying a working iFlow.
        # When force=True the user explicitly wants to apply the fix anyway
        # (e.g. iFlow is running but still has wrong config).
        if not force and iflow_id and self._observer and self._observer.error_fetcher:
            try:
                _rt = await self._observer.error_fetcher.fetch_runtime_artifact_detail(iflow_id)
                _rt_status = (
                    str(_rt.get("Status") or _rt.get("DeployState") or _rt.get("RuntimeStatus") or "")
                    .strip().lower()
                )
                if _rt_status == "started":
                    _health_summary = (
                        f"iFlow '{iflow_id}' is already in Started state — "
                        f"likely fixed manually before the AI fix could run. No changes applied."
                    )
                    logger.info("[FIX] Runtime pre-check: %s is healthy (Started) — skipping fix.", iflow_id)
                    update_incident(incident_id, {
                        "status":              "HUMAN_INITIATED_FIX",
                        "fix_summary":         _health_summary,
                        "resolved_at":         get_hana_timestamp(),
                        "verification_status": "VERIFIED",
                    })
                    return {
                        "incident_id":    incident_id,
                        "iflow_id":       iflow_id,
                        "status":         "HUMAN_INITIATED_FIX",
                        "success":        True,
                        "fix_applied":    False,
                        "deploy_success": False,
                        "failed_stage":   None,
                        "summary":        _health_summary,
                        "root_cause":     working_incident.get("root_cause"),
                        "proposed_fix":   working_incident.get("proposed_fix"),
                        "confidence":     working_incident.get("rca_confidence"),
                        "incident":       get_incident_by_id(incident_id) or working_incident,
                    }
            except Exception as _rt_exc:
                logger.warning("[FIX] Runtime health pre-check failed for %s: %s — proceeding with fix.", iflow_id, _rt_exc)
        elif force and iflow_id:
            logger.info("[FIX] force=True — skipping Started-state pre-flight check for %s", iflow_id)

        rca = {
            "root_cause":         working_incident.get("root_cause", ""),
            "proposed_fix":       working_incident.get("proposed_fix", ""),
            "confidence":         working_incident.get("rca_confidence", 0.0),
            "auto_apply":         True,
            "error_type":         working_incident.get("error_type", ""),
            "affected_component": working_incident.get("affected_component", ""),
        }

        # ── Pattern-first ────────────────────────────────────────────────────
        _sig      = self._classifier.error_signature(
            iflow_id, working_incident.get("error_type", ""), working_incident.get("error_message", "")
        )
        _patterns = get_similar_patterns(_sig)
        _best     = next(
            (p for p in _patterns if (p.get("success_count") or 0) >= PATTERN_MIN_SUCCESS_COUNT),
            None,
        )
        if _best and not self.has_actionable_fix(rca):
            logger.info("[FIX] Pattern-first: reusing proven fix for %s (success_count=%s)",
                        iflow_id, _best.get("success_count"))
            rca.update({
                "root_cause":   _best.get("root_cause", ""),
                "proposed_fix": _best.get("fix_applied", ""),
                "confidence":   1.0,
                "error_type":   _best.get("error_type", rca.get("error_type", "")),
            })
            working_incident.update({
                "root_cause":    rca["root_cause"],
                "proposed_fix":  rca["proposed_fix"],
                "rca_confidence": 1.0,
            })
            update_incident(incident_id, {
                "status":        "RCA_COMPLETE",
                "root_cause":    rca["root_cause"],
                "proposed_fix":  rca["proposed_fix"],
                "rca_confidence": 1.0,
            })

        total = 5 if not self.has_actionable_fix(rca) else 4

        # ── RCA (if no actionable fix yet) ────────────────────────────────────
        if not self.has_actionable_fix(rca):
            self._set_progress(incident_id, "Running Root Cause Analysis…", 1, total)
            update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
            rca = await self._rca.run_rca(working_incident)
            update_incident(incident_id, {
                "status":             "RCA_COMPLETE",
                "root_cause":         rca.get("root_cause", ""),
                "proposed_fix":       rca.get("proposed_fix", ""),
                "rca_confidence":     rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
            })
            working_incident.update({
                "root_cause":     rca.get("root_cause", ""),
                "proposed_fix":   rca.get("proposed_fix", ""),
                "rca_confidence": rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
            })

        # ── Unfixable detection ───────────────────────────────────────────────
        # Clear-cut structural signals: these always require changes that cannot be
        # expressed as iFlow XML property edits.
        _unfixable_signals = [
            # Groovy code changes — only compound phrases, not "groovy script" alone
            "jsonslurper",              # Groovy class reference → script code edit needed
            "try/catch",                # Groovy exception block → code edit needed
            "try {",                    # Groovy code block → code edit needed
            "rewrite the script",
            "rewrite the groovy",
            "groovy script needs to",
            "groovy script must",
            "groovy script to add",
            "groovy script to handle",
            # Structural additions — none of these can be expressed as an XML property change
            "add a router",
            "add router",
            "add content-based router",
            "add an exception subprocess",
            "add exception subprocess",
            "add a subprocess",
            "add new step",
            "add a new step",
            "add a new channel",
            "add new channel",
            "add a new adapter",
            "add a converter",
            "add json-to-xml converter",
            "add xml-to-json converter",
            # Unambiguous external-only fixes
            "requires backend changes",
            "backend team must",
            "infrastructure change required",
        ]
        # Only unfixable when the PROPOSED FIX (not just the root cause) mentions these.
        # "backend response/returns" in the root cause merely describes what happened —
        # the adapter configuration may still be fixable via iFlow XML.
        _fix_only_signals = ["backend returns", "backend response"]
        _fix_hint   = (rca.get("proposed_fix") or "").lower()
        _rc_hint    = (rca.get("root_cause") or "").lower()
        _unfixable  = (
            next((s for s in _unfixable_signals if s in _fix_hint or s in _rc_hint), None)
            or next((s for s in _fix_only_signals if s in _fix_hint), None)
        )
        if _unfixable and not deploy_only:
            _reason = (
                f"Auto-fix skipped: the root cause requires changes that cannot be safely applied "
                f"by editing iFlow XML properties (detected: '{_unfixable}'). "
                f"Manual intervention is required.\n\n"
                f"Root cause: {rca.get('root_cause', '')}\n"
                f"Suggested fix: {rca.get('proposed_fix', '')}"
            )
            ticket_id = await self._create_external_ticket(
                {**working_incident, "fix_summary": _reason}, rca
            )
            update_incident(incident_id, {
                "status":      "TICKET_CREATED",
                "fix_summary": _reason,
                "ticket_id":   ticket_id,
                "resolved_at": get_hana_timestamp(),
            })
            self._set_progress(
                incident_id, "Unfixable — ticket created for manual review",
                total, total, status="TICKET_CREATED",
            )
            return {
                "incident_id":    incident_id,
                "iflow_id":       iflow_id,
                "status":         "TICKET_CREATED",
                "success":        False,
                "fix_applied":    False,
                "deploy_success": False,
                "failed_stage":   "unfixable",
                "summary":        _reason,
                "root_cause":     rca.get("root_cause"),
                "proposed_fix":   rca.get("proposed_fix"),
                "confidence":     rca.get("confidence"),
                "incident":       get_incident_by_id(incident_id) or working_incident,
            }

        step_base = 2 if total == 5 else 1
        self._set_progress(incident_id, "Verifying iFlow exists…", step_base, total)

        # ── Double-check existence right before fix ───────────────────────────
        if iflow_id:
            existence = await self._fix.verify_iflow_exists(iflow_id)
            if not existence["exists"]:
                fix_summary = (
                    f"Cannot fix — iFlow '{iflow_id}' "
                    f"{'was deleted during analysis.' if existence.get('verified') else 'could not be confirmed mid-fix — blocked.'} "
                    f"{existence['message']}"
                )
                self._set_progress(
                    incident_id, "iFlow deleted — cannot fix", total, total, status="ARTIFACT_MISSING"
                )
                update_incident(incident_id, {
                    "status":              "ARTIFACT_MISSING",
                    "fix_summary":         fix_summary,
                    "resolved_at":         get_hana_timestamp(),
                    "verification_status": "ARTIFACT_NOT_FOUND",
                })
                return {
                    "incident_id":    incident_id,
                    "iflow_id":       iflow_id,
                    "status":         "ARTIFACT_MISSING",
                    "success":        False,
                    "fix_applied":    False,
                    "deploy_success": False,
                    "failed_stage":   "verification",
                    "technical_details": existence["message"],
                    "summary":        fix_summary,
                    "incident":       get_incident_by_id(incident_id) or working_incident,
                }

        # Serialise concurrent fixes for the same iFlow artifact — a second error type
        # arriving while a fix is in progress would otherwise overwrite the first fix.
        _fix_lock = self._get_iflow_fix_lock(iflow_id) if iflow_id else None
        if _fix_lock:
            try:
                await asyncio.wait_for(_fix_lock.acquire(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[FIX] iFlow '%s' lock timeout — concurrent fix still running after 60 s. "
                    "incident=%s returning lock_timeout.",
                    iflow_id, incident_id,
                )
                return {
                    "incident_id":    incident_id,
                    "iflow_id":       iflow_id,
                    "status":         "FIX_FAILED",
                    "success":        False,
                    "fix_applied":    False,
                    "deploy_success": False,
                    "failed_stage":   "lock_timeout",
                    "summary": (
                        f"Another fix is already running for iFlow '{iflow_id}'. "
                        "The concurrent fix is taking longer than 60 s. Please retry."
                    ),
                    "incident": get_incident_by_id(incident_id) or working_incident,
                }
        try:
            self._set_progress(
                incident_id, "Downloading iFlow configuration…", step_base + 1, total,
                iflow_id=iflow_id,
                root_cause=working_incident.get("root_cause"),
                proposed_fix=working_incident.get("proposed_fix"),
                rca_confidence=working_incident.get("rca_confidence"),
                error_type=working_incident.get("error_type"),
            )
            update_incident(incident_id, {"status": "FIX_IN_PROGRESS"})

            # ── Snapshot iFlow before modification ────────────────────────────
            if iflow_id and not deploy_only:
                await self._fix.capture_snapshot(iflow_id, incident_id)

            # ── Apply fix or deploy-only ──────────────────────────────────────
            if deploy_only:
                self._set_progress(
                    incident_id, "Deploying iFlow (update already applied)…", step_base + 2, total
                )
                fix_result = await self._fix.ask_deploy_only(
                    iflow_id=iflow_id,
                    user_id="system_autofix",
                    timestamp=get_hana_timestamp(),
                )
            else:
                # Use 5 steps to match the 5 visual stages:
                # 0=Submit 1=Get iFlow 2=Validate 3=Patch 4=Deploy
                _PROGRESS_TOTAL = 5
                self._set_progress(incident_id, "Applying fix and deploying iFlow…", 1, _PROGRESS_TOTAL)

                _LABEL_TO_SLOT: dict = {
                    "reading current iflow":     1,  # get-iflow
                    "get_iflow":                 1,
                    "validate":                  2,  # validate_iflow_xml
                    "uploading fixed iflow":     3,  # update-iflow
                    "update_iflow":              3,
                    "deploying iflow":           4,  # deploy-iflow
                    "deploy_iflow":              4,
                    "checking deployment":       4,
                }
                # Tracks the highest slot reached so correction/retry passes never
                # visually regress the stage rail (e.g. get-iflow in pass 2 stays at 3+)
                _max_slot: list[int] = [0]

                def _fix_progress(label: str) -> None:
                    lower = label.lower()
                    slot = next((v for k, v in _LABEL_TO_SLOT.items() if k in lower), None)
                    if slot is not None:
                        if slot < _max_slot[0]:
                            # We're in a correction/retry pass — never go backwards.
                            # Clamp to max-1 (re-patch slot) for non-deploy, or max for deploy.
                            slot = _max_slot[0] if "deploy" in lower else max(3, _max_slot[0] - 1)
                            label = f"Retrying: {label}"
                        else:
                            _max_slot[0] = slot
                        self._set_progress(incident_id, label, slot, _PROGRESS_TOTAL)

                fix_result = await self._fix.apply_fix(working_incident, rca, progress_fn=_fix_progress)

            policy       = self.get_remediation_policy(working_incident, rca)
            fix_summary  = fix_result.get("summary", "") or ""
            retry_result = None

            if fix_result.get("failed_stage") == "deploy" or (
                fix_result.get("fix_applied") and not fix_result.get("deploy_success")
            ):
                deploy_error_text = await self._fix.get_deploy_error_details(iflow_id)
                if deploy_error_text:
                    fix_result["technical_details"] = deploy_error_text[:1500]
                    fix_summary = (
                        f"{fix_summary}\nDeployment error details: {deploy_error_text[:800]}"
                        if fix_summary else deploy_error_text[:800]
                    )

            # ── Post-fix replay ───────────────────────────────────────────────
            replay_success = False
            replay_skipped = False
            if fix_result.get("success"):
                self._set_progress(
                    incident_id, "Validating fix — replaying failed message…", total, total
                )
                retry_result  = await self._verifier.retry_failed_message(working_incident)
                replay_success = retry_result.get("success", False)
                replay_skipped = retry_result.get("skipped", False)
                if retry_result.get("summary"):
                    fix_summary = f"{fix_summary}\nReplay: {retry_result['summary']}"

            if fix_result.get("success") and not replay_success and not replay_skipped:
                final_status = "FIX_DEPLOYED"
            else:
                final_status = self._fix.determine_post_fix_status(
                    fix_result.get("success", False),
                    policy,
                    retry_result=retry_result,
                    failed_stage=fix_result.get("failed_stage", ""),
                    human_approved=human_approved,
                )

            technical_details = fix_result.get("technical_details", "")
            if technical_details and not fix_result.get("success"):
                fix_summary = (
                    f"{fix_summary}\nTechnical detail: {technical_details}"
                    if fix_summary else technical_details
                )

            failed_stage = fix_result.get("failed_stage", "")
            done_step = (
                "Fix applied and validated successfully" if fix_result.get("success") and replay_success
                else ("Fix deployed — awaiting message verification" if fix_result.get("success")
                      else f"Fix failed — stage: {failed_stage}" if failed_stage else "Fix failed")
            )
            self._set_progress(
                incident_id, done_step, total, total, status=final_status,
                failed_stage=failed_stage or None,
                technical_details=technical_details[:300] if technical_details else None,
            )

            update_incident(incident_id, {
                "status":               final_status,
                "fix_summary":          fix_summary,
                "last_failed_stage":    failed_stage or None,
                "resolved_at":          get_hana_timestamp() if final_status in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX", "FIX_DEPLOYED"} else None,
                "verification_status":  "VERIFIED" if final_status in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX"} else ("DEPLOYED_UNVERIFIED" if final_status == "FIX_DEPLOYED" else "PENDING"),
                "root_cause":           rca.get("root_cause", ""),
                "proposed_fix":         rca.get("proposed_fix", ""),
                "rca_confidence":       rca.get("confidence", 0.0),
                "affected_component":   rca.get("affected_component", ""),
                "consecutive_failures": 0 if fix_result.get("success") else (
                    (int(working_incident.get("consecutive_failures") or 0)) + 1
                ),
            })
            _fix_applied_desc2 = (
                fix_result.get("summary") or rca.get("proposed_fix", "")
            ).strip()[:1000]
            upsert_fix_pattern(
                {
                    "error_signature": self._classifier.error_signature(
                        working_incident.get("iflow_id", ""),
                        rca.get("error_type", ""),
                        working_incident.get("error_message", ""),
                    ),
                    "iflow_id":   working_incident.get("iflow_id", ""),
                    "error_type": rca.get("error_type", ""),
                    "root_cause": rca.get("root_cause", ""),
                    "fix_applied": _fix_applied_desc2,
                    "outcome":    "SUCCESS" if fix_result.get("success") else "FAILED",
                    "key_steps":  fix_result.get("steps", []) if fix_result.get("success") else [],
                },
                replay_success=replay_success,
            )
            logger.info(
                "[FIX_OUTCOME] incident=%s iflow=%s status=%s fix_applied=%s deploy_success=%s "
                "replay=%s failed_stage=%s | summary=%.300s",
                incident_id, iflow_id, final_status,
                fix_result.get("fix_applied"), fix_result.get("deploy_success"),
                replay_success, fix_result.get("failed_stage", ""), fix_summary,
            )
            refreshed = get_incident_by_id(incident_id) or working_incident
            return {
                "incident_id":      incident_id,
                "iflow_id":         refreshed.get("iflow_id"),
                "status":           final_status,
                "success":          fix_result.get("success", False),
                "fix_applied":      fix_result.get("fix_applied", False),
                "deploy_success":   fix_result.get("deploy_success", False),
                "failed_stage":     fix_result.get("failed_stage"),
                "technical_details": fix_result.get("technical_details", ""),
                "summary":          fix_summary,
                "root_cause":       refreshed.get("root_cause"),
                "proposed_fix":     refreshed.get("proposed_fix"),
                "confidence":       refreshed.get("rca_confidence"),
                "incident":         refreshed,
            }
        finally:
            if _fix_lock and _fix_lock.locked():
                _fix_lock.release()

    # ────────────────────────────────────────────
    # INCIDENT VIEW MODEL
    # ────────────────────────────────────────────

    @staticmethod
    def _first_non_empty(*values):
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    async def build_incident_view_model(self, incident: Dict) -> Dict[str, Any]:
        from agents.observer_agent import SAPErrorFetcher  # noqa: PLC0415
        fetcher  = SAPErrorFetcher()
        metadata = {}
        message_guid = incident.get("message_guid", "")
        if message_guid:
            metadata = await fetcher.fetch_message_metadata(message_guid)

        properties = {
            "message": {
                "message_id":     self._first_non_empty(incident.get("message_guid"), metadata.get("MessageGuid")),
                "mpl_id":         self._first_non_empty(metadata.get("MessageGuid"), incident.get("message_guid")),
                "correlation_id": self._first_non_empty(incident.get("correlation_id"), metadata.get("CorrelationId")),
                "sender":         self._first_non_empty(incident.get("sender"), metadata.get("Sender")),
                "receiver":       self._first_non_empty(incident.get("receiver"), metadata.get("Receiver")),
                "interface_iflow": self._first_non_empty(incident.get("iflow_id"), metadata.get("IntegrationFlowName")),
                "status":         self._first_non_empty(incident.get("status"), metadata.get("Status"), "FAILED"),
                "tenant":         _SAP_TENANT,
            },
            "adapter": {
                "sender_adapter":   metadata.get("SenderAdapterType"),
                "receiver_adapter": metadata.get("ReceiverAdapterType"),
                "content_type":     metadata.get("ContentType"),
                "retry_count":      metadata.get("RetryCount"),
            },
            "business_context": {
                "material_id":  metadata.get("MaterialId"),
                "plant":        metadata.get("Plant"),
                "company_code": metadata.get("CompanyCode"),
            },
        }
        artifact = {
            "name":         self._first_non_empty(incident.get("iflow_id"), metadata.get("IntegrationFlowName")),
            "artifact_id":  self._first_non_empty(metadata.get("IntegrationFlowId"), incident.get("iflow_id")),
            "version":      metadata.get("Version"),
            "package":      self._first_non_empty(metadata.get("PackageId"), metadata.get("PackageName")),
            "deployed_on":  self._first_non_empty(metadata.get("LogEnd"), incident.get("log_end"), incident.get("created_at")),
            "deployed_by":  self._first_non_empty(metadata.get("User"), metadata.get("CreatedBy")),
            "runtime_node": self._first_non_empty(metadata.get("Node"), metadata.get("RuntimeNode")),
        }
        history = [
            {"title": "Detected",    "timestamp": incident.get("created_at"),
             "description": "Failed CPI message was detected and stored as an autonomous incident."},
            {"title": "Latest Seen", "timestamp": incident.get("last_seen"),
             "description": f"Occurrence count: {incident.get('occurrence_count', 1)}"},
            {"title": "Resolution",  "timestamp": incident.get("resolved_at"),
             "description": incident.get("fix_summary") or "No fix summary available yet."},
        ]
        return {
            "incident_id":  incident.get("incident_id"),
            "message_guid": message_guid,
            "iflow_id":     incident.get("iflow_id"),
            "status":       incident.get("status"),
            "error_type":   incident.get("error_type"),
            "error_details": {
                "message":   incident.get("error_message"),
                "log_start": incident.get("log_start"),
                "log_end":   incident.get("log_end"),
            },
            "ai_recommendation": {
                "diagnosis":          incident.get("root_cause"),
                "suggested_fix":      incident.get("proposed_fix"),
                "confidence":         incident.get("rca_confidence"),
                "affected_component": incident.get("affected_component"),
                "recommended_action": (
                    _STATUS_ACTION_HINTS.get(incident.get("status", ""))
                    or ACTION_HINTS.get(incident.get("error_type", ""), "No action hint available.")
                ),
                "can_generate_fix": incident.get("status") in {
                    "RCA_COMPLETE", "AWAITING_APPROVAL",
                    "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
                },
            },
            "properties": properties,
            "artifact":    artifact,
            "attachments": [],
            "history": [i for i in history if i.get("timestamp") or i.get("description")],
        }

    # ────────────────────────────────────────────
    # GENERAL CHATBOT (ask)
    # ────────────────────────────────────────────

    def _routing_hint_for_query(self, query: str) -> Optional[str]:
        q = query.lower()
        if any(k in q for k in ["document", "documentation", "spec", "template", "sap standard"]):
            return "documentation_mcp"
        if any(k in q for k in ["iflow", "integration flow", "integration suite", "deploy flow", "groovy"]):
            return "integration_suite"
        if any(k in q for k in ["test", "testing", "validate", "verification", "assertion"]):
            return "mcp_testing"
        return None

    def _is_integration_iflow_query(self, query: str) -> bool:
        return any(k in query.lower() for k in
                   ["iflow", "integration flow", "integration suite", "groovy script", "update-iflow", "script file"])

    def _is_documentation_query(self, query: str) -> bool:
        return any(k in query.lower() for k in
                   ["document", "documentation", "guide", "spec", "template", "sap standard", "adapter guide"])

    async def ask(
        self, query: str, user_id: str, session_id: str, timestamp: str
    ) -> Dict[str, Any]:
        if self._mcp.agent is None:
            raise RuntimeError("MCP agent not ready — MCP servers may still be initialising.")

        self._mcp.cleanup_memory()
        user_memory = self._mcp.memory.setdefault(session_id, [])
        tracker     = TestExecutionTracker(user_id, query, timestamp)
        logger_cb   = StepLogger(tracker)

        guidance = ""
        route_server = self._routing_hint_for_query(query)
        if route_server:
            guidance = (
                f"\n\nRouting hint: This request best matches `{route_server}`. "
                f"{SERVER_ROUTING_GUIDE.get(route_server, '')}"
            )
        if self._is_integration_iflow_query(query):
            guidance += f"\n\n{CPI_IFLOW_GROOVY_RULES}"
        if self._is_documentation_query(query):
            guidance += f"\n\n{SAP_DOC_TEMPLATE}"

        messages = list(user_memory)
        messages.append({"role": "user", "content": query + guidance})

        result = {}
        for attempt in range(3):
            try:
                result = await self._mcp.agent.ainvoke(
                    {"messages": messages},
                    config={"callbacks": [logger_cb]},
                )
                break
            except Exception as exc:
                if attempt < 2 and "model produced invalid content" in str(exc).lower():
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise

        AI_UNWANTED    = {"additional_kwargs", "response_metadata", "usage_metadata", "id", "invalid_tool_calls", "name"}
        TOOL_UNWANTED  = {"additional_kwargs", "response_metadata", "tool_call_id", "artifact", "id"}
        HUMAN_UNWANTED = {"additional_kwargs", "response_metadata", "id", "name"}

        structured_messages = []
        for idx, msg in enumerate(result["messages"]):
            msg_dict = msg.model_dump()
            unwanted = {"ai": AI_UNWANTED, "tool": TOOL_UNWANTED, "human": HUMAN_UNWANTED}.get(
                msg_dict.get("type"), set()
            )
            for k in unwanted:
                msg_dict.pop(k, None)
            msg_dict["index"] = idx
            structured_messages.append(msg_dict)

        final_msg   = result["messages"][-1]
        answer_text = final_msg.content if hasattr(final_msg, "content") else str(final_msg)

        self._mcp.update_memory(session_id, query, answer_text)
        try:
            from db.database import updateTestSuiteStatus  # noqa: PLC0415
            updateTestSuiteStatus(test_suite_id=tracker.test_suite_id, status="COMPLETED")
        except Exception:
            pass

        return {
            "answer":          answer_text,
            "steps":           logger_cb.steps,
            "agent_work_logs": structured_messages,
        }

    # ────────────────────────────────────────────
    # AEM SINGLE-QUEUE I/O
    # ────────────────────────────────────────────

    @staticmethod
    def _normalize_aem_message(msg: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize a raw AEM / SAP CPI queue message to the standard incident dict.

        Handles four source formats in order:
          1. multimap envelope – {"multimap:Messages": {"multimap:Message1": {"MessageProcessingLogs": {...}}}}
          2. JSON multimap     – {"multimap:Messages": {"multimap:Message1": {"MessageProcessingLogs": [...]}}}
          3. Direct MPL wrap   – {"MessageProcessingLogs": {...}} or {"MessageProcessingLogs": [...]}
          4. Flat message      – top-level MessageGuid / IflowId / IntegrationFlowName fields
        """
        # Step 1 — unwrap multimap:Messages envelope; take the first message block.
        _mm = msg.get("multimap:Messages")
        if isinstance(_mm, dict) and _mm:
            msg = next(iter(_mm.values()))

        # Step 2 — unwrap MessageProcessingLogs when the iFlow nests fields under it.
        # Multi-entry lists are split upstream in _route_stage; here we handle the
        # single-object case (and first-entry fallback for any stragglers).
        _mpl = msg.get("MessageProcessingLogs")
        if isinstance(_mpl, dict):
            msg = _mpl
        elif isinstance(_mpl, list) and _mpl:
            msg = _mpl[0]

        raw_body   = msg.get("raw_body") or ""

        return {
            "source_type":    "EVENT_MESH",
            "message_guid":   (msg.get("MessageGuid") or msg.get("message_guid") or msg.get("messageGuid") or ""),
            # IflowId is the technical artifact ID (e.g. "ProcessDirect_testflow") — preferred.
            # IntegrationFlowId is a SAP artifact GUID (e.g. "AGeNKJ3Th6Dl…") — store as artifact_id only.
            "iflow_id":       (msg.get("IflowId") or msg.get("IntegrationFlowName") or msg.get("iflow_id") or msg.get("iflowId") or ""),
            "artifact_id":    (msg.get("IntegrationFlowId") or msg.get("artifact_id") or ""),
            "sender":         msg.get("Sender") or msg.get("sender") or "",
            "receiver":       msg.get("Receiver") or msg.get("receiver") or "",
            "status":         msg.get("Status") or msg.get("status") or "FAILED",
            "log_start":      msg.get("LogStart") or msg.get("log_start") or "",
            "log_end":        msg.get("LogEnd") or msg.get("log_end") or "",
            "error_message":  (msg.get("error_message") or msg.get("ErrorMessage") or msg.get("errorMessage") or msg.get("CustomStatus") or msg.get("Description") or raw_body or ""),
            "correlation_id": msg.get("CorrelationId") or msg.get("correlation_id") or "",
            "error_type":     msg.get("error_type") or msg.get("errorType") or "",
        }

    # ────────────────────────────────────────────
    # PIPELINE STAGE ROUTING
    # ────────────────────────────────────────────

    async def _route_stage(self, message: Dict[str, Any]) -> None:
        """
        Route a queue message to the correct stage handler based on the 'stage' field.
        Messages from SAP CPI have no 'stage' field — they default to 'observed'.
        """
        stage = message.get("stage", "observed")

        # Guard: only block agent-dependent stages (RCA, Fix, Verify) while agents initialise.
        # "observed" only needs the classifier (sync, always ready) + HANA — never block it,
        # otherwise webhooks received before build_agent() completes are silently lost.
        if not self._agents_ready and stage != "observed":
            logger.warning(
                "[Orchestrator] Agents not yet ready for stage=%s — "
                "event-driven mode will rely on Event Mesh retry", stage
            )
            return
        try:
            if stage == "observed":
                # ── JSON multimap format ───────────────────────────────────────
                # {"multimap:Messages": {"multimap:Message1": {"MessageProcessingLogs": [...]}}}
                mm_root = message.get("multimap:Messages")
                if mm_root and isinstance(mm_root, dict):
                    logs: list = []
                    for msg_block in mm_root.values():
                        if isinstance(msg_block, dict):
                            _mpl = msg_block.get("MessageProcessingLogs", [])
                            # Wrap single-object MPL in a list; a plain dict would
                            # otherwise cause extend() to iterate over its keys.
                            logs.extend([_mpl] if isinstance(_mpl, dict) else _mpl)
                    if logs:
                        logger.info("[Orchestrator] JSON multimap — splitting %d log entry(ies)", len(logs))
                        for entry in logs:
                            if str(entry.get("Status", "")).upper() != "FAILED":
                                continue
                            raw_err   = (entry.get("ErrorMessage") or "").strip()
                            clean_err = re.sub(
                                r"\nThe MPL ID for the failed message is\s*:\s*\S+\s*$", "", raw_err
                            ).strip()
                            inc = {
                                "source_type":    "EVENT_MESH",
                                "message_guid":   entry.get("MessageGuid", ""),
                                "iflow_id":       (entry.get("IflowId") or entry.get("IntegrationFlowName") or ""),
                                "artifact_id":    (entry.get("IntegrationFlowId") or ""),
                                "sender":         "",
                                "receiver":       "",
                                "status":         "FAILED",
                                "log_start":      "",
                                "log_end":        entry.get("LogEnd", ""),
                                "error_message":  clean_err,
                                "correlation_id": "",
                                "error_type":     "",
                            }
                            if inc["message_guid"] and self._observer:
                                try:
                                    meta = await self._observer.error_fetcher.fetch_message_metadata(inc["message_guid"])
                                    if meta.get("Sender"):
                                        inc["sender"]    = meta["Sender"]
                                        inc["receiver"]  = meta.get("Receiver", "")
                                        inc["log_start"] = meta.get("LogStart", "")
                                    if not inc["log_end"]:
                                        inc["log_end"] = meta.get("LogEnd", "")
                                    logger.info("[Orchestrator] OData enriched iflow=%s guid=%s",
                                                inc["iflow_id"], inc["message_guid"])
                                except Exception as _e:
                                    logger.warning("[Orchestrator] OData enrichment failed guid=%s: %s",
                                                   inc["message_guid"], _e)
                            await self.process_detected_error(inc)
                        return  # all entries dispatched

                # ── SAP multimap XML: one Solace message = N <Error> blocks ──
                raw_body = message.get("raw_body", "")
                if raw_body and raw_body.strip().startswith("<"):
                    error_blocks = re.findall(r"<Error>(.*?)</Error>", raw_body, re.DOTALL)
                    if error_blocks:
                        logger.info("[Orchestrator] SAP multimap XML — splitting %d error block(s)", len(error_blocks))
                        for block in error_blocks:
                            guid_m = re.search(r"MPL ID for the failed message is\s*:\s*(\S+)", block)
                            guid   = guid_m.group(1).strip() if guid_m else ""
                            clean  = re.sub(r"The MPL ID for the failed message is\s*:\s*\S+", "", block).strip()
                            inc = {
                                "source_type":    "EVENT_MESH",
                                "message_guid":   guid,
                                "iflow_id":       "",
                                "sender":         "",
                                "receiver":       "",
                                "status":         "FAILED",
                                "log_start":      "",
                                "log_end":        "",
                                "error_message":  clean,
                                "correlation_id": "",
                                "error_type":     "",
                            }
                            if guid and self._observer:
                                try:
                                    meta = await self._observer.error_fetcher.fetch_message_metadata(guid)
                                    if meta.get("IntegrationFlowName"):
                                        inc["iflow_id"]  = meta["IntegrationFlowName"]
                                        inc["sender"]    = meta.get("Sender", "")
                                        inc["receiver"]  = meta.get("Receiver", "")
                                        inc["log_start"] = meta.get("LogStart", "")
                                        inc["log_end"]   = meta.get("LogEnd", "")
                                        logger.info("[Orchestrator] Resolved iflow=%s for guid=%s",
                                                    inc["iflow_id"], guid)
                                except Exception as _e:
                                    logger.warning("[Orchestrator] OData fallback failed guid=%s: %s", guid, _e)
                            await self.process_detected_error(inc)
                        return  # all blocks dispatched — skip single-message path below

                # ── Direct MessageProcessingLogs format ───────────────────────
                # {"MessageProcessingLogs": {...}} or {"MessageProcessingLogs": [{...}]}
                _mpl_direct = message.get("MessageProcessingLogs")
                if _mpl_direct is not None:
                    _mpl_entries = _mpl_direct if isinstance(_mpl_direct, list) else [_mpl_direct]
                    logger.info("[Orchestrator] MessageProcessingLogs format — %d entry(ies)", len(_mpl_entries))
                    for _mpl_entry in _mpl_entries:
                        if str(_mpl_entry.get("Status", "")).upper() != "FAILED":
                            continue
                        _norm = self._normalize_aem_message(_mpl_entry)
                        _iflow_ph = _norm["iflow_id"].lower() in ("", "unknown_iflow", "unknown", "n/a")
                        if _iflow_ph and _norm["message_guid"] and self._observer:
                            try:
                                _meta = await self._observer.error_fetcher.fetch_message_metadata(_norm["message_guid"])
                                if _meta.get("IntegrationFlowName"):
                                    _norm["iflow_id"]  = _meta["IntegrationFlowName"]
                                    _norm["sender"]    = _meta.get("Sender", "") or _norm["sender"]
                                    _norm["receiver"]  = _meta.get("Receiver", "") or _norm["receiver"]
                                    _norm["log_start"] = _meta.get("LogStart", "") or _norm["log_start"]
                                    _norm["log_end"]   = _meta.get("LogEnd", "") or _norm["log_end"]
                            except Exception as _odata_exc:
                                logger.warning("[Orchestrator] OData iflow fallback failed (guid=%s): %s",
                                               _norm["message_guid"], _odata_exc)
                        await self.process_detected_error(_norm)
                    return  # all MPL entries dispatched

                normalized = self._normalize_aem_message(message)
                _iflow_placeholder = normalized["iflow_id"].lower() in ("", "unknown_iflow", "unknown", "n/a")
                if _iflow_placeholder and normalized["message_guid"] and self._observer:
                    try:
                        meta = await self._observer.error_fetcher.fetch_message_metadata(normalized["message_guid"])
                        if meta.get("IntegrationFlowName"):
                            normalized["iflow_id"]  = meta["IntegrationFlowName"]
                            normalized["sender"]    = meta.get("Sender", "") or normalized["sender"]
                            normalized["receiver"]  = meta.get("Receiver", "") or normalized["receiver"]
                            normalized["log_start"] = meta.get("LogStart", "") or normalized["log_start"]
                            normalized["log_end"]   = meta.get("LogEnd", "") or normalized["log_end"]
                            logger.info("[Orchestrator] iflow_id resolved via OData: %s (guid=%s)",
                                        normalized["iflow_id"], normalized["message_guid"])
                    except Exception as _odata_exc:
                        logger.warning("[Orchestrator] OData iflow fallback failed (guid=%s): %s",
                                       normalized["message_guid"], _odata_exc)
                await self.process_detected_error(normalized)
            elif stage == "classified":
                await self.on_classified_event(message)
            elif stage == "rca":
                await self.on_rca_event(message)
            elif stage == "fix":
                await self._handle_fix(message)
            elif stage == "verified":
                await self._handle_verified(message)
            else:
                logger.warning("[Orchestrator] Unknown stage '%s' — treating as new error", stage)
                normalized = self._normalize_aem_message(message)
                _iflow_placeholder = normalized["iflow_id"].lower() in ("", "unknown_iflow", "unknown", "n/a")
                if _iflow_placeholder and normalized["message_guid"] and self._observer:
                    try:
                        meta = await self._observer.error_fetcher.fetch_message_metadata(normalized["message_guid"])
                        if meta.get("IntegrationFlowName"):
                            normalized["iflow_id"] = meta["IntegrationFlowName"]
                    except Exception:
                        pass
                await self.process_detected_error(normalized)
        except Exception as exc:
            logger.error("[Orchestrator] Stage '%s' handler error: %s", stage, exc)
            # Create a PARSE_FAILED incident so the failure is visible in the dashboard
            # instead of being silently dropped.
            if stage == "observed":
                try:
                    _raw = json.dumps(message)[:2000] if message else ""
                    create_incident({
                        "incident_id":   str(uuid.uuid4()),
                        "iflow_id":      message.get("IflowId") or message.get("iflow_id") or message.get("IntegrationFlowName") or "UNKNOWN",
                        "message_guid":  message.get("message_guid") or message.get("MessageGuid") or "",
                        "status":        "PARSE_FAILED",
                        "error_type":    "PARSE_FAILED",
                        "error_message": f"AEM message could not be parsed: {exc} | raw={_raw[:500]}",
                        "created_at":    get_hana_timestamp(),
                    })
                except Exception as _db_exc:
                    logger.error("[Orchestrator] Failed to create PARSE_FAILED incident: %s", _db_exc)

    async def _handle_fix(self, message: Dict[str, Any]) -> None:
        """
        Stage 4: Fix has been applied — run verifier and publish 'verified' back to queue.
        """
        incident_id = message.get("incident_id", "")
        if not incident_id:
            return
        incident = get_incident_by_id(incident_id)
        if not incident:
            logger.error("[Orchestrator:fix] Incident %s not found in DB", incident_id)
            return
        # Guard: skip verification if the fix agent never successfully applied the change.
        # Without this check, a FIX_FAILED_UPDATE incident would be re-tested against the
        # unchanged iFlow and incorrectly promoted to FIX_FAILED_RUNTIME.
        current_status = incident.get("status", "")
        _fix_not_applied = {
            "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED",
            "ARTIFACT_MISSING", "TICKET_CREATED",
        }
        if current_status in _fix_not_applied:
            logger.warning(
                "[Orchestrator:fix] Skipping verification — fix was not applied: "
                "incident=%s current_status=%s",
                incident_id, current_status,
            )
            return
        result = await self._verifier.test_iflow_after_fix(dict(incident))
        final_status = "FIX_VERIFIED" if result.get("test_passed") else "FIX_FAILED_RUNTIME"
        _resolved    = result.get("test_passed", False)
        update_incident(incident_id, {
            "status":              final_status,
            "verification_status": "VERIFIED" if _resolved else "FAILED",
            "resolved_at":         get_hana_timestamp() if _resolved else None,
        })
        # ── Dispatch terminal stage directly — no Solace round-trip ──────────
        await self._handle_verified({
            "incident_id": incident_id,
            "iflow_id":    incident.get("iflow_id"),
            "status":      final_status,
            "resolved":    _resolved,
            "summary":     result.get("summary", ""),
        })
        logger.info("[Orchestrator:fix] incident=%s verification=%s", incident_id, final_status)

    async def _handle_verified(self, message: Dict[str, Any]) -> None:
        """Stage 5: Terminal — log the final outcome."""
        logger.info(
            "[Orchestrator:verified] incident=%s final_status=%s resolved=%s",
            message.get("incident_id", ""), message.get("status", ""), message.get("resolved", False),
        )

