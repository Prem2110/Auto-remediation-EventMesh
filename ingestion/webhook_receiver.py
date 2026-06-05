"""
ingestion/webhook_receiver.py
==============================
FastAPI router: POST /calm/webhook

Receives push notifications from SAP Cloud ALM Action Plans.
Returns 202 immediately; processes the alert in a background task.

Security: validates X-CALM-Signature header using HMAC-SHA256 if
CALM_WEBHOOK_SECRET is set (strongly recommended in production).

Mount in main.py:
    from ingestion.webhook_receiver import calm_webhook_router
    app.include_router(calm_webhook_router, prefix="/calm")
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from calm.models import CALMEventSituationPayload, CALMWebhookPayload

logger = logging.getLogger(__name__)

calm_webhook_router = APIRouter()

_WEBHOOK_SECRET = os.getenv("CALM_WEBHOOK_SECRET", "")


def _verify_signature(body: bytes, sig_header: str) -> bool:
    """
    Verify X-CALM-Signature: sha256=<hex>.
    If CALM_WEBHOOK_SECRET is not set, skip verification (dev mode).
    In production, CALM_WEBHOOK_SECRET MUST be set.
    """
    if not _WEBHOOK_SECRET:
        logger.warning(
            "[CALM_WEBHOOK] CALM_WEBHOOK_SECRET not set — signature check SKIPPED. "
            "Set this env var in production."
        )
        return True
    try:
        expected = "sha256=" + hmac.new(
            _WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, sig_header or "")
    except Exception:
        return False


async def _process_webhook_background(
    payload:     CALMWebhookPayload,
    orchestrator,
) -> None:
    """
    Background task: resolve exception IDs from the webhook payload,
    fetch each exception from CALM API, and feed into the agent pipeline.
    """
    from calm.alerts_api import get_alert
    from calm.exceptions_api import fetch_exception_by_id
    from calm.incident_adapter import calm_exception_to_normalized
    from ingestion.dedup import filter_new, mark_seen

    exception_ids = list(payload.exceptionIds)

    # If CALM didn't include exception IDs in the payload, fetch them from the alert
    if not exception_ids and payload.alertId:
        alert = await get_alert(payload.alertId)
        if alert and alert.exceptionIds:
            exception_ids = list(alert.exceptionIds)

    if not exception_ids:
        logger.warning(
            "[CALM_WEBHOOK] Alert %s has no exception IDs — nothing to process",
            payload.alertId,
        )
        return

    new_ids = filter_new(exception_ids)
    if not new_ids:
        logger.debug(
            "[CALM_WEBHOOK] All %d exception(s) already seen for alert %s",
            len(exception_ids), payload.alertId,
        )
        return

    logger.info(
        "[CALM_WEBHOOK] Processing %d new exception(s) from alert %s (event=%s)",
        len(new_ids), payload.alertId, payload.eventType,
    )

    for exc_id in new_ids:
        mark_seen(exc_id)  # mark before fetching to prevent races with poller
        try:
            calm_exc = await fetch_exception_by_id(exc_id)
            if not calm_exc:
                logger.warning("[CALM_WEBHOOK] Exception %s not found in CALM API", exc_id)
                continue

            if orchestrator is None:
                logger.warning("[CALM_WEBHOOK] Orchestrator not available for exception %s", exc_id)
                continue

            normalized = calm_exception_to_normalized(calm_exc)
            result     = await orchestrator.process_detected_error(normalized)
            logger.info(
                "[CALM_WEBHOOK] Exception %s → iflow=%s result=%s",
                exc_id, normalized.get("iflow_id", "?"), result,
            )

        except Exception as exc:
            logger.error("[CALM_WEBHOOK] Failed to process exception %s: %s", exc_id, exc)


async def _process_event_situation_background(
    payload:     CALMEventSituationPayload,
    orchestrator,
) -> None:
    """
    Background task for EVENT-SITUATION.CREATED payloads.
    All data is in resource.body — no CALM API call needed.
    """
    from calm.incident_adapter import calm_event_situation_to_normalized
    from ingestion.dedup import filter_new, mark_seen

    resource_id = payload.resourceId
    if not resource_id:
        logger.warning("[CALM_WEBHOOK] EVENT-SITUATION payload missing resourceId — skipping")
        return

    if not filter_new([resource_id]):
        logger.debug("[CALM_WEBHOOK] resourceId %s already seen — skipping", resource_id)
        return

    mark_seen(resource_id)

    if orchestrator is None:
        logger.warning("[CALM_WEBHOOK] Orchestrator not available for resourceId %s", resource_id)
        return

    try:
        normalized = calm_event_situation_to_normalized(payload)
        result     = await orchestrator.process_detected_error(normalized)
        logger.info(
            "[CALM_WEBHOOK] EVENT-SITUATION %s → iflow=%s result=%s",
            resource_id, normalized.get("iflow_id", "?"), result,
        )
    except Exception as exc:
        logger.error("[CALM_WEBHOOK] Failed to process EVENT-SITUATION %s: %s", resource_id, exc)


@calm_webhook_router.post(
    "/webhook",
    status_code=202,
    summary="Receive SAP Cloud ALM Action Plan push notification",
    description=(
        "SAP Cloud ALM posts an HTTP notification here when an alert is created or updated. "
        "Configure the Action Plan in CALM → Operations → Alert Management → Action Plans. "
        "Set the shared secret as CALM_WEBHOOK_SECRET for signature verification."
    ),
)
async def receive_calm_webhook(
    request:          Request,
    background_tasks: BackgroundTasks,
    x_calm_signature: str = Header(default="", alias="X-CALM-Signature"),
) -> dict:
    raw_body = await request.body()

    if not _verify_signature(raw_body, x_calm_signature):
        logger.warning("[CALM_WEBHOOK] Signature verification failed — rejecting request")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        raw_data   = json.loads(raw_body)
        event_type = raw_data.get("eventType", "")
    except Exception as exc:
        logger.warning("[CALM_WEBHOOK] JSON parse failed: %s | body=%s", exc, raw_body[:200])
        raise HTTPException(status_code=422, detail="Invalid payload shape")

    orchestrator = getattr(request.app.state, "orchestrator", None)

    if event_type == "EVENT-SITUATION.CREATED":
        try:
            payload = CALMEventSituationPayload.model_validate(raw_data)
        except Exception as exc:
            logger.warning("[CALM_WEBHOOK] EVENT-SITUATION parse failed: %s", exc)
            raise HTTPException(status_code=422, detail="Invalid payload shape")
        background_tasks.add_task(_process_event_situation_background, payload, orchestrator)
        resource_id = payload.resourceId
    else:
        try:
            payload = CALMWebhookPayload.model_validate(raw_data)
        except Exception as exc:
            logger.warning("[CALM_WEBHOOK] Payload parse failed: %s | body=%s", exc, raw_body[:200])
            raise HTTPException(status_code=422, detail="Invalid payload shape")
        background_tasks.add_task(_process_webhook_background, payload, orchestrator)
        resource_id = payload.alertId

    logger.info("[CALM_WEBHOOK] Accepted eventType=%s resourceId=%s", event_type, resource_id)
    return {"status": "accepted", "alertId": resource_id}


@calm_webhook_router.get(
    "/status",
    summary="Cloud ALM integration status",
)
async def calm_status() -> dict:
    """Return CALM connectivity configuration (no live API call)."""
    from calm.auth import CALM_API_BASE_URL, _DESTINATION_NAME
    return {
        "source":              "CLOUD_ALM",
        "destination_name":    _DESTINATION_NAME,
        "api_base_url":        CALM_API_BASE_URL or "(not resolved yet)",
        "managed_object_id":   os.getenv("CALM_MANAGED_OBJECT_ID", "(all)"),
        "poll_interval_s":     int(os.getenv("CALM_POLL_INTERVAL_SECONDS", "120")),
        "lookback_minutes":    int(os.getenv("CALM_LOOKBACK_MINUTES", "15")),
        "webhook_secret_set":  bool(os.getenv("CALM_WEBHOOK_SECRET")),
        "filter_style":        os.getenv("CALM_FILTER_STYLE", "odata"),
    }