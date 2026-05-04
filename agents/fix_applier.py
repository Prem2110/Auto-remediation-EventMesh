"""
agents/fix_applier.py
=====================
FixApplier — executes the actual SAP API calls (or evaluates the LLM's
tool-call history) and returns a standardised ApplyResult.

Exports:
  ApplyResult  — mutable dataclass matching evaluate_fix_result() output shape
  FixApplier   — async apply(ctx, patch, validate_result) -> ApplyResult
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
    ) -> ApplyResult:
        # Pre-validation gate
        if not validate_result.passed:
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
            return await self._apply_structured(ctx, patch, validate_result)

        return self._evaluate_free_xml(patch)

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
                update_result = await self._mcp.execute_integration_tool(
                    "update-iflow",
                    {
                        "id": ctx.iflow_id,
                        "files": [{"filepath": ctx.original_filepath, "content": merged_xml}],
                        "autoDeploy": True,
                    },
                )
                update_ok = self._update_succeeded(str(update_result.get("output", "")))
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
                        technical_details=str(update_result.get("output", ""))[:300],
                        steps=[],
                    )
                deploy_result = await self._mcp.execute_integration_tool(
                    "deploy-iflow", {"id": ctx.iflow_id}
                )
                deploy_ok = self._deploy_succeeded(str(deploy_result.get("output", "")))
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
        update_result = await self._mcp.execute_integration_tool(
            "update-iflow",
            {"id": ctx.iflow_id,
             "files": [{"filepath": ctx.original_filepath, "content": vr.patched_xml}]},
        )
        update_out = str(update_result.get("output", ""))
        if not self._update_succeeded(update_out):
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
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="no_tool_calls",
                summary="LLM did not execute any tools.",
                technical_details="",
                steps=patch.steps,
            )
        if raw.startswith("__TIMEOUT__"):
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="timeout",
                summary="Fix agent timed out.",
                technical_details="",
                steps=patch.steps,
            )
        if raw.startswith("__RECURSION_LIMIT__"):
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="recursion_limit",
                summary="Fix agent hit LangGraph recursion limit — will retry with a simpler strategy.",
                technical_details="",
                steps=patch.steps,
            )
        if raw.startswith("__ERROR__:"):
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="agent",
                summary="Fix agent encountered an error.",
                technical_details=raw[len("__ERROR__:"):],
                steps=patch.steps,
            )
        if raw.startswith("__NO_AGENT__"):
            return ApplyResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="agent",
                summary="Fix agent not initialised.",
                technical_details="",
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

    # ── static helpers ────────────────────────────────────────────────────────

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
