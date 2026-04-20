"""
aem/solace_client.py
====================
Solace PubSub+ Web Messaging client (wss:// transport via solace-pubsubplus SDK).

Wraps the synchronous Solace SDK in asyncio-compatible helpers so FastAPI
and the orchestrator can use async/await throughout.

Architecture
------------
Publisher
  Synchronous Solace SDK calls are offloaded to a thread-pool executor so
  they never block the asyncio event loop.

Receiver (background thread)
  A dedicated daemon thread opens a persistent queue receiver on the single
  AEM queue, polls every 1 s, and pushes messages into an asyncio.Queue.
  The orchestrator's autonomous loop drains that queue with get_message().

Modes
-----
  AEM_ENABLED=true   → connect to Solace broker over wss://.
  AEM_ENABLED=false  → no-op (orchestrator uses its own local asyncio.Queue).

Configuration (.env)
--------------------
  AEM_ENABLED         master switch
  AEM_HOST            wss://<broker>:443
  AEM_VPN             Solace Message VPN name
  AEM_USERNAME        REST/Web-Messaging user
  AEM_PASSWORD
  AEM_OBSERVER_QUEUE  queue name (default: sap.cpi.autofix.observer.out)
  AEM_OBSERVER_TOPIC  topic published to (default: sap/cpi/autofix/observer/out)

Exports
-------
  SolaceClient
    .connect()              async — build session + start publisher
    .publish(topic, data)   async — fire-and-forget publish to topic
    .start_receiver(loop)   sync  — start background receiver thread
    .get_message()          async — non-blocking pop from inbound queue
    .disconnect()           sync  — clean shutdown
  solace_client             module-level singleton
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from solace.messaging.config.solace_properties import (
    authentication_properties as AP,
    service_properties as SP,
    transport_layer_properties as TP,
)
from solace.messaging.config.transport_security_strategy import TLS
from solace.messaging.messaging_service import MessagingService
from solace.messaging.resources.queue import Queue
from solace.messaging.resources.topic import Topic

from core.constants import SOLACE_INBOUND_QUEUE_MAXSIZE

logger = logging.getLogger(__name__)

_AEM_HOST     = os.getenv("AEM_HOST", "")
_AEM_VPN      = os.getenv("AEM_VPN", "")
_AEM_USERNAME = os.getenv("AEM_USERNAME", "")
_AEM_PASSWORD = os.getenv("AEM_PASSWORD", "")
_AEM_QUEUE    = os.getenv("AEM_OBSERVER_QUEUE", "sap.cpi.autofix.observer.out")
_AEM_TOPIC    = os.getenv("AEM_OBSERVER_TOPIC", "sap/cpi/autofix/observer/out")


def _build_service() -> MessagingService:
    """Build a new MessagingService instance from env config."""
    props = {
        TP.HOST:                      _AEM_HOST,
        SP.VPN_NAME:                  _AEM_VPN,
        AP.SCHEME_BASIC_USER_NAME:    _AEM_USERNAME,
        AP.SCHEME_BASIC_PASSWORD:     _AEM_PASSWORD,
    }
    return (
        MessagingService.builder()
        .from_properties(props)
        .with_transport_security_strategy(TLS.create().without_certificate_validation())
        .build()
    )


class SolaceClient:
    """Async-friendly Solace PubSub+ Web Messaging client."""

    def __init__(self) -> None:
        self._service:  Optional[MessagingService] = None
        self._publisher = None
        self._receiver_thread: Optional[threading.Thread] = None
        self._running:  bool = False
        self._receiver_connected: bool = False   # True only while queue receiver is active
        self._loop:     Optional[asyncio.AbstractEventLoop] = None
        self._inbound:  asyncio.Queue = asyncio.Queue(maxsize=SOLACE_INBOUND_QUEUE_MAXSIZE)
        self.messages_retrieved: int = 0   # total messages pulled from AEM queue
        self.messages_published: int = 0   # total messages published to AEM topic
        self.messages_dropped: int = 0

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Solace broker and start the direct publisher (async wrapper)."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self) -> None:
        self._service = _build_service()
        self._service.connect()
        self._publisher = (
            self._service
            .create_direct_message_publisher_builder()
            .build()
        )
        self._publisher.start()
        logger.info(
            "[Solace] Publisher connected  host=%s  vpn=%s  user=%s",
            _AEM_HOST, _AEM_VPN, _AEM_USERNAME,
        )

    # ── Publish ───────────────────────────────────────────────────────────────

    async def publish(self, topic: str, payload: Dict[str, Any]) -> None:
        """Serialize payload to JSON and publish to the given Solace topic."""
        if self._service is None or self._publisher is None:
            logger.error("[Solace] publish() called before connect()")
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._publish_sync, topic, payload)

    def _publish_sync(self, topic: str, payload: Dict[str, Any]) -> None:
        try:
            msg = self._service.message_builder().build(json.dumps(payload))
            self._publisher.publish(destination=Topic.of(topic), message=msg)
            self.messages_published += 1
            logger.info("[Solace] Published  topic=%s", topic)
        except Exception as exc:
            logger.error("[Solace] Publish failed  topic=%s  error=%s", topic, exc)

    # ── Queue Receiver (background thread) ───────────────────────────────────

    def start_receiver(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Spawn a daemon thread that polls the AEM queue and feeds messages
        into self._inbound so the orchestrator's asyncio loop can drain it.
        """
        self._loop    = loop
        self._running = True
        self._receiver_thread = threading.Thread(
            target=self._receiver_loop,
            daemon=True,
            name="solace-receiver",
        )
        self._receiver_thread.start()
        logger.info("[Solace] Receiver thread started  queue=%s", _AEM_QUEUE)

    def _receiver_loop(self) -> None:
        """
        Runs in background thread: open receiver → poll → feed asyncio.Queue.
        Automatically reconnects with exponential backoff if the connection drops.
        """
        _RETRY_INITIAL = 2    # seconds before first retry
        _RETRY_MAX     = 30   # cap backoff at 30 s
        retry_delay    = _RETRY_INITIAL

        while self._running:
            svc  = None
            recv = None
            try:
                svc = _build_service()
                svc.connect()
                recv = (
                    svc.create_persistent_message_receiver_builder()
                    .build(Queue.durable_exclusive_queue(_AEM_QUEUE))
                )
                recv.start()
                self._receiver_connected = True
                retry_delay = _RETRY_INITIAL          # reset backoff after clean connect
                logger.info("[Solace] Queue receiver active  queue=%s", _AEM_QUEUE)

                # ── inner poll loop ──────────────────────────────────────────
                while self._running:
                    try:
                        msg = recv.receive_message(timeout=1000)   # 1-second poll
                        if msg is not None:
                            raw = msg.get_payload_as_string()
                            if raw is None:
                                raw_bytes = msg.get_payload_as_bytes()
                                if raw_bytes:
                                    raw = raw_bytes.decode("utf-8", errors="replace")
                            try:
                                event = json.loads(raw) if raw else {}
                            except Exception:
                                event = {"raw_body": raw}
                            if event:
                                self._enqueue_inbound(event)
                                recv.ack(msg)
                                self.messages_retrieved += 1
                                logger.debug("[Solace] Message received  keys=%s", list(event.keys()))
                            else:
                                logger.warning("[Solace] Empty/unparseable message — skipping, ack anyway")
                                recv.ack(msg)
                    except Exception as exc:
                        logger.error("[Solace] Receiver poll error: %s — reconnecting", exc)
                        break   # drop to outer loop → reconnect

            except Exception as exc:
                logger.error(
                    "[Solace] Receiver connection failed: %s — retrying in %ss",
                    exc, retry_delay,
                )
            finally:
                self._receiver_connected = False
                for obj, method in ((recv, "terminate"), (svc, "disconnect")):
                    if obj is not None:
                        try:
                            getattr(obj, method)()
                        except Exception:
                            pass

            if self._running:
                logger.info("[Solace] Receiver reconnecting in %ss…", retry_delay)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, _RETRY_MAX)

        logger.info("[Solace] Receiver thread exited")

    async def get_message(self) -> Optional[Dict[str, Any]]:
        """Non-blocking pop from the inbound queue. Returns None if empty."""
        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _put_with_drop_oldest(self, event: Dict[str, Any]) -> None:
        """Keep the queue bounded by dropping the oldest event on overflow."""
        if self._inbound.full():
            try:
                self._inbound.get_nowait()
                self.messages_dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            self._inbound.put_nowait(event)
        except asyncio.QueueFull:
            self.messages_dropped += 1
            logger.warning("[Solace] Inbound queue still full; dropping newest event")

    def _enqueue_inbound(self, event: Dict[str, Any]) -> None:
        if self._loop is None:
            self.messages_dropped += 1
            logger.warning("[Solace] Event loop unavailable; dropping inbound event")
            return
        future = asyncio.run_coroutine_threadsafe(
            self._put_with_drop_oldest(event), self._loop
        )
        try:
            future.result(timeout=2)
        except concurrent.futures.TimeoutError:
            self.messages_dropped += 1
            logger.warning("[Solace] Timed out enqueuing inbound event; dropping event")
        except Exception as exc:
            self.messages_dropped += 1
            logger.error("[Solace] Failed to enqueue inbound event: %s", exc)

    # ── Disconnect ────────────────────────────────────────────────────────────

    def disconnect(self) -> None:
        """Stop receiver thread and disconnect the publisher session."""
        self._running = False
        if self._publisher:
            try:
                self._publisher.terminate()
            except Exception:
                pass
        if self._service:
            try:
                self._service.disconnect()
            except Exception:
                pass
        logger.info("[Solace] Client disconnected")


# Module-level singleton — shared by orchestrator and main
solace_client = SolaceClient()
