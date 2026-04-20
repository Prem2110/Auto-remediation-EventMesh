"""
aem/event_bus.py
================
AEM Pattern 2 event bus — agent-to-agent messaging via SAP Advanced Event Mesh.

Each agent publishes its output to a well-known topic; the next agent in the
pipeline subscribes to that topic.  When AEM is not configured the bus falls
back to in-process direct calls (the current behaviour is fully preserved).

Topic layout:
  sap/cpi/remediation/observed/{incident_id}
  sap/cpi/remediation/classified/{incident_id}
  sap/cpi/remediation/rca/{incident_id}
  sap/cpi/remediation/fix/{incident_id}
  sap/cpi/remediation/verified/{incident_id}

Configuration (all via .env):
  AEM_ENABLED=false           — master switch; false = in-memory fallback only
  AEM_REST_URL                — SAP AEM REST Delivery Endpoint base URL
  AEM_USERNAME                — Basic auth username
  AEM_PASSWORD                — Basic auth password
  AEM_QUEUE_PREFIX            — queue name prefix (default: "cpi-remediation")

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
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_AEM_ENABLED       = os.getenv("AEM_ENABLED", "false").lower() == "true"
_AEM_REST_URL      = os.getenv("AEM_REST_URL", "")
_AEM_USERNAME      = os.getenv("AEM_USERNAME", "")
_AEM_PASSWORD      = os.getenv("AEM_PASSWORD", "")
_AEM_QUEUE_PREFIX  = os.getenv("AEM_QUEUE_PREFIX", "cpi-remediation")

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
        url = f"{_AEM_REST_URL.rstrip('/')}/{topic}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    content=json.dumps(event),
                    headers={"Content-Type": "application/json"},
                    auth=(_AEM_USERNAME, _AEM_PASSWORD),
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
        base = f"sap/cpi/remediation/{stage}"
        return f"{base}/{incident_id}" if incident_id else base

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
