"""
agents/fix_applier.py
=====================
FixApplier — executes the actual SAP API calls (or evaluates the LLM's
tool-call history) and returns a standardised ApplyResult.

Exports:
  ApplyResult  — mutable dataclass matching evaluate_fix_result() output shape
  FixApplier   — async apply(ctx, patch, validate_result) -> ApplyResult
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents.fix_context import FixContext
from agents.fix_generator import PatchSpec
from agents.fix_validator import PreValidateResult

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    success: bool
    fix_applied: bool
    deploy_success: bool
    failed_stage: Optional[str]
    summary: str
    technical_details: str
    steps: List[Dict]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success":          self.success,
            "fix_applied":      self.fix_applied,
            "deploy_success":   self.deploy_success,
            "failed_stage":     self.failed_stage,
            "summary":          self.summary,
            "technical_details": self.technical_details,
            "steps":            self.steps,
        }


class FixApplier:
    """
    Executes structured patch (direct MCP calls) or evaluates the LLM
    agent's tool-call history for free-XML mode.
    """

    def __init__(self, mcp) -> None:
        self._mcp = mcp

    # ── public entry point ────────────────────────────────────────────────────

    async def apply(
        self,
        ctx: FixContext,
        patch: PatchSpec,
        validate_result: PreValidateResult,
        progress_fn: Optional[Callable] = None,
    ) -> ApplyResult:
        # Pre-validation gate
        if not validate_result.passed:
            logger.warning(
                "[FixApplier] pre-validation blocked apply: iflow=%s errors=%s",
                ctx.iflow_id, validate_result.errors,
            )
            return ApplyResult(
                success=False,
                fix_applied=False,
                deploy_success=False,
                failed_stage="validation",
                summary=f"Pre-validation failed: {'; '.join(validate_result.errors)}",
                technical_details=" | ".join(validate_result.errors),
                steps=patch.steps,
            )

        if patch.mode == "structured":
            result = await self._apply_structured(ctx, patch, validate_result)
        else:
            result = self._evaluate_free_xml(patch)

        # After any deploy claimed success, poll SAP until iFlow reaches STARTED state.
        # SAP CPI deploys asynchronously — 202 accepted ≠ iFlow running.
        if result.deploy_success:
            result = await self._poll_for_started(ctx, result, progress_fn=progress_fn)

        return result

    # ── structured mode ───────────────────────────────────────────────────────

    async def _apply_structured(
        self,
        ctx: FixContext,
        patch: PatchSpec,
        vr: PreValidateResult,
    ) -> ApplyResult:
        """
        Call update-iflow and deploy-iflow directly — no LLM involved.
        The patched_xml has already been validated by FixValidator.
        """
        # ── Component replacement shortcut ────────────────────────────────────
        for op in patch.operations:
            if op.get("change_type") == "component_replace":
                merged_xml = op.get("merged_xml", "")
                if not merged_xml:
                    continue
                from core.validators import _check_iflow_xml  # noqa: PLC0415
                errors = _check_iflow_xml(ctx.original_xml, merged_xml)
                if errors:
                    logger.warning(
                        "[FixApplier] Component replace validation failed: %s", errors
                    )
                    break
                logger.info(
                    "[FixApplier] Applying component replacement for iflow=%s", ctx.iflow_id
                )
                logger.info(
                    "[FixApplier] component-replace update-iflow: iflow=%s filepath=%r xml_len=%d",
                    ctx.iflow_id, ctx.original_filepath, len(merged_xml),
                )
                update_result = await self._mcp.execute_integration_tool(
                    "update-iflow",
                    {
                        "id": ctx.iflow_id,
                        "files": [{"filepath": ctx.original_filepath, "content": merged_xml}],
                    },
                )
                _cr_out = str(update_result.get("output", ""))
                logger.info(
                    "[FixApplier] component-replace update-iflow response: iflow=%s output_len=%d output=%.200s",
                    ctx.iflow_id, len(_cr_out), _cr_out,
                )
                _cr_verified = False
                if not _cr_out.strip() and not _cr_out.startswith("ERROR") and not _cr_out.startswith("VALIDATION"):
                    _vr = await self._mcp.execute_integration_tool("get-iflow", {"id": ctx.iflow_id})
                    if _vr.get("success") and _vr.get("output", ""):
                        logger.info(
                            "[FixApplier] component-replace empty body verified via get-iflow: iflow=%s",
                            ctx.iflow_id,
                        )
                        _cr_verified = True
                update_ok = _cr_verified or self._update_succeeded(_cr_out)
                if not update_ok:
                    logger.warning(
                        "[FixApplier] Component replace update failed for iflow=%s", ctx.iflow_id
                    )
                    return ApplyResult(
                        success=False,
                        fix_applied=False,
                        deploy_success=False,
                        failed_stage="update",
                        summary=f"Component replacement update failed for {ctx.iflow_id}",
                        technical_details=_cr_out[:300],
                        steps=[],
                    )
                deploy_result = await self._mcp.execute_integration_tool(
                    "deploy-iflow", {"id": ctx.iflow_id}
                )
                _cr_deploy_out = str(deploy_result.get("output", ""))
                deploy_ok = self._deploy_succeeded(_cr_deploy_out)
                # SAP CPI returns HTTP 202 with empty body for async deploys
                if not deploy_ok and not _cr_deploy_out.strip() and not _cr_deploy_out.startswith("ERROR"):
                    logger.info(
                        "[FixApplier] component-replace deploy-iflow empty body — treating as 202 async success: iflow=%s",
                        ctx.iflow_id,
                    )
                    deploy_ok = True
                return ApplyResult(
                    success=deploy_ok,
                    fix_applied=True,
                    deploy_success=deploy_ok,
                    failed_stage=None if deploy_ok else "deploy",
                    summary=(
                        f"Component '{ctx.affected_component}' replaced with reference "
                        f"'{op.get('reference_name', '')}' and "
                        f"{'deployed successfully' if deploy_ok else 'deploy failed'}."
                    ),
                    technical_details="",
                    steps=[],
                )

        # Call update-iflow
        logger.info(
            "[FixApplier] update-iflow preflight: iflow=%s filepath=%r xml_len=%d",
            ctx.iflow_id, ctx.original_filepath, len(vr.patched_xml),
        )
        update_result = await self._mcp.execute_integration_tool(
            "update-iflow",
            {"id": ctx.iflow_id,
             "files": [{"filepath": ctx.original_filepath, "content": vr.patched_xml}]},
        )
        update_out = str(update_result.get("output", ""))
        logger.info(
            "[FixApplier] update-iflow response: iflow=%s output_len=%d output=%.300s",
            ctx.iflow_id, len(update_out), update_out,
        )
        # SAP BTP sometimes returns HTTP 200 with an empty body.
        # Verify via get-iflow before treating an empty response as failure.
        _update_verified = False
        if not update_out.strip() and not update_out.startswith("ERROR") and not update_out.startswith("VALIDATION"):
            _verify = await self._mcp.execute_integration_tool("get-iflow", {"id": ctx.iflow_id})
            if _verify.get("success") and _verify.get("output", ""):
                logger.info(
                    "[FixApplier] update-iflow empty body — iFlow accessible via get-iflow: iflow=%s",
                    ctx.iflow_id,
                )
                _update_verified = True
        if not _update_verified and not self._update_succeeded(update_out):
            logger.warning(
                "[FixApplier] update-iflow failed: iflow=%s output=%.400s",
                ctx.iflow_id, update_out,
            )
            return ApplyResult(
                success=False,
                fix_applied=False,
                deploy_success=False,
                failed_stage="update",
                summary=f"update-iflow failed after structured patch: {update_out[:200]}",
                technical_details=update_out,
                steps=[],
            )

        # Call deploy-iflow
        deploy_result = await self._mcp.execute_integration_tool(
            "deploy-iflow", {"id": ctx.iflow_id}
        )
        deploy_out = str(deploy_result.get("output", ""))
        deploy_ok  = self._deploy_succeeded(deploy_out)
        # SAP CPI returns HTTP 202 with empty body for async deploys
        if not deploy_ok and not deploy_out.strip() and not deploy_out.startswith("ERROR"):
            logger.info(
                "[FixApplier] deploy-iflow empty body — treating as 202 async success: iflow=%s",
                ctx.iflow_id,
            )
            deploy_ok = True
        logger.info(
            "[FixApplier] structured deploy result: iflow=%s deploy_ok=%s output=%.300s",
            ctx.iflow_id, deploy_ok, deploy_out,
        )

        op_summary = "; ".join(
            f"{op.get('change_type')} '{op.get('field')}' on '{op.get('target_component')}'"
            for op in patch.operations
        )
        if deploy_ok:
            logger.info("[FixApplier] Structured fix succeeded: iflow=%s ops=[%s]",
                        ctx.iflow_id, op_summary)
            return ApplyResult(
                success=True,
                fix_applied=True,
                deploy_success=True,
                failed_stage=None,
                summary=f"Structured fix applied and deployed. Changes: {op_summary}",
                technical_details=op_summary,
                steps=[],
            )
        return ApplyResult(
            success=False,
            fix_applied=True,
            deploy_success=False,
            failed_stage="deploy",
            summary=f"Patch applied but deploy failed: {deploy_out[:200]}",
            technical_details=deploy_out,
            steps=[],
        )

    # ── free_xml mode ─────────────────────────────────────────────────────────

    def _evaluate_free_xml(self, patch: PatchSpec) -> ApplyResult:
        """
        The LLM already called update-iflow and deploy-iflow during generation.
        Evaluate the tool-call steps and return the result.
        """
        # Sentinel answers from FixGenerator signal early failures
        raw = patch.raw_answer or ""
        if raw.startswith("__NO_TOOL_CALLS__"):
            logger.warning("[FixApplier] free_xml sentinel: NO_TOOL_CALLS steps=%d", len(patch.steps))
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="no_tool_calls",
                summary="LLM did not execute any tools.",
                technical_details="",
                steps=patch.steps,
            )
        if raw.startswith("__TIMEOUT__"):
            logger.warning("[FixApplier] free_xml sentinel: TIMEOUT steps=%d", len(patch.steps))
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="timeout",
                summary="Fix agent timed out.",
                technical_details="",
                steps=patch.steps,
            )
        if raw.startswith("__RECURSION_LIMIT__"):
            logger.warning(
                "[FixApplier] free_xml sentinel: RECURSION_LIMIT steps=%d", len(patch.steps)
            )
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="recursion_limit",
                summary="Fix agent hit LangGraph recursion limit — will retry with a simpler strategy.",
                technical_details="",
                steps=patch.steps,
            )
        if raw.startswith("__ERROR__:"):
            logger.warning(
                "[FixApplier] free_xml sentinel: AGENT_ERROR detail=%.200s",
                raw[len("__ERROR__:"):],
            )
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="agent",
                summary="Fix agent encountered an error.",
                technical_details=raw[len("__ERROR__:"):],
                steps=patch.steps,
            )
        if raw.startswith("__NO_AGENT__"):
            logger.warning("[FixApplier] free_xml sentinel: NO_AGENT")
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="agent",
                summary="Fix agent not initialised.",
                technical_details="",
                steps=patch.steps,
            )

        if raw.startswith("__VALIDATION_BLOCKED__"):
            logger.warning(
                "[FixApplier] free_xml sentinel: VALIDATION_BLOCKED steps=%d", len(patch.steps)
            )
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="validation_blocked",
                summary=(
                    "Fix was blocked: the agent introduced a structural regression "
                    "(e.g. removed a Content-Based Router default route). "
                    "No changes were deployed. Manual review is required."
                ),
                technical_details="FATAL structural validation failure — see validate_iflow_xml step output.",
                steps=patch.steps,
            )

        result = self.evaluate_fix_result(patch.steps, raw)
        return ApplyResult(
            success=result.get("success", False),
            fix_applied=result.get("fix_applied", False),
            deploy_success=result.get("deploy_success", False),
            failed_stage=result.get("failed_stage"),
            summary=result.get("summary", ""),
            technical_details=result.get("technical_details", ""),
            steps=patch.steps,
        )

    # ── evaluate_fix_result (also used by FixAgent for retries) ──────────────

    def evaluate_fix_result(self, steps: List[Dict], answer: str) -> Dict[str, Any]:
        def compact(text: str) -> str:
            return re.sub(r"\s+", " ", str(text or "")).strip()[:500]

        update_ok         = False
        deploy_ok         = False
        update_output     = deploy_output = ""
        validation_called = False
        validation_failed = False

        logger.debug(
            "[FixApplier] evaluate_fix_result: total_steps=%d answer_len=%d",
            len(steps), len(answer),
        )

        for step in steps:
            tool_name = str(step.get("tool", ""))
            output    = str(step.get("output", ""))
            if "validate_iflow_xml" in tool_name:
                validation_called = True
                if "ERRORS:" in output:
                    validation_failed = True
                    logger.warning(
                        "[FixApplier] validate_iflow_xml returned errors but agent may still update: %s",
                        compact(output),
                    )
            elif "update_iflow" in tool_name or "update-iflow" in tool_name:
                update_output = output
                update_ok     = self._update_succeeded(output)
                if update_ok and not validation_called:
                    logger.warning(
                        "[FixApplier] update-iflow succeeded without prior validate_iflow_xml call"
                    )
                elif update_ok and validation_failed:
                    logger.error(
                        "[FixApplier] Fix rejected — XML validation errors "
                        "must be resolved before deploying"
                    )
            elif "deploy_iflow" in tool_name or "deploy-iflow" in tool_name:
                deploy_output = output
                deploy_ok     = self._deploy_succeeded(output)
                # SAP CPI returns HTTP 202 with empty body for async deploys
                if not deploy_ok and not output.strip() and not output.startswith("ERROR"):
                    logger.info(
                        "[FixApplier] deploy-iflow empty body in free_xml steps — treating as 202 async success"
                    )
                    deploy_ok = True

        logger.info(
            "[FixApplier] evaluate_fix_result summary: update_ok=%s deploy_ok=%s "
            "validation_called=%s validation_failed=%s",
            update_ok, deploy_ok, validation_called, validation_failed,
        )

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

        if validation_failed:
            return {
                "success": False, "fix_applied": False, "deploy_success": False,
                "failed_stage": "validation_error",
                "technical_details": "XML validation returned errors; fix was not accepted.",
                "summary": "Fix rejected — iFlow XML failed validation checks. The agent deployed despite errors.",
                "failed_steps": ["validate_iflow_xml"],
            }

        _validation_note = ""
        if not validation_called:
            _validation_note = " [warn: XML validation step was skipped by agent]"

        _failed_stage = "validation_warning" if not validation_called else None

        return {
            "success": True, "fix_applied": True, "deploy_success": True,
            "failed_stage": _failed_stage,
            "technical_details": _validation_note.strip(),
            "summary": f"iFlow updated and deployed successfully. {compact(answer)}{_validation_note}",
            "failed_steps": [],
        }

    # ── post-deploy polling ───────────────────────────────────────────────────

    async def _poll_for_started(
        self,
        ctx: FixContext,
        result: ApplyResult,
        progress_fn: Optional[Callable] = None,
        max_polls: int = 6,
        poll_interval: int = 15,
        initial_wait: int = 20,
    ) -> ApplyResult:
        """
        After deploy-iflow returns 202, wait for SAP CPI to finish processing then
        call get-deploy-error to confirm the outcome.

        Per MCP tool docs: empty response = no deployment error = successful.
        Non-empty response = SAP CPI deployment error text.

        Polls up to max_polls times with poll_interval seconds between each check
        (initial_wait seconds before the first check to give SAP time to process).
        """
        await asyncio.sleep(initial_wait)

        for poll_num in range(max_polls):
            elapsed = initial_wait + poll_num * poll_interval
            if progress_fn:
                try:
                    progress_fn(f"Agent: waiting for iFlow to start… ({elapsed}s)")
                except Exception:
                    pass

            deploy_error = await self._fetch_deploy_error(ctx.iflow_id)
            logger.info(
                "[FixApplier] deploy poll %d/%d iflow=%s deploy_error_len=%d error=%.150s",
                poll_num + 1, max_polls, ctx.iflow_id, len(deploy_error), deploy_error[:150],
            )

            if not deploy_error:
                # Tool docs: "If the response is empty it means there is no deployment
                # error and it was successful."
                logger.info(
                    "[FixApplier] iFlow deploy confirmed (get-deploy-error empty) after ~%ds: iflow=%s",
                    elapsed, ctx.iflow_id,
                )
                if progress_fn:
                    try:
                        progress_fn("Agent: iFlow started successfully")
                    except Exception:
                        pass
                return ApplyResult(
                    success=True,
                    fix_applied=result.fix_applied,
                    deploy_success=True,
                    failed_stage=None,
                    summary=result.summary,
                    technical_details=result.technical_details,
                    steps=result.steps,
                )

            # Non-empty = SAP reported a deployment error
            logger.warning(
                "[FixApplier] iFlow deploy FAILED (get-deploy-error non-empty) on poll %d: "
                "iflow=%s error=%.200s",
                poll_num + 1, ctx.iflow_id, deploy_error[:200],
            )
            return ApplyResult(
                success=False,
                fix_applied=result.fix_applied,
                deploy_success=False,
                failed_stage="deploy",
                summary=f"iFlow deployment failed: {deploy_error[:300]}",
                technical_details=deploy_error,
                steps=result.steps,
            )

        # Should not be reached (each poll returns immediately), but guard anyway.
        timeout_secs = initial_wait + max_polls * poll_interval
        logger.warning(
            "[FixApplier] Deploy poll loop exhausted after ~%ds: iflow=%s",
            timeout_secs, ctx.iflow_id,
        )
        return ApplyResult(
            success=False,
            fix_applied=result.fix_applied,
            deploy_success=False,
            failed_stage="deploy_timeout",
            summary=(
                f"iFlow '{ctx.iflow_id}' deployment status could not be confirmed within "
                f"{timeout_secs}s — manual check required."
            ),
            technical_details=f"Poll exhausted after {max_polls} × {poll_interval}s",
            steps=result.steps,
        )

    async def _fetch_deploy_error(self, iflow_id: str) -> str:
        try:
            r = await self._mcp.execute_integration_tool(
                "get-deploy-error", {"id": iflow_id}
            )
            return str(r.get("output", "")).strip()
        except Exception:
            return ""

    # ── static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _update_succeeded(output: str) -> bool:
        text = (output or "").lower()
        return any(s in text for s in (
            '"status":200', '"status": 200',
            '"statuscode":200', '"statuscode": 200',
            "successfully updated", "update successful", "saved successfully",
            "upload successful", "uploaded successfully",
            '"result":"ok"', '"result": "ok"',
            '"success":true', '"success": true',
            "http/1.1 200", "http/2 200", "status_code=200",
            '"version":', '"artifactcontent"',  # SAP returns version on successful update
        ))

    @staticmethod
    def _deploy_succeeded(output: str) -> bool:
        text = (output or "").lower()
        return any(s in text for s in (
            '"deploystatus":"success"', '"deploystatus": "success"',
            '"deploymentstatus":"success"', '"deploymentstatus": "success"',
            '"deploymentstatus":"deployed"', '"deploymentstatus": "deployed"',
            '"status":"success"', '"status": "success"',
            '"status":"deployed"', '"status": "deployed"',
            '"status":"started"', '"status": "started"',
            '"result":"success"', '"result": "success"',
            "deployed successfully", "deployment successful",
            "successfully deployed", "deploy successful",
            '"deploy_success": true', '"deploy_success":true',
            '"deploystate":"started"', '"deploystate": "started"',
            '"deploystate":"success"', '"deploystate": "success"',
            "http/1.1 202", "http/2 202", "status_code=202", "status: 202",
            '"statuscode":202', '"statuscode": 202',
        ))

    @staticmethod
    def _is_locked_error(output: str) -> bool:
        text = (output or "").lower()
        return (
            "is locked" in text
            or "artifact as it is locked" in text
            or ("cannot update the artifact" in text and "locked" in text)
        )
