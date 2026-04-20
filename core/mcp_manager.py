"""
core/mcp_manager.py
===================
MCP infrastructure: MCPTool wrapper, JSON→Pydantic schema builder, and
MultiMCP manager (connect, discover, execute, build_agent).

All business logic (RCA, fix, deploy, classification, observer loop) lives in
the agent modules under agents/. This module provides only the transport layer
and the shared infrastructure they depend on.

Exports:
  create_llm()              — LLM factory (also re-exported by agents/base.py)
  build_model()             — JSON Schema → Pydantic model builder
  MCPTool(BaseTool)         — LangChain BaseTool wrapper for a single MCP tool
  MultiMCP                  — Manages clients, tool discovery, execute, build_agent
"""

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Type

import httpx
from pydantic import BaseModel, create_model
from langchain.agents import create_agent
from langchain_core.tools import BaseTool
from gen_ai_hub.proxy.langchain.openai import ChatOpenAI
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from core.constants import (
    MCP_SERVERS,
    TRANSPORT_OPTIONS,
    SERVER_ROUTING_GUIDE,
    MAX_RETRIES,
    MEMORY_LIMIT,
    MEMORY_SESSION_TTL_SECONDS,
    MAX_MEMORY_SESSIONS,
    GROOVY_STRIPE_HTTP_ADAPTER,
    GROOVY_WOOCOMMERCE_HTTP_ADAPTER,
    CPI_IFLOW_GROOVY_RULES,
    SAP_DOC_TEMPLATE,
)
from core.validators import validate_before_update_iflow

import logging
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# LLM FACTORY
# ─────────────────────────────────────────────

def create_llm(deployment_id: Optional[str] = None) -> ChatOpenAI:
    dep = deployment_id or os.getenv("LLM_DEPLOYMENT_ID")
    if not dep:
        raise RuntimeError("LLM_DEPLOYMENT_ID missing in .env")
    return ChatOpenAI(deployment_id=dep, temperature=0)


# ─────────────────────────────────────────────
# JSON SCHEMA → PYDANTIC MODEL
# ─────────────────────────────────────────────

def build_model(name: str, schema: Dict, root=None):
    """Recursively convert a JSON Schema dict into a Pydantic model class."""
    if root is None:
        root = schema
    if "type" not in schema and "schema" in schema:
        schema = schema["schema"]
    if "$ref" in schema:
        ref = schema["$ref"][2:].split("/")
        obj = root
        for r in ref:
            obj = obj.get(r, {})
        return build_model(name, obj, root)
    if "enum" in schema:
        from typing import Literal
        return Literal[tuple(schema["enum"])]
    if schema.get("type") == "object":
        props    = schema.get("properties", {})
        required = schema.get("required", [])
        fields: Dict[str, Any] = {}
        for k, v in props.items():
            t         = build_model(name + "_" + k, v, root)
            default   = ... if k in required else None
            fields[k] = (t, default)
        safe = re.sub(r"\W", "_", name)
        return create_model(safe, **fields)
    if schema.get("type") == "array":
        t = build_model(name + "_item", schema.get("items", {}), root)
        return List[t]
    return {"string": str, "integer": int, "number": float, "boolean": bool}.get(
        schema.get("type"), Any
    )


# ─────────────────────────────────────────────
# MCP TOOL WRAPPER
# ─────────────────────────────────────────────

class MCPTool(BaseTool):
    """LangChain BaseTool that delegates _arun to MultiMCP.execute()."""

    name:          str
    description:   str
    args_schema:   Type[BaseModel]
    server:        str
    mcp_tool_name: str
    manager:       "MultiMCP"

    def _run(self, *a, **kw):
        raise NotImplementedError("Use async _arun via LangChain agent.")

    async def _arun(self, **kwargs):
        return await self.manager.execute(self.server, self.mcp_tool_name, kwargs)


# ─────────────────────────────────────────────
# MULTI MCP MANAGER — infrastructure only
# ─────────────────────────────────────────────

