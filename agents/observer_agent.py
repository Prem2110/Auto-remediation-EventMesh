"""
agents/observer_agent.py
========================
SAPErrorFetcher  — OAuth2-authenticated OData client for SAP Integration Suite.
ObserverAgent    — Wraps the autonomous polling loop, error normalization, and
                   burst deduplication.  Calls OrchestratorAgent.process_detected_error()
                   for each new error found; the back-reference is injected via
                   set_orchestrator() to avoid a circular import.

Exports:
  SAPErrorFetcher
  ObserverAgent
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from core.constants import (
    FAILED_MESSAGE_FETCH_LIMIT,
    RUNTIME_ERROR_FETCH_LIMIT,
    RUNTIME_ERROR_DETAIL_FETCH_LIMIT,
    RUNTIME_ERROR_DETAIL_CONCURRENCY,
    MAX_UNIQUE_MESSAGE_ERRORS_PER_CYCLE,
    MAX_CONSECUTIVE_FAILURES,
    PENDING_APPROVAL_TIMEOUT_HRS,
    POLL_INTERVAL_SECONDS,
    BURST_DEDUP_WINDOW_SECONDS,
)
from db.database import (
    get_all_incidents,
    get_incident_by_message_guid,
    increment_incident_occurrence,
    update_incident,
    create_escalation_ticket,
)
from utils.utils import get_hana_timestamp

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# SAP credential env vars (read once at import)
# ─────────────────────────────────────────────
_SAP_TENANT     = os.getenv("SAP_HUB_TENANT_URL", "")
_SAP_TOKEN_URL  = os.getenv("SAP_HUB_TOKEN_URL", "")
_SAP_CLIENT_ID  = os.getenv("SAP_HUB_CLIENT_ID", "")
_SAP_CLIENT_SEC = os.getenv("SAP_HUB_CLIENT_SECRET", "")
_TICKET_ASSIGNEE = os.getenv("TICKET_DEFAULT_ASSIGNEE", "")

# AEM queue consumer
_AEM_REST_URL       = os.getenv("AEM_REST_URL", "")
_AEM_USERNAME       = os.getenv("AEM_USERNAME", "")
_AEM_PASSWORD       = os.getenv("AEM_PASSWORD", "")
_AEM_OBSERVER_QUEUE = os.getenv("AEM_OBSERVER_QUEUE", "sap.cpi.autofix.observer.out")


# ─────────────────────────────────────────────
# SAP ERROR FETCHER
# ─────────────────────────────────────────────

class SAPErrorFetcher:
    """
    OAuth2 client-credentials token + OData REST calls against SAP Integration Suite.
    Token is cached and refreshed 30 s before expiry.
    """

    def __init__(self):
        self._token: Optional[str] = None
        self._token_expiry: float  = 0.0

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.post(
                _SAP_TOKEN_URL,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     _SAP_CLIENT_ID,
                    "client_secret": _SAP_CLIENT_SEC,
                },
            )
            resp.raise_for_status()
            data               = resp.json()
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

    async def _get_json(
        self, path: str, params: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        token = await self._get_token()
        url   = f"{_SAP_TENANT.rstrip('/')}{path}"
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
        url   = f"{_SAP_TENANT.rstrip('/')}{path}"
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.text

    # ── Public API ───────────────────────────────────────────────────────────

    async def fetch_failed_messages(
        self, limit: int = FAILED_MESSAGE_FETCH_LIMIT
    ) -> List[Dict]:
        try:
            data = await self._get_json(
                "/api/v1/MessageProcessingLogs",
                params={
                    "$filter":  "Status eq 'FAILED'",
                    "$orderby": "LogEnd desc",
                    "$top":     str(max(1, limit)),
                    "$format":  "json",
                },
            )
            results = self._extract_results(data)
            logger.info("[SAP Poller] Fetched %d failed messages", len(results))
            return results
        except Exception as exc:
            logger.error("[SAP Poller] fetch_failed_messages error: %s", exc)
            return []

    async def fetch_failed_messages_count(self) -> int:
        try:
            token = await self._get_token()
            url   = f"{_SAP_TENANT.rstrip('/')}/api/v1/MessageProcessingLogs/$count"
            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                resp = await client.get(
                    url,
                    params={"$filter": "Status eq 'FAILED'"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                try:
                    return int(resp.text.strip())
                except ValueError:
                    pass
        except Exception as exc:
            logger.error("[SAP Poller] fetch_failed_messages_count error: %s", exc)
        msgs = await self.fetch_failed_messages(limit=1000)
        return len(msgs)

    async def fetch_error_details(self, message_guid: str) -> Dict:
        try:
            text = await self._get_text(
                f"/api/v1/MessageProcessingLogs('{message_guid}')/ErrorInformation/$value"
            )
            if text:
                return {"error_text": text}
        except Exception as exc:
            logger.error("[SAP Poller] fetch_error_details error: %s", exc)
        return {}

    async def fetch_message_metadata(self, message_guid: str) -> Dict:
        try:
            payload = await self._get_json(
                f"/api/v1/MessageProcessingLogs('{message_guid}')",
                params={"$format": "json"},
            )
            return payload.get("d", payload)
        except Exception as exc:
            logger.error("[SAP Poller] fetch_message_metadata error: %s", exc)
        return {}

    @staticmethod
    def normalize_runtime_artifact(
        raw: Dict[str, Any], error_text: str = ""
    ) -> Dict[str, Any]:
        artifact_id   = raw.get("Id") or raw.get("Name") or raw.get("IntegrationFlowId") or ""
        artifact_name = raw.get("Name") or raw.get("IntegrationFlowName") or artifact_id
        status        = raw.get("Status") or raw.get("DeployState") or raw.get("RuntimeStatus") or "UNKNOWN"
        return {
            "source_type":    "RUNTIME_ARTIFACT",
            "message_guid":   "",
            "artifact_id":    artifact_id,
            "iflow_id":       artifact_name,
            "sender":         raw.get("Sender") or "",
            "receiver":       raw.get("Receiver") or "",
            "status":         status,
            "log_start":      raw.get("CreatedAt") or raw.get("LastModified") or raw.get("DeployedOn") or "",
            "log_end":        raw.get("LastModified") or raw.get("DeployedOn") or "",
            "error_message":  (
                error_text
                or raw.get("ErrorInformation")
                or raw.get("Description")
                or raw.get("Message")
                or f"Runtime artifact is in status '{status}'."
            ),
            "correlation_id": raw.get("PackageId") or artifact_id,
            "package_id":     raw.get("PackageId") or raw.get("PackageName") or "",
            "version":        raw.get("Version") or "",
            "deployed_by":    raw.get("DeployedBy") or raw.get("ModifiedBy") or "",
            "runtime_node":   raw.get("Node") or raw.get("RuntimeNode") or "",
        }

    @staticmethod
    def normalize(raw: Dict, error_detail: Dict) -> Dict:
        return {
            "source_type":    "MESSAGE_PROCESSING_LOG",
            "message_guid":   raw.get("MessageGuid", ""),
            "iflow_id":       raw.get("IntegrationFlowName", ""),
            "artifact_id":    raw.get("ArtifactId", ""),
            "sender":         raw.get("Sender", ""),
            "receiver":       raw.get("Receiver", ""),
            "status":         raw.get("Status", "FAILED"),
            "log_start":      raw.get("LogStart", ""),
            "log_end":        raw.get("LogEnd", ""),
            "error_message":  error_detail.get("error_text", raw.get("CustomStatus", "")),
            "correlation_id": raw.get("CorrelationId", ""),
        }

    async def fetch_runtime_artifact_detail(self, artifact_id: str) -> Dict[str, Any]:
        try:
            payload = await self._get_json(
                f"/api/v1/IntegrationRuntimeArtifacts('{artifact_id}')",
                params={"$format": "json"},
            )
            return payload.get("d", payload)
        except Exception as exc:
            logger.error("[SAP Poller] fetch_runtime_artifact_detail error: %s", exc)
            return {}

    async def fetch_designtime_artifact_id(self, iflow_name: str) -> str:
        """Resolve a display name to the technical design-time artifact Id."""
        if not iflow_name:
            return ""
        try:
            safe_name = iflow_name.replace("'", "''")
            payload = await self._get_json(
                "/api/v1/IntegrationDesigntimeArtifacts",
                params={
                    "$filter": f"Name eq '{safe_name}'",
                    "$select": "Id,Name",
                    "$format": "json",
                },
            )
            results = self._extract_results(payload)
            if results:
                resolved = results[0].get("Id", "")
                if resolved:
                    logger.info(
                        "[SAP Poller] Resolved designtime artifact: '%s' → '%s'",
                        iflow_name, resolved,
                    )
                    return resolved
            logger.warning(
                "[SAP Poller] No designtime artifact found for name='%s'", iflow_name,
            )
            return ""
        except Exception as exc:
            logger.error(
                "[SAP Poller] fetch_designtime_artifact_id failed for '%s': %s",
                iflow_name, exc,
            )
            return ""

    async def fetch_runtime_artifact_error_detail(self, artifact_id: str) -> str:
        try:
            return (
                await self._get_text(
                    f"/api/v1/IntegrationRuntimeArtifacts('{artifact_id}')/ErrorInformation/$value"
                )
            ).strip()
        except Exception as exc:
            logger.debug("[SAP Poller] fetch_runtime_artifact_error_detail error: %s", exc)
            return ""

    async def fetch_runtime_artifact_errors(
        self, limit: int = RUNTIME_ERROR_FETCH_LIMIT
    ) -> List[Dict[str, Any]]:
        requested_limit = max(1, limit)
        for params in [
            {"$filter": "Status eq 'ERROR'",  "$top": str(requested_limit), "$format": "json"},
            {"$filter": "Status eq 'Error'",  "$top": str(requested_limit), "$format": "json"},
            {"$top": str(max(requested_limit * 3, requested_limit)),         "$format": "json"},
        ]:
            try:
                payload     = await self._get_json("/api/v1/IntegrationRuntimeArtifacts", params=params)
                raw_results = self._extract_results(payload)
                if raw_results:
                    break
            except Exception as exc:
                logger.debug("[SAP Poller] runtime artifact query failed for %s: %s", params, exc)
        else:
            raw_results = []

        filtered = [
            item for item in raw_results
            if (str(item.get("Status") or item.get("DeployState") or item.get("RuntimeStatus") or "").lower() == "error"
                or bool(str(item.get("ErrorInformation") or item.get("Description") or item.get("Message") or "").strip()))
        ][:requested_limit]

        items_with_text:    List[Dict[str, Any]] = []
        items_without_text: List[Dict[str, Any]] = []
        for item in filtered:
            inline = str(item.get("ErrorInformation") or item.get("Description") or item.get("Message") or "").strip()
            if inline:
                items_with_text.append(self.normalize_runtime_artifact(item, error_text=inline))
            else:
                items_without_text.append(item)

        budget    = max(0, min(RUNTIME_ERROR_DETAIL_FETCH_LIMIT, len(items_without_text)))
        semaphore = asyncio.Semaphore(max(1, RUNTIME_ERROR_DETAIL_CONCURRENCY))

        async def _resolve(item: Dict[str, Any]) -> Dict[str, Any]:
            aid = item.get("Id") or item.get("Name") or item.get("IntegrationFlowId") or ""
            async with semaphore:
                err = await self.fetch_runtime_artifact_error_detail(aid) if aid else ""
            return self.normalize_runtime_artifact(item, error_text=err)

        resolved = list(await asyncio.gather(*[_resolve(i) for i in items_without_text[:budget]]))
        leftover = [self.normalize_runtime_artifact(i) for i in items_without_text[budget:]]

        all_normalized = items_with_text + resolved + leftover
        logger.info("[SAP Poller] Fetched %d runtime artifact errors", len(all_normalized))
        return all_normalized

    async def fetch_cpi_error_inventory(
        self,
        message_limit: int = FAILED_MESSAGE_FETCH_LIMIT,
        artifact_limit: int = RUNTIME_ERROR_FETCH_LIMIT,
    ) -> Dict[str, Any]:
        failed_messages   = await self.fetch_failed_messages(limit=message_limit)
        runtime_artifacts = await self.fetch_runtime_artifact_errors(limit=artifact_limit)

        failed_message_items = []
        for raw in failed_messages:
            guid    = raw.get("MessageGuid", "")
            details = await self.fetch_error_details(guid) if guid else {}
            failed_message_items.append(self.normalize(raw, details))

        return {
            "summary": {
                "failed_message_count":        len(failed_message_items),
                "runtime_artifact_error_count": len(runtime_artifacts),
                "total_errors":                len(failed_message_items) + len(runtime_artifacts),
            },
            "failed_messages":    failed_message_items,
            "runtime_artifacts":  runtime_artifacts,
        }


# ─────────────────────────────────────────────
# OBSERVER AGENT
# ─────────────────────────────────────────────

class ObserverAgent:
    """
    Autonomous polling agent.

    - Polls SAP CPI for failed messages and runtime artifact errors.
    - Deduplicates burst errors within the BURST_DEDUP_WINDOW_SECONDS window.
    - Delegates each new unique error to OrchestratorAgent.process_detected_error().

    The orchestrator reference is injected after construction to avoid circular imports:
        observer = ObserverAgent(mcp)
        observer.set_orchestrator(orchestrator)
        observer.start()
    """

    def __init__(self, mcp):
        self._mcp          = mcp
        self._orchestrator = None
        self.error_fetcher = SAPErrorFetcher()
        self._agent        = None

    def set_orchestrator(self, orchestrator) -> None:
        """Inject the orchestrator reference (called from main.py after both are created)."""
        self._orchestrator = orchestrator

    async def build_agent(self) -> None:
        """Build a LangChain agent with local @tool functions for AEM queue monitoring."""
        from langchain_core.tools import tool as _tool  # noqa: PLC0415
        from db.database import update_incident as _upd  # noqa: PLC0415

        _self = self  # captured by closures

        @_tool
        async def fetch_failed_messages() -> str:
            """
            Report current AEM queue status.
            Queue consumption is managed exclusively by the OrchestratorAgent polling loop.
            Use GET /aem/status for live queue depth.
            """
            return (
                f"Queue consumption is managed by the OrchestratorAgent autonomous loop. "
                f"Queue: '{_AEM_OBSERVER_QUEUE}'. "
                "Check GET /aem/status for current depth and pipeline stage counts."
            )

        @_tool
        def mark_incident_in_progress(incident_id: str) -> str:
            """Mark a detected incident as actively being processed."""
            _upd(incident_id, {"status": "IN_PROGRESS"})
            return f"Incident {incident_id} marked as IN_PROGRESS."

        system_prompt = (
            "You are an SAP CPI monitoring agent. "
            f"Use fetch_failed_messages to consume new failures from the Event mesh queue "
            f"'{_AEM_OBSERVER_QUEUE}'. If the queue is empty the tool returns a WARNING — "
            "report it and do not proceed further. "
            "Mark each consumed incident with mark_incident_in_progress. "
            "Do NOT attempt to fix errors — only detect and record them."
        )
        self._agent = await self._mcp.build_agent(
            tools=[fetch_failed_messages, mark_incident_in_progress],
            system_prompt=system_prompt,
        )
        logger.info("[Observer] LangChain agent ready (AEM queue source: '%s').", _AEM_OBSERVER_QUEUE)

    # ── deduplication ────────────────────────

    @staticmethod
    def _raw_message_group_key(raw: Dict[str, Any]) -> str:
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
        deduped:   List[Dict[str, Any]] = []
        seen_keys: set[str]             = set()
        for raw in raw_errors:
            key = self._raw_message_group_key(raw)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(raw)
            if len(deduped) >= max(1, max_unique):
                break
        return deduped

    # ── approval timeout check ────────────────

    async def _check_pending_approval_timeouts(self) -> None:
        """Escalate AWAITING_APPROVAL incidents that have exceeded the timeout threshold."""
        try:
            from datetime import datetime, timedelta, UTC  # noqa: PLC0415
            cutoff = (datetime.now(UTC) - timedelta(hours=PENDING_APPROVAL_TIMEOUT_HRS)).isoformat()
            stale = [
                inc for inc in get_all_incidents(status="AWAITING_APPROVAL", limit=200)
                if (inc.get("pending_since") or inc.get("created_at") or "") < cutoff
                and not inc.get("auto_escalated")
            ]
            for inc in stale:
                logger.warning(
                    "[Autonomous] AWAITING_APPROVAL timeout: incident %s pending since %s — escalating.",
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
                ticket_id = await self._create_ticket(inc, rca_ctx)
                update_incident(inc["incident_id"], {
                    "status":    "TICKET_CREATED",
                    "ticket_id": ticket_id,
                })
        except Exception as exc:
            logger.error("[Autonomous] _check_pending_approval_timeouts error: %s", exc)

    async def _create_ticket(self, incident: Dict, rca: Dict) -> Optional[str]:
        """Direct DB escalation ticket creation (no orchestrator dependency)."""
        try:
            occurrence = incident.get("occurrence_count", 1)
            confidence = rca.get("confidence", 0.0)
            priority   = (
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
                "assigned_to": _TICKET_ASSIGNEE or None,
            }
            ticket_id = create_escalation_ticket(ticket_data)
            logger.info(
                "[EscalationTicket] Ticket %s created for incident %s",
                ticket_id, incident.get("incident_id"),
            )
            return ticket_id
        except Exception as exc:
            logger.error("[EscalationTicket] Failed to create ticket: %s", exc)
            return None

    # ── autonomous loop ───────────────────────
    # Queue polling and message routing have moved to OrchestratorAgent.
    # ObserverAgent retains: SAPErrorFetcher (monitoring endpoints),
    # _check_pending_approval_timeouts (called by orchestrator loop),
    # and the LangChain chatbot agent.
