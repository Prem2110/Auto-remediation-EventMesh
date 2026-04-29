"""
cpi_monitor/cpi_poller.py
=========================
Polls SAP CPI MessageProcessingLogs OData API for FAILED messages.

Uses the existing SAP_HUB_TENANT_URL / SAP_HUB_* credentials from .env
(same credentials already used by observer_agent).  No extra SAP Destination
is required for CPI polling.

get_destination_service_creds() is kept here as a shared utility for
error_publisher.py, which uses it to resolve the EventMesh destination.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_POLL_INTERVAL = int(os.getenv("CPI_POLL_INTERVAL_SECONDS", "600"))

# CPI credentials — prefer SAP_HUB_* (plain base URL, no /api/v1 suffix).
# API_BASE_URL includes /api/v1, so strip it to keep URL construction uniform.
_raw_base          = os.getenv("SAP_HUB_TENANT_URL", "") or os.getenv("API_BASE_URL", "")
_API_BASE_URL      = _raw_base.removesuffix("/api/v1").rstrip("/")
_API_TOKEN_URL     = os.getenv("SAP_HUB_TOKEN_URL", "")     or os.getenv("API_OAUTH_TOKEN_URL", "")
_API_CLIENT_ID     = os.getenv("SAP_HUB_CLIENT_ID", "")     or os.getenv("API_OAUTH_CLIENT_ID", "")
_API_CLIENT_SECRET = os.getenv("SAP_HUB_CLIENT_SECRET", "") or os.getenv("API_OAUTH_CLIENT_SECRET", "")

# In-process OAuth token cache
_direct_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


async def _fetch_direct_token() -> str:
    """Return a cached OAuth2 client-credentials token for the CPI API."""
    now = time.monotonic()
    if _direct_token_cache["token"] and now < _direct_token_cache["expires_at"] - 60:
        return _direct_token_cache["token"]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _API_TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     _API_CLIENT_ID,
                "client_secret": _API_CLIENT_SECRET,
            },
        )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    _direct_token_cache["token"]      = token
    _direct_token_cache["expires_at"] = now + int(body.get("expires_in", 3600))
    return token


def get_destination_service_creds() -> Optional[Dict[str, Any]]:
    """
    Parse VCAP_SERVICES for the SAP Destination service credential block.
    Used by error_publisher.py to resolve the EventMesh destination.
    Returns the credentials dict or None if the binding is absent.
    """
    vcap_raw = os.getenv("VCAP_SERVICES", "")
    if not vcap_raw:
        return None
    try:
        vcap = json.loads(vcap_raw)
        for key in ("destination", "destination-lite"):
            services = vcap.get(key, [])
            if services:
                return services[0].get("credentials", {})
    except Exception:
        pass
    return None


async def get_cpi_client() -> Optional[Tuple[str, str]]:
    """
    Return (base_url, bearer_token) for the CPI API.
    Uses SAP_HUB_TENANT_URL + SAP_HUB_* credentials directly from .env.
    Returns None if credentials are not configured.
    """
    if not (_API_BASE_URL and _API_TOKEN_URL and _API_CLIENT_ID and _API_CLIENT_SECRET):
        return None
    token = await _fetch_direct_token()
    return _API_BASE_URL, token


async def _fetch_failed_messages(base_url: str, bearer_token: str) -> List[Dict[str, Any]]:
    """Issue the OData query and return the list of FAILED message records."""
    from_time = (
        datetime.now(timezone.utc) - timedelta(seconds=_POLL_INTERVAL)
    ).strftime("%Y-%m-%dT%H:%M:%S")

    params = {
        "$select":  "MessageGuid,Status,LogEnd,IntegrationFlowName",
        "$top":     "10",
        "$filter":  (
            f"Status eq 'FAILED' "
            f"and LogEnd ge datetime'{from_time}' "
            f"and IntegrationFlowName ne 'FailedLogs_capturing_EM_Topic_Based'"
        ),
        "$orderby": "LogEnd desc",
        "$format":  "json",
    }
    url = f"{base_url.rstrip('/')}/api/v1/MessageProcessingLogs"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept":        "application/json",
            },
        )
    resp.raise_for_status()
    return resp.json().get("d", {}).get("results", [])


async def poll_failed_messages() -> List[Dict[str, Any]]:
    """
    Poll CPI for FAILED messages within the last poll interval.
    Returns an empty list on any error so callers are never broken.
    """
    try:
        client = await get_cpi_client()
        if client is None:
            logger.warning("[CPI_MONITOR] No CPI credentials configured — skipping poll")
            return []
        base_url, token = client
        return await _fetch_failed_messages(base_url, token)
    except Exception as exc:
        logger.error("[CPI_MONITOR] poll_failed_messages error: %s", exc)
        return []
