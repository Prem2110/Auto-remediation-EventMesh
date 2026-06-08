"""
monitoring/llm_monitor.py
=========================
Fire-and-forget LLM usage reporter.

Sends every agent/LLM invocation to the central monitor API as a background
asyncio Task. Errors are caught and logged at DEBUG level so they never
surface into the application path. If LLM_USAGE_MONITOR_BASE_URL is not set,
all calls are no-ops.

Environment variables consumed
-------------------------------
LLM_USAGE_MONITOR_BASE_URL          — service root URL (disabled if unset/empty)
LLM_USAGE_MONITOR_API_KEY           — Bearer token
LLM_USAGE_MONITOR_APP_ID            — app_id query param
LLM_USAGE_MONITOR_MODEL_NAME        — ultimate model-name fallback
LLM_USAGE_MONITOR_CALL_TYPE_L_INVOKE — call_type for direct LLM calls (default "l_invoke")
LLM_USAGE_MONITOR_CALL_TYPE_A_INVOKE — call_type for agent calls     (default "a_invoke")

LLM_MODEL_NAME      — human-readable model name for LLM_DEPLOYMENT_ID
LLM_MODEL_NAME_RCA  — human-readable model name for LLM_DEPLOYMENT_ID_RCA
LLM_MODEL_NAME_FIX  — human-readable model name for LLM_DEPLOYMENT_ID_FIX
"""

import asyncio
import json
import logging
import os
import threading
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ── module-level config (read once at import time) ────────────────────────────

_BASE_URL      = os.getenv("LLM_USAGE_MONITOR_BASE_URL", "").rstrip("/")
_API_KEY       = os.getenv("LLM_USAGE_MONITOR_API_KEY", "")
_APP_ID        = os.getenv("LLM_USAGE_MONITOR_APP_ID", "")
_DEFAULT_MODEL = os.getenv("LLM_USAGE_MONITOR_MODEL_NAME", "")
_TYPE_A        = os.getenv("LLM_USAGE_MONITOR_CALL_TYPE_A_INVOKE", "a_invoke")
_TYPE_L        = os.getenv("LLM_USAGE_MONITOR_CALL_TYPE_L_INVOKE", "l_invoke")

# Deployment-ID → model-name lookup built at startup from env vars.
# Call sites pass deployment_id (what they know); monitor resolves model name.
_DEPLOYMENT_MODEL_MAP: dict[str, str] = {}
for _dep_key, _name_key in [
    ("LLM_DEPLOYMENT_ID",     "LLM_MODEL_NAME"),
    ("LLM_DEPLOYMENT_ID_RCA", "LLM_MODEL_NAME_RCA"),
    ("LLM_DEPLOYMENT_ID_FIX", "LLM_MODEL_NAME_FIX"),
]:
    _dep  = os.getenv(_dep_key, "")
    _name = os.getenv(_name_key, "")
    if _dep and _name:
        _DEPLOYMENT_MODEL_MAP[_dep] = _name


# ── model name resolution ─────────────────────────────────────────────────────

def _extract_model_from_response(response: Any) -> str:
    """Best-effort extraction of the real model name from a LangChain response."""
    try:
        # AIMessage path — direct llm.ainvoke() result
        if hasattr(response, "response_metadata"):
            name = (response.response_metadata or {}).get("model_name", "")
            if name:
                return str(name)
        # LLMResult path — from on_llm_end callback
        if hasattr(response, "llm_output"):
            name = (response.llm_output or {}).get("model_name", "")
            if name:
                return str(name)
    except Exception:
        pass
    return ""


def _resolve_model(deployment_id: Optional[str], response: Any = None) -> str:
    """Return the model name to send to the monitor service."""
    # 1. Extract from the response object (most accurate — comes from the API)
    if response is not None:
        name = _extract_model_from_response(response)
        if name:
            return name
    # 2. Map deployment_id → configured model name
    if deployment_id and deployment_id in _DEPLOYMENT_MODEL_MAP:
        return _DEPLOYMENT_MODEL_MAP[deployment_id]
    # 3. Fall back to the static env var
    return _DEFAULT_MODEL


# ── async sender ──────────────────────────────────────────────────────────────

def _build_request_kwargs(call_type: str, model_name: str, metadata_str: str) -> dict:
    # Truncate to avoid 400s from oversized bodies (monitor stores for analytics, not replay)
    if len(metadata_str) > 8000:
        metadata_str = metadata_str[:8000] + "...[truncated]"
    return {
        "url": f"{_BASE_URL}/log-metadata",
        "params": {
            "app_id":     _APP_ID,
            "call_type":  call_type,
            "model_name": model_name or _DEFAULT_MODEL,
        },
        "headers": {"Authorization": f"Bearer {_API_KEY}"},
        "json": {"metadata": metadata_str},
    }


