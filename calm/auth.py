"""
calm/auth.py
============
OAuth2 token manager for SAP Cloud ALM.
Uses BTP Destination service (same pattern as integrations/itsm_client.py).

The BTP Destination named CALM_DESTINATION_NAME resolves to:
  base_url — https://<subdomain>.calm.sap.com
  token    — OAuth2 client-credentials bearer token

Requires env var: CALM_DESTINATION_NAME (default: "CALM-Sierra")
The destination-service binding must already exist in manifest.yaml (it does).
"""

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DESTINATION_NAME = os.getenv("CALM_DESTINATION_NAME", "CALM-Sierra")

_calm_cache: dict = {"token": None, "base_url": None, "expires_at": 0.0}

# Module-level base URL — set after first successful destination resolution
CALM_API_BASE_URL: str = os.getenv("CALM_API_BASE_URL", "").rstrip("/")


def _get_destination_service_creds() -> Optional[dict]:
    """Parse VCAP_SERVICES for the SAP Destination service credentials."""
    import json
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


async def _resolve_calm_destination() -> Optional[dict]:
    """
    Resolve CALM-Sierra BTP Destination → {"base_url": ..., "token": ...}.
    Caches until 120s before expiry. Returns None if destination unavailable.
    """
    global CALM_API_BASE_URL

    now = time.monotonic()
    if (
        _calm_cache["token"]
        and _calm_cache["base_url"]
        and now < _calm_cache["expires_at"] - 120
    ):
        return {"base_url": _calm_cache["base_url"], "token": _calm_cache["token"]}

    # Fallback: direct env var credentials (for local dev / non-CF environments)
    direct_token_url    = os.getenv("CALM_TOKEN_URL", "")
    direct_client_id    = os.getenv("CALM_CLIENT_ID", "")
    direct_client_secret = os.getenv("CALM_CLIENT_SECRET", "")

    if direct_token_url and direct_client_id and direct_client_secret and CALM_API_BASE_URL:
        logger.info("[CALM] Using direct env var credentials (non-CF mode)")
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                direct_token_url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     direct_client_id,
                    "client_secret": direct_client_secret,
                },
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            logger.error("[CALM] Direct token fetch failed: HTTP %d — %s", resp.status_code, resp.text[:200])
            return None
        body = resp.json()
        token      = body["access_token"]
        expires_in = int(body.get("expires_in", 3600))
        _calm_cache.update({"token": token, "base_url": CALM_API_BASE_URL, "expires_at": now + expires_in})
        return {"base_url": CALM_API_BASE_URL, "token": token}

    # CF / BTP path: use Destination service
    creds = _get_destination_service_creds()
    if not creds:
        logger.warning(
            "[CALM] No Destination service in VCAP_SERVICES and no direct CALM_* env vars. "
            "Set CALM_TOKEN_URL + CALM_CLIENT_ID + CALM_CLIENT_SECRET + CALM_API_BASE_URL for local dev."
        )
        return None

    dest_uri   = creds.get("uri", "")
    token_url  = creds.get("url", "").rstrip("/") + "/oauth/token"
    client_id  = creds.get("clientid", "")
    client_sec = creds.get("clientsecret", "")

    if not (dest_uri and client_id and client_sec):
        logger.error("[CALM] Incomplete Destination service credentials in VCAP_SERVICES")
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            tok_resp = await client.post(
                token_url,
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_sec},
            )
        tok_resp.raise_for_status()
        dest_token = tok_resp.json()["access_token"]

        async with httpx.AsyncClient(timeout=10) as client:
            dest_resp = await client.get(
                f"{dest_uri.rstrip('/')}/destination-configuration/v1/destinations/{_DESTINATION_NAME}",
                headers={"Authorization": f"Bearer {dest_token}"},
            )

        if dest_resp.status_code == 404:
            logger.error(
                "[CALM] Destination '%s' not found in BTP. "
                "Create it in BTP Cockpit → Connectivity → Destinations.",
                _DESTINATION_NAME,
            )
            return None
        dest_resp.raise_for_status()
        dest_data = dest_resp.json()

        base_url    = dest_data.get("destinationConfiguration", {}).get("URL", "").rstrip("/")
        auth_tokens = dest_data.get("authTokens", [])

        if not auth_tokens or auth_tokens[0].get("error"):
            logger.error(
                "[CALM] Destination '%s' auth error: %s",
                _DESTINATION_NAME,
                auth_tokens[0].get("error") if auth_tokens else "no authTokens",
            )
            return None

        token_entry = auth_tokens[0]
        calm_token  = token_entry.get("value", "")
        expires_in  = int(token_entry.get("expires_in", 3600))

        if not calm_token:
            logger.error("[CALM] Destination '%s' returned empty token", _DESTINATION_NAME)
            return None

        CALM_API_BASE_URL = base_url
        _calm_cache.update({"token": calm_token, "base_url": base_url, "expires_at": now + expires_in})
        logger.info("[CALM] Destination '%s' resolved, base_url=%s, expires_in=%ds", _DESTINATION_NAME, base_url, expires_in)
        return {"base_url": base_url, "token": calm_token}

    except Exception as exc:
        logger.error("[CALM] Destination resolution failed: %s", exc)
        return None


async def get_calm_token() -> str:
    resolved = await _resolve_calm_destination()
    if not resolved:
        raise RuntimeError(
            f"[CALM] Cannot resolve credentials. "
            f"For CF: create BTP Destination '{_DESTINATION_NAME}'. "
            f"For local dev: set CALM_TOKEN_URL + CALM_CLIENT_ID + CALM_CLIENT_SECRET + CALM_API_BASE_URL."
        )
    return resolved["token"]


async def calm_headers() -> dict:
    return {
        "Authorization": f"Bearer {await get_calm_token()}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


async def get_calm_base_url() -> str:
    resolved = await _resolve_calm_destination()
    if not resolved:
        raise RuntimeError("[CALM] Cannot resolve base URL")
    return resolved["base_url"]