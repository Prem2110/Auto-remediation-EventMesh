"""
agents/base.py
==============
Shared utilities re-used by every agent module:

  create_llm()            — LLM factory (re-exported from core/mcp_manager)
  formatjson()            — safely parse JSON / ast literal from a string
  TestExecutionTracker    — tracks test-execution payloads, message IDs, and logs
  StepLogger              — LangChain BaseCallbackHandler for tool call/result logging
  _FIX_TOOL_PROGRESS_LABELS — maps short tool names → human-readable progress strings
  Pydantic models:
    QueryRequest, QueryResponse, ApprovalRequest, DirectFixRequest

Nothing here owns any MCP connection or agent state.
"""

import ast
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel

from db.database import (
    addTestSuiteLog,
    update_test_suite_executions,
    updateTestSuiteStatus,
)

# Re-export so agents don't need to reach into core.mcp_manager directly
from core.mcp_manager import create_llm  # noqa: F401

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# PYDANTIC MODELS  (used by main.py endpoints)
# ─────────────────────────────────────────────

class QueryRequest(BaseModel):
    query:   str
    id:      Optional[str] = None
    user_id: str


class QueryResponse(BaseModel):
    response: str
    id:       str
    error:    Optional[Any] = None


class ApprovalRequest(BaseModel):
    approved: bool
    comment:  Optional[str] = None


class DirectFixRequest(BaseModel):
    iflow_id:      str
    error_message: str
    proposed_fix:  Optional[str] = None
    user_id:       str


# ─────────────────────────────────────────────
# JSON HELPER
# ─────────────────────────────────────────────

def formatjson(input: Any) -> Any:
    """Best-effort convert a raw string or value into a Python dict/list."""
    try:
        if isinstance(input, str):
            try:
                return json.loads(input)
            except json.JSONDecodeError:
                return ast.literal_eval(input)
        return input
    except Exception:
        return {}


# ─────────────────────────────────────────────
# TEST EXECUTION TRACKER
# ─────────────────────────────────────────────

class TestExecutionTracker:
    """
    Correlates test-iflow tool calls with message IDs and log entries so that
    full execution records can be persisted to the test-suite DB table.
    """

    def __init__(self, user_id: str, prompt: str, timestamp: str):
        self.user_id       = user_id
        self.prompt        = prompt
        self.timestamp     = timestamp
        self.executions:   List[Dict[str, Any]] = []
        self.tool_map:     Dict[str, int]        = {}   # tool_call_id → executions index
        self.test_suite_id: Optional[str]        = None
        self.test_started  = False

    def handle_test_start(self, tool_call_id: str, args: Dict[str, Any]):
        try:
            if not self.test_started:
                self.test_suite_id = str(uuid.uuid4())
                self.test_started  = True
                addTestSuiteLog({
                    "test_suite_id": self.test_suite_id,
                    "user":          self.user_id,
                    "prompt":        self.prompt,
                    "timestamp":     self.timestamp,
                    "status":        "IN_PROGRESS",
                    "executions":    [],
                })
            payload   = formatjson(args.get("payload"))
            header    = formatjson(args.get("header"))
            execution = {
                "payload":      payload,
                "headers":      header,
                "http_method":  args.get("http_method"),
                "message_id":   None,
                "message_logs": None,
            }
            self.executions.append(execution)
            self.tool_map[tool_call_id] = len(self.executions) - 1
            update_test_suite_executions(self.test_suite_id, self.executions)
        except Exception as exc:
            logger.debug("[TestTracker] handle_test_start skipped: %s", exc)

    def handle_test_response(self, tool_call_id: str, response_json: Dict[str, Any]):
        try:
            body = response_json.get("response", {}).get("body", "")
            if not isinstance(body, str):
                return
            body_stripped = body.strip()
            if body_stripped.startswith("{") and body_stripped.endswith("}"):
                return
            match = re.search(r"MPL ID for the failed message is\s*:\s*([^\r\n]+)", body)
            if not match:
                return
            message_id = match.group(1)
            index = self.tool_map.get(tool_call_id)
            if index is None:
                return
            self.executions[index]["message_id"] = message_id
            update_test_suite_executions(self.test_suite_id, self.executions)
        except Exception as exc:
            logger.debug("[TestTracker] handle_test_response skipped: %s", exc)

    def handle_log_response(self, message_id: str, logs: Any):
        try:
            for execution in self.executions:
                if execution.get("message_id") in message_id:
                    execution["message_logs"] = logs
                    break
            update_test_suite_executions(self.test_suite_id, self.executions)
        except Exception as exc:
            logger.debug("[TestTracker] handle_log_response skipped: %s", exc)


