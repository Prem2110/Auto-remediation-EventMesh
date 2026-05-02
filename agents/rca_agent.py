"""
agents/rca_agent.py
===================
RCAAgent — runs LLM-based Root Cause Analysis for a detected CPI incident.

Uses a filtered tool set: only get-iflow and get_message_logs from
integration_suite, keeping the agent focused and preventing it from
accidentally calling deploy or update tools.

Exports:
  RCAAgent
    .run_rca(incident)  → {"root_cause", "proposed_fix", "confidence", ...}
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from agents.base import StepLogger, TestExecutionTracker
from agents.classifier_agent import ClassifierAgent
from core.constants import FALLBACK_FIX_BY_ERROR_TYPE
from db.database import get_similar_patterns
from utils.utils import get_hana_timestamp
from utils.vector_store import get_vector_store

logger = logging.getLogger(__name__)

_WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "false").lower() == "true"


class RCAAgent:
    """
    Performs Root Cause Analysis for a single CPI incident.

    Holds a reference to MultiMCP for tool access.  At construction time,
    a filtered agent (get-iflow + message-logs only) is built so the LLM
    cannot accidentally call update-iflow or deploy-iflow during RCA.
    """

    def __init__(self, mcp):
        self._mcp        = mcp
        self._agent      = None   # set by build_agent()
        self._classifier = ClassifierAgent()

    async def build_agent(self) -> None:
        """Build a filtered RCA agent with local @tool functions + targeted MCP tools."""
        from langchain_core.tools import tool as _tool  # noqa: PLC0415

        _classifier = self._classifier
        _mcp        = self._mcp

        @_tool
        async def get_vector_store_notes(error_description: str) -> str:
            """Search the SAP Notes vector store for guidance relevant to this error."""
            vs    = get_vector_store()
            notes = vs.retrieve_relevant_notes(error_description, "", "", limit=5)
            return vs.format_notes_for_prompt(notes)

        @_tool
        async def get_cross_iflow_patterns(error_type: str, fragment: str) -> str:
            """Find fixes that successfully resolved the same error type on other iFlows."""
            patterns = get_similar_patterns(fragment)
            return str(patterns)

        @_tool
        async def web_search_sap_error(query: str) -> str:
            """
            Search the web for SAP CPI solutions. Always prefix the query with the error
            type or component name for best results (e.g. 'HttpResponseException No consumers
            available SAP CPI iFlow').
            """
            try:
                from duckduckgo_search import DDGS  # noqa: PLC0415
                loop = asyncio.get_running_loop()
                def _search():
                    with DDGS() as ddgs:
                        return list(ddgs.text(f"SAP CPI {query}", max_results=5))
                results = await loop.run_in_executor(None, _search)
                if not results:
                    return "No web results found."
                lines = []
                for r in results:
                    lines.append(f"Title: {r.get('title', '')}")
                    lines.append(f"URL:   {r.get('href', '')}")
                    lines.append(f"Body:  {r.get('body', '')[:400]}")
                    lines.append("---")
                logger.info("[RCA] Web search returned %d result(s) for query: %s", len(results), query)
                return "\n".join(lines)
            except Exception as exc:
                logger.warning("[RCA] Web search failed: %s", exc)
                return f"Web search unavailable: {exc}"

        # Targeted read-only MCP tools — no write/update/deploy
        rca_mcp_names = {
            "get-iflow", "get_message_logs", "get-message-logs",
            "list-iflows", "get_iflow_example", "list_iflow_examples",
        }
        rca_mcp_tools = [
            t for t in _mcp.tools
            if t.mcp_tool_name in rca_mcp_names
            or any(kw in t.mcp_tool_name.lower() for kw in ("message_log", "message-log"))
        ]
        if not rca_mcp_tools:
            rca_mcp_tools = [t for t in _mcp.tools if t.server == "integration_suite"]

        local_tools = [get_vector_store_notes, get_cross_iflow_patterns]
        if _WEB_SEARCH_ENABLED:
            local_tools.append(web_search_sap_error)
        all_tools = local_tools + rca_mcp_tools

        _web_search_line = (
            "- web_search_sap_error      — search the web for SAP CPI solutions (use if SAP Notes are insufficient)\n"
            if _WEB_SEARCH_ENABLED else ""
        )
        _web_search_rule = (
            "- Call web_search_sap_error if SAP Notes and patterns do not provide a clear fix.\n"
            if _WEB_SEARCH_ENABLED else ""
        )
        _max_calls = "8" if _WEB_SEARCH_ENABLED else "6"

        system_prompt = f"""You are an SAP CPI Root Cause Analysis agent.

Your ONLY job is to investigate a failed CPI message and produce a structured diagnosis.

Available local tools:
- get_vector_store_notes    — search SAP Notes for relevant guidance
- get_cross_iflow_patterns  — find fixes that worked for same error on other iFlows
{_web_search_line}
Available MCP tools (read-only):
- get-iflow         — read current iFlow configuration
- get_message_logs  — read message processing log (use only if message GUID provided)

Rules:
- When `knowledge_base_notes` is present in the input — READ IT CAREFULLY; it is your primary
  grounding source. Do NOT call get_vector_store_notes again unless the notes section is empty.
- When `pattern_history` contains a REUSE entry with success_count >= 2 — use it directly with
  confidence = 1.0 and skip additional tool calls (only call get-iflow to confirm the step ID).
- If no pre-fetched notes or patterns are provided, call get_vector_store_notes first.
- Call get_cross_iflow_patterns to check for proven fixes from other iFlows.
{_web_search_rule}- Call get-iflow ONCE to read the current iFlow configuration.
- Call get_message_logs at MOST ONCE if a message GUID is provided.
- Do NOT call update-iflow, deploy-iflow, or any write/modify tool.
- Do NOT ask for human input.
- Return ONLY valid JSON after your investigation — no markdown, no preamble.
- Maximum {_max_calls} tool calls total.

`affected_component` MUST be the exact step ID from the iFlow XML
(e.g. "CallActivity_1", "MessageMapping_1", "ReceiverHTTP_1") — not a generic label like
"adapter" or "mapping". Read the iFlow XML and copy the id= attribute value exactly.

Return exactly:
{{
  "root_cause": "<clear description referencing the specific step/adapter/mapping>",
  "proposed_fix": "<precise diagnosis grounded in the actual iFlow config>",
  "confidence": 0.0,
  "auto_apply": false,
  "error_type": "<error type>",
  "affected_component": "<exact id= attribute value from iFlow XML, e.g. CallActivity_1>"
}}
"""
        self._agent = await _mcp.build_agent(
            tools=all_tools,
            system_prompt=system_prompt,
            deployment_id=os.getenv("LLM_DEPLOYMENT_ID_RCA") or None,
        )
        logger.info(
            "[RCA] Agent ready — %d local tools + %d MCP tools (web_search=%s).",
            len(local_tools), len(rca_mcp_tools), _WEB_SEARCH_ENABLED,
        )

    # ── main entry point ─────────────────────────────────────────────────────

    async def run_rca(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        iflow_id      = (
            incident.get("designtime_artifact_id")
            or incident.get("iflow_id", "")
        )
        error_message = incident.get("error_message", "")
        message_guid  = incident.get("message_guid", "")
        error_type    = incident.get("error_type", "UNKNOWN")

        agent = self._agent or self._mcp.agent
        if agent is None:
            raise RuntimeError(
                "MCP agent is not ready — SAP CPI MCP servers may still be connecting. "
                "Wait a few seconds and retry."
            )

        # ── Pattern history hint ──────────────────────────────────────────────
        sig      = self._classifier.error_signature(iflow_id, error_type, error_message)
        patterns = get_similar_patterns(sig)
        history_hint = ""
        if patterns:
            compact = []
            for p in patterns:
                entry: Dict[str, Any] = {
                    "fix_applied":  p.get("fix_applied", ""),
                    "root_cause":   p.get("root_cause", ""),
                    "success_rate": round(
                        (p.get("success_count") or 0) / max(p.get("applied_count") or 1, 1), 2
                    ),
                }
                if p.get("key_steps"):
                    try:
                        entry["key_steps"] = json.loads(p["key_steps"])
                    except Exception:
                        entry["key_steps"] = p["key_steps"]
                compact.append(entry)
            history_hint = (
                f"\n\nHistorical fix patterns (ranked by success rate):\n"
                f"{json.dumps(compact, indent=2)}"
            )

        # ── SAP Notes from vector store ────────────────────────────────────────
        vector_store      = get_vector_store()
        sap_notes         = vector_store.retrieve_relevant_notes(error_message, error_type, iflow_id, limit=3)
        kb_notes          = vector_store.format_notes_for_prompt(sap_notes) if sap_notes else ""
        iflow_hint        = f"- iFlow ID for config lookup: {iflow_id}" if iflow_id else ""

        # ── MD5-based pattern pre-fetch (matches record_fix_outcome signature) ──
        _md5_sig      = hashlib.md5(f"{iflow_id}:{error_type}".encode()).hexdigest()[:16]
        _md5_patterns = get_similar_patterns(_md5_sig)
        pattern_text  = ""
        if _md5_patterns:
            best = _md5_patterns[0]
            if best.get("success_count", 0) >= 2:
                pattern_text = (
                    f"REUSE THIS FIX (success_count={best['success_count']}, confidence=1.0):\n"
                    f"root_cause: {best.get('root_cause', '')}\n"
                    f"fix_applied: {best.get('fix_applied', '')}\n"
                )

        # ── Prompt — two variants depending on whether we have a message GUID ─
        if message_guid:
            prompt = f"""
AUTONOMOUS RCA — do NOT ask for human input. Maximum 8 tool calls total.

Error detected:
- iFlow:      {iflow_id}
- Error Type: {error_type}
- Message:    {error_message}
- Message ID: {message_guid}
{history_hint}

=== knowledge_base_notes (pre-fetched — use as primary grounding source) ===
{kb_notes}

=== pattern_history (pre-fetched — REUSE if success_count >= 2) ===
{pattern_text}

Steps (execute in order, stop after step 3):
1. Call get_message_logs ONCE for message ID: {message_guid}
2. Call get-iflow ONCE for iFlow ID: {iflow_id} — read the actual configuration to pinpoint which step/adapter/mapping is misconfigured
3. Cross-reference the log error with the iFlow configuration and produce a precise diagnosis

Return ONLY valid JSON (no markdown, no preamble):
{{
  "root_cause": "<clear description referencing the specific iFlow step, adapter, or mapping that is wrong and why>",
  "proposed_fix": "<precise diagnosis grounded in the actual iFlow config — e.g. 'XPath expression /ns1:Order uses prefix ns1 but namespace is not declared in the Message Mapping step MM_OrderTransform', 'Receiver HTTP adapter in step CallStripe has URL path /v1/charges but the Stripe API expects /v1/payment_intents'. Do NOT write XML — the fix agent applies the change.>",
  "confidence": 0.0,
  "auto_apply": false,
  "error_type": "<error type>",
  "affected_component": "<exact id= attribute value from iFlow XML, e.g. CallActivity_1>"
}}

STOP after returning JSON. Do not call any other tools.
"""
        else:
            prompt = f"""
AUTONOMOUS RCA — do NOT ask for human input. No message GUID is available.
{iflow_hint}
{history_hint}

=== knowledge_base_notes (pre-fetched — use as primary grounding source) ===
{kb_notes}

=== pattern_history (pre-fetched — REUSE if success_count >= 2) ===
{pattern_text}

Error detected:
- iFlow:      {iflow_id}
- Error Type: {error_type}
- Message:    {error_message}

Steps (execute in order, stop after step 2):
1. Call get-iflow ONCE for iFlow ID: {iflow_id} — read the actual configuration to identify the misconfigured step
2. Produce a precise diagnosis based on the iFlow config and the error above

Return ONLY valid JSON (no markdown, no preamble):
{{
  "root_cause": "<clear description referencing the specific step or adapter that is wrong and why>",
  "proposed_fix": "<precise diagnosis grounded in the actual iFlow config — name the exact step/adapter and what is wrong. Do NOT write XML — the fix agent applies the change.>",
  "confidence": 0.0,
  "auto_apply": false,
  "error_type": "<error type>",
  "affected_component": "<exact id= attribute value from iFlow XML, e.g. CallActivity_1>"
}}
"""

        timestamp = get_hana_timestamp()
        tracker   = TestExecutionTracker("system_rca", prompt, timestamp)
        logger_cb = StepLogger(tracker)
        messages  = [{"role": "user", "content": prompt}]

        rca: Dict[str, Any] = {}
        for attempt in range(3):
            try:
                result = await agent.ainvoke(
                    {"messages": messages},
                    config={"callbacks": [logger_cb], "recursion_limit": 14},
                )
                final_msg = result["messages"][-1]
                answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
                try:
                    clean = re.sub(r"```(?:json)?|```", "", answer).strip()
                    rca   = json.loads(clean)
                except Exception:
                    match = re.search(r"\{.*\}", answer, re.DOTALL)
                    try:
                        rca = json.loads(match.group(0)) if match else {}
                    except Exception:
                        rca = {}
                break
            except Exception as exc:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                logger.error("[RCA] agent error: %s", exc)
                return {
                    "root_cause":   str(exc),
                    "proposed_fix": "",
                    "confidence":   0.0,
                    "auto_apply":   False,
                    "agent_steps":  logger_cb.steps,
                }

        # ── Confidence floor from rule-based classifier ───────────────────────
        classifier_result  = self._classifier.classify_error(error_message)
        llm_confidence     = float(rca.get("confidence", 0.0))
        final_confidence   = max(llm_confidence, classifier_result["confidence"])
        if final_confidence > llm_confidence:
            logger.info("[RCA] Confidence floor: LLM=%.2f → classifier=%.2f", llm_confidence, final_confidence)

        final_error_type = rca.get("error_type", error_type) or classifier_result["error_type"]
        proposed_fix     = (rca.get("proposed_fix", "") or "").strip()
        root_cause       = (rca.get("root_cause", "") or "").strip()

        if not proposed_fix:
            proposed_fix = FALLBACK_FIX_BY_ERROR_TYPE.get(
                final_error_type, FALLBACK_FIX_BY_ERROR_TYPE["UNKNOWN_ERROR"]
            )
            logger.info("[RCA] Using fallback fix for error type: %s", final_error_type)
        if not root_cause:
            root_cause = self._classifier.fallback_root_cause(final_error_type, error_message)
            logger.info("[RCA] Using fallback root cause for error type: %s", final_error_type)

        logger.info(
            "[RCA_RESULT] iflow=%s error_type=%s confidence=%.2f affected=%s | "
            "root_cause=%.200s | proposed_fix=%.200s",
            iflow_id, final_error_type, final_confidence,
            rca.get("affected_component", ""),
            root_cause, proposed_fix,
        )
        return {
            "root_cause":         root_cause,
            "proposed_fix":       proposed_fix,
            "confidence":         final_confidence,
            "auto_apply":         bool(rca.get("auto_apply", False)),
            "error_type":         final_error_type,
            "affected_component": rca.get("affected_component", ""),
            "agent_steps":        logger_cb.steps,
        }
