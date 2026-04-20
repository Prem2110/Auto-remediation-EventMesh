"""
SAP CPI Self-Healing Agent — Sierra Digital
============================================
Key fixes over original:
  1. Chatbot /query endpoint now detects "fix" intent and runs full RCA → fix → deploy pipeline
  2. apply_fix() verifies tool outputs and retries on failure (was silently succeeding on failure)
  3. run_rca() prompt is hardened — agent MUST return JSON; fallback classifier is used when LLM score < classifier score
  4. remediation_gate() always calls apply_fix when AUTO_FIX_ALL_CPI_ERRORS=true (was gated too strictly)
  5. deploy step is explicit and verified separately from update step
  6. Autonomous loop deduplication is fixed — seen_guids cleared per restart, not per run
  7. StepLogger captures tool name correctly (was losing it on async callbacks)
  8. All background tasks use a fresh copy of the incident dict to avoid mutation bugs
"""

from fastapi import FastAPI, HTTPException, Depends, Form, File, UploadFile, BackgroundTasks
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import ast
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import os
import uuid
import sys
import logging
import re
import hashlib
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, List, Any, Optional, Type
from datetime import datetime, UTC

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, create_model

from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from langchain.agents import create_agent
from langchain_core.tools import BaseTool
from langchain_core.callbacks import BaseCallbackHandler
from gen_ai_hub.proxy.langchain.openai import ChatOpenAI

from db.database import (
    get_all_history, create_query_history, update_query_history,
    get_xsd_files_by_session, addTestSuiteLog, get_testsuite_log_entries,
    update_test_suite_executions, updateTestSuiteStatus,
    create_incident, update_incident, get_all_incidents, get_incident_by_id,
    get_incident_by_message_guid, get_open_incident_by_signature,
    upsert_fix_pattern, get_similar_patterns, get_pending_approvals,
    increment_incident_occurrence, ensure_autonomous_incident_schema,
    ensure_fix_patterns_schema, get_recent_incident_by_group_key,
    create_escalation_ticket, ensure_escalation_tickets_schema,
)
from storage.storage import upload_multiple_files
from utils.utils import get_hana_timestamp
from utils.vector_store import get_vector_store

# ─────────────────────────────────────────────
# LOAD ENV
# ─────────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────────
# WINDOWS UTF FIX
# ─────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# MCP SERVERS
# ─────────────────────────────────────────────
MCP_SERVERS = {
    "integration_suite": "https://sap-integration-suite-mcp-lean-capybara-mb.cfapps.us10-001.hana.ondemand.com/mcp",
    "mcp_testing":       "https://iflow-test-mcp-py-wise-fox-ay.cfapps.us10-001.hana.ondemand.com/mcp",
    "documentation_mcp": "https://Documentation-Agent-py-reflective-armadillo-kx.cfapps.us10-001.hana.ondemand.com/mcp",
}

TRANSPORT_OPTIONS = {
    "integration_suite": {"verify": True,  "timeout": 300.0},  # deploy can take several minutes
    "mcp_testing":       {"verify": False, "timeout": 10.0},
    "documentation_mcp": {"verify": False, "timeout": 10.0},
}

MAX_RETRIES  = 3
MEMORY_LIMIT = 12

# ── In-memory fix progress store ─────────────────────────────────────────────
# Keyed by incident_id.  Written by execute_incident_fix at each pipeline step.
# Polled by /fix_status — avoids opening a HANA connection on every poll.
# Dict is lost on server restart, which is fine (ongoing fixes re-poll from DB).
FIX_PROGRESS: Dict[str, Dict] = {}

SERVER_ROUTING_GUIDE = {
    "documentation_mcp": "Use for SAP-standard documentation/specification/template generation.",
    "integration_suite": "Use for iFlow and SAP Integration Suite design/creation/deployment tasks.",
    "mcp_testing":       "Use for validation, test execution, and test-report related tasks.",
}

# ─────────────────────────────────────────────
# SAP POLLING CONFIG
# ─────────────────────────────────────────────
SAP_HUB_TENANT_URL    = os.getenv("SAP_HUB_TENANT_URL", "")
SAP_HUB_TOKEN_URL     = os.getenv("SAP_HUB_TOKEN_URL", "")
SAP_HUB_CLIENT_ID     = os.getenv("SAP_HUB_CLIENT_ID", "")
SAP_HUB_CLIENT_SECRET = os.getenv("SAP_HUB_CLIENT_SECRET", "")

POLL_INTERVAL_SECONDS   = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
AUTO_FIX_CONFIDENCE     = float(os.getenv("AUTO_FIX_CONFIDENCE", "0.90"))
SUGGEST_FIX_CONFIDENCE  = float(os.getenv("SUGGEST_FIX_CONFIDENCE", "0.70"))
AUTONOMOUS_ENABLED      = os.getenv("AUTONOMOUS_ENABLED", "false").lower() == "true"
AUTO_FIX_ALL_CPI_ERRORS = os.getenv("AUTO_FIX_ALL_CPI_ERRORS", "true").lower() == "true"
AUTO_DEPLOY_AFTER_FIX   = os.getenv("AUTO_DEPLOY_AFTER_FIX", "true").lower() == "true"
FAILED_MESSAGE_FETCH_LIMIT = int(os.getenv("FAILED_MESSAGE_FETCH_LIMIT", "100"))
RUNTIME_ERROR_FETCH_LIMIT  = int(os.getenv("RUNTIME_ERROR_FETCH_LIMIT", "200"))
MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE = int(os.getenv("MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE", "25"))
RUNTIME_ERROR_DETAIL_FETCH_LIMIT = int(os.getenv("RUNTIME_ERROR_DETAIL_FETCH_LIMIT", "25"))
RUNTIME_ERROR_DETAIL_CONCURRENCY = int(os.getenv("RUNTIME_ERROR_DETAIL_CONCURRENCY", "8"))
MAX_CONSECUTIVE_FAILURES     = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))
PENDING_APPROVAL_TIMEOUT_HRS = int(os.getenv("PENDING_APPROVAL_TIMEOUT_HRS", "24"))
PATTERN_MIN_SUCCESS_COUNT    = int(os.getenv("PATTERN_MIN_SUCCESS_COUNT", "2"))
TICKET_DEFAULT_ASSIGNEE = os.getenv("TICKET_DEFAULT_ASSIGNEE", "")
BURST_DEDUP_WINDOW_SECONDS = int(os.getenv("BURST_DEDUP_WINDOW_SECONDS", "60"))

REMEDIATION_POLICIES = {
    "MAPPING_ERROR":     {"action": "AUTO_FIX",        "replay_after_fix": True},
    "DATA_VALIDATION":   {"action": "AUTO_FIX",        "replay_after_fix": True},
    "AUTH_ERROR":        {"action": "AUTO_FIX",        "replay_after_fix": True},
    "CONNECTIVITY_ERROR":    {"action": "RETRY",           "replay_after_fix": True},
    # 4xx — iFlow sent a bad request; the adapter config (URL, path, method) is wrong
    "ADAPTER_CONFIG_ERROR":  {"action": "AUTO_FIX",        "replay_after_fix": True},
    # 5xx — backend service is failing; nothing in the iFlow can fix a server fault
    "BACKEND_ERROR":         {"action": "TICKET_CREATED",  "replay_after_fix": False},
    # SFTP errors (missing directory, permission denied) require human action on the
    # remote server — the agent cannot create directories or fix server-side paths.
    "SFTP_ERROR":        {"action": "TICKET_CREATED",  "replay_after_fix": False},
    "UNKNOWN_ERROR":     {"action": "APPROVAL",        "replay_after_fix": False},
}

# One-liner shown in the API response so callers know exactly what to do next.
# Keyed by error_type first; status overrides are applied at response-build time.
ACTION_HINTS: Dict[str, str] = {
    "MAPPING_ERROR":        "Agent is auto-fixing the message mapping — redeploy will follow automatically.",
    "DATA_VALIDATION":      "Agent is auto-fixing payload validation — redeploy will follow automatically.",
    "AUTH_ERROR":           "Agent is auto-fixing the credential/certificate configuration — redeploy will follow automatically.",
    "CONNECTIVITY_ERROR":   "Transient network issue — the agent will retry the iFlow automatically.",
    "ADAPTER_CONFIG_ERROR": "iFlow receiver adapter is misconfigured (HTTP 4xx) — agent is fixing the endpoint URL, method, or headers.",
    "BACKEND_ERROR":        "Backend service returned HTTP 5xx — the iFlow is working correctly. Backend team must investigate and restore the service.",
    "SFTP_ERROR":           "SFTP server-side issue — verify the target directory exists and the SFTP user has write/read access. Re-trigger the iFlow once resolved.",
    "UNKNOWN_ERROR":        "Error could not be classified automatically — awaiting human review before any fix is applied.",
}

# Status-level overrides — shown when the incident has already been routed.
_STATUS_ACTION_HINTS: Dict[str, str] = {
    "TICKET_CREATED":             "A support ticket has been created — waiting for the responsible team to resolve the underlying issue.",
    "AWAITING_APPROVAL":          "Fix is ready but requires human approval before it is applied.",
    "FIX_IN_PROGRESS":            "Agent is currently applying the fix — check back shortly.",
    "FIX_VERIFIED":               "Fix was applied and verified successfully — no further action needed.",
    "HUMAN_INITIATED_FIX":        "Fix was applied manually — no further action needed.",
    "RETRIED":                    "iFlow was retried automatically — monitor the next execution.",
    "FIX_FAILED":                 "Automatic fix failed — review the fix log and apply the suggested change manually.",
    "FIX_FAILED_UPDATE":          "Fix could not be uploaded to SAP CPI — check artifact permissions and retry.",
    "FIX_FAILED_DEPLOY":          "Fix was uploaded but deployment failed — check CPI deploy logs and retry deploy.",
    "FIX_FAILED_RUNTIME":         "iFlow deployed but failed again at runtime — a deeper manual investigation is needed.",
    "ARTIFACT_MISSING":           "iFlow artifact could not be found in SAP CPI — verify the iFlow ID and package.",
    "REJECTED":                   "Fix was rejected by the approver — re-open and submit a revised fix if needed.",
    "RCA_INCONCLUSIVE":           "Root cause could not be determined — additional logs or manual analysis required.",
    "VERIFICATION_UNAVAILABLE":   "Fix deployed but verification test could not run — manually verify the iFlow in CPI.",
}


TRANSIENT_ERROR_MARKERS = (
    "429", "503", "service unavailable", "too many requests",
    "connection refused", "connect timed out", "socketexception", "temporarily unavailable",
)

# Keywords that signal the user wants a fix action in chat
FIX_INTENT_KEYWORDS = (
    "fix", "repair", "resolve", "remediate", "apply fix",
    "auto fix", "self heal", "heal", "correct the error", "fix the error",
    "fix and deploy", "fix iflow", "deploy fix",
)

FALLBACK_FIX_BY_ERROR_TYPE: Dict[str, str] = {
    "MAPPING_ERROR": (
        "Open the affected message mapping in the iFlow and replace invalid target/source field references. "
        "Refresh the source and target structures, update renamed or missing fields, validate the mapping, "
        "then redeploy the iFlow."
    ),
    "DATA_VALIDATION": (
        "Add validation before the mapping or receiver step. Ensure mandatory fields are present, handle nulls, "
        "and route invalid payloads to an exception subprocess or dead-letter handling before redeploying."
    ),
    "AUTH_ERROR": (
        "Verify the credential alias, OAuth configuration, certificates, and security material used by the receiver "
        "adapter. Refresh expired credentials or certificates, update the adapter config, and redeploy."
    ),
    "ADAPTER_CONFIG_ERROR": (
        "The iFlow is sending a malformed or incorrect request (HTTP 4xx). "
        "Fix the receiver adapter: check the endpoint URL path, HTTP method, Content-Type header, "
        "and request payload structure. Correct the adapter config and redeploy."
    ),
    "BACKEND_ERROR": (
        "The backend service returned a server-side fault (HTTP 5xx). "
        "This cannot be fixed by changing the iFlow — the backend team must investigate and restore the service. "
        "A support ticket has been created."
    ),
    "CONNECTIVITY_ERROR": (
        "Verify destination, host, port, firewall, proxy, and adapter connectivity settings. Add or adjust retries "
        "only if the receiver adapter configuration is incomplete, then redeploy."
    ),
    "SFTP_ERROR": (
        "SFTP errors (missing directory, permission denied) cannot be fixed by changing the iFlow configuration. "
        "Required actions: (1) verify the directory path exists on the SFTP server, "
        "(2) create any missing directories on the SFTP server, "
        "(3) confirm the SFTP user has write permission to the target path. "
        "Once the server-side issue is resolved, re-trigger the iFlow manually."
    ),
    "UNKNOWN_ERROR": (
        "Inspect the message processing log, identify the failing iFlow component, correct the relevant step "
        "configuration or script, validate the artifact, and redeploy."
    ),
}

# ─────────────────────────────────────────────
# GROOVY TEMPLATES
# ─────────────────────────────────────────────
GROOVY_STRIPE_HTTP_ADAPTER = r'''
import com.sap.it.script.v2.api.Message
import groovy.json.JsonSlurper
import java.net.URLEncoder
import java.io.Reader
import com.sap.gateway.ip.core.customdev.util.Message
def Message processData(Message message) {
    Reader bodyReader = message.getBody(java.io.Reader)
    if (bodyReader != null) {
        try {
            def json = new JsonSlurper().parse(bodyReader)
            if (json instanceof Map) {
                if (json.containsKey("entity")) {
                    message.setHeader("entity", json.get("entity"))
                    json.remove("entity")
                }
                String formEncoded = json.collect { key, value ->
                    def encodedKey = URLEncoder.encode(key.toString(), "UTF-8")
                    def encodedValue = value == null ? "" : URLEncoder.encode(value.toString(), "UTF-8")
                    "${encodedKey}=${encodedValue}"
                }.join("&")
                message.setBody(formEncoded)
            }
        } catch (Exception e) {}
    }
    def headers = message.getHeaders()
    def queryParam = headers.get("Queryparam")
    def queryValue = headers.get("Queryvalue")
    if (queryParam && queryValue) {
        def keys   = queryParam.split(",")
        def values = queryValue.split(",")
        def resultList = []
        int count = Math.min(keys.length, values.length)
        for (int i = 0; i < count; i++) {
            resultList.add(keys[i].trim() + "=" + values[i].trim())
        }
        message.setProperty("QueryParameter", resultList.join("&"))
    }
    return message
}
'''.strip()

GROOVY_WOOCOMMERCE_HTTP_ADAPTER = r'''
import com.sap.it.script.v2.api.Message
import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.nio.charset.StandardCharsets

Message processData(Message message) {
    def headerCI = { String name ->
        def headers = message.getHeaders()
        for (def e : headers.entrySet()) {
            if (e?.key?.toString()?.equalsIgnoreCase(name)) return e.value?.toString()
        }
        return null
    }
    def setHeader = { String name, Object value -> message.setHeader(name, value) }
    def setProp   = { String name, Object value -> message.setProperty(name, value) }
    def require   = { boolean cond, String msg -> if (!cond) throw new IllegalArgumentException(msg) }

    String entity = headerCI('X-WC-Entity')
    String id     = headerCI('X-WC-Id')
    String method = headerCI('CamelHttpMethod')
    if (!method) method = headerCI('X-HTTP-Method')
    method = (method ?: 'GET').toUpperCase()
    String query  = headerCI('X-WC-Query')

    def allowedMethods = ['GET','POST','PUT','DELETE'] as Set
    require(allowedMethods.contains(method), "Unsupported HTTP method '"+method+"'.")
    require(entity != null && entity.trim().length() > 0, "Missing required header 'X-WC-Entity'.")
    entity = entity.trim()
    require(entity ==~ /^[A-Za-z0-9_\-]+$/, "Invalid entity '"+entity+"'.")

    if (id != null && id.trim().length() > 0) {
        id = id.trim()
        require(id ==~ /^[A-Za-z0-9_\-]+$/, "Invalid X-WC-Id '"+id+"'.")
    } else { id = null }

    StringBuilder path = new StringBuilder()
    path.append('wp-json/wc/v3/').append(entity)
    if (id != null) path.append('/').append(id)
    if (query != null && query.trim()) path.append('?').append(query.trim())
    String finalPath = path.toString()

    setHeader('CamelHttpPath', finalPath)
    setProp('wc.request.path', finalPath)
    setProp('wc.entity', entity)
    setProp('wc.id', id)
    setProp('wc.method', method)
    setHeader('Accept', 'application/json')

    if (method == 'GET' || method == 'DELETE') {
        message.setBody(null)
        setHeader('Content-Type', null)
        setHeader('Content-Length', '0')
    } else if (method == 'POST' || method == 'PUT') {
        def bodyObj = message.getBody(String)
        require(bodyObj != null && bodyObj.trim().length() > 0, "Missing JSON payload for "+method+".")
        try {
            def parsed        = new JsonSlurper().parseText(bodyObj)
            String normalized = JsonOutput.toJson(parsed)
            message.setBody(normalized)
            setHeader('Content-Type', 'application/json')
            int len = normalized.getBytes(StandardCharsets.UTF_8).length
            setHeader('Content-Length', Integer.toString(len))
        } catch (Exception ex) {
            throw new IllegalArgumentException("Invalid JSON payload: "+ex.message, ex)
        }
    }
    setHeader('CamelHttpMethod', method)
    String useBasic = headerCI('X-WC-UseBasicAuth')
    String authPair = headerCI('X-WC-Auth')
    if (useBasic != null && useBasic.equalsIgnoreCase('true')) {
        if (authPair != null && authPair.contains(':')) {
            String encoded = authPair.getBytes(StandardCharsets.UTF_8).encodeBase64().toString()
            setHeader('Authorization', "Basic "+encoded)
        } else {
            throw new IllegalArgumentException("X-WC-Auth missing or not in 'key:secret' format.")
        }
    }
    return message
}
'''.strip()

CPI_IFLOW_MESSAGE_MAPPING_RULES = """
Working with XSD Files
===================================================
When XSD files are provided in the user's message:
1. Identify XSD Content: Look for sections marked "--- XSD Files Available ---"
2. Parse XSD Structure: Understand the elements, types, and namespace
3. Link Message Mappings: Ensure message mappings referencing these XSDs are correctly linked.
4. Naming convention: Use source and target structure names to name XSD files.
5. Use tools to upload XSD files to src/main/resources/xsd/ if asked to create or update an IFlow.
6. Use other message mapping related tools as required.
===================================================
Message Mapping creation and update tools
===================================================
- get-messagemapping        - Get data of a Message Mapping
- update-message-mapping    - Update Message Mapping files/content
- deploy-message-mapping    - Deploy a message-mapping
- create-empty-mapping      - Create an empty message mapping
- get-all-messagemappings   - Get all available message mappings
- list-mapping-examples     - Get all available message mapping examples
- get-mapping-example       - Get an example provided by list-mapping-examples
"""

CPI_IFLOW_GROOVY_RULES = """
For SAP CPI iFlow updates involving Groovy Script steps, follow these rules strictly:
- Physical script file path in iFlow archive must be: src/main/resources/script/<FileName>.groovy
- Script reference inside the iFlow model must be: /script/<FileName>.groovy
- Never use invalid references such as /script/src/main/resource, /src/main/resources, scripts/<FileName>.groovy, or absolute paths.
- Before calling update-iflow, verify:
  1) the script file exists in payload at src/main/resources/script/<FileName>.groovy
  2) the Groovy Script step property points to /script/<FileName>.groovy
- If either check fails, fix payload first and only then call update-iflow.
""".strip()

# ─────────────────────────────────────────────
# SAP CPI iFlow XML PATTERNS REFERENCE
# Loaded from rules/sap_cpi_iflow_xml_patterns.md at startup.
# Injected into the fix prompt so the LLM has structural knowledge
# of common failure patterns without requiring hardcoded lookup rules.
# ─────────────────────────────────────────────
_RULES_DIR = Path(__file__).parent / "rules"
try:
    CPI_IFLOW_XML_PATTERNS = (_RULES_DIR / "sap_cpi_iflow_xml_patterns.md").read_text(encoding="utf-8")
except FileNotFoundError:
    CPI_IFLOW_XML_PATTERNS = ""

SAP_DOC_TEMPLATE = """
Documentation Creation Instruction:
    if the user asks specifically for t412 documentation:
        1. Call getT412AdapterDocReference with adapter_name to get the T412-based markdown reference.
    else:
        1. Call getCpiAdapterDocTemplate with adapter_name to get the base markdown template.
    2. Call getGenerationPrompt with the same adapter_name for detailed instructions.
    3. Generate a complete adapter documentation in Markdown format.
    4. Call savePdfDocument with adapter_name and the final Markdown content.
    5. Call uploadDocumentToS3 with adapter_name and the pdf_base64 value.
    6. Call generateDownloadLink with the adapter_name.
    7. Show the final download_url to the user as a clickable HTML link.
    ALWAYS validate each tool call returns success: True before moving to next step.
    If any tool returns an error, surface it and stop.
""".strip()

# ─────────────────────────────────────────────
# FIX + DEPLOY SYSTEM PROMPT  ← KEY FIX
# This is the strict prompt used when user asks to fix an error via chat.
# It forces the agent to: get iflow → update → deploy → confirm.
# ─────────────────────────────────────────────
ERROR_TYPE_FIX_GUIDANCE: Dict[str, str] = {
    "MAPPING_ERROR": (
        "=== MAPPING_ERROR GUIDANCE ===\n"
        "- Download the iFlow and inspect message mapping steps.\n"
        "- Check for renamed, removed, or type-mismatched source/target fields.\n"
        "- If XSD files are referenced, verify the XSD structure still matches the mapping.\n"
        "- Update field names, re-link structures, and validate before deploying."
    ),
    "AUTH_ERROR": (
        "=== AUTH_ERROR GUIDANCE ===\n"
        "- Inspect the receiver adapter's security configuration (credential alias, OAuth, certificate).\n"
        "- Look for expired tokens, wrong credential alias names, or missing security material.\n"
        "- Update the credential alias or adapter config — do NOT hardcode credentials.\n"
        "- Redeploy after fixing the security configuration."
    ),
    "DATA_VALIDATION": (
        "=== DATA_VALIDATION GUIDANCE ===\n"
        "- Locate the failing validation step or mapping that rejects the payload.\n"
        "- Add null checks, mandatory field guards, or default value handling.\n"
        "- Route invalid payloads to an exception subprocess rather than failing silently.\n"
        "- Redeploy after updating validation logic."
    ),
    "CONNECTIVITY_ERROR": (
        "=== CONNECTIVITY_ERROR GUIDANCE ===\n"
        "- This is likely a transient network/destination issue, NOT a code bug.\n"
        "- Check the receiver adapter's host, port, timeout, and retry settings.\n"
        "- Increase connection timeout or retry count ONLY if the current config is insufficient.\n"
        "- Do NOT modify business logic or message mappings for connectivity errors."
    ),
    "ADAPTER_CONFIG_ERROR": (
        "=== ADAPTER_CONFIG_ERROR GUIDANCE ===\n"
        "- The backend returned HTTP 4xx — the iFlow is sending an incorrect request.\n"
        "- Inspect the receiver adapter: endpoint URL path, HTTP method, Content-Type, Accept headers.\n"
        "- Check if the API version in the URL is outdated or if a required query parameter is missing.\n"
        "- Do NOT modify sender-side mappings unless the payload format is the root cause.\n"
        "- CRITICAL — CONFIG-ONLY FIX: Apply ONLY property/configuration changes to EXISTING components "
        "(receiver URL, HTTP method, Accept header, credential alias). "
        "Do NOT add new structural components (Content-Based Router, Exception Subprocess, "
        "JSON/XML Converter, new adapter). "
        "The proposed_fix may describe structural changes as conceptual guidance — "
        "implement ONLY the adapter property portion, skip any 'add Router' or 'add Exception Subprocess' instructions."
    ),
}

