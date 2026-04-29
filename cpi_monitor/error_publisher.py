"""
cpi_monitor/error_publisher.py
===============================
Fetches per-message error details from CPI and publishes each failed message
as a structured payload to the Event Mesh topic cpi/evt/02/autofix/in.

Publishing path (tried in order):
  1. SAP Destination service — looks up the destination named
     EVENT_MESH_DESTINATION_NAME (default: "EventMesh") to obtain a
     pre-resolved bearer token and the Event Mesh REST base URL, then
     POSTs directly to the topic endpoint.  This is the production path
     when the microservice runs on SAP BTP CloudFoundry.
  2. event_bus.publish_to_next() — falls back to the in-process AEM event
     bus (which uses AEM_REST_URL + EVENT_MESH_* env vars) if the Destination
     service binding is unavailable (local dev, non-CF environments).

Deduplication: a MessageGuid is not re-published within 30 minutes.  The
dedup store is in-process memory — it resets on restart, which is acceptable
because the OData query window (10 min) is far shorter than the TTL.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from aem.event_bus import event_bus
from cpi_monitor.cpi_poller import get_cpi_client, get_destination_service_creds

logger = logging.getLogger(__name__)

_PUBLISH_TOPIC        = "cpi/evt/02/autofix/in"
_EM_DESTINATION_NAME  = os.getenv("EVENT_MESH_DESTINATION_NAME", "EventMesh")
_DEDUP_TTL            = 30 * 60  # 30 minutes in seconds

# MessageGuid → monotonic expiry timestamp
_published: Dict[str, float] = {}

# Cached Event Mesh Destination resolution (URL + token)
_em_cache: Dict[str, Any] = {"base_url": None, "token": None, "expires_at": 0.0}


# ── Deduplication ────────────────────────────────────────────────────────────

def _is_duplicate(message_guid: str) -> bool:
    """Return True if this guid was published within the last 30 minutes."""
    now = time.monotonic()
    expiry = _published.get(message_guid)
    if expiry and now < expiry:
        return True
    # Opportunistic eviction of expired entries
    stale = [k for k, v in _published.items() if now >= v]
    for k in stale:
        del _published[k]
    return False


def _mark_published(message_guid: str) -> None:
    _published[message_guid] = time.monotonic() + _DEDUP_TTL


# ── Event Mesh via SAP Destination ──────────────────────────────────────────

async def _resolve_event_mesh_destination() -> Optional[Tuple[str, str]]:
    """
    Look up the EventMesh SAP Destination via the Destination service and
    return (base_url, bearer_token).

    The resolved token is cached until 60 s before its expiry to avoid
    a Destination service round-trip on every publish call.
    Returns None if the Destination service binding is not present.
    """
    now = time.monotonic()
    if _em_cache["token"] and now < _em_cache["expires_at"] - 60:
        return _em_cache["base_url"], _em_cache["token"]

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
        # 1. Get a token scoped to the Destination service
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
            logger.warning(
                "[CPI_MONITOR] Destination '%s' returned no authTokens — cannot publish",
                _EM_DESTINATION_NAME,
            )
            return None

        em_token    = auth_tokens[0].get("value", "")
        em_url      = dest_data.get("destinationConfiguration", {}).get("URL", "")
        expires_in  = int(auth_tokens[0].get("expires_in", 3600))

        if not (em_url and em_token):
            logger.warning(
                "[CPI_MONITOR] Destination '%s' missing URL or token value",
                _EM_DESTINATION_NAME,
            )
            return None

        _em_cache["base_url"]   = em_url
        _em_cache["token"]      = em_token
        _em_cache["expires_at"] = now + expires_in
        logger.debug(
            "[CPI_MONITOR] EventMesh destination resolved: url=%s expires_in=%ds",
            em_url, expires_in,
        )
        return em_url, em_token

    except Exception as exc:
        logger.warning("[CPI_MONITOR] EventMesh destination lookup failed: %s", exc)
        return None


async def _publish_via_destination(
    topic: str, payload: Dict[str, Any], base_url: str, token: str
) -> None:
    """POST payload directly to the Event Mesh topic endpoint."""
    encoded_topic = quote(topic, safe="")
    url = f"{base_url.rstrip('/')}/messagingrest/v1/topics/{encoded_topic}/messages"
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
        raise RuntimeError(
            f"Event Mesh publish HTTP {resp.status_code}: {resp.text[:200]}"
        )


# ── Error detail fetch ───────────────────────────────────────────────────────

async def _fetch_error_detail(message_guid: str, base_url: str, bearer_token: str) -> str:
    """
    Fetch the raw error text for a MessageGuid from CPI OData.
    Returns an empty string on any failure so publishing still proceeds.
    """
    url = (
        f"{base_url.rstrip('/')}/api/v1/MessageProcessingLogs('{message_guid}')"
        f"/ErrorInformation/$value"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "Accept":        "text/plain",
                },
            )
        if resp.status_code == 200:
            return resp.text.strip()
        logger.warning(
            "[CPI_MONITOR] ErrorInformation HTTP %d for MessageGuid=%s",
            resp.status_code, message_guid,
        )
    except Exception as exc:
        logger.warning(
            "[CPI_MONITOR] ErrorInformation fetch failed for MessageGuid=%s: %s",
            message_guid, exc,
        )
    return ""


# ── Main entry point ─────────────────────────────────────────────────────────

async def publish_failed_messages(messages: List[Dict[str, Any]]) -> None:
    """
    For each FAILED message: fetch CPI error details, build the payload,
    then publish to the Event Mesh topic cpi/evt/02/autofix/in.

    Publishing tries the SAP Destination service ('EventMesh' destination)
    first; falls back to event_bus.publish_to_next() if the binding is absent.
    Silently skips duplicates.  Never raises.
    """
    if not messages:
        return

    # Resolve CPI credentials once for all error-detail fetches
    try:
        cpi = await get_cpi_client()
        if cpi is None:
            logger.error("[CPI_MONITOR] Cannot resolve CPI credentials — error fetch skipped")
            return
        cpi_base_url, cpi_token = cpi
    except Exception as exc:
        logger.error("[CPI_MONITOR] CPI auth resolution failed in error_publisher: %s", exc)
        return

    # Resolve Event Mesh publishing credentials once for the whole batch
    em_client = await _resolve_event_mesh_destination()
    if em_client:
        em_base_url, em_token = em_client
        logger.debug("[CPI_MONITOR] Publishing via SAP Destination '%s'", _EM_DESTINATION_NAME)
    else:
        em_base_url = em_token = None
        logger.debug("[CPI_MONITOR] SAP Destination unavailable — falling back to event_bus")

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
                "ErrorMessage":        error_text,
            }

            if em_base_url and em_token:
                await _publish_via_destination(_PUBLISH_TOPIC, payload, em_base_url, em_token)
            else:
                await event_bus.publish_to_next(_PUBLISH_TOPIC, payload)

            _mark_published(guid)
            logger.info(
                "[CPI_MONITOR] Published MessageGuid=%s iflow=%s",
                guid, iflow,
            )
        except Exception as exc:
            logger.error(
                "[CPI_MONITOR] Failed to publish MessageGuid=%s: %s",
                guid, exc,
            )
