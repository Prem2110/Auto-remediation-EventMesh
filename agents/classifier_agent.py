"""
agents/classifier_agent.py
==========================
Rule-based error classifier for SAP CPI incidents.

ClassifierAgent exposes three pure-Python classifiers as both instance methods
and LangChain @tool callables so the orchestrator can invoke them directly OR
wire them into a LangChain agent's tool list.

Exports:
  ClassifierAgent
    .classify_error(error_message)        → {"error_type", "confidence", "tags", "is_iflow_fixable"}
    .is_iflow_fixable(error_type)         → bool
    .classify_with_llm(msg, iflow_id)     → {"error_type", "confidence", "tags", "is_iflow_fixable"}
    .error_signature(...)                 → md5 hex string used as DB dedup key
    .fallback_root_cause(...)             → human-readable fallback root-cause string
    .create_tools()                       → List[BaseTool] — LangChain-compatible wrappers
"""

import hashlib
import json
import logging
import re
from typing import Any, Dict, List

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# Error types that can be fixed by modifying iFlow XML.
# All others require infrastructure/backend/manual action.
FIXABLE_ERROR_TYPES: frozenset = frozenset({
    "MAPPING_ERROR",
    "ADAPTER_CONFIG_ERROR",
    "AUTH_CONFIG_ERROR",
    "DATA_VALIDATION",
    "GROOVY_ERROR",
    "SOAP_ERROR",
    "ODATA_ERROR",
    "ROUTING_ERROR",
    "PROPERTY_ERROR",
    "SCRIPT_ERROR",
    "UNKNOWN_ERROR",   # attempt — let RCA decide
})


