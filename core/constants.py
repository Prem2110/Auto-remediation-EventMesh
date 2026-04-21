"""
core/constants.py
=================
All configuration constants, environment variables, remediation policies,
prompt templates, and Groovy/XML reference material.
Imported by all agents and main.py — never import from main.py here.
"""

import os
from pathlib import Path
from typing import Dict

# ─────────────────────────────────────────────
# MCP SERVERS
# ─────────────────────────────────────────────
MCP_SERVERS = {
    "integration_suite": "https://sap-integration-suite-mcp-lean-capybara-mb.cfapps.us10-001.hana.ondemand.com/mcp",
    "mcp_testing":       "https://iflow-test-mcp-py-wise-fox-ay.cfapps.us10-001.hana.ondemand.com/mcp",
    "documentation_mcp": "https://Documentation-Agent-py-reflective-armadillo-kx.cfapps.us10-001.hana.ondemand.com/mcp",
}

TRANSPORT_OPTIONS = {
    "integration_suite": {"verify": True,  "timeout": 300.0},
    "mcp_testing":       {"verify": False, "timeout": 10.0},
    "documentation_mcp": {"verify": False, "timeout": 10.0},
}

SERVER_ROUTING_GUIDE = {
    "documentation_mcp": "Use for SAP-standard documentation/specification/template generation.",
    "integration_suite": "Use for iFlow and SAP Integration Suite design/creation/deployment tasks.",
    "mcp_testing":       "Use for validation, test execution, and test-report related tasks.",
}

MAX_RETRIES  = 3
MEMORY_LIMIT = 12
MEMORY_SESSION_TTL_SECONDS   = int(os.getenv("MEMORY_SESSION_TTL_SECONDS", "3600"))
MAX_MEMORY_SESSIONS          = int(os.getenv("MAX_MEMORY_SESSIONS", "500"))
FIX_PROGRESS_TTL_SECONDS     = int(os.getenv("FIX_PROGRESS_TTL_SECONDS", "7200"))
MAX_FIX_PROGRESS_ENTRIES     = int(os.getenv("MAX_FIX_PROGRESS_ENTRIES", "1000"))
LOCAL_QUEUE_MAXSIZE          = int(os.getenv("LOCAL_QUEUE_MAXSIZE", "1000"))

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
FAILED_MESSAGE_FETCH_LIMIT          = int(os.getenv("FAILED_MESSAGE_FETCH_LIMIT", "100"))
RUNTIME_ERROR_FETCH_LIMIT           = int(os.getenv("RUNTIME_ERROR_FETCH_LIMIT", "200"))
MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE = int(os.getenv("MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE", "25"))
RUNTIME_ERROR_DETAIL_FETCH_LIMIT    = int(os.getenv("RUNTIME_ERROR_DETAIL_FETCH_LIMIT", "25"))
RUNTIME_ERROR_DETAIL_CONCURRENCY    = int(os.getenv("RUNTIME_ERROR_DETAIL_CONCURRENCY", "8"))
MAX_CONSECUTIVE_FAILURES            = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))
PENDING_APPROVAL_TIMEOUT_HRS        = int(os.getenv("PENDING_APPROVAL_TIMEOUT_HRS", "24"))
PATTERN_MIN_SUCCESS_COUNT           = int(os.getenv("PATTERN_MIN_SUCCESS_COUNT", "2"))
TICKET_DEFAULT_ASSIGNEE             = os.getenv("TICKET_DEFAULT_ASSIGNEE", "")
BURST_DEDUP_WINDOW_SECONDS          = int(os.getenv("BURST_DEDUP_WINDOW_SECONDS", "60"))

# ─────────────────────────────────────────────
# EVENT MESH CONFIG
# ─────────────────────────────────────────────
AEM_ENABLED              = os.getenv("AEM_ENABLED", "false").lower() == "true"
AEM_REST_URL             = os.getenv("AEM_REST_URL", "")
AEM_USERNAME             = os.getenv("AEM_USERNAME", "")
AEM_PASSWORD             = os.getenv("AEM_PASSWORD", "")
AEM_QUEUE_PREFIX         = os.getenv("AEM_QUEUE_PREFIX", "sap/cpi/remediation")

EVENT_MESH_QUEUE         = os.getenv("EVENT_MESH_QUEUE", os.getenv("AEM_OBSERVER_QUEUE", "cpi/evt/02/autofix/observer/out"))
EVENT_MESH_TOKEN_URL     = os.getenv("EVENT_MESH_TOKEN_URL", "")
EVENT_MESH_CLIENT_ID     = os.getenv("EVENT_MESH_CLIENT_ID", "")
EVENT_MESH_CLIENT_SECRET = os.getenv("EVENT_MESH_CLIENT_SECRET", "")

