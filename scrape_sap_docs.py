"""
scrape_sap_docs.py — SAP documentation scraper + HANA ingestion
===============================================================
Data sources (in order of reliability):

  1. STATIC — Hardcoded IFLMAP constraint knowledge (always succeeds).
  2. SAP HELP API — Queries help.sap.com search API.
  3. SAP COMMUNITY — Scrapes community.sap.com blog posts.
  4. SAP BLOGS — Scrapes blogs.sap.com (WordPress, plain HTML).
  5. SAP NOTES — Fetches specific SAP Support Notes from me.sap.com
                 using SAP_USERNAME / SAP_PASSWORD credentials.

All chunks are stored in HANA table SAP_HELP_DOCS.

Usage:
    python scrape_sap_docs.py                        # full run
    python scrape_sap_docs.py --dry-run              # scrape only, no DB write
    python scrape_sap_docs.py --clear                # drop + recreate table
    python scrape_sap_docs.py --static-only          # static knowledge only
    python scrape_sap_docs.py --notes-file links.txt # ingest SAP Notes from file
    python scrape_sap_docs.py --notes-only           # skip phases 1-4, notes only
    python scrape_sap_docs.py --max-notes 200        # limit note count per run

SAP Notes authentication (.env):
    SAP_USERNAME=your.email@company.com
    SAP_PASSWORD=yourpassword
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from hdbcli import dbapi
from playwright.async_api import async_playwright, BrowserContext
from yaspin import yaspin
from yaspin.spinners import Spinners

load_dotenv()

logging.basicConfig(level=logging.WARNING)

# ── HANA config ────────────────────────────────────────────────────────────────
HANA_ADDRESS  = os.getenv("HANA_ADDRESS") or os.getenv("HANA_HOST")
HANA_PORT     = int(os.getenv("HANA_PORT", "443"))
HANA_USER     = os.getenv("HANA_USER")
HANA_PASSWORD = os.getenv("HANA_PASSWORD")
HANA_SCHEMA   = os.getenv("HANA_SCHEMA")
HANA_TABLE    = os.getenv("HANA_TABLE_SAP_DOCS", "SAP_HELP_DOCS")

# ── SAP Notes auth config ──────────────────────────────────────────────────────
SAP_USERNAME        = os.getenv("SAP_USERNAME", "")
SAP_PASSWORD        = os.getenv("SAP_PASSWORD", "")
SAP_NOTE_CONCURRENCY = int(os.getenv("SAP_NOTE_CONCURRENCY", "5"))   # browser pages
SAP_NOTE_DELAY      = float(os.getenv("SAP_NOTE_DELAY", "0.3"))

CHUNK_MAX_CHARS = 1200
REQUEST_DELAY   = 1.5

# ── SOURCE 1: Static IFLMAP knowledge (always reliable) ───────────────────────
# Based directly on the real deploy errors from mcp.log. These constraints are
# the ground truth the fixer agent needs most — guaranteed to be ingested.

STATIC_KNOWLEDGE: list[dict[str, str]] = [
    {
        "title":    "IFLMAP Profile — Forbidden Components and Version Limits",
        "category": "ADAPTER_VERSIONS",
        "url":      "static://iflmap-constraints",
        "text": (
            "SAP Cloud Integration IFLMAP Profile — Forbidden Components and Version Limits.\n"
            "FORBIDDEN — these components cause deployment validation failure:\n"
            "  - SOAP 1.1 adapter: NOT SUPPORTED on IFLMAP profile. "
            "Use SOAP 1.x adapter (max version 1.11) or HTTP adapter instead.\n"
            "  - SOAP adapter version 1.12 or higher: maximum supported version is 1.11.\n"
            "  - EndEvent version 1.1 or higher: maximum supported version is 1.0. "
            "Always set version='1.0' on EndEvent elements.\n"
            "  - ExceptionSubprocess version 1.2 or higher: maximum supported version is 1.1. "
            "Always set version='1.1' on ExceptionSubprocess elements.\n"
            "  - Any component marked 'not supported in Cloud Integration profile' "
            "or 'not supported in IFLMAP profile'.\n"
            "RULE: Do not change version attributes from the original iFlow. "
            "Preserve exact version numbers on all components unless a version "
            "violates the limits above — in which case downgrade to the allowed maximum."
        ),
    },
    {
        "title":    "Content-Based Router — Default Route Requirement",
        "category": "CONTENT_BASED_ROUTER",
        "url":      "static://router-default-route",
        "text": (
            "SAP Cloud Integration Content-Based Router — Default Route is Mandatory.\n"
            "Every Content-Based Router block MUST contain a route with conditionType='Default'. "
            "Deployment validation will fail with error 'Content Based Router model must have a "
            "default route' if no default route is present.\n"
            "How to add a default route in iFlow XML:\n"
            "  <route xml:id='route-default' conditionType='Default' "
            "targetRef='end-event-id' sourceRef='router-id'/>\n"
            "The default route target can be an End Event, an Exception Subprocess, "
            "or a logging step — but it must exist. "
            "Never create a Content-Based Router without a default path."
        ),
    },
    {
        "title":    "SOAP Adapter — Version Constraints and SOAP 1.1 Replacement",
        "category": "SOAP_ADAPTER",
        "url":      "static://soap-adapter-versions",
        "text": (
            "SAP Cloud Integration SOAP Adapter Version Constraints.\n"
            "SOAP 1.1 adapter (componentVersion='1.1' on a SOAP sender/receiver) is NOT supported "
            "on the Cloud Integration IFLMAP profile and causes deploy failure: "
            "'SOAP 1.1 not supported'.\n"
            "Maximum supported SOAP adapter version: 1.11.\n"
            "Replacement strategy when SOAP 1.1 is present:\n"
            "  - If the endpoint accepts plain HTTP/REST calls: replace with HTTP adapter.\n"
            "  - If the endpoint requires SOAP: use SOAP 1.x adapter with version <= 1.11.\n"
            "  - Do NOT add a SOAP adapter if the original iFlow had no SOAP adapter — "
            "use HTTP adapter instead.\n"
            "Never set SOAP adapter componentVersion to 1.12 or higher."
        ),
    },
    {
        "title":    "Exception Subprocess — Version and Configuration",
        "category": "ERROR_HANDLING",
        "url":      "static://exception-subprocess",
        "text": (
            "SAP Cloud Integration Exception Subprocess — Supported Versions.\n"
            "Maximum supported ExceptionSubprocess version: 1.1.\n"
            "Deploy error 'ExceptionSubprocess version 1.2 not supported' occurs when "
            "version='1.2' or higher is set.\n"
            "Always use version='1.1' or lower on ExceptionSubprocess elements.\n"
            "Do NOT add an ExceptionSubprocess to an iFlow that did not originally have one, "
            "unless the proposed fix explicitly requires it and the version is set to 1.1 or lower.\n"
            "Exception Subprocess is used to catch and handle runtime errors within an iFlow. "
            "It must be connected to an End Message or End Event step."
        ),
    },
    {
        "title":    "HTTP Adapter — Safe Alternative to SOAP 1.1 and REST Endpoints",
        "category": "HTTP_ADAPTER",
        "url":      "static://http-adapter-config",
        "text": (
            "SAP Cloud Integration HTTP Receiver Adapter — Configuration Reference.\n"
            "The HTTP adapter is the safe alternative to SOAP 1.1 for REST/JSON backend calls.\n"
            "Key configuration properties:\n"
            "  - Address: full endpoint URL including path (e.g. https://host/api/resource).\n"
            "  - Credential Name: credential alias stored in SAP CPI security material.\n"
            "  - Method: GET / POST / PUT / DELETE / PATCH.\n"
            "  - Request Headers: set Accept and Content-Type headers as needed.\n"
            "  - Timeout: connection and response timeout in milliseconds (default 60000).\n"
            "  - Send Body: enable for POST/PUT to include the message body.\n"
            "CONFIG-ONLY FIX RULE: for BACKEND_ERROR incidents, only modify existing adapter "
            "properties (URL, credential alias, timeout, method, headers). "
            "Do not add new structural components."
        ),
    },
    {
        "title":    "OData Receiver Adapter — JSON Converter Compatibility",
        "category": "ODATA_ADAPTER",
        "url":      "static://odata-adapter-json",
        "text": (
            "SAP Cloud Integration OData Receiver Adapter — Message Type Compatibility.\n"
            "Deploy error 'JSON To XML Converter cannot process the message type passed by "
            "element OData Receiver' occurs when a JSON To XML Converter is connected directly "
            "after an OData Receiver Adapter.\n"
            "Root cause: OData Receiver already returns XML — adding a JSON To XML Converter "
            "is invalid because the input is already XML, not JSON.\n"
            "Fix: remove the JSON To XML Converter that is placed after an OData Receiver. "
            "If JSON output is needed, use an XML to JSON Converter instead.\n"
            "OData Receiver Adapter returns: XML (ATOM or JSON depending on $format parameter). "
            "Default format is XML/ATOM."
        ),
    },
    {
        "title":    "iFlow Fix Strategy — Config-Only vs Structural Changes",
        "category": "BEST_PRACTICES",
        "url":      "static://fix-strategy",
        "text": (
            "SAP Cloud Integration iFlow Fix Strategy.\n"
            "CONFIG-ONLY fixes (safe, low risk of deploy failure):\n"
            "  - Change receiver adapter URL, endpoint path, timeout, credential alias.\n"
            "  - Update Accept / Content-Type headers in adapter properties.\n"
            "  - Modify retry count and retry interval on an adapter.\n"
            "  - Change method (GET/POST) on an HTTP adapter.\n"
            "STRUCTURAL changes (high risk — verify version compatibility before applying):\n"
            "  - Adding a new Content-Based Router (must add default route).\n"
            "  - Adding an ExceptionSubprocess (version must be <= 1.1).\n"
            "  - Adding a converter step (check input type compatibility).\n"
            "  - Adding a new adapter type (verify IFLMAP profile support).\n"
            "RULE: When the proposed_fix text describes structural changes as 'conceptual guidance', "
            "implement ONLY the adapter property / configuration portion. "
            "Skip 'add Router' or 'add Exception Subprocess' instructions unless "
            "the original iFlow already contains that component type."
        ),
    },
    {
        "title":    "iFlow Unlock — Artifact Locked Error",
        "category": "DEPLOY_ERRORS",
        "url":      "static://artifact-locked",
        "text": (
            "SAP Cloud Integration — Cannot update artifact because it is locked.\n"
            "Error: 'Cannot update the artifact as it is locked'.\n"
            "Cause: another user or session has checked out (locked) the iFlow for editing "
            "in the SAP Integration Suite design-time UI.\n"
            "Resolution options:\n"
            "  1. Ask the user who locked the iFlow to cancel their checkout in the design-time UI.\n"
            "  2. Use the SAP CPI design-time API to cancel the checkout: "
            "DELETE /api/v1/IntegrationDesigntimeArtifacts(Id='{id}',Version='active')/$value/checkout\n"
            "  3. If the API returns HTTP 501 (Not Implemented), the tenant does not support "
            "programmatic checkout cancellation — manual unlock via UI is required.\n"
            "The fix pipeline will mark the incident as FIX_FAILED with failed_stage=locked "
            "and should not be retried until the lock is cleared."
        ),
    },
    {
        "title":    "EndEvent — Version Constraint",
        "category": "ADAPTER_VERSIONS",
        "url":      "static://endevent-version",
        "text": (
            "SAP Cloud Integration EndEvent Version Constraint.\n"
            "Maximum supported EndEvent version on the IFLMAP profile: 1.0.\n"
            "Deploy error 'EndEvent version 1.1 not supported' occurs when "
            "version='1.1' or higher is set on an EndEvent element.\n"
            "Always set version='1.0' on all EndEvent XML elements.\n"
            "Example: <endEvent xml:id='end1' version='1.0'/>\n"
            "Do not change the version from the original iFlow unless the original version "
            "already violates the 1.0 limit. "
            "Preserve version='1.0' when applying any iFlow fix."
        ),
    },
]

# ── SOURCE 2: SAP Help search API ─────────────────────────────────────────────
# Multiple endpoint + product-code combinations tried in order.

_SAP_HELP_ENDPOINTS = [
    "https://help.sap.com/api/v2/search",
    "https://help.sap.com/api/v1/search",
    "https://help.sap.com/http.svc/api/search",
]
_SAP_PRODUCT_CODES = [
    "SAP_INTEGRATION_SUITE",
    "CP_INTEGRATION_SUITE",
    "CLOUD_INTEGRATION",
    "SAP_CLOUD_INTEGRATION",
    "HCI",
    None,   # no product filter — broadest search
]

SEARCH_QUERIES: list[dict[str, str]] = [
    {"query": "SOAP adapter version IFLMAP cloud integration profile restriction",       "category": "ADAPTER_VERSIONS"},
    {"query": "ExceptionSubprocess EndEvent version supported cloud integration",        "category": "ADAPTER_VERSIONS"},
    {"query": "Content Based Router default route required iFlow",                       "category": "CONTENT_BASED_ROUTER"},
    {"query": "SOAP 1.1 not supported HTTP adapter alternative cloud integration",       "category": "SOAP_ADAPTER"},
    {"query": "HTTP receiver adapter timeout credential alias cloud integration",        "category": "HTTP_ADAPTER"},
    {"query": "Exception Subprocess error handling iFlow cloud integration",             "category": "ERROR_HANDLING"},
    {"query": "deploy validation error iFlow cloud integration",                         "category": "DEPLOY_ERRORS"},
    {"query": "OData receiver adapter JSON XML converter compatibility",                 "category": "ODATA_ADAPTER"},
    {"query": "backend error 500 receiver adapter retry cloud integration",              "category": "BACKEND_ERROR"},
    {"query": "authentication error credential alias OAuth SAP Cloud Integration",      "category": "AUTH_ERROR"},
    {"query": "message mapping error XSLT groovy script cloud integration",             "category": "MAPPING_ERROR"},
    {"query": "iFlow component version restriction cloud integration profile",          "category": "BEST_PRACTICES"},
]

# ── SOURCE 3: SAP Community blog posts ────────────────────────────────────────
# community.sap.com — Khoros/Lithium platform, plain HTML search results.

COMMUNITY_SEARCH_URL = "https://community.sap.com/t5/forums/searchpage/tab/message"
COMMUNITY_QUERIES: list[dict[str, str]] = [
    {"query": "SAP Cloud Integration SOAP adapter version error fix",         "category": "SOAP_ADAPTER"},
    {"query": "SAP CPI Content Based Router default route deployment error",  "category": "CONTENT_BASED_ROUTER"},
    {"query": "SAP Cloud Integration ExceptionSubprocess version fix",        "category": "ERROR_HANDLING"},
    {"query": "SAP CPI HTTP adapter backend error fix",                       "category": "BACKEND_ERROR"},
    {"query": "SAP Cloud Integration iFlow deploy validation error fix",      "category": "DEPLOY_ERRORS"},
]

# ── SOURCE 4: SAP Blogs ────────────────────────────────────────────────────────
# blogs.sap.com — WordPress site, plain HTML, rich CPI technical content.

SAP_BLOGS_SEARCH_URL = "https://blogs.sap.com/"
SAP_BLOGS_QUERIES: list[dict[str, str]] = [
    {"query": "SAP Cloud Integration SOAP adapter IFLMAP profile error",      "category": "SOAP_ADAPTER"},
    {"query": "SAP CPI iFlow deploy validation error fix",                    "category": "DEPLOY_ERRORS"},
    {"query": "SAP Cloud Integration Content Based Router default route",     "category": "CONTENT_BASED_ROUTER"},
    {"query": "SAP CPI HTTP adapter backend error timeout",                   "category": "BACKEND_ERROR"},
    {"query": "SAP Cloud Integration authentication OAuth error fix",         "category": "AUTH_ERROR"},
    {"query": "SAP CPI message mapping Groovy script error",                  "category": "MAPPING_ERROR"},
    {"query": "SAP Cloud Integration OData receiver adapter error",           "category": "ODATA_ADAPTER"},
]


# ── HANA helpers ───────────────────────────────────────────────────────────────

def _hana_connect() -> dbapi.Connection | None:
    if not all([HANA_ADDRESS, HANA_USER, HANA_PASSWORD, HANA_SCHEMA]):
        return None
    try:
        conn = dbapi.connect(
            address=HANA_ADDRESS, port=HANA_PORT,
            user=HANA_USER, password=HANA_PASSWORD,
            encrypt=True, **{"sslValidateCertificate": False},  # type: ignore[arg-type]
        )
        cur = conn.cursor()
        cur.execute(f'SET SCHEMA "{HANA_SCHEMA}"')
        cur.close()
        return conn
    except Exception:
        return None


def _create_table(conn: dbapi.Connection, drop_first: bool = False) -> None:
    cur = conn.cursor()
    if drop_first:
        try:
            cur.execute(f'DROP TABLE "{HANA_TABLE}"')
            conn.commit()
        except Exception:
            conn.rollback()
    try:
        cur.execute(
            f"""
            CREATE TABLE "{HANA_TABLE}" (
                ID       INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                VEC_TEXT NCLOB,
                VEC_META NCLOB
            )
            """
        )
        conn.commit()
    except Exception:
        # Table already exists — that's fine
        conn.rollback()
    cur.close()


def _get_existing_urls(conn: dbapi.Connection) -> set[str]:
    cur = conn.cursor()
    try:
        cur.execute(f'SELECT VEC_META FROM "{HANA_TABLE}"')
        urls: set[str] = set()
        for (meta_str,) in cur.fetchall():
            try:
                meta = json.loads(meta_str or "{}")
                if meta.get("url"):
                    urls.add(meta["url"])
            except Exception:
                pass
        return urls
    except Exception:
        return set()
    finally:
        cur.close()


def _insert_rows(conn: dbapi.Connection, rows: list[dict[str, Any]], sp: Any) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    inserted = 0
    for idx, row in enumerate(rows, 1):
        sp.text = f"Writing to HANA  [{idx}/{len(rows)}]  {row['meta'].get('title', '')[:45]}"
        try:
            cur.execute(
                f'INSERT INTO "{HANA_TABLE}" (VEC_TEXT, VEC_META) VALUES (?, ?)',
                (row["text"], json.dumps(row["meta"])),
            )
            inserted += 1
        except Exception:
            pass
    conn.commit()
    cur.close()
    return inserted


# ── Text helpers ───────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def _chunk_text(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?\n])\s+", text)
    chunks: list[str] = []
    current = ""
    for s in sentences:
        if len(current) + len(s) + 1 <= CHUNK_MAX_CHARS:
            current = f"{current} {s}".strip() if current else s
        else:
            if current:
                chunks.append(current)
            current = s if len(s) <= CHUNK_MAX_CHARS else ""
            if len(s) > CHUNK_MAX_CHARS:
                for i in range(0, len(s), CHUNK_MAX_CHARS):
                    chunks.append(s[i : i + CHUNK_MAX_CHARS])
    if current:
        chunks.append(current)
    return [c for c in chunks if len(c.strip()) > 40]


def _make_row(text: str, title: str, url: str, category: str, source: str, query: str = "") -> dict[str, Any]:
    return {
        "text": text,
        "meta": {
            "title": title, "url": url, "category": category,
            "source": source, "query": query, "product": "SAP Cloud Integration",
        },
    }


# ── SOURCE 1: Static knowledge ─────────────────────────────────────────────────

def build_static_rows() -> list[dict[str, Any]]:
    rows = []
    for entry in STATIC_KNOWLEDGE:
        for chunk in (_chunk_text(entry["text"]) or [entry["text"][:CHUNK_MAX_CHARS]]):
            rows.append(_make_row(chunk, entry["title"], entry["url"], entry["category"], "static"))
    return rows


# ── SOURCE 2: SAP Help search API ─────────────────────────────────────────────

async def _try_sap_help_search(
    client: httpx.AsyncClient,
    query: str,
) -> list[Any]:
    """Try all endpoint + product-code combinations. Return raw results list."""
    for endpoint in _SAP_HELP_ENDPOINTS:
        for product in _SAP_PRODUCT_CODES:
            for top_key in ("top", "$top", "limit"):
                params: dict[str, Any] = {
                    "q": query, "language": "en-US",
                    "state": "PRODUCTION", top_key: 8,
                }
                if product:
                    params["product"] = product
                try:
                    resp = await client.get(endpoint, params=params, timeout=15.0)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    # Handle different response shapes
                    results = (
                        data.get("results") or data.get("data") or
                        data.get("items") or data.get("hits") or
                        data.get("documents") or []
                    )
                    if isinstance(results, dict):
                        results = (
                            results.get("items") or results.get("results") or
                            results.get("documents") or []
                        )
                    if isinstance(results, list) and results:
                        return results
                except Exception:
                    continue
    return []


async def search_sap_help(
    client: httpx.AsyncClient,
    query: str,
    category: str,
) -> list[dict[str, Any]]:
    results = await _try_sap_help_search(client, query)
    rows: list[dict[str, Any]] = []
    for item in results:
        title = item.get("title") or item.get("name") or "SAP Help"
        url   = item.get("url") or item.get("link") or ""
        raw   = (
            item.get("content") or item.get("description") or
            item.get("body")    or item.get("text") or
            item.get("excerpt") or ""
        )
        text = _strip_html(raw) if "<" in raw else raw.strip()
        if not text:
            text = title
        for chunk in (_chunk_text(text) or [text[:CHUNK_MAX_CHARS]]):
            rows.append(_make_row(chunk, title, url, category, "SAP Help Portal", query))
    return rows


# ── SOURCE 3: SAP Community ────────────────────────────────────────────────────

async def search_sap_community(
    client: httpx.AsyncClient,
    query: str,
    category: str,
) -> list[dict[str, Any]]:
    """Search SAP Community (Khoros platform) and scrape result snippets."""
    rows: list[dict[str, Any]] = []
    for search_url, params in [
        (COMMUNITY_SEARCH_URL,           {"q": query, "search_type": "thread"}),
        ("https://community.sap.com/t5/search/searchresultpage", {"q": query}),
    ]:
        try:
            resp = await client.get(search_url, params=params, timeout=15.0)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            # Khoros uses multiple class naming conventions across versions
            cards = (
                soup.find_all(class_=re.compile(r"search-result|result-item|lia-component-subject|MessageSubject|lia-message", re.I))
                or soup.find_all("li", class_=re.compile(r"result|message|thread", re.I))
                or soup.find_all("article")
            )
            cards = cards[:6]
            for card in cards:
                title_el = card.find(["h3", "h4", "h2", "a"])
                title    = title_el.get_text(strip=True) if title_el else "SAP Community"
                link_el  = card.find("a", href=True)
                link     = str(link_el.get("href", "")) if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://community.sap.com" + link
                snippet_el = card.find(class_=re.compile(r"snippet|preview|body|content|teaser|summary", re.I))
                text = snippet_el.get_text(separator=" ", strip=True) if snippet_el else card.get_text(separator=" ", strip=True)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 40:
                    rows.append(_make_row(text[:CHUNK_MAX_CHARS], title, link, category, "SAP Community", query))
            if rows:
                break
        except Exception:
            continue
    return rows


async def search_sap_blogs(
    client: httpx.AsyncClient,
    query: str,
    category: str,
) -> list[dict[str, Any]]:
    """Search blogs.sap.com (WordPress). Returns plain HTML article cards."""
    rows: list[dict[str, Any]] = []
    try:
        resp = await client.get(SAP_BLOGS_SEARCH_URL, params={"s": query}, timeout=15.0)
        if resp.status_code != 200:
            return rows
        soup = BeautifulSoup(resp.text, "html.parser")
        # WordPress uses <article> tags for each post
        articles = soup.find_all("article")[:6]
        for article in articles:
            title_el   = article.find(["h2", "h3", "h1"])
            title      = title_el.get_text(strip=True) if title_el else "SAP Blog"
            link_el    = (title_el.find("a", href=True) if title_el else None) or article.find("a", href=True)
            link       = str(link_el.get("href", "")) if link_el else ""
            excerpt_el = article.find(class_=re.compile(r"excerpt|summary|entry-summary|description|content", re.I))
            text       = excerpt_el.get_text(separator=" ", strip=True) if excerpt_el else title
            text       = re.sub(r"\s+", " ", text).strip()
            if len(text) > 40:
                rows.append(_make_row(text[:CHUNK_MAX_CHARS], title, link, category, "SAP Blogs", query))
    except Exception:
        pass
    return rows


# ── SOURCE 5: SAP Notes (me.sap.com) ──────────────────────────────────────────

def _load_note_ids(filepath: str) -> list[str]:
    """Extract SAP note IDs from a file containing me.sap.com/notes/... URLs."""
    ids: list[str] = []
    try:
        with open(filepath, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                m = re.search(r"/notes/(\d+)", line)
                if m:
                    ids.append(m.group(1))
    except Exception:
        pass
    return ids


async def _playwright_login(context: BrowserContext) -> bool:
    """
    Authenticate with me.sap.com using a real browser (Playwright).
    Navigates to a note URL to trigger the XSUAA → SAML → accounts.sap.com flow,
    then fills the two-step logOnForm (email, then password).
    The resulting session cookies are stored in `context` for all subsequent pages.

    Note: me.sap.com is a React SPA that never fires "networkidle"; all
    wait_for_load_state calls use try/except so a timeout does not abort the flow —
    we check the final URL instead.
    """
    if not SAP_USERNAME or not SAP_PASSWORD:
        return False
    page = await context.new_page()
    try:
        try:
            await page.goto(
                "https://me.sap.com/notes/3355155",
                wait_until="networkidle", timeout=60_000,
            )
        except Exception:
            pass  # SPA never goes networkidle — check URL below

        # If already on me.sap.com we're good (cached context)
        if "me.sap.com" in page.url and "accounts.sap.com" not in page.url:
            return True

        if "accounts.sap.com" not in page.url:
            return False

        # ── Step 1: fill email ─────────────────────────────────────────────────
        await page.locator("input[name='j_username']").fill(SAP_USERNAME)
        # submit button is NOT inside #logOnForm in the DOM — use a broad selector
        await page.locator("input[type='submit'], button[type='submit']").first.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # ── Step 2: fill password (appears on same page after email submit) ───
        if await page.locator("input[name='j_password']").count() > 0:
            await page.locator("input[name='j_password']").fill(SAP_PASSWORD)
            await page.locator("input[type='submit'], button[type='submit']").first.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except Exception:
                pass  # React SPA keeps background requests — check URL instead

        # Success = landed back on me.sap.com (SAML round-trip completed)
        return "me.sap.com" in page.url and "accounts.sap.com" not in page.url
    except Exception:
        return False
    finally:
        await page.close()


async def fetch_sap_note_playwright(
    context: BrowserContext,
    note_id: str,
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """
    Fetch one SAP Note by navigating with a real browser (Playwright).
    me.sap.com is a React SPA — only a real browser can execute the JS and
    render note content into the DOM.
    """
    async with sem:
        rows: list[dict[str, Any]] = []
        url = f"https://me.sap.com/notes/{note_id}"
        page = None
        try:
            page = await context.new_page()
            # Initiate navigation — SPA will briefly visit /loading before /notes/{id}
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass

            # Wait until the URL has settled back on /notes/{note_id} (not /loading)
            try:
                await page.wait_for_url(f"**/notes/{note_id}", timeout=30_000)
            except Exception:
                pass

            # Wait for React to render substantive content (h1 with real note title)
            # "SAP for Me" is the loading-screen title — wait for something more specific
            try:
                await page.wait_for_function(
                    """() => {
                        const h1 = document.querySelector('h1');
                        return h1 && h1.innerText.trim().length > 5
                            && !h1.innerText.includes('SAP for Me');
                    }""",
                    timeout=25_000,
                )
            except Exception:
                pass

            # Extract title from the rendered h1 (more reliable than page.title())
            try:
                h1_text = await page.locator("h1").first.inner_text(timeout=5_000)
                title = h1_text.strip() or f"SAP Note {note_id}"
            except Exception:
                raw_title = await page.title()
                title = re.sub(r"\s*[-|]\s*SAP.*$", "", raw_title).strip() or f"SAP Note {note_id}"

            # Extract rendered inner text — strip chrome (nav/header/footer/scripts)
            text: str = await page.evaluate(
                """() => {
                    document.querySelectorAll(
                        'script, style, nav, header, footer, [aria-hidden="true"]'
                    ).forEach(el => el.remove());
                    const main = (
                        document.querySelector('main') ||
                        document.querySelector('[id*="content"]') ||
                        document.querySelector('[class*="content"]') ||
                        document.body
                    );
                    return main ? (main.innerText || main.textContent || '') : '';
                }"""
            )
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 100:
                for chunk in (_chunk_text(text) or [text[:CHUNK_MAX_CHARS]]):
                    rows.append(_make_row(chunk, title, url, "SAP_NOTES", "SAP Notes", note_id))
        except Exception:
            pass
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            await asyncio.sleep(SAP_NOTE_DELAY)
        return rows


# ── Deduplication ──────────────────────────────────────────────────────────────

def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = row["text"].strip()[:200]
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="SAP Help scraper + HANA ingestion")
    parser.add_argument("--dry-run",     action="store_true", help="Scrape only, no DB write")
    parser.add_argument("--clear",       action="store_true", help="Drop + recreate table")
    parser.add_argument("--static-only", action="store_true", help="Ingest static knowledge only")
    parser.add_argument("--notes-only",  action="store_true", help="Skip phases 1-4, run SAP Notes only")
    parser.add_argument("--notes-file",  default="links.txt",  help="File with me.sap.com/notes/* URLs (default: links.txt)")
    parser.add_argument("--max-notes",   type=int, default=0,  help="Max notes to fetch per run (0 = all)")
    args = parser.parse_args()

    start    = time.time()
    all_rows: list[dict[str, Any]] = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # ── Phase 1: Static knowledge ──────────────────────────────────────────────
    if not args.notes_only:
        print(f"\n  Phase 1 — Static IFLMAP knowledge  ({len(STATIC_KNOWLEDGE)} entries)\n")
        with yaspin(Spinners.dots, text="Building static knowledge chunks…", color="cyan") as sp:
            static_rows = build_static_rows()
            all_rows.extend(static_rows)
            sp.ok(f"  ✔  {len(static_rows)} chunks built from {len(STATIC_KNOWLEDGE)} entries")

    if args.static_only:
        print("\n  --static-only: skipping live scraping.\n")
    else:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:

            if not args.notes_only:
                # ── Phase 2: SAP Help search API ──────────────────────────────
                print(f"\n  Phase 2 — SAP Help Search API  ({len(SEARCH_QUERIES)} queries)\n")
                phase2_total = 0
                for i, q in enumerate(SEARCH_QUERIES, 1):
                    label = f"[{i:>2}/{len(SEARCH_QUERIES)}]  {q['category']:<22}  {q['query'][:50]}"
                    with yaspin(Spinners.dots, text=label, color="cyan") as sp:
                        rows = await search_sap_help(client, q["query"], q["category"])
                        all_rows.extend(rows)
                        phase2_total += len(rows)
                        if rows:
                            sp.ok(f"  ✔  {len(rows)} chunks")
                        else:
                            sp.write(f"  –  [{i:>2}/{len(SEARCH_QUERIES)}] {q['category']} — 0 results")
                    if i < len(SEARCH_QUERIES):
                        await asyncio.sleep(REQUEST_DELAY)
                print(f"\n  Phase 2 total: {phase2_total} chunks\n")

                # ── Phase 3: SAP Community ─────────────────────────────────────
                print(f"\n  Phase 3 — SAP Community  ({len(COMMUNITY_QUERIES)} queries)\n")
                phase3_total = 0
                for i, q in enumerate(COMMUNITY_QUERIES, 1):
                    label = f"[{i}/{len(COMMUNITY_QUERIES)}]  {q['query'][:60]}"
                    with yaspin(Spinners.dots, text=label, color="cyan") as sp:
                        rows = await search_sap_community(client, q["query"], q["category"])
                        all_rows.extend(rows)
                        phase3_total += len(rows)
                        if rows:
                            sp.ok(f"  ✔  {len(rows)} snippets")
                        else:
                            sp.write(f"  –  [{i}/{len(COMMUNITY_QUERIES)}] {q['query'][:50]} — 0 results")
                    if i < len(COMMUNITY_QUERIES):
                        await asyncio.sleep(REQUEST_DELAY)
                print(f"\n  Phase 3 total: {phase3_total} chunks\n")

                # ── Phase 4: SAP Blogs (WordPress) ────────────────────────────
                print(f"\n  Phase 4 — SAP Blogs (blogs.sap.com)  ({len(SAP_BLOGS_QUERIES)} queries)\n")
                phase4_total = 0
                for i, q in enumerate(SAP_BLOGS_QUERIES, 1):
                    label = f"[{i}/{len(SAP_BLOGS_QUERIES)}]  {q['query'][:60]}"
                    with yaspin(Spinners.dots, text=label, color="cyan") as sp:
                        rows = await search_sap_blogs(client, q["query"], q["category"])
                        all_rows.extend(rows)
                        phase4_total += len(rows)
                        if rows:
                            sp.ok(f"  ✔  {len(rows)} articles")
                        else:
                            sp.write(f"  –  [{i}/{len(SAP_BLOGS_QUERIES)}] {q['query'][:50]} — 0 results")
                    if i < len(SAP_BLOGS_QUERIES):
                        await asyncio.sleep(REQUEST_DELAY)
                print(f"\n  Phase 4 total: {phase4_total} chunks\n")

            # ── Phase 5: SAP Notes (Playwright — renders React SPA) ───────────
            note_ids = _load_note_ids(args.notes_file)
            if not note_ids:
                print(f"  Phase 5 — SAP Notes: no IDs loaded from '{args.notes_file}' (skipping)\n")
            elif not SAP_USERNAME or not SAP_PASSWORD:
                print(
                    "  Phase 5 — SAP Notes: SAP_USERNAME / SAP_PASSWORD not set in .env  (skipping)\n"
                    "  Add them to .env and re-run to ingest the SAP Notes.\n"
                )
            else:
                if args.max_notes and args.max_notes < len(note_ids):
                    note_ids = note_ids[: args.max_notes]
                print(
                    f"\n  Phase 5 — SAP Notes ({len(note_ids)} notes,"
                    f" concurrency={SAP_NOTE_CONCURRENCY} browser pages)\n"
                )

                # ── Connect to HANA now so we can write per-note (crash-safe) ──
                notes_conn: dbapi.Connection | None = None
                notes_existing: set[str] = set()
                if not args.dry_run:
                    with yaspin(Spinners.dots, text=f"Connecting to HANA  {HANA_ADDRESS}…", color="yellow") as sp:
                        notes_conn = _hana_connect()
                        if notes_conn:
                            _create_table(notes_conn, drop_first=args.clear)
                            notes_existing = _get_existing_urls(notes_conn)
                            sp.ok(f"  ✔  {HANA_ADDRESS}:{HANA_PORT}  schema={HANA_SCHEMA}  ({len(notes_existing)} already ingested)")
                        else:
                            sp.fail("  ✘  HANA connection failed — notes will be collected in memory only")

                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True)
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="en-US",
                    )

                    with yaspin(Spinners.dots, text="Authenticating with SAP (browser)…", color="magenta") as sp:
                        logged_in = await _playwright_login(context)
                        if logged_in:
                            sp.ok("  ✔  SAP login successful")
                        else:
                            sp.fail("  ✘  SAP login failed — notes will be skipped (check credentials)")

                    if logged_in:
                        sem          = asyncio.Semaphore(SAP_NOTE_CONCURRENCY)
                        phase5_total = 0
                        phase5_inserted = 0
                        done         = 0

                        with yaspin(Spinners.dots, text=f"Fetching notes  [0/{len(note_ids)}]", color="magenta") as sp:
                            tasks = [fetch_sap_note_playwright(context, nid, sem) for nid in note_ids]
                            for coro in asyncio.as_completed(tasks):
                                note_rows = await coro
                                done += 1
                                phase5_total += len(note_rows)

                                if notes_conn and note_rows:
                                    # Filter already-ingested, insert immediately, update set
                                    new_rows = [
                                        r for r in note_rows
                                        if r["meta"].get("url") not in notes_existing
                                    ]
                                    if new_rows:
                                        _insert_rows(notes_conn, new_rows, sp)
                                        phase5_inserted += len(new_rows)
                                        for r in new_rows:
                                            notes_existing.add(r["meta"]["url"])
                                else:
                                    # dry-run or no HANA — fall back to in-memory
                                    all_rows.extend(note_rows)

                                sp.text = (
                                    f"Fetching notes  [{done}/{len(note_ids)}]"
                                    f"  {phase5_inserted} inserted  {phase5_total} chunks"
                                )
                            sp.ok(f"  ✔  {phase5_total} chunks from {done} notes  ({phase5_inserted} inserted to HANA)")
                        print(f"\n  Phase 5 total: {phase5_total} chunks  |  {phase5_inserted} inserted\n")

                    await browser.close()

                if notes_conn:
                    try:
                        notes_conn.close()
                    except Exception:
                        pass
                    # Phase 5 already written — skip HANA section at end if notes-only
                    if args.notes_only:
                        elapsed = time.time() - start
                        print(f"  Done. {phase5_inserted} chunks in {HANA_SCHEMA}.{HANA_TABLE}  ({elapsed:.1f}s)\n")
                        return

    # ── Deduplicate + summary ──────────────────────────────────────────────────
    before    = len(all_rows)
    all_rows  = _deduplicate(all_rows)
    dupes     = before - len(all_rows)
    cats      = Counter(r["meta"]["category"] for r in all_rows)
    sources   = Counter(r["meta"]["source"] for r in all_rows)

    print(f"  {'─'*50}")
    print(f"  Total chunks : {len(all_rows)}  ({dupes} duplicates removed)")
    print(f"  {'─'*50}")
    for src, cnt in sorted(sources.items()):
        print(f"  {src:<30}  {cnt:>4} chunks")
    print(f"  {'─'*50}")
    for cat, cnt in sorted(cats.items()):
        print(f"  {cat:<30}  {cnt:>4} chunks")
    print(f"  {'─'*50}\n")

    if not all_rows:
        print("  No content collected — check network or run with --static-only\n")
        sys.exit(1)

    if args.dry_run:
        print("  --dry-run: skipping HANA write.\n")
        return

    # ── HANA connect ───────────────────────────────────────────────────────────
    with yaspin(Spinners.dots, text=f"Connecting to HANA  {HANA_ADDRESS}…", color="yellow") as sp:
        conn = _hana_connect()
        if not conn:
            sp.fail(
                "  ✘  Connection failed — set HANA_ADDRESS (or HANA_HOST), "
                "HANA_USER, HANA_PASSWORD, HANA_SCHEMA in .env"
            )
            sys.exit(1)
        sp.ok(f"  ✔  {HANA_ADDRESS}:{HANA_PORT}  schema={HANA_SCHEMA}")

    # ── Create table ───────────────────────────────────────────────────────────
    label = "Recreating" if args.clear else "Ensuring"
    with yaspin(Spinners.dots, text=f"{label} table {HANA_TABLE}…", color="yellow") as sp:
        _create_table(conn, drop_first=args.clear)
        sp.ok(f"  ✔  {HANA_SCHEMA}.{HANA_TABLE} ready")

    # ── Skip already-ingested ──────────────────────────────────────────────────
    if not args.clear:
        with yaspin(Spinners.dots, text="Checking for already-ingested entries…", color="yellow") as sp:
            existing = _get_existing_urls(conn)
            before   = len(all_rows)
            all_rows = [r for r in all_rows if r["meta"].get("url") not in existing]
            skipped  = before - len(all_rows)
            sp.ok(f"  ✔  {skipped} already in DB — skipped  ({len(all_rows)} new rows to insert)")

    # ── Insert ─────────────────────────────────────────────────────────────────
    with yaspin(Spinners.dots, text=f"Writing to HANA  [0/{len(all_rows)}]", color="green") as sp:
        inserted = _insert_rows(conn, all_rows, sp)
        elapsed  = time.time() - start
        sp.ok(f"  ✔  Inserted {inserted} chunks into {HANA_SCHEMA}.{HANA_TABLE}  ({elapsed:.1f}s)")

    try:
        conn.close()
    except Exception:
        pass

    print(f"\n  Done. Run again anytime — already-ingested URLs are skipped automatically.\n")


if __name__ == "__main__":
    asyncio.run(main())
