"""
calm/exceptions_api.py
======================
Client for SAP Cloud ALM Integration & Exception Monitoring API.
Endpoint: GET /api/calm-intm/v1/exceptions

Handles:
  - Cursor-based pagination ($top / $skip)
  - Retry with exponential backoff on 429 / 5xx
  - Managed object filtering (CALM_MANAGED_OBJECT_ID)
  - Both OData $filter and query-param filter styles (CALM_FILTER_STYLE)
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import httpx

from calm.auth import calm_headers, get_calm_base_url

logger = logging.getLogger(__name__)

_MANAGED_OBJECT_ID = os.getenv("CALM_MANAGED_OBJECT_ID", "")
_EXCEPTIONS_PATH   = os.getenv("CALM_EXCEPTIONS_PATH", "/calm-intm/v1/exceptions")
_FILTER_STYLE      = os.getenv("CALM_FILTER_STYLE", "odata")   # "odata" or "params"
_PAGE_SIZE         = int(os.getenv("CALM_PAGE_SIZE", "100"))
_MAX_RETRIES       = 3
_BACKOFF_BASE      = 2.0


async def _get_with_retry(url: str, params: dict) -> dict:
    headers  = await calm_headers()
    last_exc: Optional[Exception] = None

    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params, headers=headers)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 401:
                # Token expired mid-flight — invalidate cache and refresh
                from calm.auth import _calm_cache
                _calm_cache.update({"token": None, "base_url": None, "expires_at": 0.0})
                headers = await calm_headers()
                continue

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", _BACKOFF_BASE ** (attempt + 2)))
                logger.warning("[CALM] Rate-limited (429); waiting %.0fs before retry", wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = _BACKOFF_BASE ** (attempt + 1)
                logger.warning("[CALM] Server error %d; retry in %.0fs — %s", resp.status_code, wait, resp.text[:200])
                await asyncio.sleep(wait)
                continue

            logger.error("[CALM] Exception API returned %d: %s", resp.status_code, resp.text[:400])
            resp.raise_for_status()

        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning("[CALM] Timeout on attempt %d/%d: %s", attempt + 1, _MAX_RETRIES, exc)
            await asyncio.sleep(_BACKOFF_BASE ** attempt)

    raise RuntimeError(f"[CALM] Exception API unreachable after {_MAX_RETRIES} retries") from last_exc


async def fetch_failed_exceptions_since(since: datetime) -> AsyncIterator["CALMException"]:
    """
    Async generator yielding FAILED CALMException objects created after `since`.
    Pages automatically. Stops when a page returns fewer items than PAGE_SIZE.

    Pull mode note: CALM's pull job may not have collected exceptions yet.
    Set CALM_LOOKBACK_MINUTES >= your CALM pull interval + buffer.
    """
    from calm.models import CALMException  # late import avoids circular

    since_str = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    skip      = 0
    base_url  = await get_calm_base_url()
    url       = f"{base_url}{_EXCEPTIONS_PATH}"

    while True:
        params: dict = {
            "$top":     _PAGE_SIZE,
            "$skip":    skip,
            "$orderby": "startTime asc",
        }

        if _FILTER_STYLE == "odata":
            filt = f"status eq 'FAILED' and startTime ge {since_str}"
            if _MANAGED_OBJECT_ID:
                filt += f" and managedObjectId eq '{_MANAGED_OBJECT_ID}'"
            params["$filter"] = filt
        else:
            # Older CALM releases use query parameters
            params["status"]          = "FAILED"
            params["startTime[gte]"]  = since_str
            if _MANAGED_OBJECT_ID:
                params["managedObjectId"] = _MANAGED_OBJECT_ID

        body  = await _get_with_retry(url, params)
        items = body.get("value", body.get("results", []))

        if not items:
            logger.debug("[CALM] No exceptions since %s (skip=%d)", since_str, skip)
            break

        for raw in items:
            try:
                yield CALMException.model_validate(raw)
            except Exception as exc:
                logger.warning(
                    "[CALM] Failed to parse exception id=%s: %s | raw=%s",
                    raw.get("id", "?"), exc, str(raw)[:300],
                )

        if len(items) < _PAGE_SIZE:
            break
        skip += _PAGE_SIZE
        await asyncio.sleep(0.1)  # yield control; don't hammer the CALM API


async def fetch_exception_by_id(exception_id: str) -> "Optional[CALMException]":
    """Fetch a single exception by ID. Used by the webhook handler."""
    from calm.models import CALMException

    try:
        base_url = await get_calm_base_url()
        url      = f"{base_url}{_EXCEPTIONS_PATH}/{exception_id}"
        headers  = await calm_headers()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            logger.warning("[CALM] Exception %s not found", exception_id)
            return None
        resp.raise_for_status()
        return CALMException.model_validate(resp.json())
    except Exception as exc:
        logger.error("[CALM] fetch_exception_by_id %s failed: %s", exception_id, exc)
        return None