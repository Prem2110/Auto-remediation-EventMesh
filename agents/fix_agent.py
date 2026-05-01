"""
agents/fix_agent.py
===================
FixAgent — handles the get → update → deploy pipeline for a CPI iFlow.

Responsibilities:
  - ask_fix_and_deploy()     — main entry: build prompt, invoke agent, verify output
  - ask_deploy_only()        — re-deploy without re-applying the fix
  - apply_fix()              — thin wrapper used by OrchestratorAgent
  - _try_unlock_iflow_via_api() — cancel checkout before/after fix
  - verify_iflow_exists()    — existence check via get-iflow MCP tool
  - get_deploy_error_details() — retrieve deploy errors from MCP + SAP OData
  - evaluate_fix_result()    — classify step output → success/failed_stage dict
  - _diagnose_timeout()      — diagnose which pipeline stage timed out
  - _poll_deploy_status()    — post-timeout runtime artifact poll

All fix-specific helper methods that were previously on MultiMCP live here.

Exports:
  FixAgent
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

import httpx

from agents.base import StepLogger, TestExecutionTracker
from core.constants import (
    CPI_IFLOW_GROOVY_RULES,
    CPI_IFLOW_XML_PATTERNS,
    ERROR_TYPE_FIX_GUIDANCE,
    FIX_AND_DEPLOY_PROMPT_TEMPLATE,
    FIX_OPERATION_PROMPT_TEMPLATE,
    GROOVY_STRIPE_HTTP_ADAPTER,
    GROOVY_WOOCOMMERCE_HTTP_ADAPTER,
)
from core.xml_patcher import PatcherError, apply_fix_operations
from core.validators import _extract_iflow_file, _fix_ctx
from db.database import get_similar_patterns
from utils.utils import get_hana_timestamp
from utils.vector_store import get_vector_store

logger = logging.getLogger(__name__)

_SAP_TENANT = os.getenv("SAP_HUB_TENANT_URL", "")


class FixAgent:
    """
    Executes the fix + deploy pipeline for a CPI iFlow.
    Holds a reference to MultiMCP for tool access and the error_fetcher
    (injected from ObserverAgent at startup) for deploy status polling.
    """

    def __init__(self, mcp, error_fetcher=None):
        self._mcp               = mcp
        self.error_fetcher      = error_fetcher   # SAPErrorFetcher, injected after construction
        self._agent             = None            # filtered agent (fix tools only)
        self._diagnosis_agent   = None            # read-only agent for structured fix operations

    def set_error_fetcher(self, error_fetcher) -> None:
        self.error_fetcher = error_fetcher

    async def build_agent(self) -> None:
        """Build a fix+deploy agent with local validate_iflow_xml @tool + 3 specific MCP tools."""
        from langchain_core.tools import tool as _tool   # noqa: PLC0415
        from core.validators import _check_iflow_xml, _fix_ctx as _validator_ctx  # noqa: PLC0415

        @_tool
        def validate_iflow_xml(xml_content: str) -> str:
            """
            Validate iFlow XML structure before sending to SAP CPI.
            Runs 7 structural checks (namespace, BPMN shape IDs, property files, etc.).
            Returns 'VALID' or 'ERRORS: <list>'.
            """
            ctx = _validator_ctx.get()
            original_xml = ctx.get("xml", "") if ctx else ""
            errors = _check_iflow_xml(original_xml, xml_content)
            if not errors:
                return "VALID"
            return "ERRORS: " + " | ".join(errors)

        # Exactly 3 MCP tools — no testing, no docs
        get_iflow_tool    = self._mcp.get_mcp_tool("integration_suite", "get-iflow")
        update_iflow_tool = self._mcp.get_mcp_tool("integration_suite", "update-iflow")
        deploy_iflow_tool = self._mcp.get_mcp_tool("integration_suite", "deploy-iflow")

        mcp_tools = [t for t in [get_iflow_tool, update_iflow_tool, deploy_iflow_tool] if t]
        if not mcp_tools:
            # Fallback: use all integration_suite tools if specific tools not yet discovered
            mcp_tools = [t for t in self._mcp.tools if t.server == "integration_suite"]

        all_tools = [validate_iflow_xml] + mcp_tools

        system_prompt = f"""You are an SAP CPI iFlow fix and deployment agent.

CRITICAL — execute these steps IN ORDER for every fix task:
1. Call get-iflow to read the current iFlow configuration.
2. Analyse the error and determine the minimal required XML change.
3. Call validate_iflow_xml with the updated XML to confirm it is structurally valid.
4. Call update-iflow with the corrected iFlow ONLY IF validate_iflow_xml returns VALID.
5. Call deploy-iflow — MANDATORY after every successful update. NEVER stop after update.

Must do:
- Report actual tool responses — never fabricate success.
- Return structured JSON at the end:
  {{"fix_applied": true/false, "deploy_success": true/false, "summary": "..."}}
