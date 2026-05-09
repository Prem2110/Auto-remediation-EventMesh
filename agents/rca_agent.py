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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agents.base import StepLogger, TestExecutionTracker
from agents.classifier_agent import ClassifierAgent
from core.constants import FALLBACK_FIX_BY_ERROR_TYPE
from db.database import get_similar_patterns
from utils.utils import clean_error_message, get_hana_timestamp
from utils.vector_store import get_vector_store

logger = logging.getLogger(__name__)

_WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "false").lower() == "true"

# Default property to inspect per error type when RCA returns fixes:[] but
# affected_component is known.  Used by the post-RCA XML fill-in pass.
_ERROR_TYPE_LIKELY_PROPERTIES: Dict[str, List[str]] = {
    "BACKEND_ERROR":      ["Address", "address"],
    "HTTP_CALL_FAILED":   ["Address", "address", "httpMethod"],
    "CONNECTIVITY_ERROR": ["Address", "address"],
    "AUTH_ERROR":         ["credentialName", "CredentialName"],
    "AUTH_CONFIG_ERROR":  ["credentialName", "CredentialName"],
    "SCRIPT_ERROR":       ["scriptRef", "script", "fileName"],
    "MAPPING_ERROR":      ["mappingRef", "fileName"],
}


def _extract_prop_from_xml(xml_raw: str, component_id: str, prop_name: str) -> str:
    """
    Extract a single ifl:property value from raw get-iflow output.
    Handles both concatenated-package format (with ---begin-of-file--- markers)
    and plain XML.  Returns "" on any failure.
    """
    if not xml_raw or not component_id or not prop_name:
        return ""
    import xml.etree.ElementTree as _ET  # noqa: PLC0415

    xml_str = xml_raw
    if "---begin-of-file---" in xml_raw:
        m = re.search(
            r"integrationflow/[^\n]+\n---begin-of-file---\n(.*?)---end-of-file---",
            xml_raw, re.DOTALL,
        )
        if m:
            xml_str = m.group(1).lstrip("﻿")

    try:
        root = _ET.fromstring(xml_str.strip().lstrip("﻿"))
        target = None
        for elem in root.iter():
            if elem.get("id") == component_id:
                target = elem
                break
        if target is None:
            return ""
        prop_lower = prop_name.lower()
        for prop in target.iter():
            key_elem = prop.find("key") or prop.find("{*}key")
            val_elem = prop.find("value") or prop.find("{*}value")
            if key_elem is not None and val_elem is not None:
                if (key_elem.text or "").lower() == prop_lower:
                    return (val_elem.text or "").strip()
    except Exception:
        pass
    return ""


@dataclass
class FixStep:
    """One atomic property change produced by RCA."""
    component_id: str
    property_to_change: str
    current_value: str
    correct_value: str


def get_all_fixes(rca: Dict[str, Any]) -> List[FixStep]:
    """
    Return the list of FixStep objects from a raw RCA result dict.

    Prefers the `fixes` list when present; falls back to the legacy
    single-fix fields (property_to_change / current_value / correct_value +
    affected_component) so older RCA results still route through DirectPatch.
    """
    steps: List[FixStep] = []
    for item in (rca.get("fixes") or []):
        if not isinstance(item, dict):
            continue
        cid = (item.get("component_id") or "").strip()
        ptc = (item.get("property_to_change") or "").strip()
        cv  = (item.get("correct_value") or "").strip()
        if cid and ptc and cv:
            steps.append(FixStep(
                component_id=cid,
                property_to_change=ptc,
                current_value=(item.get("current_value") or ""),
                correct_value=cv,
            ))

    # Legacy fallback: single-fix scalar fields
    if not steps:
        ptc = (rca.get("property_to_change") or "").strip()
        cv  = (rca.get("correct_value") or "").strip()
        ac  = (rca.get("affected_component") or "").strip()
        if ptc and cv and ac and ac.lower() not in ("unknown", ""):
            steps.append(FixStep(
                component_id=ac,
                property_to_change=ptc,
                current_value=(rca.get("current_value") or ""),
                correct_value=cv,
            ))
    return steps


