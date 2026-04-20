"""
agents/verifier_agent.py
========================
VerifierAgent — post-fix verification: retry failed messages and run iFlow tests.

Uses only mcp_testing server tools so it can never accidentally call any
integration_suite write tools (update-iflow / deploy-iflow).

Exports:
  VerifierAgent
    .retry_failed_message(incident)    → {"success", "skipped", "summary", "steps"}
    .test_iflow_after_fix(incident)    → {"success", "skipped", "http_status", "summary", "steps"}
    .get_retry_tool_candidates()       → List[str]
"""

import asyncio
import json
import logging
from typing import Any, Dict, List

from agents.base import StepLogger, TestExecutionTracker
from utils.utils import get_hana_timestamp

logger = logging.getLogger(__name__)


class VerifierAgent:
    """
    Verifies that a deployed fix works by replaying the original failed message
    and/or running a test payload through the iFlow HTTP endpoint.

    Tools are restricted to mcp_testing server + any retry/replay tools from
    integration_suite (read-only).
    """

    def __init__(self, mcp):
        self._mcp          = mcp
        self._agent        = None  # set by build_agent()
        self.error_fetcher = None  # injected via set_error_fetcher()

    def set_error_fetcher(self, error_fetcher) -> None:
        """Inject the SAPErrorFetcher so check_iflow_runtime_status can poll CPI."""
        self.error_fetcher = error_fetcher

    async def build_agent(self) -> None:
        """Build a verifier agent with check_iflow_runtime_status @tool + mcp_testing tools."""
        from langchain_core.tools import tool as _tool  # noqa: PLC0415

        _fetcher = self.error_fetcher  # may be None if not injected

        @_tool
        async def check_iflow_runtime_status(iflow_id: str) -> str:
            """
            Poll SAP CPI runtime to check if an iFlow is in Started/Error state.
            Returns the current DeployState/Status of the runtime artifact.
            """
            if _fetcher is None:
                return f"Runtime fetcher not available — cannot poll status for '{iflow_id}'."
            detail = await _fetcher.fetch_runtime_artifact_detail(iflow_id)
            status = (
                detail.get("Status")
                or detail.get("DeployState")
                or detail.get("RuntimeStatus")
                or "UNKNOWN"
            )
            error_info = (
                detail.get("ErrorInformation")
                or detail.get("Description")
                or ""
            )
            if error_info:
                return f"Status={status} | ErrorInformation={str(error_info)[:300]}"
            return f"Status={status}"

        # mcp_testing tools + any retry/replay tools
        verify_tools = [
            t for t in self._mcp.tools
            if t.server == "mcp_testing"
            or any(kw in f"{t.name} {t.mcp_tool_name} {t.description}".lower()
                   for kw in ("retry", "replay", "resubmit", "test_iflow"))
        ]
        if not verify_tools:
            verify_tools = [t for t in self._mcp.tools if t.server == "mcp_testing"]

        # Add get-iflow (read-only) so the verifier can inspect the iFlow's payload schema
        # before constructing a test payload — this prevents schema-blind guessing.
        get_iflow_tool = next(
            (t for t in self._mcp.tools if t.mcp_tool_name == "get-iflow"),
            None,
        )
        if get_iflow_tool:
            verify_tools = [get_iflow_tool] + verify_tools

        all_tools = [check_iflow_runtime_status] + verify_tools

        system_prompt = """You are an SAP CPI post-fix verification agent.

Your job is to verify that a deployed fix works. Execute in order:
1. Call check_iflow_runtime_status to confirm the iFlow is in Started (not Error) state.
   - If status is ERROR or STOPPED: return test_passed=false immediately.
2. Call get_iflow_endpoint to discover whether the iFlow has an HTTP trigger.
   - If NO HTTP endpoint (count=0, SFTP, File, Scheduler):
     Runtime status check from step 1 IS the verification.
     Return: {"test_passed": true, "http_status": null, "summary": "Non-HTTP iFlow confirmed in Started state post-fix."}
   - If YES HTTP endpoint: proceed to step 3.
3. Call get-iflow to read the iFlow configuration. From the iFlow XML:
   - Identify the sender channel type (REST/SOAP/HTTP).
   - Find any message mapping or schema step to infer required field names and payload structure.
   - Use ONLY confirmed field names from the iFlow — never guess field names.
4. Call test_iflow_with_payload ONCE using the payload you constructed from the iFlow schema.
5. Return EXACTLY this JSON (no markdown):
   {"test_passed": true/false, "http_status": <code or null>, "summary": "<one sentence: payload structure used and what the iFlow returned>"}

Do NOT call update-iflow or deploy-iflow.
Do NOT modify any iFlow.
Maximum 5 tool calls total.
"""
        self._agent = await self._mcp.build_agent(
            tools=all_tools,
            system_prompt=system_prompt,
        )
        logger.info(
            "[Verifier] Agent ready — 1 local @tool + %d mcp_testing tools.", len(verify_tools)
        )

    # ── retry candidates ────────────────────────────────────────────────────

    def get_retry_tool_candidates(self) -> List[str]:
        return [
            t.name for t in self._mcp.tools
            if any(
                tok in f"{t.name} {t.description}".lower()
                for tok in ("retry", "replay", "resubmit")
            )
        ]

    # ── retry failed message ────────────────────────────────────────────────

    async def retry_failed_message(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        message_guid = incident.get("message_guid", "")
        if not message_guid:
            return {"success": False, "skipped": True, "summary": "No message GUID available for retry."}

        retry_tools = self.get_retry_tool_candidates()
        if not retry_tools:
            return {
                "success": False,
                "skipped": True,
                "summary": "No retry or replay MCP tool is currently available.",
            }

        agent = self._agent or self._mcp.agent
        if agent is None:
            return {"success": False, "skipped": True, "summary": "MCP agent not ready."}

        prompt = f"""RETRY FAILED MESSAGE — use exactly one retry or replay tool call, then stop.
Message GUID: {message_guid}
Candidate tools: {", ".join(retry_tools)}
Rules:
- Retry or replay only this failed message.
- Do not fetch logs.
- Do not modify the iFlow.
- Return a one-sentence plain-text result.
"""
        timestamp = get_hana_timestamp()
        tracker   = TestExecutionTracker("system_retry", prompt, timestamp)
        logger_cb = StepLogger(tracker)
        try:
            result    = await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"callbacks": [logger_cb], "recursion_limit": 4},
            )
            final_msg = result["messages"][-1]
            answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            return {"success": True, "skipped": False, "summary": answer, "steps": logger_cb.steps}
        except Exception as exc:
            logger.error("[RETRY] retry_failed_message error: %s", exc)
            return {"success": False, "skipped": False, "summary": str(exc), "steps": logger_cb.steps}

    # ── test iFlow after fix ─────────────────────────────────────────────────

    async def test_iflow_after_fix(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        has_test_tool = any("test_iflow_with_payload" in t.name for t in self._mcp.tools)
        if not has_test_tool:
            return {
                "success": False,
                "skipped": True,
                "summary": "test_iflow_with_payload tool not available.",
            }

        agent = self._agent or self._mcp.agent
        if agent is None:
            return {"success": False, "skipped": True, "summary": "MCP agent not ready."}

        iflow_id     = incident.get("iflow_id", "")
        error_type   = incident.get("error_type", "")
        error_msg    = incident.get("error_message", "")
        proposed_fix = incident.get("proposed_fix", "")

        prompt = f"""IFLOW VERIFICATION — the fix has been deployed. Confirm it works.

iFlow ID:     {iflow_id}
Error type:   {error_type}
Original error (truncated): {error_msg[:400]}
Applied fix:  {proposed_fix[:400]}

INSTRUCTIONS — execute in order, stop early if a step gives a definitive result:
1. Call check_iflow_runtime_status for iflow_id='{iflow_id}'.
   - If status is ERROR or STOPPED: return immediately:
     {{"test_passed": false, "http_status": null, "summary": "iFlow is in <status> state after deploy — fix did not recover it."}}
2. Call get_iflow_endpoint for iflow_id='{iflow_id}' to discover the HTTP trigger.
   - If no HTTP endpoint (count=0 / SFTP / File / Scheduler-triggered):
     Runtime status = Started is sufficient confirmation. Return:
     {{"test_passed": true, "http_status": null, "summary": "Non-HTTP iFlow confirmed in Started state post-fix — no payload test required."}}
     Do NOT call test_iflow_with_payload.
3. If an HTTP endpoint IS found:
   a. Call get-iflow with iflow_id='{iflow_id}' to read the iFlow configuration.
      - Identify the sender channel type (REST / SOAP / HTTPS).
      - Look at message mapping steps or Content Modifier steps to find field names used in the payload.
      - Use ONLY field names you observed in the iFlow XML — do NOT guess or invent field names.
   b. Construct a minimal test payload using the field names and structure you observed.
   c. Call test_iflow_with_payload ONCE with iflow_id='{iflow_id}' and the schema-based payload.
4. Return EXACTLY this JSON (no markdown):
{{"test_passed": true/false, "http_status": <status code or null>, "summary": "<what payload structure was sent and what the iFlow returned>"}}

Do NOT call update-iflow. Do NOT call deploy-iflow. Do NOT modify the iFlow.
Maximum 5 tool calls total.
"""
        timestamp = get_hana_timestamp()
        tracker   = TestExecutionTracker(
            incident.get("user_id", "system"),
            f"test_after_fix:{iflow_id}",
            timestamp,
        )
        logger_cb = StepLogger(tracker)
        try:
            result    = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"callbacks": [logger_cb], "recursion_limit": 10},
                ),
                timeout=120.0,
            )
            final_msg = result["messages"][-1]
            answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            try:
                parsed = json.loads(answer.strip())
            except Exception:
                parsed = {}
            test_passed = parsed.get("test_passed", False)
            logger.info(
                "[TEST_AFTER_FIX] iflow=%s test_passed=%s status=%s summary=%s",
                iflow_id, test_passed, parsed.get("http_status"), parsed.get("summary", "")[:200],
            )
            return {
                "success":     test_passed,
                "skipped":     False,
                "http_status": parsed.get("http_status"),
                "summary":     parsed.get("summary", answer[:200]),
                "steps":       logger_cb.steps,
            }
        except asyncio.TimeoutError:
            logger.warning("[TEST_AFTER_FIX] timed out for iflow=%s", iflow_id)
            return {
                "success": False, "skipped": True,
                "summary": "iFlow test timed out after 120s.", "steps": logger_cb.steps,
            }
        except Exception as exc:
            logger.error("[TEST_AFTER_FIX] error for iflow=%s: %s", iflow_id, exc)
            return {"success": False, "skipped": False, "summary": str(exc), "steps": logger_cb.steps}