FIX_AND_DEPLOY_PROMPT_TEMPLATE = """
YOU ARE A SAP CPI SELF-HEALING AGENT. Fix and deploy the broken iFlow described below.

=== INCIDENT CONTEXT ===
iFlow ID:          {iflow_id}
Error Type:        {error_type}
Message GUID:      {message_guid}
Raw SAP Error:     {error_message}
RCA — Root Cause:  {root_cause}
RCA — Fix Spec:    {proposed_fix}
Affected Component:{affected_component}
{pattern_history}

IMPORTANT — "RCA — Fix Spec" is derived from automated analysis. It is a STARTING POINT, not
a literal instruction. You MUST verify every field name, XPath expression, and namespace prefix
against the actual iFlow XML and (for expression errors) the real message payload before writing
any fix. Never output an expression containing "e.g." or placeholder names — use only confirmed,
real values observed in the iFlow XML or message payload.

=== CRITICAL: SAP CLOUD INTEGRATION (IFLMAP) CONSTRAINTS ===
This iFlow runs on SAP Cloud Integration (node type: IFLMAP), NOT on-premise SAP PI/PO.
You MUST follow these platform-specific rules:

1. FORBIDDEN COMPONENTS (will cause deployment validation failure):
   - SOAP 1.1 adapter (use SOAP 1.x or HTTP adapter instead)
   - SOAP adapter version 1.12+ (max supported: 1.11)
   - EndEvent version 1.1+ (max supported: 1.0)
   - ExceptionSubprocess version 1.2+ (max supported: 1.1)
   - Any component marked "not supported in Cloud Integration profile"

2. REQUIRED ROUTER RULES:
   - Every Content-Based Router MUST have a default route
   - Default route can be: exception handler, logging step, or end event
   - Never create a router without a default path

3. ADAPTER COMPATIBILITY:
   - Use HTTP adapter for REST/JSON endpoints (not SOAP 1.1)
   - For SOAP services: use SOAP 1.x adapter (not SOAP 1.1)
   - Check adapter version compatibility with IFLMAP profile

4. ERROR HANDLING:
   - Use Exception Subprocess version 1.1 or lower
   - Use standard Camel error handlers
   - Avoid PI/PO-specific error handling patterns

5. VALIDATION BEFORE CHANGES:
   - If adding new components, verify they're Cloud Integration compatible
   - If unsure about a component, use HTTP/Groovy/Content Modifier instead
   - Test component versions against IFLMAP profile limits

{iflow_xml_patterns}

=== OPTIONAL REFERENCE STEP (run before mandatory steps if helpful) ===
STEP 0: If the fix requires structural changes (new adapter, mapping, channel, or script step),
         use an example iFlow as a structural reference:
         a. Call list-iflow-examples to see available examples.
         b. Pick the example whose name most closely matches: "{error_type}" / "{affected_component}"
         c. Call get-iflow-example with that name to retrieve the reference content.
         d. Use the retrieved structure as a reference ONLY — do not copy it verbatim.
         Skip entirely if the fix is a simple value/config change with no structural modifications.

=== MANDATORY STEPS — EXECUTE IN ORDER, NO SKIPPING ===
STEP 1: Call get-iflow tool with iFlow ID: "{iflow_id}"
         After receiving the iFlow XML, analyse it thoroughly:
         a. Read the iFlow name and infer its business purpose.
         b. Walk through every component in order — sender channel, each processing step
            (Content Modifiers, Mappings, Scripts, Routers, etc.), receiver channel.
         c. For each component, note: its type, its ID, and what it is configured to do.
         d. Identify which specific component is most likely responsible for the error,
            based on the error type "{error_type}" and affected component "{affected_component}".
         e. Read that component's current configuration in the XML carefully.
         Only proceed to STEP 1.5 (or STEP 2 if skipped) after completing this analysis.

STEP 1.5 — PAYLOAD INSPECTION (run this step if the error involves XPath, Groovy expressions,
           field names, namespace prefixes, or data values; SKIP for pure config/URL/auth fixes):
         Call get_message_logs ONCE with Message GUID: "{message_guid}"
         From the log output:
         a. Identify the actual XML/JSON structure of the message at the failing step.
         b. Confirm which field names and namespace prefixes exist in the real payload.
         c. Use ONLY confirmed field names and namespaces when writing any XPath, Groovy,
            or expression — never guess or use names from the RCA Fix Spec without verifying.
         d. If get_message_logs returns no payload content (e.g. message already gone),
            proceed to STEP 2 using only the iFlow XML structure as reference.
         SKIP this step entirely for errors that are pure adapter config changes
         (wrong URL, wrong HTTP method, missing auth header, wrong Content-Type).

STEP 2: Based on your iFlow analysis from STEP 1 and the RCA diagnosis above,
         determine the minimal, precise XML change needed to fix the error.
         Apply ONLY that change, then call update-iflow with the modified iFlow.
         → CRITICAL: The 'filepath' in the files array MUST be the EXACT path of the .iflw file
           as it appeared in the get-iflow response (e.g. "src/main/resources/scenarioflows/
           integrationflow/<OriginalName>.iflw"). DO NOT invent, guess, or reuse a filepath from
           a previous iFlow. Extract it directly from the get-iflow output you just received.
         → VERIFY the response contains status 200 or "successfully updated".
         → If update FAILS with "artifact is locked" or "Cannot update the artifact as it is locked":
             a. Search your available tools for: cancel-checkout, unlock-iflow, force-unlock, discard-draft.
             b. If found, call it with iFlow ID: "{iflow_id}", then WAIT and retry update-iflow ONCE.
             c. If no unlock tool found or retry still fails, stop and report:
                LOCKED: Artifact '{iflow_id}' is currently checked out by another user.
                Cancel checkout manually in SAP CPI Integration Flow Designer and retry.
             d. Do NOT keep looping — maximum 1 unlock attempt + 1 update retry.
         → If update FAILS for any other reason, stop and report the exact error.

STEP 2.5 — PRE-UPDATE SELF-CHECK (mandatory before calling update-iflow):
Review the modified iFlow XML. If any issue is found, correct it before uploading:
  a. Changes must be minimal — modify only what the fix requires. Do not restructure,
     rename, or reorganise any other part of the iFlow.
  b. Do not change any version attribute from the original. Preserve all component versions exactly.
  c. Do not introduce any new component (adapter, channel, step) that was not in the original iFlow,
     unless explicitly required by the fix. If you do add one, ensure it is fully configured with
     no empty or placeholder values.
  d. Verify every attribute value you wrote is valid for that element type in the IFLMAP profile.
  e. Properties added for configuration MUST be placed at the correct level in the iFlow XML:
     - Step-level properties belong inside the <bpmn2:extensionElements> of the specific
       <bpmn2:serviceTask> or <bpmn2:callActivity> element — NOT in <bpmn2:collaboration>.
     - For XPath namespace issues: declare the namespace INLINE in the XPath expression value
       using: declare namespace prefix='uri'; //prefix:element
       Do NOT add a top-level namespaceMapping property to the collaboration or flow root.

STEP 3: Call deploy-iflow tool with iFlow ID: "{iflow_id}"
         → VERIFY the response contains deployStatus "Success" or "DEPLOYED".
         → If deploy FAILS, stop and report the exact error.

=== STRICT RULES ===
- Do NOT skip any step.
- Call get_message_logs AT MOST ONCE and ONLY in STEP 1.5. Do not call it again later.
- Do NOT modify any other iFlow.
- Do NOT ask for confirmation — execute all steps automatically.
- After step 3, return this EXACT JSON (no markdown, no extra text):
{{
  "fix_applied": true/false,
  "deploy_success": true/false,
  "update_response": "<raw response from update-iflow>",
  "deploy_response": "<raw response from deploy-iflow>",
  "summary": "<2 sentences: what was changed and deploy outcome>"
}}

{error_type_guidance}
{groovy_rules}
"""


# ─────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────
class QueryRequest(BaseModel):
    query:   str
    id:      Optional[str] = None
    user_id: str

class QueryResponse(BaseModel):
    response: str
    id:       str
    error:    Optional[Any] = None

class ApprovalRequest(BaseModel):
    approved: bool
    comment:  Optional[str] = None

class DirectFixRequest(BaseModel):
    iflow_id:          str
    error_message:     str
    proposed_fix:      Optional[str] = None
    user_id:           str


# ─────────────────────────────────────────────
# LLM FACTORY
# ─────────────────────────────────────────────
def create_llm():
    dep = os.getenv("LLM_DEPLOYMENT_ID")
    if not dep:
        raise RuntimeError("LLM_DEPLOYMENT_ID missing in .env")
    return ChatOpenAI(deployment_id=dep, temperature=0)


# ─────────────────────────────────────────────
# JSON SCHEMA → PYDANTIC MODEL
# ─────────────────────────────────────────────
def build_model(name: str, schema: Dict, root=None):
    if root is None:
        root = schema
    if "type" not in schema and "schema" in schema:
        schema = schema["schema"]
    if "$ref" in schema:
        ref = schema["$ref"][2:].split("/")
        obj = root
        for r in ref:
            obj = obj.get(r, {})
        return build_model(name, obj, root)
    if "enum" in schema:
        from typing import Literal
        return Literal[tuple(schema["enum"])]
    if schema.get("type") == "object":
        props    = schema.get("properties", {})
        required = schema.get("required", [])
        fields   = {}
        for k, v in props.items():
            t         = build_model(name + "_" + k, v, root)
            default   = ... if k in required else None
            fields[k] = (t, default)
        safe = re.sub(r"\W", "_", name)
        return create_model(safe, **fields)
    if schema.get("type") == "array":
        t = build_model(name + "_item", schema.get("items", {}), root)
        return List[t]
    return {"string": str, "integer": int, "number": float, "boolean": bool}.get(schema.get("type"), Any)


# ─────────────────────────────────────────────
# MCP TOOL WRAPPER
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# IFLOW XML VALIDATOR — runs before every update-iflow call
# ─────────────────────────────────────────────

# Holds per-fix-run state: original filepath + XML captured from get-iflow snapshot.
# Each async task inherits a copy of the context, so concurrent fixes don't interfere.
_fix_ctx: ContextVar[Optional[Dict[str, str]]] = ContextVar("_fix_ctx", default=None)

_BPMN2 = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_IFL   = "http:///com.sap.ifl.model/Ifl.xsd"


def _extract_iflow_file(snapshot_str: str) -> tuple[str, str]:
    """
    Parse a get-iflow response string and return (filepath, xml_content) for the .iflw file.
    Returns ("", "") if not found.
    """
    try:
        data = json.loads(snapshot_str) if isinstance(snapshot_str, str) else snapshot_str
        files = data.get("files", []) if isinstance(data, dict) else []
        for f in files:
            fp = f.get("filepath", "")
            if fp.endswith(".iflw"):
                return fp, f.get("content", "")
    except Exception:
        pass
    # Fallback: regex scan for filepath key in raw text
    try:
        m = re.search(r'"filepath"\s*:\s*"([^"]+\.iflw)"', snapshot_str or "")
        if m:
            return m.group(1), ""
    except Exception:
        pass
    return "", ""


def _check_iflow_xml(original_xml: str, modified_xml: str) -> list[str]:
    """Structural checks on the modified iFlow XML. Returns list of error strings."""
    import re as _re

    errors: list[str] = []
    try:
        mod_root = ET.fromstring(modified_xml)
    except ET.ParseError as e:
        return [f"Modified iFlow XML is not valid XML: {e}. Fix the XML before calling update-iflow."]

    # Check 1 — no ifl:property inside bpmn2:collaboration extensionElements
    collab = mod_root.find(f"{{{_BPMN2}}}collaboration")
    if collab is not None:
        ext = collab.find(f"{{{_BPMN2}}}extensionElements")
        if ext is not None:
            bad_props = ext.findall(f"{{{_IFL}}}property")
            if bad_props:
                keys = [
                    (p.findtext(f"{{{_IFL}}}key") or p.findtext("key") or "?")
                    for p in bad_props
                ]
                errors.append(
                    f"ifl:property elements found inside <bpmn2:collaboration> extensionElements "
                    f"(keys: {keys}). These MUST be placed inside the specific step that uses them "
                    f"(e.g. inside the <bpmn2:serviceTask> for the XPath or mapping step), "
                    f"NOT at collaboration level. Move them to the correct step."
                )

    # Check 2 — version attributes must not change from original
    if original_xml:
        try:
            orig_root = ET.fromstring(original_xml)
            orig_versions = {
                el.get("id"): el.get("version")
                for el in orig_root.iter()
                if el.get("id") and el.get("version")
            }
            for el in mod_root.iter():
                el_id  = el.get("id")
                el_ver = el.get("version")
                if el_id and el_ver and el_id in orig_versions:
                    if orig_versions[el_id] != el_ver:
                        errors.append(
                            f"Version changed for element '{el_id}': "
                            f"original='{orig_versions[el_id]}', submitted='{el_ver}'. "
                            f"Do not modify version attributes."
                        )
        except ET.ParseError:
            pass  # original XML unreadable — skip version check

    # Check 3 — platform version caps on NEW elements (not in original)
    # Catches LLM adding a new component with a version above the IFLMAP profile limit.
    _VERSION_CAPS: Dict[str, tuple[str, float]] = {
        "EndEvent":           ("EndEvent", 1.0),
        "ExceptionSubProcess": ("ExceptionSubProcess", 1.1),
        "com.sap.soa.proxy.ws": ("SOAP adapter", 1.11),
        "SOAP":               ("SOAP adapter", 1.11),
    }
    orig_ids: set = set()
    if original_xml:
        try:
            orig_root2 = ET.fromstring(original_xml)
            orig_ids = {el.get("id") for el in orig_root2.iter() if el.get("id")}
        except ET.ParseError:
            pass
    for el in mod_root.iter():
        el_id  = el.get("id", "")
        el_ver = el.get("version", "")
        el_tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if el_id and el_id not in orig_ids and el_ver:
            # New element — check version cap
            for cap_key, (cap_label, cap_max) in _VERSION_CAPS.items():
                if cap_key in el_tag or cap_key in (el.get("name") or ""):
                    try:
                        if float(el_ver) > cap_max:
                            errors.append(
                                f"New element '{el_id}' has version '{el_ver}' which exceeds "
                                f"the IFLMAP platform maximum for {cap_label} ({cap_max}). "
                                f"Set version='{cap_max}' or lower."
                            )
                    except ValueError:
                        pass

    # Check 4 — XPath expressions that use a namespace prefix must include declare namespace
    # Looks for ifl:property values that look like XPath (contain '//' or 'xpath' in key)
    # but use a 'prefix:element' pattern without a 'declare namespace' directive.
    for el in mod_root.iter():
        key_el = el.find(f"{{{_IFL}}}key") or el.find("key")
        val_el = el.find(f"{{{_IFL}}}value") or el.find("value")
        if key_el is None or val_el is None:
            continue
        key = (key_el.text or "").lower()
        val = (val_el.text or "")
        if "xpath" in key or val.strip().startswith("//") or "//" in val:
            # Check for namespace prefix usage: word:word pattern (excluding http://)
            ns_uses = _re.findall(r'\b([a-zA-Z][a-zA-Z0-9_]*):[a-zA-Z]', val)
            ns_uses = [p for p in ns_uses if p.lower() not in ("http", "https", "urn", "xmlns")]
            if ns_uses:
                declared = _re.findall(r'declare\s+namespace\s+([a-zA-Z][a-zA-Z0-9_]*)\s*=', val)
                missing = [p for p in ns_uses if p not in declared]
                if missing:
                    errors.append(
                        f"XPath expression in property '{key_el.text}' uses namespace prefix(es) "
                        f"{missing} but no 'declare namespace' directive found. "
                        f"Add inline declarations before the path, e.g.: "
                        f"declare namespace {missing[0]}='http://...'; //{missing[0]}:element"
                    )

    # Check 5 — Content Modifier header rows must use srcType="Expression", never "Constant"
    # Header rows are identified by a 'headerName' sibling property inside the same parent element.
    for task in mod_root.iter(f"{{{_BPMN2}}}serviceTask"):
        ext = task.find(f"{{{_BPMN2}}}extensionElements")
        if ext is None:
            continue
        props = ext.findall(f"{{{_IFL}}}property")
        # Build a dict of key→value for the properties in this step
        kv: Dict[str, str] = {}
        for p in props:
            k = p.findtext(f"{{{_IFL}}}key") or p.findtext("key") or ""
            v = p.findtext(f"{{{_IFL}}}value") or p.findtext("value") or ""
            kv[k] = v
        # If this step has a headerName property (Content Modifier header row)
        # and srcType is set to "Constant", that is invalid
        if "headerName" in kv and kv.get("srcType", "") == "Constant":
            errors.append(
                f"Content Modifier step '{task.get('id', '?')}' has a header row with "
                f"srcType='Constant'. Header rows MUST use srcType='Expression'. "
                f"Change srcType value to 'Expression'."
            )

    # Check 6 — every exclusiveGateway (Content-Based Router) must have a default route
    # A default route is a sequenceFlow with isDefault="true" sourced from the gateway,
    # or a condition expression that always evaluates to true.
    for gw in mod_root.iter(f"{{{_BPMN2}}}exclusiveGateway"):
        gw_id = gw.get("id", "")
        # Collect outgoing sequence flow IDs
        outgoing_ids = {sf.text.strip() for sf in gw.findall(f"{{{_BPMN2}}}outgoing") if sf.text}
        if not outgoing_ids:
            continue  # no outgoing flows yet — don't flag incomplete edits
        has_default = False
        for sf in mod_root.iter(f"{{{_BPMN2}}}sequenceFlow"):
            if sf.get("sourceRef") == gw_id and sf.get("isDefault", "").lower() == "true":
                has_default = True
                break
        if not has_default and len(outgoing_ids) > 1:
            errors.append(
                f"Content-Based Router (exclusiveGateway) '{gw_id}' has no default route. "
                f"Every router MUST have a default outgoing sequenceFlow with isDefault='true'. "
                f"Add a default route to prevent deployment failure."
            )

    # Check 7 — Groovy script step references must use /script/<Name>.groovy format,
    # not the full archive path src/main/resources/script/...
    for el in mod_root.iter():
        key_el = el.find(f"{{{_IFL}}}key") or el.find("key")
        val_el = el.find(f"{{{_IFL}}}value") or el.find("value")
        if key_el is None or val_el is None:
            continue
        key = (key_el.text or "").lower()
        val = (val_el.text or "")
        if "script" in key and "src/main/resources" in val:
            errors.append(
                f"Groovy script reference '{val}' uses the full archive path. "
                f"The model reference must be '/script/<FileName>.groovy' "
                f"(not 'src/main/resources/script/...'). Fix the value."
            )

    return errors


