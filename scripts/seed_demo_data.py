"""
seed_demo_data.py
=================
Inserts realistic demo data into EM_AUTONOMOUS_INCIDENTS and EM_ESCALATION_TICKETS
for client presentation. Shows a healthy spread of all pipeline statuses.

Usage:
    python seed_demo_data.py            # insert demo data
    python seed_demo_data.py --clear    # wipe demo data first, then insert
    python seed_demo_data.py --wipe     # wipe demo data only

Requires the same .env as the main app (HANA_HOST, HANA_USER, HANA_PASSWORD, etc.)
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Inline get_connection (mirrors db/database.py) ───────────────────────────
def get_connection():
    import hdbcli.dbapi as dbapi
    host     = os.getenv("HANA_HOST")
    user     = os.getenv("HANA_USER")
    password = os.getenv("HANA_PASSWORD")
    port     = int(os.getenv("HANA_PORT", "443"))
    schema   = os.getenv("HANA_SCHEMA", "")
    conn     = dbapi.connect(address=host, port=port, user=user, password=password,
                              encrypt=True, sslValidateCertificate=False)
    if schema:
        conn.cursor().execute(f'SET SCHEMA "{schema}"')
    return conn

_INCIDENTS_TABLE = os.getenv("HANA_TABLE_EM_INCIDENTS",         "EM_AUTONOMOUS_INCIDENTS")
_TICKETS_TABLE   = os.getenv("HANA_TABLE_EM_ESCALATION_TICKETS","EM_ESCALATION_TICKETS")

DEMO_TAG = "DEMO_SEED"   # tag used to identify and wipe seed rows

# ── Timestamp helpers ─────────────────────────────────────────────────────────

now = datetime.now(timezone.utc)

def ts(val):
    """Accept hours_ago (float/int) or an absolute ISO timestamp string."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return (now - timedelta(hours=float(val))).isoformat()

# ── iFlows — realistic SAP CPI integration names ─────────────────────────────
IFLOWS = [
    "EH8-BPP-Material-UPSERT",
    "EH8-BPP-SalesOrder-CREATE",
    "EH8-BPP-Vendor-SYNC",
    "EH8-BPP-Invoice-POST",
    "EH8-BPP-PurchaseOrder-REPLICATE",
    "EH8-BPP-Customer-UPSERT",
    "EH8-BPP-StockTransfer-PROCESS",
    "EH8-BPP-DeliveryNote-SEND",
    "EH8-BPP-PaymentAdvice-NOTIFY",
    "EH8-BPP-GoodsReceipt-CONFIRM",
    "Enricher",
    "EH8-BPP-ApprovalNotifier-EMAIL",
    "EH8-BPP-InvoiceValidator-SOAP",
    "EH8-BPP-AssetSync-RFC",
    "EH8-BPP-ChangeOrder-ODATA",
]

# ── Seed incident definitions ─────────────────────────────────────────────────
# Each entry is a dict. Timestamps can be hours_ago (float) or absolute ISO string.
# Fields populated here map directly to UI tabs:
#   created    → History "Detected" event
#   last_seen  → History "RCA Analysis" and "Recurrence" timestamps
#   log_start  → Properties "Processing Start"
#   log_end    → Properties "Processing End" / Artifact "Deployed On"
#   message_guid → Properties "Message Id" and "Mpl Id"
#   integration_flow_name / artifact_id → Artifact tab Name / Artifact ID

