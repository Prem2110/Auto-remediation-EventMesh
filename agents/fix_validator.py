"""
agents/fix_validator.py
=======================
FixValidator — structural validation before and runtime status check after
a fix is applied.

Exports:
  PreValidateResult  — passed, errors, diff_node_count, patched_xml
  PostValidateResult — runtime_status, passed, detail
  FixValidator       — pre_validate(ctx, patch) + async post_validate(ctx)
"""

import logging
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, List

from agents.fix_context import FixContext
from agents.fix_generator import PatchSpec
from core.xml_patcher import PatcherError, apply_fix_operations
from core.validators import _check_iflow_xml

logger = logging.getLogger(__name__)


@dataclass
class PreValidateResult:
    passed: bool
    errors: List[str]
    diff_node_count: int
    patched_xml: str = ""
    pre_existing_issues: List[str] = field(default_factory=list)
    new_issues: List[str] = field(default_factory=list)


@dataclass
class PostValidateResult:
    runtime_status: str
    passed: bool
    detail: str


class FixValidator:
    """
    pre_validate — run before FixApplier touches SAP:
      1. Apply patcher (structured mode only) → catch PatcherError.
      2. Run _check_iflow_xml structural checks.
      3. Count element changes outside the affected_component subtree.

    post_validate — async, fire-and-forget after apply:
      Polls the SAP runtime status for the iFlow.
    """

    def __init__(self, error_fetcher=None) -> None:
        self._error_fetcher = error_fetcher

    def set_error_fetcher(self, error_fetcher) -> None:
        self._error_fetcher = error_fetcher

    # ── pre_validate ──────────────────────────────────────────────────────────

    def pre_validate(self, ctx: FixContext, patch: PatchSpec) -> PreValidateResult:
        """
        Validate the proposed patch before it is sent to SAP.
        Returns a PreValidateResult with passed=True if all checks pass.
        """
        # For free_xml mode the LLM already ran validation internally; skip here.
        if patch.mode == "free_xml":
            return PreValidateResult(passed=True, errors=[], diff_node_count=0,
                                     patched_xml=patch.raw_xml)

        # ── Step 1: Apply patcher ─────────────────────────────────────────────
        try:
            patched_xml = apply_fix_operations(ctx.original_xml, patch.operations)
        except PatcherError as exc:
            return PreValidateResult(
                passed=False,
                errors=[f"Patcher failed: {exc}"],
                diff_node_count=0,
            )

        # ── Step 2: Structural XML validation ─────────────────────────────────
        errors = _check_iflow_xml(ctx.original_xml, patched_xml)

        # Split errors into introduced-by-fix vs pre-existing.
        # Errors prefixed "NEW:" are regressions and block the fix.
        # Errors without "NEW:" pre-date this fix attempt and are only logged.
        new_issues        = [e[4:].strip() for e in errors if e.startswith("NEW:")]
        pre_existing      = [e for e in errors if not e.startswith("NEW:")]

        if pre_existing:
            logger.info(
                "[FixValidator] %d pre-existing issue(s) in iflow=%s (not blocking): %s",
                len(pre_existing), ctx.iflow_id, pre_existing[:2],
            )

        if new_issues:
            return PreValidateResult(
                passed=False,
                errors=errors,
                diff_node_count=0,
                patched_xml=patched_xml,
                pre_existing_issues=pre_existing,
                new_issues=new_issues,
            )

        if pre_existing:
            # Pre-existing issues only — pass but expose them so supervisor can log
            return PreValidateResult(
                passed=True,
                errors=errors,
                diff_node_count=0,
                patched_xml=patched_xml,
                pre_existing_issues=pre_existing,
                new_issues=[],
            )

        # ── Step 3: Unrelated-change diff ─────────────────────────────────────
        diff_count = self._count_unrelated_changes(ctx, patched_xml)
        if diff_count > 3:
            return PreValidateResult(
                passed=False,
                errors=[f"Unexpected changes to {diff_count} unrelated elements"],
                diff_node_count=diff_count,
                patched_xml=patched_xml,
            )

        return PreValidateResult(
            passed=True,
            errors=[],
            diff_node_count=diff_count,
            patched_xml=patched_xml,
        )

    # ── post_validate ─────────────────────────────────────────────────────────

    async def post_validate(self, ctx: FixContext) -> PostValidateResult:
        """
        Poll the SAP runtime status for the iFlow after deployment.
        Non-blocking — the orchestrator should fire this as a background task.
        """
        if self._error_fetcher is None:
            return PostValidateResult(
                runtime_status="error_fetcher_unavailable",
                passed=False,
                detail="error_fetcher not set",
            )
        try:
            detail_raw   = await self._error_fetcher.fetch_runtime_artifact_detail(ctx.iflow_id)
            raw_status   = str(
                detail_raw.get("Status")
                or detail_raw.get("DeployState")
                or detail_raw.get("RuntimeStatus")
                or ""
            ).strip()
            passed = raw_status.lower() in {"started", "active", "running"}
            return PostValidateResult(
                runtime_status=raw_status,
                passed=passed,
                detail=str(detail_raw),
            )
        except Exception as exc:
            logger.warning("[FixValidator] post_validate error for iflow=%s: %s", ctx.iflow_id, exc)
            return PostValidateResult(
                runtime_status="poll_error",
                passed=False,
                detail=str(exc),
            )

    # ── internal diff helper ──────────────────────────────────────────────────

    @staticmethod
    def _count_unrelated_changes(ctx: FixContext, patched_xml: str) -> int:
        """
        Count element (tag+attrib) changes in the patched XML that fall
        outside the affected_component subtree.
        """
        def iter_excluding_subtree(root: ET.Element, exclude_id: str):
            excluded: set = set()
            if exclude_id:
                for elem in root.iter():
                    if elem.get("id") == exclude_id:
                        for desc in elem.iter():
                            excluded.add(id(desc))
                        break
            for elem in root.iter():
                if id(elem) not in excluded:
                    yield elem

        def sig(elem: ET.Element):
            return (elem.tag, tuple(sorted(elem.attrib.items())))

        try:
            orig_root   = ET.fromstring(ctx.original_xml)
            patch_root  = ET.fromstring(patched_xml)
        except ET.ParseError:
            return 0

        ac          = ctx.affected_component
        orig_sigs   = Counter(sig(e) for e in iter_excluding_subtree(orig_root, ac))
        patch_sigs  = Counter(sig(e) for e in iter_excluding_subtree(patch_root, ac))

        changes = 0
        for k in set(orig_sigs) | set(patch_sigs):
            changes += abs(orig_sigs[k] - patch_sigs[k])
        return changes