"""
        self._agent = await self._mcp.build_agent(
            tools=all_tools,
            system_prompt=system_prompt,
            deployment_id=os.getenv("LLM_DEPLOYMENT_ID_FIX") or None,
        )

        # Diagnosis-only agent: get-iflow only (no update/deploy).
        # Used in Phase 1 to derive a structured operation JSON from the iFlow XML.
        _diag_tools = [t for t in [get_iflow_tool] if t]
        if _diag_tools:
            self._diagnosis_agent = await self._mcp.build_agent(
                tools=_diag_tools,
                system_prompt=(
                    "You are a read-only SAP CPI iFlow analyser. "
                    "You MAY call get-iflow if the XML is not in the prompt. "
                    "Return ONLY a JSON fix operation — no markdown, no tool calls other than get-iflow."
                ),
                deployment_id=os.getenv("LLM_DEPLOYMENT_ID_FIX") or None,
            )

        logger.info(
            "[Fix] Agent ready — 1 local @tool + %d MCP tools (diagnosis_agent=%s).",
            len(mcp_tools), self._diagnosis_agent is not None,
        )

    # ── tool output success detectors ────────────────────────────────────────

    @staticmethod
    def _update_succeeded(output: str) -> bool:
        text = (output or "").lower()
        return any(s in text for s in (
            '"status":200', '"status": 200',
            "successfully updated", "update successful", "saved successfully",
            '"result":"ok"', '"result": "ok"',
            '"success":true', '"success": true',
        ))

    @staticmethod
    def _deploy_succeeded(output: str) -> bool:
        text = (output or "").lower()
        return any(s in text for s in (
            '"deploystatus":"success"', '"deploystatus": "success"',
            '"status":"success"', '"status": "success"',
            '"result":"success"', '"result": "success"',
            "deployed successfully", "deployment successful",
            "successfully deployed",
            '"deploy_success": true', '"deploy_success":true',
        ))

    @staticmethod
    def _is_locked_error(output: str) -> bool:
        text = (output or "").lower()
        return (
            "is locked" in text
            or "artifact as it is locked" in text
            or ("cannot update the artifact" in text and "locked" in text)
        )

    # ── timeout diagnosis ────────────────────────────────────────────────────

    def _diagnose_timeout(self, steps: List[Dict], iflow_id: str) -> Dict[str, Any]:
        tools_called  = [str(s.get("tool", "")) for s in steps]
        deploy_called = any("deploy" in t for t in tools_called)
        update_called = any("update" in t and "iflow" in t for t in tools_called)
        get_called    = any("get" in t and "iflow" in t for t in tools_called)

        update_ok = any(
            self._update_succeeded(str(s.get("output", "")))
            for s in steps
            if "update" in str(s.get("tool", "")) and "iflow" in str(s.get("tool", ""))
        )

        if deploy_called:
            return {
                "success": False, "fix_applied": True, "deploy_success": False,
                "failed_stage": "deploy",
                "technical_details": (
                    f"deploy-iflow tool was called for '{iflow_id}' but did not return within 600 s. "
                    "The SAP Cloud Foundry router likely closed the SSE stream. "
                    "The iFlow content was already updated. "
                    "Check SAP CPI Monitor → Manage Integration Content."
                ),
                "summary": (
                    f"iFlow '{iflow_id}' content was updated but deployment confirmation timed out. "
                    "Verify in Monitor → Manage Integration Content, or use /retry to redeploy."
                ),
            }
        if update_called and not update_ok:
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "update",
                "technical_details": f"update-iflow timed out before returning for '{iflow_id}'.",
                "summary": f"iFlow update for '{iflow_id}' timed out before completing. Retry the fix.",
            }
        if get_called:
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "agent",
                "technical_details": (
                    f"get-iflow succeeded but agent timed out before calling update-iflow for '{iflow_id}'."
                ),
                "summary": f"iFlow '{iflow_id}' was downloaded but the fix agent timed out. Retry.",
            }
        return {
            "success": False, "fix_applied": False, "deploy_success": False,
            "failed_stage": "agent",
            "technical_details": f"Fix agent timed out before calling any MCP tools for '{iflow_id}'.",
            "summary": f"Fix agent timed out before starting tool calls for '{iflow_id}'. Retry.",
        }

    # ── deploy status poll ───────────────────────────────────────────────────

    async def _poll_deploy_status(
        self, iflow_id: str, polls: int = 5, interval: float = 15.0
    ) -> Dict[str, Any]:
        _STARTED = {"started", "starting", "deployed", "active", "running"}
        _FAILED  = {"error", "failed", "stopped"}

        if self.error_fetcher is None:
            return {"deploy_confirmed": False, "status": "error_fetcher_unavailable"}

        for attempt in range(1, polls + 1):
            try:
                detail = await self.error_fetcher.fetch_runtime_artifact_detail(iflow_id)
                raw_status = str(
                    detail.get("Status") or detail.get("DeployState") or detail.get("RuntimeStatus") or ""
                ).lower().strip()
                logger.info("[DEPLOY_POLL] iflow=%s attempt=%d/%d status=%s", iflow_id, attempt, polls, raw_status)
                if raw_status in _STARTED:
                    return {"deploy_confirmed": True, "status": raw_status}
                if raw_status in _FAILED:
                    return {"deploy_confirmed": False, "status": raw_status}
            except Exception as exc:
                logger.warning("[DEPLOY_POLL] iflow=%s attempt=%d error: %s", iflow_id, attempt, exc)
            if attempt < polls:
                await asyncio.sleep(interval)

        return {"deploy_confirmed": False, "status": "timeout"}

    # ── fix result evaluator ─────────────────────────────────────────────────

    def evaluate_fix_result(self, steps: List[Dict], answer: str) -> Dict[str, Any]:
        def compact(text: str) -> str:
            return re.sub(r"\s+", " ", str(text or "")).strip()[:500]

        update_ok         = False
        deploy_ok         = False
        update_output     = deploy_output = ""
        validation_called = False
        validation_failed = False
        for step in steps:
            tool_name = str(step.get("tool", ""))
            output    = str(step.get("output", ""))
            if "validate_iflow_xml" in tool_name:
                validation_called = True
                if "ERRORS:" in output:
                    validation_failed = True
                    logger.warning(
                        "[FIX_DEPLOY] validate_iflow_xml returned errors but agent may still update: %s",
                        compact(output),
                    )
            elif "update_iflow" in tool_name or "update-iflow" in tool_name:
                update_output = output
                update_ok     = self._update_succeeded(output)
                if update_ok and not validation_called:
                    logger.warning(
                        "[FIX_DEPLOY] update-iflow succeeded without prior validate_iflow_xml call"
                    )
                elif update_ok and validation_failed:
                    logger.warning(
                        "[FIX_DEPLOY] update-iflow called despite validate_iflow_xml reporting ERRORS"
                    )
            elif "deploy_iflow" in tool_name or "deploy-iflow" in tool_name:
                deploy_output = output
                deploy_ok     = self._deploy_succeeded(output)

        if not update_ok:
            is_locked = self._is_locked_error(update_output)
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "locked" if is_locked else "update",
                "technical_details": compact(update_output),
                "summary": (
                    "iFlow is locked in SAP CPI Integration Flow Designer. "
                    "Cancel/close the checkout in SAP CPI and retry."
                    if is_locked else
                    "iFlow update failed during the SAP Integration Suite update step."
                ),
                "failed_steps": ["update-iflow"],
            }
        if not deploy_ok:
            return {
                "success": False, "fix_applied": True, "deploy_success": False,
                "failed_stage": "deploy",
                "technical_details": compact(deploy_output),
                "summary": "iFlow content was updated but deployment did not complete successfully.",
                "failed_steps": ["deploy-iflow"],
            }
        _validation_note = ""
        if not validation_called:
            _validation_note = " [warn: XML validation step was skipped by agent]"
        elif validation_failed:
            _validation_note = " [warn: agent proceeded despite XML validation errors]"
        return {
            "success": True, "fix_applied": True, "deploy_success": True,
            "failed_stage": None,
            "technical_details": _validation_note.strip(),
            "summary": f"iFlow updated and deployed successfully. {compact(answer)}{_validation_note}",
            "failed_steps": [],
        }

    # ── iFlow unlock ─────────────────────────────────────────────────────────

    async def _try_unlock_iflow_via_api(self, iflow_id: str) -> Dict[str, Any]:
        if not iflow_id:
            return {"success": False, "status_code": 0, "message": "No iFlow ID provided"}
        if self.error_fetcher is None:
            return {"success": False, "status_code": 0, "message": "error_fetcher not set"}

        try:
            token = await self.error_fetcher._get_token()
        except Exception as exc:
            return {"success": False, "status_code": 0, "message": f"Token error: {exc}"}

        base          = _SAP_TENANT.rstrip("/")
        artifact_path = f"/api/v1/IntegrationDesigntimeArtifacts(Id='{iflow_id}',Version='active')"
        headers       = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        found_lock    = False

        for method, url_suffix in [
            ("DELETE", f"{base}{artifact_path}/checkout"),
            ("POST",   f"{base}{artifact_path}/CancelCheckout"),
        ]:
            try:
                async with httpx.AsyncClient(verify=False, timeout=30) as client:
                    if method == "DELETE":
                        resp = await client.delete(url_suffix, headers=headers)
                    else:
                        resp = await client.post(url_suffix, headers=headers)
                if resp.status_code in (200, 202, 204):
                    logger.info("[UNLOCK] Unlocked via %s: %s (HTTP %s)", method, iflow_id, resp.status_code)
                    return {"success": True, "status_code": resp.status_code,
                            "message": f"Unlock successful via {method}"}
                if resp.status_code != 404:
                    found_lock = True
            except Exception as exc:
                logger.debug("[UNLOCK] %s exception for %s: %s", method, iflow_id, exc)

        # Try MCP cancel/unlock tool
        unlock_tools = [
            t.mcp_tool_name for t in self._mcp.tools
            if t.server == "integration_suite" and any(
                kw in f"{t.name} {t.mcp_tool_name}".lower()
                for kw in ("cancel", "checkout", "unlock", "force_unlock", "discard")
            )
        ]
        if unlock_tools:
            try:
                out = await self._mcp.execute(
                    "integration_suite", unlock_tools[0],
                    {"iflow_id": iflow_id, "id": iflow_id, "artifact_id": iflow_id},
                )
                if "error" not in str(out).lower() and "fail" not in str(out).lower():
                    return {"success": True, "status_code": 200,
                            "message": f"MCP unlock via {unlock_tools[0]}"}
            except Exception as exc:
                logger.debug("[UNLOCK] MCP tool error: %s", exc)

        return {
            "success": False, "status_code": 0,
            "message": (
                f"No active checkout API lock found for '{iflow_id}'."
                if not found_lock else
                f"Could not unlock '{iflow_id}' — cancel checkout in SAP CPI IFD and retry."
            ),
        }

    # ── verify iFlow exists ───────────────────────────────────────────────────

    async def verify_iflow_exists(self, iflow_id: str) -> Dict[str, Any]:
        if not iflow_id:
            return {"exists": False, "verified": False, "status_code": 0,
                    "message": "No iFlow ID provided", "detail": {}}
        try:
            result = await self._mcp.execute_integration_tool("get-iflow", {"id": iflow_id})
            if result.get("success"):
                output     = result.get("output", {})
                iflow_data = (
                    output if isinstance(output, dict) else {}
                )
                if isinstance(output, str):
                    try:
                        iflow_data = json.loads(output)
                    except Exception:
                        iflow_data = {"raw_output": output}
                return {"exists": True, "verified": True, "status_code": 200,
                        "message": f"iFlow '{iflow_id}' exists in SAP CPI", "detail": iflow_data}

            error_msg = str(result.get("output", "")).lower()
            # Only mark verified=True when the error unambiguously refers to the
            # artifact itself (genuine CPI 404), not an MCP/network/auth failure
            # that happens to contain generic "not found" text.
            _artifact_terms = ("artifact", "iflow", "integration", "designtime", "content")
            _is_artifact_404 = "404" in error_msg and any(t in error_msg for t in _artifact_terms)
            _is_named_missing = (
                ("not found" in error_msg or "does not exist" in error_msg)
                and any(t in error_msg for t in _artifact_terms)
            )
            if _is_artifact_404 or _is_named_missing:
                return {"exists": False, "verified": True, "status_code": 404,
                        "message": f"iFlow '{iflow_id}' does not exist in SAP CPI",
                        "detail": {}}
            return {"exists": False, "verified": False, "status_code": 0,
                    "message": f"iFlow '{iflow_id}' existence could not be confirmed. "
                               f"Error: {str(result.get('output', ''))[:200]}",
                    "detail": {}}
        except Exception as exc:
            return {"exists": False, "verified": False, "status_code": 0,
                    "message": f"Verification error: {exc}", "detail": {}}

    # ── deploy error details ──────────────────────────────────────────────────

    async def get_deploy_error_details(self, iflow_id: str) -> str:
        if not iflow_id:
            return "No iFlow ID available to retrieve deploy error details."
        deploy_error = await self._mcp.execute_integration_tool(
            "get-deploy-error",
            {"iflow_id": iflow_id, "artifact_id": iflow_id, "artifact_name": iflow_id, "name": iflow_id},
        )
        if deploy_error.get("success") and deploy_error.get("output", "").strip():
            return str(deploy_error["output"]).strip()
        if self.error_fetcher:
            for ref in (iflow_id, iflow_id.replace("_", "%5F")):
                detail = await self.error_fetcher.fetch_runtime_artifact_error_detail(ref)
                if detail:
                    return detail
            artifact_detail = await self.error_fetcher.fetch_runtime_artifact_detail(iflow_id)
            status     = artifact_detail.get("Status") or artifact_detail.get("DeployState") or ""
            error_info = artifact_detail.get("ErrorInformation") or artifact_detail.get("Description") or ""
            if status or error_info:
                return f"Status={status} ErrorInformation={error_info}".strip()
        return (
            f"Could not retrieve deploy error detail for '{iflow_id}'. "
            f"Check SAP CPI monitoring: {_SAP_TENANT}/itspaces/shell/monitor/messages"
        )

    # ── _get_fix_operation ────────────────────────────────────────────────────

    async def _get_fix_operation(
        self,
        iflow_id: str,
        error_type: str,
        error_message: str,
        proposed_fix: str,
        root_cause: str,
        affected_component: str,
        original_xml: str,
        pattern_history: str,
        sap_notes: str,
        error_type_guidance: str,
    ) -> List[Dict]:
        """
        Phase 1 — ask the diagnosis-only agent to return a structured fix operation.
        The iFlow XML is injected directly so the agent rarely needs to call get-iflow.
        Returns a list of operation dicts (may be empty if parsing fails).
        """
        agent = self._diagnosis_agent
        if agent is None:
            logger.debug("[FIX] No diagnosis agent — skipping structured path.")
            return []

        prompt = FIX_OPERATION_PROMPT_TEMPLATE.format(
            iflow_id=iflow_id,
            error_type=error_type,
            error_message=(error_message or "")[:3000],
            proposed_fix=proposed_fix or "",
            root_cause=root_cause or "",
            affected_component=affected_component or "",
            original_xml=original_xml or "",
            pattern_history=pattern_history or "",
            sap_notes=sap_notes or "",
            error_type_guidance=error_type_guidance or "",
        )
        try:
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"recursion_limit": 6},
                ),
                timeout=240.0,
            )
            final_msg = result["messages"][-1]
            answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            clean     = re.sub(r"```(?:json)?|```", "", answer).strip()
            parsed    = json.loads(clean)
            ops: List[Dict] = parsed if isinstance(parsed, list) else [parsed]
            logger.info(
                "[FIX] Structured operation(s) from diagnosis agent: %d op(s) for iflow=%s",
                len(ops), iflow_id,
            )
            return ops
        except asyncio.TimeoutError:
            logger.warning("[FIX] Diagnosis agent timed out for iflow=%s — falling back.", iflow_id)
        except Exception as exc:
            logger.warning("[FIX] _get_fix_operation failed for iflow=%s: %s", iflow_id, exc)
        return []

    # ── _apply_structured_fix ─────────────────────────────────────────────────

    async def _apply_structured_fix(
        self,
        iflow_id: str,
        operations: List[Dict],
        original_xml: str,
        original_filepath: str,
    ) -> Dict[str, Any]:
        """
        Phase 2 — apply the structured operations via the patcher, then call
        update-iflow and deploy-iflow directly (no LLM free-XML rewriting).
        Returns a result dict compatible with evaluate_fix_result output.
        """
        from core.validators import _check_iflow_xml  # noqa: PLC0415

        # Apply all operations via the patcher
        try:
            patched_xml = apply_fix_operations(original_xml, operations)
        except PatcherError as exc:
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "patcher",
                "summary": f"Structured patcher failed: {exc}",
                "technical_details": str(exc),
            }

        # Validate patched XML before touching SAP
        errors = _check_iflow_xml(original_xml, patched_xml)
        if errors:
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "validation",
                "summary": f"Patched XML failed structural validation: {errors[0]}",
                "technical_details": " | ".join(errors),
            }

        # Call update-iflow directly
        update_result = await self._mcp.execute_integration_tool(
            "update-iflow",
            {"id": iflow_id, "files": [{"filepath": original_filepath, "content": patched_xml}]},
        )
        update_out = str(update_result.get("output", ""))
        if not self._update_succeeded(update_out):
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "update",
                "summary": f"update-iflow failed after structured patch: {update_out[:200]}",
                "technical_details": update_out,
            }

        # Call deploy-iflow directly
        deploy_result = await self._mcp.execute_integration_tool(
            "deploy-iflow", {"id": iflow_id}
        )
        deploy_out = str(deploy_result.get("output", ""))
        deploy_ok  = self._deploy_succeeded(deploy_out)

        op_summary = "; ".join(
            f"{op.get('change_type')} '{op.get('field')}' on '{op.get('target_component')}'"
            for op in operations
        )
        if deploy_ok:
            logger.info("[FIX] Structured fix succeeded: iflow=%s ops=[%s]", iflow_id, op_summary)
            return {
                "success": True, "fix_applied": True, "deploy_success": True,
                "failed_stage": None,
                "summary": f"Structured fix applied and deployed. Changes: {op_summary}",
                "technical_details": op_summary,
            }
        return {
            "success": False, "fix_applied": True, "deploy_success": False,
            "failed_stage": "deploy",
            "summary": f"Patch applied but deploy failed: {deploy_out[:200]}",
            "technical_details": deploy_out,
        }

    # ── ask_fix_and_deploy ────────────────────────────────────────────────────

    async def ask_fix_and_deploy(
        self,
        iflow_id: str,
        error_message: str,
        proposed_fix: str,
        root_cause: str,
        error_type: str,
        affected_component: str,
        user_id: str,
        session_id: str,
        timestamp: str,
        progress_fn: Optional[Callable] = None,
        message_guid: str = "",
    ) -> Dict[str, Any]:
        agent = self._agent or self._mcp.agent
        if agent is None:
            raise RuntimeError("MCP agent not ready.")

        missing_tools = self._mcp.validate_required_tools(
            "integration_suite", ["get-iflow", "update-iflow", "deploy-iflow"]
        )
        if missing_tools:
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "tool_validation",
                "technical_details": f"Missing required MCP tools: {', '.join(missing_tools)}",
                "summary": "Fix execution cannot start — required Integration Suite MCP tools are unavailable.",
                "steps": [],
            }

        # Pre-flight unlock
        if iflow_id:
            unlock_result = await self._try_unlock_iflow_via_api(iflow_id)
            if unlock_result["success"]:
                logger.info("[FIX_DEPLOY] Pre-unlock cleared checkout lock for '%s'", iflow_id)

        # Historical pattern hint
        from agents.classifier_agent import ClassifierAgent  # noqa: PLC0415
        _sig      = ClassifierAgent.error_signature(iflow_id, error_type or "UNKNOWN", error_message)
        _patterns = get_similar_patterns(_sig)
        pattern_history = ""
        if _patterns:
            best = _patterns[0]
            pattern_history = (
                f"\n=== HISTORICAL FIX (applied {best.get('applied_count', 1)}x, outcome=SUCCESS) ===\n"
                f"Known fix that worked before: {best.get('fix_applied', '')}\n"
                f"Root cause at the time:       {best.get('root_cause', '')}\n"
                f"Use this as a reference — apply the same change if the root cause matches.\n"
            )

        # SAP Notes from vector store — gives the fixer knowledge base context
        _vs = get_vector_store()
        _notes = _vs.retrieve_relevant_notes(error_message or "", error_type or "", iflow_id, limit=3)
        sap_notes = _vs.format_notes_for_prompt(_notes) if _notes else ""
        if sap_notes:
            logger.info("[FIX_DEPLOY] Vector store returned %d SAP note(s) for iflow=%s", len(_notes), iflow_id)

        error_type_guidance = ERROR_TYPE_FIX_GUIDANCE.get(error_type or "", "")

        # ── Pre-fetch iFlow XML: shared by patcher, validator, and rollback ──
        _original_xml: Optional[str] = None
        _original_filepath: Optional[str] = None
        try:
            _get_result = await self._mcp.execute_integration_tool("get-iflow", {"id": iflow_id})
            if _get_result.get("success"):
                _out     = _get_result.get("output", "")
                _out_str = _out if isinstance(_out, str) else json.dumps(_out)
                _fp, _xml = _extract_iflow_file(_out_str)
                if _fp and _xml:
                    _original_filepath = _fp
                    _original_xml      = _xml
                    _fix_ctx.set({"filepath": _fp, "xml": _xml})
                    logger.debug(
                        "[FIX_DEPLOY] Snapshot captured: filepath=%s xml_len=%d",
                        _fp, len(_xml),
                    )
                elif _out_str:
                    _original_xml = _out_str
        except Exception as _pre_exc:
            logger.debug("[FIX_DEPLOY] Pre-fetch for rollback/snapshot failed (non-fatal): %s", _pre_exc)

        # ── PHASE 1: Structured fix path (programmatic XML surgery) ─────────
        if _original_xml and _original_filepath:
            _operations   = await self._get_fix_operation(
                iflow_id=iflow_id,
                error_type=error_type or "UNKNOWN",
                error_message=error_message or "",
                proposed_fix=proposed_fix or "",
                root_cause=root_cause or "",
                affected_component=affected_component or "",
                original_xml=_original_xml,
                pattern_history=pattern_history,
                sap_notes=sap_notes,
                error_type_guidance=error_type_guidance,
            )
            _applicable = [op for op in _operations if op.get("change_type") != "structural"]
            if _applicable:
                _operations = _applicable
                _struct_result = await self._apply_structured_fix(
                    iflow_id=iflow_id,
                    operations=_operations,
                    original_xml=_original_xml,
                    original_filepath=_original_filepath,
                )
                if _struct_result.get("success"):
                    self._mcp.update_memory(session_id, f"Fix {iflow_id}", _struct_result["summary"])
                    logger.info("[FIX] Structured path succeeded: iflow=%s", iflow_id)
                    return {**_struct_result, "steps": []}
                logger.warning(
                    "[FIX] Structured path failed (stage=%s) — falling back to free-XML agent: %s",
                    _struct_result.get("failed_stage"), _struct_result.get("summary"),
                )

        # ── PHASE 2: Free-XML fallback (LLM rewrites full XML) ──────────────
        _comp = (affected_component or "").strip()
        targeted_component_hint = (
            f'            → CONFIRMED failing component: "{_comp}"\n'
            f'              Search for id="{_comp}" in the XML and go directly to this step.\n'
            f"              Limit your change to this component — do not modify anything else."
            if _comp and _comp.lower() != "unknown"
            else "            → No specific component confirmed — infer from error type and iFlow structure."
        )
        # Only inject rules relevant to the error type to keep the prompt focused
        _et = (error_type or "UNKNOWN").upper()
        _groovy_relevant = {"MAPPING_ERROR", "DATA_VALIDATION", "UNKNOWN_ERROR"}
        _struct_relevant = {"MAPPING_ERROR", "DATA_VALIDATION", "UNKNOWN_ERROR", "AUTH_CONFIG_ERROR"}
        _groovy_rules_ctx = CPI_IFLOW_GROOVY_RULES if _et in _groovy_relevant else ""
        _xml_patterns_ctx = CPI_IFLOW_XML_PATTERNS if _et in _struct_relevant else ""
        prompt = FIX_AND_DEPLOY_PROMPT_TEMPLATE.format(
            iflow_id=iflow_id,
            error_type=error_type or "UNKNOWN",
            error_message=(error_message or "")[:3000],
            message_guid=message_guid or "N/A",
            root_cause=root_cause or error_message,
            proposed_fix=proposed_fix or f"Investigate and fix the error: {error_message}",
            affected_component=affected_component or "unknown",
            targeted_component_hint=targeted_component_hint,
            pattern_history=pattern_history,
            sap_notes=sap_notes,
            error_type_guidance=error_type_guidance,
            groovy_rules=_groovy_rules_ctx,
            iflow_xml_patterns=_xml_patterns_ctx,
        )
        messages  = [{"role": "user", "content": prompt}]
        tracker   = TestExecutionTracker(user_id, f"fix:{iflow_id}", timestamp)
        logger_cb = StepLogger(tracker, progress_fn=progress_fn)

        answer: str = ""
        evaluation: Dict[str, Any] = {
            "success": False, "fix_applied": False, "deploy_success": False,
            "failed_stage": "agent", "summary": "No result from fix agent.",
            "technical_details": "", "failed_steps": [],
        }
        # ── ATTEMPT 2: Single LLM full-rewrite ──────────────────────────────────
        try:
            _result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": messages},
                    config={"callbacks": [logger_cb], "recursion_limit": 18},
                ),
                timeout=600.0,
            )
        except asyncio.TimeoutError:
            diagnosis = self._diagnose_timeout(logger_cb.steps, iflow_id)
            logger.error("[FIX_DEPLOY] agent timed out | iflow=%s stage=%s",
                         iflow_id, diagnosis["failed_stage"])
            if diagnosis["failed_stage"] == "deploy" and iflow_id:
                poll = await self._poll_deploy_status(iflow_id)
                if poll["deploy_confirmed"]:
                    return {
                        **diagnosis,
                        "success": True, "deploy_success": True, "failed_stage": None,
                        "summary": (
                            f"iFlow '{iflow_id}' updated and deployed successfully. "
                            f"Confirmed via runtime status poll (status: {poll['status']})."
                        ),
                        "technical_details": (
                            f"deploy-iflow SSE timed out; runtime poll confirmed '{poll['status']}'."
                        ),
                        "steps": logger_cb.steps,
                    }
            return {**diagnosis, "steps": logger_cb.steps}
        except Exception as exc:
            logger.error("[FIX_DEPLOY] agent error: %s", exc)
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "agent",
                "technical_details": str(exc),
                "summary": "Fix execution failed while generating or applying the iFlow change plan.",
                "steps": logger_cb.steps,
            }

        _final_msg = _result["messages"][-1]
        answer     = _final_msg.content if hasattr(_final_msg, "content") else str(_final_msg)
        evaluation = self.evaluate_fix_result(logger_cb.steps, answer)

        # Validation retry: one targeted retry when the agent skipped validate_iflow_xml
        # or validate_iflow_xml returned errors before update-iflow was called.
        if not evaluation.get("success") and evaluation.get("failed_stage") != "locked":
            _val_skipped = not any(
                "validate_iflow_xml" in str(s.get("tool", "")) for s in logger_cb.steps
            )
            _val_error = next(
                (str(s.get("output", "")) for s in logger_cb.steps
                 if "validate_iflow_xml" in str(s.get("tool", "")) and "ERRORS:" in str(s.get("output", ""))),
                "",
            )
            if _val_skipped or _val_error:
                _val_hint = (
                    "You skipped validate_iflow_xml. Call it BEFORE update-iflow."
                    if _val_skipped else
                    f"validate_iflow_xml errors: {_val_error[:300]}. Fix these before calling update-iflow."
                )
                _logger_cb_val = StepLogger(
                    TestExecutionTracker(user_id, f"fix:{iflow_id}:val_retry", timestamp),
                    progress_fn=progress_fn,
                )
                try:
                    _result_val = await asyncio.wait_for(
                        agent.ainvoke(
                            {"messages": [
                                *messages,
                                {"role": "assistant", "content": answer},
                                {"role": "user", "content": (
                                    f"VALIDATION REQUIRED: {_val_hint}\n"
                                    f"Call get-iflow for '{iflow_id}', apply the fix, "
                                    f"call validate_iflow_xml (must return VALID), "
                                    f"then update-iflow, then deploy-iflow."
                                )},
                            ]},
                            config={"callbacks": [_logger_cb_val], "recursion_limit": 18},
                        ),
                        timeout=600.0,
                    )
                    _answer_val = _result_val["messages"][-1]
                    _answer_val = _answer_val.content if hasattr(_answer_val, "content") else str(_answer_val)
                    _eval_val   = self.evaluate_fix_result(_logger_cb_val.steps, _answer_val)
                    if _eval_val.get("success") or _eval_val.get("fix_applied"):
                        evaluation = _eval_val
                        logger_cb  = _logger_cb_val
                        answer     = _answer_val
                        logger.info("[FIX_DEPLOY] Validation retry succeeded: iflow=%s", iflow_id)
                except Exception as _val_exc:
                    logger.warning("[FIX_DEPLOY] Validation retry failed: %s", _val_exc)

        # ── Locked mid-run: unlock + one retry ───────────────────────────────
        if evaluation.get("failed_stage") == "locked" and iflow_id:
            logger.info("[FIX_DEPLOY] Locked mid-run — attempting unlock + retry: %s", iflow_id)
            unlock_retry = await self._try_unlock_iflow_via_api(iflow_id)
            if unlock_retry["success"]:
                tracker2   = TestExecutionTracker(user_id, f"fix_retry:{iflow_id}", timestamp)
                logger_cb2 = StepLogger(tracker2, progress_fn=progress_fn)
                try:
                    result2    = await asyncio.wait_for(
                        agent.ainvoke(
                            {"messages": messages},
                            config={"callbacks": [logger_cb2], "recursion_limit": 12},
                        ),
                        timeout=480.0,
                    )
                    answer2    = result2["messages"][-1]
                    answer2    = answer2.content if hasattr(answer2, "content") else str(answer2)
                    eval2      = self.evaluate_fix_result(logger_cb2.steps, answer2)
                    if eval2.get("success") or eval2.get("failed_stage") != "locked":
                        evaluation = eval2
                        logger_cb  = logger_cb2
                        answer     = answer2
                except Exception as retry_exc:
                    logger.error("[FIX_DEPLOY] Retry after unlock failed: %s", retry_exc)
            else:
                evaluation["summary"] = (
                    f"Fix could not be applied — iFlow is locked by an active browser edit session.\n"
                    f"Technical detail: {unlock_retry['message']}"
                )
                evaluation["technical_details"] = unlock_retry["message"]

        # ── Deploy-error self-correction passes (up to 3) ────────────────────
        if (
            iflow_id
            and evaluation.get("fix_applied")
            and not evaluation.get("deploy_success")
            and evaluation.get("failed_stage") == "deploy"
        ):
            _pre_poll = await self._poll_deploy_status(iflow_id, polls=6, interval=15.0)
            if _pre_poll["deploy_confirmed"]:
                evaluation.update({
                    "success": True, "deploy_success": True, "failed_stage": None,
                    "summary": (
                        f"iFlow '{iflow_id}' updated and deployed successfully. "
                        f"Confirmed via runtime poll (status: {_pre_poll['status']})."
                    ),
                    "technical_details": (
                        f"deploy-iflow response was unrecognised; runtime poll confirmed '{_pre_poll['status']}'."
                    ),
                })
                deploy_errors = None
            else:
                deploy_errors = await self.get_deploy_error_details(iflow_id)

            if deploy_errors:
                for _pass in range(1, 4):
                    correction_prompt = (
                        f"DEPLOY CORRECTION (pass {_pass}/3) — the previous fix for iFlow '{iflow_id}' "
                        f"was uploaded but deployment failed with:\n\n{deploy_errors}\n\n"
                        f"Execute in order:\n"
                        f"1. Call get-iflow with ID '{iflow_id}'.\n"
                        f"2. Fix ONLY the validation errors listed above — preserve all else.\n"
                        f"3. Call update-iflow with the corrected iFlow.\n"
                        f"4. Call deploy-iflow with iFlow ID '{iflow_id}'.\n\n"
                        f"Return EXACTLY this JSON (no markdown):\n"
                        f'{{"fix_applied": true, "deploy_success": true/false, '
                        f'"summary": "<what was corrected and deploy outcome>"}}'
                    )
                    tracker_c  = TestExecutionTracker(user_id, f"fix_correction_p{_pass}:{iflow_id}", timestamp)
                    logger_cb_c = StepLogger(tracker_c, progress_fn=progress_fn)
                    try:
                        result_c  = await asyncio.wait_for(
                            agent.ainvoke(
                                {"messages": [{"role": "user", "content": correction_prompt}]},
                                config={"callbacks": [logger_cb_c], "recursion_limit": 12},
                            ),
                            timeout=480.0,
                        )
                        answer_c  = result_c["messages"][-1]
                        answer_c  = answer_c.content if hasattr(answer_c, "content") else str(answer_c)
                        eval_c    = self.evaluate_fix_result(logger_cb_c.steps, answer_c)
                        if eval_c.get("deploy_success"):
                            evaluation = eval_c
                            logger_cb  = logger_cb_c
                            answer     = answer_c
                            break
                        else:
                            evaluation["failed_stage"]     = "deploy_validation"
                            evaluation["technical_details"] = (
                                f"Original deploy errors: {deploy_errors[:600]}\n"
                                f"Pass {_pass}: {eval_c.get('technical_details', '')}"
                            )
                            new_errors = await self.get_deploy_error_details(iflow_id)
                            if new_errors:
                                deploy_errors = new_errors
                    except Exception as corr_exc:
                        logger.error("[FIX_DEPLOY] Self-correction pass %d error: %s", _pass, corr_exc)
                        break

            # ── Fixed XML preserved — signal pending deploy rather than rolling back ──
            # Rolling back would restore the broken original XML and guarantee the same error
            # fires again. Instead, keep the fixed design-time XML and let the orchestrator
            # schedule a deploy-only retry via FIX_APPLIED_PENDING_VERIFICATION status.
            if (
                iflow_id
                and evaluation.get("fix_applied")
                and not evaluation.get("deploy_success")
            ):
                logger.warning(
                    "[FIX_DEPLOY] Deploy failed after all passes — fixed XML preserved for retry deploy: '%s'",
                    iflow_id,
                )
                evaluation["failed_stage"] = "pending_deploy"
                evaluation["summary"] += (
                    " Fixed XML was uploaded but could not be deployed automatically. "
                    "Use 'Deploy Only' to retry deployment without re-applying the fix."
                )

        if not evaluation.get("success"):
            logger.error(
                "[FIX_FAILED] iflow=%s stage=%s reason=%s",
                iflow_id,
                evaluation.get("failed_stage", "unknown"),
                evaluation.get("summary", "")[:200],
            )
        self._mcp.update_memory(session_id, f"Fix {iflow_id}", evaluation["summary"])
        logger.info(
            "[FIX_DEPLOY] iflow=%s fix_applied=%s deploy_success=%s",
            iflow_id, evaluation["fix_applied"], evaluation["deploy_success"],
        )
        return {**evaluation, "steps": logger_cb.steps, "raw_answer": answer}

    # ── ask_deploy_only ───────────────────────────────────────────────────────

    async def ask_deploy_only(
        self, iflow_id: str, user_id: str, timestamp: str
    ) -> Dict[str, Any]:
        agent = self._agent or self._mcp.agent
        if agent is None:
            raise RuntimeError("MCP agent not ready.")

        missing = self._mcp.validate_required_tools("integration_suite", ["deploy-iflow"])
        if missing:
            return {
                "success": False, "fix_applied": True, "deploy_success": False,
                "failed_stage": "tool_validation",
                "technical_details": f"Missing tool: {', '.join(missing)}",
                "summary": "Deploy-only retry aborted — deploy-iflow tool unavailable.",
                "steps": [],
            }
        tracker   = TestExecutionTracker(user_id, f"deploy_only:{iflow_id}", timestamp)
        logger_cb = StepLogger(tracker)
        prompt = (
            f"DEPLOY ONLY — the iFlow '{iflow_id}' was already updated successfully.\n"
            f"Call deploy-iflow tool ONCE with iFlow ID: \"{iflow_id}\".\n"
            f"VERIFY the response contains deployStatus \"Success\" or \"DEPLOYED\".\n"
            f"Return ONLY valid JSON (no markdown):\n"
            f'{{"fix_applied": true, "deploy_success": true/false, '
            f'"deploy_response": "<raw response>", "summary": "<one sentence>"}}'
        )
        try:
            result      = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"callbacks": [logger_cb], "recursion_limit": 6},
                ),
                timeout=300.0,
            )
            final_msg   = result["messages"][-1]
            answer      = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            eval_result = self.evaluate_fix_result(logger_cb.steps, answer)
            eval_result["fix_applied"] = True
            return {**eval_result, "steps": logger_cb.steps}
        except asyncio.TimeoutError:
            logger.error("[DEPLOY_ONLY] timed out for %s", iflow_id)
            poll = await self._poll_deploy_status(iflow_id, polls=4, interval=15.0)
            if poll["deploy_confirmed"]:
                return {
                    "success": True, "fix_applied": True, "deploy_success": True,
                    "failed_stage": None,
                    "summary": f"Deploy confirmed via runtime poll (status: {poll['status']}).",
                    "steps": logger_cb.steps,
                }
            return {
                "success": False, "fix_applied": True, "deploy_success": False,
                "failed_stage": "pending_deploy",
                "summary": "Deploy-only timed out. Fixed XML is in place — retry deployment.",
                "steps": logger_cb.steps,
            }
        except Exception as exc:
            logger.error("[DEPLOY_ONLY] error for %s: %s", iflow_id, exc)
            return {
                "success": False, "fix_applied": True, "deploy_success": False,
                "failed_stage": "deploy", "technical_details": str(exc),
                "summary": f"Deploy-only retry failed: {exc}", "steps": logger_cb.steps,
            }

    # ── apply_fix (thin wrapper used by orchestrator) ─────────────────────────

    async def apply_fix(
        self, incident: Dict[str, Any], rca: Dict[str, Any], progress_fn=None
    ) -> Dict[str, Any]:
        return await self.ask_fix_and_deploy(
            iflow_id=(
                incident.get("designtime_artifact_id")
                or incident.get("iflow_id", "")
            ),
            error_message=incident.get("error_message", ""),
            proposed_fix=rca.get("proposed_fix", ""),
            root_cause=rca.get("root_cause", ""),
            error_type=rca.get("error_type", incident.get("error_type", "UNKNOWN")),
            affected_component=rca.get("affected_component", ""),
            user_id="system_autofix",
            session_id=f"autofix_{incident.get('incident_id', 'unknown')}",
            timestamp=get_hana_timestamp(),
            progress_fn=progress_fn,
            message_guid=incident.get("message_guid", ""),
        )

    # ── determine_post_fix_status ─────────────────────────────────────────────

    @staticmethod
    def determine_post_fix_status(
        fix_success: bool,
        policy: Dict[str, Any],
        retry_result: Optional[Dict[str, Any]] = None,
        human_approved: bool = False,
        failed_stage: str = "",
    ) -> str:
        if not fix_success:
            if failed_stage == "pending_deploy":
                return "FIX_APPLIED_PENDING_VERIFICATION"
            if failed_stage in ("deploy", "deploy_validation"):
                return "FIX_FAILED_DEPLOY"
            if failed_stage in ("update", "get", "locked", "patcher", "validation"):
                return "FIX_FAILED_UPDATE"
            if failed_stage in ("agent", "tool_validation"):
                return "FIX_FAILED"
            return "FIX_FAILED"
        if policy.get("action") == "RETRY":
            if retry_result and (retry_result.get("success") or retry_result.get("skipped")):
                return "HUMAN_INITIATED_FIX" if human_approved else "FIX_VERIFIED"
            return "FIX_DEPLOYED"
        return "HUMAN_INITIATED_FIX" if human_approved else "FIX_VERIFIED"

    # ── capture pre-fix iFlow snapshot ───────────────────────────────────────

    async def capture_snapshot(self, iflow_id: str, incident_id: str) -> None:
        """
        Download the current iFlow before any modification and:
          1. Store the snapshot in the DB (iflow_snapshot_before).
          2. Set the _fix_ctx ContextVar so update-iflow calls are validated
             against this snapshot by the iFlow XML validator.
        """
        try:
            get_tool = self._mcp.get_mcp_tool("integration_suite", "get-iflow")
            if not get_tool:
                return
            snapshot_raw = await get_tool.ainvoke({"id": iflow_id})
            snapshot_str = json.dumps(snapshot_raw) if not isinstance(snapshot_raw, str) else snapshot_raw
            from db.database import update_incident as _update  # noqa: PLC0415
            _update(incident_id, {"iflow_snapshot_before": snapshot_str})
            logger.info("[FIX] iFlow snapshot captured for %s (%d chars)", iflow_id, len(snapshot_str))
            orig_fp, orig_xml = _extract_iflow_file(snapshot_str)
            if orig_fp:
                _fix_ctx.set({"filepath": orig_fp, "xml": orig_xml})
                logger.info("[VALIDATOR] Fix context set: filepath='%s' xml_len=%d", orig_fp, len(orig_xml))
        except Exception as exc:
            logger.warning("[FIX] Could not capture iFlow snapshot for %s: %s", iflow_id, exc)
