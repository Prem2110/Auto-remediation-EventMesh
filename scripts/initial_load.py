"""
scripts/initial_load.py
========================
First-deployment backfill: fetches ALL failed CPI messages (no polling
window restriction) and publishes each to the SAP Event Mesh ingest topic
so the 5-stage agent pipeline processes them.

Run ONCE immediately after the first deployment, then let the normal CPI
Monitor polling loop handle new failures going forward.

By default the script fetches ALL failed messages with NO time restriction.

On SAP BTP Cloud Foundry run as a one-off task:
    cf run-task orbit-<client>-be "uv run python scripts/initial_load.py" --name initial-load

Or locally (with .env):
    uv run python scripts/initial_load.py                        # all errors, all time
    uv run python scripts/initial_load.py --dry-run              # preview, no publish
    uv run python scripts/initial_load.py --limit 50             # smoke-test: first 50 only
    uv run python scripts/initial_load.py --days-back 30         # optional: narrow to 30 days

Flags:
    (none)          Fetch every FAILED message in the CPI tenant — no date filter (default)
    --days-back N   Narrow to the last N days (optional, not needed for a proper initial load)
    --limit N       Stop after N messages (smoke-test only)
    --dry-run       List what would be published; send nothing to Event Mesh
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

# ── Load .env when running locally ───────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # On CF env vars are injected by the platform

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("initial_load")

# ── CPI credentials ───────────────────────────────────────────────────────────
_raw_base          = os.getenv("SAP_HUB_TENANT_URL", "") or os.getenv("API_BASE_URL", "")
_API_BASE_URL      = _raw_base.removesuffix("/api/v1").rstrip("/")
_API_TOKEN_URL     = os.getenv("SAP_HUB_TOKEN_URL", "")     or os.getenv("API_OAUTH_TOKEN_URL", "")
_API_CLIENT_ID     = os.getenv("SAP_HUB_CLIENT_ID", "")     or os.getenv("API_OAUTH_CLIENT_ID", "")
_API_CLIENT_SECRET = os.getenv("SAP_HUB_CLIENT_SECRET", "") or os.getenv("API_OAUTH_CLIENT_SECRET", "")

# ── Event Mesh config ─────────────────────────────────────────────────────────
_EM_REST_URL         = os.getenv("EM_REST_URL", "")
_EM_INGEST_TOPIC     = os.getenv("EM_INGEST_TOPIC", "")
_EM_DESTINATION_NAME = os.getenv("EVENT_MESH_DESTINATION_NAME", "EventMesh")

# Direct OAuth fallback (local dev when no VCAP_SERVICES is available)
_EM_TOKEN_URL     = os.getenv("EVENT_MESH_TOKEN_URL", "")
_EM_CLIENT_ID     = os.getenv("EVENT_MESH_CLIENT_ID", "")
_EM_CLIENT_SECRET = os.getenv("EVENT_MESH_CLIENT_SECRET", "")

PAGE_SIZE = 100  # CPI OData page size

# ── Token caches ──────────────────────────────────────────────────────────────
_cpi_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}
_em_cache:  Dict[str, Any] = {"token": None, "expires_at": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _cpi_token() -> str:
    now = time.monotonic()
    if _cpi_cache["token"] and now < _cpi_cache["expires_at"] - 60:
        return _cpi_cache["token"]
    async with httpx.AsyncClient(timeout=15) as c:
        resp = await c.post(_API_TOKEN_URL, data={
            "grant_type":    "client_credentials",
            "client_id":     _API_CLIENT_ID,
            "client_secret": _API_CLIENT_SECRET,
        })
    resp.raise_for_status()
    body = resp.json()
    _cpi_cache["token"]      = body["access_token"]
    _cpi_cache["expires_at"] = now + int(body.get("expires_in", 3600))
    logger.debug("CPI token refreshed")
    return _cpi_cache["token"]


async def _em_token_via_destination() -> str:
    """Resolve EventMesh bearer token via SAP BTP Destination service (VCAP_SERVICES)."""
    vcap_raw = os.getenv("VCAP_SERVICES", "")
    if not vcap_raw:
        raise RuntimeError("VCAP_SERVICES not set")
    vcap  = json.loads(vcap_raw)
    creds = None
    for key in ("destination", "destination-lite"):
        services = vcap.get(key, [])
        if services:
            creds = services[0].get("credentials", {})
            break
    if not creds:
        raise RuntimeError("Destination service binding not found in VCAP_SERVICES")

    dest_uri   = creds.get("uri", "")
    token_url  = creds.get("url", "").rstrip("/") + "/oauth/token"
    client_id  = creds.get("clientid", "")
    client_sec = creds.get("clientsecret", "")

    async with httpx.AsyncClient(timeout=15) as c:
        resp = await c.post(token_url, data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_sec,
        })
    resp.raise_for_status()
    dest_token = resp.json()["access_token"]

    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.get(
            f"{dest_uri.rstrip('/')}/destination-configuration/v1/destinations/{_EM_DESTINATION_NAME}",
            headers={"Authorization": f"Bearer {dest_token}"},
        )
    resp.raise_for_status()
    auth_tokens = resp.json().get("authTokens", [])
    if not auth_tokens:
        raise RuntimeError(f"Destination '{_EM_DESTINATION_NAME}' returned no authTokens")

    em_token   = auth_tokens[0]["value"]
    expires_in = int(auth_tokens[0].get("expires_in", 3600))
    return em_token, expires_in


async def _em_token_via_oauth() -> tuple[str, int]:
    """Resolve EventMesh bearer token directly via OAuth2 (local dev fallback)."""
    if not (_EM_TOKEN_URL and _EM_CLIENT_ID and _EM_CLIENT_SECRET):
        raise RuntimeError(
            "Neither VCAP_SERVICES nor EVENT_MESH_TOKEN_URL/CLIENT_ID/CLIENT_SECRET are set"
        )
    async with httpx.AsyncClient(timeout=15) as c:
        resp = await c.post(_EM_TOKEN_URL, data={
            "grant_type":    "client_credentials",
            "client_id":     _EM_CLIENT_ID,
            "client_secret": _EM_CLIENT_SECRET,
        })
    resp.raise_for_status()
    body = resp.json()
    return body["access_token"], int(body.get("expires_in", 3600))


async def _em_token() -> str:
    now = time.monotonic()
    if _em_cache["token"] and now < _em_cache["expires_at"] - 60:
        return _em_cache["token"]

    try:
        token, expires_in = await _em_token_via_destination()
        logger.info("EventMesh token resolved via Destination service '%s'", _EM_DESTINATION_NAME)
    except Exception as dest_exc:
        logger.warning("Destination service unavailable (%s) — trying direct OAuth", dest_exc)
        try:
            token, expires_in = await _em_token_via_oauth()
            logger.info("EventMesh token resolved via direct OAuth (EVENT_MESH_CLIENT_ID)")
        except Exception as oauth_exc:
            raise RuntimeError(
                f"Cannot obtain EventMesh token.\n"
                f"  Destination error: {dest_exc}\n"
                f"  OAuth error:       {oauth_exc}"
            ) from oauth_exc

    _em_cache["token"]      = token
    _em_cache["expires_at"] = now + expires_in
    return token


# ─────────────────────────────────────────────────────────────────────────────
# CPI data fetch
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_page(
    cpi_token: str,
    skip: int,
    days_back: Optional[int],
) -> List[Dict[str, Any]]:
    params: Dict[str, str] = {
        "$select":  "MessageGuid,Status,LogEnd,IntegrationFlowName",
        "$top":     str(PAGE_SIZE),
        "$skip":    str(skip),
        "$orderby": "LogEnd desc",
        "$format":  "json",
    }
    if days_back is not None:
        from_dt = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S")
        params["$filter"] = f"Status eq 'FAILED' and LogEnd ge datetime'{from_dt}'"
    else:
        params["$filter"] = "Status eq 'FAILED'"

    url = f"{_API_BASE_URL}/api/v1/MessageProcessingLogs"
    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.get(url, params=params, headers={
            "Authorization": f"Bearer {cpi_token}",
            "Accept":        "application/json",
        })
    resp.raise_for_status()
    return resp.json().get("d", {}).get("results", [])


async def _fetch_all_failed(days_back: Optional[int]) -> List[Dict[str, Any]]:
    token    = await _cpi_token()
    all_msgs: List[Dict[str, Any]] = []
    skip = 0
    while True:
        page = await _fetch_page(token, skip, days_back)
        if not page:
            break
        all_msgs.extend(page)
        logger.info(
            "  Page skip=%-5d  got %d  |  running total: %d",
            skip, len(page), len(all_msgs),
        )
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return all_msgs


async def _fetch_error_detail(guid: str, cpi_token: str) -> str:
    url = f"{_API_BASE_URL}/api/v1/MessageProcessingLogs('{guid}')/ErrorInformation/$value"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(url, headers={
                "Authorization": f"Bearer {cpi_token}",
                "Accept":        "text/plain",
            })
        if resp.status_code == 200:
            return _clean_error(resp.text.strip())
    except Exception as exc:
        logger.warning("ErrorInformation fetch failed  guid=%s: %s", guid, exc)
    return ""


def _clean_error(raw: str) -> str:
    """Strip SAP CPI noise from error text (MPL IDs, stack traces, Java prefixes)."""
    if not raw:
        return ""
    text = re.sub(r'\nThe MPL ID for the failed message is\s*:\s*\S+', '', raw)
    text = re.sub(r'\n\tat .*', '', text)
    text = re.sub(r'\n\t\.\.\..*', '', text)
    text = re.sub(r'com\.sap\.[a-z.]+\.([A-Z][a-zA-Z]+Exception)', r'\1', text)
    text = re.sub(r'com\.sap\.[a-z.]+\.([A-Z][a-zA-Z]+Error)',     r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()[:600]


# ─────────────────────────────────────────────────────────────────────────────
# Event Mesh publish
# ─────────────────────────────────────────────────────────────────────────────

async def _publish(payload: Dict[str, Any], em_token: str) -> None:
    encoded = quote(_EM_INGEST_TOPIC, safe="")
    url     = f"{_EM_REST_URL.rstrip('/')}/messagingrest/v1/topics/{encoded}/messages"
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.post(url, content=json.dumps(payload), headers={
            "Authorization": f"Bearer {em_token}",
            "Content-Type":  "application/json",
            "x-qos":         "1",
        })
    if resp.status_code not in (200, 202, 204):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run(days_back: Optional[int], limit: Optional[int], dry_run: bool) -> None:
    # ── Validate required config ──────────────────────────────────────────────
    missing_cpi = [name for name, val in {
        "SAP_HUB_TENANT_URL (or API_BASE_URL)": _API_BASE_URL,
        "SAP_HUB_TOKEN_URL":                    _API_TOKEN_URL,
        "SAP_HUB_CLIENT_ID":                    _API_CLIENT_ID,
        "SAP_HUB_CLIENT_SECRET":                _API_CLIENT_SECRET,
    }.items() if not val]
    if missing_cpi:
        logger.error("Missing CPI env vars: %s", ", ".join(missing_cpi))
        sys.exit(1)

    if not dry_run:
        if not _EM_REST_URL:
            logger.error("EM_REST_URL is not set. Use --dry-run to preview without publishing.")
            sys.exit(1)
        if not _EM_INGEST_TOPIC:
            logger.error("EM_INGEST_TOPIC is not set. Use --dry-run to preview without publishing.")
            sys.exit(1)

    # ── Fetch ─────────────────────────────────────────────────────────────────
    range_label = f"last {days_back} days" if days_back is not None else "ALL TIME — no date filter"
    logger.info("=" * 60)
    logger.info("Orbit initial load — fetching ALL failed CPI messages (%s)", range_label)
    logger.info("=" * 60)

    messages = await _fetch_all_failed(days_back)

    if limit is not None:
        messages = messages[:limit]
        logger.info("--limit applied: processing first %d messages", len(messages))

    logger.info("Total messages to process: %d", len(messages))

    if not messages:
        logger.info("No failed messages found. Nothing to publish.")
        return

    # ── Dry run ───────────────────────────────────────────────────────────────
    if dry_run:
        logger.info("[DRY RUN] Would publish %d messages to topic: %s", len(messages), _EM_INGEST_TOPIC)
        logger.info("-" * 60)
        for m in messages:
            logger.info(
                "  %-38s  iflow=%-40s  LogEnd=%s",
                m.get("MessageGuid", ""),
                m.get("IntegrationFlowName", ""),
                m.get("LogEnd", ""),
            )
        logger.info("-" * 60)
        logger.info("[DRY RUN] No messages were published.")
        return

    # ── Resolve Event Mesh token ──────────────────────────────────────────────
    try:
        em_token = await _em_token()
    except Exception as exc:
        logger.error("Failed to obtain Event Mesh token: %s", exc)
        sys.exit(1)

    # Re-get CPI token (for error detail calls)
    cpi_token = await _cpi_token()

    # ── Publish loop ──────────────────────────────────────────────────────────
    published = 0
    skipped   = 0
    failed    = 0

    logger.info("Publishing to topic: %s", _EM_INGEST_TOPIC)
    logger.info("-" * 60)

    for i, msg in enumerate(messages, 1):
        guid  = msg.get("MessageGuid", "")
        iflow = msg.get("IntegrationFlowName", "unknown")

        if not guid:
            skipped += 1
            continue

        try:
            error_text = await _fetch_error_detail(guid, cpi_token)
            payload: Dict[str, Any] = {
                "IflowId":             iflow,
                "MessageGuid":         guid,
                "IntegrationFlowName": iflow,
                "Status":              "FAILED",
                "LogEnd":              msg.get("LogEnd", ""),
                "ErrorMessage":        error_text,
            }
            await _publish(payload, em_token)
            published += 1
            logger.info("[%d/%d] ✓  guid=%-36s  iflow=%s", i, len(messages), guid, iflow)

        except Exception as exc:
            failed += 1
            logger.error("[%d/%d] ✗  guid=%-36s  iflow=%-30s  error=%s", i, len(messages), guid, iflow, exc)

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(
        "Done.  published=%d  failed=%d  skipped=%d  total=%d",
        published, failed, skipped, len(messages),
    )
    if failed:
        logger.warning("%d messages failed to publish — check logs above and re-run if needed.", failed)
    logger.info("The pipeline will now process each published message autonomously.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orbit initial load — publish all existing CPI failures to Event Mesh",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Publish ALL failed messages — no time restriction (recommended for first deployment)
  uv run python scripts/initial_load.py

  # Preview what would be published without sending anything
  uv run python scripts/initial_load.py --dry-run

  # Smoke-test: publish only the first 50 messages
  uv run python scripts/initial_load.py --limit 50

  # Optional: narrow to last 30 days
  uv run python scripts/initial_load.py --days-back 30

  # On Cloud Foundry (one-off CF task — recommended)
  cf run-task orbit-<client>-be "uv run python scripts/initial_load.py" --name initial-load
        """,
    )
    parser.add_argument(
        "--days-back", type=int, default=None, metavar="N",
        help="Only include failures from the last N days (default: all time)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Stop after processing N messages (useful for smoke-testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be published without sending anything to Event Mesh",
    )
    args = parser.parse_args()
    asyncio.run(run(days_back=args.days_back, limit=args.limit, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
