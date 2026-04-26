"""
aem/event_bus.py
================
AEM Pattern 2 event bus — agent-to-agent messaging via SAP Advanced Event Mesh.

Each agent publishes its output to a well-known topic; the next agent in the
pipeline subscribes to that topic.  When AEM is not configured the bus falls
back to in-process direct calls (the current behaviour is fully preserved).

Topic layout:
  cpi/evt/02/autofix/agent/orbit/observed/{incident_id}
  cpi/evt/02/autofix/agent/orbit/classified/{incident_id}
  cpi/evt/02/autofix/agent/orbit/rca/{incident_id}
  cpi/evt/02/autofix/agent/orbit/fix/{incident_id}
  cpi/evt/02/autofix/agent/orbit/verified/{incident_id}

Configuration (all via .env):
  AEM_ENABLED=false              — master switch; false = in-memory fallback only
  AEM_REST_URL                   — SAP Event Mesh REST Delivery Endpoint base URL
  EVENT_MESH_TOKEN_URL           — OAuth2 token endpoint URL
  EVENT_MESH_CLIENT_ID           — OAuth2 client ID
  EVENT_MESH_CLIENT_SECRET       — OAuth2 client secret
  AEM_QUEUE_PREFIX               — queue name prefix (default: "cpi-remediation")

Exports:
  AEMEventBus
    .publish(topic, event)     → None   (fire-and-forget; logs on failure)
    .subscribe(topic, handler) → None   (register in-process handler)
    .emit(stage, incident_id, payload) → None  (convenience: build topic + publish)
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_AEM_ENABLED              = os.getenv("AEM_ENABLED", "false").lower() == "true"
_AEM_REST_URL             = os.getenv("AEM_REST_URL", "")
_AEM_QUEUE_PREFIX         = os.getenv("AEM_QUEUE_PREFIX", "cpi-remediation")
_EVENT_MESH_TOKEN_URL     = os.getenv("EVENT_MESH_TOKEN_URL", "")
_EVENT_MESH_CLIENT_ID     = os.getenv("EVENT_MESH_CLIENT_ID", "")
_EVENT_MESH_CLIENT_SECRET = os.getenv("EVENT_MESH_CLIENT_SECRET", "")

# Module-level token cache shared across all AEMEventBus instances
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


async def _fetch_oauth_token() -> Tuple[str, int]:
    """Obtain a fresh client-credentials token from EVENT_MESH_TOKEN_URL."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _EVENT_MESH_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": _EVENT_MESH_CLIENT_ID,
                "client_secret": _EVENT_MESH_CLIENT_SECRET,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"OAuth token fetch failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    body = resp.json()
    return body["access_token"], int(body.get("expires_in", 3600))


async def _get_bearer_token() -> str:
    """Return a cached bearer token, refreshing 60 s before expiry."""
    now = time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    token, expires_in = await _fetch_oauth_token()
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in
    logger.debug("[AEM] OAuth token refreshed, expires in %ds", expires_in)
    return token

# Known pipeline stages in order
PIPELINE_STAGES = ("observed", "classified", "rca", "fix", "verified")


