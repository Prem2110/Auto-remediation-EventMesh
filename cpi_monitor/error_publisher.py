"""
cpi_monitor/error_publisher.py
===============================
Fetches per-message error details from CPI and publishes each failed message
as a structured payload to the Event Mesh topic default/sierra.automation/1/autofix/in.

Publishing path:
  SAP Destination service — looks up the destination named
  EVENT_MESH_DESTINATION_NAME (default: "EventMesh") to obtain a
  bearer token.  The publish URL comes from AEM_REST_URL env var.
  If the Destination is unavailable the message is skipped (logged as error).

Deduplication: a MessageGuid is not re-published within 30 minutes.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from cpi_monitor.cpi_poller import get_cpi_client, get_destination_service_creds
from utils.utils import clean_error_message

logger = logging.getLogger(__name__)

_PUBLISH_TOPIC       = "default/sierra.automation/1/autofix/in"
_EM_DESTINATION_NAME = os.getenv("EVENT_MESH_DESTINATION_NAME", "EventMesh")
_AEM_REST_URL        = os.getenv("AEM_REST_URL", "")
_DEDUP_TTL           = 30 * 60  # 30 minutes in seconds

# MessageGuid → monotonic expiry timestamp
_published: Dict[str, float] = {}

# Cached bearer token from the EventMesh destination
_em_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


# ── Deduplication ────────────────────────────────────────────────────────────

def _is_duplicate(message_guid: str) -> bool:
    now = time.monotonic()
    expiry = _published.get(message_guid)
    if expiry and now < expiry:
        return True
    stale = [k for k, v in _published.items() if now >= v]
    for k in stale:
        del _published[k]
    return False


def _mark_published(message_guid: str) -> None:
    _published[message_guid] = time.monotonic() + _DEDUP_TTL


# ── Bearer token from SAP Destination ────────────────────────────────────────

async def _resolve_em_token() -> Optional[str]:
    """
    Look up the EventMesh SAP Destination and return only the bearer token.
    The publish URL is taken from AEM_REST_URL env var — not from the destination.
    Token is cached until 60 s before expiry.
    """
    now = time.monotonic()
    if _em_cache["token"] and now < _em_cache["expires_at"] - 60:
        return _em_cache["token"]

    creds = get_destination_service_creds()
    if not creds:
        return None

    dest_uri   = creds.get("uri", "")
    token_url  = creds.get("url", "").rstrip("/") + "/oauth/token"
    client_id  = creds.get("clientid", "")
    client_sec = creds.get("clientsecret", "")

    if not (dest_uri and client_id and client_sec):
        return None

    try:
        # 1. Get a token scoped to the Destination service itself
        async with httpx.AsyncClient(timeout=15) as client:
            tok_resp = await client.post(
                token_url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     client_id,
                    "client_secret": client_sec,
                },
            )
        tok_resp.raise_for_status()
        dest_token = tok_resp.json()["access_token"]

        # 2. Fetch the named EventMesh destination
        async with httpx.AsyncClient(timeout=10) as client:
            dest_resp = await client.get(
                f"{dest_uri.rstrip('/')}/destination-configuration/v1/destinations/{_EM_DESTINATION_NAME}",
                headers={"Authorization": f"Bearer {dest_token}"},
            )
        dest_resp.raise_for_status()
        dest_data = dest_resp.json()

        auth_tokens = dest_data.get("authTokens", [])
        if not auth_tokens:
            logger.warning("[CPI_MONITOR] Destination '%s' returned no authTokens", _EM_DESTINATION_NAME)
            return None

        em_token   = auth_tokens[0].get("value", "")
        expires_in = int(auth_tokens[0].get("expires_in", 3600))

        if not em_token:
            return None

        _em_cache["token"]      = em_token
        _em_cache["expires_at"] = now + expires_in
        logger.debug("[CPI_MONITOR] EventMesh token resolved via Destination service, expires_in=%ds", expires_in)
        return em_token

    except Exception as exc:
        logger.warning("[CPI_MONITOR] EventMesh destination token lookup failed: %s", exc)
        return None


# ── Publish to Event Mesh ─────────────────────────────────────────────────────

async def _publish_via_destination(topic: str, payload: Dict[str, Any], token: str) -> None:
    """
    POST payload to Event Mesh using:
      - AEM_REST_URL  as the base publish endpoint
      - token         from the EventMesh SAP Destination
    """
    if not _AEM_REST_URL:
        raise RuntimeError("AEM_REST_URL is not set — cannot publish via Destination")

    encoded_topic = quote(topic, safe="")
    url = f"{_AEM_REST_URL.rstrip('/')}/messagingrest/v1/topics/{encoded_topic}/messages"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            content=json.dumps(payload),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
                "x-qos":         "1",
            },
        )
    if resp.status_code not in (200, 202, 204):
        raise RuntimeError(f"Event Mesh publish HTTP {resp.status_code}: {resp.text[:200]}")


# ── Error detail fetch ────────────────────────────────────────────────────────

async def _fetch_error_detail(message_guid: str, base_url: str, bearer_token: str) -> str:
    """Fetch raw error text for a MessageGuid from CPI OData."""
    url = (
        f"{base_url.rstrip('/')}/api/v1/MessageProcessingLogs('{message_guid}')"
        f"/ErrorInformation/$value"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {bearer_token}", "Accept": "text/plain"},
            )
        if resp.status_code == 200:
            return resp.text.strip()
        logger.warning("[CPI_MONITOR] ErrorInformation HTTP %d for %s", resp.status_code, message_guid)
    except Exception as exc:
        logger.warning("[CPI_MONITOR] ErrorInformation fetch failed for %s: %s", message_guid, exc)
    return ""


# ── Main entry point ──────────────────────────────────────────────────────────

async def publish_failed_messages(messages: List[Dict[str, Any]]) -> None:
    """
    For each FAILED message: fetch CPI error details, build payload, publish
    to Event Mesh topic default/sierra.automation/1/autofix/in.

    Uses SAP Destination for the bearer token + AEM_REST_URL for the endpoint.
    Skips the message (logs error) if the Destination token is unavailable.
    Skips duplicates. Never raises.
    """
    if not messages:
        return

    # Resolve CPI credentials once for all error-detail fetches
    try:
        cpi = await get_cpi_client()
        if cpi is None:
            logger.error("[CPI_MONITOR] No CPI credentials — error fetch skipped")
            return
        cpi_base_url, cpi_token = cpi
    except Exception as exc:
        logger.error("[CPI_MONITOR] CPI auth failed: %s", exc)
        return

    # Resolve Event Mesh bearer token once for the whole batch
    em_token = await _resolve_em_token()
    if not em_token:
        logger.error("[CPI_MONITOR] EventMesh destination token resolution FAILED - check destination binding")
        return
    logger.info("[CPI_MONITOR] EventMesh destination token resolved successfully")
    logger.debug("[CPI_MONITOR] Publishing via SAP Destination '%s' + AEM_REST_URL", _EM_DESTINATION_NAME)

    for msg in messages:
        guid    = msg.get("MessageGuid", "")
        iflow   = msg.get("IntegrationFlowName", "")
        log_end = msg.get("LogEnd", "")

        if not guid:
            continue

        if _is_duplicate(guid):
            logger.debug("[CPI_MONITOR] Skipping duplicate MessageGuid=%s", guid)
            continue

        try:
            error_text = await _fetch_error_detail(guid, cpi_base_url, cpi_token)
            payload: Dict[str, Any] = {
                "IflowId":             iflow,
                "MessageGuid":         guid,
                "IntegrationFlowName": iflow,
                "Status":              "FAILED",
                "LogEnd":              log_end,
                "ErrorMessage":        clean_error_message(error_text),
            }

            await _publish_via_destination(_PUBLISH_TOPIC, payload, em_token)

            _mark_published(guid)
            logger.info("[CPI_MONITOR] Published MessageGuid=%s iflow=%s", guid, iflow)

        except Exception as exc:
            logger.error("[CPI_MONITOR] Failed to publish MessageGuid=%s: %s", guid, exc)