# ─────────────────────────────────────────────
# REMEDIATION POLICIES
# ─────────────────────────────────────────────
REMEDIATION_POLICIES: Dict[str, Dict] = {
    "MAPPING_ERROR":         {"action": "AUTO_FIX",       "replay_after_fix": True},
    "DATA_VALIDATION":       {"action": "AUTO_FIX",       "replay_after_fix": True},
    "SSL_ERROR":             {"action": "TICKET_CREATED", "replay_after_fix": False},
    "AUTH_CONFIG_ERROR":     {"action": "AUTO_FIX",       "replay_after_fix": True},
    "AUTH_ERROR":            {"action": "APPROVAL",       "replay_after_fix": False},
    "CONNECTIVITY_ERROR":    {"action": "RETRY",          "replay_after_fix": True},
    "ADAPTER_CONFIG_ERROR":  {"action": "AUTO_FIX",       "replay_after_fix": True},
    "BACKEND_ERROR":         {"action": "TICKET_CREATED", "replay_after_fix": False},
    "SFTP_ERROR":            {"action": "TICKET_CREATED", "replay_after_fix": False},
    "DUPLICATE_ERROR":       {"action": "TICKET_CREATED", "replay_after_fix": False},
    "PAYLOAD_SIZE_ERROR":    {"action": "TICKET_CREATED", "replay_after_fix": False},
    "UNKNOWN_ERROR":         {"action": "APPROVAL",       "replay_after_fix": False},
}

ACTION_HINTS: Dict[str, str] = {
    "MAPPING_ERROR":        "Agent is auto-fixing the message mapping — redeploy will follow automatically.",
    "DATA_VALIDATION":      "Agent is auto-fixing payload validation — redeploy will follow automatically.",
    "SSL_ERROR":            "SSL/TLS certificate error — certificate renewal or trust store update required. A ticket has been created.",
    "AUTH_CONFIG_ERROR":    "Wrong credential alias or security material reference inside iFlow — agent is fixing the adapter config and redeploying.",
    "AUTH_ERROR":           "Authentication failed — could not determine if iFlow config or credentials are at fault. Awaiting human review.",
    "CONNECTIVITY_ERROR":   "Transient network issue — the agent will retry the iFlow automatically.",
    "ADAPTER_CONFIG_ERROR": "iFlow receiver adapter is misconfigured (HTTP 4xx) — agent is fixing the endpoint URL, method, or headers.",
    "BACKEND_ERROR":        "Backend service returned HTTP 5xx — the iFlow is working correctly. Backend team must investigate and restore the service.",
    "SFTP_ERROR":           "SFTP server-side issue — verify the target directory exists and the SFTP user has write/read access. Re-trigger the iFlow once resolved.",
    "DUPLICATE_ERROR":      "Duplicate record rejected by backend — idempotency issue requires backend or source-system fix. A ticket has been created.",
    "PAYLOAD_SIZE_ERROR":   "Message payload exceeds size limit — splitting or infrastructure change required. A ticket has been created.",
    "UNKNOWN_ERROR":        "Error could not be classified automatically — awaiting human review before any fix is applied.",
}

_STATUS_ACTION_HINTS: Dict[str, str] = {
    "TICKET_CREATED":           "A support ticket has been created — waiting for the responsible team to resolve the underlying issue.",
    "AWAITING_APPROVAL":        "Fix is ready but requires human approval before it is applied.",
    "FIX_IN_PROGRESS":          "Agent is currently applying the fix — check back shortly.",
    "FIX_VERIFIED":             "Fix was applied and verified successfully — no further action needed.",
    "HUMAN_INITIATED_FIX":      "Fix was applied manually — no further action needed.",
    "RETRIED":                  "iFlow was retried automatically — monitor the next execution.",
    "FIX_FAILED":               "Automatic fix failed — review the fix log and apply the suggested change manually.",
    "FIX_FAILED_UPDATE":        "Fix could not be uploaded to SAP CPI — check artifact permissions and retry.",
    "FIX_FAILED_DEPLOY":        "Fix was uploaded but deployment failed — check CPI deploy logs and retry deploy.",
    "FIX_FAILED_RUNTIME":       "iFlow deployed but failed again at runtime — a deeper manual investigation is needed.",
    "ARTIFACT_MISSING":         "iFlow artifact could not be found in SAP CPI — verify the iFlow ID and package.",
    "REJECTED":                 "Fix was rejected by the approver — re-open and submit a revised fix if needed.",
    "RCA_INCONCLUSIVE":         "Root cause could not be determined — additional logs or manual analysis required.",
    "VERIFICATION_UNAVAILABLE": "Fix deployed but verification test could not run — manually verify the iFlow in CPI.",
}