class ClassifierAgent:
    """Pure rule-based error classifier. No LLM calls, no MCP connections."""

    def __init__(self):
        self._agent = None

    # ── is_iflow_fixable ────────────────────────────────────────────────────

    @staticmethod
    def is_iflow_fixable(error_type: str) -> bool:
        """Return True if the error type is resolvable by changing the iFlow XML."""
        return error_type in FIXABLE_ERROR_TYPES

    # ── classify_error ──────────────────────────────────────────────────────

    @staticmethod
    def classify_error(error_message: str) -> Dict[str, Any]:
        """
        Rule-based classifier — returns error_type, confidence, tags, and is_iflow_fixable.

        Order matters: more specific / higher-confidence rules are checked first
        to avoid false positives from broad keyword matches lower in the list.
        """
        msg = (error_message or "").lower()

        def _r(error_type: str, confidence: float, tags: List[str]) -> Dict[str, Any]:
            return {
                "error_type":       error_type,
                "confidence":       confidence,
                "tags":             tags,
                "is_iflow_fixable": ClassifierAgent.is_iflow_fixable(error_type),
            }

        # ── Resource exhaustion — check early; OOM can mask any other error ──
        if any(k in msg for k in [
            "outofmemoryerror", "java heap space", "gc overhead limit",
            "out of memory", "heap space", "permgen space", "metaspace",
            "threadpool exhausted", "too many open files",
        ]):
            return _r("RESOURCE_ERROR", 0.93, ["resource", "memory"])

        # ── SFTP — before AUTH_ERROR; SFTP auth failures are server-side ──
        if any(k in msg for k in [
            "sftp", "sshexception", "jsch", "failed to connect sftp",
            "cannot open channel", "publickey", "hostkey",
            "known hosts", "host key", "no such file", "no such directory",
            "file already exists", "quota exceeded", "no space left",
        ]):
            return _r("SFTP_ERROR", 0.93, ["sftp", "filesystem"])
        if "permission denied" in msg and any(k in msg for k in ["sftp", "ssh", "ftp"]):
            return _r("SFTP_ERROR", 0.92, ["sftp", "filesystem"])

        # ── SSL / TLS — before AUTH; cert renewal is infra, not an iFlow change ──
        if any(k in msg for k in [
            "ssl handshake", "tls handshake", "sslhandshakeexception",
            "pkix path", "certificate expired", "certificate has expired",
            "handshake_failure", "ssl alert", "sslexception",
            "peer not authenticated", "sun.security.validator",
            "unable to find valid certification", "cert_untrusted",
        ]):
            return _r("SSL_ERROR", 0.93, ["ssl", "cert"])

        # ── Groovy / script execution — before generic Java exceptions ──
        if any(k in msg for k in [
            "groovyscript", "groovy script", "groovy.lang",
            "script execution", "executescript", "javax.script",
            "com.sap.it.rt.pipeline.groovy", "groovyexception",
            "script step failed", "script threw an exception",
        ]):
            return _r("GROOVY_ERROR", 0.92, ["groovy", "script"])
        # NullPointerException / ClassCastException inside a script context
        if any(k in msg for k in ["nullpointerexception", "classcastexception", "classnotfoundexception"]) \
                and any(k in msg for k in ["script", "groovy", "com.sap.it"]):
            return _r("GROOVY_ERROR", 0.88, ["groovy", "script"])

        # ── General script / custom code ──
        if any(k in msg for k in [
            "scriptengine", "script execution failed", "java.lang.runtimeexception",
            "com.sap.it.rt.pipeline.pipelineexception",
        ]) and "groovy" not in msg:
            return _r("SCRIPT_ERROR", 0.85, ["script", "runtime"])

        # ── IDoc / EDI ──
        if any(k in msg for k in [
            "idoc", "idoctype", "partner profile", "message type idoc",
            "bd10", "we19", "we20", "idoc_inbound", "idoc_outbound",
            "edifact", "edi_dc40", "idoc status", "idoc processing",
            "ale", "distribution model",
        ]):
            return _r("IDOC_ERROR", 0.91, ["idoc", "edi"])

        # ── SOAP / Web Service ──
        if any(k in msg for k in [
            "soapfault", "soap fault", "soap:fault", "wsdl",
            "soap action", "soapaction", "soap header",
            "soap envelope", "soap body", "webserviceexception",
            "com.sap.aii.af.lib.mp.module.soap",
            "axiom", "saaj", "soap message",
        ]):
            return _r("SOAP_ERROR", 0.91, ["soap", "webservice"])

        # ── OData ──
        if any(k in msg for k in [
            "odata", "$metadata", "entityset not found",
            "navigationproperty", "odata service", "odata error",
            "com.sap.cloud.sdk.odatav2", "com.sap.cloud.sdk.odatav4",
            "odataexception", "odata query", "entity not found",
        ]):
            return _r("ODATA_ERROR", 0.91, ["odata", "api"])

        # ── Routing / conditional branching ──
        if any(k in msg for k in [
            "no route", "router step", "no matching route",
            "message routing", "routingexception", "camel.no.consumers",
            "route not found", "condition not matched",
            "com.sap.aii.af.lib.mp.module.routing",
            "no branch matched", "content based routing",
        ]):
            return _r("ROUTING_ERROR", 0.89, ["routing", "condition"])

        # ── Exchange property / header missing ──
        if any(k in msg for k in [
            "property not found", "header not found",
            "exchange property", "property is null", "header is null",
            "missing header", "missing property", "camelexchange",
            "no value for property", "required property",
        ]):
            return _r("PROPERTY_ERROR", 0.88, ["property", "header"])

        # ── Auth config inside iFlow — wrong credential alias / security material ref ──
        if any(k in msg for k in [
            "credential alias", "no security artifact", "security material not found",
            "credential reference", "security material", "no credential",
        ]):
            return _r("AUTH_CONFIG_ERROR", 0.91, ["auth", "config"])

        # ── Auth / credential — ambiguous; route to APPROVAL not AUTO_FIX ──
        if any(k in msg for k in [
            "unauthorized", "invalid credentials", "credential",
            "token expired", "access token", "oauth",
        ]):
            return _r("AUTH_ERROR", 0.93, ["auth"])
        if any(k in msg for k in ["401", "403"]) and not any(k in msg for k in ["sftp", "ssh"]):
            return _r("AUTH_ERROR", 0.91, ["auth"])

        # ── Mapping / schema ──
        if any(k in msg for k in [
            "mappingexception", "does not exist in target",
            "target structure", "mapping runtime",
            "xpath", "namespace", "xslt", "transformation failed",
            "xsd validation", "schema mismatch", "element not found in schema",
            "source field", "target field", "cannot map",
        ]):
            return _r("MAPPING_ERROR", 0.90, ["mapping", "schema"])

        # ── Data validation ──
        if any(k in msg for k in [
            "mandatory", "required field", "null value",
            "validation failed", "data validation",
            "schema validation", "invalid payload",
            "field is required", "empty value", "missing mandatory",
        ]):
            return _r("DATA_VALIDATION", 0.87, ["validation", "data"])

        # ── Connectivity / network ──
        if any(k in msg for k in [
            "connection refused", "connect timed out", "read timed out",
            "unreachable", "socketexception", "network unreachable",
            "dns resolution", "no route to host", "connection reset",
            "broken pipe", "ioexception", "econnrefused",
            "host not found", "unknown host",
        ]):
            return _r("CONNECTIVITY_ERROR", 0.90, ["network", "timeout"])

        # ── Rate limiting — transient ──
        if any(k in msg for k in ["429", "too many requests", "rate limit", "rate limited", "throttl"]):
            return _r("CONNECTIVITY_ERROR", 0.82, ["network", "ratelimit"])

        # ── 5xx backend errors ──
        if any(k in msg for k in [
            "503", "service unavailable", "502", "bad gateway",
            "504", "gateway timeout",
        ]):
            return _r("BACKEND_ERROR", 0.87, ["backend", "5xx"])
        if any(k in msg for k in ["500", "internal server error"]):
            return _r("BACKEND_ERROR", 0.83, ["backend", "500"])

        # ── 4xx adapter config errors — iFlow sent a bad request ──
        if any(k in msg for k in [
            "400", "bad request", "404", "not found",
            "422", "unprocessable", "405", "method not allowed",
            "406", "415", "unsupported media type",
        ]):
            return _r("ADAPTER_CONFIG_ERROR", 0.83, ["adapter", "4xx"])

        # ── Duplicate / idempotency ──
        if any(k in msg for k in [
            "duplicate entry", "record already exists", "duplicate key",
            "already processed", "idempotency", "unique constraint",
            "duplicate record", "violates unique",
        ]):
            return _r("DUPLICATE_ERROR", 0.88, ["duplicate", "idempotency"])

        # ── Payload too large ──
        if any(k in msg for k in [
            "413", "payload too large", "request entity too large",
            "message size", "size limit exceeded", "content too large",
        ]):
            return _r("PAYLOAD_SIZE_ERROR", 0.88, ["payload", "size"])

        # ── Weak signals — broad keywords last ──
        if any(k in msg for k in ["mapping", "field", "structure"]):
            return _r("MAPPING_ERROR", 0.72, ["mapping", "schema"])
        if any(k in msg for k in ["expired", "tls"]):
            return _r("AUTH_ERROR", 0.70, ["auth"])
        if "ssl" in msg:
            return _r("SSL_ERROR", 0.70, ["ssl", "cert"])
        if any(k in msg for k in ["script", "groovy"]):
            return _r("GROOVY_ERROR", 0.70, ["groovy", "script"])
        if "soap" in msg:
            return _r("SOAP_ERROR", 0.70, ["soap", "webservice"])
        if "odata" in msg:
            return _r("ODATA_ERROR", 0.70, ["odata", "api"])
        if "idoc" in msg:
            return _r("IDOC_ERROR", 0.70, ["idoc", "edi"])

        return _r("UNKNOWN_ERROR", 0.50, [])

    # ── classify_with_llm ───────────────────────────────────────────────────

    async def classify_with_llm(
        self, error_message: str, iflow_id: str = ""
    ) -> Dict[str, Any]:
        """
        LLM-powered fallback classifier. Called only when rule-based returns
        UNKNOWN_ERROR or confidence < 0.70.

        Returns same shape as classify_error() or empty dict on failure.
        """
        if self._agent is None:
            return {}
        try:
            prompt = json.dumps({
                "iflow_id":      iflow_id,
                "error_message": error_message,
                "task": (
                    "Classify this SAP CPI error. Return ONLY valid JSON: "
                    '{"error_type": "...", "confidence": 0.0, "tags": []}'
                ),
            })
            result = await self._agent.ainvoke({"input": prompt})
            output = result.get("output", "") if isinstance(result, dict) else str(result)
            match  = re.search(r'\{[^{}]+\}', output)
            if match:
                parsed = json.loads(match.group())
                error_type = parsed.get("error_type", "UNKNOWN_ERROR")
                return {
                    "error_type":       error_type,
                    "confidence":       float(parsed.get("confidence", 0.60)),
                    "tags":             parsed.get("tags", []),
                    "is_iflow_fixable": self.is_iflow_fixable(error_type),
                }
        except Exception as exc:
            logger.warning("[Classifier] LLM fallback failed: %s", exc)
        return {}

    # ── error_signature ──────────────────────────────────────────────────────

    @staticmethod
    def error_signature(
        iflow_id: str,
        error_type: str,
        error_message: str = "",
    ) -> str:
        """
        Stable 16-char MD5 hex key used to look up fix patterns in the DB.

        GUIDs, timestamps, and long numeric IDs are stripped so the same
        logical error always produces the same signature regardless of IDs.
        """
        clean = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID
            r"|[A-Z0-9]{20,}"      # long message GUIDs / IDs
            r"|\b\d{4,}\b"         # standalone 4+ digit numbers
            r"|[\s]+",             # collapse whitespace
            " ",
            (error_message or "").lower(),
        ).strip()[:60]
        return hashlib.md5(f"{iflow_id}:{error_type}:{clean}".encode()).hexdigest()[:16]

    # ── fallback_root_cause ──────────────────────────────────────────────────

    @staticmethod
    def fallback_root_cause(error_type: str, error_message: str) -> str:
        """
        Human-readable root-cause description when the LLM RCA returns nothing useful.
        Keyed by error_type so each type gets a specific, actionable message.
        """
        if error_type == "MAPPING_ERROR":
            return (
                f"Message mapping is inconsistent with the latest structure or field definitions. "
                f"Error: {error_message}"
            )
        if error_type == "DATA_VALIDATION":
            return (
                f"Payload validation failed because required or type-safe input data is missing "
                f"or invalid. Error: {error_message}"
            )
        if error_type == "SSL_ERROR":
            return (
                f"SSL/TLS handshake or certificate validation failed. "
                f"The certificate may be expired or the trust store is misconfigured — "
                f"this requires infra/certificate renewal, not an iFlow change. "
                f"Error: {error_message}"
            )
        if error_type == "AUTH_CONFIG_ERROR":
            return (
                f"The credential alias or security material reference inside the iFlow adapter "
                f"is incorrect or missing. Update the credential alias in the receiver adapter "
                f"configuration and redeploy. "
                f"Error: {error_message}"
            )
        if error_type == "AUTH_ERROR":
            return (
                f"Authentication failed — could not determine whether the issue is a wrong "
                f"credential alias in the iFlow (fixable) or expired/invalid credentials (infra). "
                f"Human review required. Error: {error_message}"
            )
        if error_type == "DUPLICATE_ERROR":
            return (
                f"The backend rejected the message because an identical record already exists. "
                f"This is an idempotency issue — the iFlow cannot resolve duplicate records. "
                f"Error: {error_message}"
            )
        if error_type == "PAYLOAD_SIZE_ERROR":
            return (
                f"The message payload exceeds the size limit accepted by the backend or CPI runtime. "
                f"Splitting the payload or increasing backend limits requires design changes beyond iFlow XML. "
                f"Error: {error_message}"
            )
        if error_type == "ADAPTER_CONFIG_ERROR":
            return (
                f"The iFlow sent an incorrect request to the backend (HTTP 4xx) — the receiver "
                f"adapter URL path, HTTP method, or request format does not match what the backend "
                f"expects. Error: {error_message}"
            )
        if error_type == "BACKEND_ERROR":
            return (
                f"The backend service returned a server-side fault (HTTP 5xx). The iFlow is "
                f"working correctly — the backend must be investigated and restored by the "
                f"responsible team. Error: {error_message}"
            )
        if error_type == "CONNECTIVITY_ERROR":
            return (
                f"Network or destination connectivity to the receiver system failed. "
                f"Error: {error_message}"
            )
        if error_type == "GROOVY_ERROR":
            return (
                f"A Groovy script step inside the iFlow threw an exception during execution. "
                f"The script logic needs to be reviewed and corrected — this is fixable via iFlow XML update. "
                f"Error: {error_message}"
            )
        if error_type == "SCRIPT_ERROR":
            return (
                f"A custom script or Java/JavaScript step failed during iFlow execution. "
                f"Review the script logic and exception handling in the failing step. "
                f"Error: {error_message}"
            )
        if error_type == "IDOC_ERROR":
            return (
                f"An IDoc processing failure occurred — this is typically caused by a missing or "
                f"incorrect partner profile, message type, or IDoc type configuration in SAP. "
                f"This requires action in SAP WE20/BD54, not an iFlow XML change. "
                f"Error: {error_message}"
            )
        if error_type == "SOAP_ERROR":
            return (
                f"A SOAP fault or WSDL-related error occurred. The SOAP adapter configuration, "
                f"WSDL endpoint, or SOAP header in the iFlow may be incorrect — fixable via iFlow update. "
                f"Error: {error_message}"
            )
        if error_type == "ODATA_ERROR":
            return (
                f"An OData protocol error occurred — the EntitySet, NavigationProperty, or "
                f"OData query in the receiver adapter may be misconfigured. Fixable via iFlow update. "
                f"Error: {error_message}"
            )
        if error_type == "ROUTING_ERROR":
            return (
                f"The content-based router found no matching branch for the incoming message. "
                f"The routing condition expression in the iFlow needs to be corrected. "
                f"Error: {error_message}"
            )
        if error_type == "PROPERTY_ERROR":
            return (
                f"A required exchange property or message header was not found at runtime. "
                f"The iFlow is trying to read a property that was never set upstream — "
                f"add the missing Content Modifier or Groovy step to set it. "
                f"Error: {error_message}"
            )
        if error_type == "RESOURCE_ERROR":
            return (
                f"The CPI runtime ran out of memory or system resources during iFlow execution. "
                f"This requires infrastructure-level intervention (JVM heap, tenant sizing) — "
                f"not an iFlow XML change. Error: {error_message}"
            )
        if error_type == "SFTP_ERROR":
            msg = (error_message or "").lower()
            if any(k in msg for k in ["auth fail", "authentication failed", "publickey"]):
                detail = (
                    "SFTP authentication failed — the credential alias, SSH key, or password "
                    "configured in the receiver adapter is incorrect or expired."
                )
            elif any(k in msg for k in ["hostkey", "known hosts", "host key"]):
                detail = (
                    "SFTP host key verification failed — the server fingerprint changed or is "
                    "not trusted. Update the known hosts configuration."
                )
            elif "permission denied" in msg:
                detail = (
                    "SFTP permission denied — the SFTP user does not have write access to the "
                    "target directory."
                )
            elif "file already exists" in msg:
                detail = (
                    "SFTP file already exists on the server — enable overwrite in the adapter "
                    "or clean up the existing file."
                )
            elif any(k in msg for k in ["quota", "no space left"]):
                detail = "SFTP server disk quota exceeded — free up space on the target server."
            else:
                detail = (
                    "SFTP operation failed — the remote directory does not exist or the SFTP "
                    "user lacks permission."
                )
            return (
                f"{detail} This requires manual action on the SFTP server or credential store. "
                f"Error: {error_message}"
            )

        return (
            f"Unable to fully classify the CPI failure. Use logs and the failing iFlow step to "
            f"identify the required configuration change. Error: {error_message}"
        )

    # ── LangChain @tool wrappers ─────────────────────────────────────────────

    def create_tools(self) -> List:
        """Return LangChain @tool wrappers for the orchestrator agent tool list."""
        classifier = self

        @tool
        def classify_error_tool(error_message: str) -> Dict[str, Any]:
            """
            Rule-based SAP CPI error classifier.
            Returns error_type, confidence (0-1), tags, and is_iflow_fixable.
            Use this BEFORE calling the LLM RCA to get a fast baseline classification.
            """
            return classifier.classify_error(error_message)

        @tool
        def error_signature_tool(
            iflow_id: str,
            error_type: str,
            error_message: str = "",
        ) -> str:
            """
            Generate a stable 16-char hex signature for the (iflow_id, error_type,
            error_message) triple. Used to look up historical fix patterns.
            """
            return classifier.error_signature(iflow_id, error_type, error_message)

        @tool
        def fallback_root_cause_tool(error_type: str, error_message: str) -> str:
            """
            Return a human-readable root-cause description for the given error_type
            when the LLM RCA could not produce a specific diagnosis.
            """
            return classifier.fallback_root_cause(error_type, error_message)

        return [classify_error_tool, error_signature_tool, fallback_root_cause_tool]

    async def build_agent(self, mcp=None) -> None:
        """Build a LangChain classifier agent used as LLM fallback for low-confidence errors."""
        from langchain_core.tools import tool as _tool  # noqa: PLC0415
        from db.database import get_similar_patterns     # noqa: PLC0415

        classifier = self

        @_tool
        def lookup_error_pattern(error_message: str) -> str:
            """Classify error type using regex pattern rules. Returns error_type, confidence, tags."""
            result = classifier.classify_error(error_message)
            return str(result)

        @_tool
        def search_similar_past_errors(error_signature: str) -> str:
            """Search historical fix patterns by error signature. Returns list of past fixes."""
            patterns = get_similar_patterns(error_signature)
            return str(patterns)

        tools = [lookup_error_pattern, search_similar_past_errors]
        if mcp is not None:
            get_iflow_tool = mcp.get_mcp_tool("integration_suite", "get-iflow")
            if get_iflow_tool:
                tools.append(get_iflow_tool)

        system_prompt = (
            "You classify SAP CPI errors into one of these types: "
            "MAPPING_ERROR, DATA_VALIDATION, SSL_ERROR, AUTH_CONFIG_ERROR, AUTH_ERROR, "
            "CONNECTIVITY_ERROR, ADAPTER_CONFIG_ERROR, BACKEND_ERROR, SFTP_ERROR, "
            "DUPLICATE_ERROR, PAYLOAD_SIZE_ERROR, GROOVY_ERROR, SCRIPT_ERROR, "
            "IDOC_ERROR, SOAP_ERROR, ODATA_ERROR, ROUTING_ERROR, PROPERTY_ERROR, "
            "RESOURCE_ERROR, UNKNOWN_ERROR. "
            "Use lookup_error_pattern for a fast rule-based baseline, "
            "search_similar_past_errors for historical context, "
            "and get-iflow only if the error message alone is ambiguous. "
            'Return ONLY valid JSON: {"error_type": "...", "confidence": 0.0, "tags": []}.'
        )

        if mcp is not None:
            self._agent = await mcp.build_agent(tools=tools, system_prompt=system_prompt)
        else:
            self._agent = None
        logger.info(
            "[Classifier] LangChain agent ready (%d tools, mcp=%s).", len(tools), mcp is not None
        )
