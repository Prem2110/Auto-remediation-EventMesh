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
                        "autoDeploy": False,
                    },
                )
                _cr_out = str(update_result.get("output", ""))
                logger.info(
                    "[FixApplier] component-replace update-iflow response: iflow=%s output_len=%d output=%.200s",
                    ctx.iflow_id, len(_cr_out), _cr_out,
                )
                update_ok = self._update_succeeded(_cr_out)
                if not update_ok:
                    _cr_locked = self._is_locked_error(_cr_out)
                    logger.warning(
                        "[FixApplier] Component replace update failed for iflow=%s locked=%s",
                        ctx.iflow_id, _cr_locked,
                    )
                    return ApplyResult(
                        success=False,
                        fix_applied=False,
                        deploy_success=False,
                        failed_stage="locked" if _cr_locked else "update",
                        summary=(
                            "iFlow is locked in SAP CPI Integration Flow Designer. "
                            "Cancel/close the checkout in SAP CPI and retry."
                            if _cr_locked else
                            f"Component replacement update failed for {ctx.iflow_id}"
                        ),
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
        _files = [{"filepath": ctx.original_filepath, "content": vr.patched_xml}]
        for _sfp, _sfc in (ctx.secondary_files or {}).items():
            _files.append({"filepath": _sfp, "content": _sfc})
        update_result = await self._mcp.execute_integration_tool(
            "update-iflow",
            {
                "id": ctx.iflow_id,
                "files": _files,
                "autoDeploy": False,
            },
        )
        update_out = str(update_result.get("output", ""))
        logger.info(
            "[FixApplier] update-iflow response: iflow=%s output_len=%d output=%.300s",
            ctx.iflow_id, len(update_out), update_out,
        )
        # An empty update-iflow body is ambiguous — SAP CPI sometimes returns
        # HTTP 200 with no body even when the write succeeded.  Verify by
        # re-fetching the live XML and checking every patched field landed.
        if not self._update_succeeded(update_out):
            if self._is_locked_error(update_out):
                # Try to cancel the checkout lock automatically, then retry once.
                logger.info(
                    "[FixApplier] Locked iFlow — attempting auto-unlock: iflow=%s", ctx.iflow_id
                )
                unlocked = await self._try_unlock(ctx.iflow_id)
                if unlocked:
                    logger.info(
                        "[FixApplier] Auto-unlock succeeded — retrying update-iflow: iflow=%s", ctx.iflow_id
                    )
                    _retry_result = await self._mcp.execute_integration_tool(
                        "update-iflow",
                        {"id": ctx.iflow_id, "files": _files, "autoDeploy": False},
                    )
                    update_out = str(_retry_result.get("output", ""))
                    logger.info(
                        "[FixApplier] Post-unlock update-iflow: iflow=%s output=%.200s",
                        ctx.iflow_id, update_out,
                    )
                    if not self._update_succeeded(update_out):
                        return ApplyResult(
                            success=False,
                            fix_applied=False,
                            deploy_success=False,
                            failed_stage="locked" if self._is_locked_error(update_out) else "update",
                            summary=(
                                "iFlow checkout was cancelled automatically but the update still failed. "
                                "Retry the fix or check the iFlow status in SAP CPI."
                            ),
                            technical_details=update_out[:300],
                            steps=[],
                        )
                    # Unlock + retry succeeded — fall through to deploy
                else:
                    return ApplyResult(
                        success=False,
                        fix_applied=False,
                        deploy_success=False,
                        failed_stage="locked",
                        summary=(
                            "iFlow is locked in SAP CPI — automatic unlock failed. "
                            "Cancel the checkout manually in SAP Integration Flow Designer and retry."
                        ),
                        technical_details=update_out[:300],
                        steps=[],
                    )
            else:
                patch_confirmed = await self._verify_patch_landed(ctx, patch.operations)
                if not patch_confirmed:
                    _reason = "empty body — patch not confirmed in live XML" if not update_out.strip() else update_out[:200]
                    logger.warning(
                        "[FixApplier] update-iflow failed (content check): iflow=%s reason=%s",
                        ctx.iflow_id, _reason,
                    )
                    return ApplyResult(
                        success=False,
                        fix_applied=False,
                        deploy_success=False,
                        failed_stage="update",
                        summary=f"update-iflow failed after structured patch: {_reason}",
                        technical_details=update_out,
                        steps=[],
                    )
                logger.info(
                    "[FixApplier] update-iflow empty body — content check confirmed patch landed: iflow=%s",
                    ctx.iflow_id,
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

    # ── unlock helper ─────────────────────────────────────────────────────────

    async def _try_unlock(self, iflow_id: str) -> bool:
        """Cancel the SAP CPI checkout lock via MCP unlock tools.

        Tries every unlock-related tool registered on the integration_suite server
        and returns True as soon as one succeeds.
        """
        unlock_tools: List[str] = []
        for t in self._mcp.tools:
            server = getattr(t, "server", "") or ""
            if server != "integration_suite":
                continue
            combined = f"{getattr(t, 'name', '')} {getattr(t, 'mcp_tool_name', '')}".lower()
            if any(kw in combined for kw in ("cancel", "checkout", "unlock", "force_unlock", "discard")):
                tool_name = getattr(t, "mcp_tool_name", None) or getattr(t, "name", None)
                if tool_name:
                    unlock_tools.append(tool_name)

        if not unlock_tools:
            logger.warning("[FixApplier] No unlock MCP tools found for iflow=%s", iflow_id)
            return False

        for tool_name in unlock_tools:
            try:
                out = await self._mcp.execute_integration_tool(
                    tool_name,
                    {"iflow_id": iflow_id, "id": iflow_id, "artifact_id": iflow_id},
                )
                out_str = str(out.get("output", out) if isinstance(out, dict) else out)
                out_lower = out_str.lower()
                if "error" not in out_lower and "fail" not in out_lower:
                    logger.info(
                        "[FixApplier] Unlock via '%s' succeeded: iflow=%s", tool_name, iflow_id
                    )
                    return True
                logger.debug(
                    "[FixApplier] MCP unlock tool '%s' did not succeed: %.100s", tool_name, out_str
                )
            except Exception as exc:
                logger.debug("[FixApplier] MCP unlock tool '%s' exception: %s", tool_name, exc)

        return False

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
            if poll_num > 0:
                await asyncio.sleep(poll_interval)
            elapsed = initial_wait + poll_num * poll_interval
            if progress_fn:
                try:
                    progress_fn(f"Agent: waiting for iFlow to start… ({elapsed}s)")
                except Exception as cb_exc:
                    logger.debug("[FixApplier] progress_fn callback error (non-fatal): %s", cb_exc)

            deploy_error = await self._fetch_deploy_error(ctx.iflow_id)
            logger.info(
                "[FixApplier] deploy poll %d/%d iflow=%s deploy_error_len=%d error=%r",
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

                # Secondary verification: check original message log to confirm error
                # signature is gone.  Non-blocking — skip if tool unavailable or no GUID.
                _msg_note = await self._check_message_log_after_deploy(ctx)
                if _msg_note:
                    logger.warning(
                        "[FixApplier] Post-deploy message log check: %s", _msg_note
                    )

                return ApplyResult(
                    success=True,
                    fix_applied=result.fix_applied,
                    deploy_success=True,
                    failed_stage=None,
                    summary=result.summary,
                    technical_details=(
                        f"{result.technical_details} | {_msg_note}"
                        if _msg_note else result.technical_details
                    ),
                    steps=result.steps,
                )

            # Non-empty = SAP reported a deployment error; keep polling — SAP sometimes
            # clears transient errors within the first few ticks after a 202 response.
            logger.warning(
                "[FixApplier] iFlow deploy FAILED (get-deploy-error non-empty) on poll %d/%d: "
                "iflow=%s error=%.200s",
                poll_num + 1, max_polls, ctx.iflow_id, deploy_error[:200],
            )
            if poll_num < max_polls - 1:
                continue
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

    async def _check_message_log_after_deploy(self, ctx: FixContext) -> str:
        """
        After a successful deploy, call get_message_logs for the original message GUID
        to check whether the error signature is still present in the log.

        Returns a non-empty advisory string when the original message still shows a
        FAILED status (meaning a replay is needed to fully verify the fix), or "" when
        the log shows success or the tool is unavailable.  Never raises.
        """
        if not ctx.message_guid:
            return ""
        try:
            r = await self._mcp.execute_integration_tool(
                "get_message_logs", {"message_guid": ctx.message_guid}
            )
            log_out = str(r.get("output", "")).strip()
            if not log_out:
                return ""
            log_lower = log_out.lower()
            if any(kw in log_lower for kw in ("failed", "error", "exception")):
                return (
                    f"Original message GUID {ctx.message_guid} still shows a failed status "
                    "in the message log — the iFlow was redeployed successfully but the "
                    "original message has not been replayed. Trigger a manual replay or wait "
                    "for the next business message to confirm the fix is effective."
                )
            if any(kw in log_lower for kw in ("completed", "success", "delivered")):
                logger.info(
                    "[FixApplier] Post-deploy log check: message %s shows success status — "
                    "fix verified via message log for iflow=%s",
                    ctx.message_guid, ctx.iflow_id,
                )
        except Exception as exc:
            logger.debug(
                "[FixApplier] Post-deploy message log check unavailable (non-fatal): %s", exc
            )
        return ""

    async def _fetch_deploy_error(self, iflow_id: str) -> str:
        try:
            r = await self._mcp.execute_integration_tool(
                "get-deploy-error", {"id": iflow_id}
            )
            raw = str(r.get("output", "")).strip()
            logger.debug("[FixApplier] get-deploy-error raw=%r iflow=%s", raw, iflow_id)
            # SAP MCP tool returns "{}" or "[]" when there is no deployment error.
            # Treat these empty JSON sentinels the same as an empty string.
            if raw in ("{}", "[]", "null", "None", "none", '""', "''"):
                return ""
            return raw
        except Exception:
            return ""

    # ── content verification ──────────────────────────────────────────────────

    async def _verify_patch_landed(
        self,
        ctx: "FixContext",
        operations: List[Dict[str, Any]],
    ) -> bool:
        """
        Verify that every structured patch operation is reflected in the live
        iFlow XML by calling get-iflow and parsing the returned XML.

        Used as a fallback when update-iflow returns an empty body — SAP CPI
        occasionally returns HTTP 200 with no body even when the write succeeded.
        Returns True only when every operation's changed value is confirmed.
        """
        import xml.etree.ElementTree as ET  # noqa: PLC0415
        from core.validators import _extract_iflow_file  # noqa: PLC0415

        _BPMN2 = "http://www.omg.org/spec/BPMN/20100524/MODEL"
        _IFL   = "http:///com.sap.ifl.model/Ifl.xsd"

        try:
            verify = await self._mcp.execute_integration_tool("get-iflow", {"id": ctx.iflow_id})
            if not verify.get("success") or not verify.get("output"):
                logger.warning(
                    "[FixApplier] verify-patch: get-iflow returned no output for iflow=%s",
                    ctx.iflow_id,
                )
                return False

            _, live_xml = _extract_iflow_file(str(verify["output"]), iflow_id=ctx.iflow_id)
            if not live_xml:
                logger.warning(
                    "[FixApplier] verify-patch: could not extract iflw XML from get-iflow for iflow=%s",
                    ctx.iflow_id,
                )
                return False

            root = ET.fromstring(live_xml.encode("utf-8"))
        except Exception as exc:
            logger.warning("[FixApplier] verify-patch: XML fetch/parse error for iflow=%s: %s", ctx.iflow_id, exc)
            return False

        for op in operations:
            change_type = (op.get("change_type") or "").strip()
            target_id   = (op.get("target_component") or "").strip()
            field       = (op.get("field") or "").strip()
            new_value   = str(op.get("new_value") or "")

            if change_type not in ("update_property", "update_expression", "add_property", "update_attribute"):
                logger.debug(
                    "[FixApplier] verify-patch: cannot verify change_type=%s — treating as unconfirmed",
                    change_type,
                )
                return False

            target = next((e for e in root.iter() if e.get("id") == target_id), None)
            if target is None:
                logger.warning(
                    "[FixApplier] verify-patch: component '%s' not found in live XML for iflow=%s",
                    target_id, ctx.iflow_id,
                )
                return False

            if change_type in ("update_property", "update_expression", "add_property"):
                ext = target.find(f"{{{_BPMN2}}}extensionElements")
                if ext is None:
                    logger.warning(
                        "[FixApplier] verify-patch: no extensionElements on '%s' for iflow=%s",
                        target_id, ctx.iflow_id,
                    )
                    return False
                live_val: Optional[str] = None
                for prop in ext.findall(f"{{{_IFL}}}property"):
                    key_text = (
                        prop.findtext(f"{{{_IFL}}}key")
                        or prop.findtext("key")
                        or ""
                    )
                    if key_text == field:
                        val_elem = prop.find(f"{{{_IFL}}}value") or prop.find("value")
                        live_val = (val_elem.text if val_elem is not None else "") or ""
                        break
                if live_val is None:
                    logger.warning(
                        "[FixApplier] verify-patch: property '%s' not found on '%s' in live XML iflow=%s",
                        field, target_id, ctx.iflow_id,
                    )
                    return False
                if live_val != new_value:
                    logger.warning(
                        "[FixApplier] verify-patch: property '%s' on '%s' is '%s', expected '%s' "
                        "— update did not land for iflow=%s",
                        field, target_id, live_val, new_value, ctx.iflow_id,
                    )
                    return False

            elif change_type == "update_attribute":
                live_attr = target.get(field)
                if live_attr != new_value:
                    logger.warning(
                        "[FixApplier] verify-patch: attribute '%s' on '%s' is '%s', expected '%s' "
                        "— update did not land for iflow=%s",
                        field, target_id, live_attr, new_value, ctx.iflow_id,
                    )
                    return False

            logger.debug(
                "[FixApplier] verify-patch: confirmed %s '%s'='%s' on '%s' iflow=%s",
                change_type, field, new_value, target_id, ctx.iflow_id,
            )

        logger.info(
            "[FixApplier] verify-patch: all %d operation(s) confirmed in live XML for iflow=%s",
            len(operations), ctx.iflow_id,
        )
        return True

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