TRANSIENT_ERROR_MARKERS = (
    "429", "503", "service unavailable", "too many requests",
    "connection refused", "connect timed out", "socketexception", "temporarily unavailable",
)

FIX_INTENT_KEYWORDS = (
    "fix", "repair", "resolve", "remediate", "apply fix",
    "auto fix", "self heal", "heal", "correct the error", "fix the error",
    "fix and deploy", "fix iflow", "deploy fix",
)

FALLBACK_FIX_BY_ERROR_TYPE: Dict[str, str] = {
    "MAPPING_ERROR": (
        "In the iFlow XML, locate the failing <bpmn2:serviceTask> element for the mapping step. "
        "Inspect its <ifl:property> entries for source/target field references. "
        "If a field was renamed or removed, update the XPath expression or field reference in that property. "
        "For namespace errors: declare the namespace inline in the XPath value using "
        "'declare namespace prefix=\\'uri\\'; //prefix:element'. "
        "Apply only the minimal property-level change. Call validate_iflow_xml, then update-iflow, then deploy-iflow."
    ),
    "DATA_VALIDATION": (
        "In the iFlow XML, locate the Content Modifier or mapping step that rejects the payload. "
        "To add a null/empty guard: set a property using XPath "
        "'string-length(normalize-space(/ns:root/ns:field)) > 0'. "
        "To add a default value: add an <ifl:property name='CamelHttpMethod'> or use a Content Modifier "
        "step's Header/Property section to set a fallback. "
        "Apply as a property-level change on the existing step — do NOT add new structural components. "
        "Call validate_iflow_xml, then update-iflow, then deploy-iflow."
    ),
    "SSL_ERROR": (
        "SSL/TLS certificate errors cannot be fixed by changing iFlow XML. "
        "Required actions: (1) renew the expired certificate, "
        "(2) update the trust store in SAP BTP, "
        "(3) verify the certificate chain is complete. "
        "A support ticket has been created."
    ),
    "AUTH_CONFIG_ERROR": (
        "Locate the receiver adapter in the iFlow XML. "
        "Find the <ifl:property name='credentialName'> or equivalent security material reference. "
        "Update the credential alias value to match the correct alias in BTP Security Material. "
        "Call validate_iflow_xml, then update-iflow, then deploy-iflow."
    ),
    "AUTH_ERROR": (
        "Authentication failure — could not determine from the error message whether the issue "
        "is a wrong credential alias in the iFlow (fixable) or expired/invalid credentials (infra). "
        "Human review required before any fix is applied."
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
    "DUPLICATE_ERROR": (
        "Duplicate record errors are caused by idempotency issues in the backend or source system. "
        "The iFlow cannot resolve duplicate keys — the backend team must handle deduplication. "
        "A support ticket has been created."
    ),
    "PAYLOAD_SIZE_ERROR": (
        "The message payload exceeds the configured size limit. "
        "This cannot be fixed by editing iFlow XML — options are: "
        "(1) split the payload in a preceding system, "
        "(2) request a limit increase from the backend team, "
        "(3) implement pagination in the source. "
        "A support ticket has been created."
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

# Load XML patterns reference from rules directory
_RULES_DIR = Path(__file__).parent.parent / "rules"
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

ERROR_TYPE_FIX_GUIDANCE: Dict[str, str] = {
    "MAPPING_ERROR": (
        "=== MAPPING_ERROR GUIDANCE ===\n"
        "- Download the iFlow and inspect message mapping steps.\n"
        "- Check for renamed, removed, or type-mismatched source/target fields.\n"
        "- If XSD files are referenced, verify the XSD structure still matches the mapping.\n"
        "- Update field names, re-link structures, and validate before deploying."
    ),
    "SSL_ERROR": (
        "=== SSL_ERROR GUIDANCE ===\n"
        "- SSL/TLS certificate errors CANNOT be resolved by editing iFlow XML.\n"
        "- The certificate is expired, the trust store is missing the CA, or the chain is broken.\n"
        "- Required actions: renew the certificate, update the SAP BTP trust store, verify the chain.\n"
        "- Escalate to the infra/security team — do NOT attempt an iFlow fix."
    ),
    "AUTH_CONFIG_ERROR": (
        "=== AUTH_CONFIG_ERROR GUIDANCE ===\n"
        "- The credential alias or security material reference inside the iFlow adapter is wrong.\n"
        "- Open the receiver adapter in the iFlow XML and find the credentialName / security material property.\n"
        "- Update the alias value to match the correct entry in BTP Security Material.\n"
        "- Do NOT hardcode credentials — use the alias name only.\n"
        "- Validate with validate_iflow_xml, then update-iflow, then deploy-iflow."
    ),
    "AUTH_ERROR": (
        "=== AUTH_ERROR GUIDANCE ===\n"
        "- Authentication failed but the root cause is ambiguous (could be iFlow config or actual credentials).\n"
        "- If the error references a specific credential alias → treat as AUTH_CONFIG_ERROR and fix the alias.\n"
        "- If the error mentions expired token / wrong password → escalate; credentials must be refreshed in BTP.\n"
        "- Do NOT attempt an iFlow fix without confirming the credential alias is the issue."
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
    "DUPLICATE_ERROR": (
        "=== DUPLICATE_ERROR GUIDANCE ===\n"
        "- The backend rejected the message because a record with the same key already exists.\n"
        "- This is an idempotency / deduplication issue in the backend or source system.\n"
        "- iFlow XML changes CANNOT resolve duplicate key violations.\n"
        "- Escalate to the backend/source-system team to handle deduplication logic."
    ),
    "PAYLOAD_SIZE_ERROR": (
        "=== PAYLOAD_SIZE_ERROR GUIDANCE ===\n"
        "- The message payload exceeds the size limit of the backend or CPI runtime.\n"
        "- iFlow XML changes CANNOT increase system size limits.\n"
        "- Options: split the payload at the source, implement pagination, or request a limit increase.\n"
        "- Escalate to the integration architect or backend team."
    ),
}

FIX_OPERATION_PROMPT_TEMPLATE = """
YOU ARE AN SAP CPI DIAGNOSIS AGENT.
Your ONLY job: read the iFlow XML below and return a structured fix operation.
DO NOT call update-iflow or deploy-iflow. The fix will be applied programmatically.

=== INCIDENT CONTEXT ===
iFlow ID:          {iflow_id}
Error Type:        {error_type}
Raw SAP Error:     {error_message}
RCA — Root Cause:  {root_cause}
RCA — Fix Spec:    {proposed_fix}
Affected Component:{affected_component}
{pattern_history}
{sap_notes}
{error_type_guidance}

=== iFlow XML (pre-fetched — do NOT call get-iflow unless XML is missing below) ===
{original_xml}

=== YOUR TASK ===
1. Read the XML above.
2. Identify the minimal change needed to resolve the error.
3. Return EXACTLY ONE JSON object or array — no markdown, no preamble, no extra text.

Option A — update an existing property value (most common):
{{"change_type": "update_property", "target_component": "<exact id= from XML>",
  "field": "<exact <key> text>", "old_value": "<current value — copy exactly>", "new_value": "<corrected value>"}}

Option B — add a missing property:
{{"change_type": "add_property", "target_component": "<exact id= from XML>",
  "field": "<key to add>", "old_value": "", "new_value": "<value>"}}

Option C — update an XML attribute on the element itself:
{{"change_type": "update_attribute", "target_component": "<exact id= from XML>",
  "field": "<attribute name>", "old_value": "<current value>", "new_value": "<corrected value>"}}

Option D — XPath / Groovy expression inside a property (same as A but explicit):
{{"change_type": "update_expression", "target_component": "<exact id= from XML>",
  "field": "<property key>", "old_value": "<current expression — copy exactly>", "new_value": "<corrected expression>"}}

Option E — structural change required (add/remove steps, new channels):
{{"change_type": "structural", "target_component": "", "field": "", "old_value": "", "new_value": "",
  "reason": "<why structural change is needed>"}}

For multiple changes: return a JSON array [{{}}, {{}}]

CRITICAL:
- target_component MUST be the exact id= attribute value from the XML (e.g. "ReceiverHTTP_1")
- old_value MUST be copied verbatim from the XML — do NOT paraphrase
- If you cannot find the exact component or confirm the field, return change_type="structural"
- Return ONLY JSON — no explanation, no markdown fences
"""

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
{sap_notes}

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
{targeted_component_hint}
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
{targeted_component_hint}
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
