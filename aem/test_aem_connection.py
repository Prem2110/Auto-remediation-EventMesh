"""
aem/test_aem_connection.py
==========================
Standalone AEM connectivity test — run directly:

    python aem/test_aem_connection.py

Tests both endpoint ports and both credential sets:
  - Port 9443 = Solace REST Messaging (HTTPS)
  - Port 443  = Solace Web Messaging  (HTTPS on same host as wss://)

For each combination it tries:
  - POST /<topic>        → publish a test ping (expects 200/202/204)
  - GET  /QUEUE/<queue>  → consume attempt (405 is normal for Solace push model)
"""

import asyncio
import json

import httpx

# ── Config ──────────────────────────────────────────────────────────────────
BROKER_HOST = "mr-connection-e14fh18ynw3.messaging.solace.cloud"
QUEUE       = "sap.cpi.autofix.observer.out"
TEST_TOPIC  = "sap/cpi/autofix/test/ping"

ENDPOINTS = [
    {"label": "Port 9443 (REST Messaging)", "base": f"https://{BROKER_HOST}:9443/"},
    {"label": "Port 443  (Web Messaging)",  "base": f"https://{BROKER_HOST}:443/"},
]

CREDENTIALS = [
    {
        "label":    "Autofix_AI_agent",
        "username": "Autofix_AI_agent",
        "password": "Sierra@2026",
    },
    {
        "label":    "solace-cloud-client",
        "username": "solace-cloud-client",
        "password": "4gmho9emmi9entkgjqj9087ie1",
    },
]

TEST_PAYLOAD = json.dumps({
    "stage":      "test",
    "source":     "aem_connectivity_test",
    "message":    "ping from auto-remediation app",
})


# ── Helpers ──────────────────────────────────────────────────────────────────

async def test_publish(base: str, cred: dict) -> bool:
    """POST /<topic> — publish a test ping."""
    url = f"{base.rstrip('/')}/{TEST_TOPIC}"
    payload = json.dumps({"stage": "test", "source": "aem_connectivity_test"})
    try:
        async with httpx.AsyncClient(
            auth=(cred["username"], cred["password"]), timeout=10
        ) as client:
            r = await client.post(url, content=payload,
                                  headers={"Content-Type": "application/json"})
        if r.status_code in (200, 202, 204):
            print(f"  [OK]   PUBLISH  HTTP {r.status_code}")
            return True
        else:
            print(f"  [FAIL] PUBLISH  HTTP {r.status_code}: {r.text[:100]}")
            return False
    except Exception as exc:
        print(f"  [ERR]  PUBLISH  {exc}")
        return False


async def run_tests() -> None:
    print("=" * 65)
    print("  SAP AEM (Solace) Web Messaging Connectivity Test")
    print(f"  Broker: {BROKER_HOST}")
    print(f"  Topic:  {TEST_TOPIC}")
    print("=" * 65)

    # key: (endpoint_label, cred_label) → bool
    results: dict = {}

    for ep in ENDPOINTS:
        for cred in CREDENTIALS:
            tag = f"{ep['label']} | {cred['label']}"
            print(f"\n-- {tag} --")
            ok = await test_publish(ep["base"], cred)
            results[tag] = {"base": ep["base"], "cred": cred, "ok": ok}

    print("\n" + "=" * 65)
    print("  Summary")
    print("=" * 65)
    working = [v for v in results.values() if v["ok"]]
    for tag, res in results.items():
        mark = "[OK]  " if res["ok"] else "[FAIL]"
        print(f"  {mark}  {tag}")

    print()
    if working:
        best = working[0]
        print("  >> Recommended .env settings:")
        print(f"     AEM_REST_URL={best['base']}")
        print(f"     AEM_USERNAME={best['cred']['username']}")
        print(f"     AEM_PASSWORD={best['cred']['password']}")
    else:
        print("  [WARN] All combinations failed.")
        print("  Check: REST Delivery enabled on VPN, user has publish ACL.")
    print()


if __name__ == "__main__":
    asyncio.run(run_tests())
