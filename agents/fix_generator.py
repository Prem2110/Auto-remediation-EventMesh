"""
agents/fix_generator.py
=======================
FixGenerator — produces a PatchSpec by either wrapping structured operations
(no LLM call) or running the full free-XML LLM agent pipeline.

Exports:
  PatchSpec     — dataclass returned by generate()
  FixGenerator  — async generate(ctx, strategy, progress_fn) -> PatchSpec
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional

from agents.base import StepLogger, TestExecutionTracker, extract_token_counts
from db.database import log_agent_event
from agents.fix_context import FixContext
from agents.fix_planner import FixStrategy
from core.constants import (
    CPI_IFLOW_GROOVY_RULES,
    CPI_IFLOW_XML_PATTERNS,
    FIX_AND_DEPLOY_PROMPT_TEMPLATE,
)
from core.validators import _fix_ctx

logger = logging.getLogger(__name__)

_WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "false").lower() == "true"

# Validation errors the free-XML agent cannot self-correct: structural regressions
# it introduced itself (e.g. removed a CBR default route while rewriting routing).
_STRUCTURAL_PATTERNS: tuple = (
    "no default route",
    "exclusivegateway",
    "ifl:property elements found inside",
)


def _find_fatal_validation(steps: List[Dict]) -> str:
    """
    Return the first FATAL: tool output from validate_iflow_xml steps,
    or '' if no structural validation block was found.
    """
    for step in steps:
        if "validate_iflow_xml" not in str(step.get("tool", "")):
            continue
        out = str(step.get("output", ""))
        if "FATAL:" in out:
            return out
    return ""


@dataclass(frozen=True)
class PatchSpec:
    mode: Literal["structured", "free_xml"]
    operations: List[Dict]
    raw_xml: str
    raw_answer: str
    steps: List[Dict] = field(default_factory=list)


class FixGenerator:
    """
    Owns the fix+deploy LLM agent (validate, update, deploy tools).
    For structured mode: wraps ops in a PatchSpec with no LLM call.
    For free_xml mode: runs the full LLM pipeline and returns PatchSpec
    containing the tool-call steps.
    """

    def __init__(self, mcp) -> None:
        self._mcp   = mcp
        self._agent = None

    async def build_agent(self) -> None:
        """Build the fix+deploy agent with validate, record_outcome, and 3 MCP tools."""
        from langchain_core.tools import tool as _tool                              # noqa: PLC0415
        from core.validators import _check_iflow_xml, _fix_ctx_store, _fix_ctx_lock  # noqa: PLC0415

        @_tool
        def validate_iflow_xml(xml_content: str) -> str:
            """
            Validate iFlow XML structure before sending to SAP CPI.
            Runs 7 structural checks. Returns 'VALID', 'ERRORS: <list>', or
            'FATAL: <list>' for structural regressions that cannot be self-corrected.
            """
            # Read original XML from the process-global store (replaces ContextVar shim
            # which always returned None from inside a LangChain agent sub-task).
            with _fix_ctx_lock:
                _ctx_entry = next(iter(_fix_ctx_store.values()), None)
            original_xml = _ctx_entry["xml"] if _ctx_entry else ""
            errors       = _check_iflow_xml(original_xml, xml_content)
            if not errors:
                return "VALID"
            el       = " | ".join(errors)
            el_lower = el.lower()
            if any(p in el_lower for p in _STRUCTURAL_PATTERNS):
                return (
                    "FATAL: " + el
                    + "\n\nThis is a structural regression you cannot fix."
                    " STOP immediately — return JSON with failed_stage='validation_blocked'."
                )
            return "ERRORS: " + el

        @_tool
        def record_fix_outcome(
            iflow_id: str,
            error_type: str,
            root_cause: str,
            fix_applied: str,
            outcome: str,
        ) -> str:
            """Record a fix outcome (SUCCESS or FAILED) in fix_patterns table."""
            from db.database import upsert_fix_pattern  # noqa: PLC0415
            import hashlib                              # noqa: PLC0415
            sig = hashlib.md5(f"{iflow_id}:{error_type}".encode()).hexdigest()[:16]
            try:
                upsert_fix_pattern({
                    "error_signature": sig,
                    "iflow_id":        iflow_id,
                    "error_type":      error_type,
                    "root_cause":      root_cause,
                    "fix_applied":     fix_applied,
                    "outcome":         outcome,
                })
                return f"Fix pattern recorded for {iflow_id} ({outcome})"
            except Exception as e:
                return f"Error recording fix pattern: {e}"

        get_iflow_tool       = self._mcp.get_mcp_tool("integration_suite", "get-iflow")
        update_iflow_tool    = self._mcp.get_mcp_tool("integration_suite", "update-iflow")
        deploy_iflow_tool    = self._mcp.get_mcp_tool("integration_suite", "deploy-iflow")
        list_examples_tool   = self._mcp.get_mcp_tool("integration_suite", "list-iflow-examples")
        get_example_tool     = self._mcp.get_mcp_tool("integration_suite", "get-iflow-example")
        get_msg_logs_tool    = self._mcp.get_mcp_tool("integration_suite", "get_message_logs")

        mcp_tools = [
            t for t in [
                get_iflow_tool, update_iflow_tool, deploy_iflow_tool,
                list_examples_tool, get_example_tool, get_msg_logs_tool,
            ] if t
        ]
        if not mcp_tools:
            mcp_tools = [t for t in self._mcp.tools if t.server == "integration_suite"]

        local_tools = [validate_iflow_xml, record_fix_outcome]

        if _WEB_SEARCH_ENABLED:
            @_tool
            async def web_search_sap_fix(query: str) -> str:
                """
                Search the web for SAP CPI configuration, adapter setup, or API endpoint
                guidance when the correct value or structure is uncertain.
                Always include the component type and specific question, e.g.:
                  'SAP CPI HTTP receiver adapter OAuth2 credential alias format'
                  'SAP CPI OData sender address URL format S/4HANA cloud'
                Use ONLY when the iFlow XML and RCA do not provide a confirmed correct value.
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
                        lines.append(f"Body:  {r.get('body', '')[:300]}")
                        lines.append("---")
                    logger.info("[FixGenerator] Web search returned %d result(s) for: %s", len(results), query)
                    return "\n".join(lines)
                except Exception as exc:
                    logger.warning("[FixGenerator] Web search failed: %s", exc)
                    return f"Web search unavailable: {exc}"

            local_tools.append(web_search_sap_fix)

        all_tools = local_tools + mcp_tools

        _web_search_step = (
            "\nOPTIONAL — STEP 1.5: If the correct value for a URL, credential alias, or adapter"
            "\nproperty is not confirmed by the iFlow XML or RCA, call web_search_sap_fix with a"
            "\nspecific query (e.g. 'HTTP receiver adapter OAuth2 credential name format SAP CPI')."
            "\nUse ONLY confirmed values from the search — never apply a guessed value.\n"
        ) if _WEB_SEARCH_ENABLED else ""

        system_prompt = """You are the FixAgent in a SAP CPI self-healing pipeline.
Your ONLY job is to fix and deploy broken SAP CPI iFlows.

=== MANDATORY TOOL CALLS — EXECUTE IN ORDER, NO SKIPPING ===

STEP 1: Call get-iflow with the iFlow ID provided in the user message.
  - If get-iflow fails → STOP immediately. Return failed_stage="get".

STEP 2: Apply the fix described in the RCA.

Read root_cause and proposed_fix carefully. They tell you exactly what is broken and what to change.

Your job:
- Find the exact XML property, adapter, or step mentioned in proposed_fix
- Change ONLY that specific value
- Do NOT change anything else in the iFlow
- Do NOT add or remove steps
- Do NOT restructure sequence flows

Examples of what you might change:
- A URL/Address property value (fix typo, add missing path)
- A CredentialName property value
- A mapping field name
- An adapter configuration property
- A script file reference

The SAP CPI iFlow XML structure is standard — you already know how to read it. Trust the RCA output.
__WEB_SEARCH_STEP__
STEP 3: Call validate_iflow_xml with the modified XML.
  - If ERRORS returned → fix the XML issues and re-validate.
  - Do NOT call update-iflow until validate_iflow_xml returns "VALID".

CRITICAL: If validate_iflow_xml returns ERRORS:
  - You MUST fix ALL reported errors before calling update-iflow.
  - Do NOT call update-iflow with XML that has validation errors.
  - Re-read the ERRORS list carefully — fix each one specifically.
  - Call validate_iflow_xml again after fixing.
  - Only proceed to update-iflow when validate_iflow_xml returns VALID.
  - If you cannot fix the validation errors after 2 attempts → STOP,
    return failed_stage="validation" immediately.
  - NEVER deploy XML that has not passed validation.
  - If validate_iflow_xml returns FATAL: → STOP immediately.
    Do NOT call any more tools. Return JSON with failed_stage="validation_blocked".
    These are structural regressions you introduced — they cannot be self-corrected.

=== USING REFERENCE COMPONENT EXAMPLES (AUTH_CONFIG_ERROR, HTTP_CALL_FAILED, adapter misconfig) ===
When fixing an adapter configuration error (wrong credential alias, wrong auth type, wrong
HTTP method/content-type, misconfigured receiver):

BEFORE modifying the XML:
  1. Call list-iflow-examples — returns a list of COMPONENT names
     (e.g. "HTTP Receiver", "OData Sender", "SOAP Receiver", "Basic Authentication").
  2. Pick the name that most closely matches the failing component type.
  3. Call get-iflow-example with that name — returns the XML for that INDIVIDUAL COMPONENT
     (the root element is the component itself, not a full iFlow).
  4. Use the returned component XML as a structural reference for the correct property layout.
     Copy only the relevant properties — do not replace the entire component verbatim.

This is MANDATORY for AUTH_CONFIG_ERROR and strongly recommended for HTTP_CALL_FAILED.
Skip this step only if list-iflow-examples returns empty or the error is clearly a simple
value typo (e.g. URL path suffix) that requires no structural adapter changes.

STEP 4: Call update-iflow with id, files, autoDeploy=true.
  - If response contains "artifact is locked": call cancel-checkout → retry ONCE.
    If still locked → STOP, return failed_stage="locked".
  - If any other failure → STOP, return failed_stage="update".

=== CRITICAL — FILEPATH FOR update-iflow ===
CRITICAL: When calling update-iflow, the filepath MUST be a RELATIVE path starting with "src/".

The get-iflow response returns ABSOLUTE container paths like:
  /home/vcap/app/temp/<uuid>/src/main/resources/scenarioflows/integrationflow/<id>.iflw

You MUST strip everything before "src/" and use only the relative portion:
  CORRECT:  src/main/resources/scenarioflows/integrationflow/<id>.iflw
  WRONG:    /home/vcap/app/temp/<uuid>/src/main/resources/scenarioflows/integrationflow/<id>.iflw

Reason: the MCP server calls path.join(newTempFolder, filepath). When filepath is absolute,
Node.js path.join ignores the base entirely — the patch is written to the deleted temp
folder and silently lost. Always use the relative path starting from "src/".

STEP 5: Call deploy-iflow with the iFlow ID.
  - If FAILS: call get-deploy-error to retrieve diagnostic details.
  - Return failed_stage="deploy" with the error details.

STEP 6: Call record_fix_outcome with iflow_id, error_type, root_cause, fix_applied summary,
  and outcome="SUCCESS" or "FAILED".

=== CONTENT-BASED ROUTER (CBR) — DO NOT TOUCH ===
Unless the proposed_fix EXPLICITLY mentions routing, ExclusiveGateway, or CBR:
- NEVER add, remove, or reorder <bpmn2:ExclusiveGateway> elements.
- NEVER remove or modify any <bpmn2:SequenceFlow> that has no conditionExpression
  (that is the default route — SAP CPI requires exactly one per gateway).
- NEVER modify <bpmn2:conditionExpression> elements.
- NEVER reorder or restructure <bpmn2:SequenceFlow> elements around a gateway.
If you are unsure whether a SequenceFlow is the default route, leave it untouched.
Removing the default route causes validate_iflow_xml to return FATAL and blocks deploy.

=== GROOVY SCRIPT RULES ===
- Physical file path in iFlow archive: src/main/resources/script/<Name>.groovy
- Model reference inside iFlow XML: /script/<Name>.groovy
- Never use /src/main/resources, scripts/<Name>.groovy, or absolute paths.
- Verify both the file path and the model reference before calling update-iflow.

=== URL / ADDRESS FIX RULES ===
When fixing an HTTP or OData receiver Address or URL property:

STEP A — Read the CURRENT value first from get-iflow output
STEP B — Identify what is wrong (typo, missing .svc, wrong path)
STEP C — Write the corrected value using ONLY ONE of these formats:

  Valid formats:
  a) Complete hardcoded URL:
     https://api.example.com/v1/${header.param}
  b) Single header expression:
     ${header.baseurl}/${header.param}
  c) Simple hardcoded URL:
     https://api.example.com/v1/resource

  INVALID formats (NEVER produce these):
  - ${header.X}-https://...    ← dash between expression and URL
  - https://...${header.X}...  ← unless it's a proper template
  - Any URL with two separate expressions joined by dash

STEP D — Validate the Address value:
  - Must start with 'https://' OR start with '${'
  - Must NOT contain '-https://' or '-http://'
  - Must NOT concatenate two separate URL bases

STEP E — If unsure about the correct URL value:
  - Call get_message_logs with the MessageGuid to read
    the actual request URL from message processing logs
  - Use ONLY confirmed values from logs, not guesses

=== CRITICAL — DO NOT HALLUCINATE TOOL RESULTS ===
- Every tool call MUST be a real invocation using an available tool.
- The pipeline verifies tool_calls in the message history.
- If no tool_calls are found in the message history → the fix is treated as FAILED.
- Never invent tool responses or claim a step succeeded without calling the tool.

=== FINAL OUTPUT (MANDATORY) ===
After completing all steps, return EXACTLY this JSON — no markdown, no extra text:
{"fix_applied": true/false, "deploy_success": true/false, "update_response": "<short summary>",
 "deploy_response": "<short summary>", "summary": "<2 sentences: what changed and deploy outcome>",
 "failed_stage": null}
If any step failed, set failed_stage to: "get" | "update" | "locked" | "deploy" | "validation"
""".replace("__WEB_SEARCH_STEP__", _web_search_step)
        self._agent = await self._mcp.build_agent(
            tools=all_tools,
            system_prompt=system_prompt,
            deployment_id=os.getenv("LLM_DEPLOYMENT_ID_FIX") or None,
        )
        logger.info(
            "[FixGenerator] Agent ready — 2 local @tools + %d MCP tools.", len(mcp_tools)
        )

    # ── public entry point ────────────────────────────────────────────────────

    async def generate(
        self,
        ctx: FixContext,
        strategy: FixStrategy,
        progress_fn: Optional[Callable] = None,
    ) -> PatchSpec:
        """
        For 'structured': return a PatchSpec wrapping the pre-computed operations.
        For 'free_xml':   run the LLM agent pipeline and return the full PatchSpec
                          containing the tool-call steps.
        """
        if strategy.strategy == "direct_patch":
            merged_xml = strategy.operations[0].get("merged_xml", "") if strategy.operations else ""
            if merged_xml:
                logger.info(
                    "[FixGenerator] Using direct_patch strategy for iflow=%s — no LLM call needed",
                    ctx.iflow_id,
                )
                return PatchSpec(
                    mode="structured",
                    operations=[{
                        "change_type":    "component_replace",
                        "merged_xml":     merged_xml,
                        "reference_name": "direct_patch",
                    }],
                    raw_xml=merged_xml,
                    raw_answer=(
                        f"Direct patch: {ctx.property_to_change} "
                        f"changed from {ctx.current_value!r} "
                        f"to {ctx.correct_value!r}"
                    ),
                    steps=[],
                )
            logger.warning(
                "[FixGenerator] direct_patch has no merged_xml for iflow=%s — "
                "falling back to free_xml",
                ctx.iflow_id,
            )

        if strategy.strategy == "component_replace":
            merged_xml = strategy.operations[0].get("merged_xml", "") if strategy.operations else ""
            if merged_xml:
                logger.info(
                    "[FixGenerator] Using component_replace strategy for iflow=%s",
                    ctx.iflow_id,
                )
                return PatchSpec(
                    mode="structured",
                    operations=[{
                        "change_type": "component_replace",
                        "merged_xml": merged_xml,
                        "reference_name": strategy.reference_name,
                    }],
                    raw_xml=merged_xml,
                    raw_answer=f"Component replaced with reference '{strategy.reference_name}'",
                    steps=[],
                )
            logger.warning(
                "[FixGenerator] component_replace has no merged_xml for iflow=%s — "
                "falling back to free_xml",
                ctx.iflow_id,
            )

        if strategy.strategy == "structured":
            if not strategy.operations:
                logger.warning(
                    "[FixGenerator] structured strategy has no operations for iflow=%s — "
                    "falling back to free_xml to avoid a no-op deploy",
                    ctx.iflow_id,
                )
            else:
                logger.info(
                    "[FixGenerator] Strategy=structured_patch | iflow=%s | "
                    "affected_component=%s | sap_notes=%s",
                    ctx.iflow_id, ctx.affected_component,
                    "yes" if ctx.sap_notes else "NO — KB context missing",
                )
                return PatchSpec(
                    mode="structured",
                    operations=strategy.operations,
                    raw_xml="",
                    raw_answer="",
                    steps=[],
                )

        return await self._run_free_xml_agent(ctx, progress_fn)

    # ── free-XML LLM agent execution ──────────────────────────────────────────

    async def _run_free_xml_agent(
        self, ctx: FixContext, progress_fn: Optional[Callable]
    ) -> PatchSpec:
        agent = self._agent
        if agent is None:
            logger.error("[FixGenerator] Agent not built — cannot run free-XML path.")
            return PatchSpec(mode="free_xml", operations=[], raw_xml="",
                             raw_answer="__NO_AGENT__", steps=[])

        xml_to_send = ctx.sliced_xml if ctx.sliced_xml else ctx.original_xml
        logger.info(
            "[FIX_GEN] xml_mode=%s xml_len=%d iflow=%s",
            "sliced" if ctx.sliced_xml else "full",
            len(xml_to_send),
            ctx.iflow_id,
        )

        _comp = (ctx.affected_component or "").strip()
        targeted_component_hint = (
            f'            → CONFIRMED failing component: "{_comp}"\n'
            f'              Search for id="{_comp}" in the XML and go directly to this step.\n'
            f"              Limit your change to this component — do not modify anything else."
            if _comp and _comp.lower() != "unknown"
            else "            → No specific component confirmed — infer from error type and iFlow structure."
        )

        _et             = (ctx.error_type or "UNKNOWN").upper()
        _groovy_relevant = {"MAPPING_ERROR", "DATA_VALIDATION", "UNKNOWN_ERROR"}
        _struct_relevant = {"MAPPING_ERROR", "DATA_VALIDATION", "UNKNOWN_ERROR", "AUTH_CONFIG_ERROR"}
        _groovy_rules_ctx = CPI_IFLOW_GROOVY_RULES if _et in _groovy_relevant else ""
        _xml_patterns_ctx = CPI_IFLOW_XML_PATTERNS if _et in _struct_relevant else ""

        # When the XML is already in this prompt, tell the agent to skip the get-iflow
        # call (STEP 1 in the system prompt). That call costs 30-90s and 2 recursion
        # steps before the fix work starts, causing "timed out before update-iflow" errors.
        _skip_note = (
            "IMPORTANT — SKIP STEP 1: The iFlow XML is pre-fetched and provided at the end of "
            "this message. Do NOT call get-iflow. Proceed directly to STEP 2.\n\n"
        ) if (ctx.original_xml or ctx.sliced_xml) else ""

        prompt = _skip_note + FIX_AND_DEPLOY_PROMPT_TEMPLATE.format(
            iflow_id=ctx.iflow_id,
            error_type=ctx.error_type or "UNKNOWN",
            error_message=(ctx.error_message or "")[:3000],
            message_guid=ctx.message_guid or "N/A",
            root_cause=ctx.root_cause or ctx.error_message,
            proposed_fix=ctx.proposed_fix or f"Investigate and fix the error: {ctx.error_message}",
            affected_component=ctx.affected_component or "unknown",
            targeted_component_hint=targeted_component_hint,
            pattern_history=ctx.pattern_history,
            sap_notes=ctx.sap_notes,
            error_type_guidance=ctx.error_type_guidance,
            groovy_rules=_groovy_rules_ctx,
            iflow_xml_patterns=_xml_patterns_ctx,
        )
        if ctx.cross_pattern_text:
            prompt += f"\n\n{ctx.cross_pattern_text}"

        if ctx.last_deploy_error:
            prompt += (
                "\n\nPREVIOUS ATTEMPT DEPLOY ERROR: "
                f"{ctx.last_deploy_error}\n"
                "Your fix must address this specific deploy error."
            )

        # Inject static component map from XMLAnalyst (no LLM, no network)
        try:
            from agents.fix_xml_analyst import XMLAnalyst  # noqa: PLC0415
            _xml_summary = XMLAnalyst().analyse(ctx.original_xml, ctx.affected_component)
            if _xml_summary:
                prompt += f"\n\n{_xml_summary}"
        except Exception as _xa_exc:
            logger.debug("[FixGenerator] XMLAnalyst failed (non-fatal): %s", _xa_exc)

        if ctx.sliced_xml:
            prompt += (
                "\n\n=== XML CONTEXT (focused view — centred on failing component) ===\n"
                "The section below shows the failing component and its 2 nearest siblings.\n"
                "This is sufficient for most fixes. Call get-iflow ONLY if you need the\n"
                "full iFlow structure (e.g. to trace sequence flows or find a missing router).\n"
                f"{ctx.sliced_xml}"
            )
        elif ctx.original_xml:
            prompt += (
                "\n\n=== FULL IFLOW XML (pre-fetched) ===\n"
                "The complete iFlow XML is provided below. Focus on the affected component.\n"
                f"{ctx.original_xml}"
            )

        messages  = [{"role": "user", "content": prompt}]
        tracker   = TestExecutionTracker(ctx.user_id, f"fix:{ctx.iflow_id}", ctx.timestamp)
        logger_cb = StepLogger(tracker, progress_fn=progress_fn)

        _xml_len   = len(ctx.original_xml or "")
        # Lower threshold: 20 KB already takes 300+ s on SAP AI Core for complex fixes.
        _default   = "300.0" if _xml_len < 20_000 else "600.0"
        _timeout   = float(os.getenv("FIX_AGENT_TIMEOUT", _default))
        logger.info(
            "[FixGenerator] Strategy=free_xml | timeout=%.0fs | xml_len=%d | "
            "sap_notes=%s | iflow=%s",
            _timeout, _xml_len,
            "yes" if ctx.sap_notes else "NO — KB context missing",
            ctx.iflow_id,
        )
        try:
            _result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": messages},
                    config={"callbacks": [logger_cb], "recursion_limit": 30},
                ),
                timeout=_timeout,
            )
        except asyncio.TimeoutError:
            _corr = ctx.message_guid or ctx.iflow_id
            fatal = _find_fatal_validation(logger_cb.steps)
            if fatal:
                logger.warning(
                    "[FixGenerator] FATAL validation block (timed out) for iflow=%s: %s",
                    ctx.iflow_id, fatal,
                )
                log_agent_event(_corr, "fix_generator", 0, 0)
                return PatchSpec(
                    mode="free_xml",
                    operations=[],
                    raw_xml="",
                    raw_answer="__VALIDATION_BLOCKED__",
                    steps=logger_cb.steps,
                )
            log_agent_event(_corr, "fix_generator", 0, 0)
            return PatchSpec(
                mode="free_xml",
                operations=[],
                raw_xml="",
                raw_answer="__TIMEOUT__",
                steps=logger_cb.steps,
            )
        except Exception as exc:
            _corr = ctx.message_guid or ctx.iflow_id
            exc_type = type(exc).__name__
            # GraphRecursionError means the agent ran out of steps, not that it crashed.
            # Treat it like a timeout so the supervisor can retry with a different strategy.
            _is_recursion = (
                "GraphRecursionError" in exc_type
                or "recursion limit" in str(exc).lower()
            )
            if _is_recursion:
                fatal = _find_fatal_validation(logger_cb.steps)
                if fatal:
                    logger.warning(
                        "[FixGenerator] FATAL validation block (recursion limit) for iflow=%s: %s",
                        ctx.iflow_id, fatal,
                    )
                    log_agent_event(_corr, "fix_generator", 0, 0)
                    return PatchSpec(
                        mode="free_xml",
                        operations=[],
                        raw_xml="",
                        raw_answer="__VALIDATION_BLOCKED__",
                        steps=logger_cb.steps,
                    )
                logger.warning(
                    "[FixGenerator] Recursion limit reached for iflow=%s — will retry with simpler strategy.",
                    ctx.iflow_id,
                )
                log_agent_event(_corr, "fix_generator", 0, 0)
                return PatchSpec(
                    mode="free_xml",
                    operations=[],
                    raw_xml="",
                    raw_answer="__RECURSION_LIMIT__",
                    steps=logger_cb.steps,
                )
            logger.error("[FixGenerator] agent error: %s", exc)
            log_agent_event(_corr, "fix_generator", 0, 0)
            return PatchSpec(
                mode="free_xml",
                operations=[],
                raw_xml="",
                raw_answer=f"__ERROR__:{exc}",
                steps=logger_cb.steps,
            )

        _ti, _to = extract_token_counts(_result.get("messages", []))
        log_agent_event(ctx.message_guid or ctx.iflow_id, "fix_generator", _ti, _to)

        final_msg = _result["messages"][-1]
        answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)

        # Check for structural validation block before any further processing
        fatal = _find_fatal_validation(logger_cb.steps)
        if fatal:
            logger.warning(
                "[FixGenerator] FATAL validation block detected for iflow=%s: %s",
                ctx.iflow_id, fatal,
            )
            return PatchSpec(
                mode="free_xml",
                operations=[],
                raw_xml="",
                raw_answer="__VALIDATION_BLOCKED__",
                steps=logger_cb.steps,
            )

        # Verify tool invocations
        invoked_tools: List[str] = []
        for msg in _result["messages"]:
            for tc in (getattr(msg, "tool_calls", None) or []):
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name and name not in invoked_tools:
                    invoked_tools.append(name)

        if not invoked_tools:
            logger.warning(
                "[FixGenerator] NO tool calls made — LLM described fix without executing. "
                "Treating as FAILED."
            )
            return PatchSpec(
                mode="free_xml",
                operations=[],
                raw_xml="",
                raw_answer="__NO_TOOL_CALLS__",
                steps=logger_cb.steps,
            )

        answer += f"\n\n__TOOLS_INVOKED__={','.join(invoked_tools)}"
        logger.info("[FixGenerator] Tools invoked: %s", invoked_tools)

        # Validate filepath used in update-iflow against what get-iflow returned.
        # Normalize both sides because get-iflow returns absolute CF paths while the
        # LLM should now use relative paths (src/...) per the updated system prompt.
        _used_fp      = _extract_update_filepath(_result["messages"])
        _expected_fps = _extract_getiflow_filepaths(_result["messages"])
        if _used_fp and _expected_fps:
            from core.validators import _normalize_iflow_filepath  # noqa: PLC0415
            _used_fp_norm      = _normalize_iflow_filepath(_used_fp)
            _expected_fps_norm = [_normalize_iflow_filepath(fp) for fp in _expected_fps]
            if _used_fp_norm not in _expected_fps_norm:
                logger.warning(
                    "[FixGenerator] WRONG FILEPATH detected: used=%r (norm=%r) expected=%s",
                    _used_fp, _used_fp_norm, _expected_fps_norm,
                )

        # Extract patched XML from update-iflow tool call arguments
        raw_xml = _extract_patched_xml(_result["messages"])

        return PatchSpec(
            mode="free_xml",
            operations=[],
            raw_xml=raw_xml,
            raw_answer=answer,
            steps=logger_cb.steps,
        )