def validate_before_update_iflow(args: Dict) -> list[str]:
    """
    Validate update-iflow args against the per-fix context captured from get-iflow.
    Returns list of error strings. Empty = valid, proceed with the real API call.
    """
    ctx = _fix_ctx.get()
    if ctx is None:
        return []  # no context set (e.g. manual / chat use) — skip validation

    errors: list[str] = []
    original_filepath = ctx.get("filepath", "")
    original_xml      = ctx.get("xml", "")

    # Parse the files argument (LLM may pass it as a JSON string or a list)
    files = args.get("files", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except Exception:
            files = []

    submitted_filepath = ""
    submitted_xml      = ""
    if isinstance(files, list) and files:
        submitted_filepath = files[0].get("filepath", "") if isinstance(files[0], dict) else ""
        submitted_xml      = files[0].get("content", "")  if isinstance(files[0], dict) else ""

    # Check 1 — filepath must match original exactly
    if original_filepath and submitted_filepath and submitted_filepath != original_filepath:
        errors.append(
            f"Wrong filepath: you submitted '{submitted_filepath}' but the iFlow filepath "
            f"from get-iflow is '{original_filepath}'. "
            f"Use the EXACT filepath from the get-iflow response — do not guess or invent it."
        )

    # Check 2 — XML structural rules
    if submitted_xml:
        errors.extend(_check_iflow_xml(original_xml, submitted_xml))

    return errors


class MCPTool(BaseTool):
    name:          str
    description:   str
    args_schema:   Type[BaseModel]
    server:        str
    mcp_tool_name: str
    manager:       "MultiMCP"

    def _run(self, *a, **kw):
        raise NotImplementedError()

    async def _arun(self, **kwargs):
        return await self.manager.execute(self.server, self.mcp_tool_name, kwargs)


def formatjson(input):
    try:
        if isinstance(input, str):
            try:
                return json.loads(input)
            except json.JSONDecodeError:
                return ast.literal_eval(input)
        return input
    except Exception:
        return {}


# ─────────────────────────────────────────────
# TEST EXECUTION TRACKER
# ─────────────────────────────────────────────
class TestExecutionTracker:
    def __init__(self, user_id, prompt, timestamp):
        self.user_id       = user_id
        self.prompt        = prompt
        self.timestamp     = timestamp
        self.executions    = []
        self.tool_map      = {}
        self.test_suite_id = None
        self.test_started  = False

    def handle_test_start(self, tool_call_id, args):
        try:
            if not self.test_started:
                self.test_suite_id = str(uuid.uuid4())
                self.test_started  = True
                addTestSuiteLog({
                    "test_suite_id": self.test_suite_id,
                    "user":          self.user_id,
                    "prompt":        self.prompt,
                    "timestamp":     self.timestamp,
                    "status":        "IN_PROGRESS",
                    "executions":    []
                })
            payload   = formatjson(args.get("payload"))
            header    = formatjson(args.get("header"))
            execution = {
                "payload":      payload,
                "headers":      header,
                "http_method":  args.get("http_method"),
                "message_id":   None,
                "message_logs": None,
            }
            self.executions.append(execution)
            self.tool_map[tool_call_id] = len(self.executions) - 1
            update_test_suite_executions(self.test_suite_id, self.executions)
        except Exception as e:
            logger.debug(f"[TestTracker] handle_test_start skipped: {e}")

    def handle_test_response(self, tool_call_id, response_json):
        try:
            body = response_json.get("response", {}).get("body", "")
            if not isinstance(body, str):
                return
            body_stripped = body.strip()
            if body_stripped.startswith("{") and body_stripped.endswith("}"):
                return
            match = re.search(r"MPL ID for the failed message is\s*:\s*([^\r\n]+)", body)
            if not match:
                return
            message_id = match.group(1)
            index = self.tool_map.get(tool_call_id)
            if index is None:
                return
            self.executions[index]["message_id"] = message_id
            update_test_suite_executions(self.test_suite_id, self.executions)
        except Exception as e:
            logger.debug(f"[TestTracker] handle_test_response skipped: {e}")

    def handle_log_response(self, message_id, logs):
        try:
            for execution in self.executions:
                if execution.get("message_id") in message_id:
                    execution["message_logs"] = logs
                    break
            update_test_suite_executions(self.test_suite_id, self.executions)
        except Exception as e:
            logger.debug(f"[TestTracker] handle_log_response skipped: {e}")


# ─────────────────────────────────────────────
# STEP LOGGER  ← FIXED: preserves tool name per run_id
# ─────────────────────────────────────────────
_FIX_TOOL_PROGRESS_LABELS: Dict[str, str] = {
    "get_iflow":         "Agent: reading current iFlow XML…",
    "update_iflow":      "Agent: uploading fixed iFlow to SAP CPI…",
    "deploy_iflow":      "Agent: deploying iFlow to runtime…",
    "get_deploy_error":  "Agent: checking deployment errors…",
    "list_iflow_examples": "Agent: searching reference examples…",
    "get_iflow_example": "Agent: loading reference example…",
    "unlock_iflow":      "Agent: unlocking iFlow for editing…",
    "cancel_checkout":   "Agent: cancelling existing checkout…",
    "force_unlock":      "Agent: force-unlocking iFlow…",
}


class StepLogger(BaseCallbackHandler):
    def __init__(self, tracker: TestExecutionTracker, progress_fn=None):
        self.steps            = []
        self.tracker          = tracker
        self._tool_names: Dict[str, str] = {}   # run_id → tool_name
        self._progress_fn     = progress_fn     # Optional[Callable[[str], None]]

    def on_tool_start(self, serialized, input_str, run_id=None, **kw):
        tool_name    = serialized.get("name", "unknown")
        tool_call_id = str(run_id)
        self._tool_names[tool_call_id] = tool_name
        self.steps.append({"tool": tool_name, "input": input_str, "output": None})
        logger.info("[TOOL_CALL] tool=%s | input=%.800s", tool_name, str(input_str))
        if self._progress_fn:
            # Strip server prefix (e.g. "integration_suite__get_iflow" → "get_iflow")
            short = tool_name.split("__")[-1] if "__" in tool_name else tool_name
            label = _FIX_TOOL_PROGRESS_LABELS.get(short)
            if label:
                try:
                    self._progress_fn(label)
                except Exception:
                    pass
        try:
            args = json.loads(input_str) if isinstance(input_str, str) else input_str
        except Exception:
            try:
                args = ast.literal_eval(input_str)
            except Exception:
                args = {}
        if "test_iflow_with_payload" in tool_name:
            self.tracker.handle_test_start(tool_call_id, args)

    def on_tool_end(self, output, run_id=None, **kw):
        tool_call_id = str(run_id)
        tool_name    = self._tool_names.get(tool_call_id, "unknown")
        raw_content  = output.content if hasattr(output, "content") else output

        try:
            response_json = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        except Exception:
            try:
                response_json = ast.literal_eval(raw_content)
            except Exception:
                response_json = {}

        if "test_iflow_with_payload" in tool_name:
            self.tracker.handle_test_response(tool_call_id, response_json)
        elif "get_message_logs" in tool_name:
            message_id = response_json.get("message_id")
            logs       = response_json.get("logs")
            self.tracker.handle_log_response(message_id, logs)

        # Update the last step that has no output yet
        for step in reversed(self.steps):
            if step["tool"] == tool_name and step["output"] is None:
                step["output"] = str(output)
                break

        logger.info("[TOOL_RESULT] tool=%s | output=%.2000s", tool_name, str(output))


# ─────────────────────────────────────────────
# SAP ERROR FETCHER
# ─────────────────────────────────────────────
class SAPErrorFetcher:
    def __init__(self):
        self._token: Optional[str] = None
        self._token_expiry: float  = 0.0

    async def _get_token(self) -> str:
        import time
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.post(
                SAP_HUB_TOKEN_URL,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     SAP_HUB_CLIENT_ID,
                    "client_secret": SAP_HUB_CLIENT_SECRET,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token        = data["access_token"]
            self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._token

    @staticmethod
    def _extract_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        if isinstance(payload.get("d"), dict):
            results = payload["d"].get("results")
            if isinstance(results, list):
                return results
            if payload["d"]:
                return [payload["d"]]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        return []

    async def _get_json(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        token = await self._get_token()
        base  = SAP_HUB_TENANT_URL.rstrip("/")
        url   = f"{base}{path}"
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.get(
                url,
                params=params or {"$format": "json"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()

    async def _get_text(self, path: str) -> str:
        token = await self._get_token()
        base  = SAP_HUB_TENANT_URL.rstrip("/")
        url   = f"{base}{path}"
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.text

    async def fetch_failed_messages(self, limit: int = FAILED_MESSAGE_FETCH_LIMIT) -> List[Dict]:
        try:
            params = {
                "$filter":  "Status eq 'FAILED'",
                "$orderby": "LogEnd desc",
                "$top":     str(max(1, limit)),
                "$format":  "json",
            }
            data    = await self._get_json("/api/v1/MessageProcessingLogs", params=params)
            results = self._extract_results(data)
            logger.info(f"[SAP Poller] Fetched {len(results)} failed messages")
            return results
        except Exception as e:
            logger.error(f"[SAP Poller] fetch_failed_messages error: {e}")
            return []

    async def fetch_failed_messages_count(self) -> int:
        """
        Fetch the total count of failed messages from SAP Integration Suite.
        Uses the OData $count endpoint (as a path segment) for efficient counting.
        Example: /api/v1/MessageProcessingLogs/$count?$filter=Status eq 'FAILED'
        """
        try:
            # Use $count as a path segment, not a query parameter
            token = await self._get_token()
            base  = SAP_HUB_TENANT_URL.rstrip("/")
            url   = f"{base}/api/v1/MessageProcessingLogs/$count"
            
            params = {
                "$filter": "Status eq 'FAILED'",
            }
            
            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                resp = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                
                # The $count endpoint returns a plain integer as text
                count_text = resp.text.strip()
                try:
                    count = int(count_text)
                    logger.info(f"[SAP Poller] Total failed messages count: {count}")
                    return count
                except ValueError:
                    logger.error(f"[SAP Poller] Invalid count response: {count_text}")
                    # Fallback to fetching messages
                    messages = await self.fetch_failed_messages(limit=1000)
                    return len(messages)
            
        except Exception as e:
            logger.error(f"[SAP Poller] fetch_failed_messages_count error: {e}")
            # Fallback: fetch with limit and count
            try:
                messages = await self.fetch_failed_messages(limit=1000)
                return len(messages)
            except Exception:
                return 0

    async def fetch_error_details(self, message_guid: str) -> Dict:
        try:
            error_text = await self._get_text(f"/api/v1/MessageProcessingLogs('{message_guid}')/ErrorInformation/$value")
            if error_text:
                return {"error_text": error_text}
        except Exception as e:
            logger.error(f"[SAP Poller] fetch_error_details error: {e}")
        return {}

    async def fetch_message_metadata(self, message_guid: str) -> Dict:
        try:
            payload = await self._get_json(
                f"/api/v1/MessageProcessingLogs('{message_guid}')",
                params={"$format": "json"},
            )
            return payload.get("d", payload)
        except Exception as e:
            logger.error(f"[SAP Poller] fetch_message_metadata error: {e}")
        return {}

    @staticmethod
    def normalize_runtime_artifact(raw: Dict[str, Any], error_text: str = "") -> Dict[str, Any]:
        artifact_id = raw.get("Id") or raw.get("Name") or raw.get("IntegrationFlowId") or ""
        artifact_name = raw.get("Name") or raw.get("IntegrationFlowName") or artifact_id
        status = raw.get("Status") or raw.get("DeployState") or raw.get("RuntimeStatus") or "UNKNOWN"
        return {
            "source_type": "RUNTIME_ARTIFACT",
            "message_guid": "",
            "artifact_id": artifact_id,
            "iflow_id": artifact_name,
            "sender": raw.get("Sender") or "",
            "receiver": raw.get("Receiver") or "",
            "status": status,
            "log_start": raw.get("CreatedAt") or raw.get("LastModified") or raw.get("DeployedOn") or "",
            "log_end": raw.get("LastModified") or raw.get("DeployedOn") or "",
            "error_message": (
                error_text
                or raw.get("ErrorInformation")
                or raw.get("Description")
                or raw.get("Message")
                or f"Runtime artifact is in status '{status}'."
            ),
            "correlation_id": raw.get("PackageId") or artifact_id,
            "package_id": raw.get("PackageId") or raw.get("PackageName") or "",
            "version": raw.get("Version") or "",
            "deployed_by": raw.get("DeployedBy") or raw.get("ModifiedBy") or "",
            "runtime_node": raw.get("Node") or raw.get("RuntimeNode") or "",
        }

    async def fetch_runtime_artifact_detail(self, artifact_id: str) -> Dict[str, Any]:
        try:
            payload = await self._get_json(
                f"/api/v1/IntegrationRuntimeArtifacts('{artifact_id}')",
                params={"$format": "json"},
            )
            return payload.get("d", payload)
        except Exception as e:
            logger.error(f"[SAP Poller] fetch_runtime_artifact_detail error: {e}")
            return {}

    async def fetch_runtime_artifact_error_detail(self, artifact_id: str) -> str:
        try:
            return (await self._get_text(
                f"/api/v1/IntegrationRuntimeArtifacts('{artifact_id}')/ErrorInformation/$value"
            )).strip()
        except Exception as e:
            logger.debug(f"[SAP Poller] fetch_runtime_artifact_error_detail error: {e}")
            return ""

    async def fetch_runtime_artifact_errors(self, limit: int = RUNTIME_ERROR_FETCH_LIMIT) -> List[Dict[str, Any]]:
        requested_limit = max(1, limit)
        query_variants = [
            {
                "$filter": "Status eq 'ERROR'",
                "$top": str(requested_limit),
                "$format": "json",
            },
            {
                "$filter": "Status eq 'Error'",
                "$top": str(requested_limit),
                "$format": "json",
            },
            {
                "$top": str(max(requested_limit * 3, requested_limit)),
                "$format": "json",
            },
        ]

        raw_results: List[Dict[str, Any]] = []
        for params in query_variants:
            try:
                payload = await self._get_json("/api/v1/IntegrationRuntimeArtifacts", params=params)
                raw_results = self._extract_results(payload)
                if raw_results:
                    break
            except Exception as e:
                logger.debug(f"[SAP Poller] runtime artifact query failed for {params}: {e}")

        filtered: List[Dict[str, Any]] = []
        for item in raw_results:
            status = str(item.get("Status") or item.get("DeployState") or item.get("RuntimeStatus") or "")
            has_error_text = bool(
                str(item.get("ErrorInformation") or item.get("Description") or item.get("Message") or "").strip()
            )
            if status.lower() == "error" or has_error_text:
                filtered.append(item)

        normalized: List[Dict[str, Any]] = []
        selected_items = filtered[:requested_limit]
        missing_detail_items: List[Dict[str, Any]] = []

        for item in selected_items:
            inline_error = str(
                item.get("ErrorInformation") or item.get("Description") or item.get("Message") or ""
            ).strip()
            if inline_error:
                normalized.append(self.normalize_runtime_artifact(item, error_text=inline_error))
            else:
                missing_detail_items.append(item)

        detail_budget = max(0, min(RUNTIME_ERROR_DETAIL_FETCH_LIMIT, len(missing_detail_items)))
        if detail_budget > 0:
            semaphore = asyncio.Semaphore(max(1, RUNTIME_ERROR_DETAIL_CONCURRENCY))

            async def _resolve_with_limit(item: Dict[str, Any]) -> Dict[str, Any]:
                artifact_id = item.get("Id") or item.get("Name") or item.get("IntegrationFlowId") or ""
                async with semaphore:
                    error_text = await self.fetch_runtime_artifact_error_detail(artifact_id) if artifact_id else ""
                return self.normalize_runtime_artifact(item, error_text=error_text)

            resolved = await asyncio.gather(*[
                _resolve_with_limit(item) for item in missing_detail_items[:detail_budget]
            ])
            normalized.extend(resolved)

        for item in missing_detail_items[detail_budget:]:
            normalized.append(self.normalize_runtime_artifact(item))

        logger.info(f"[SAP Poller] Fetched {len(normalized)} runtime artifact errors")
        return normalized

    async def fetch_cpi_error_inventory(
        self,
        message_limit: int = FAILED_MESSAGE_FETCH_LIMIT,
        artifact_limit: int = RUNTIME_ERROR_FETCH_LIMIT,
    ) -> Dict[str, Any]:
        failed_messages = await self.fetch_failed_messages(limit=message_limit)
        runtime_artifacts = await self.fetch_runtime_artifact_errors(limit=artifact_limit)

        failed_message_items = []
        for raw in failed_messages:
            guid = raw.get("MessageGuid", "")
            details = await self.fetch_error_details(guid) if guid else {}
            failed_message_items.append(self.normalize(raw, details))

        return {
            "summary": {
                "failed_message_count": len(failed_message_items),
                "runtime_artifact_error_count": len(runtime_artifacts),
                "total_errors": len(failed_message_items) + len(runtime_artifacts),
            },
            "failed_messages": failed_message_items,
            "runtime_artifacts": runtime_artifacts,
        }

    @staticmethod
    def normalize(raw: Dict, error_detail: Dict) -> Dict:
        return {
            "source_type": "MESSAGE_PROCESSING_LOG",
            "message_guid":   raw.get("MessageGuid", ""),
            "iflow_id":       raw.get("IntegrationFlowName", ""),
            "sender":         raw.get("Sender", ""),
            "receiver":       raw.get("Receiver", ""),
            "status":         raw.get("Status", "FAILED"),
            "log_start":      raw.get("LogStart", ""),
            "log_end":        raw.get("LogEnd", ""),
            "error_message":  error_detail.get("error_text", raw.get("CustomStatus", "")),
            "correlation_id": raw.get("CorrelationId", ""),
        }


# ─────────────────────────────────────────────
# MULTI MCP MANAGER
# ─────────────────────────────────────────────
class MultiMCP:
    def __init__(self):
        self.clients: Dict[str, Client]               = {}
        self.tools:   List[MCPTool]                   = []
        self._tool_index: Dict[tuple[str, str], MCPTool] = {}
        self.llm      = create_llm()
        self.agent    = None
        self.memory:  Dict[str, List[Dict]]           = {}
        self._autonomous_task: Optional[asyncio.Task] = None
        self._autonomous_running: bool                = False
        self.error_fetcher = SAPErrorFetcher()

    # ── helpers ──────────────────────────────

    def _safe_tool_name(self, server: str, tool_name: str) -> str:
        safe = re.sub(r"\W+", "_", f"{server}__{tool_name}").strip("_").lower()
        return safe[:64] if safe else f"{server}_tool"

    def _routing_hint_for_query(self, query: str) -> Optional[str]:
        q = query.lower()
        if any(k in q for k in ["document", "documentation", "spec", "template", "sap standard"]):
            return "documentation_mcp"
        if any(k in q for k in ["iflow", "integration flow", "integration suite", "deploy flow", "groovy"]):
            return "integration_suite"
        if any(k in q for k in ["test", "testing", "validate", "verification", "assertion"]):
            return "mcp_testing"
        return None

    def _is_integration_iflow_query(self, query: str) -> bool:
        return any(k in query.lower() for k in
                   ["iflow", "integration flow", "integration suite", "groovy script", "update-iflow", "script file"])

    def _is_documentation_query(self, query: str) -> bool:
        return any(k in query.lower() for k in
                   ["document", "documentation", "guide", "spec", "template", "sap standard", "adapter guide"])

    @staticmethod
    def _has_fix_intent(query: str) -> bool:
        """Returns True when the user's chat message is asking for a fix action."""
        q = query.lower()
        return any(kw in q for kw in FIX_INTENT_KEYWORDS)

    @staticmethod
    def is_transient_error(error_message: str) -> bool:
        msg = (error_message or "").lower()
        return any(marker in msg for marker in TRANSIENT_ERROR_MARKERS)

    def get_remediation_policy(self, incident: Dict, rca: Dict) -> Dict[str, Any]:
        error_type = rca.get("error_type") or incident.get("error_type") or "UNKNOWN_ERROR"
        policy = dict(REMEDIATION_POLICIES.get(error_type, REMEDIATION_POLICIES["UNKNOWN_ERROR"]))
        if error_type in {"BACKEND_ERROR", "CONNECTIVITY_ERROR"} and self.is_transient_error(
            incident.get("error_message", "")
        ):
            policy["action"] = "RETRY"
        return policy

    @staticmethod
    def incident_group_key(incident: Dict) -> str:
        iflow_id   = incident.get("iflow_id", "")
        error_type = incident.get("error_type", "")
        affected   = incident.get("affected_component", "") or incident.get("receiver", "") or "unknown"
        return hashlib.md5(f"{iflow_id}|{error_type}|{affected}".encode()).hexdigest()[:20]

    @staticmethod
    def raw_message_group_key(raw: Dict[str, Any]) -> str:
        return "|".join([
            str(raw.get("IntegrationFlowName", "")),
            str(raw.get("Sender", "")),
            str(raw.get("Receiver", "")),
            str(raw.get("CustomStatus", "")),
        ]).lower()

    def dedupe_raw_failed_messages(
        self,
        raw_errors: List[Dict[str, Any]],
        max_unique: int = MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE,
    ) -> List[Dict[str, Any]]:
        deduped: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()

        for raw in raw_errors:
            key = self.raw_message_group_key(raw)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(raw)
            if len(deduped) >= max(1, max_unique):
                break
        return deduped

    @staticmethod
    def has_actionable_fix(rca: Dict) -> bool:
        return bool((rca.get("proposed_fix") or "").strip() and (rca.get("root_cause") or "").strip())

    def should_auto_fix(self, incident: Dict, rca: Dict, policy: Dict[str, Any], confidence: float) -> bool:
        if not self.has_actionable_fix(rca):
            return False
        if policy["action"] in {"AUTO_FIX", "RETRY"}:
            threshold = SUGGEST_FIX_CONFIDENCE if AUTO_FIX_ALL_CPI_ERRORS else AUTO_FIX_CONFIDENCE
            return confidence >= threshold
        return False

    # ── tool output success checks ──────────

    @staticmethod
    def _update_succeeded(output: str) -> bool:
        """Returns True when update-iflow reported success."""
        text = (output or "").lower()
        return (
            '"status":200' in text
            or '"status": 200' in text
            or "successfully updated" in text
            or "update successful" in text
            or "saved successfully" in text
            or '"result":"ok"' in text
            or '"result": "ok"' in text
            or '"success":true' in text
            or '"success": true' in text
        )

    @staticmethod
    def _deploy_succeeded(output: str) -> bool:
        """Returns True when deploy-iflow reported success."""
        text = (output or "").lower()
        return (
            '"deploystatus":"success"' in text
            or '"deploystatus": "success"' in text
            or '"status":"success"' in text
            or '"status": "success"' in text
            or '"result":"success"' in text
            or '"result": "success"' in text
            or "deployed successfully" in text
            or "deployment successful" in text
            or "successfully deployed" in text
            or "deploy_success" in text
        )

    @staticmethod
    def _is_locked_error(output: str) -> bool:
        """Returns True when update-iflow failed because the artifact is locked/checked out."""
        text = (output or "").lower()
        return (
            "is locked" in text
            or "artifact as it is locked" in text
            or ("cannot update the artifact" in text and "locked" in text)
        )

    def _diagnose_timeout(self, steps: List[Dict], iflow_id: str) -> Dict[str, Any]:
        """Inspect partial step history to determine which pipeline stage timed out.

        Returns a result dict with accurate fix_applied / failed_stage / summary
        instead of a generic 'agent timed out' message.
        """
        tools_called = [str(s.get("tool", "")) for s in steps]

        deploy_called = any("deploy" in t for t in tools_called)
        update_called = any("update" in t and "iflow" in t for t in tools_called)
        get_called    = any("get" in t and "iflow" in t for t in tools_called)

        update_ok = False
        for s in steps:
            if "update" in str(s.get("tool", "")) and "iflow" in str(s.get("tool", "")):
                update_ok = self._update_succeeded(str(s.get("output", "")))

        if deploy_called:
            # deploy-iflow was called but never returned — CF SSE stream likely dropped
            return {
                "success": False,
                "fix_applied": True,   # update already succeeded before deploy was called
                "deploy_success": False,
                "failed_stage": "deploy",
                "technical_details": (
                    f"deploy-iflow tool was called for '{iflow_id}' but did not return within 600 s. "
                    "The SAP Cloud Foundry router likely closed the SSE stream while waiting for the "
                    "async deploy job to complete. The iFlow content was already updated successfully. "
                    "Check SAP CPI Monitor → Manage Integration Content to confirm deploy status."
                ),
                "summary": (
                    f"iFlow '{iflow_id}' content was updated but deployment confirmation timed out. "
                    "The deploy may have completed on SAP CPI — verify in Monitor → Manage Integration Content, "
                    "or use /retry (deploy-only) to redeploy."
                ),
            }

        if update_called and not update_ok:
            return {
                "success": False,
                "fix_applied": False,
                "deploy_success": False,
                "failed_stage": "update",
                "technical_details": (
                    f"update-iflow tool was called for '{iflow_id}' but timed out before returning a result. "
                    "The iFlow was not modified. Retry the full fix pipeline."
                ),
                "summary": (
                    f"iFlow update for '{iflow_id}' timed out before completing. "
                    "No changes were applied — retry the fix."
                ),
            }

        if get_called:
            return {
                "success": False,
                "fix_applied": False,
                "deploy_success": False,
                "failed_stage": "agent",
                "technical_details": (
                    f"get-iflow succeeded but the agent timed out before calling update-iflow for '{iflow_id}'. "
                    "The LLM may have stalled while analysing the iFlow XML. No changes were applied."
                ),
                "summary": (
                    f"iFlow '{iflow_id}' was downloaded but the fix agent timed out before applying changes. "
                    "No modifications were made — retry the fix."
                ),
            }

        # Nothing useful was called (timed out during first LLM inference)
        return {
            "success": False,
            "fix_applied": False,
            "deploy_success": False,
            "failed_stage": "agent",
            "technical_details": (
                f"Fix agent for '{iflow_id}' timed out before calling any MCP tools. "
                "The LLM call itself may have stalled. No changes were applied."
            ),
            "summary": (
                f"Fix agent timed out before starting tool calls for '{iflow_id}'. "
                "No changes were applied — retry the fix."
            ),
        }

    async def _poll_deploy_status(self, iflow_id: str, polls: int = 5, interval: float = 15.0) -> Dict[str, Any]:
        """
        After a deploy-iflow timeout, poll IntegrationRuntimeArtifacts directly
        to check whether SAP CPI completed the deploy independently.

        Returns a result dict:
          - deploy_confirmed=True  → iFlow is STARTED/DEPLOYED — treat as success
          - deploy_confirmed=False → still deploying, error, or unreachable
          - status                 → raw status string from SAP CPI
        """
        _STARTED = {"started", "deployed", "active", "running"}
        _FAILED  = {"error", "failed", "stopped"}

        for attempt in range(1, polls + 1):
            try:
                detail = await self.error_fetcher.fetch_runtime_artifact_detail(iflow_id)
                raw_status = str(
                    detail.get("Status")
                    or detail.get("DeployState")
                    or detail.get("RuntimeStatus")
                    or ""
                ).lower().strip()

                logger.info(
                    "[DEPLOY_POLL] iflow=%s attempt=%d/%d status=%s",
                    iflow_id, attempt, polls, raw_status,
                )

                if raw_status in _STARTED:
                    return {"deploy_confirmed": True, "status": raw_status}
                if raw_status in _FAILED:
                    return {"deploy_confirmed": False, "status": raw_status}
                # Status is STARTING / DEPLOYING / unknown — wait and retry
            except Exception as exc:
                logger.warning("[DEPLOY_POLL] iflow=%s attempt=%d error: %s", iflow_id, attempt, exc)

            if attempt < polls:
                await asyncio.sleep(interval)

        return {"deploy_confirmed": False, "status": "timeout"}

    def evaluate_fix_result(self, steps: List[Dict], answer: str) -> Dict[str, Any]:
        update_ok = False
        deploy_ok = False
        update_output = ""
        deploy_output = ""

        def compact(text: str) -> str:
            cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
            return cleaned[:500]

        for step in steps:
            tool_name = str(step.get("tool", ""))
            output    = str(step.get("output", ""))
            if "update_iflow" in tool_name or "update-iflow" in tool_name:
                update_output = output
                update_ok     = self._update_succeeded(output)
            elif "deploy_iflow" in tool_name or "deploy-iflow" in tool_name:
                deploy_output = output
                deploy_ok     = self._deploy_succeeded(output)

        if not update_ok:
            is_locked = self._is_locked_error(update_output)
            return {
                "success": False,
                "fix_applied": False,
                "deploy_success": False,
                "failed_stage": "locked" if is_locked else "update",
                "technical_details": compact(update_output),
                "summary": (
                    "iFlow is locked in SAP CPI Integration Flow Designer. "
                    "The artifact is currently checked out by another user or session. "
                    "Please cancel/close the checkout in SAP CPI and retry the fix."
                    if is_locked else
                    "iFlow update failed during the SAP Integration Suite update step."
                ),
                "failed_steps": ["update-iflow"],
            }
        if not deploy_ok:
            return {
                "success": False,
                "fix_applied": True,
                "deploy_success": False,
                "failed_stage": "deploy",
                "technical_details": compact(deploy_output),
                "summary": "iFlow content was updated but deployment did not complete successfully.",
                "failed_steps": ["deploy-iflow"],
            }
        return {
            "success": True,
            "fix_applied": True,
            "deploy_success": True,
            "failed_stage": None,
            "technical_details": "",
            "summary": f"iFlow updated and deployed successfully. {compact(answer)}",
            "failed_steps": [],
        }

    @staticmethod
    def determine_post_fix_status(
        fix_success: bool,
        policy: Dict[str, Any],
        retry_result: Optional[Dict[str, Any]] = None,
        human_approved: bool = False,
        failed_stage: str = "",
    ) -> str:
        if not fix_success:
            if failed_stage in ("deploy", "deploy_validation"):
                return "FIX_FAILED_DEPLOY"
            if failed_stage in ("update", "get"):
                return "FIX_FAILED_UPDATE"
            return "FIX_FAILED"
        if policy.get("action") == "RETRY":
            if retry_result and (retry_result.get("success") or retry_result.get("skipped")):
                return "HUMAN_INITIATED_FIX" if human_approved else "FIX_VERIFIED"
            return "FIX_DEPLOYED"
        return "HUMAN_INITIATED_FIX" if human_approved else "FIX_VERIFIED"

    def get_retry_tool_candidates(self) -> List[str]:
        return [
            t.name for t in self.tools
            if any(tok in f"{t.name} {t.description}".lower() for tok in ("retry", "replay", "resubmit"))
        ]

    def get_mcp_tool(self, server: str, mcp_tool_name: str) -> Optional[MCPTool]:
        return self._tool_index.get((server, mcp_tool_name))

    def has_mcp_tool(self, server: str, mcp_tool_name: str) -> bool:
        return self.get_mcp_tool(server, mcp_tool_name) is not None

    @staticmethod
    def _tool_model_fields(model: Type[BaseModel]) -> Dict[str, Any]:
        if hasattr(model, "model_fields"):
            return getattr(model, "model_fields")
        if hasattr(model, "__fields__"):
            return getattr(model, "__fields__")
        return {}

    def validate_required_tools(self, server: str, tool_names: List[str]) -> List[str]:
        return [tool_name for tool_name in tool_names if not self.has_mcp_tool(server, tool_name)]

    def get_tool_field_names(self, server: str, mcp_tool_name: str) -> List[str]:
        tool = self.get_mcp_tool(server, mcp_tool_name)
        if not tool:
            return []
        return list(self._tool_model_fields(tool.args_schema).keys())

    def _infer_tool_args(self, server: str, mcp_tool_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.get_mcp_tool(server, mcp_tool_name)
        if not tool:
            return {}

        alias_map = {
            "iflow_id": ["iflow_id", "artifact_id", "integration_flow_id", "id", "name"],
            "id": ["iflow_id", "artifact_id", "id", "name"],
            "name": ["iflow_id", "artifact_name", "name"],
            "artifact_id": ["artifact_id", "iflow_id", "id", "name"],
            "artifact_name": ["artifact_name", "iflow_id", "name"],
            "integrationflowid": ["iflow_id", "artifact_id", "id", "name"],
            "integrationflowname": ["iflow_id", "artifact_name", "name"],
            "package_id": ["package_id", "package", "package_name"],
            "package_name": ["package_name", "package", "package_id"],
            "message_guid": ["message_guid", "message_id", "mpl_id"],
            "message_id": ["message_guid", "message_id", "mpl_id"],
            "mpl_id": ["mpl_id", "message_guid", "message_id"],
        }

        args: Dict[str, Any] = {}
        for field_name in self._tool_model_fields(tool.args_schema).keys():
            candidates = [field_name] + alias_map.get(field_name.lower(), [])
            for candidate in candidates:
                if candidate in context and context[candidate] not in (None, ""):
                    args[field_name] = context[candidate]
                    break
        return args

    async def execute_integration_tool(self, mcp_tool_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.get_mcp_tool("integration_suite", mcp_tool_name)
        if not tool:
            return {
                "success": False,
                "tool": mcp_tool_name,
                "args": {},
                "output": "",
                "error": f"Required MCP tool not available: {mcp_tool_name}",
            }

        args = self._infer_tool_args("integration_suite", mcp_tool_name, context)
        missing_required = []
        for field_name, field in self._tool_model_fields(tool.args_schema).items():
            is_required = False
            if hasattr(field, "is_required"):
                try:
                    is_required = bool(field.is_required())
                except Exception:
                    is_required = False
            elif getattr(field, "required", False):
                is_required = True
            if is_required and field_name not in args:
                missing_required.append(field_name)

        if missing_required:
            return {
                "success": False,
                "tool": mcp_tool_name,
                "args": args,
                "output": "",
                "error": f"Missing required arguments for {mcp_tool_name}: {', '.join(missing_required)}",
            }

        output = await self.execute("integration_suite", mcp_tool_name, args)
        return {
            "success": not str(output).startswith("ERROR:"),
            "tool": mcp_tool_name,
            "args": args,
            "output": output,
            "error": "" if not str(output).startswith("ERROR:") else str(output),
        }

    async def verify_iflow_exists(self, iflow_id: str) -> Dict[str, Any]:
        """
        Check if an iFlow exists in SAP CPI before attempting to fix it.
        Uses the MCP get-iflow tool for verification.
        
        Returns:
            {
                "exists": bool,
                "verified": bool,
                "status_code": int,
                "message": str,
                "detail": Dict (if exists)
            }
        """
        if not iflow_id:
            logger.warning("[VERIFY] No iFlow ID provided for verification")
            return {
                "exists": False,
                "verified": False,
                "status_code": 0,
                "message": "No iFlow ID provided",
                "detail": {}
            }

        try:
            logger.info("=" * 80)
            logger.info(f"[VERIFY] Starting iFlow existence check via MCP get-iflow tool")
            logger.info(f"[VERIFY] Target iFlow ID: {iflow_id}")
            logger.info("=" * 80)
            
            # Use the MCP get-iflow tool to verify existence
            result = await self.execute_integration_tool("get-iflow", {"id": iflow_id})
            
            logger.info(f"[VERIFY] MCP tool execution completed - Success: {result.get('success')}")
            
            if result.get("success"):
                # Tool succeeded - iFlow exists
                output = result.get("output", {})
                
                # Parse the output to extract iFlow details
                iflow_data = {}
                if isinstance(output, dict):
                    iflow_data = output
                elif isinstance(output, str):
                    try:
                        iflow_data = json.loads(output)
                    except Exception as parse_err:
                        logger.debug(f"[VERIFY] Could not parse output as JSON: {parse_err}")
                        iflow_data = {"raw_output": output}
                
                logger.info("=" * 80)
                logger.info(f"[VERIFY] ✅ RESULT: iFlow EXISTS")
                logger.info(f"[VERIFY] iFlow ID: {iflow_id}")
                logger.info(f"[VERIFY] Status: VERIFIED")
                logger.info(f"[VERIFY] Data size: {len(str(iflow_data))} chars")
                logger.info("=" * 80)
                
                return {
                    "exists": True,
                    "verified": True,
                    "status_code": 200,
                    "message": f"iFlow '{iflow_id}' exists in SAP CPI",
                    "detail": iflow_data
                }
            else:
                # Tool failed - check if it's a 404 (not found) or other error
                error_msg = str(result.get("output", ""))
                error_msg_lower = error_msg.lower()
                
                logger.warning(f"[VERIFY] MCP tool returned error: {error_msg[:300]}")
                
                # Check for 404 indicators in the error message
                if "404" in error_msg_lower or "not found" in error_msg_lower or "does not exist" in error_msg_lower:
                    logger.info("=" * 80)
                    logger.warning(f"[VERIFY] ❌ RESULT: iFlow DOES NOT EXIST (404)")
                    logger.warning(f"[VERIFY] iFlow ID: {iflow_id}")
                    logger.warning(f"[VERIFY] Status: DELETED/NOT FOUND")
                    logger.warning(f"[VERIFY] Error indicators: 404/not found/does not exist")
                    logger.info("=" * 80)
                    
                    return {
                        "exists": False,
                        "verified": True,
                        "status_code": 404,
                        "message": f"iFlow '{iflow_id}' does not exist in SAP CPI (may have been deleted)",
                        "detail": {}
                    }
                else:
                    # Non-404 error — cannot confirm iFlow exists; block fix to be safe
                    logger.info("=" * 80)
                    logger.warning(f"[VERIFY] ⚠️  RESULT: VERIFICATION INCONCLUSIVE — BLOCKING FIX")
                    logger.warning(f"[VERIFY] iFlow ID: {iflow_id}")
                    logger.warning(f"[VERIFY] Status: UNVERIFIED (treating as missing — will not proceed)")
                    logger.warning(f"[VERIFY] Error: {error_msg[:200]}")
                    logger.info("=" * 80)

                    return {
                        "exists": False,
                        "verified": False,
                        "status_code": 0,
                        "message": f"iFlow '{iflow_id}' existence could not be confirmed — fix blocked. Error: {error_msg[:200]}",
                        "detail": {}
                    }
        except Exception as e:
            logger.info("=" * 80)
            logger.error(f"[VERIFY] ❌ EXCEPTION during verification — BLOCKING FIX")
            logger.error(f"[VERIFY] iFlow ID: {iflow_id}")
            logger.error(f"[VERIFY] Exception: {type(e).__name__}: {str(e)}")
            logger.info("=" * 80)

            return {
                "exists": False,
                "verified": False,
                "status_code": 0,
                "message": f"iFlow '{iflow_id}' existence could not be verified due to an error — fix blocked. {type(e).__name__}: {str(e)}",
                "detail": {}
            }

    async def get_deploy_error_details(self, iflow_id: str) -> str:
        if not iflow_id:
            return "No iFlow ID available to retrieve deploy error details."

        deploy_error = await self.execute_integration_tool(
            "get-deploy-error",
            {
                "iflow_id": iflow_id,
                "artifact_id": iflow_id,
                "artifact_name": iflow_id,
                "name": iflow_id,
            },
        )
        if deploy_error.get("success") and deploy_error.get("output", "").strip():
            return str(deploy_error["output"]).strip()

        for artifact_ref in (iflow_id, iflow_id.replace("_", "%5F")):
            detail = await self.error_fetcher.fetch_runtime_artifact_error_detail(artifact_ref)
            if detail:
                return detail

        artifact_detail = await self.error_fetcher.fetch_runtime_artifact_detail(iflow_id)
        status = artifact_detail.get("Status") or artifact_detail.get("DeployState") or ""
        error_info = artifact_detail.get("ErrorInformation") or artifact_detail.get("Description") or ""
        if status or error_info:
            return f"Status={status} ErrorInformation={error_info}".strip()

        return (
            f"Could not retrieve deploy error detail for '{iflow_id}'. "
            f"Check SAP CPI monitoring manually: {SAP_HUB_TENANT_URL}/itspaces/shell/monitor/messages"
        )

    # ── iFlow unlock helper ───────────────────────────────────────────────────

    async def _try_unlock_iflow_via_api(self, iflow_id: str) -> Dict[str, Any]:
        """
        Attempt to cancel checkout (force-unlock) for a locked SAP CPI iFlow.

        Tries three methods in order:
          1. DELETE .../checkout          — standard cancel-checkout OData action
          2. POST  .../CancelCheckout     — alternate action name in some tenants
          3. MCP cancel/unlock tool       — if integration_suite MCP exposes one

        Returns {"success": bool, "status_code": int, "message": str}
        """
        if not iflow_id:
            return {"success": False, "status_code": 0, "message": "No iFlow ID provided"}

        try:
            token = await self.error_fetcher._get_token()
        except Exception as e:
            logger.warning(f"[UNLOCK] Could not obtain token for unlock: {e}")
            return {"success": False, "status_code": 0, "message": f"Token error: {e}"}

        base = SAP_HUB_TENANT_URL.rstrip("/")
        artifact_path = f"/api/v1/IntegrationDesigntimeArtifacts(Id='{iflow_id}',Version='active')"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Track whether any endpoint returned a real error (not just 404/no-lock-found)
        found_lock = False

        # Method 1: DELETE .../checkout
        try:
            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                resp = await client.delete(f"{base}{artifact_path}/checkout", headers=headers)
            if resp.status_code in (200, 202, 204):
                logger.info(f"[UNLOCK] Unlocked via DELETE /checkout: {iflow_id} (HTTP {resp.status_code})")
                return {"success": True, "status_code": resp.status_code,
                        "message": "Unlock successful via DELETE /checkout"}
            if resp.status_code == 404:
                logger.debug(f"[UNLOCK] DELETE /checkout → 404 for {iflow_id} (no active checkout)")
            else:
                found_lock = True
                logger.debug(f"[UNLOCK] DELETE /checkout → {resp.status_code} for {iflow_id}: {resp.text[:100]}")
        except Exception as e:
            logger.debug(f"[UNLOCK] DELETE /checkout exception for {iflow_id}: {e}")

        # Method 2: POST .../CancelCheckout
        try:
            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                resp = await client.post(f"{base}{artifact_path}/CancelCheckout", headers=headers)
            if resp.status_code in (200, 202, 204):
                logger.info(f"[UNLOCK] Unlocked via POST /CancelCheckout: {iflow_id} (HTTP {resp.status_code})")
                return {"success": True, "status_code": resp.status_code,
                        "message": "Unlock successful via POST /CancelCheckout"}
            if resp.status_code == 404:
                logger.debug(f"[UNLOCK] POST /CancelCheckout → 404 for {iflow_id} (no active checkout)")
            else:
                found_lock = True
                logger.debug(f"[UNLOCK] POST /CancelCheckout → {resp.status_code} for {iflow_id}: {resp.text[:100]}")
        except Exception as e:
            logger.debug(f"[UNLOCK] POST /CancelCheckout exception for {iflow_id}: {e}")

        # Method 3: MCP tool with cancel/checkout/unlock in name
        unlock_tools = [
            t.mcp_tool_name for t in self.tools
            if t.server == "integration_suite" and any(
                kw in f"{t.name} {t.mcp_tool_name}".lower()
                for kw in ("cancel", "checkout", "unlock", "force_unlock", "discard")
            )
        ]
        if unlock_tools:
            tool_name = unlock_tools[0]
            logger.debug(f"[UNLOCK] Trying MCP unlock tool '{tool_name}' for {iflow_id}")
            try:
                out = await self.execute(
                    "integration_suite", tool_name,
                    {"iflow_id": iflow_id, "id": iflow_id, "artifact_id": iflow_id},
                )
                if "error" not in str(out).lower() and "fail" not in str(out).lower():
                    logger.info(f"[UNLOCK] MCP unlock succeeded via {tool_name}: {str(out)[:100]}")
                    return {"success": True, "status_code": 200,
                            "message": f"MCP unlock via {tool_name}"}
                logger.debug(f"[UNLOCK] MCP tool {tool_name} response: {str(out)[:200]}")
            except Exception as e:
                logger.debug(f"[UNLOCK] MCP tool {tool_name} error: {e}")

        # Only warn if we actually hit a non-404 response (real lock refused to clear)
        if found_lock:
            logger.warning(f"[UNLOCK] Artifact is locked and could not be unlocked: {iflow_id}")
        else:
            logger.debug(f"[UNLOCK] No active checkout found for {iflow_id} — proceeding (artifact may not be locked)")

        return {
            "success": False,
            "status_code": 0,
            # Neutral message: 404 = no checkout to cancel, not necessarily locked
            "message": (
                f"No active checkout API lock found for '{iflow_id}'. "
                "If the artifact is locked via browser edit session, "
                "please close the edit session in SAP CPI Integration Flow Designer and retry."
            ) if not found_lock else (
                f"Could not unlock '{iflow_id}' — the artifact checkout refused to cancel. "
                "Please manually cancel the checkout in SAP CPI Integration Flow Designer "
                "(Design tab → open iFlow → Cancel Checkout) and retry."
            ),
        }

    # ── MCP connection ────────────────────────

    async def connect(self):
        for name, url in MCP_SERVERS.items():
            try:
                opts = TRANSPORT_OPTIONS.get(name, {})
                def factory(**kw):
                    kw["verify"]  = opts.get("verify", True)
                    kw["timeout"] = opts.get("timeout", 30)
                    return httpx.AsyncClient(**kw)
                transport          = StreamableHttpTransport(url, httpx_client_factory=factory)
                self.clients[name] = Client(transport=transport)
                logger.info(f"[OK] Connected → {name}")
            except Exception as e:
                logger.error(f"[FAIL] {name} → {e}")

    async def discover_tools(self):
        self.tools.clear()
        self._tool_index.clear()
        used_names = set()

        async def load(server, client):
            server_tools = []
            async with client:
                raw = await client.list_tools()
                logger.info(f"[MCP] Discovering tools from server: {server}")
                for t in raw:
                    schema          = t.inputSchema or {}
                    Model           = build_model(t.name + "_Input", schema)
                    agent_tool_name = self._safe_tool_name(server, t.name)
                    suffix = 2
                    while agent_tool_name in used_names:
                        agent_tool_name = f"{agent_tool_name}_{suffix}"
                        suffix += 1
                    used_names.add(agent_tool_name)
                    desc_prefix = f"[server={server}] {SERVER_ROUTING_GUIDE.get(server, '')}".strip()
                    full_desc   = f"{desc_prefix} Original tool: {t.name}. {t.description or ''}".strip()
                    self.tools.append(MCPTool(
                        name=agent_tool_name, description=full_desc,
                        args_schema=Model, server=server,
                        mcp_tool_name=t.name, manager=self,
                    ))
                    self._tool_index[(server, t.name)] = self.tools[-1]
                    server_tools.append(t.name)
                
                logger.info(f"[MCP] Loaded {len(server_tools)} tools from {server}: {', '.join(server_tools[:5])}{'...' if len(server_tools) > 5 else ''}")

        await asyncio.gather(*(load(n, c) for n, c in self.clients.items()))
        
        # Log summary by server
        tools_by_server = {}
        for tool in self.tools:
            tools_by_server.setdefault(tool.server, []).append(tool.mcp_tool_name)
        
        logger.info("=" * 80)
        logger.info(f"[MCP] TOOL DISCOVERY COMPLETE - Total: {len(self.tools)} tools loaded")
        logger.info("=" * 80)
        for server, tool_names in tools_by_server.items():
            logger.info(f"[MCP] {server}: {len(tool_names)} tools")
            for tool_name in tool_names:
                logger.info(f"[MCP]   ✓ {tool_name}")
        logger.info("=" * 80)

    async def execute(self, server, tool, args):
        # ── Pre-call validation for update-iflow ─────────────────────────────
        if tool == "update-iflow":
            val_errors = validate_before_update_iflow(args)
            if val_errors:
                error_block = "\n".join(f"  • {e}" for e in val_errors)
                logger.warning(f"[VALIDATOR] update-iflow blocked for iFlow. Issues:\n{error_block}")
                return (
                    "VALIDATION FAILED — update-iflow was NOT sent to SAP CPI.\n"
                    "Fix the following issues and call update-iflow again with corrected values:\n"
                    + error_block
                )

        client = self.clients[server]
        for attempt in range(MAX_RETRIES):
            try:
                async with client:
                    res = await client.call_tool(tool, args)
                out = []
                for c in res.content:
                    if getattr(c, "text", None):
                        out.append(c.text)
                    elif getattr(c, "json", None):
                        out.append(json.dumps(c.json, indent=2))
                    else:
                        out.append(str(c))
                return "\n".join(out)
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    return f"ERROR: {e}"
                await asyncio.sleep(1)

    async def build_agent(self):
        if not self.tools:
            raise RuntimeError("No MCP tools discovered.")
        routing_text = "\n".join(f"- {n}: {g}" for n, g in SERVER_ROUTING_GUIDE.items())
        self.agent = create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=f"""
You are an SAP MCP automation agent.
Select tools strictly by server responsibility.
Server routing rules:
{routing_text}

Routing rules:
- SAP-standard documentation → documentation_mcp
- iFlow creation/configuration/fix/deploy → integration_suite
- Testing/validation → mcp_testing
- Do not mix servers unless explicitly required.

Must do:
- Use this script: {GROOVY_STRIPE_HTTP_ADAPTER} for Stripe Groovy scripts only.
- Use this script: {GROOVY_WOOCOMMERCE_HTTP_ADAPTER} for WooCommerce Groovy scripts only.
- For other adapters, use the MCP tool for Groovy script creation.

CRITICAL — When asked to FIX and DEPLOY an iFlow:
1. ALWAYS call get-iflow first to get current config.
2. ALWAYS call update-iflow after applying the fix.
3. ALWAYS call deploy-iflow after a successful update.
4. NEVER stop after just the update — deploy is MANDATORY.
5. Report the actual tool response, do not fabricate success.

Execution policy:
- Plan first, then execute tools in order.
- Do not show method: POST in result summary.
- End with a structured summary including fix status and deploy status.
- Do not deploy the iFlow without confirmation from the user EXCEPT when the system prompt explicitly instructs you to fix and deploy autonomously.
""",
        )

    def update_memory(self, session_id, user, assistant):
        session_memory = self.memory.setdefault(session_id, [])
        session_memory.append({"role": "user",      "content": user})
        session_memory.append({"role": "assistant", "content": assistant})
        if len(session_memory) > MEMORY_LIMIT:
            self.memory[session_id] = session_memory[-MEMORY_LIMIT:]

    # ══════════════════════════════════════════
    # CHATBOT: ASK (general)
    # ══════════════════════════════════════════

    async def ask(self, query: str, user_id: str, session_id: str, timestamp: str):
        user_memory = self.memory.setdefault(session_id, [])
        tracker     = TestExecutionTracker(user_id, query, timestamp)
        logger_cb   = StepLogger(tracker)

        route_server = self._routing_hint_for_query(query)
        guidance = ""
        if route_server:
            guidance = (f"\n\nRouting hint: This request best matches `{route_server}`. "
                        f"{SERVER_ROUTING_GUIDE.get(route_server, '')}")
        if self._is_integration_iflow_query(query):
            guidance += f"\n\n{CPI_IFLOW_GROOVY_RULES}"
        if self._is_documentation_query(query):
            guidance += f"\n\n{SAP_DOC_TEMPLATE}"

        messages = list(user_memory)
        messages.append({"role": "user", "content": query + guidance})

        result = {}
        for attempt in range(3):
            try:
                result = await self.agent.ainvoke(
                    {"messages": messages},
                    config={"callbacks": [logger_cb]},
                )
                break
            except Exception as e:
                if attempt < 2 and "model produced invalid content" in str(e).lower():
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise

        AI_UNWANTED    = {"additional_kwargs","response_metadata","usage_metadata","id","invalid_tool_calls","name"}
        TOOL_UNWANTED  = {"additional_kwargs","response_metadata","tool_call_id","artifact","id"}
        HUMAN_UNWANTED = {"additional_kwargs","response_metadata","id","name"}

        structured_messages = []
        for idx, msg in enumerate(result["messages"]):
            msg_dict = msg.model_dump()
            unwanted = {"ai": AI_UNWANTED, "tool": TOOL_UNWANTED, "human": HUMAN_UNWANTED}.get(
                msg_dict.get("type"), set())
            for k in unwanted:
                msg_dict.pop(k, None)
            msg_dict["index"] = idx
            structured_messages.append(msg_dict)

        final_msg   = result["messages"][-1]
        answer_text = final_msg.content if hasattr(final_msg, "content") else str(final_msg)

        self.update_memory(session_id, query, answer_text)
        try:
            updateTestSuiteStatus(test_suite_id=tracker.test_suite_id, status="COMPLETED")
        except Exception:
            pass

        return {
            "answer":          answer_text,
            "steps":           logger_cb.steps,
            "agent_work_logs": structured_messages,
        }

    # ══════════════════════════════════════════
    # CHATBOT: FIX + DEPLOY via chat  ← KEY NEW METHOD
    # Called when user's chat message has fix intent AND an iflow_id is known
    # ══════════════════════════════════════════

    async def ask_fix_and_deploy(
        self,
        iflow_id: str,
        error_message: str,
        proposed_fix: str,
        root_cause: str,
        error_type: str,
        affected_component: str,
        user_id: str,
        session_id: str,
        timestamp: str,
        progress_fn=None,
        message_guid: str = "",
    ) -> Dict[str, Any]:
        """
        Builds a strict fix+deploy prompt and invokes the agent.
        Verifies both update and deploy tool outputs.
        Returns a structured result dict.
        """
        missing_tools = self.validate_required_tools(
            "integration_suite",
            ["get-iflow", "update-iflow", "deploy-iflow"],
        )
        if missing_tools:
            return {
                "success": False,
                "fix_applied": False,
                "deploy_success": False,
                "failed_stage": "tool_validation",
                "technical_details": f"Missing required MCP tools: {', '.join(missing_tools)}",
                "summary": "Fix execution cannot start because required Integration Suite MCP tools are unavailable.",
                "steps": [],
            }

        # ── Pre-flight: silently attempt to cancel any existing checkout lock ──
        if iflow_id:
            unlock_result = await self._try_unlock_iflow_via_api(iflow_id)
            if unlock_result["success"]:
                logger.info(f"[FIX_DEPLOY] Pre-unlock cleared checkout lock for '{iflow_id}'")
            else:
                # Non-fatal: 404 = no checkout to cancel (artifact not in edit mode); proceed
                logger.debug(f"[FIX_DEPLOY] Pre-unlock no-op for '{iflow_id}': {unlock_result['message']}")

        tracker   = TestExecutionTracker(user_id, f"fix:{iflow_id}", timestamp)
        logger_cb = StepLogger(tracker, progress_fn=progress_fn)

        sig = self.error_signature(iflow_id, error_type or "UNKNOWN", error_message)
        patterns = get_similar_patterns(sig)
        if patterns:
            best = patterns[0]
            pattern_history = (
                f"\n=== HISTORICAL FIX (applied {best.get('applied_count', 1)}x, outcome=SUCCESS) ===\n"
                f"Known fix that worked before: {best.get('fix_applied', '')}\n"
                f"Root cause at the time:       {best.get('root_cause', '')}\n"
                f"Use this as a reference — apply the same change if the root cause matches.\n"
            )
        else:
            pattern_history = ""

        error_type_guidance = ERROR_TYPE_FIX_GUIDANCE.get(error_type or "", "")

        prompt = FIX_AND_DEPLOY_PROMPT_TEMPLATE.format(
            iflow_id=iflow_id,
            error_type=error_type or "UNKNOWN",
            error_message=(error_message or "")[:500],
            message_guid=message_guid or "N/A",
            root_cause=root_cause or error_message,
            proposed_fix=proposed_fix or f"Investigate and fix the error: {error_message}",
            affected_component=affected_component or "unknown",
            pattern_history=pattern_history,
            error_type_guidance=error_type_guidance,
            groovy_rules=CPI_IFLOW_GROOVY_RULES,
            iflow_xml_patterns=CPI_IFLOW_XML_PATTERNS,
        )

        messages = [{"role": "user", "content": prompt}]

        for attempt in range(3):
            try:
                result = await asyncio.wait_for(
                    self.agent.ainvoke(
                        {"messages": messages},
                        config={"callbacks": [logger_cb], "recursion_limit": 18},
                    ),
                    timeout=600.0,
                )
                break
            except asyncio.TimeoutError:
                diagnosis = self._diagnose_timeout(logger_cb.steps, iflow_id)
                logger.error(
                    "[FIX_DEPLOY] agent timed out after 600s | iflow=%s | stage=%s | fix_applied=%s | detail=%s",
                    iflow_id, diagnosis["failed_stage"], diagnosis["fix_applied"],
                    diagnosis["technical_details"][:200],
                )
                # Deploy was called but SAP CPI SSE stream dropped — poll directly to
                # check whether the deploy completed on SAP's side before giving up.
                if diagnosis["failed_stage"] == "deploy" and iflow_id:
                    logger.info(
                        "[FIX_DEPLOY] Deploy timed out — polling SAP CPI runtime status for '%s'", iflow_id
                    )
                    poll = await self._poll_deploy_status(iflow_id)
                    if poll["deploy_confirmed"]:
                        logger.info(
                            "[FIX_DEPLOY] Post-timeout poll confirmed iFlow '%s' is %s — marking as success",
                            iflow_id, poll["status"],
                        )
                        return {
                            **diagnosis,
                            "success":        True,
                            "deploy_success": True,
                            "failed_stage":   None,
                            "summary": (
                                f"iFlow '{iflow_id}' updated and deployed successfully. "
                                f"Deploy confirmation arrived via runtime status poll (SAP CPI status: {poll['status']})."
                            ),
                            "technical_details": (
                                "deploy-iflow SSE stream timed out, but a direct poll of "
                                f"IntegrationRuntimeArtifacts confirmed status '{poll['status']}'."
                            ),
                            "steps": logger_cb.steps,
                        }
                    logger.warning(
                        "[FIX_DEPLOY] Post-timeout poll for '%s' returned status='%s' — deploy not confirmed",
                        iflow_id, poll["status"],
                    )
                return {**diagnosis, "steps": logger_cb.steps}
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                logger.error(f"[FIX_DEPLOY] agent error: {e}")
                return {
                    "success": False, "fix_applied": False, "deploy_success": False,
                    "failed_stage": "agent",
                    "technical_details": str(e),
                    "summary": "Fix execution failed while generating or applying the iFlow change plan.",
                    "steps": logger_cb.steps,
                }

        final_msg = result["messages"][-1]
        answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)

        evaluation = self.evaluate_fix_result(logger_cb.steps, answer)

        # ── If the artifact was locked mid-run, unlock and retry once ─────────────────
        if evaluation.get("failed_stage") == "locked" and iflow_id:
            logger.info(f"[FIX_DEPLOY] Locked artifact detected mid-run — attempting unlock + retry: {iflow_id}")
            unlock_retry = await self._try_unlock_iflow_via_api(iflow_id)
            if unlock_retry["success"]:
                logger.info(f"[FIX_DEPLOY] Unlock succeeded — retrying fix agent for {iflow_id}")
                tracker2   = TestExecutionTracker(user_id, f"fix_retry:{iflow_id}", timestamp)
                logger_cb2 = StepLogger(tracker2, progress_fn=progress_fn)
                try:
                    result2 = await asyncio.wait_for(
                        self.agent.ainvoke(
                            {"messages": messages},
                            config={"callbacks": [logger_cb2], "recursion_limit": 12},
                        ),
                        timeout=480.0,
                    )
                    final_msg2 = result2["messages"][-1]
                    answer2    = final_msg2.content if hasattr(final_msg2, "content") else str(final_msg2)
                    eval2      = self.evaluate_fix_result(logger_cb2.steps, answer2)
                    if eval2.get("success") or eval2.get("failed_stage") != "locked":
                        evaluation = eval2
                        logger_cb  = logger_cb2
                        answer     = answer2
                        logger.info(
                            f"[FIX_DEPLOY] Retry result: fix_applied={eval2.get('fix_applied')} "
                            f"deploy_success={eval2.get('deploy_success')}"
                        )
                except Exception as retry_e:
                    logger.error(f"[FIX_DEPLOY] Retry after unlock failed: {retry_e}")
            else:
                # Unlock failed — enrich the summary with an actionable human-readable message
                locked_msg = (
                    f"The iFlow '{iflow_id}' is currently open for editing in SAP Integration Suite. "
                    "To apply this fix automatically:\n"
                    "  1. Ask the user/team to close the iFlow edit session (click Cancel in IFD).\n"
                    "  2. The next autonomous cycle or manual retry will apply the fix automatically.\n"
                    f"Technical detail: {unlock_retry['message']}"
                )
                evaluation["summary"] = (
                    f"Fix could not be applied — iFlow is locked by an active browser edit session.\n\n"
                    f"{locked_msg}"
                )
                evaluation["technical_details"] = locked_msg

        # ── Deploy-error self-correction passes (up to 3 attempts) ─────────────
        # If update succeeded but deploy failed, fetch the exact validation errors
        # and give the agent up to 3 targeted correction passes to resolve them.
        if (
            iflow_id
            and evaluation.get("fix_applied")
            and not evaluation.get("deploy_success")
            and evaluation.get("failed_stage") == "deploy"
        ):
            # Before attempting correction: check whether SAP CPI already finished
            # deploying (deploy-iflow may have returned an unrecognised response format
            # even though the deploy actually succeeded).
            _pre_poll = await self._poll_deploy_status(iflow_id, polls=3, interval=10.0)
            if _pre_poll["deploy_confirmed"]:
                logger.info(
                    "[FIX_DEPLOY] Pre-correction poll confirmed '%s' is already %s — skipping correction passes",
                    iflow_id, _pre_poll["status"],
                )
                evaluation["success"]          = True
                evaluation["deploy_success"]   = True
                evaluation["failed_stage"]     = None
                evaluation["summary"]          = (
                    f"iFlow '{iflow_id}' updated and deployed successfully. "
                    f"Deployment confirmed via runtime status poll (status: {_pre_poll['status']})."
                )
                evaluation["technical_details"] = (
                    f"deploy-iflow response was unrecognised, but a direct poll of "
                    f"IntegrationRuntimeArtifacts confirmed status '{_pre_poll['status']}'."
                )
                # Skip the correction loop entirely
                deploy_errors = None
            else:
                deploy_errors = await self.get_deploy_error_details(iflow_id)
            if deploy_errors:
                _MAX_CORRECTION_PASSES = 3
                for _corr_pass in range(1, _MAX_CORRECTION_PASSES + 1):
                    logger.info(
                        "[FIX_DEPLOY] Deploy validation errors for '%s' — "
                        "self-correction pass %d/%d. Errors: %s",
                        iflow_id, _corr_pass, _MAX_CORRECTION_PASSES, deploy_errors[:300],
                    )
                    correction_prompt = (
                        f"DEPLOY CORRECTION (pass {_corr_pass}/{_MAX_CORRECTION_PASSES}) — "
                        f"the previous fix for iFlow '{iflow_id}' was uploaded "
                        f"but deployment failed with these validation errors:\n\n"
                        f"{deploy_errors[:2000]}\n\n"
                        f"INSTRUCTIONS — execute in order, no skipping:\n"
                        f"1. Call get-iflow with ID '{iflow_id}' to download the current (already-updated) iFlow.\n"
                        f"2. Read each validation error carefully and reason about what XML change caused it. "
                        f"Fix ONLY those errors in the iFlow XML. Preserve all other content unchanged.\n"
                        f"   - Do not guess — derive the fix directly from the error message and the XML you see.\n"
                        f"   - Do not add new components. Correct or remove only what the error points to.\n"
                        f"3. Call update-iflow with the corrected iFlow.\n"
                        f"4. Call deploy-iflow with iFlow ID '{iflow_id}'.\n\n"
                        f"Return EXACTLY this JSON (no markdown):\n"
                        f'{{"fix_applied": true, "deploy_success": true/false, '
                        f'"summary": "<what was corrected and deploy outcome>"}}'
                    )
                    tracker_corr = TestExecutionTracker(user_id, f"fix_correction_p{_corr_pass}:{iflow_id}", timestamp)
                    logger_cb_corr = StepLogger(tracker_corr, progress_fn=progress_fn)
                    try:
                        result_corr = await asyncio.wait_for(
                            self.agent.ainvoke(
                                {"messages": [{"role": "user", "content": correction_prompt}]},
                                config={"callbacks": [logger_cb_corr], "recursion_limit": 12},
                            ),
                            timeout=480.0,
                        )
                        final_msg_corr = result_corr["messages"][-1]
                        answer_corr = (
                            final_msg_corr.content
                            if hasattr(final_msg_corr, "content")
                            else str(final_msg_corr)
                        )
                        eval_corr = self.evaluate_fix_result(logger_cb_corr.steps, answer_corr)
                        if eval_corr.get("deploy_success"):
                            logger.info(
                                "[FIX_DEPLOY] Self-correction pass %d succeeded for '%s'",
                                _corr_pass, iflow_id,
                            )
                            evaluation = eval_corr
                            logger_cb  = logger_cb_corr
                            answer     = answer_corr
                            break  # deploy succeeded — stop correction loop
                        else:
                            logger.warning(
                                "[FIX_DEPLOY] Self-correction pass %d did not resolve deploy errors for '%s'",
                                _corr_pass, iflow_id,
                            )
                            evaluation["failed_stage"] = "deploy_validation"
                            evaluation["technical_details"] = (
                                f"Original deploy errors: {deploy_errors[:600]}\n"
                                f"Correction pass {_corr_pass} result: {eval_corr.get('technical_details', '')}"
                            )
                            # Re-fetch errors for the next pass in case they changed
                            new_errors = await self.get_deploy_error_details(iflow_id)
                            if new_errors:
                                deploy_errors = new_errors
                    except Exception as corr_exc:
                        logger.error(
                            "[FIX_DEPLOY] Self-correction pass %d error for '%s': %s",
                            _corr_pass, iflow_id, corr_exc,
                        )
                        break  # don't retry on unexpected exception

        self.update_memory(session_id, f"Fix {iflow_id}", evaluation["summary"])

        logger.info(
            f"[FIX_DEPLOY] iflow={iflow_id} fix_applied={evaluation['fix_applied']} "
            f"deploy_success={evaluation['deploy_success']}"
        )
        return {**evaluation, "steps": logger_cb.steps, "raw_answer": answer}

    # ══════════════════════════════════════════
    # RCA
    # ══════════════════════════════════════════

    @staticmethod
    def classify_error(error_message: str) -> Dict:
        msg = (error_message or "").lower()

        # ── SFTP — must come before AUTH_ERROR; SFTP auth failures are server-side ──
        if any(k in msg for k in ["sftp", "sshexception", "jsch", "failed to connect sftp",
                                   "cannot open channel", "publickey", "hostkey",
                                   "known hosts", "host key", "no such file", "no such directory",
                                   "file already exists", "quota exceeded", "no space left"]):
            return {"error_type": "SFTP_ERROR",            "confidence": 0.93, "tags": ["sftp","filesystem"]}
        if "permission denied" in msg and any(k in msg for k in ["sftp", "ssh", "ftp"]):
            return {"error_type": "SFTP_ERROR",            "confidence": 0.92, "tags": ["sftp","filesystem"]}

        # ── Explicit auth/cert signals — must come before numeric HTTP code checks ──
        if any(k in msg for k in ["unauthorized", "invalid credentials", "credential",
                                   "certificate", "ssl handshake", "tls handshake",
                                   "token expired", "access token", "oauth"]):
            return {"error_type": "AUTH_ERROR",            "confidence": 0.93, "tags": ["auth","cert"]}
        if any(k in msg for k in ["401", "403"]) and not any(k in msg for k in ["sftp", "ssh"]):
            return {"error_type": "AUTH_ERROR",            "confidence": 0.91, "tags": ["auth","cert"]}

        # ── Mapping / schema — specific exceptions before generic "field"/"mapping" ──
        if any(k in msg for k in ["mappingexception", "does not exist in target",
                                   "target structure", "mapping runtime",
                                   "xpath", "namespace", "xslt", "transformation failed"]):
            return {"error_type": "MAPPING_ERROR",         "confidence": 0.90, "tags": ["mapping","schema"]}

        # ── Data validation ──
        if any(k in msg for k in ["mandatory", "required field", "null value",
                                   "validation failed", "data validation",
                                   "schema validation", "invalid payload"]):
            return {"error_type": "DATA_VALIDATION",       "confidence": 0.87, "tags": ["validation","data"]}

        # ── Connectivity / network ──
        if any(k in msg for k in ["connection refused", "connect timed out", "read timed out",
                                   "unreachable", "socketexception", "network unreachable",
                                   "dns resolution", "no route to host"]):
            return {"error_type": "CONNECTIVITY_ERROR",    "confidence": 0.90, "tags": ["network","timeout"]}

        # ── Rate limiting — transient; retry, not an iFlow fix ──
        if any(k in msg for k in ["429", "too many requests", "rate limit", "rate limited",
                                   "throttl"]):
            return {"error_type": "CONNECTIVITY_ERROR",    "confidence": 0.82, "tags": ["network","ratelimit"]}

        # ── 5xx backend errors — server-side, iFlow cannot fix ──
        if any(k in msg for k in ["503", "service unavailable", "502", "bad gateway",
                                   "504", "gateway timeout"]):
            return {"error_type": "BACKEND_ERROR",         "confidence": 0.87, "tags": ["backend","5xx"]}
        if any(k in msg for k in ["500", "internal server error"]):
            return {"error_type": "BACKEND_ERROR",         "confidence": 0.83, "tags": ["backend","500"]}

        # ── 4xx adapter config errors — iFlow sent a bad request ──
        if any(k in msg for k in ["400", "bad request", "404", "not found",
                                   "422", "unprocessable", "405", "method not allowed",
                                   "406", "415", "unsupported media type"]):
            return {"error_type": "ADAPTER_CONFIG_ERROR",  "confidence": 0.83, "tags": ["adapter","4xx"]}

        # ── Weak signals — broad keywords last to avoid false positives ──
        if any(k in msg for k in ["mapping", "field", "structure"]):
            return {"error_type": "MAPPING_ERROR",         "confidence": 0.72, "tags": ["mapping","schema"]}
        if any(k in msg for k in ["expired", "ssl", "tls"]):
            return {"error_type": "AUTH_ERROR",            "confidence": 0.70, "tags": ["auth","cert"]}

        return {"error_type": "UNKNOWN_ERROR",             "confidence": 0.50, "tags": []}

    @staticmethod
    def error_signature(iflow_id: str, error_type: str, error_message: str = "") -> str:
        # Strip GUIDs, timestamps, and numeric IDs from the message so the same
        # logical error always produces the same signature even with different IDs.
        import re as _re
        clean = _re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID
            r"|[A-Z0-9]{20,}"           # long message GUIDs / IDs
            r"|\b\d{4,}\b"             # standalone 4+ digit numbers
            r"|[\s]+",                  # collapse whitespace
            " ",
            (error_message or "").lower(),
        ).strip()[:60]
        return hashlib.md5(f"{iflow_id}:{error_type}:{clean}".encode()).hexdigest()[:16]

    @staticmethod
    def fallback_root_cause(error_type: str, error_message: str) -> str:
        if error_type == "MAPPING_ERROR":
            return f"Message mapping is inconsistent with the latest structure or field definitions. Error: {error_message}"
        if error_type == "DATA_VALIDATION":
            return f"Payload validation failed because required or type-safe input data is missing or invalid. Error: {error_message}"
        if error_type == "AUTH_ERROR":
            return f"Authentication or certificate configuration is invalid or expired for the target connection. Error: {error_message}"
        if error_type == "ADAPTER_CONFIG_ERROR":
            return f"The iFlow sent an incorrect request to the backend (HTTP 4xx) — the receiver adapter URL path, HTTP method, or request format does not match what the backend expects. Error: {error_message}"
        if error_type == "BACKEND_ERROR":
            return f"The backend service returned a server-side fault (HTTP 5xx). The iFlow is working correctly — the backend must be investigated and restored by the responsible team. Error: {error_message}"
        if error_type == "CONNECTIVITY_ERROR":
            return f"Network or destination connectivity to the receiver system failed. Error: {error_message}"
        if error_type == "SFTP_ERROR":
            msg = error_message.lower()
            if any(k in msg for k in ["auth fail", "authentication failed", "publickey"]):
                detail = "SFTP authentication failed — the credential alias, SSH key, or password configured in the receiver adapter is incorrect or expired."
            elif any(k in msg for k in ["hostkey", "known hosts", "host key"]):
                detail = "SFTP host key verification failed — the server fingerprint changed or is not trusted. Update the known hosts configuration."
            elif any(k in msg for k in ["permission denied"]):
                detail = "SFTP permission denied — the SFTP user does not have write access to the target directory."
            elif any(k in msg for k in ["file already exists"]):
                detail = "SFTP file already exists on the server — enable overwrite in the adapter or clean up the existing file."
            elif any(k in msg for k in ["quota", "no space left"]):
                detail = "SFTP server disk quota exceeded — free up space on the target server."
            else:
                detail = "SFTP operation failed — the remote directory does not exist or the SFTP user lacks permission."
            return f"{detail} This requires manual action on the SFTP server or credential store. Error: {error_message}"
        return f"Unable to fully classify the CPI failure. Use logs and the failing iFlow step to identify the required configuration change. Error: {error_message}"

    async def run_rca(self, incident: Dict) -> Dict:
        iflow_id      = incident.get("iflow_id", "")
        error_message = incident.get("error_message", "")
        message_guid  = incident.get("message_guid", "")
        error_type    = incident.get("error_type", "UNKNOWN")

        sig      = self.error_signature(iflow_id, error_type, error_message)
        patterns = get_similar_patterns(sig)
        history_hint = ""
        if patterns:
            # Include key_steps from successful fixes so RCA knows exactly what worked before
            compact = []
            for p in patterns:
                entry = {
                    "fix_applied":  p.get("fix_applied", ""),
                    "root_cause":   p.get("root_cause", ""),
                    "success_rate": round(
                        (p.get("success_count") or 0) / max(p.get("applied_count") or 1, 1), 2
                    ),
                }
                if p.get("key_steps"):
                    try:
                        entry["key_steps"] = json.loads(p["key_steps"])
                    except Exception:
                        entry["key_steps"] = p["key_steps"]
                compact.append(entry)
            history_hint = f"\n\nHistorical fix patterns (ranked by success rate):\n{json.dumps(compact, indent=2)}"
        
        # Retrieve relevant SAP notes from vector store
        vector_store = get_vector_store()
        sap_notes = vector_store.retrieve_relevant_notes(error_message, error_type, iflow_id, limit=5)
        sap_notes_context = vector_store.format_notes_for_prompt(sap_notes)

        iflow_hint = f"- iFlow ID for config lookup: {iflow_id}" if iflow_id else ""

        if message_guid:
            prompt = f"""
AUTONOMOUS RCA — do NOT ask for human input. Maximum 4 tool calls total.

Error detected:
- iFlow:      {iflow_id}
- Error Type: {error_type}
- Message:    {error_message}
- Message ID: {message_guid}
{history_hint}
{sap_notes_context}

Steps (execute in order, stop after step 3):
1. Call get_message_logs ONCE for message ID: {message_guid}
2. Call get-iflow ONCE for iFlow ID: {iflow_id} — read the actual configuration to pinpoint which step/adapter/mapping is misconfigured
3. Cross-reference the log error with the iFlow configuration and produce a precise diagnosis

Return ONLY valid JSON (no markdown, no preamble):
{{
  "root_cause": "<clear description referencing the specific iFlow step, adapter, or mapping that is wrong and why>",
  "proposed_fix": "<precise diagnosis grounded in the actual iFlow config — e.g. 'XPath expression /ns1:Order uses prefix ns1 but namespace is not declared in the Message Mapping step MM_OrderTransform', 'Receiver HTTP adapter in step CallStripe has URL path /v1/charges but the Stripe API expects /v1/payment_intents', 'Content Modifier step SetHeader has srcType=Constant but the value references a property that must use Expression type'. Do NOT write XML — the fix agent applies the change.>",
  "confidence": 0.0,
  "auto_apply": false,
  "error_type": "<error type>",
  "affected_component": "<exact step ID or adapter name from the iFlow config>"
}}

STOP after returning JSON. Do not call any other tools.
"""
        else:
            prompt = f"""
AUTONOMOUS RCA — do NOT ask for human input. No message GUID is available.
{iflow_hint}
{history_hint}
{sap_notes_context}

Error detected:
- iFlow:      {iflow_id}
- Error Type: {error_type}
- Message:    {error_message}

Steps (execute in order, stop after step 2):
1. Call get-iflow ONCE for iFlow ID: {iflow_id} — read the actual configuration to identify the misconfigured step
2. Produce a precise diagnosis based on the iFlow config and the error above

Return ONLY valid JSON (no markdown, no preamble):
{{
  "root_cause": "<clear description referencing the specific step or adapter that is wrong and why>",
  "proposed_fix": "<precise diagnosis grounded in the actual iFlow config — name the exact step/adapter and what is wrong. Do NOT write XML — the fix agent applies the change.>",
  "confidence": 0.0,
  "auto_apply": false,
  "error_type": "<error type>",
  "affected_component": "<exact step ID or adapter name from the iFlow config>"
}}
"""
        if self.agent is None:
            raise RuntimeError(
                "MCP agent is not ready — SAP CPI MCP servers may still be connecting. "
                "Wait a few seconds and retry."
            )

        timestamp  = get_hana_timestamp()
        tracker    = TestExecutionTracker("system_rca", prompt, timestamp)
        logger_cb  = StepLogger(tracker)
        messages   = [{"role": "user", "content": prompt}]

        for attempt in range(3):
            try:
                result = await self.agent.ainvoke(
                    {"messages": messages},
                    config={"callbacks": [logger_cb], "recursion_limit": 10},
                )
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                logger.error(f"[RCA] agent error: {e}")
                return {"root_cause": str(e), "proposed_fix": "", "confidence": 0.0, "auto_apply": False}

        final_msg = result["messages"][-1]
        answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)

        try:
            clean = re.sub(r"```(?:json)?|```", "", answer).strip()
            rca   = json.loads(clean)
        except Exception:
            match = re.search(r"\{.*\}", answer, re.DOTALL)
            try:
                rca = json.loads(match.group(0)) if match else {}
            except Exception:
                rca = {}

        # Apply confidence floor from rule-based classifier
        classifier = self.classify_error(error_message)
        llm_confidence = float(rca.get("confidence", 0.0))
        final_confidence = max(llm_confidence, classifier["confidence"])
        if final_confidence > llm_confidence:
            logger.info(f"[RCA] Confidence floor: LLM={llm_confidence} → classifier={final_confidence}")

        final_error_type = rca.get("error_type", error_type) or classifier["error_type"]
        proposed_fix = (rca.get("proposed_fix", "") or "").strip()
        root_cause = (rca.get("root_cause", "") or "").strip()
        if not proposed_fix:
            proposed_fix = FALLBACK_FIX_BY_ERROR_TYPE.get(
                final_error_type,
                FALLBACK_FIX_BY_ERROR_TYPE["UNKNOWN_ERROR"],
            )
            logger.info(f"[RCA] Using fallback fix for error type: {final_error_type}")
        if not root_cause:
            root_cause = self.fallback_root_cause(final_error_type, error_message)
            logger.info(f"[RCA] Using fallback root cause for error type: {final_error_type}")

        logger.info(
            "[RCA_RESULT] iflow=%s error_type=%s confidence=%.2f affected=%s | "
            "root_cause=%.200s | proposed_fix=%.200s",
            iflow_id, final_error_type, final_confidence,
            rca.get("affected_component", ""),
            root_cause or answer, proposed_fix,
        )
        return {
            "root_cause":         root_cause or answer,
            "proposed_fix":       proposed_fix,
            "confidence":         final_confidence,
            "auto_apply":         bool(rca.get("auto_apply", False)),
            "error_type":         final_error_type,
            "affected_component": rca.get("affected_component", ""),
            "agent_steps":        logger_cb.steps,
        }

    # ══════════════════════════════════════════
    # APPLY FIX (autonomous)  ← FIXED: strict verify
    # ══════════════════════════════════════════

    async def ask_deploy_only(self, iflow_id: str, user_id: str, timestamp: str) -> Dict[str, Any]:
        """
        Deploy-only path: iFlow content was already updated (update succeeded),
        but deployment failed. Skip get-iflow and update-iflow — just deploy.
        """
        missing_tools = self.validate_required_tools("integration_suite", ["deploy-iflow"])
        if missing_tools:
            return {
                "success": False, "fix_applied": True, "deploy_success": False,
                "failed_stage": "tool_validation",
                "technical_details": f"Missing tool: {', '.join(missing_tools)}",
                "summary": "Deploy-only retry aborted — deploy-iflow tool unavailable.",
                "steps": [],
            }

        tracker   = TestExecutionTracker(user_id, f"deploy_only:{iflow_id}", timestamp)
        logger_cb = StepLogger(tracker)
        prompt = (
            f"DEPLOY ONLY — the iFlow '{iflow_id}' was already updated successfully.\n"
            f"Call deploy-iflow tool ONCE with iFlow ID: \"{iflow_id}\".\n"
            f"VERIFY the response contains deployStatus \"Success\" or \"DEPLOYED\".\n"
            f"Return ONLY valid JSON (no markdown):\n"
            f'{{"fix_applied": true, "deploy_success": true/false, '
            f'"deploy_response": "<raw response>", "summary": "<one sentence>"}}'
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
                config={"callbacks": [logger_cb], "recursion_limit": 6},
            )
            final_msg = result["messages"][-1]
            answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            eval_result = self.evaluate_fix_result(logger_cb.steps, answer)
            # update succeeded before, so always mark fix_applied=True
            eval_result["fix_applied"] = True
            return {**eval_result, "steps": logger_cb.steps}
        except Exception as exc:
            logger.error(f"[DEPLOY_ONLY] error for {iflow_id}: {exc}")
            return {
                "success": False, "fix_applied": True, "deploy_success": False,
                "failed_stage": "deploy", "technical_details": str(exc),
                "summary": f"Deploy-only retry failed: {exc}", "steps": logger_cb.steps,
            }

    async def apply_fix(self, incident: Dict, rca: Dict, progress_fn=None) -> Dict:
        """Apply fix and deploy the target iFlow. Verifies both update and deploy."""
        result = await self.ask_fix_and_deploy(
            iflow_id=incident.get("iflow_id", ""),
            error_message=incident.get("error_message", ""),
            proposed_fix=rca.get("proposed_fix", ""),
            root_cause=rca.get("root_cause", ""),
            error_type=rca.get("error_type", incident.get("error_type", "UNKNOWN")),
            affected_component=rca.get("affected_component", ""),
            user_id="system_autofix",
            session_id=f"autofix_{incident.get('incident_id', 'unknown')}",
            timestamp=get_hana_timestamp(),
            progress_fn=progress_fn,
            message_guid=incident.get("message_guid", ""),
        )
        return result

    async def retry_failed_message(self, incident: Dict) -> Dict[str, Any]:
        message_guid = incident.get("message_guid", "")
        if not message_guid:
            return {"success": False, "skipped": True, "summary": "No message GUID available for retry."}

        retry_tools = self.get_retry_tool_candidates()
        if not retry_tools:
            return {
                "success": False, "skipped": True,
                "summary": "No retry or replay MCP tool is currently available.",
            }

        prompt = f"""
RETRY FAILED MESSAGE - use exactly one retry or replay tool call, then stop.
Message GUID: {message_guid}
Candidate tools: {", ".join(retry_tools)}
Rules:
- Retry or replay only this failed message
- Do not fetch logs
- Do not modify the iFlow
- Return a one-sentence plain-text result
"""
        timestamp = get_hana_timestamp()
        tracker   = TestExecutionTracker("system_retry", prompt, timestamp)
        logger_cb = StepLogger(tracker)
        messages  = [{"role": "user", "content": prompt}]

        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
                config={"callbacks": [logger_cb], "recursion_limit": 4},
            )
            final_msg = result["messages"][-1]
            answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            return {"success": True, "skipped": False, "summary": answer, "steps": logger_cb.steps}
        except Exception as e:
            logger.error(f"[RETRY] retry_failed_message error: {e}")
            return {"success": False, "skipped": False, "summary": str(e), "steps": logger_cb.steps}

    async def test_iflow_after_fix(self, incident: Dict) -> Dict[str, Any]:
        """
        After a successful deploy, call test_iflow_with_payload directly.
        Payload is derived from the error/fix context — no need to re-download the iFlow XML.
        """
        has_test_tool = any("test_iflow_with_payload" in t.name for t in self.tools)
        if not has_test_tool:
            return {"success": False, "skipped": True, "summary": "test_iflow_with_payload tool not available."}

        iflow_id     = incident.get("iflow_id", "")
        error_type   = incident.get("error_type", "")
        error_msg    = incident.get("error_message", "")
        proposed_fix = incident.get("proposed_fix", "")

        prompt = f"""
IFLOW VERIFICATION — the fix has been deployed. Confirm it works with one test call.

iFlow ID: {iflow_id}
Original error type: {error_type}
Original error: {error_msg[:400]}
Applied fix: {proposed_fix[:400]}

INSTRUCTIONS:
1. Call get_iflow_endpoint with iflow_id='{iflow_id}' to discover the HTTP endpoint.
   - If it returns 0 endpoints, count=0, or "unable to fetch", the iFlow has no HTTP trigger
     (it may be SFTP, File, or scheduler-triggered). Immediately return:
     {{"test_passed": false, "http_status": null, "summary": "iFlow has no HTTP endpoint — test not applicable."}}
     Do NOT call test_iflow_with_payload in this case.
2. If an endpoint IS found, construct a minimal valid payload from the error context above
   and call test_iflow_with_payload once with iflow_id='{iflow_id}' and that payload.
3. Return EXACTLY this JSON (no markdown):
{{"test_passed": true/false, "http_status": <status code or null>, "summary": "<one sentence: what was sent and what the iFlow returned>"}}

Do NOT call get-iflow. Do NOT modify the iFlow. Do NOT call deploy. Do NOT fetch message logs.
"""
        timestamp = get_hana_timestamp()
        tracker   = TestExecutionTracker(incident.get("user_id", "system"), f"test_after_fix:{iflow_id}", timestamp)
        logger_cb = StepLogger(tracker)

        try:
            result = await asyncio.wait_for(
                self.agent.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={"callbacks": [logger_cb], "recursion_limit": 6},
                ),
                timeout=120.0,
            )
            final_msg = result["messages"][-1]
            answer    = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            try:
                parsed = json.loads(answer.strip())
            except Exception:
                parsed = {}
            test_passed = parsed.get("test_passed", False)
            logger.info(
                "[TEST_AFTER_FIX] iflow=%s test_passed=%s status=%s summary=%s",
                iflow_id, test_passed, parsed.get("http_status"), parsed.get("summary", "")[:200],
            )
            return {
                "success":     test_passed,
                "skipped":     False,
                "http_status": parsed.get("http_status"),
                "summary":     parsed.get("summary", answer[:200]),
                "steps":       logger_cb.steps,
            }
        except asyncio.TimeoutError:
            logger.warning("[TEST_AFTER_FIX] timed out for iflow=%s", iflow_id)
            return {"success": False, "skipped": True, "summary": "iFlow test timed out after 120s.", "steps": logger_cb.steps}
        except Exception as e:
            logger.error("[TEST_AFTER_FIX] error for iflow=%s: %s", iflow_id, e)
            return {"success": False, "skipped": False, "summary": str(e), "steps": logger_cb.steps}

    # ══════════════════════════════════════════
    # INTERNAL ESCALATION TICKET CREATION
    # ══════════════════════════════════════════

    async def _create_external_ticket(self, incident: Dict, rca: Dict) -> Optional[str]:
        """
        Create an internal escalation ticket in the HANA escalation_tickets table.
        Returns the generated ticket_id.
        """
        try:
            occurrence = incident.get("occurrence_count", 1)
            confidence = rca.get("confidence", 0.0)
            priority = (
                "CRITICAL" if occurrence >= 5 or confidence < 0.3
                else "HIGH"   if occurrence >= 3 or confidence < 0.5
                else "MEDIUM"
            )
            ticket_data = {
                "incident_id": incident.get("incident_id"),
                "iflow_id":    incident.get("iflow_id"),
                "error_type":  incident.get("error_type"),
                "title": (
                    f"[SAP CPI] Auto-remediation escalation: {incident.get('iflow_id', 'unknown')} — "
                    f"{incident.get('error_type', 'UNKNOWN_ERROR')}"
                ),
                "description": (
                    f"iFlow: {incident.get('iflow_id')}\n"
                    f"Error: {incident.get('error_message', '')[:500]}\n"
                    f"Root cause: {rca.get('root_cause', '')}\n"
                    f"Proposed fix: {rca.get('proposed_fix', '')}\n"
                    f"Incident ID: {incident.get('incident_id')}\n"
                    f"Occurrence count: {occurrence}\n"
                    f"RCA confidence: {confidence}"
                ),
                "priority":    priority,
                "status":      "OPEN",
                "assigned_to": TICKET_DEFAULT_ASSIGNEE or None,
            }
            ticket_id = create_escalation_ticket(ticket_data)
            logger.info(f"[EscalationTicket] Internal ticket {ticket_id} created for incident {incident.get('incident_id')}")
            return ticket_id
        except Exception as exc:
            logger.error(f"[EscalationTicket] Failed to create internal ticket: {exc}")
            return None

    # ══════════════════════════════════════════
    # REMEDIATION GATE  ← FIXED: always fix when AUTO_FIX_ALL_CPI_ERRORS
    # ══════════════════════════════════════════

    async def remediation_gate(self, incident: Dict, rca: Dict) -> str:
        confidence = rca.get("confidence", 0.0)
        policy     = self.get_remediation_policy(incident, rca)

        # Confidence floor from classifier
        classifier_confidence = self.classify_error(incident.get("error_message", "")).get("confidence", 0.0)
        if classifier_confidence > confidence:
            logger.info(f"[Gate] Confidence floor: LLM={confidence} → classifier={classifier_confidence}")
            confidence        = classifier_confidence
            rca["confidence"] = confidence

        logger.info(
            "[GATE_ENTRY] iflow=%s error_type=%s confidence=%.2f policy=%s "
            "auto_fix_all=%s has_actionable_fix=%s",
            incident.get("iflow_id", ""), rca.get("error_type", ""), confidence,
            policy["action"], AUTO_FIX_ALL_CPI_ERRORS, self.has_actionable_fix(rca),
        )

        if policy["action"] == "RETRY" and confidence >= SUGGEST_FIX_CONFIDENCE:
            logger.info(f"[Gate] POLICY RETRY ({confidence}) → {incident['iflow_id']}")
            retry_result = await self.retry_failed_message(incident)
            if retry_result["success"]:
                update_incident(incident["incident_id"], {
                    "status":      "RETRIED",
                    "fix_summary": retry_result["summary"],
                    "resolved_at": get_hana_timestamp(),
                })
                return "RETRIED"
            logger.info(f"[Gate] Retry unavailable or failed, escalating to iFlow fix: {incident['iflow_id']}")

        # ── KEY FIX: when AUTO_FIX_ALL_CPI_ERRORS=true, treat any known error type as auto-fixable ──
        effective_auto_fix = (
            AUTO_FIX_ALL_CPI_ERRORS
            and self.has_actionable_fix(rca)
            and rca.get("error_type", "UNKNOWN_ERROR") != "UNKNOWN_ERROR"
        )

        if self.should_auto_fix(incident, rca, policy, confidence) or effective_auto_fix:
            logger.info(f"[Gate] AUTO-FIX ({confidence}) → {incident['iflow_id']}")
            fix_result  = await self.apply_fix(incident, rca)
            outcome     = "SUCCESS" if fix_result["success"] else "FAILED"
            fix_summary = fix_result["summary"]
            retry_result = None
            if fix_result["success"] and policy.get("replay_after_fix"):
                retry_result = await self.retry_failed_message(incident)
                if retry_result.get("summary"):
                    fix_summary = f"{fix_summary}\nRetry: {retry_result['summary']}"
            final_status = self.determine_post_fix_status(
                fix_result["success"], policy, retry_result=retry_result, human_approved=False,
                failed_stage=fix_result.get("failed_stage", ""),
            )
            _resolved = final_status in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX"}
            update_incident(incident["incident_id"], {
                "status":      final_status,
                "fix_summary": fix_summary,
                "resolved_at": get_hana_timestamp() if _resolved else None,
                "verification_status": "VERIFIED" if _resolved else "PENDING",
            })
            upsert_fix_pattern({
                "error_signature": self.error_signature(incident["iflow_id"], rca.get("error_type",""), incident.get("error_message","")),
                "iflow_id":        incident["iflow_id"],
                "error_type":      rca.get("error_type",""),
                "root_cause":      rca.get("root_cause",""),
                "fix_applied":     rca.get("proposed_fix",""),
                "outcome":         outcome,
                "key_steps":       fix_result.get("steps", []) if fix_result.get("success") else [],
            })
            return final_status

        elif confidence >= SUGGEST_FIX_CONFIDENCE:
            logger.info(f"[Gate] MEDIUM ({confidence}) → awaiting approval: {incident['iflow_id']}")
            update_incident(incident["incident_id"], {
                "status":        "AWAITING_APPROVAL",
                "pending_since": get_hana_timestamp(),
            })
            return "AWAITING_APPROVAL"

        else:
            logger.info(f"[Gate] LOW ({confidence}) → inconclusive, creating ticket: {incident['iflow_id']}")
            update_incident(incident["incident_id"], {"status": "RCA_INCONCLUSIVE"})
            ticket_id = await self._create_external_ticket(incident, rca)
            update_incident(incident["incident_id"], {
                "status":    "TICKET_CREATED",
                "ticket_id": ticket_id,
            })
            return "TICKET_CREATED"

    # ══════════════════════════════════════════
    # AUTONOMOUS LOOP
    # ══════════════════════════════════════════

    async def resume_correlated_incident(self, incident: Dict, latest_data: Dict) -> str:
        """
        For recurring failures matched to an existing open incident, resume RCA/fix flow
        instead of only incrementing the occurrence counter.
        """
        incident_id = incident.get("incident_id", "")
        current_status = str(incident.get("status", "")).upper()
        latest_guid = latest_data.get("message_guid") or latest_data.get("MessageGuid") or incident.get("message_guid")

        refresh_updates = {
            "message_guid": latest_guid,
            "error_message": latest_data.get("error_message", incident.get("error_message", "")),
            "correlation_id": latest_data.get("correlation_id", incident.get("correlation_id", "")),
            "last_seen": get_hana_timestamp(),
        }
        update_incident(incident_id, refresh_updates)

        merged_incident = {**incident, **latest_data, **refresh_updates}

        if current_status in {"RCA_IN_PROGRESS", "FIX_IN_PROGRESS"}:
            logger.info("[Autonomous] Existing incident still in progress: %s", incident_id)
            return current_status

        rca = {
            "root_cause": merged_incident.get("root_cause", ""),
            "proposed_fix": merged_incident.get("proposed_fix", ""),
            "confidence": merged_incident.get("rca_confidence", 0.0),
            "auto_apply": True,
            "error_type": merged_incident.get("error_type", ""),
            "affected_component": merged_incident.get("affected_component", ""),
        }

        needs_rca = (
            current_status in {"DETECTED", "RCA_FAILED"}
            or not self.has_actionable_fix(rca)
        )

        if needs_rca:
            logger.info("[Autonomous] Re-running RCA for recurring incident: %s", incident_id)
            update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
            rca = await self.run_rca(merged_incident)
            update_incident(incident_id, {
                "status": "RCA_COMPLETE",
                "root_cause": rca.get("root_cause", ""),
                "proposed_fix": rca.get("proposed_fix", ""),
                "rca_confidence": rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
            })
            merged_incident.update({
                "root_cause": rca.get("root_cause", ""),
                "proposed_fix": rca.get("proposed_fix", ""),
                "rca_confidence": rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
            })
        else:
            logger.info("[Autonomous] Reusing RCA for recurring incident: %s", incident_id)

        final_status = await self.remediation_gate(dict(merged_incident), rca)
        logger.info("[Autonomous] Recurring incident %s moved to status: %s", incident_id, final_status)
        return final_status

    @staticmethod
    def _first_non_empty(*values):
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    async def build_incident_view_model(self, incident: Dict) -> Dict[str, Any]:
        metadata = {}
        message_guid = incident.get("message_guid", "")
        if message_guid:
            metadata = await self.error_fetcher.fetch_message_metadata(message_guid)

        properties = {
            "message": {
                "message_id": self._first_non_empty(incident.get("message_guid"), metadata.get("MessageGuid")),
                "mpl_id": self._first_non_empty(metadata.get("MessageGuid"), incident.get("message_guid")),
                "correlation_id": self._first_non_empty(incident.get("correlation_id"), metadata.get("CorrelationId")),
                "sender": self._first_non_empty(incident.get("sender"), metadata.get("Sender")),
                "receiver": self._first_non_empty(incident.get("receiver"), metadata.get("Receiver")),
                "interface_iflow": self._first_non_empty(incident.get("iflow_id"), metadata.get("IntegrationFlowName")),
                "status": self._first_non_empty(incident.get("status"), metadata.get("Status"), "FAILED"),
                "tenant": SAP_HUB_TENANT_URL,
            },
            "adapter": {
                "sender_adapter": metadata.get("SenderAdapterType"),
                "receiver_adapter": metadata.get("ReceiverAdapterType"),
                "content_type": metadata.get("ContentType"),
                "retry_count": metadata.get("RetryCount"),
            },
            "business_context": {
                "material_id": metadata.get("MaterialId"),
                "plant": metadata.get("Plant"),
                "company_code": metadata.get("CompanyCode"),
            },
        }

        artifact = {
            "name": self._first_non_empty(incident.get("iflow_id"), metadata.get("IntegrationFlowName")),
            "artifact_id": self._first_non_empty(metadata.get("IntegrationFlowId"), incident.get("iflow_id")),
            "version": metadata.get("Version"),
            "package": self._first_non_empty(metadata.get("PackageId"), metadata.get("PackageName")),
            "deployed_on": self._first_non_empty(metadata.get("LogEnd"), incident.get("log_end"), incident.get("created_at")),
            "deployed_by": self._first_non_empty(metadata.get("User"), metadata.get("CreatedBy")),
            "runtime_node": self._first_non_empty(metadata.get("Node"), metadata.get("RuntimeNode")),
        }

        history = [
            {
                "title": "Detected",
                "timestamp": incident.get("created_at"),
                "description": "Failed CPI message was detected and stored as an autonomous incident.",
            },
            {
                "title": "Latest Seen",
                "timestamp": incident.get("last_seen"),
                "description": f"Occurrence count: {incident.get('occurrence_count', 1)}",
            },
            {
                "title": "Resolution",
                "timestamp": incident.get("resolved_at"),
                "description": incident.get("fix_summary") or "No fix summary available yet.",
            },
        ]

        return {
            "incident_id": incident.get("incident_id"),
            "message_guid": message_guid,
            "iflow_id": incident.get("iflow_id"),
            "status": incident.get("status"),
            "error_type": incident.get("error_type"),
            "error_details": {
                "message": incident.get("error_message"),
                "log_start": incident.get("log_start"),
                "log_end": incident.get("log_end"),
            },
            "ai_recommendation": {
                "diagnosis": incident.get("root_cause"),
                "suggested_fix": incident.get("proposed_fix"),
                "confidence": incident.get("rca_confidence"),
                "affected_component": incident.get("affected_component"),
                "recommended_action": (
                    _STATUS_ACTION_HINTS.get(incident.get("status", ""))
                    or ACTION_HINTS.get(incident.get("error_type", ""), "No action hint available.")
                ),
                "can_generate_fix": incident.get("status") in {
                    "RCA_COMPLETE", "AWAITING_APPROVAL",
                    "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
                },
            },
            "properties": properties,
            "artifact": artifact,
            "attachments": [],
            "history": [item for item in history if item.get("timestamp") or item.get("description")],
        }

    def _set_progress(self, incident_id: str, step: str, step_index: int, total_steps: int, status: str = "FIX_IN_PROGRESS", **context: object) -> None:
        """Write a granular pipeline step into the in-memory FIX_PROGRESS store.

        Extra keyword args (e.g. iflow_id, root_cause, proposed_fix) are stored
        in the entry so the fix_status polling endpoint can surface them while
        the pipeline is still running.
        """
        import main as _self_module  # noqa: PLC0415
        store = getattr(_self_module, "FIX_PROGRESS", {})
        entry = store.get(incident_id, {"steps_done": [], "started_at": get_hana_timestamp()})
        if step_index > 1 and entry.get("current_step"):
            entry["steps_done"].append(entry["current_step"])
        entry.update({
            "status":      status,
            "current_step": step,
            "step_index":  step_index,
            "total_steps": total_steps,
            "updated_at":  get_hana_timestamp(),
        })
        # Persist extra context fields (never overwrite with None if already set)
        for k, v in context.items():
            if v is not None or k not in entry:
                entry[k] = v
        store[incident_id] = entry

    async def execute_incident_fix(self, incident: Dict, human_approved: bool = False, deploy_only: bool = False) -> Dict[str, Any]:
        incident_id = incident.get("incident_id", "")
        working_incident = dict(incident)
        iflow_id = working_incident.get("iflow_id", "")

        # ── Pre-flight check: Verify iFlow exists before attempting fix ──
        if iflow_id:
            logger.info(f"[FIX] Verifying iFlow existence before fix: {iflow_id}")
            existence_check = await self.verify_iflow_exists(iflow_id)
            
            if not existence_check["exists"]:
                confirmed = existence_check.get("verified", False)
                logger.warning(f"[FIX] iFlow existence check failed ({'confirmed deleted' if confirmed else 'inconclusive — blocked'}): {iflow_id}")
                final_status = "ARTIFACT_MISSING"
                fix_summary = (
                    f"Cannot fix - iFlow '{iflow_id}' does not exist in SAP CPI. "
                    f"{'The artifact has been deleted.' if confirmed else 'Existence could not be confirmed — fix blocked as a precaution.'} "
                    f"{existence_check['message']}"
                )

                update_incident(incident_id, {
                    "status": final_status,
                    "fix_summary": fix_summary,
                    "resolved_at": get_hana_timestamp(),
                    "verification_status": "ARTIFACT_NOT_FOUND",
                })

                return {
                    "incident_id": incident_id,
                    "iflow_id": iflow_id,
                    "status": final_status,
                    "success": False,
                    "fix_applied": False,
                    "deploy_success": False,
                    "failed_stage": "verification",
                    "technical_details": existence_check["message"],
                    "summary": fix_summary,
                    "root_cause": working_incident.get("root_cause"),
                    "proposed_fix": working_incident.get("proposed_fix"),
                    "confidence": working_incident.get("rca_confidence"),
                    "incident": get_incident_by_id(incident_id) or working_incident,
                }
            elif not existence_check.get("verified", True):
                logger.warning(
                    f"[FIX] iFlow existence check inconclusive (HTTP {existence_check['status_code']}), "
                    f"proceeding with fix attempt: {iflow_id}"
                )

        rca = {
            "root_cause": working_incident.get("root_cause", ""),
            "proposed_fix": working_incident.get("proposed_fix", ""),
            "confidence": working_incident.get("rca_confidence", 0.0),
            "auto_apply": True,
            "error_type": working_incident.get("error_type", ""),
            "affected_component": working_incident.get("affected_component", ""),
        }

        # ── Pattern-first: apply proven fix directly, skip RCA if high-confidence pattern exists ──
        _sig = self.error_signature(iflow_id, working_incident.get("error_type", ""), working_incident.get("error_message", ""))
        _patterns = get_similar_patterns(_sig)
        _best_pattern = next(
            (p for p in _patterns
             if (p.get("success_count") or 0) >= PATTERN_MIN_SUCCESS_COUNT),
            None,
        )
        if _best_pattern and not self.has_actionable_fix(rca):
            logger.info(
                f"[FIX] Pattern-first: reusing proven fix for {iflow_id} "
                f"(success_count={_best_pattern.get('success_count')})"
            )
            rca.update({
                "root_cause":    _best_pattern.get("root_cause", ""),
                "proposed_fix":  _best_pattern.get("fix_applied", ""),
                "confidence":    1.0,
                "error_type":    _best_pattern.get("error_type", rca.get("error_type", "")),
            })
            working_incident.update({
                "root_cause":    rca["root_cause"],
                "proposed_fix":  rca["proposed_fix"],
                "rca_confidence": 1.0,
            })
            update_incident(incident_id, {
                "status":        "RCA_COMPLETE",
                "root_cause":    rca["root_cause"],
                "proposed_fix":  rca["proposed_fix"],
                "rca_confidence": 1.0,
            })

        total = 5 if not self.has_actionable_fix(rca) else 4

        if not self.has_actionable_fix(rca):
            self._set_progress(incident_id, "Running Root Cause Analysis…", 1, total)
            update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
            rca = await self.run_rca(working_incident)
            update_incident(incident_id, {
                "status": "RCA_COMPLETE",
                "root_cause": rca.get("root_cause", ""),
                "proposed_fix": rca.get("proposed_fix", ""),
                "rca_confidence": rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
            })
            working_incident.update({
                "root_cause": rca.get("root_cause", ""),
                "proposed_fix": rca.get("proposed_fix", ""),
                "rca_confidence": rca.get("confidence", 0.0),
                "affected_component": rca.get("affected_component", ""),
            })

        # ── Unfixable detection — route to TICKET_CREATED before wasting a fix attempt ──
        # Some errors are structurally unfixable by XML property edits:
        #   • Groovy script logic needs rewriting (try/catch, JsonSlurper, etc.)
        #   • Structural additions needed (add Router, add Subprocess, etc.)
        #   • Bad runtime data from upstream (empty payload, non-JSON, wrong Content-Type)
        # In all these cases the proposed_fix will contain specific language that signals
        # the fix is beyond what the agent can safely do. Detect and ticket early.
        _unfixable_signals = [
            # Groovy script rewrites
            "jsonslurper", "try/catch", "try {", "groovy script", "switch to",
            "rewrite the script", "modify the script", "update the script",
            # Structural changes
            "add a router", "add router", "add content-based router",
            "add an exception subprocess", "add exception subprocess",
            "add a subprocess", "add new step", "add a new step",
            "add a converter", "add json-to-xml", "add xml-to-json",
            # Runtime data problems (upstream issue — not iFlow config)
            "upstream", "payload is not valid json", "empty payload",
            "payload is empty", "non-json payload", "invalid json payload",
            "content-type mismatch", "backend returns", "backend response",
        ]
        _fix_hint = (rca.get("proposed_fix") or "").lower()
        _root_cause_hint = (rca.get("root_cause") or "").lower()
        _unfixable_match = next(
            (s for s in _unfixable_signals if s in _fix_hint or s in _root_cause_hint),
            None,
        )
        if _unfixable_match and not deploy_only:
            logger.warning(
                "[FIX] Unfixable signal detected in proposed_fix/root_cause — "
                "routing to TICKET_CREATED without attempting XML edit. "
                "Signal: '%s' | iflow=%s", _unfixable_match, iflow_id,
            )
            _unfixable_reason = (
                f"Auto-fix skipped: the root cause requires changes that cannot be safely applied "
                f"by editing iFlow XML properties (detected: '{_unfixable_match}'). "
                f"Manual intervention is required.\n\n"
                f"Root cause: {rca.get('root_cause', '')}\n"
                f"Suggested fix: {rca.get('proposed_fix', '')}"
            )
            ticket_id = await self._create_external_ticket(
                {**working_incident, "fix_summary": _unfixable_reason},
                rca,
            )
            update_incident(incident_id, {
                "status":     "TICKET_CREATED",
                "fix_summary": _unfixable_reason,
                "ticket_id":  ticket_id,
                "resolved_at": get_hana_timestamp(),
            })
            self._set_progress(
                incident_id, "Unfixable — ticket created for manual review",
                total, total, status="TICKET_CREATED",
            )
            return {
                "incident_id":    incident_id,
                "iflow_id":       iflow_id,
                "status":         "TICKET_CREATED",
                "success":        False,
                "fix_applied":    False,
                "deploy_success": False,
                "failed_stage":   "unfixable",
                "summary":        _unfixable_reason,
                "root_cause":     rca.get("root_cause"),
                "proposed_fix":   rca.get("proposed_fix"),
                "confidence":     rca.get("confidence"),
                "incident":       get_incident_by_id(incident_id) or working_incident,
            }

        step_base = 2 if total == 5 else 1
        self._set_progress(incident_id, "Verifying iFlow exists…", step_base, total)

        # ── Double-check iFlow existence right before fix (in case it was deleted during RCA) ──
        if iflow_id:
            existence_check = await self.verify_iflow_exists(iflow_id)
            if not existence_check["exists"]:
                confirmed = existence_check.get("verified", False)
                logger.warning(f"[FIX] iFlow existence check failed mid-fix ({'confirmed deleted' if confirmed else 'inconclusive — blocked'}): {iflow_id}")
                final_status = "ARTIFACT_MISSING"
                fix_summary = (
                    f"Cannot fix - iFlow '{iflow_id}' "
                    f"{'was deleted during analysis.' if confirmed else 'existence could not be confirmed mid-fix — blocked as a precaution.'} "
                    f"{existence_check['message']}"
                )
                self._set_progress(incident_id, "iFlow deleted - cannot fix", total, total, status=final_status)
                update_incident(incident_id, {
                    "status": final_status,
                    "fix_summary": fix_summary,
                    "resolved_at": get_hana_timestamp(),
                    "verification_status": "ARTIFACT_NOT_FOUND",
                })
                
                return {
                    "incident_id": incident_id,
                    "iflow_id": iflow_id,
                    "status": final_status,
                    "success": False,
                    "fix_applied": False,
                    "deploy_success": False,
                    "failed_stage": "verification",
                    "technical_details": existence_check["message"],
                    "summary": fix_summary,
                    "root_cause": working_incident.get("root_cause"),
                    "proposed_fix": working_incident.get("proposed_fix"),
                    "confidence": working_incident.get("rca_confidence"),
                    "incident": get_incident_by_id(incident_id) or working_incident,
                }
        
        self._set_progress(
            incident_id, "Downloading iFlow configuration…", step_base + 1, total,
            iflow_id=iflow_id,
            root_cause=working_incident.get("root_cause"),
            proposed_fix=working_incident.get("proposed_fix"),
            rca_confidence=working_incident.get("rca_confidence"),
            error_type=working_incident.get("error_type"),
        )
        update_incident(incident_id, {"status": "FIX_IN_PROGRESS"})

        # ── Snapshot iFlow content before any modification ────────────────────
        if iflow_id and not deploy_only:
            try:
                get_tool = self.get_mcp_tool("integration_suite", "get-iflow")
                if get_tool:
                    snapshot_raw = await get_tool.ainvoke({"id": iflow_id})
                    snapshot_str = json.dumps(snapshot_raw) if not isinstance(snapshot_raw, str) else snapshot_raw
                    update_incident(incident_id, {"iflow_snapshot_before": snapshot_str[:50000]})
                    logger.info(f"[FIX] iFlow snapshot captured for {iflow_id} ({len(snapshot_str)} chars)")
                    # Set validator context so update-iflow calls are validated against this snapshot
                    orig_fp, orig_xml = _extract_iflow_file(snapshot_str)
                    if orig_fp:
                        _fix_ctx.set({"filepath": orig_fp, "xml": orig_xml})
                        logger.info(f"[VALIDATOR] Fix context set: filepath='{orig_fp}', xml_len={len(orig_xml)}")
            except Exception as snap_exc:
                logger.warning(f"[FIX] Could not capture iFlow snapshot for {iflow_id}: {snap_exc}")

        if deploy_only:
            self._set_progress(incident_id, "Deploying iFlow (update already applied)…", step_base + 2, total)
            fix_result = await self.ask_deploy_only(
                iflow_id=iflow_id,
                user_id="system_autofix",
                timestamp=get_hana_timestamp(),
            )
        else:
            self._set_progress(incident_id, "Applying fix and deploying iFlow…", step_base + 2, total)
            _fix_step = step_base + 2

            def _fix_progress(label: str) -> None:
                self._set_progress(incident_id, label, _fix_step, total)

            fix_result = await self.apply_fix(working_incident, rca, progress_fn=_fix_progress)
        policy = self.get_remediation_policy(working_incident, rca)
        retry_result = None
        fix_summary = fix_result.get("summary", "") or ""
        if not fix_summary and fix_result.get("success"):
            fix_summary = f"iFlow '{working_incident.get('iflow_id', '')}' updated and deployed successfully."

        if fix_result.get("failed_stage") == "deploy" or (
            fix_result.get("fix_applied") and not fix_result.get("deploy_success")
        ):
            deploy_error_text = await self.get_deploy_error_details(working_incident.get("iflow_id", ""))
            if deploy_error_text:
                fix_result["technical_details"] = deploy_error_text[:1500]
                fix_summary = (
                    f"{fix_summary}\nDeployment error details: {deploy_error_text[:800]}"
                    if fix_summary else deploy_error_text[:800]
                )

        # ── Mandatory post-fix replay ─────────────────────────────────────────
        # Always replay the failed message after a successful deploy so we confirm
        # the fix actually works end-to-end, not just that deployment completed.
        replay_success = False
        replay_skipped = False
        test_result: Dict[str, Any] = {}
        if fix_result.get("success"):
            self._set_progress(incident_id, "Validating fix — replaying failed message…", total, total)
            retry_result = await self.retry_failed_message(working_incident)
            replay_success = retry_result.get("success", False)
            replay_skipped = retry_result.get("skipped", False)
            if retry_result.get("summary"):
                fix_summary = f"{fix_summary}\nReplay: {retry_result['summary']}"
            if not replay_success and not replay_skipped:
                logger.warning(
                    f"[FIX] Deploy succeeded but message replay failed for {incident_id}. "
                    "Marking as FIX_APPLIED_PENDING_VERIFICATION."
                )

            # iFlow payload testing is disabled — deploy success is the verification signal.

        # Status: if deploy succeeded but replay genuinely failed → pending verification
        # If replay was skipped (no message GUID / no retry tool / test tool unavailable), trust the deploy result
        if fix_result.get("success") and not replay_success and not replay_skipped:
            final_status = "FIX_DEPLOYED"
        else:
            final_status = self.determine_post_fix_status(
                fix_result.get("success", False),
                policy,
                retry_result=retry_result,
                failed_stage=fix_result.get("failed_stage", ""),
                human_approved=human_approved,
            )

        # Append technical_details to fix_summary so it's stored and visible via API
        technical_details = fix_result.get("technical_details", "")
        if technical_details and not fix_result.get("success"):
            fix_summary = (
                f"{fix_summary}\nTechnical detail: {technical_details}"
                if fix_summary else technical_details
            )

        failed_stage = fix_result.get("failed_stage", "")

        # Mark progress as done — UI stops polling after seeing a terminal status
        done_step = (
            "Fix applied and validated successfully" if fix_result.get("success") and replay_success
            else ("Fix deployed — awaiting message verification" if fix_result.get("success")
                  else f"Fix failed — stage: {failed_stage}" if failed_stage else "Fix failed")
        )
        self._set_progress(
            incident_id, done_step, total, total, status=final_status,
            failed_stage=failed_stage or None,
            technical_details=technical_details[:300] if technical_details else None,
        )
        update_incident(incident_id, {
            "status":               final_status,
            "fix_summary":          fix_summary,
            "last_failed_stage":    failed_stage or None,
            "resolved_at":          get_hana_timestamp() if final_status in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX"} else None,
            "verification_status":  "VERIFIED" if final_status in {"FIX_VERIFIED", "HUMAN_INITIATED_FIX"} else "PENDING",
            "root_cause":           rca.get("root_cause", ""),
            "proposed_fix":         rca.get("proposed_fix", ""),
            "rca_confidence":       rca.get("confidence", 0.0),
            "affected_component":   rca.get("affected_component", ""),
            "consecutive_failures": 0 if fix_result.get("success") else (
                (int(working_incident.get("consecutive_failures") or 0)) + 1
            ),
        })
        upsert_fix_pattern(
            {
                "error_signature": self.error_signature(working_incident.get("iflow_id", ""), rca.get("error_type", ""), working_incident.get("error_message", "")),
                "iflow_id":   working_incident.get("iflow_id", ""),
                "error_type": rca.get("error_type", ""),
                "root_cause": rca.get("root_cause", ""),
                "fix_applied": rca.get("proposed_fix", ""),
                "outcome":    "SUCCESS" if fix_result.get("success") else "FAILED",
                "key_steps":  fix_result.get("steps", []) if fix_result.get("success") else [],
            },
            replay_success=replay_success,
        )

        logger.info(
            "[FIX_OUTCOME] incident=%s iflow=%s status=%s fix_applied=%s deploy_success=%s "
            "replay=%s failed_stage=%s | summary=%.300s",
            incident_id, iflow_id, final_status,
            fix_result.get("fix_applied"), fix_result.get("deploy_success"),
            replay_success, fix_result.get("failed_stage", ""),
            fix_summary,
        )
        refreshed = get_incident_by_id(incident_id) or working_incident
        return {
            "incident_id": incident_id,
            "iflow_id": refreshed.get("iflow_id"),
            "status": final_status,
            "success": fix_result.get("success", False),
            "fix_applied": fix_result.get("fix_applied", False),
            "deploy_success": fix_result.get("deploy_success", False),
            "failed_stage": fix_result.get("failed_stage"),
            "technical_details": fix_result.get("technical_details", ""),
            "summary": fix_summary,
            "root_cause": refreshed.get("root_cause"),
            "proposed_fix": refreshed.get("proposed_fix"),
            "confidence": refreshed.get("rca_confidence"),
            "incident": refreshed,
        }

    async def process_detected_error(self, normalized_error: Dict[str, Any]) -> str:
        normalized = dict(normalized_error)
        classification = self.classify_error(normalized.get("error_message", ""))
        normalized.update(classification)
        logger.info(
            "[ERROR_DETECTED] iflow=%s error_type=%s confidence=%.2f guid=%s source=%s | "
            "error=%.250s",
            normalized.get("iflow_id", ""), classification.get("error_type", ""),
            classification.get("confidence", 0.0), normalized.get("message_guid", ""),
            normalized.get("source_type", ""),
            normalized.get("error_message", ""),
        )

        existing_sig = get_open_incident_by_signature(
            normalized.get("iflow_id", ""),
            normalized.get("error_type", ""),
        )
        if existing_sig:
            # ── Circuit breaker: stop auto-fixing after too many consecutive failures ──
            consec = int(existing_sig.get("consecutive_failures") or 0)
            if consec >= MAX_CONSECUTIVE_FAILURES and not existing_sig.get("auto_escalated"):
                logger.warning(
                    "[Autonomous] Circuit breaker triggered: %s consecutive failures for %s — escalating.",
                    consec, existing_sig.get("iflow_id"),
                )
                rca_for_ticket = {
                    "root_cause":    existing_sig.get("root_cause", ""),
                    "proposed_fix":  existing_sig.get("proposed_fix", ""),
                    "confidence":    existing_sig.get("rca_confidence", 0.0),
                    "error_type":    existing_sig.get("error_type", ""),
                }
                ticket_id = await self._create_external_ticket(dict(existing_sig), rca_for_ticket)
                update_incident(existing_sig["incident_id"], {
                    "status":       "TICKET_CREATED",
                    "auto_escalated": 1,
                    "ticket_id":    ticket_id,
                })
                return "CIRCUIT_BREAKER_ESCALATED"

            if existing_sig.get("auto_escalated"):
                logger.info("[Autonomous] Skipping auto-escalated incident: %s", existing_sig.get("incident_id"))
                return "SKIPPED_AUTO_ESCALATED"

            increment_incident_occurrence(
                existing_sig.get("incident_id"),
                message_guid=normalized.get("message_guid") or None,
                last_seen=get_hana_timestamp(),
            )
            logger.info(
                "[Autonomous] Correlated duplicate: iflow=%s type=%s existing=%s",
                normalized.get("iflow_id", ""),
                normalized.get("error_type", ""),
                existing_sig.get("incident_id"),
            )
            return await self.resume_correlated_incident(dict(existing_sig), normalized)

        # ── Burst deduplication: suppress new incident if same group seen < BURST_DEDUP_WINDOW_SECONDS ago ──
        _group_key = self.incident_group_key(normalized)
        _recent = get_recent_incident_by_group_key(_group_key, within_seconds=BURST_DEDUP_WINDOW_SECONDS)
        if _recent:
            increment_incident_occurrence(
                _recent["incident_id"],
                message_guid=normalized.get("message_guid") or None,
                last_seen=get_hana_timestamp(),
            )
            logger.info(
                "[Autonomous] Burst dedup: absorbed into recent incident %s (group=%s)",
                _recent["incident_id"], _group_key,
            )
            return "BURST_DEDUPED"

        incident_id = str(uuid.uuid4())
        incident = {
            **normalized,
            "incident_id": incident_id,
            "status": "DETECTED",
            "created_at": get_hana_timestamp(),
            "incident_group_key": _group_key,
            "occurrence_count": 1,
            "last_seen": get_hana_timestamp(),
            "verification_status": "UNVERIFIED",
            "consecutive_failures": 0,
            "auto_escalated": 0,
        }
        create_incident(incident)
        logger.info(
            "[Autonomous] New incident: %s | %s | %s",
            incident_id,
            normalized.get("iflow_id", ""),
            normalized.get("error_type", ""),
        )

        update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
        rca = await self.run_rca(incident)
        update_incident(incident_id, {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
        })
        await self.remediation_gate(dict(incident), rca)
        return "PROCESSED"

    async def _check_pending_approval_timeouts(self) -> None:
        """Escalate AWAITING_APPROVAL incidents that have exceeded PENDING_APPROVAL_TIMEOUT_HRS."""
        try:
            from datetime import timedelta
            cutoff = (datetime.now(UTC) - timedelta(hours=PENDING_APPROVAL_TIMEOUT_HRS)).isoformat()
            stale = [
                inc for inc in get_all_incidents(status="AWAITING_APPROVAL", limit=200)
                if (inc.get("pending_since") or inc.get("created_at") or "") < cutoff
                and not inc.get("auto_escalated")
            ]
            for inc in stale:
                logger.warning(
                    "[Autonomous] AWAITING_APPROVAL timeout: incident %s pending since %s — escalating to human review.",
                    inc["incident_id"], inc.get("pending_since"),
                )
                update_incident(inc["incident_id"], {
                    "status":        "AWAITING_HUMAN_REVIEW",
                    "auto_escalated": 1,
                })
                rca_ctx = {
                    "root_cause":   inc.get("root_cause", ""),
                    "proposed_fix": inc.get("proposed_fix", ""),
                    "confidence":   inc.get("rca_confidence", 0.0),
                    "error_type":   inc.get("error_type", ""),
                }
                ticket_id = await self._create_external_ticket(inc, rca_ctx)
                update_incident(inc["incident_id"], {
                    "status":    "TICKET_CREATED",
                    "ticket_id": ticket_id,
                })
        except Exception as exc:
            logger.error(f"[Autonomous] _check_pending_approval_timeouts error: {exc}")

    async def _autonomous_loop(self):
        logger.info("[Autonomous] Monitoring loop started.")
        seen_sources: set = set()   # reset per loop start, not per iteration

        while self._autonomous_running:
            try:
                logger.info("[Autonomous] Polling SAP for failed messages...")
                await self._check_pending_approval_timeouts()
                raw_errors = await self.error_fetcher.fetch_failed_messages(limit=FAILED_MESSAGE_FETCH_LIMIT)
                unique_raw_errors = self.dedupe_raw_failed_messages(raw_errors)
                logger.info(
                    "[Autonomous] Poll complete — %s failed message(s) found, %s unique pattern(s) queued.",
                    len(raw_errors),
                    len(unique_raw_errors),
                )

                for raw in unique_raw_errors:
                    guid = raw.get("MessageGuid", "")
                    source_key = f"message::{guid}"
                    if not guid or source_key in seen_sources:
                        continue
                    seen_sources.add(source_key)

                    existing_guid_incident = get_incident_by_message_guid(guid)
                    if existing_guid_incident:
                        # Skip incidents for deleted iFlows
                        if existing_guid_incident.get("status") in {"ARTIFACT_MISSING", "VERIFICATION_UNAVAILABLE"}:
                            logger.info(f"[Autonomous] Skipping deleted iFlow incident for GUID: {guid}")
                            continue
                        # Skip other active incidents
                        if existing_guid_incident.get("status") not in {
                            "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED",
                            "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
                            "REJECTED", "TICKET_CREATED", "ARTIFACT_MISSING", "VERIFICATION_UNAVAILABLE",
                        }:
                            logger.info(f"[Autonomous] Skipping active incident for GUID: {guid}")
                            continue

                    error_detail   = await self.error_fetcher.fetch_error_details(guid)
                    normalized     = self.error_fetcher.normalize(raw, error_detail)
                    logger.info(
                        "[LOOP_PROCESSING] guid=%s iflow=%s | error=%.200s",
                        guid, normalized.get("iflow_id", ""), normalized.get("error_message", ""),
                    )
                    await self.process_detected_error(normalized)

                runtime_errors = await self.error_fetcher.fetch_runtime_artifact_errors(
                    limit=RUNTIME_ERROR_FETCH_LIMIT
                )
                logger.info(f"[Autonomous] Poll complete — {len(runtime_errors)} runtime artifact error(s) found.")
                for normalized in runtime_errors:
                    artifact_id = normalized.get("artifact_id") or normalized.get("iflow_id", "")
                    source_key = f"artifact::{artifact_id}"
                    if not artifact_id or source_key in seen_sources:
                        continue
                    seen_sources.add(source_key)
                    await self.process_detected_error(normalized)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Autonomous] Loop error: {e}")

            logger.info(f"[Autonomous] Sleeping {POLL_INTERVAL_SECONDS}s until next poll...")
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

        logger.info("[Autonomous] Monitoring loop stopped.")

    def start_autonomous(self):
        if self._autonomous_running:
            return False
        self._autonomous_running = True

        async def _guarded_loop():
            try:
                await self._autonomous_loop()
            except Exception as e:
                logger.error(f"[Autonomous] Loop crashed: {e}")
                self._autonomous_running = False

        self._autonomous_task = asyncio.create_task(_guarded_loop())
        return True

    def stop_autonomous(self):
        if not self._autonomous_running:
            return False
        self._autonomous_running = False
        if self._autonomous_task:
            self._autonomous_task.cancel()
        return True