class MultiMCP:
    """
    Manages connections to all three MCP servers, discovers tools, and
    provides execute() / build_agent() for use by all agent modules.

    Business logic (RCA, fix, deploy, classify, observe) is NOT here —
    it lives in the agents/ package.
    """

    def __init__(self):
        self.clients: Dict[str, Client]                  = {}
        self.tools:   List[MCPTool]                      = []
        self._tool_index: Dict[tuple[str, str], MCPTool] = {}
        self.llm      = create_llm()
        self.agent    = None          # populated by build_agent() with no args
        self.memory:  Dict[str, List[Dict]] = {}
        self._memory_last_seen: Dict[str, float] = {}

    # ── safe tool name ───────────────────────

    def _safe_tool_name(self, server: str, tool_name: str) -> str:
        safe = re.sub(r"\W+", "_", f"{server}__{tool_name}").strip("_").lower()
        return safe[:64] if safe else f"{server}_tool"

    # ── connection ───────────────────────────

    async def connect(self):
        """Open StreamableHttpTransport clients for all configured MCP servers."""
        for name, url in MCP_SERVERS.items():
            try:
                opts = TRANSPORT_OPTIONS.get(name, {})
                # Use a default-arg capture to avoid the Python closure-in-loop bug:
                # without _opts=opts, all factories would share the final loop value of opts.
                def factory(_opts=opts, **kw):
                    kw["verify"]  = _opts.get("verify", True)
                    kw["timeout"] = _opts.get("timeout", 30)
                    return httpx.AsyncClient(**kw)
                transport          = StreamableHttpTransport(url, httpx_client_factory=factory)
                self.clients[name] = Client(transport=transport)
                logger.info("[MCP] Connected → %s", name)
            except Exception as e:
                logger.error("[MCP] Failed to connect %s: %s", name, e)

    # ── tool discovery ───────────────────────

    async def discover_tools(self):
        """Call list_tools on every connected server, build MCPTool wrappers."""
        self.tools.clear()
        self._tool_index.clear()
        used_names: set[str] = set()

        async def _load(server: str, client: Client):
            async with client:
                raw = await client.list_tools()
                logger.info("[MCP] Discovering tools from server: %s", server)
                server_tool_names: List[str] = []
                for t in raw:
                    schema          = t.inputSchema or {}
                    Model           = build_model(t.name + "_Input", schema)
                    agent_tool_name = self._safe_tool_name(server, t.name)
                    suffix = 2
                    while agent_tool_name in used_names:
                        agent_tool_name = f"{agent_tool_name}_{suffix}"
                        suffix += 1
                    used_names.add(agent_tool_name)
                    desc_prefix = f"[server={server}] {SERVER_ROUTING_GUIDE.get(server, '')}".strip()
                    full_desc   = f"{desc_prefix} Original tool: {t.name}. {t.description or ''}".strip()
                    self.tools.append(MCPTool(
                        name=agent_tool_name,
                        description=full_desc,
                        args_schema=Model,
                        server=server,
                        mcp_tool_name=t.name,
                        manager=self,
                    ))
                    self._tool_index[(server, t.name)] = self.tools[-1]
                    server_tool_names.append(t.name)
                preview = ", ".join(server_tool_names[:5])
                if len(server_tool_names) > 5:
                    preview += "…"
                logger.info("[MCP] Loaded %d tools from %s: %s", len(server_tool_names), server, preview)

        await asyncio.gather(*(_load(n, c) for n, c in self.clients.items()))

        tools_by_server: Dict[str, List[str]] = {}
        for tool in self.tools:
            tools_by_server.setdefault(tool.server, []).append(tool.mcp_tool_name)

        logger.info("=" * 72)
        logger.info("[MCP] TOOL DISCOVERY COMPLETE — %d tools total", len(self.tools))
        for srv, names in tools_by_server.items():
            logger.info("[MCP]   %s: %d tools", srv, len(names))
            for n in names:
                logger.info("[MCP]     ✓ %s", n)
        logger.info("=" * 72)

    # ── execute ──────────────────────────────

    async def execute(self, server: str, tool: str, args: Dict) -> str:
        """
        Call a single MCP tool. Runs the iFlow XML validator before any
        update-iflow call. Retries up to MAX_RETRIES on transport errors.
        """
        if tool == "update-iflow":
            val_errors = validate_before_update_iflow(args)
            if val_errors:
                error_block = "\n".join(f"  • {e}" for e in val_errors)
                logger.warning("[VALIDATOR] update-iflow blocked. Issues:\n%s", error_block)
                return (
                    "VALIDATION FAILED — update-iflow was NOT sent to SAP CPI.\n"
                    "Fix the following issues and call update-iflow again with corrected values:\n"
                    + error_block
                )

        client = self.clients[server]
        for attempt in range(MAX_RETRIES):
            try:
                async with client:
                    res = await client.call_tool(tool, args)
                parts: List[str] = []
                for c in res.content:
                    if getattr(c, "text", None):
                        parts.append(c.text)
                    elif getattr(c, "json", None):
                        parts.append(json.dumps(c.json, indent=2))
                    else:
                        parts.append(str(c))
                return "\n".join(parts)
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    return f"ERROR: {e}"
                await asyncio.sleep(1)

    # ── build_agent ──────────────────────────

    async def build_agent(
        self,
        tools: Optional[List[MCPTool]] = None,
        system_prompt: Optional[str] = None,
        deployment_id: Optional[str] = None,
    ):
        """
        Create a LangChain agent.

        - tools=None  → use all discovered tools; result stored as self.agent
        - tools=[...]  → use only the supplied subset; result returned but not stored
        - system_prompt=None → use standard routing prompt
        """
        agent_tools = tools if tools is not None else self.tools
        if not agent_tools:
            raise RuntimeError("No MCP tools available — call discover_tools() first.")

        if system_prompt is None:
            routing_text = "\n".join(f"- {n}: {g}" for n, g in SERVER_ROUTING_GUIDE.items())
            system_prompt = f"""You are an SAP MCP automation agent.
Select tools strictly by server responsibility.

Server routing rules:
{routing_text}

Routing rules:
- SAP-standard documentation → documentation_mcp
- iFlow creation/configuration/fix/deploy → integration_suite
- Testing/validation → mcp_testing
- Do not mix servers unless explicitly required.

Must do:
- Use this script: {GROOVY_STRIPE_HTTP_ADAPTER} for Stripe Groovy scripts only.
- Use this script: {GROOVY_WOOCOMMERCE_HTTP_ADAPTER} for WooCommerce Groovy scripts only.
- For other adapters, use the MCP tool for Groovy script creation.

CRITICAL — When asked to FIX and DEPLOY an iFlow:
1. ALWAYS call get-iflow first to get current config.
2. ALWAYS call update-iflow after applying the fix.
3. ALWAYS call deploy-iflow after a successful update.
4. NEVER stop after just the update — deploy is MANDATORY.
5. Report the actual tool response, do not fabricate success.

Execution policy:
- Plan first, then execute tools in order.
- Do not show method: POST in result summary.
- End with a structured summary including fix status and deploy status.
- Do not deploy the iFlow without confirmation from the user EXCEPT when the system prompt
  explicitly instructs you to fix and deploy autonomously.
"""

        llm = create_llm(deployment_id) if deployment_id else self.llm
        agent = create_agent(
            model=llm,
            tools=agent_tools,
            system_prompt=system_prompt,
        )

        if tools is None:
            # Caller used the default "all tools" path — cache as the shared agent
            self.agent = agent

        return agent

    # ── tool lookup helpers ──────────────────

    def get_mcp_tool(self, server: str, mcp_tool_name: str) -> Optional[MCPTool]:
        return self._tool_index.get((server, mcp_tool_name))

    def has_mcp_tool(self, server: str, mcp_tool_name: str) -> bool:
        return self.get_mcp_tool(server, mcp_tool_name) is not None

    def validate_required_tools(self, server: str, tool_names: List[str]) -> List[str]:
        """Return the subset of tool_names that are NOT discovered on the given server."""
        return [name for name in tool_names if not self.has_mcp_tool(server, name)]

    def get_tool_field_names(self, server: str, mcp_tool_name: str) -> List[str]:
        tool = self.get_mcp_tool(server, mcp_tool_name)
        if not tool:
            return []
        return list(self._tool_model_fields(tool.args_schema).keys())

    @staticmethod
    def _tool_model_fields(model: Type[BaseModel]) -> Dict[str, Any]:
        if hasattr(model, "model_fields"):
            return getattr(model, "model_fields")
        if hasattr(model, "__fields__"):
            return getattr(model, "__fields__")
        return {}

    def _infer_tool_args(
        self, server: str, mcp_tool_name: str, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Map context keys onto the tool's required field names using an alias table,
        so callers don't need to know the exact schema field name.
        """
        tool = self.get_mcp_tool(server, mcp_tool_name)
        if not tool:
            return {}

        alias_map: Dict[str, List[str]] = {
            "iflow_id":           ["iflow_id", "artifact_id", "integration_flow_id", "id", "name"],
            "id":                 ["iflow_id", "artifact_id", "id", "name"],
            "name":               ["iflow_id", "artifact_name", "name"],
            "artifact_id":        ["artifact_id", "iflow_id", "id", "name"],
            "artifact_name":      ["artifact_name", "iflow_id", "name"],
            "integrationflowid":  ["iflow_id", "artifact_id", "id", "name"],
            "integrationflowname":["iflow_id", "artifact_name", "name"],
            "package_id":         ["package_id", "package", "package_name"],
            "package_name":       ["package_name", "package", "package_id"],
            "message_guid":       ["message_guid", "message_id", "mpl_id"],
            "message_id":         ["message_guid", "message_id", "mpl_id"],
            "mpl_id":             ["mpl_id", "message_guid", "message_id"],
        }

        args: Dict[str, Any] = {}
        for field_name in self._tool_model_fields(tool.args_schema).keys():
            candidates = [field_name] + alias_map.get(field_name.lower(), [])
            for candidate in candidates:
                if candidate in context and context[candidate] not in (None, ""):
                    args[field_name] = context[candidate]
                    break
        return args

    async def execute_integration_tool(
        self, mcp_tool_name: str, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Convenience wrapper: infer args from context, validate required fields,
        then call execute() for the integration_suite server.
        """
        tool = self.get_mcp_tool("integration_suite", mcp_tool_name)
        if not tool:
            return {
                "success": False,
                "tool":    mcp_tool_name,
                "args":    {},
                "output":  "",
                "error":   f"Required MCP tool not available: {mcp_tool_name}",
            }

        args = self._infer_tool_args("integration_suite", mcp_tool_name, context)

        missing_required: List[str] = []
        for field_name, field in self._tool_model_fields(tool.args_schema).items():
            is_required = False
            if hasattr(field, "is_required"):
                try:
                    is_required = bool(field.is_required())
                except Exception:
                    is_required = False
            elif getattr(field, "required", False):
                is_required = True
            if is_required and field_name not in args:
                missing_required.append(field_name)

        if missing_required:
            return {
                "success": False,
                "tool":    mcp_tool_name,
                "args":    args,
                "output":  "",
                "error":   f"Missing required arguments for {mcp_tool_name}: {', '.join(missing_required)}",
            }

        output = await self.execute("integration_suite", mcp_tool_name, args)
        out_str = str(output)
        failed = out_str.startswith("ERROR:") or out_str.startswith("VALIDATION FAILED")
        return {
            "success": not failed,
            "tool":    mcp_tool_name,
            "args":    args,
            "output":  output,
            "error":   out_str if failed else "",
        }

    # ── memory ───────────────────────────────

    def cleanup_memory(self, now: Optional[float] = None) -> None:
        """Remove stale sessions and cap the total remembered session count."""
        if not self.memory:
            return

        current = now if now is not None else time.time()
        expired_sessions = [
            session_id
            for session_id, last_seen in self._memory_last_seen.items()
            if current - last_seen > MEMORY_SESSION_TTL_SECONDS
        ]
        for session_id in expired_sessions:
            self.memory.pop(session_id, None)
            self._memory_last_seen.pop(session_id, None)

        overflow = len(self.memory) - MAX_MEMORY_SESSIONS
        if overflow > 0:
            oldest_sessions = sorted(
                self.memory,
                key=lambda session_id: self._memory_last_seen.get(session_id, 0.0),
            )[:overflow]
            for session_id in oldest_sessions:
                self.memory.pop(session_id, None)
                self._memory_last_seen.pop(session_id, None)

    def update_memory(self, session_id: str, user: str, assistant: str):
        """Append a user/assistant exchange to the rolling per-session memory."""
        now = time.time()
        self.cleanup_memory(now)
        session_memory = self.memory.setdefault(session_id, [])
        session_memory.append({"role": "user",      "content": user})
        session_memory.append({"role": "assistant", "content": assistant})
        if len(session_memory) > MEMORY_LIMIT:
            self.memory[session_id] = session_memory[-MEMORY_LIMIT:]
        self._memory_last_seen[session_id] = now
