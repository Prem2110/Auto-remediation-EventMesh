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
from typing import Any, Dict, List, Literal, Optional, Tuple

from agents.fix_context import FixContext
from core.constants import FIX_OPERATION_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FixStrategy:
    strategy: Literal["structured", "free_xml", "deploy_only", "component_replace", "direct_patch"]
    operations: List[Dict]
    reason: str
    reference_name: str = ""


class FixPlanner:
    """
    Runs the diagnosis-only agent to extract a structured operation list,
    then decides which fix strategy to use.
    """

    def __init__(self, mcp) -> None:
        from agents.fix_component_replacer import ComponentReplacer  # noqa: PLC0415
        self._mcp              = mcp
        self._diagnosis_agent  = None
        self._replacer         = ComponentReplacer(mcp)

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

        # ── Direct patch — RCA gave exact component + property + value ────────
        _ac = (ctx.affected_component or "").strip()
        if (
            ctx.property_to_change
            and ctx.correct_value
            and _ac
            and _ac.lower() not in ("unknown", "")
            and ctx.original_xml
        ):
            patched_xml = self._apply_direct_patch(
                xml=ctx.original_xml,
                component_id=_ac,
                property_name=ctx.property_to_change,
                new_value=ctx.correct_value,
            )
            if patched_xml:
                logger.info(
                    "[FixPlanner] Direct patch succeeded: iflow=%s component=%s property=%s old=%r new=%r",
                    ctx.iflow_id, _ac, ctx.property_to_change,
                    ctx.current_value, ctx.correct_value,
                )
                return FixStrategy(
                    strategy="direct_patch",
                    operations=[{"merged_xml": patched_xml}],
                    reason=(
                        f"RCA provided exact change: "
                        f"{ctx.property_to_change} → {ctx.correct_value}"
                    ),
                ), sliced_xml
            else:
                logger.info(
                    "[FixPlanner] Direct patch failed (component/property not found) — "
                    "falling through to structured path"
                )

        if not ctx.original_xml or not ctx.original_filepath:
            return FixStrategy(
                strategy="free_xml",
                operations=[],
                reason="No pre-fetched iFlow XML — structured path unavailable.",
            ), sliced_xml

        # ── Component replacement (first strategy attempt) ────────────────────
        from agents.fix_component_replacer import REPLACEMENT_ELIGIBLE_ERROR_TYPES  # noqa: PLC0415
        if (
            ctx.error_type in REPLACEMENT_ELIGIBLE_ERROR_TYPES
            and self._replacer.is_eligible(ctx.error_type, ctx.affected_component)
        ):
            ref_name, ref_xml = await self._replacer.find_and_fetch_reference(
                ctx.error_type, ctx.affected_component
            )
            if ref_name and ref_xml:
                merged_xml = self._replacer.merge_component(
                    original_full_xml=ctx.original_xml,
                    reference_xml=ref_xml,
                    affected_component=ctx.affected_component,
                    proposed_fix=ctx.proposed_fix,
                    error_type=ctx.error_type,
                )
                if merged_xml:
                    logger.info(
                        "[FixPlanner] Component replacement succeeded: "
                        "iflow=%s component=%s reference=%s",
                        ctx.iflow_id, ctx.affected_component, ref_name,
                    )
                    return FixStrategy(
                        strategy="component_replace",
                        operations=[{"merged_xml": merged_xml}],
                        reason=f"Component replaced with reference '{ref_name}'",
                        reference_name=ref_name,
                    ), sliced_xml
                else:
                    logger.info(
                        "[FixPlanner] Component merge failed — falling through to structured path"
                    )

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

    # ── Direct XML patch (no LLM) ────────────────────────────────────────────

    @staticmethod
    def _apply_direct_patch(
        xml: str,
        component_id: str,
        property_name: str,
        new_value: str,
    ) -> Optional[str]:
        """
        Pure XML find-and-replace. No LLM involved.
        Finds the element with id=component_id, then finds the ifl:property child
        with the matching key and sets its value to new_value.
        Returns patched XML string or None if not found.
        """
        try:
            root = ET.fromstring(xml)

            # Find target element by id attribute
            target = None
            for elem in root.iter():
                if elem.get("id") == component_id:
                    target = elem
                    break

            if target is None:
                logger.warning("[DirectPatch] Component id=%s not found", component_id)
                return None

            prop_found = False

            # Search for ifl:property with matching key
            for prop in target.iter():
                tag = (prop.tag.split("}")[-1] if "}" in prop.tag else prop.tag).lower()
                if tag == "property":
                    key_elem = prop.find(".//{*}key") or prop.find("key")
                    val_elem = prop.find(".//{*}value") or prop.find("value")
                    if key_elem is not None and val_elem is not None:
                        if (key_elem.text or "").strip().lower() == property_name.lower():
                            old_val = val_elem.text
                            val_elem.text = new_value
                            prop_found = True
                            logger.info(
                                "[DirectPatch] Changed %s: %r → %r",
                                property_name, old_val, new_value,
                            )
                            break

            if not prop_found:
                # Fallback: try as a direct attribute on the element
                if property_name in target.attrib:
                    target.set(property_name, new_value)
                    prop_found = True
                    logger.info("[DirectPatch] Changed attribute %s → %r", property_name, new_value)

            if not prop_found:
                logger.warning(
                    "[DirectPatch] Property %s not found in component %s",
                    property_name, component_id,
                )
                return None

            return ET.tostring(root, encoding="unicode")

        except Exception as exc:
            logger.warning("[DirectPatch] failed: %s", exc)
            return None

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