INCIDENTS = [
    # ── 0: FIX_VERIFIED ──────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-Material-UPSERT",
        "error_type": "MAPPING_ERROR",
        "status":     "FIX_VERIFIED",
        "occurrence_count": 3,
        "rca_confidence": 0.92,
        "error_message": "XPathException: Variable 'materialCode' not declared in namespace",
        "root_cause":  "XPath expression references undeclared namespace prefix in MM_MaterialTransform",
        "proposed_fix":"Declare namespace prefix in message mapping step MM_MaterialTransform",
        "fix_summary": "Added namespace declaration to XPath expression. iFlow redeployed and verified.",
        "created": 48.0, "resolved": 46.0,
        "sender": "S4HANA", "receiver": "MDG",
        "message_guid": "AFkL2XmBnGR7sYpQ_HJW3-Kd9v1",
        "correlation_id": "AFkL2XmBnGR7sYpQ_HJW3-Kd9v1",
        "log_start": 48.08, "log_end": 48.0, "last_seen": 47.2,
    },
    # ── 1: FIX_VERIFIED ──────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-SalesOrder-CREATE",
        "error_type": "MAPPING_ERROR",
        "status":     "FIX_VERIFIED",
        "occurrence_count": 2,
        "rca_confidence": 0.88,
        "error_message": "NullPointerException in SalesOrder mapping step",
        "root_cause":  "Optional field 'customerPO' accessed without null check in mapping",
        "proposed_fix":"Add null guard for customerPO field in mapping function",
        "fix_summary": "Null guard added to mapping. Message processed successfully after redeploy.",
        "created": 36.0, "resolved": 34.0,
        "sender": "SFDC", "receiver": "S4HANA",
        "message_guid": "BHmN4ZxCpKT8wVrS_LKX5-Pe2b3",
        "correlation_id": "BHmN4ZxCpKT8wVrS_LKX5-Pe2b3",
        "log_start": 36.05, "log_end": 36.0, "last_seen": 35.1,
    },
    # ── 2: FIX_VERIFIED ──────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-Vendor-SYNC",
        "error_type": "AUTH_CONFIG_ERROR",
        "status":     "FIX_VERIFIED",
        "occurrence_count": 1,
        "rca_confidence": 0.95,
        "error_message": "HTTP 401 Unauthorized — credential alias 'Vendor_API_Cred' not found",
        "root_cause":  "Credential alias name mismatch — iFlow references 'Vendor_API_Cred' but SecureStore has 'VendorAPI_Cred'",
        "proposed_fix":"Update receiver adapter credential alias to 'VendorAPI_Cred'",
        "fix_summary": "Credential alias corrected in receiver adapter. iFlow redeployed and authenticated.",
        "created": 24.0, "resolved": 23.0,
        "sender": "MDG", "receiver": "Vendor Portal",
        "message_guid": "CGrO5AyDqLU9xWsT_MNY6-Qf3c4",
        "correlation_id": "CGrO5AyDqLU9xWsT_MNY6-Qf3c4",
        "log_start": 24.05, "log_end": 24.0, "last_seen": 24.0,
    },
    # ── 3: FIX_VERIFIED ──────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-Customer-UPSERT",
        "error_type": "ADAPTER_CONFIG_ERROR",
        "status":     "FIX_VERIFIED",
        "occurrence_count": 2,
        "rca_confidence": 0.91,
        "error_message": "HTTP 404 Not Found — /api/v1/customers endpoint does not exist",
        "root_cause":  "Receiver HTTP adapter URL path is /api/v1/customers but API expects /api/v2/customers",
        "proposed_fix":"Update receiver adapter URL path from /api/v1 to /api/v2",
        "fix_summary": "Endpoint URL updated to /api/v2/customers. Response 200 OK confirmed.",
        "created": 20.0, "resolved": 19.0,
        "sender": "CRM", "receiver": "Customer API",
        "message_guid": "DHsP6BzErMV0yXtU_NOZ7-Rg4d5",
        "correlation_id": "DHsP6BzErMV0yXtU_NOZ7-Rg4d5",
        "log_start": 20.05, "log_end": 20.0, "last_seen": 19.5,
    },
    # ── 4: FIX_VERIFIED ──────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-StockTransfer-PROCESS",
        "error_type": "MAPPING_ERROR",
        "status":     "FIX_VERIFIED",
        "occurrence_count": 4,
        "rca_confidence": 0.87,
        "error_message": "XSLT transformation failed: element 'StockType' not found in source",
        "root_cause":  "Source schema change — StockType field renamed to StorageType in upstream S4HANA patch",
        "proposed_fix":"Update XSLT mapping to use StorageType instead of StockType",
        "fix_summary": "XSLT updated. All 4 queued messages replayed and processed successfully.",
        "created": 15.0, "resolved": 14.0,
        "sender": "S4HANA", "receiver": "WMS",
        "message_guid": "EItQ7CaFsNW1zYuV_OPa8-Sh5e6",
        "correlation_id": "EItQ7CaFsNW1zYuV_OPa8-Sh5e6",
        "log_start": 15.08, "log_end": 15.0, "last_seen": 14.8,
    },
    # ── 5: FIX_DEPLOYED ──────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-Invoice-POST",
        "error_type": "ADAPTER_CONFIG_ERROR",
        "status":     "FIX_DEPLOYED",
        "occurrence_count": 1,
        "rca_confidence": 0.83,
        "error_message": "HTTP 400 Bad Request — Content-Type header missing in POST",
        "root_cause":  "Receiver HTTP adapter missing Content-Type: application/json header",
        "proposed_fix":"Add Content-Type header to receiver HTTP adapter configuration",
        "fix_summary": None,
        "created": 4.0, "resolved": None,
        "sender": "AP", "receiver": "Finance API",
        "message_guid": "FJuR8DbGtOX2aZvW_PQb9-Ti6f7",
        "correlation_id": "FJuR8DbGtOX2aZvW_PQb9-Ti6f7",
        "log_start": 4.05, "log_end": 4.0, "last_seen": 4.0,
    },
    # ── 6: FIX_IN_PROGRESS ───────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-PurchaseOrder-REPLICATE",
        "error_type": "MAPPING_ERROR",
        "status":     "FIX_IN_PROGRESS",
        "occurrence_count": 2,
        "rca_confidence": 0.79,
        "error_message": "java.lang.ClassCastException in PO mapping — expected String got Integer",
        "root_cause":  "Type mismatch in PO line item quantity field mapping",
        "proposed_fix":"Cast quantity field to String before mapping",
        "fix_summary": None,
        "created": 2.0, "resolved": None,
        "sender": "Ariba", "receiver": "S4HANA",
        "message_guid": "GKvS9EcHuPY3baWX_QRc0-Uj7g8",
        "correlation_id": "GKvS9EcHuPY3baWX_QRc0-Uj7g8",
        "log_start": 2.05, "log_end": 2.0, "last_seen": 1.8,
    },
    # ── 7: RCA_COMPLETE ───────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-DeliveryNote-SEND",
        "error_type": "AUTH_CONFIG_ERROR",
        "status":     "RCA_COMPLETE",
        "occurrence_count": 1,
        "rca_confidence": 0.86,
        "error_message": "HTTP 401 — Security material 'DeliveryAPI_OAuth' expired",
        "root_cause":  "OAuth token in security material DeliveryAPI_OAuth has expired",
        "proposed_fix":"Refresh OAuth credentials in Security Material store",
        "fix_summary": None,
        "created": 3.0, "resolved": None,
        "sender": "EWM", "receiver": "Delivery API",
        "message_guid": "HLwT0FdIvQZ4cbXY_RSd1-Vk8h9",
        "correlation_id": "HLwT0FdIvQZ4cbXY_RSd1-Vk8h9",
        "log_start": 3.05, "log_end": 3.0, "last_seen": 3.0,
    },
    # ── 8: AWAITING_APPROVAL ─────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-PaymentAdvice-NOTIFY",
        "error_type": "AUTH_ERROR",
        "status":     "AWAITING_APPROVAL",
        "occurrence_count": 1,
        "rca_confidence": 0.61,
        "error_message": "Authentication failed — unclear if iFlow config or credentials are at fault",
        "root_cause":  "Could not determine root cause automatically — may be credential rotation or iFlow misconfiguration",
        "proposed_fix":"Human review required to identify if credential store or iFlow adapter needs update",
        "fix_summary": None,
        "created": 6.0, "resolved": None,
        "sender": "Treasury", "receiver": "Payment Gateway",
        "message_guid": "IMxU1GeJwRa5dcYZ_STe2-Wl9i0",
        "correlation_id": "IMxU1GeJwRa5dcYZ_STe2-Wl9i0",
        "log_start": 6.05, "log_end": 6.0, "last_seen": 6.0,
    },
    # ── 9: TICKET_CREATED (CLASSIFIER_ESCALATION) ─────────────────────────────
    {
        "iflow_id":   "EH8-BPP-GoodsReceipt-CONFIRM",
        "error_type": "BACKEND_ERROR",
        "status":     "TICKET_CREATED",
        "occurrence_count": 5,
        "rca_confidence": 0.94,
        "error_message": "HTTP 503 Service Unavailable from GoodsReceipt backend",
        "root_cause":  "Backend GR service is down — infrastructure issue outside iFlow scope",
        "proposed_fix":"Backend team must restore GR service. iFlow is working correctly.",
        "fix_summary": None,
        "created": 12.0, "resolved": None,
        "sender": "WMS", "receiver": "GR Backend",
        "message_guid": "JNyV2HfKxSb6edZa_TUf3-Xm0j1",
        "correlation_id": "JNyV2HfKxSb6edZa_TUf3-Xm0j1",
        "log_start": 12.08, "log_end": 12.0, "last_seen": 10.5,
    },
    # ── 10: TICKET_CREATED (CLASSIFIER_ESCALATION) ────────────────────────────
    {
        "iflow_id":   "EH8-BPP-SalesOrder-CREATE",
        "error_type": "SSL_ERROR",
        "status":     "TICKET_CREATED",
        "occurrence_count": 2,
        "rca_confidence": 0.97,
        "error_message": "PKIX path building failed: unable to find valid certification path",
        "root_cause":  "SSL certificate on SalesOrder receiver endpoint has expired",
        "proposed_fix":"Certificate renewal required on receiver endpoint — cannot be fixed by agent",
        "fix_summary": None,
        "created": 18.0, "resolved": None,
        "sender": "SFDC", "receiver": "S4HANA",
        "message_guid": "KOzW3IgLyTc7feAb_UVg4-Yn1k2",
        "correlation_id": "KOzW3IgLyTc7feAb_UVg4-Yn1k2",
        "log_start": 18.05, "log_end": 18.0, "last_seen": 16.0,
    },
    # ── 11: TICKET_CREATED (FIX_FAILED_ESCALATION) ────────────────────────────
    {
        "iflow_id":   "EH8-BPP-Material-UPSERT",
        "error_type": "MAPPING_ERROR",
        "status":     "TICKET_CREATED",
        "occurrence_count": 6,
        "rca_confidence": 0.55,
        "error_message": "Recursive mapping loop detected in MM_MaterialEnrich — stack overflow",
        "root_cause":  "Complex recursive structure in mapping could not be resolved automatically after 3 attempts",
        "proposed_fix":"Manual mapping redesign required — recursion depth exceeds auto-fix capability",
        "fix_summary": None,
        "created": 30.0, "resolved": None,
        "sender": "S4HANA", "receiver": "MDG",
        "message_guid": "LPaX4JhMzUd8gfBc_VWh5-Zo2l3",
        "correlation_id": "LPaX4JhMzUd8gfBc_VWh5-Zo2l3",
        "log_start": 30.08, "log_end": 30.0, "last_seen": 25.0,
    },
    # ── 12: FIX_FAILED ────────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-Invoice-POST",
        "error_type": "ADAPTER_CONFIG_ERROR",
        "status":     "FIX_FAILED",
        "occurrence_count": 3,
        "rca_confidence": 0.72,
        "error_message": "HTTP 400 — malformed JSON body in Invoice POST",
        "root_cause":  "Request body structure changed in Invoice API v3 — requires schema update",
        "proposed_fix":"Update request body mapping to match Invoice API v3 schema",
        "fix_summary": None,
        "created": 8.0, "resolved": None,
        "sender": "AP", "receiver": "Invoice API",
        "message_guid": "MQbY5KiNaVe9hgCd_WXi6-Ap3m4",
        "correlation_id": "MQbY5KiNaVe9hgCd_WXi6-Ap3m4",
        "log_start": 8.08, "log_end": 8.0, "last_seen": 6.5,
    },
    # ── 13: HUMAN_RESOLVED ────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-GoodsReceipt-CONFIRM",
        "error_type": "BACKEND_ERROR",
        "status":     "HUMAN_RESOLVED",
        "occurrence_count": 3,
        "rca_confidence": 0.94,
        "error_message": "HTTP 500 from GoodsReceipt confirmation service",
        "root_cause":  "Database connection pool exhausted on GR backend",
        "proposed_fix":"Backend team to increase DB connection pool size",
        "fix_summary": "DBA increased connection pool from 50 to 200. Service restored. All queued messages replayed.",
        "created": 72.0, "resolved": 68.0,
        "sender": "WMS", "receiver": "GR Backend",
        "message_guid": "NRcZ6LjObWf0ihDe_XYj7-Bq4n5",
        "correlation_id": "NRcZ6LjObWf0ihDe_XYj7-Bq4n5",
        "log_start": 72.08, "log_end": 72.0, "last_seen": 70.0,
    },
    # ── 14: DETECTED ──────────────────────────────────────────────────────────
    {
        "iflow_id":   "EH8-BPP-PurchaseOrder-REPLICATE",
        "error_type": "UNKNOWN_ERROR",
        "status":     "DETECTED",
        "occurrence_count": 1,
        "rca_confidence": 0.0,
        "error_message": "com.sap.it.rt.adapter.http.api.exception.HttpResponseException: 422 Unprocessable Entity",
        "root_cause":  None,
        "proposed_fix":None,
        "fix_summary": None,
        "created": 0.5, "resolved": None,
        "sender": "Ariba", "receiver": "S4HANA",
        "message_guid": "OSdA7MkPcXg1jiEf_YZk8-Cr5o6",
        "correlation_id": "OSdA7MkPcXg1jiEf_YZk8-Cr5o6",
        "log_start": 0.55, "log_end": 0.5, "last_seen": 0.5,
    },

    # ══════════════════════════════════════════════════════════════════════════
    # NEW iFlows — with full artifact, properties and history detail
    # ══════════════════════════════════════════════════════════════════════════

    # ── 15: RCA_COMPLETE — Enricher (from demo screenshot) ───────────────────
    # Error: smtp misconfiguration in mail receiver adapter
    # Artifact: Name=Enricher, ID=Enricher, Deployed On=May 07 2026 07:38
    # Properties: MessageGuid=AGn8PwkKoUEG8PJZ_V5XN-T1j_g4, ProcessingEnd=07:38
    # History: Detected 07:28, RCA 11:24, Recurrence 21 times last seen 11:24
    {
        "iflow_id":             "Enricher",
        "integration_flow_name":"Enricher",
        "artifact_id":          "Enricher",
        "error_type":           "BACKEND_ERROR",
        "status":               "RCA_COMPLETE",
        "occurrence_count":     21,
        "rca_confidence":       0.91,
        "error_message": (
            "com.sap.it.rt.adapter.http.api.exception.HttpResponseException: "
            "An internal server error occured: smtp."
        ),
        "root_cause": (
            "Mail receiver adapter on message flow MessageFlow_642 "
            "(End 1 -> Receiver4) is misconfigured: the server property is set "
            "to 'smtp:587' without a hostname"
        ),
        "proposed_fix": (
            "Update Mail receiver adapter Server property to use only the hostname "
            "(e.g. 'smtp.sierradigitalinc.com'). Set the Port field separately to 587. "
            "Re-deploy the iFlow after saving the change."
        ),
        "fix_summary": None,
        "created":  "2026-05-07T07:28:31+00:00",
        "resolved": None,
        "sender":   "S4HANA",
        "receiver": "Mail Server",
        "message_guid":   "AGn8PwkKoUEG8PJZ_V5XN-T1j_g4",
        "correlation_id": "AGn8PwkKoUEG8PJZ_V5XN-T1j_g4",
        "log_start": "2026-05-07T07:28:31+00:00",
        "log_end":   "2026-05-07T07:38:00+00:00",
        "last_seen": "2026-05-07T11:24:31+00:00",
    },

    # ── 16: FIX_IN_PROGRESS — ApprovalNotifier email adapter ─────────────────
    # Same class of error as Enricher: host:port combined in Server field
    {
        "iflow_id":             "EH8-BPP-ApprovalNotifier-EMAIL",
        "integration_flow_name":"EH8-BPP-ApprovalNotifier-EMAIL",
        "artifact_id":          "EH8-BPP-ApprovalNotifier-EMAIL",
        "error_type":           "ADAPTER_CONFIG_ERROR",
        "status":               "FIX_IN_PROGRESS",
        "occurrence_count":     7,
        "rca_confidence":       0.89,
        "error_message": (
            "com.sap.it.rt.adapter.mail.api.MailAdapterException: "
            "Could not connect to mail server smtp:25 — Connection refused"
        ),
        "root_cause": (
            "Mail adapter Server property contains 'smtp:25' (hostname:port combined) "
            "instead of separate host and port fields. SAP CPI mail adapter does not "
            "accept host:port format in the Server field."
        ),
        "proposed_fix": (
            "Set Server field to 'smtp.sierradigitalinc.com' and Port field to '25' "
            "separately in the Mail receiver adapter. Remove ':25' suffix from the "
            "Server property and redeploy."
        ),
        "fix_summary": None,
        "created": 5.5, "resolved": None,
        "sender": "S4HANA", "receiver": "Mail Server",
        "message_guid":   "PTdB8NlQdYh2kjFg_ZAl9-Ds6p7",
        "correlation_id": "PTdB8NlQdYh2kjFg_ZAl9-Ds6p7",
        "log_start": 5.55, "log_end": 5.5, "last_seen": 4.0,
    },

    # ── 17: FIX_VERIFIED — InvoiceValidator SOAP namespace mismatch ───────────
    {
        "iflow_id":             "EH8-BPP-InvoiceValidator-SOAP",
        "integration_flow_name":"EH8-BPP-InvoiceValidator-SOAP",
        "artifact_id":          "EH8-BPP-InvoiceValidator-SOAP",
        "error_type":           "MAPPING_ERROR",
        "status":               "FIX_VERIFIED",
        "occurrence_count":     5,
        "rca_confidence":       0.93,
        "error_message": (
            "javax.xml.soap.SOAPException: Namespace URI "
            "'urn:sap.com:proxy:MM:/1SAI/TAS3E9D15B32F781B:750:2023' "
            "does not match expected "
            "'urn:sap.com:proxy:MM:/1SAI/TAS3E9D15B32F781B:750:2024'"
        ),
        "root_cause": (
            "SOAP namespace version mismatch in InvoiceValidator — iFlow WSDL still "
            "references 2023 schema version after SAP S/4HANA 2024 upgrade patched "
            "the namespace URI to the 2024 version"
        ),
        "proposed_fix": (
            "Update the SOAP receiver adapter WSDL endpoint URL to reference the 2024 "
            "namespace version. Regenerate the SOAP artefact from the updated WSDL "
            "and redeploy."
        ),
        "fix_summary": (
            "SOAP adapter WSDL updated to 2024 namespace. All 5 pending invoices "
            "processed successfully after redeploy."
        ),
        "created": 22.0, "resolved": 21.0,
        "sender": "AP", "receiver": "S4HANA SOAP",
        "message_guid":   "QUeC9OmReZi3lkGh_ABm0-Et7q8",
        "correlation_id": "QUeC9OmReZi3lkGh_ABm0-Et7q8",
        "log_start": 22.08, "log_end": 22.0, "last_seen": 21.5,
    },

    # ── 18: AWAITING_APPROVAL — AssetSync RFC destination unreachable ─────────
    {
        "iflow_id":             "EH8-BPP-AssetSync-RFC",
        "integration_flow_name":"EH8-BPP-AssetSync-RFC",
        "artifact_id":          "EH8-BPP-AssetSync-RFC",
        "error_type":           "AUTH_CONFIG_ERROR",
        "status":               "AWAITING_APPROVAL",
        "occurrence_count":     3,
        "rca_confidence":       0.77,
        "error_message": (
            "com.sap.conn.jco.JCoException: (102) RFC_ERROR_COMMUNICATION: "
            "Connect to SAP gateway failed — LOCATION CPIC (TCP/IP) on local host "
            "— ERROR partner not reached (host ASSETMGMT-PRD-001, system 00)"
        ),
        "root_cause": (
            "RFC destination 'ASSETMGMT_PRD' pointing to host ASSETMGMT-PRD-001 is "
            "unreachable — possible hostname resolution failure or firewall rule change "
            "blocking port 3300. Confidence reduced because RFC connectivity issues "
            "can originate from network infrastructure outside iFlow scope."
        ),
        "proposed_fix": (
            "1. Verify RFC destination 'ASSETMGMT_PRD' in SAP BTP Connectivity Service. "
            "2. Test connection from CPI tenant to ASSETMGMT-PRD-001:3300. "
            "3. If firewall is blocking, open port 3300 for CPI outbound traffic. "
            "4. If hostname changed, update RFC destination host property. "
            "Human approval required before applying network-level changes."
        ),
        "fix_summary": None,
        "created": 9.0, "resolved": None,
        "sender": "FixedAssets", "receiver": "Asset Management",
        "message_guid":   "RVfD0PnSfAj4mlHi_BCn1-Fu8r9",
        "correlation_id": "RVfD0PnSfAj4mlHi_BCn1-Fu8r9",
        "log_start": 9.08, "log_end": 9.0, "last_seen": 7.5,
    },

    # ── 19: TICKET_CREATED — ChangeOrder OData 503 (infrastructure) ───────────
    {
        "iflow_id":             "EH8-BPP-ChangeOrder-ODATA",
        "integration_flow_name":"EH8-BPP-ChangeOrder-ODATA",
        "artifact_id":          "EH8-BPP-ChangeOrder-ODATA",
        "error_type":           "BACKEND_ERROR",
        "status":               "TICKET_CREATED",
        "occurrence_count":     9,
        "rca_confidence":       0.96,
        "error_message": (
            "com.sap.it.rt.adapter.http.api.exception.HttpResponseException: "
            "HTTP 503 Service Unavailable — OData service "
            "'/sap/opu/odata/sap/API_CHANGE_ORDER_SRV' returned 503"
        ),
        "root_cause": (
            "Target S/4HANA OData service API_CHANGE_ORDER_SRV is returning 503 — "
            "the service is temporarily unavailable on the target system. This is an "
            "infrastructure issue on the S/4HANA backend and cannot be resolved by "
            "modifying the iFlow configuration."
        ),
        "proposed_fix": (
            "Escalate to S/4HANA Basis team to investigate service "
            "API_CHANGE_ORDER_SRV availability. Check SM50/SM66 for blocked work "
            "processes. iFlow configuration is correct and requires no changes."
        ),
        "fix_summary": None,
        "created": 11.0, "resolved": None,
        "sender": "Procurement", "receiver": "S4HANA OData",
        "message_guid":   "SWgE1QoTgBk5nmIj_CDo2-Gv9s0",
        "correlation_id": "SWgE1QoTgBk5nmIj_CDo2-Gv9s0",
        "log_start": 11.08, "log_end": 11.0, "last_seen": 8.5,
    },
]