# ─────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────
mcp_manager: Optional[MultiMCP] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_manager
    
    # Quick synchronous setup
    ensure_autonomous_incident_schema()
    ensure_fix_patterns_schema()
    ensure_escalation_tickets_schema()
    mcp_manager = MultiMCP()
    
    # Initialize MCP servers in background (non-blocking)
    async def init_mcp_background():
        try:
            logger.info("[Startup] Initializing MCP servers in background...")
            await mcp_manager.connect()
            await mcp_manager.discover_tools()
            await mcp_manager.build_agent()
            if AUTONOMOUS_ENABLED:
                mcp_manager.start_autonomous()
                logger.info("[Startup] Autonomous monitoring auto-started.")
            logger.info("[Startup] MCP initialization complete - all systems ready")
        except Exception as e:
            logger.error(f"[Startup] MCP initialization failed: {e}")
    
    # Start background task (don't await - allows FastAPI to serve immediately)
    asyncio.create_task(init_mcp_background())
    logger.info("[Startup] FastAPI ready - MCP initialization running in background")
    
    # Yield immediately - Swagger UI can now load
    yield
    
    # Cleanup
    if mcp_manager:
        mcp_manager.stop_autonomous()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ── Smart Monitoring UI router ────────────────────────────────────────────────
from smart_monitoring import router as _sm_router  # noqa: E402
app.include_router(_sm_router)