# ─────────────────────────────────────────────
# STEP LOGGER (LangChain callback)
# ─────────────────────────────────────────────

_FIX_TOOL_PROGRESS_LABELS: Dict[str, str] = {
    "get_iflow":             "Agent: reading current iFlow XML…",
    "update_iflow":          "Agent: uploading fixed iFlow to SAP CPI…",
    "deploy_iflow":          "Agent: deploying iFlow to runtime…",
    "get_deploy_error":      "Agent: checking deployment errors…",
    "list_iflow_examples":   "Agent: searching reference examples…",
    "get_iflow_example":     "Agent: loading reference example…",
    "unlock_iflow":          "Agent: unlocking iFlow for editing…",
    "cancel_checkout":       "Agent: cancelling existing checkout…",
    "force_unlock":          "Agent: force-unlocking iFlow…",
}


class StepLogger(BaseCallbackHandler):
    """
    Intercepts on_tool_start / on_tool_end callbacks from a LangChain agent run
    and builds a flat list of step dicts for result logging and test tracking.

    Optionally calls progress_fn(label) so callers can push live-progress
    updates to a WebSocket or SSE stream.
    """

    def __init__(
        self,
        tracker: TestExecutionTracker,
        progress_fn=None,
    ):
        self.steps:            List[Dict[str, Any]]  = []
        self.tracker           = tracker
        self._tool_names:      Dict[str, str]         = {}   # run_id → tool_name
        self._progress_fn      = progress_fn  # Optional[Callable[[str], None]]

    def on_tool_start(self, serialized, input_str, run_id=None, **kw):
        tool_name    = serialized.get("name", "unknown")
        tool_call_id = str(run_id)
        self._tool_names[tool_call_id] = tool_name
        self.steps.append({"tool": tool_name, "input": input_str, "output": None})
        logger.info("[TOOL_CALL] tool=%s | input=%.800s", tool_name, str(input_str))

        if self._progress_fn:
            short = tool_name.split("__")[-1] if "__" in tool_name else tool_name
            label = _FIX_TOOL_PROGRESS_LABELS.get(short)
            if label:
                try:
                    self._progress_fn(label)
                except Exception:
                    pass

        try:
            args = json.loads(input_str) if isinstance(input_str, str) else input_str
        except Exception:
            try:
                args = ast.literal_eval(input_str)
            except Exception:
                args = {}
        if "test_iflow_with_payload" in tool_name:
            self.tracker.handle_test_start(tool_call_id, args)

    def on_tool_end(self, output, run_id=None, **kw):
        tool_call_id = str(run_id)
        tool_name    = self._tool_names.get(tool_call_id, "unknown")
        raw_content  = output.content if hasattr(output, "content") else output

        try:
            response_json = (
                json.loads(raw_content) if isinstance(raw_content, str) else raw_content
            )
        except Exception:
            try:
                response_json = ast.literal_eval(raw_content)
            except Exception:
                response_json = {}

        if "test_iflow_with_payload" in tool_name:
            self.tracker.handle_test_response(tool_call_id, response_json)
        elif "get_message_logs" in tool_name:
            message_id = response_json.get("message_id")
            logs       = response_json.get("logs")
            self.tracker.handle_log_response(message_id, logs)

        # Fill in the output for the matching pending step
        for step in reversed(self.steps):
            if step["tool"] == tool_name and step["output"] is None:
                step["output"] = str(output)
                break

        logger.info("[TOOL_RESULT] tool=%s | output=%.2000s", tool_name, str(output))