# ── XML / filepath extraction helpers ────────────────────────────────────────

def _extract_patched_xml(messages: List[Any]) -> str:
    """
    Walk the message history and extract the XML content passed to update-iflow
    (files[0]["content"]).  Returns "" if not found.
    """
    for msg in messages:
        for tc in (getattr(msg, "tool_calls", None) or []):
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name and "update" in name.lower() and "iflow" in name.lower():
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                if isinstance(args, dict):
                    files = args.get("files", [])
                    if files and isinstance(files, list):
                        content = files[0].get("content", "") if isinstance(files[0], dict) else ""
                        if content:
                            return content
    return ""


def _extract_update_filepath(messages: List[Any]) -> str:
    """Return the filepath the LLM passed to update-iflow, or '' if not found."""
    for msg in messages:
        for tc in (getattr(msg, "tool_calls", None) or []):
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name and "update" in name.lower() and "iflow" in name.lower():
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                if isinstance(args, dict):
                    files = args.get("files", [])
                    if files and isinstance(files, list) and isinstance(files[0], dict):
                        return files[0].get("filepath", "")
    return ""


def _extract_getiflow_filepaths(messages: List[Any]) -> List[str]:
    """
    Scan tool-response messages for filepath values returned by get-iflow.
    Handles both JSON {"filepath":"..."} format and the ---begin-of-file---
    format where the filepath appears on the line immediately before the marker.
    """
    filepaths: List[str] = []
    for msg in messages:
        content = getattr(msg, "content", None)
        if not content or not isinstance(content, str):
            continue
        # JSON "filepath": "..." format (legacy)
        for match in re.finditer(r'"filepath"\s*:\s*"([^"]+)"', content):
            fp = match.group(1)
            if fp and fp not in filepaths:
                filepaths.append(fp)
        # ---begin-of-file--- format: filepath is the line immediately before the marker
        for match in re.finditer(r'([^\r\n]+)\r?\n---begin-of-file---', content):
            fp = match.group(1).strip().replace("\\", "/")
            if fp and fp not in filepaths:
                filepaths.append(fp)
    return filepaths
