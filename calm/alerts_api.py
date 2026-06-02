"""
calm/alerts_api.py
==================
SAP Cloud ALM Alert Management API client.
Used to:
  1. Fetch alert details when webhook arrives (get exception IDs)
  2. PATCH alert status to RESOLVED/IN_PROGRESS after auto-remediation
  3. Add remediation comments to the alert audit trail
"""

import logging
from typing import Optional

import httpx

from calm.auth import calm_headers, get_calm_base_url
from calm.models import CALMAlert, CALMAlertStatus

logger = logging.getLogger(__name__)

_ALERTS_PATH = "/calm-alert/v1/alerts"


async def get_alert(alert_id: str) -> Optional[CALMAlert]:
    """Fetch a single alert by ID. Returns None if not found."""
    try:
        base_url = await get_calm_base_url()
        url      = f"{base_url}{_ALERTS_PATH}/{alert_id}"
        headers  = await calm_headers()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            logger.warning("[CALM] Alert %s not found", alert_id)
            return None
        resp.raise_for_status()
        return CALMAlert.model_validate(resp.json())
    except Exception as exc:
        logger.error("[CALM] get_alert %s failed: %s", alert_id, exc)
        return None


async def patch_alert_status(
    alert_id:    str,
    status:      Optional[CALMAlertStatus] = None,
    comment:     Optional[str]             = None,
    assigned_to: Optional[str]             = None,
) -> bool:
    """
    Update alert status, add comment, or reassign.
    Idempotent: patching RESOLVED twice is safe.
    Returns True on success, False on failure.
    """
    from calm.models import CALMAlertPatch

    patch = CALMAlertPatch(status=status, comment=comment, assignedTo=assigned_to)
    payload = patch.model_dump(exclude_none=True)
    if not payload:
        return True  # nothing to update

    try:
        base_url = await get_calm_base_url()
        url      = f"{base_url}{_ALERTS_PATH}/{alert_id}"
        headers  = await calm_headers()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(url, json=payload, headers=headers)

        if resp.status_code in (200, 204):
            logger.info("[CALM] Alert %s patched → %s", alert_id, payload)
            return True

        logger.error(
            "[CALM] Alert PATCH failed for %s: HTTP %d — %s",
            alert_id, resp.status_code, resp.text[:300],
        )
        return False

    except Exception as exc:
        logger.error("[CALM] patch_alert_status %s failed: %s", alert_id, exc)
        return False


async def add_alert_comment(alert_id: str, text: str) -> bool:
    """Post a comment to a CALM alert for audit trail."""
    try:
        base_url = await get_calm_base_url()
        url      = f"{base_url}{_ALERTS_PATH}/{alert_id}/comments"
        headers  = await calm_headers()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json={"text": text[:2000]}, headers=headers)
        if resp.status_code in (200, 201):
            return True
        logger.warning("[CALM] Comment on alert %s failed: HTTP %d", alert_id, resp.status_code)
        return False
    except Exception as exc:
        logger.error("[CALM] add_alert_comment %s failed: %s", alert_id, exc)
        return False