class RCAAgent:
    """
    Performs Root Cause Analysis for a single CPI incident.

    Holds a reference to MultiMCP for tool access.  At construction time,
    a filtered agent (get-iflow + message-logs only) is built so the LLM
    cannot accidentally call update-iflow or deploy-iflow during RCA.
    """

    def __init__(self, mcp):
        self._mcp                  = mcp
        self._agent                = None   # set by build_agent()
        self._classifier           = ClassifierAgent()
        self._current_error_type   = ""     # set by run_rca before agent invoke
        self._current_iflow_id     = ""     # set by run_rca before agent invoke

    async def build_agent(self) -> None:
        """Build a filtered RCA agent with local @tool functions + targeted MCP tools."""
        from langchain_core.tools import tool as _tool  # noqa: PLC0415

        _classifier = self._classifier
        _mcp        = self._mcp
        _agent_self = self   # captured for closure — run_rca sets _current_* before invoke

        @_tool
        async def get_vector_store_notes(error_description: str) -> str:
            """Search the SAP Notes vector store for guidance relevant to this error."""
            vs    = get_vector_store()
            notes = vs.retrieve_relevant_notes(
                error_description,
                _agent_self._current_error_type or "",
                _agent_self._current_iflow_id   or "",
                limit=5,
            )
            result = vs.format_notes_for_prompt(notes)
            logger.info(
                "[RCA] get_vector_store_notes tool returned %d note(s) for error_type=%s iflow=%s",
                len(notes), _agent_self._current_error_type, _agent_self._current_iflow_id,
            )
            return result

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
- For HTTP_CALL_FAILED and CONNECTIVITY_ERROR: call get_message_logs FIRST (before get-iflow)
  when a message GUID is available. Read the actual outbound request URL from the log — it is
  more accurate than the iFlow config value (which may contain expressions that resolve differently
  at runtime). Use the logged URL to determine the exact fix (typo, missing path segment, wrong host).
- For all other error types: call get_message_logs at MOST ONCE if a message GUID is provided.
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
  "affected_component": "<exact id= attribute value from iFlow XML — use the FIRST affected component>",
  "property_to_change": "<primary property name> or null",
  "current_value":      "<primary broken value from iFlow XML> or null",
  "correct_value":      "<primary correct value to set> or null",
  "fixes": [
    {{
      "component_id":      "<exact id= attribute value from iFlow XML>",
      "property_to_change": "<exact ifl:property key name>",
      "current_value":      "<the broken value as it exists now in the iFlow XML>",
      "correct_value":      "<the exact correct value to set>"
    }}
  ]
}}

Rules for `fixes`:
- ALWAYS populate `fixes` as a list — even for a single change.
- Each entry targets one ifl:property key on one component.
- When multiple properties must change simultaneously (e.g. httpShouldSendBody AND isDefault),
  add one entry per property — all will be applied atomically in a single DirectPatch call.
- Read ALL values from the actual get-iflow output — never guess.
- If the fix requires structural changes (add/remove steps) → return `fixes: []` and set the
  scalar fields to null.
- The scalar fields (property_to_change / current_value / correct_value) MUST mirror the first
  entry in `fixes` so older consumers remain compatible.

Example — two simultaneous property changes on the same component:
  "affected_component": "ReceiverHTTP_1",
  "property_to_change": "httpShouldSendBody",
  "current_value": "false",
  "correct_value": "true",
  "fixes": [
    {{"component_id": "ReceiverHTTP_1", "property_to_change": "httpShouldSendBody", "current_value": "false", "correct_value": "true"}},
    {{"component_id": "ReceiverHTTP_1", "property_to_change": "isDefault",          "current_value": "false", "correct_value": "true"}}
  ]

CRITICAL — always populate `current_value` in every fixes entry:
- Copy the exact value verbatim from the iFlow XML returned by get-iflow.
- Even if you are uncertain about `correct_value`, you MUST still set `current_value`.
- The fix engine uses `current_value` to locate the XML element for surgical patching.
- An empty `current_value` forces a slower, riskier full-XML LLM rewrite that can
  accidentally remove routing rules or other structural elements.

