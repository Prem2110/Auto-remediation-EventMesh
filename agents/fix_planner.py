"""
agents/fix_planner.py
=====================
FixPlanner — selects a fix strategy (structured vs free-XML) by running a
read-only diagnosis agent against the iFlow XML.

Exports:
  FixStrategy   — dataclass returned by plan()
  FixPlanner    — async plan(ctx) -> FixStrategy
"""

import asyncio
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Tuple

from agents.fix_context import FixContext
from core.constants import FIX_OPERATION_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FixStrategy:
    strategy: Literal["structured", "free_xml", "deploy_only"]
    operations: List[Dict]
    reason: str


class FixPlanner:
    """
    Runs the diagnosis-only agent to extract a structured operation list,
    then decides which fix strategy to use.
    """

    def __init__(self, mcp) -> None:
        self._mcp              = mcp
        self._diagnosis_agent  = None

    async def build_agent(self) -> None:
        """Build a read-only diagnosis agent (get-iflow only)."""
        get_iflow_tool = self._mcp.get_mcp_tool("integration_suite", "get-iflow")
        diag_tools = [t for t in [get_iflow_tool] if t]
        if not diag_tools:
            logger.warning("[FixPlanner] get-iflow tool unavailable — structured path disabled.")
            return

        self._diagnosis_agent = await self._mcp.build_agent(
            tools=diag_tools,
            system_prompt=(
                "You are a read-only SAP CPI iFlow analyser. "
                "You MAY call get-iflow if the XML is not in the prompt. "
                "Return ONLY a JSON fix operation — no markdown, no tool calls other than get-iflow."
            ),
            deployment_id=os.getenv("LLM_DEPLOYMENT_ID_FIX") or None,
        )
        logger.info("[FixPlanner] Diagnosis agent ready.")

    # ── public entry point ────────────────────────────────────────────────────

    async def plan(self, ctx: FixContext) -> Tuple[FixStrategy, str]:
        """
        Try to produce a structured operation list via the diagnosis agent.
        Falls back to free_xml if the agent is unavailable, times out, or
        returns no valid operations.

        Returns (FixStrategy, sliced_xml) where sliced_xml is a focused XML
        snippet centred on the affected component, or "" if unavailable.
        """
        sliced_xml = self._slice_xml(ctx.original_xml, ctx.affected_component)

        if not ctx.original_xml or not ctx.original_filepath:
            return FixStrategy(
                strategy="free_xml",
                operations=[],
                reason="No pre-fetched iFlow XML — structured path unavailable.",
            ), sliced_xml

        ops = await self._get_fix_operation(ctx)

        # Filter structural ops — patcher cannot handle structural graph rewiring
        applicable = [op for op in ops if op.get("change_type") != "structural"]

        # Reject entire list if any op is missing required fields
        valid_ops: List[Dict] = []
        for op in applicable:
            if op.get("change_type") and op.get("target_component") and op.get("field"):
                valid_ops.append(op)
            else:
                logger.warning(
                    "[FixPlanner] Rejecting op with missing fields: %s", op
                )
                valid_ops = []   # reject the whole batch
                break

        if valid_ops:
            return FixStrategy(
                strategy="structured",
                operations=valid_ops,
                reason=f"Diagnosis agent returned {len(valid_ops)} valid operation(s).",
            ), sliced_xml

        return FixStrategy(
            strategy="free_xml",
            operations=[],
            reason=(
                "No valid structured operations returned — "
                "falling through to free-XML LLM agent."
            ),
        ), sliced_xml

    # ── XML context slicer ───────────────────────────────────────────────────

    @staticmethod
    def _slice_xml(original_xml: str, affected_component: str) -> str:
        """
        Extract a focused XML snippet centred on affected_component.
        Returns "" if the component is unknown, not found, or parsing fails.
        Never raises — callers fall back to full XML on any empty return.
        """
        try:
            ac = (affected_component or "").strip()
            if not ac or ac.lower() == "unknown":
                return ""

            root = ET.fromstring(original_xml)

            # ── Header summary from first two <bpmn2:participant> elements ──
            ns_candidates = [
                "{http://www.omg.org/spec/BPMN/20100524/MODEL}participant",
                "participant",
            ]
            participants: List[str] = []
            collab_id = ""
            for tag in ns_candidates:
                found = root.findall(f".//{tag}")
                if found:
                    participants = [e.get("name", e.get("id", "")) for e in found[:2]]
                    # collaboration id lives on the parent <bpmn2:collaboration>
                    for collab_tag in (
                        "{http://www.omg.org/spec/BPMN/20100524/MODEL}collaboration",
                        "collaboration",
                    ):
                        collab = root.find(f".//{collab_tag}")
                        if collab is not None:
                            collab_id = collab.get("id", "")
                            break
                    break

            header = (
                "<iflow_summary>\n"
                f"  <name>{collab_id}</name>\n"
                f"  <participants>{' → '.join(participants)}</participants>\n"
                "</iflow_summary>"
            )

            # ── Find the affected element and its siblings ────────────────
            target = None
            target_parent = None
            for parent in root.iter():
                for child in list(parent):
                    if child.get("id") == ac:
                        target        = child
                        target_parent = parent
                        break
                if target is not None:
                    break

            if target is None:
                return ""

            siblings = list(target_parent)
            idx      = siblings.index(target)
            lo       = max(0, idx - 2)
            hi       = min(len(siblings), idx + 3)
            slices   = siblings[lo:hi]

            inner = "".join(ET.tostring(e, encoding="unicode") for e in slices)
            failing_section = f"<failing_section>\n{inner}\n</failing_section>"

            return header + "\n" + failing_section

        except Exception as exc:
            logger.warning("[FixPlanner] _slice_xml failed for component=%s: %s", affected_component, exc)
            return ""

    # ── internal: call diagnosis agent ───────────────────────────────────────

    async def _get_fix_operation(self, ctx: FixContext) -> List[Dict]:
        agent = self._diagnosis_agent
        if agent is None:
            logger.debug("[FixPlanner] No diagnosis agent — structured path skipped.")
            return []

        prompt = FIX_OPERATION_PROMPT_TEMPLATE.format(
            iflow_id=ctx.iflow_id,
            error_type=ctx.error_type,
            error_message=(ctx.error_message or "")[:3000],
            proposed_fix=ctx.proposed_fix or "",
            root_cause=ctx.root_cause or "",
            affected_component=ctx.affected_component or "",
            original_xml=ctx.original_xml or "",
            pattern_history=ctx.pattern_history or "",
            sap_notes=ctx.sap_notes or "",
            error_type_guidance=ctx.error_type_guidance or "",
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
                "[FixPlanner] Diagnosis agent returned %d op(s) for iflow=%s",
                len(ops), ctx.iflow_id,
            )
            return ops
        except asyncio.TimeoutError:
            logger.warning("[FixPlanner] Diagnosis agent timed out for iflow=%s — falling back.", ctx.iflow_id)
        except Exception as exc:
            logger.warning("[FixPlanner] _get_fix_operation failed for iflow=%s: %s", ctx.iflow_id, exc)
        return []
