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

# ── Seed data definition ──────────────────────────────────────────────────────

now = datetime.now(timezone.utc)

def ts(hours_ago: float) -> str:
    return (now - timedelta(hours=hours_ago)).isoformat()

# iFlows — realistic SAP CPI integration names
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
]

# Each row: (iflow, error_type, status, occurrence_count, rca_confidence,
#            error_message, root_cause, proposed_fix, fix_summary,
#            hours_ago_created, hours_ago_resolved, sender, receiver)
INCIDENTS = [
    # ── FIX_VERIFIED (auto-fixed successfully) ──────────────────────────────
    (IFLOWS[0], "MAPPING_ERROR",       "FIX_VERIFIED",   3, 0.92,
     "XPathException: Variable 'materialCode' not declared in namespace",
     "XPath expression references undeclared namespace prefix in MM_MaterialTransform",
     "Declare namespace prefix in message mapping step MM_MaterialTransform",
     "Added namespace declaration to XPath expression. iFlow redeployed and verified.",
     48, 46, "S4HANA", "MDG"),

    (IFLOWS[1], "MAPPING_ERROR",       "FIX_VERIFIED",   2, 0.88,
     "NullPointerException in SalesOrder mapping step",
     "Optional field 'customerPO' accessed without null check in mapping",
     "Add null guard for customerPO field in mapping function",
     "Null guard added to mapping. Message processed successfully after redeploy.",
     36, 34, "SFDC", "S4HANA"),

    (IFLOWS[2], "AUTH_CONFIG_ERROR",   "FIX_VERIFIED",   1, 0.95,
     "HTTP 401 Unauthorized — credential alias 'Vendor_API_Cred' not found",
     "Credential alias name mismatch — iFlow references 'Vendor_API_Cred' but SecureStore has 'VendorAPI_Cred'",
     "Update receiver adapter credential alias to 'VendorAPI_Cred'",
     "Credential alias corrected in receiver adapter. iFlow redeployed and authenticated.",
     24, 23, "MDG", "Vendor Portal"),

    (IFLOWS[5], "ADAPTER_CONFIG_ERROR","FIX_VERIFIED",   2, 0.91,
     "HTTP 404 Not Found — /api/v1/customers endpoint does not exist",
     "Receiver HTTP adapter URL path is /api/v1/customers but API expects /api/v2/customers",
     "Update receiver adapter URL path from /api/v1 to /api/v2",
     "Endpoint URL updated to /api/v2/customers. Response 200 OK confirmed.",
     20, 19, "CRM", "Customer API"),

    (IFLOWS[6], "MAPPING_ERROR",       "FIX_VERIFIED",   4, 0.87,
     "XSLT transformation failed: element 'StockType' not found in source",
     "Source schema change — StockType field renamed to StorageType in upstream S4HANA patch",
     "Update XSLT mapping to use StorageType instead of StockType",
     "XSLT updated. All 4 queued messages replayed and processed successfully.",
     15, 14, "S4HANA", "WMS"),

    # ── FIX_DEPLOYED (deployed, awaiting verification) ──────────────────────
    (IFLOWS[3], "ADAPTER_CONFIG_ERROR","FIX_DEPLOYED",   1, 0.83,
     "HTTP 400 Bad Request — Content-Type header missing in POST",
     "Receiver HTTP adapter missing Content-Type: application/json header",
     "Add Content-Type header to receiver HTTP adapter configuration",
     None,
     4, None, "AP", "Finance API"),

    # ── FIX_IN_PROGRESS ─────────────────────────────────────────────────────
    (IFLOWS[4], "MAPPING_ERROR",       "FIX_IN_PROGRESS",2, 0.79,
     "java.lang.ClassCastException in PO mapping — expected String got Integer",
     "Type mismatch in PO line item quantity field mapping",
     "Cast quantity field to String before mapping",
     None,
     2, None, "Ariba", "S4HANA"),

    # ── RCA_COMPLETE (analysed, fix not yet started) ─────────────────────────
    (IFLOWS[7], "AUTH_CONFIG_ERROR",   "RCA_COMPLETE",   1, 0.86,
     "HTTP 401 — Security material 'DeliveryAPI_OAuth' expired",
     "OAuth token in security material DeliveryAPI_OAuth has expired",
     "Refresh OAuth credentials in Security Material store",
     None,
     3, None, "EWM", "Delivery API"),

    # ── AWAITING_APPROVAL ───────────────────────────────────────────────────
    (IFLOWS[8], "AUTH_ERROR",          "AWAITING_APPROVAL",1, 0.61,
     "Authentication failed — unclear if iFlow config or credentials are at fault",
     "Could not determine root cause automatically — may be credential rotation or iFlow misconfiguration",
     "Human review required to identify if credential store or iFlow adapter needs update",
     None,
     6, None, "Treasury", "Payment Gateway"),

    # ── TICKET_CREATED — CLASSIFIER_ESCALATION (non-fixable errors) ─────────
    (IFLOWS[9], "BACKEND_ERROR",       "TICKET_CREATED", 5, 0.94,
     "HTTP 503 Service Unavailable from GoodsReceipt backend",
     "Backend GR service is down — infrastructure issue outside iFlow scope",
     "Backend team must restore GR service. iFlow is working correctly.",
     None,
     12, None, "WMS", "GR Backend"),

    (IFLOWS[1], "SSL_ERROR",           "TICKET_CREATED", 2, 0.97,
     "PKIX path building failed: unable to find valid certification path",
     "SSL certificate on SalesOrder receiver endpoint has expired",
     "Certificate renewal required on receiver endpoint — cannot be fixed by agent",
     None,
     18, None, "SFDC", "S4HANA"),

    # ── TICKET_CREATED — FIX_FAILED_ESCALATION (fixer exhausted attempts) ───
    (IFLOWS[0], "MAPPING_ERROR",       "TICKET_CREATED", 6, 0.55,
     "Recursive mapping loop detected in MM_MaterialEnrich — stack overflow",
     "Complex recursive structure in mapping could not be resolved automatically after 3 attempts",
     "Manual mapping redesign required — recursion depth exceeds auto-fix capability",
     None,
     30, None, "S4HANA", "MDG"),

    # ── FIX_FAILED ──────────────────────────────────────────────────────────
    (IFLOWS[3], "ADAPTER_CONFIG_ERROR","FIX_FAILED",     3, 0.72,
     "HTTP 400 — malformed JSON body in Invoice POST",
     "Request body structure changed in Invoice API v3 — requires schema update",
     "Update request body mapping to match Invoice API v3 schema",
     None,
     8, None, "AP", "Invoice API"),

    # ── HUMAN_RESOLVED (ticket closed by engineer) ──────────────────────────
    (IFLOWS[9], "BACKEND_ERROR",       "HUMAN_RESOLVED", 3, 0.94,
     "HTTP 500 from GoodsReceipt confirmation service",
     "Database connection pool exhausted on GR backend",
     "Backend team to increase DB connection pool size",
     "DBA increased connection pool from 50 to 200. Service restored. All queued messages replayed.",
     72, 68, "WMS", "GR Backend"),

    # ── DETECTED (just arrived, no analysis yet) ────────────────────────────
    (IFLOWS[4], "UNKNOWN_ERROR",       "DETECTED",       1, 0.0,
     "com.sap.it.rt.adapter.http.api.exception.HttpResponseException: 422 Unprocessable Entity",
     None,
     None,
     None,
     0.5, None, "Ariba", "S4HANA"),
]