# ── Escalation tickets (for TICKET_CREATED incidents) ─────────────────────────
# (incident_index, ticket_source, priority, status, itsm_ticket_id, resolved_hrs)
TICKETS = [
    (9,  "CLASSIFIER_ESCALATION", "HIGH",     "OPEN",     "ITSM-TKT-1041", None),
    (10, "CLASSIFIER_ESCALATION", "CRITICAL", "OPEN",     "ITSM-TKT-1042", None),
    (11, "FIX_FAILED_ESCALATION", "HIGH",     "OPEN",     "ITSM-TKT-1043", None),
    (13, "CLASSIFIER_ESCALATION", "HIGH",     "RESOLVED", "ITSM-TKT-1038", 68),
    (19, "CLASSIFIER_ESCALATION", "HIGH",     "OPEN",     "ITSM-TKT-1044", None),
]


# ── Insert logic ──────────────────────────────────────────────────────────────

def insert_incidents(cur) -> list:
    """Insert all demo incidents. Returns list of generated incident_ids."""
    incident_ids = []
    for inc in INCIDENTS:
        iid      = str(uuid.uuid4())
        incident_ids.append(iid)

        iflow    = inc["iflow_id"]
        status   = inc["status"]
        msg_guid = inc.get("message_guid") or iid
        created  = ts(inc["created"])
        resolved = ts(inc["resolved"]) if inc.get("resolved") is not None else None
        log_start_val = ts(inc.get("log_start")) if inc.get("log_start") is not None else created
        log_end_val   = ts(inc.get("log_end"))   if inc.get("log_end")   is not None else created
        last_seen_val = ts(inc.get("last_seen")) if inc.get("last_seen") is not None else created

        vstatus = (
            "VERIFIED"            if status == "FIX_VERIFIED"
            else "DEPLOYED_UNVERIFIED" if status == "FIX_DEPLOYED"
            else "PENDING"
        )

        integration_flow_name = inc.get("integration_flow_name") or iflow
        artifact_id_val       = inc.get("artifact_id") or iflow

        cur.execute(
            f"""INSERT INTO "{_INCIDENTS_TABLE}"
               (incident_id, message_guid, iflow_id, sender, receiver, status,
                error_type, error_message, root_cause, proposed_fix,
                fix_summary, rca_confidence, occurrence_count,
                verification_status, source_type, tags,
                created_at, resolved_at,
                log_start, log_end, last_seen,
                correlation_id, integration_flow_name, artifact_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                iid, msg_guid, iflow,
                inc.get("sender"), inc.get("receiver"),
                status,
                inc["error_type"], inc.get("error_message"),
                inc.get("root_cause"), inc.get("proposed_fix"),
                inc.get("fix_summary"),
                inc["rca_confidence"], inc["occurrence_count"],
                vstatus, "EVENT_MESH", DEMO_TAG,
                created, resolved,
                log_start_val, log_end_val, last_seen_val,
                inc.get("correlation_id") or msg_guid,
                integration_flow_name, artifact_id_val,
            ),
        )
    print(f"  ✓ Inserted {len(incident_ids)} incidents")
    return incident_ids


def insert_tickets(cur, incident_ids: list):
    """Insert escalation tickets linked to their incidents."""
    count = 0
    for (inc_idx, ticket_source, priority, t_status, itsm_id, resolved_hrs) in TICKETS:
        iid      = incident_ids[inc_idx]
        inc      = INCIDENTS[inc_idx]
        iflow    = inc["iflow_id"]
        etype    = inc["error_type"]
        tid      = str(uuid.uuid4())
        created  = ts(inc["created"])
        resolved = ts(resolved_hrs) if resolved_hrs else None
        err_msg  = inc.get("error_message") or ""

        cur.execute(
            f"""INSERT INTO "{_TICKETS_TABLE}"
               (ticket_id, incident_id, iflow_id, error_type,
                title, description, priority, status,
                ticket_source, itsm_ticket_id, message_guid,
                assigned_to, resolution_notes,
                created_at, updated_at, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tid, iid, iflow, etype,
                f"Auto-remediation escalation: {iflow} — {etype}",
                (
                    f"iFlow: {iflow}\n"
                    f"Error type: {etype}\n"
                    f"Error: {err_msg[:300]}\n"
                    f"Root cause: {inc.get('root_cause') or 'Pending analysis'}"
                ),
                priority, t_status,
                ticket_source, itsm_id, str(uuid.uuid4()),
                "sap-cpi-ops@company.com",
                inc.get("fix_summary") if t_status == "RESOLVED" else None,
                created, created, resolved,
            ),
        )
        count += 1
    print(f"  ✓ Inserted {count} escalation tickets")


