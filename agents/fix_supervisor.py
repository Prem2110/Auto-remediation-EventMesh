"""
agents/fix_supervisor.py
========================
FixSupervisor — orchestrates the fix pipeline with strategy rotation and
pre-existing issue awareness.

Strategy priority order:
  1. component_replace  (fast, deterministic, uses reference iFlows)
  2. structured         (patcher-based, no LLM for execution)
  3. free_xml           (full LLM agent)
  4. escalate           (all strategies exhausted — signals human review)

Pre-existing issue awareness:
  If PreValidateResult.new_issues is empty but pre_existing_issues is not,
  the supervisor proceeds with deployment rather than blocking, since the
  issues pre-date the fix attempt and are not regressions.

Exports:
  SuperviseResult  — result dataclass with to_dict()
  FixSupervisor    — async supervise(ctx, progress_fn) -> SuperviseResult
"""

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents.fix_context import FixContext

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS    = 3
_STRATEGY_ORDER  = ["component_replace", "structured", "free_xml"]

# Failure stages that are not recoverable by trying another strategy
_NON_RECOVERABLE = {"locked", "timeout", "agent", "no_tool_calls"}


@dataclass
class SuperviseResult:
    success: bool
    fix_applied: bool
    deploy_success: bool
    failed_stage: Optional[str]
    summary: str
    technical_details: str
    steps: List[Dict]
    raw_answer: str = ""
    strategy_used: str = ""
    attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success":           self.success,
            "fix_applied":       self.fix_applied,
            "deploy_success":    self.deploy_success,
            "failed_stage":      self.failed_stage,
            "summary":           self.summary,
            "technical_details": self.technical_details,
            "steps":             self.steps,
            "raw_answer":        self.raw_answer,
            "strategy_used":     self.strategy_used,
            "attempts":          self.attempts,
        }