# ── Escalation tickets (for TICKET_CREATED incidents) ────────────────────────
# (incident_index_in_INCIDENTS, ticket_source, priority, status, itsm_ticket_id, resolved_at_offset)
TICKETS = [
    (9,  "CLASSIFIER_ESCALATION",  "HIGH",     "OPEN",     "ITSM-TKT-1041", None),
    (10, "CLASSIFIER_ESCALATION",  "CRITICAL", "OPEN",     "ITSM-TKT-1042", None),
    (11, "FIX_FAILED_ESCALATION",  "HIGH",     "OPEN",     "ITSM-TKT-1043", None),
    (13, "CLASSIFIER_ESCALATION",  "HIGH",     "RESOLVED", "ITSM-TKT-1038", 68),   # HUMAN_RESOLVED incident
]


# ── Insert logic ──────────────────────────────────────────────────────────────

def insert_incidents(cur) -> list:
    """Insert all demo incidents. Returns list of generated incident_ids."""
    incident_ids = []
    for (iflow, error_type, status, occ, conf,
         error_msg, root_cause, proposed_fix, fix_summary,
         hours_created, hours_resolved, sender, receiver) in INCIDENTS:

        iid = str(uuid.uuid4())
        incident_ids.append(iid)
        created  = ts(hours_created)
        resolved = ts(hours_resolved) if hours_resolved else None
        vstatus  = (
            "VERIFIED"           if status == "FIX_VERIFIED"
            else "DEPLOYED_UNVERIFIED" if status == "FIX_DEPLOYED"
            else "PENDING"
        )

        cur.execute(
            f"""INSERT INTO "{_INCIDENTS_TABLE}"
               (incident_id, message_guid, iflow_id, sender, receiver, status,
                error_type, error_message, root_cause, proposed_fix,
                fix_summary, rca_confidence, occurrence_count,
                verification_status, source_type, tags,
                created_at, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                iid, iid, iflow, sender, receiver, status,
                error_type, error_msg, root_cause, proposed_fix,
                fix_summary, conf, occ,
                vstatus, "EVENT_MESH", DEMO_TAG,
                created, resolved,
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
        iflow    = inc[0]
        etype    = inc[1]
        tid      = str(uuid.uuid4())
        created  = ts(inc[9])
        resolved = ts(resolved_hrs) if resolved_hrs else None

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
                f"[SAP CPI] Auto-remediation escalation: {iflow} — {etype}",
                f"iFlow: {iflow}\nError type: {etype}\nError: {inc[5][:300] if inc[5] else ''}\nRoot cause: {inc[6] or 'Pending analysis'}",
                priority, t_status,
                ticket_source, itsm_id, str(uuid.uuid4()),
                "sap-cpi-ops@company.com",
                inc[8] if t_status == "RESOLVED" else None,
                created, created, resolved,
            ),
        )
        count += 1
    print(f"  ✓ Inserted {count} escalation tickets")


def wipe_demo_data(cur):
    """Delete all rows tagged as demo seed data."""
    cur.execute(f'DELETE FROM "{_INCIDENTS_TABLE}" WHERE tags=?', (DEMO_TAG,))
    # Tickets are linked by incident_id — clean those up too
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

    # Run schema migrations so older tables gain any new columns before INSERT
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
    status_counts = {}
    for row in INCIDENTS:
        s = row[2]
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