def wipe_demo_data(cur):
    """Delete all rows tagged as demo seed data."""
    cur.execute(f'DELETE FROM "{_INCIDENTS_TABLE}" WHERE tags=?', (DEMO_TAG,))
    cur.execute(f"""DELETE FROM "{_TICKETS_TABLE}" WHERE ticket_source IN
                   ('CLASSIFIER_ESCALATION','FIX_FAILED_ESCALATION')
                   AND itsm_ticket_id LIKE 'ITSM-TKT-10%'""")
    print("  ✓ Demo data wiped")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Seed demo data for Orbit client presentation")
    parser.add_argument("--clear", action="store_true", help="Wipe existing demo data then re-insert")
    parser.add_argument("--wipe",  action="store_true", help="Wipe demo data only, do not insert")
    args = parser.parse_args()

    print("\n🌱 Orbit Demo Data Seeder")
    print("─" * 40)

    try:
        from db.database import ensure_em_schema
        ensure_em_schema()
    except Exception as e:
        print(f"  ✗ Schema migration failed: {e}")
        sys.exit(1)

    try:
        conn = get_connection()
        cur  = conn.cursor()
    except Exception as e:
        print(f"  ✗ Could not connect to HANA: {e}")
        sys.exit(1)

    if args.wipe or args.clear:
        print("  Wiping existing demo data...")
        wipe_demo_data(cur)
        conn.commit()
        if args.wipe:
            conn.close()
            print("\n✅ Done — demo data wiped.\n")
            return

    print("  Inserting demo incidents and tickets...")
    incident_ids = insert_incidents(cur)
    insert_tickets(cur, incident_ids)
    conn.commit()
    conn.close()

    print("\n✅ Done! Here's what was inserted:\n")
    print("  Status breakdown:")
    status_counts: dict = {}
    for row in INCIDENTS:
        s = row["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
    for s, c in sorted(status_counts.items(), key=lambda x: x[1], reverse=True):
        bar = "█" * c
        print(f"    {s:<30} {bar} {c}")

    print(f"\n  Escalation tickets : {len(TICKETS)}")
    print(f"    OPEN             : {sum(1 for t in TICKETS if t[3]=='OPEN')}")
    print(f"    RESOLVED         : {sum(1 for t in TICKETS if t[3]=='RESOLVED')}")
    print(f"\n  To wipe:   python seed_demo_data.py --wipe")
    print(f"  To re-seed: python seed_demo_data.py --clear\n")


if __name__ == "__main__":
    main()