from smart_monitoring_dashboard import router as _dashboard_router
app.include_router(_dashboard_router)


def parse_query_request(
    query:   str           = Form(...),
    id:      Optional[str] = Form(None),
    user_id: str           = Form(...),
) -> QueryRequest:
    return QueryRequest(query=query, id=id, user_id=user_id)


# ══════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════

@app.get("/")
async def root():
    return {"status": "running", "service": "CPI MCP Servers + Autonomous Ops", "version": "3.0.0"}


# ── /query: chatbot endpoint  ─────────────────
# NEW LOGIC: If the user's message has fix intent AND references an incident/iflow,
# we run the strict fix+deploy pipeline instead of generic ask().
@app.post("/query", response_model=QueryResponse)
async def query_endpoint(
    req:   QueryRequest               = Depends(parse_query_request),
    files: Optional[List[UploadFile]] = File(None),
):
    timestamp  = get_hana_timestamp()
    session_id = req.id or str(uuid.uuid4())
    result     = {}

    try:
        if files:
            try:
                await upload_multiple_files(session_id, files, timestamp, req.user_id)
            except Exception as e:
                logger.warning(f"File upload failed: {e}")

        xsd_files      = get_xsd_files_by_session(session_id)
        enhanced_query = req.query
        if xsd_files:
            xsd_context = "\n\n--- XSD Files Available in This Session ---\n"
            for xsd in xsd_files:
                xsd_context += (f"\nFile: {xsd['file_id']}\n"
                                f"Target Namespace: {xsd['target_namespace']}\n"
                                f"Elements: {xsd['element_count']}, Types: {xsd['type_count']}\n"
                                f"XSD Content:\n```xml\n{xsd['content']}\n```\n")
            enhanced_query = xsd_context + "\n\n" + req.query

        if mcp_manager is None:
            return JSONResponse(status_code=503, content={"error": "MCP manager not ready"})

        # ── Check if user wants to fix a specific iFlow from an active incident ──
        fix_triggered = False
        if mcp_manager._has_fix_intent(req.query):
            # Try to find an incident referenced in the query (incident_id or iflow name)
            pending = get_all_incidents(status="AWAITING_APPROVAL", limit=5)
            rca_done = get_all_incidents(status="RCA_COMPLETE", limit=5)
            candidates = pending + rca_done

            matched_incident = None
            for inc in candidates:
                if (inc.get("iflow_id", "").lower() in req.query.lower()
                        or inc.get("incident_id", "") in req.query):
                    matched_incident = inc
                    break

            # Only auto-select when there is exactly one actionable incident.
            if not matched_incident and len(candidates) == 1:
                matched_incident = candidates[0]

            if matched_incident and matched_incident.get("proposed_fix"):
                fix_triggered = True
                logger.info(f"[Query] Fix intent detected → incident: {matched_incident['incident_id']}")
                fix_result = await mcp_manager.ask_fix_and_deploy(
                    iflow_id=matched_incident["iflow_id"],
                    error_message=matched_incident.get("error_message", ""),
                    proposed_fix=matched_incident.get("proposed_fix", ""),
                    root_cause=matched_incident.get("root_cause", ""),
                    error_type=matched_incident.get("error_type", "UNKNOWN"),
                    affected_component=matched_incident.get("affected_component", ""),
                    user_id=req.user_id,
                    session_id=session_id,
                    timestamp=timestamp,
                )

                # Update incident status based on fix outcome
                final_status = "HUMAN_INITIATED_FIX" if fix_result["success"] else (
                    "FIX_FAILED_DEPLOY" if fix_result.get("failed_stage") == "deploy"
                    else "FIX_FAILED_UPDATE" if fix_result.get("failed_stage") in ("update", "get")
                    else "FIX_FAILED"
                )
                update_incident(matched_incident["incident_id"], {
                    "status":      final_status,
                    "fix_summary": fix_result["summary"],
                    "resolved_at": get_hana_timestamp() if fix_result["success"] else None,
                    "verification_status": "VERIFIED" if fix_result["success"] else "PENDING",
                })

                result = {"answer": fix_result["summary"], "steps": fix_result.get("steps", [])}
            elif candidates:
                result = {
                    "answer": (
                        "Multiple actionable incidents exist. Please specify the incident_id or iFlow ID "
                        "you want to fix."
                    ),
                    "steps": [],
                }
                fix_triggered = True

        if not fix_triggered:
            result = await mcp_manager.ask(enhanced_query, req.user_id, session_id, timestamp)

        question = req.query.strip()
        if not req.id:
            create_query_history(session_id, question, result.get("answer") or "Request failed!", timestamp, req.user_id)
        else:
            update_query_history(session_id, question, result.get("answer") or "Request failed!", timestamp)

    except Exception as e:
        logger.error(f"query_endpoint error: {e}")
        result = {"error": str(e)}

    return QueryResponse(
        response=result.get("answer") or "Request failed! Try again.",
        id=session_id,
        error=result,
    )