async def _post(call_type: str, model_name: str, metadata_str: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.post(**_build_request_kwargs(call_type, model_name, metadata_str))
            if r.status_code >= 400:
                logger.warning(
                    "[LLMMonitor] POST /log-metadata status=%d body=%s",
                    r.status_code, r.text[:300],
                )
    except Exception as exc:
        logger.debug("[LLMMonitor] post failed (non-fatal): %s", exc)


def _post_sync(call_type: str, model_name: str, metadata_str: str) -> None:
    """Synchronous HTTP post — used when called from a non-async thread (e.g. LangChain callbacks)."""
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            r = client.post(**_build_request_kwargs(call_type, model_name, metadata_str))
            if r.status_code >= 400:
                logger.warning(
                    "[LLMMonitor] POST /log-metadata status=%d body=%s",
                    r.status_code, r.text[:300],
                )
    except Exception as exc:
        logger.debug("[LLMMonitor] sync post failed (non-fatal): %s", exc)


def _fire(call_type: str, model_name: str, metadata_str: str) -> None:
    """Fire-and-forget — works from both async context and LangChain callback threads."""
    if not _BASE_URL:
        return
    try:
        loop = asyncio.get_running_loop()
        # Running inside an async coroutine — schedule as a Task (non-blocking)
        loop.create_task(_post(call_type, model_name or _DEFAULT_MODEL, metadata_str))
    except RuntimeError:
        # LangChain callbacks run in a thread pool with no event loop — use sync httpx
        threading.Thread(
            target=_post_sync,
            args=(call_type, model_name, metadata_str),
            daemon=True,
        ).start()
    except Exception as exc:
        logger.debug("[LLMMonitor] _fire failed: %s", exc)


# ── public API ────────────────────────────────────────────────────────────────

def log_agent_invoke(result: Any, deployment_id: Optional[str] = None) -> None:
    """
    Call immediately after agent.ainvoke() returns.
    Serialises with langchain_core.load.dumpd so LangChain message objects are
    JSON-safe. Falls back to plain json.dumps if dumpd is unavailable.
    """
    if not _BASE_URL:
        return
    try:
        from langchain_core.load import dumpd  # noqa: PLC0415
        metadata_str = json.dumps(dumpd(result), default=str)
    except Exception:
        try:
            metadata_str = json.dumps(result, default=str)
        except Exception as exc:
            logger.debug("[LLMMonitor] serialize failed: %s", exc)
            return
    _fire(_TYPE_A, _resolve_model(deployment_id), metadata_str)


def log_llm_invoke(response: Any, deployment_id: Optional[str] = None) -> None:
    """
    Call after llm.ainvoke() OR from the on_llm_end callback.
    Handles both AIMessage (.model_dump) and LLMResult (.generations).
    Model name is extracted from the response object first, then resolved
    via the deployment_id → model-name map, then falls back to env var.
    """
    if not _BASE_URL:
        return
    model_name = _resolve_model(deployment_id, response)
    try:
        if hasattr(response, "generations"):
            # LLMResult from on_llm_end callback
            # Use the first generation's AIMessage.model_dump() — it includes all fields
            # the monitor expects (usage_metadata, response_metadata, content, etc.)
            first_gen = None
            try:
                first_gen = (response.generations or [[]])[0][0] if response.generations else None
            except (IndexError, AttributeError):
                pass
            if first_gen is not None and hasattr(first_gen, "message") and hasattr(first_gen.message, "model_dump"):
                metadata_str = json.dumps(first_gen.message.model_dump(), default=str)
            else:
                # Fallback: build minimal payload with required fields from llm_output
                tu = (response.llm_output or {}).get("token_usage", {})
                usage_metadata = {
                    "input_tokens": tu.get("prompt_tokens", 0),
                    "output_tokens": tu.get("completion_tokens", 0),
                    "total_tokens": tu.get("total_tokens", 0),
                } if tu else None
                metadata_str = json.dumps(
                    {
                        "llm_output": response.llm_output,
                        "usage_metadata": usage_metadata,
                        "response_metadata": response.llm_output,
                        "generations": [
                            [g.text if hasattr(g, "text") else str(g) for g in gen_list]
                            for gen_list in response.generations
                        ],
                    },
                    default=str,
                )
        elif hasattr(response, "model_dump"):
            # AIMessage from direct llm.ainvoke()
            metadata_str = json.dumps(response.model_dump(), default=str)
        else:
            metadata_str = json.dumps({"content": str(response)}, default=str)
    except Exception as exc:
        logger.debug("[LLMMonitor] serialize failed: %s", exc)
        return
    _fire(_TYPE_L, model_name, metadata_str)
