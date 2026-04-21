"""
agents/classifier_agent.py
==========================
Rule-based error classifier for SAP CPI incidents.

ClassifierAgent exposes three pure-Python classifiers as both instance methods
and LangChain @tool callables so the orchestrator can invoke them directly OR
wire them into a LangChain agent's tool list.

Exports:
  ClassifierAgent
    .classify_error(error_message)   → {"error_type", "confidence", "tags"}
    .error_signature(...)            → md5 hex string used as DB dedup key
    .fallback_root_cause(...)        → human-readable fallback root-cause string
    .create_tools()                  → List[BaseTool] — LangChain-compatible wrappers
"""

import hashlib
import logging
import re
from typing import Any, Dict, List

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


class ClassifierAgent:
    """Pure rule-based error classifier. No LLM calls, no MCP connections."""

    def __init__(self):
        self._agent = None

    # ── classify_error ──────────────────────────────────────────────────────

    @staticmethod
    def classify_error(error_message: str) -> Dict[str, Any]:
        """
        Rule-based classifier — returns error_type, confidence, and tags.

        Order matters: more specific / higher-confidence rules are checked first
        to avoid false positives from broad keyword matches lower in the list.
        """
        msg = (error_message or "").lower()

        # ── SFTP — before AUTH_ERROR; SFTP auth failures are server-side ──
        if any(k in msg for k in [
            "sftp", "sshexception", "jsch", "failed to connect sftp",
            "cannot open channel", "publickey", "hostkey",
            "known hosts", "host key", "no such file", "no such directory",
            "file already exists", "quota exceeded", "no space left",
        ]):
            return {"error_type": "SFTP_ERROR", "confidence": 0.93, "tags": ["sftp", "filesystem"]}
        if "permission denied" in msg and any(k in msg for k in ["sftp", "ssh", "ftp"]):
            return {"error_type": "SFTP_ERROR", "confidence": 0.92, "tags": ["sftp", "filesystem"]}

        # ── SSL / TLS — before AUTH; cert renewal is infra, not an iFlow change ──
        if any(k in msg for k in [
            "ssl handshake", "tls handshake", "sslhandshakeexception",
            "pkix path", "certificate expired", "certificate has expired",
            "handshake_failure", "ssl alert", "sslexception",
        ]):
            return {"error_type": "SSL_ERROR", "confidence": 0.93, "tags": ["ssl", "cert"]}

        # ── Auth config inside iFlow — wrong credential alias / security material ref ──
        if any(k in msg for k in [
            "credential alias", "no security artifact", "security material not found",
            "credential reference", "security material", "no credential",
        ]):
            return {"error_type": "AUTH_CONFIG_ERROR", "confidence": 0.91, "tags": ["auth", "config"]}

        # ── Auth / credential — ambiguous; route to APPROVAL not AUTO_FIX ──
        if any(k in msg for k in [
            "unauthorized", "invalid credentials", "credential",
            "token expired", "access token", "oauth",
        ]):
            return {"error_type": "AUTH_ERROR", "confidence": 0.93, "tags": ["auth"]}
        if any(k in msg for k in ["401", "403"]) and not any(k in msg for k in ["sftp", "ssh"]):
            return {"error_type": "AUTH_ERROR", "confidence": 0.91, "tags": ["auth"]}

        # ── Mapping / schema ──
        if any(k in msg for k in [
            "mappingexception", "does not exist in target",
            "target structure", "mapping runtime",
            "xpath", "namespace", "xslt", "transformation failed",
        ]):
            return {"error_type": "MAPPING_ERROR", "confidence": 0.90, "tags": ["mapping", "schema"]}

        # ── Data validation ──
        if any(k in msg for k in [
            "mandatory", "required field", "null value",
            "validation failed", "data validation",
            "schema validation", "invalid payload",
        ]):
            return {"error_type": "DATA_VALIDATION", "confidence": 0.87, "tags": ["validation", "data"]}

        # ── Connectivity / network ──
        if any(k in msg for k in [
            "connection refused", "connect timed out", "read timed out",
            "unreachable", "socketexception", "network unreachable",
            "dns resolution", "no route to host",
        ]):
            return {"error_type": "CONNECTIVITY_ERROR", "confidence": 0.90, "tags": ["network", "timeout"]}

        # ── Rate limiting — transient ──
        if any(k in msg for k in ["429", "too many requests", "rate limit", "rate limited", "throttl"]):
            return {"error_type": "CONNECTIVITY_ERROR", "confidence": 0.82, "tags": ["network", "ratelimit"]}

        # ── 5xx backend errors ──
        if any(k in msg for k in [
            "503", "service unavailable", "502", "bad gateway",
            "504", "gateway timeout",
        ]):
            return {"error_type": "BACKEND_ERROR", "confidence": 0.87, "tags": ["backend", "5xx"]}
        if any(k in msg for k in ["500", "internal server error"]):
            return {"error_type": "BACKEND_ERROR", "confidence": 0.83, "tags": ["backend", "500"]}

        # ── 4xx adapter config errors — iFlow sent a bad request ──
        if any(k in msg for k in [
            "400", "bad request", "404", "not found",
            "422", "unprocessable", "405", "method not allowed",
            "406", "415", "unsupported media type",
        ]):
            return {"error_type": "ADAPTER_CONFIG_ERROR", "confidence": 0.83, "tags": ["adapter", "4xx"]}

        # ── Duplicate / idempotency ──
        if any(k in msg for k in [
            "duplicate entry", "record already exists", "duplicate key",
            "already processed", "idempotency", "unique constraint",
            "duplicate record", "violates unique",
        ]):
            return {"error_type": "DUPLICATE_ERROR", "confidence": 0.88, "tags": ["duplicate", "idempotency"]}

        # ── Payload too large ──
        if any(k in msg for k in [
            "413", "payload too large", "request entity too large",
            "message size", "size limit exceeded", "content too large",
        ]):
            return {"error_type": "PAYLOAD_SIZE_ERROR", "confidence": 0.88, "tags": ["payload", "size"]}

        # ── Weak signals — broad keywords last ──
        if any(k in msg for k in ["mapping", "field", "structure"]):
            return {"error_type": "MAPPING_ERROR", "confidence": 0.72, "tags": ["mapping", "schema"]}
        if any(k in msg for k in ["expired", "tls"]):
            return {"error_type": "AUTH_ERROR", "confidence": 0.70, "tags": ["auth"]}
        if "ssl" in msg:
            return {"error_type": "SSL_ERROR", "confidence": 0.70, "tags": ["ssl", "cert"]}

        return {"error_type": "UNKNOWN_ERROR", "confidence": 0.50, "tags": []}

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
        """
        Return LangChain @tool wrappers for classify_error, error_signature,
        and fallback_root_cause so the orchestrator agent can call them as tools.
        """
        classifier = self  # capture for closures

        @tool
        def classify_error_tool(error_message: str) -> Dict[str, Any]:
            """
            Rule-based SAP CPI error classifier.
            Returns error_type, confidence (0-1), and tags list.
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
            error_message) triple.  Used to look up historical fix patterns.
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
        """
        Build a LangChain classifier agent.

        @tool local functions:
          - lookup_error_pattern   — rule-based classifier
          - search_similar_past_errors — DB pattern lookup

        MCP tools (read-only):
          - get-iflow from integration_suite (if mcp supplied)
        """
        from langchain_core.tools import tool as _tool  # noqa: PLC0415
        from db.database import get_similar_patterns  # noqa: PLC0415

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
            "You classify SAP CPI errors into: MAPPING_ERROR, DATA_VALIDATION, "
            "SSL_ERROR, AUTH_CONFIG_ERROR, AUTH_ERROR, "
            "CONNECTIVITY_ERROR, ADAPTER_CONFIG_ERROR, BACKEND_ERROR, "
            "SFTP_ERROR, DUPLICATE_ERROR, PAYLOAD_SIZE_ERROR, UNKNOWN_ERROR. "
            "Use lookup_error_pattern for a fast rule-based baseline, search_similar_past_errors "
            "for historical context, and get-iflow only if the error message alone is ambiguous. "
            "Return structured JSON: {\"error_type\": \"...\", \"confidence\": 0.0, \"tags\": []}."
        )

        if mcp is not None:
            self._agent = await mcp.build_agent(tools=tools, system_prompt=system_prompt)
        else:
            # No MCP: store as None; callers fall back to classify_error() directly
            self._agent = None
        logger.info(
            "[Classifier] LangChain agent ready (%d tools, mcp=%s).", len(tools), mcp is not None
        )