Per error type — primary property to extract from get-iflow output:
  BACKEND_ERROR / HTTP_CALL_FAILED   → Address property on the failing HTTP receiver adapter
  CONNECTIVITY_ERROR                 → Address property; also check timeout properties
  AUTH_ERROR / AUTH_CONFIG_ERROR     → credentialName property on the receiver adapter
  SCRIPT_ERROR                       → scriptRef or fileName property on the Script step
  MAPPING_ERROR                      → mappingRef or fileName property on the Message Mapping step

CRITICAL — XPath namespace fix format (applies to MAPPING_ERROR with XPathException):
  SAP CPI's Saxon engine REJECTS inline 'declare namespace' inside XPath values.
  The correct fix requires TWO property changes:
    1. collaboration namespaceMapping → correct_value must be "xmlns:prefix=uri"
       e.g. "xmlns:d=http://schemas.microsoft.com/ado/2007/08/dataservices"
       (NOT "d=http://..." — SAP deploy will reject the short form)
    2. the XPath property (e.g. wrapContent) → correct_value must remove the inline
       'declare namespace ...; ' prefix and use just the bare XPath: //prefix:element
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
        error_message = clean_error_message(incident.get("error_message", ""))
        message_guid  = incident.get("message_guid", "")
        error_type    = incident.get("error_type", "UNKNOWN")

        # Expose per-run context to the get_vector_store_notes closure built in build_agent().
        self._current_error_type = error_type or ""
        self._current_iflow_id   = iflow_id   or ""

        # LLM second-pass reclassification for low-signal UNKNOWN_ERROR cases
        if error_type in ("UNKNOWN_ERROR", "", None):
            try:
                self._classifier._mcp = self._mcp
                reclassified = await self._classifier.reclassify_with_llm(
                    error_message, iflow_id
                )
                if reclassified.get("confidence", 0) > 0.60:
                    logger.info(
                        "[RCA] UNKNOWN_ERROR reclassified → %s (%.2f)",
                        reclassified["error_type"],
                        reclassified["confidence"],
                    )
                    error_type = reclassified["error_type"]
                    incident["error_type"] = error_type
            except Exception as e:
                logger.warning("[RCA] LLM reclassification failed: %s", e)

        agent = self._agent or self._mcp.agent
        if agent is None:
            # Startup may have failed — attempt a lazy rebuild before giving up
            try:
                logger.warning("[RCA] Agent not ready at run time — attempting lazy build.")
                await self.build_agent()
                agent = self._agent or self._mcp.agent
            except Exception as build_exc:
                logger.error("[RCA] Lazy build failed: %s", build_exc)
        if agent is None:
            raise RuntimeError(
                "MCP agent is not ready — SAP CPI MCP servers may still be connecting. "
                "Wait a few seconds and retry."
            )

        # ── Parallel pre-fetch: DB lookups + MCP reads all at once ──────────────
        from db.database import get_patterns_by_error_type as _get_cross  # noqa: PLC0415

        sig      = self._classifier.error_signature(iflow_id, error_type, error_message)
        _md5_sig = hashlib.md5(f"{iflow_id}:{error_type}".encode()).hexdigest()[:16]

        async def _fetch_patterns():
            return get_similar_patterns(sig)

        async def _fetch_md5_patterns():
            return get_similar_patterns(_md5_sig)

        async def _fetch_cross_patterns():
            return _get_cross(error_type, min_success_count=1, limit=3)

        async def _fetch_sap_notes():
            vs = get_vector_store()
            notes = vs.retrieve_relevant_notes(error_message, error_type, iflow_id, limit=3)
            return vs, notes

        async def _fetch_iflow_xml():
            if not iflow_id or not self._mcp:
                return ""
            try:
                r = await self._mcp.execute_integration_tool("get-iflow", {"id": iflow_id})
                return str(r.get("output", "")).strip() if r.get("success") else ""
            except Exception as exc:
                logger.warning("[RCA] iFlow XML pre-fetch failed for %s: %s", iflow_id, exc)
                return ""

        async def _fetch_message_logs():
            if not message_guid or not self._mcp:
                return ""
            try:
                r = await self._mcp.execute_integration_tool(
                    "get_message_logs", {"message_guid": message_guid}
                )
                return str(r.get("output", "")).strip() if r.get("success") else ""
            except Exception as exc:
                logger.warning("[RCA] Message logs pre-fetch failed for %s: %s", message_guid, exc)
                return ""

        _gathered = await asyncio.gather(
            _fetch_patterns(),
            _fetch_md5_patterns(),
            _fetch_cross_patterns(),
            _fetch_sap_notes(),
            _fetch_iflow_xml(),
            _fetch_message_logs(),
            return_exceptions=True,
        )
        _r_patterns, _r_md5, _r_cross, _r_notes, _r_xml, _r_logs = _gathered

        patterns      = _r_patterns  if isinstance(_r_patterns, list)  else []
        _md5_patterns = _r_md5       if isinstance(_r_md5, list)       else []
        _cross_patterns = _r_cross   if isinstance(_r_cross, list)     else []
        iflow_xml     = _r_xml       if isinstance(_r_xml, str)        else ""
        raw_message_logs = _r_logs   if isinstance(_r_logs, str)       else ""

        if isinstance(_r_notes, tuple) and len(_r_notes) == 2:
            vector_store, sap_notes = _r_notes
        else:
            vector_store, sap_notes = get_vector_store(), []

        logger.info(
            "[RCA] Parallel pre-fetch complete | iflow_xml=%d chars | logs=%d chars | "
            "patterns=%d | md5=%d | cross=%d | notes=%d",
            len(iflow_xml), len(raw_message_logs),
            len(patterns), len(_md5_patterns), len(_cross_patterns), len(sap_notes),
        )

        # ── Assemble text blocks from pre-fetched data ────────────────────────
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

        kb_notes = vector_store.format_notes_for_prompt(sap_notes) if sap_notes else ""
        if kb_notes:
            logger.info(
                "[RCA] Pre-fetched %d SAP note(s) from vector store | error_type=%s | iflow=%s",
                len(sap_notes), error_type, iflow_id,
            )
        else:
            logger.warning(
                "[RCA] Vector store returned NO notes for error_type=%s iflow=%s — "
                "RCA will rely on LLM knowledge only. "
                "Check HANA_TABLE_VECTOR and EMBEDDING_DEPLOYMENT_ID env vars.",
                error_type, iflow_id,
            )
        iflow_hint = f"- iFlow ID for config lookup: {iflow_id}" if iflow_id else ""

        pattern_text = ""
        if _md5_patterns:
            best = _md5_patterns[0]
            if best.get("success_count", 0) >= 2:
                pattern_text = (
                    f"REUSE THIS FIX (success_count={best['success_count']}, confidence=1.0):\n"
                    f"root_cause: {best.get('root_cause', '')}\n"
                    f"fix_applied: {best.get('fix_applied', '')}\n"
                )

        cross_iflow_text = ""
        if _cross_patterns:
            lines = ["=== PROVEN FIXES FROM OTHER IFLOWS (same error type) ==="]
            for _cp in _cross_patterns:
                if _cp.get("iflow_id") != iflow_id:
                    lines.append(
                        f"iFlow: {_cp.get('iflow_id')} | "
                        f"root_cause: {(_cp.get('root_cause') or '')[:120]} | "
                        f"fix: {(_cp.get('fix_applied') or '')[:150]} | "
                        f"success_count: {_cp.get('success_count', 0)}"
                    )
            if len(lines) > 1:
                cross_iflow_text = "\n".join(lines)
                logger.info(
                    "[RCA] Pre-fetched %d cross-iFlow pattern(s) for error_type=%s iflow=%s",
                    len(lines) - 1, error_type, iflow_id,
                )

        # Sections injected into the prompt so the LLM skips redundant tool calls
        _iflow_section = (
            f"\n=== iFlow XML (pre-fetched — do NOT call get-iflow again) ===\n{iflow_xml}\n"
            if iflow_xml and not iflow_xml.startswith("ERROR")
            else ""
        )
        _logs_section = (
            f"\n=== Message Processing Log (pre-fetched — do NOT call get_message_logs again) ===\n{raw_message_logs}\n"
            if raw_message_logs and not raw_message_logs.startswith("ERROR")
            else ""
        )

        # ── Prompt — two variants depending on whether we have a message GUID ─
        # iFlow XML and message logs are pre-fetched and injected directly.
        # The LLM must skip tool calls for data that is already provided.
        _has_iflow  = bool(_iflow_section)
        _has_logs   = bool(_logs_section)

        if message_guid:
            _step_instructions = (
                "The iFlow XML and message logs are provided below — analyse them directly.\n"
                "Only call get-iflow or get_message_logs if the pre-fetched data is missing or truncated."
                if _has_iflow and _has_logs else
                "Steps (execute in order, stop after step 3):\n"
                f"1. Call get_message_logs ONCE for message ID: {message_guid}\n"
                f"2. Call get-iflow ONCE for iFlow ID: {iflow_id}\n"
                "3. Cross-reference the log error with the iFlow configuration and produce a precise diagnosis"
            )
            prompt = f"""
AUTONOMOUS RCA — do NOT ask for human input. Maximum 6 tool calls total.

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

=== cross_iflow_patterns (proven fixes from other iFlows — use as reference) ===
{cross_iflow_text}
{_logs_section}{_iflow_section}
{_step_instructions}

Return ONLY valid JSON (no markdown, no preamble):
{{
  "root_cause": "<clear description referencing the specific iFlow step, adapter, or mapping that is wrong and why>",
  "proposed_fix": "<precise diagnosis grounded in the actual iFlow config — e.g. 'XPath expression /ns1:Order uses prefix ns1 but namespace is not declared in the Message Mapping step MM_OrderTransform', 'Receiver HTTP adapter in step CallStripe has URL path /v1/charges but the Stripe API expects /v1/payment_intents'. Do NOT write XML — the fix agent applies the change.>",
  "confidence": 0.0,
  "auto_apply": false,
  "error_type": "<error type>",
  "affected_component": "<exact id= attribute value from iFlow XML — first affected component>",
  "property_to_change": "<primary property name> or null",
  "current_value":      "<primary broken value from iFlow XML> or null",
  "correct_value":      "<primary correct value to set> or null",
  "fixes": [
    {{
      "component_id":       "<exact id= attribute value>",
      "property_to_change": "<exact ifl:property key>",
      "current_value":      "<broken value from iFlow XML>",
      "correct_value":      "<correct value to set>"
    }}
  ]
}}

Always populate `fixes` as a list (one entry per property change). Mirror the first entry in the
scalar fields for backwards compatibility. For structural changes set `fixes: []` and scalars to null.

STOP after returning JSON. Do not call any other tools.
"""
        else:
            _step_instructions = (
                "The iFlow XML is provided below — analyse it directly.\n"
                "Only call get-iflow if the pre-fetched XML is missing or truncated."
                if _has_iflow else
                f"Steps (execute in order, stop after step 2):\n"
                f"1. Call get-iflow ONCE for iFlow ID: {iflow_id}\n"
                "2. Produce a precise diagnosis based on the iFlow config and the error above"
            )
            prompt = f"""
AUTONOMOUS RCA — do NOT ask for human input. No message GUID is available.
{iflow_hint}
{history_hint}

=== knowledge_base_notes (pre-fetched — use as primary grounding source) ===
{kb_notes}

=== pattern_history (pre-fetched — REUSE if success_count >= 2) ===
{pattern_text}

=== cross_iflow_patterns (proven fixes from other iFlows — use as reference) ===
{cross_iflow_text}
{_iflow_section}
Error detected:
- iFlow:      {iflow_id}
- Error Type: {error_type}
- Message:    {error_message}

{_step_instructions}

Return ONLY valid JSON (no markdown, no preamble):
{{
  "root_cause": "<clear description referencing the specific step or adapter that is wrong and why>",
  "proposed_fix": "<precise diagnosis grounded in the actual iFlow config — name the exact step/adapter and what is wrong. Do NOT write XML — the fix agent applies the change.>",
  "confidence": 0.0,
  "auto_apply": false,
  "error_type": "<error type>",
  "affected_component": "<exact id= attribute value from iFlow XML — first affected component>",
  "property_to_change": "<primary property name> or null",
  "current_value":      "<primary broken value from iFlow XML> or null",
  "correct_value":      "<primary correct value to set> or null",
  "fixes": [
    {{
      "component_id":       "<exact id= attribute value>",
      "property_to_change": "<exact ifl:property key>",
      "current_value":      "<broken value from iFlow XML>",
      "correct_value":      "<correct value to set>"
    }}
  ]
}}

Always populate `fixes` as a list (one entry per property change). Mirror the first entry in the
scalar fields for backwards compatibility. For structural changes set `fixes: []` and scalars to null.
"""

        timestamp = get_hana_timestamp()
        tracker   = TestExecutionTracker("system_rca", prompt, timestamp)
        logger_cb = StepLogger(tracker)
        messages  = [{"role": "user", "content": prompt}]

        rca: Dict[str, Any] = {}
        for attempt in range(3):
            try:
                result = await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": messages},
                        config={"callbacks": [logger_cb], "recursion_limit": 14},
                    ),
                    timeout=180.0,
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
            except asyncio.TimeoutError:
                logger.warning("[RCA] agent timed out on attempt %d/3 for iflow=%s", attempt + 1, iflow_id)
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                logger.error("[RCA] All 3 attempts timed out for iflow=%s — using fallback", iflow_id)
                # Return a fallback so the pipeline can still attempt a fix
                _clf = self._classifier.classify_error(error_message)
                _fallback_fix = FALLBACK_FIX_BY_ERROR_TYPE.get(
                    _clf.get("error_type", "UNKNOWN_ERROR"),
                    FALLBACK_FIX_BY_ERROR_TYPE["UNKNOWN_ERROR"],
                )
                return {
                    "root_cause":         f"RCA timed out — classifier suggests {_clf.get('error_type')}: {error_message[:200]}",
                    "proposed_fix":       _fallback_fix,
                    "confidence":         min(_clf.get("confidence", 0.50), 0.65),
                    "auto_apply":         False,
                    "error_type":         _clf.get("error_type", error_type),
                    "affected_component": "",
                    "property_to_change": "",
                    "current_value":      "",
                    "correct_value":      "",
                    "fixes":              [],
                    "agent_steps":        logger_cb.steps,
                }
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

        all_fixes = get_all_fixes(rca)

        # Boost confidence when RCA produced complete, actionable property-level fixes.
        # A fully populated fixes entry (current_value + correct_value both non-empty)
        # means DirectPatch + value-search can apply the change deterministically — the
        # risk profile is much lower than a free-XML LLM rewrite, so the threshold is met.
        if all_fixes and all(
            (f.current_value if hasattr(f, "current_value") else (f.get("current_value") if isinstance(f, dict) else ""))
            and (f.correct_value if hasattr(f, "correct_value") else (f.get("correct_value") if isinstance(f, dict) else ""))
            for f in all_fixes
        ):
            _boosted = min(final_confidence + 0.10, 0.95)
            if _boosted > final_confidence:
                logger.info(
                    "[RCA] Confidence boosted %.2f → %.2f — %d complete fix entr%s "
                    "(direct patch eligible)",
                    final_confidence, _boosted, len(all_fixes),
                    "y" if len(all_fixes) == 1 else "ies",
                )
                final_confidence = _boosted

        # Cross-check: verify each current_value from RCA actually exists in the iFlow XML
        # that the agent fetched.  If RCA hallucinated a value, direct_patch will silently
        # produce a no-op — the wrong iFlow gets redeployed and the error persists.
        # Extract the raw get-iflow output from agent steps to run this check without a
        # second network call.
        _iflow_xml_raw = ""
        for _step in logger_cb.steps:
            _tool_name = str(_step.get("tool", ""))
            if "get-iflow" in _tool_name or "get_iflow" in _tool_name:
                _iflow_xml_raw = str(_step.get("output", ""))
                break

        if _iflow_xml_raw and all_fixes:
            _mismatched = [
                f for f in all_fixes
                if (
                    (f.current_value if hasattr(f, "current_value") else (f.get("current_value") if isinstance(f, dict) else ""))
                    and (f.current_value if hasattr(f, "current_value") else f.get("current_value", ""))
                    not in _iflow_xml_raw
                )
            ]
            if _mismatched:
                _mis_labels = [
                    f"{(f.property_to_change if hasattr(f, 'property_to_change') else f.get('property_to_change', ''))}="
                    f"{(f.current_value if hasattr(f, 'current_value') else f.get('current_value', ''))!r}"
                    for f in _mismatched
                ]
                logger.warning(
                    "[RCA] current_value mismatch for iflow=%s — %d fix(es) reference values "
                    "not found in fetched iFlow XML (possible hallucination or stale value). "
                    "Direct patch will likely be skipped; LLM path will attempt the fix. "
                    "Mismatched: %s",
                    iflow_id, len(_mismatched), "; ".join(_mis_labels),
                )
                # Cap confidence so the mismatched fixes don't auto-deploy without LLM review.
                final_confidence = min(final_confidence, 0.75)

        # ── Post-RCA fill-in pass ─────────────────────────────────────────────
        # When the LLM left current_value blank, try to extract it directly from
        # the iFlow XML the agent already fetched — no extra network call needed.
        if _iflow_xml_raw and all_fixes:
            filled = []
            for fix in all_fixes:
                cv = fix.current_value if hasattr(fix, "current_value") else fix.get("current_value", "")
                if not cv and fix.component_id and fix.property_to_change:
                    extracted = _extract_prop_from_xml(_iflow_xml_raw, fix.component_id, fix.property_to_change)
                    if extracted:
                        logger.info(
                            "[RCA] Auto-filled current_value for %s.%s=%r",
                            fix.component_id, fix.property_to_change, extracted,
                        )
                        fix = FixStep(
                            component_id=fix.component_id,
                            property_to_change=fix.property_to_change,
                            current_value=extracted,
                            correct_value=fix.correct_value,
                        )
                filled.append(fix)
            all_fixes = filled

        # When fixes is empty but affected_component is known, infer the most
        # likely property from the error type and seed a partial FixStep so the
        # fix agent has a concrete XML target to work with.
        if not all_fixes and _iflow_xml_raw:
            ac = rca.get("affected_component", "").strip()
            if ac and ac.lower() not in ("unknown", ""):
                for prop in _ERROR_TYPE_LIKELY_PROPERTIES.get(final_error_type, []):
                    extracted = _extract_prop_from_xml(_iflow_xml_raw, ac, prop)
                    if extracted:
                        logger.info(
                            "[RCA] Inferred fix target: error_type=%s component=%s property=%s current=%r",
                            final_error_type, ac, prop, extracted,
                        )
                        all_fixes = [FixStep(
                            component_id=ac,
                            property_to_change=prop,
                            current_value=extracted,
                            correct_value="",
                        )]
                        break

        # Re-run the confidence boost check now that fill-in may have completed entries.
        if all_fixes and all(
            (f.current_value if hasattr(f, "current_value") else f.get("current_value", ""))
            and (f.correct_value if hasattr(f, "correct_value") else f.get("correct_value", ""))
            for f in all_fixes
        ):
            _boosted2 = min(final_confidence + 0.10, 0.95)
            if _boosted2 > final_confidence:
                logger.info(
                    "[RCA] Post-fill confidence boost %.2f → %.2f — all %d fix entr%s complete",
                    final_confidence, _boosted2, len(all_fixes),
                    "y" if len(all_fixes) == 1 else "ies",
                )
                final_confidence = _boosted2

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
            "property_to_change": (rca.get("property_to_change") or ""),
            "current_value":      (rca.get("current_value") or ""),
            "correct_value":      (rca.get("correct_value") or ""),
            "fixes":              all_fixes,
            "agent_steps":        logger_cb.steps,
        }