class FixSupervisor:
    """
    Orchestrates FixPlanner / FixGenerator / FixValidator / FixApplier with
    strategy rotation and pre-existing issue awareness.

    Accepts the four already-constructed pipeline components so they are
    shared with FixAgent (no duplicate agent builds).
    """

    def __init__(self, planner, generator, validator, applier) -> None:
        self._planner   = planner
        self._generator = generator
        self._validator = validator
        self._applier   = applier

    # ── public entry point ────────────────────────────────────────────────────

    async def supervise(
        self,
        ctx: FixContext,
        progress_fn: Optional[Callable] = None,
    ) -> SuperviseResult:
        """
        Try strategies in _STRATEGY_ORDER up to _MAX_ATTEMPTS times.
        Returns a SuperviseResult with the final outcome.
        """
        strategy, sliced_xml = await self._planner.plan(ctx)
        ctx = dataclasses.replace(ctx, sliced_xml=sliced_xml)

        last_result: Optional[SuperviseResult] = None
        tried_strategies: set = set()

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            strat_name = strategy.strategy
            tried_strategies.add(strat_name)

            logger.info(
                "[Supervisor] attempt=%d/%d strategy=%s iflow=%s",
                attempt, _MAX_ATTEMPTS, strat_name, ctx.iflow_id,
            )

            patch   = await self._generator.generate(ctx, strategy, progress_fn=progress_fn)
            vresult = self._validator.pre_validate(ctx, patch)

            # Allow deploy when only pre-existing issues are present
            if not vresult.passed and not vresult.new_issues and vresult.pre_existing_issues:
                from agents.fix_validator import PreValidateResult  # noqa: PLC0415
                logger.info(
                    "[Supervisor] pre_existing_only=True — proceeding despite %d pre-existing issue(s)",
                    len(vresult.pre_existing_issues),
                )
                vresult = PreValidateResult(
                    passed=True,
                    errors=vresult.errors,
                    diff_node_count=vresult.diff_node_count,
                    patched_xml=vresult.patched_xml,
                    pre_existing_issues=vresult.pre_existing_issues,
                    new_issues=[],
                )

            result = await self._applier.apply(ctx, patch, vresult)

            last_result = SuperviseResult(
                success=result.success,
                fix_applied=result.fix_applied,
                deploy_success=result.deploy_success,
                failed_stage=result.failed_stage,
                summary=result.summary,
                technical_details=result.technical_details,
                steps=result.steps,
                raw_answer=patch.raw_answer,
                strategy_used=strat_name,
                attempts=attempt,
            )

            if result.success:
                logger.info(
                    "[Supervisor] Fix succeeded: iflow=%s strategy=%s attempt=%d",
                    ctx.iflow_id, strat_name, attempt,
                )
                return last_result

            if result.failed_stage in _NON_RECOVERABLE:
                logger.warning(
                    "[Supervisor] Non-recoverable stage=%s — stopping retry for iflow=%s",
                    result.failed_stage, ctx.iflow_id,
                )
                break

            next_strat = self._next_strategy(strat_name, tried_strategies)
            if next_strat is None:
                logger.info("[Supervisor] All strategies exhausted for iflow=%s", ctx.iflow_id)
                break

            # Rollback and capture deploy error before next attempt
            deploy_error_hint = ""
            if last_result.fix_applied and last_result.failed_stage in ("deploy", "deploy_validation"):
                deploy_error_hint = await self._rollback_and_get_deploy_error(ctx)
                if deploy_error_hint:
                    ctx = dataclasses.replace(ctx, deploy_error_hint=deploy_error_hint)

            _reason = (
                f"Strategy '{strat_name}' failed (stage={result.failed_stage}); "
                f"retrying with '{next_strat}'."
            )
            if deploy_error_hint:
                _reason += f" SAP CPI deploy error: {deploy_error_hint[:200]}"

            from agents.fix_planner import FixStrategy  # noqa: PLC0415
            strategy = FixStrategy(
                strategy=next_strat,
                operations=[],
                reason=_reason,
            )

        # Escalate
        if last_result is not None:
            last_result.failed_stage = last_result.failed_stage or "escalate"
            last_result.summary = (
                f"All {last_result.attempts} fix attempt(s) failed for '{ctx.iflow_id}'. "
                f"Last stage: {last_result.failed_stage}. Escalating to human review."
            )
        else:
            last_result = SuperviseResult(
                success=False, fix_applied=False, deploy_success=False,
                failed_stage="escalate",
                summary=f"No fix strategies could be attempted for '{ctx.iflow_id}'.",
                technical_details="", steps=[], raw_answer="",
                strategy_used="none", attempts=0,
            )

        logger.error(
            "[Supervisor] Escalating: iflow=%s attempts=%d last_stage=%s",
            ctx.iflow_id, last_result.attempts, last_result.failed_stage,
        )
        return last_result

    async def _rollback_and_get_deploy_error(self, ctx: FixContext) -> str:
        """
        After a failed deploy:
        1. Restore the iFlow to ctx.original_xml via update-iflow.
        2. Call get-deploy-error to fetch the SAP CPI deploy diagnostics.
        Returns the deploy error text (empty string on any failure).
        """
        mcp = getattr(self._applier, "_mcp", None)
        if mcp is None:
            logger.warning("[Supervisor] Cannot rollback — applier has no _mcp")
            return ""

        # Restore original XML
        try:
            restore_result = await mcp.execute_integration_tool(
                "update-iflow",
                {
                    "id":    ctx.iflow_id,
                    "files": [{"filepath": ctx.original_filepath, "content": ctx.original_xml}],
                },
            )
            restore_out = str(restore_result.get("output", ""))
            from agents.fix_applier import FixApplier  # noqa: PLC0415
            if FixApplier._update_succeeded(restore_out):
                logger.info("[Supervisor] Rollback succeeded for iflow=%s", ctx.iflow_id)
            else:
                logger.warning(
                    "[Supervisor] Rollback may have failed for iflow=%s: %s",
                    ctx.iflow_id, restore_out[:200],
                )
        except Exception as exc:
            logger.warning("[Supervisor] Rollback error for iflow=%s: %s", ctx.iflow_id, exc)

        # Fetch SAP CPI deploy error
        deploy_error = ""
        try:
            err_result = await mcp.execute_integration_tool(
                "get-deploy-error", {"id": ctx.iflow_id}
            )
            deploy_error = str(err_result.get("output", "")).strip()[:500]
            if deploy_error:
                logger.info(
                    "[Supervisor] Deploy error fetched for iflow=%s: %s",
                    ctx.iflow_id, deploy_error[:100],
                )
        except Exception as exc:
            logger.debug(
                "[Supervisor] get-deploy-error unavailable for iflow=%s (non-fatal): %s",
                ctx.iflow_id, exc,
            )

        return deploy_error

    @staticmethod
    def _next_strategy(current: str, tried: set) -> Optional[str]:
        for s in _STRATEGY_ORDER:
            if s not in tried:
                return s
        return None
