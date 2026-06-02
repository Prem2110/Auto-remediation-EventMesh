"""
scripts/discover_calm_managed_objects.py
========================================
One-time discovery script: finds the CALM internal managed object ID for CPI_SBX
and prints all available exception field names.

Run BEFORE deploying the CALM integration:
    python scripts/discover_calm_managed_objects.py

What it does:
  1. Fetches OAuth2 token using CALM_TOKEN_URL + CALM_CLIENT_ID + CALM_CLIENT_SECRET
  2. Tries known CALM API endpoints to find managed objects / services
  3. Fetches a sample exception to print available field names
  4. Tells you exactly what to set in .env / manifest.yaml

Requirements: Set in .env before running:
    CALM_API_BASE_URL=https://YOUR-SUBDOMAIN.calm.sap.com
    CALM_TOKEN_URL=https://YOUR-SUBDOMAIN.authentication.us10.hana.ondemand.com/oauth/token
    CALM_CLIENT_ID=<from BTP service key>
    CALM_CLIENT_SECRET=<from BTP service key>
"""

import asyncio
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

_TOKEN_URL    = os.getenv("CALM_TOKEN_URL", "")
_CLIENT_ID    = os.getenv("CALM_CLIENT_ID", "")
_CLIENT_SEC   = os.getenv("CALM_CLIENT_SECRET", "")
_API_BASE     = os.getenv("CALM_API_BASE_URL", "").rstrip("/")

SEP = "─" * 70


async def get_token() -> str:
    if not all([_TOKEN_URL, _CLIENT_ID, _CLIENT_SEC]):
        print("ERROR: Missing CALM credentials in .env")
        print("  CALM_TOKEN_URL=?")
        print("  CALM_CLIENT_ID=?")
        print("  CALM_CLIENT_SECRET=?")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": _CLIENT_ID, "client_secret": _CLIENT_SEC},
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        print(f"ERROR: Token fetch failed HTTP {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    print(f"✓ Token acquired from {_TOKEN_URL}")
    return resp.json()["access_token"]


async def try_endpoint(token: str, path: str, params: dict = None) -> tuple[int, dict | list | None]:
    url = f"{_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params or {}, headers=headers)
    if resp.status_code == 200:
        return resp.status_code, resp.json()
    return resp.status_code, None


async def main() -> None:
    if not _API_BASE:
        print("ERROR: CALM_API_BASE_URL not set in .env")
        sys.exit(1)

    print(SEP)
    print("SAP Cloud ALM Discovery Script")
    print(f"API Base: {_API_BASE}")
    print(SEP)

    token = await get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ── Step 1: Find managed objects ─────────────────────────────────────────
    print("\n[1] Searching for managed objects / integration services...")
    endpoints_to_try = [
        "/api/calm-intm/v1/services",
        "/api/calm-intm/v1/managedObjects",
        "/api/calm-intm/v1/integrationSuites",
        "/api/calm-intm/v1/integrationFlows",
    ]
    found_mo = False
    for path in endpoints_to_try:
        status, data = await try_endpoint(token, path, {"$top": "50"})
        print(f"  GET {path} → HTTP {status}")
        if status == 200 and data:
            items = data.get("value", data.get("results", data if isinstance(data, list) else []))
            print(f"  Found {len(items)} item(s):")
            for item in items[:20]:
                mo_id   = item.get("id") or item.get("managedObjectId") or item.get("serviceId") or ""
                mo_name = item.get("name") or item.get("displayName") or item.get("technicalName") or ""
                print(f"    ID={mo_id!r:50s}  name={mo_name!r}")
                if "CPI" in mo_name.upper() or "CPI" in mo_id.upper() or "INTEGRATION" in mo_name.upper():
                    print(f"    *** ADD TO .env: CALM_MANAGED_OBJECT_ID={mo_id}")
                    found_mo = True
            if not found_mo:
                print("  No CPI_SBX match in this endpoint — trying next...")
            break

    # ── Step 2: Fetch sample exception ───────────────────────────────────────
    print(f"\n{SEP}")
    print("[2] Fetching a sample exception to discover field names...")
    exc_endpoints = [
        "/api/calm-intm/v1/exceptions",
    ]
    for path in exc_endpoints:
        status, data = await try_endpoint(token, path, {"$top": "1", "status": "FAILED"})
        print(f"  GET {path}?$top=1&status=FAILED → HTTP {status}")
        if status == 200 and data:
            items = data.get("value", data.get("results", []))
            if items:
                print("  Sample exception fields:")
                for key, val in items[0].items():
                    print(f"    {key:35s}: {str(val)!r[:80]}")

                # Check if messageId is present (critical for CPI correlation)
                sample = items[0]
                print(f"\n  *** FIELD MAPPING FINDINGS ***")
                print(f"  messageId present: {'messageId' in sample} (maps to CPI MessageGuid)")
                print(f"  managedObjectId present: {'managedObjectId' in sample}")
                print(f"  technicalName present: {'technicalName' in sample} (should be iFlow name)")
                print(f"  errorCategory present: {'errorCategory' in sample}")
            else:
                print("  No FAILED exceptions found. Trigger a CPI iFlow failure first, then re-run.")

    # ── Step 3: Test alert endpoint ───────────────────────────────────────────
    print(f"\n{SEP}")
    print("[3] Testing CALM Alert Management API...")
    status, data = await try_endpoint(token, "/api/calm-alert/v1/alerts", {"$top": "1"})
    print(f"  GET /api/calm-alert/v1/alerts → HTTP {status}")
    if status == 200:
        print("  ✓ Alert API accessible")
        items = (data or {}).get("value", [])
        if items:
            print(f"  Sample alert fields: {list(items[0].keys())}")
    else:
        print("  ✗ Alert API not accessible — check CALM service key scopes")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("SUMMARY — add to manifest.yaml env: block:")
    print(f"  CALM_API_BASE_URL: {_API_BASE}")
    print(f"  CALM_MANAGED_OBJECT_ID: <ID from Step 1 above>")
    print(f"  CALM_FILTER_STYLE: odata   # or 'params' if $filter returned 400")
    print(f"  CALM_POLL_INTERVAL_SECONDS: '120'")
    print(f"  CALM_LOOKBACK_MINUTES: '15'")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())