# ── NEW: /fix endpoint — direct fix by iflow_id + error message ─────────────
@app.post("/fix")
async def direct_fix_endpoint(req: DirectFixRequest):
    """
    Direct API for fixing and deploying an iFlow.
    Runs: RCA (if no proposed_fix) → fix → deploy.
    Returns structured result with fix_applied and deploy_success flags.
    """
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")

    timestamp  = get_hana_timestamp()
    session_id = f"direct_fix_{uuid.uuid4()}"

    # If no proposed_fix given, run quick RCA first
    proposed_fix       = req.proposed_fix or ""
    root_cause         = ""
    error_type         = "UNKNOWN"
    affected_component = ""
    confidence         = 0.0

    if not proposed_fix:
        classification = mcp_manager.classify_error(req.error_message)
        fake_incident  = {
            "incident_id":   str(uuid.uuid4()),
            "iflow_id":      req.iflow_id,
            "error_message": req.error_message,
            "error_type":    classification["error_type"],
            "message_guid":  "",
        }
        rca = await mcp_manager.run_rca(fake_incident)
        proposed_fix       = rca.get("proposed_fix", "")
        root_cause         = rca.get("root_cause", "")
        error_type         = rca.get("error_type", classification["error_type"])
        affected_component = rca.get("affected_component", "")
        confidence         = rca.get("confidence", 0.0)

    if not proposed_fix:
        return {
            "success": False,
            "fix_applied": False,
            "deploy_success": False,
            "summary": "Could not determine a proposed fix from the error message. Please provide proposed_fix.",
            "rca_confidence": confidence,
        }

    fix_result = await mcp_manager.ask_fix_and_deploy(
        iflow_id=req.iflow_id,
        error_message=req.error_message,
        proposed_fix=proposed_fix,
        root_cause=root_cause,
        error_type=error_type,
        affected_component=affected_component,
        user_id=req.user_id,
        session_id=session_id,
        timestamp=timestamp,
    )

    return {
        "iflow_id":         req.iflow_id,
        "fix_applied":      fix_result.get("fix_applied", False),
        "deploy_success":   fix_result.get("deploy_success", False),
        "success":          fix_result.get("success", False),
        "summary":          fix_result.get("summary", ""),
        "rca_confidence":   confidence,
        "proposed_fix":     proposed_fix,
        "steps_count":      len(fix_result.get("steps", [])),
    }