class AEMEventBus:
    """
    Lightweight event bus with two modes:

    1. AEM_ENABLED=false (default)  — in-memory only.
       subscribe() registers a Python coroutine as handler.
       publish() calls registered handlers directly, no network I/O.
       Zero external dependencies; safe for local dev and unit tests.

    2. AEM_ENABLED=true — REST delivery to SAP AEM.
       publish() POSTs the event JSON to the AEM REST Delivery Endpoint.
       subscribe() still registers in-process handlers (for the same process
       to consume its own events during local-mode hybrid testing).
    """

    def __init__(self):
        # in-process handler registry: topic_prefix → List[async callable]
        self._handlers: Dict[str, List[Callable]] = {}

    # ── subscribe ────────────────────────────────────────────────────────────

    def subscribe(self, topic: str, handler: Callable) -> None:
        """
        Register an async callable as an in-process handler for messages on
        topic.  The handler receives a single argument: the decoded event dict.

        Subscribing does NOT require AEM to be enabled — it works in both modes
        and is the primary mechanism for inter-agent calls when AEM_ENABLED=false.
        """
        self._handlers.setdefault(topic, []).append(handler)
        logger.debug("[AEM] Handler registered for topic: %s", topic)

    # ── publish ──────────────────────────────────────────────────────────────

    async def publish(self, topic: str, event: Dict[str, Any]) -> None:
        """
        Publish an event to the given topic.

        If AEM is enabled, POST to the REST endpoint.
        Always call any in-process handlers registered for this topic
        (so the pipeline keeps working even without external AEM connectivity).
        """
        if _AEM_ENABLED and _AEM_REST_URL:
            await self._publish_rest(topic, event)
        await self._dispatch_local(topic, event)

    async def _publish_rest(self, topic: str, event: Dict[str, Any]) -> None:
        encoded_topic = quote(topic, safe="")
        url = f"{_AEM_REST_URL.rstrip('/')}/messagingrest/v1/topics/{encoded_topic}/messages"
        try:
            token = await _get_bearer_token()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    content=json.dumps(event),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                        "x-qos": "0",
                    },
                )
            if resp.status_code not in (200, 202, 204):
                logger.warning(
                    "[AEM] REST publish to '%s' returned HTTP %d: %s",
                    topic, resp.status_code, resp.text[:200],
                )
            else:
                logger.debug("[AEM] Published to '%s' (HTTP %d)", topic, resp.status_code)
        except Exception as exc:
            logger.warning("[AEM] REST publish failed for topic '%s': %s", topic, exc)

    async def _dispatch_local(self, topic: str, event: Dict[str, Any]) -> None:
        """
        Call all in-process handlers registered for this topic.

        Supports prefix matching: a handler registered at "sap/cpi/remediation/classified"
        will also fire for "sap/cpi/remediation/classified/{incident_id}".
        """
        matched: list = list(self._handlers.get(topic, []))
        for reg_topic, reg_handlers in self._handlers.items():
            if reg_topic != topic and topic.startswith(reg_topic + "/"):
                matched.extend(reg_handlers)
        if not matched:
            return
        for handler in matched:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as exc:
                logger.error("[AEM] Handler for topic '%s' raised: %s", topic, exc)

    # ── convenience helper ───────────────────────────────────────────────────

    def make_topic(self, stage: str, incident_id: str = "") -> str:
        """Build the canonical topic string for a pipeline stage."""
        base = f"cpi/evt/02/autofix/agent/orbit/{stage}"
        return f"{base}/{incident_id}" if incident_id else base

    async def publish_to_next(self, topic: str, payload: Dict[str, Any]) -> None:
        """
        Publish an inter-agent handoff with x-qos: 1 (guaranteed delivery).

        Use this instead of publish() when delivering from one agent endpoint
        to the next in the event-driven pipeline.  Failures are logged only —
        the caller must NOT raise so the current agent's response is unaffected.
        """
        if _AEM_ENABLED and _AEM_REST_URL:
            encoded_topic = quote(topic, safe="")
            url = f"{_AEM_REST_URL.rstrip('/')}/messagingrest/v1/topics/{encoded_topic}/messages"
            try:
                token = await _get_bearer_token()
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        url,
                        content=json.dumps(payload),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {token}",
                            "x-qos": "1",
                        },
                    )
                if resp.status_code not in (200, 202, 204):
                    logger.warning(
                        "[AEM] publish_to_next '%s' returned HTTP %d: %s",
                        topic, resp.status_code, resp.text[:200],
                    )
                else:
                    logger.debug("[AEM] publish_to_next '%s' OK (HTTP %d)", topic, resp.status_code)
            except Exception as exc:
                logger.warning("[AEM] publish_to_next dead-letter for '%s': %s", topic, exc)
        # always dispatch in-process handlers too
        await self._dispatch_local(topic, payload)

    async def emit(
        self,
        stage: str,
        incident_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """
        Convenience method: publish a pipeline stage event.

        Example:
            await bus.emit("rca", incident_id, {"root_cause": ..., "proposed_fix": ...})
        """
        topic = self.make_topic(stage, incident_id)
        event = {
            "stage":       stage,
            "incident_id": incident_id,
            "payload":     payload,
        }
        await self.publish(topic, event)
        logger.info("[AEM] Emitted stage='%s' incident='%s'", stage, incident_id)


# ─────────────────────────────────────────────
# Module-level singleton — shared by all agents
# ─────────────────────────────────────────────
event_bus = AEMEventBus()
