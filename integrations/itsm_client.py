"""
integrations/itsm_client.py
Calls the in-house ITSM OData API via SAP Destination "ITSM-Sierra".
Pattern mirrors cpi_monitor/error_publisher.py exactly.
"""
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from cpi_monitor.cpi_poller import get_destination_service_creds

logger = logging.getLogger(__name__)

_DESTINATION_NAME = "ITSM-sierra"
_TICKETS_ENDPOINT = "/odata/v4/ticket/Tickets"

# Token + base URL cache (same pattern as _em_cache in error_publisher.py)
_itsm_cache: Dict[str, Any] = {"token": None, "base_url": None, "expires_at": 0.0}


def _invalidate_cache() -> None:
    """Clear cached token so the next call forces a fresh Destination resolution."""
    _itsm_cache["token"]      = None
    _itsm_cache["base_url"]   = None
    _itsm_cache["expires_at"] = 0.0
    logger.warning("[ITSM] Token cache invalidated — will re-resolve on next request")


async def _resolve_itsm_destination() -> Optional[Dict[str, str]]:
    """
    Resolve the ITSM-Sierra SAP Destination.
    Returns {"base_url": ..., "token": ...} or None on failure.
    Caches until 60s before expiry — same pattern as _resolve_em_token().
    """
    now = time.monotonic()
    if _itsm_cache["token"] and _itsm_cache["base_url"] and now < _itsm_cache["expires_at"] - 60:
        return {"base_url": _itsm_cache["base_url"], "token": _itsm_cache["token"]}

    creds = get_destination_service_creds()
    if not creds:
        logger.error("[ITSM] SAP Destination service credentials not found in VCAP_SERVICES")
        return None

    dest_uri   = creds.get("uri", "")
    token_url  = creds.get("url", "").rstrip("/") + "/oauth/token"
    client_id  = creds.get("clientid", "")
    client_sec = creds.get("clientsecret", "")

    if not (dest_uri and client_id and client_sec):
        logger.error("[ITSM] Incomplete SAP Destination service credentials")
        return None

    try:
        # Step 1 — Get a token scoped to the Destination service itself
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

        # Step 2 — Fetch the named destination "ITSM-Sierra"
        async with httpx.AsyncClient(timeout=10) as client:
            dest_resp = await client.get(
                f"{dest_uri.rstrip('/')}/destination-configuration/v1/destinations/{_DESTINATION_NAME}",
                headers={"Authorization": f"Bearer {dest_token}"},
            )
        dest_resp.raise_for_status()
        dest_data = dest_resp.json()

        # Step 3 — Extract base URL and bearer token
        base_url    = dest_data.get("destinationConfiguration", {}).get("URL", "").rstrip("/")
        auth_tokens = dest_data.get("authTokens", [])

        if not base_url:
            logger.error("[ITSM] Destination 'ITSM-Sierra' returned no URL")
            return None
        if not auth_tokens:
            logger.error("[ITSM] Destination 'ITSM-Sierra' returned no authTokens")
            return None

        itsm_token = auth_tokens[0].get("value", "")
        expires_in = int(auth_tokens[0].get("expires_in", 3600))

        if not itsm_token:
            logger.error("[ITSM] ITSM-Sierra authToken value is empty")
            return None

        _itsm_cache["token"]      = itsm_token
        _itsm_cache["base_url"]   = base_url
        _itsm_cache["expires_at"] = now + expires_in
        logger.debug(
            "[ITSM] ITSM-Sierra destination resolved. base_url=%s expires_in=%ds",
            base_url, expires_in,
        )
        return {"base_url": base_url, "token": itsm_token}

    except Exception as exc:
        logger.error("[ITSM] Failed to resolve ITSM-Sierra destination: %s", exc)
        return None


async def create_itsm_ticket(ticket: Dict) -> Optional[str]:
    """
    POST /odata/v4/ticket/Tickets to create a ticket in ITSM.
    Maps incident fields to ITSM payload.
    Returns the ITSM ticket ID (as string) or None on failure.
    Never raises — failures are logged and swallowed.

    Mandatory fields:  title, type_code, source, requester_ID
    Full payload uses all available incident context.
    """
    dest = await _resolve_itsm_destination()
    if not dest:
        return None

    try:
        payload = {
            # Mandatory
            "title":        ticket.get("title") or f"Auto-remediation failed: {ticket.get('iflow_id', 'unknown')}",
            "type_code":    "incident",
            "source":       "Orbit Monitor app",
            "requester_ID": ticket.get("requester_id", os.getenv("ITSM_REQUESTER_ID", "")),

            # LLM-generated fields passed through from orchestrator
            "description":     ticket.get("description", ""),
            "priority":        ticket.get("priority",        "medium"),
            "severity_code":   ticket.get("severity_code",   "medium"),
            "impact_code":     ticket.get("impact_code",     "medium"),
            "urgency_code":    ticket.get("urgency_code",    "medium"),
            "category":        ticket.get("category",        ""),
            "subcategory":     ticket.get("subcategory",     ""),
            "tags":            ticket.get("tags",            ""),
            "businessService": ticket.get("businessService", ""),
            "application":     ticket.get("application",     ""),
            "component":       ticket.get("component",       ""),
            "environment":     ticket.get("environment",     "production"),
            "configItem":      ticket.get("configItem",      ""),
            "hostName":        ticket.get("hostName",        ""),
            "errorCode":       ticket.get("errorCode",       ""),
        }

        url = f"{dest['base_url']}{_TICKETS_ENDPOINT}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {dest['token']}",
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                },
            )
        if resp.status_code in (401, 403):
            _invalidate_cache()
        resp.raise_for_status()
        data = resp.json()
        # "ID" confirmed as the field name from the real ITSM POST response
        itsm_id = str(data.get("ID") or "")
        logger.info(
            "[ITSM] Ticket created successfully. ITSM ID: %s  iflow: %s",
            itsm_id, ticket.get("iflow_id"),
        )
        return itsm_id or None

    except Exception as exc:
        logger.error("[ITSM] create_itsm_ticket failed for iflow=%s: %s", ticket.get("iflow_id"), exc)
        return None


async def get_itsm_ticket(itsm_ticket_id: str) -> Optional[Dict]:
    """
    GET /odata/v4/ticket/Tickets/{id}
    Used by the poller to detect when a ticket has been resolved.
    """
    dest = await _resolve_itsm_destination()
    if not dest:
        return None
    try:
        url = f"{dest['base_url']}{_TICKETS_ENDPOINT}/{itsm_ticket_id}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {dest['token']}",
                    "Accept":        "application/json",
                },
            )
        if resp.status_code in (401, 403):
            _invalidate_cache()
        if resp.status_code == 404:
            logger.warning("[ITSM] Ticket ID=%s not found in ITSM (404) — may be stale or demo data", itsm_ticket_id)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("[ITSM] get_itsm_ticket failed for ID=%s: %s", itsm_ticket_id, exc)
        return None