@app.get("/get_all_history")
async def get_history_endpoint(user_id: Optional[str] = None):
    try:
        return {"history": get_all_history(user_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get_testsuite_logs")
async def get_testsuite_logs(user_id: Optional[str] = None):
    try:
        return {"ts_logs": get_testsuite_log_entries(user_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════
# AUTONOMOUS ROUTES
# ══════════════════════════════════════════════

@app.post("/autonomous/start")
async def start_autonomous():
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    started = mcp_manager.start_autonomous()
    return {"status": "started" if started else "already_running",
            "poll_interval_seconds": POLL_INTERVAL_SECONDS}


@app.post("/autonomous/stop")
async def stop_autonomous():
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    stopped = mcp_manager.stop_autonomous()
    return {"status": "stopped" if stopped else "not_running"}


@app.get("/autonomous/status")
async def autonomous_status():
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    return {
        "running":                mcp_manager._autonomous_running,
        "poll_interval_seconds":  POLL_INTERVAL_SECONDS,
        "auto_fix_confidence":    AUTO_FIX_CONFIDENCE,
        "suggest_fix_confidence": SUGGEST_FIX_CONFIDENCE,
        "auto_fix_all":           AUTO_FIX_ALL_CPI_ERRORS,
        "auto_deploy":            AUTO_DEPLOY_AFTER_FIX,
    }


# ══════════════════════════════════════════════
# AUTO-FIX CONFIGURATION ROUTES
# ══════════════════════════════════════════════

@app.get("/api/config/auto-fix")
async def get_auto_fix_status():
    """Get current auto-fix configuration status"""
    try:
        from config.config import Config
        enabled = Config.get_auto_fix_enabled()
        env_value = os.getenv("AUTO_FIX_ENABLED", "false").lower() == "true"
        source = "runtime" if enabled != env_value else "env"
        return {
            "enabled": enabled,
            "source": source,
            "env_default": env_value,
            "timestamp": get_hana_timestamp()
        }
    except Exception as e:
        logger.error(f"Error getting auto-fix status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/auto-fix")
async def set_auto_fix_status(enabled: bool):
    """Set auto-fix configuration"""
    try:
        from config.config import Config
        success = Config.set_auto_fix_enabled(enabled)
        if success:
            logger.info(f"Auto-fix {'enabled' if enabled else 'disabled'} via API")
            return {
                "success": True,
                "enabled": enabled,
                "message": f"Auto-fix {'enabled' if enabled else 'disabled'} successfully",
                "timestamp": get_hana_timestamp()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to update configuration")
    except Exception as e:
        logger.error(f"Error setting auto-fix status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/auto-fix/reset")
async def reset_auto_fix_to_env():
    """Reset auto-fix to use .env value"""
    try:
        from config.config import Config
        success = Config.reset_auto_fix_to_env()
        if success:
            env_value = os.getenv("AUTO_FIX_ENABLED", "false").lower() == "true"
            logger.info(f"Auto-fix reset to .env value: {env_value}")
            return {
                "success": True,
                "enabled": env_value,
                "message": "Auto-fix reset to .env configuration",
                "timestamp": get_hana_timestamp()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to reset configuration")
    except Exception as e:
        logger.error(f"Error resetting auto-fix: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/autonomous/cpi/errors")
async def get_cpi_error_inventory(
    message_limit: int = FAILED_MESSAGE_FETCH_LIMIT,
    artifact_limit: int = RUNTIME_ERROR_FETCH_LIMIT,
):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    try:
        return await mcp_manager.error_fetcher.fetch_cpi_error_inventory(
            message_limit=message_limit,
            artifact_limit=artifact_limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/autonomous/cpi/messages/errors")
async def get_cpi_message_errors(limit: int = FAILED_MESSAGE_FETCH_LIMIT):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    try:
        raw_errors = await mcp_manager.error_fetcher.fetch_failed_messages(limit=limit)
        normalized = []
        for raw in raw_errors:
            guid = raw.get("MessageGuid", "")
            details = await mcp_manager.error_fetcher.fetch_error_details(guid) if guid else {}
            normalized.append(mcp_manager.error_fetcher.normalize(raw, details))
        return {"count": len(normalized), "messages": normalized}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/autonomous/cpi/runtime_artifacts/errors")
async def get_cpi_runtime_artifact_errors(limit: int = RUNTIME_ERROR_FETCH_LIMIT):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    try:
        artifacts = await mcp_manager.error_fetcher.fetch_runtime_artifact_errors(limit=limit)
        return {"count": len(artifacts), "artifacts": artifacts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/autonomous/cpi/runtime_artifacts/{artifact_id}")
async def get_cpi_runtime_artifact_detail(artifact_id: str):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    try:
        detail = await mcp_manager.error_fetcher.fetch_runtime_artifact_detail(artifact_id)
        error_information = await mcp_manager.error_fetcher.fetch_runtime_artifact_error_detail(artifact_id)
        if not detail and not error_information:
            raise HTTPException(status_code=404, detail="Runtime artifact not found")
        return {
            "artifact_id": artifact_id,
            "detail": detail,
            "error_information": error_information,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/autonomous/tools")
async def list_loaded_tools(server: Optional[str] = None):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")

    grouped: Dict[str, List[Dict[str, str]]] = {}
    for tool in mcp_manager.tools:
        if server and tool.server != server:
            continue
        grouped.setdefault(tool.server, []).append({
            "agent_tool_name": tool.name,
            "mcp_tool_name": tool.mcp_tool_name,
            "description": tool.description,
            "fields": mcp_manager.get_tool_field_names(tool.server, tool.mcp_tool_name),
        })

    if server and server not in grouped:
        return {"server": server, "tools": [], "count": 0}

    if server:
        return {"server": server, "tools": grouped[server], "count": len(grouped[server])}

    return {
        "servers": grouped,
        "counts": {name: len(items) for name, items in grouped.items()},
        "total": sum(len(items) for items in grouped.values()),
    }


def _resolve_incident_reference(incident_ref: str) -> Optional[Dict]:
    incident = get_incident_by_id(incident_ref)
    if incident:
        return incident
    return get_incident_by_message_guid(incident_ref)


@app.get("/autonomous/incidents")
async def get_incidents(status: Optional[str] = None, limit: int = 50):
    try:
        incidents = get_all_incidents(status=status, limit=limit)
        return {"incidents": incidents, "total": len(incidents)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/autonomous/incidents/{incident_id}")
async def get_incident(incident_id: str):
    try:
        incident = _resolve_incident_reference(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        return incident
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/autonomous/incidents/{incident_id}/view_model")
async def get_incident_view_model(incident_id: str):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    try:
        incident = _resolve_incident_reference(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        return await mcp_manager.build_incident_view_model(incident)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/autonomous/incidents/{incident_id}/approve")
async def approve_fix(incident_id: str, req: ApprovalRequest, background_tasks: BackgroundTasks):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    incident = _resolve_incident_reference(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    resolved_incident_id = incident["incident_id"]
    if incident.get("status") not in ("AWAITING_APPROVAL", "RCA_COMPLETE"):
        raise HTTPException(status_code=400,
                            detail=f"Incident status '{incident.get('status')}' is not approvable")
    if req.approved:
        update_incident(resolved_incident_id, {"status": "FIX_IN_PROGRESS"})
        background_tasks.add_task(_apply_fix_background, resolved_incident_id, dict(incident))
        return {
            "status":       "fix_started",
            "incident_id":  resolved_incident_id,
            "message_guid": incident.get("message_guid"),
        }
    else:
        update_incident(resolved_incident_id, {"status": "REJECTED", "comment": req.comment or "Rejected by user"})
        return {
            "status":       "rejected",
            "incident_id":  resolved_incident_id,
            "message_guid": incident.get("message_guid"),
        }


@app.post("/autonomous/incidents/{incident_id}/generate_fix")
async def generate_fix_for_incident(incident_id: str, background_tasks: BackgroundTasks, sync: bool = False):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    incident = _resolve_incident_reference(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    current_status = incident.get("status")
    if current_status not in (
        "AWAITING_APPROVAL", "RCA_COMPLETE",
        "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Incident status '{current_status}' cannot generate a fix right now"
        )

    resolved_incident_id = incident["incident_id"]
    if sync:
        try:
            return await mcp_manager.execute_incident_fix(dict(incident), human_approved=True)
        except Exception as e:
            logger.error(f"[generate_fix_for_incident] {e}")
            raise HTTPException(status_code=500, detail=str(e))

    update_incident(resolved_incident_id, {"status": "FIX_IN_PROGRESS"})
    background_tasks.add_task(_apply_fix_background, resolved_incident_id, dict(incident))
    return {
        "status": "fix_started",
        "message": "AI fix generation and apply flow started in background",
        "incident_id": resolved_incident_id,
        "message_guid": incident.get("message_guid"),
    }


async def _apply_fix_background(incident_id: str, incident: Dict):
    try:
        await mcp_manager.execute_incident_fix(dict(incident), human_approved=True)
    except Exception as e:
        logger.error(f"[_apply_fix_background] {e}")
        update_incident(incident_id, {"status": "FIX_FAILED", "fix_summary": str(e)})


@app.post("/autonomous/incidents/{incident_id}/retry_rca")
async def retry_rca(incident_id: str, background_tasks: BackgroundTasks):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")
    incident = _resolve_incident_reference(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.get("status") in {
        "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED", "REJECTED", "TICKET_CREATED",
    }:
        raise HTTPException(status_code=400,
                            detail=f"Incident status '{incident.get('status')}' cannot be retried")
    resolved_incident_id = incident["incident_id"]
    update_incident(resolved_incident_id, {"status": "RCA_IN_PROGRESS"})
    background_tasks.add_task(_retry_rca_background, resolved_incident_id, dict(incident))
    return {"status": "rca_started", "incident_id": resolved_incident_id}


async def _retry_rca_background(incident_id: str, incident: Dict):
    try:
        rca = await mcp_manager.run_rca(incident)
        update_incident(incident_id, {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
        })
        await mcp_manager.remediation_gate(dict(incident), rca)
    except Exception as e:
        logger.error(f"[_retry_rca_background] {e}")
        update_incident(incident_id, {"status": "RCA_FAILED", "root_cause": str(e)})


@app.get("/autonomous/incidents/{incident_id}/fix_patterns")
async def get_fix_patterns(incident_id: str):
    incident = _resolve_incident_reference(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    sig      = MultiMCP.error_signature(incident.get("iflow_id", ""), incident.get("error_type", ""), incident.get("error_message", ""))
    patterns = get_similar_patterns(sig)
    return {"patterns": patterns, "signature": sig}


@app.get("/autonomous/pending_approvals")
async def list_pending_approvals():
    try:
        pending = get_pending_approvals()
        for incident in pending:
            incident["approval_ref"]      = incident.get("incident_id")
            incident["message_guid_ref"]  = incident.get("message_guid")
        return {"pending": pending}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/autonomous/manual_trigger")
async def manual_trigger(background_tasks: BackgroundTasks):
    if mcp_manager is None:
        raise HTTPException(status_code=503, detail="MCP manager not ready")

    async def one_shot():
        try:
            raw_errors = await mcp_manager.error_fetcher.fetch_failed_messages(limit=FAILED_MESSAGE_FETCH_LIMIT)
            unique_raw_errors = mcp_manager.dedupe_raw_failed_messages(raw_errors)
            for raw in unique_raw_errors:
                guid = raw.get("MessageGuid", "")
                if not guid:
                    continue
                existing = get_incident_by_message_guid(guid)
                if existing and existing.get("status") not in {
                    "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "RETRIED",
                    "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
                    "REJECTED", "TICKET_CREATED", "ARTIFACT_MISSING", "VERIFICATION_UNAVAILABLE",
                }:
                    continue
                error_detail   = await mcp_manager.error_fetcher.fetch_error_details(guid)
                normalized     = mcp_manager.error_fetcher.normalize(raw, error_detail)
                await mcp_manager.process_detected_error(normalized)

            runtime_errors = await mcp_manager.error_fetcher.fetch_runtime_artifact_errors(
                limit=RUNTIME_ERROR_FETCH_LIMIT
            )
            for normalized in runtime_errors:
                await mcp_manager.process_detected_error(normalized)
        except Exception as e:
            logger.error(f"[manual_trigger] {e}")

    background_tasks.add_task(one_shot)
    return {"status": "triggered", "message": "One-shot poll started in background"}


@app.post("/autonomous/test_incident")
async def inject_test_incident(background_tasks: BackgroundTasks):
    """Inject a synthetic incident to test the full RCA + fix + deploy pipeline."""
    incident_id = str(uuid.uuid4())
    incident = {
        "incident_id":    incident_id,
        "message_guid":   "TEST-" + incident_id[:8],
        "iflow_id":       "EH8-BPP-Material-UPSERT",
        "sender":         "S4HANA",
        "receiver":       "BPP",
        "status":         "DETECTED",
        "error_type":     "MAPPING_ERROR",
        "error_message":  "MappingException: Field 'NetPrice' does not exist in target structure. Did you mean 'NetAmount'?",
        "correlation_id": "COR-TEST-001",
        "log_start":      get_hana_timestamp(),
        "log_end":        get_hana_timestamp(),
        "created_at":     get_hana_timestamp(),
        "tags":           ["mapping","schema"],
    }
    create_incident(incident)

    async def run_full_pipeline():
        update_incident(incident_id, {"status": "RCA_IN_PROGRESS"})
        rca = await mcp_manager.run_rca(incident)
        update_incident(incident_id, {
            "status":             "RCA_COMPLETE",
            "root_cause":         rca.get("root_cause", ""),
            "proposed_fix":       rca.get("proposed_fix", ""),
            "rca_confidence":     rca.get("confidence", 0.0),
            "affected_component": rca.get("affected_component", ""),
        })
        await mcp_manager.remediation_gate(dict(incident), rca)

    background_tasks.add_task(run_full_pipeline)
    return {"status": "test_incident_created", "incident_id": incident_id}


# ── debug endpoints ──────────────────────────

@app.get("/autonomous/db_test")
async def db_test():
    import traceback
    test_id = str(uuid.uuid4())
    try:
        create_incident({
            "incident_id":   test_id,
            "message_guid":  "TEST-DB",
            "iflow_id":      "TEST-IFLOW",
            "status":        "DETECTED",
            "error_type":    "MAPPING_ERROR",
            "error_message": "test error",
            "created_at":    get_hana_timestamp(),
            "tags":          [],
        })
        fetched = get_incident_by_id(test_id)
        return {"create": "OK" if fetched else "FAILED", "fetch": fetched,
                "total": len(get_all_incidents())}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


@app.get("/autonomous/debug")
async def autonomous_debug():
    results = {
        "env_vars": {
            "SAP_HUB_TENANT_URL":    os.getenv("SAP_HUB_TENANT_URL", "NOT SET"),
            "SAP_HUB_TOKEN_URL":     os.getenv("SAP_HUB_TOKEN_URL",  "NOT SET"),
            "SAP_HUB_CLIENT_ID":     "SET" if os.getenv("SAP_HUB_CLIENT_ID")     else "NOT SET",
            "SAP_HUB_CLIENT_SECRET": "SET" if os.getenv("SAP_HUB_CLIENT_SECRET") else "NOT SET",
        },
        "autonomous_running": mcp_manager._autonomous_running if mcp_manager else False,
        "auto_fix_all":       AUTO_FIX_ALL_CPI_ERRORS,
        "auto_deploy":        AUTO_DEPLOY_AFTER_FIX,
        "fetch_test":         None,
        "fetch_error":        None,
    }
    try:
        errors = await mcp_manager.error_fetcher.fetch_failed_messages()
        results["fetch_test"] = f"SUCCESS - got {len(errors)} messages"
    except Exception as e:
        results["fetch_error"] = str(e)
    return results


@app.get("/autonomous/debug2")
async def autonomous_debug2():
    results = {}
    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.post(
                os.getenv("SAP_HUB_TOKEN_URL"),
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     os.getenv("SAP_HUB_CLIENT_ID"),
                    "client_secret": os.getenv("SAP_HUB_CLIENT_SECRET"),
                },
            )
            results["token_status"] = resp.status_code
            if resp.status_code != 200:
                results["token_error"] = resp.text
                return results
            token = resp.json()["access_token"]
            results["token"] = "OK"
    except Exception as e:
        results["token_exception"] = str(e)
        return results

    try:
        base   = os.getenv("SAP_HUB_TENANT_URL", "").rstrip("/")
        params = {"$filter": "Status eq 'FAILED'", "$orderby": "LogEnd desc",
                  "$top": "5", "$format": "json"}
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.get(
                f"{base}/api/v1/MessageProcessingLogs",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            results["api_status"]           = resp.status_code
            results["api_response_preview"] = resp.text[:500]
    except Exception as e:
        results["api_exception"] = str(e)
    return results


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        reload_excludes=["*.log", "*.db", "logs/*", "__pycache__"],
    )
