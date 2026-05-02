import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import SvgIcon from "../../components/icons/SvgIcon.tsx";
import {
  fetchMonitorMessages,
  fetchMonitorMessageDetail,
  analyzeMessage,
  explainError,
  generateFixPatch,
  applyMessageFix,
  fetchFixStatus,
  fetchTickets,
  updateTicket,
  fetchPendingApprovals,
  approveIncident,
} from "../../services/api.ts";
import type {
  IMonitorMessage,
  IFilterState,
  IMessageDetail,
  IFixPatchResponse,
  IFieldChange,
  IFixPlanStep,
  IHistoryTimelineEntry,
  IErrorExplanation,
} from "../../types/index.ts";
import styles from "./observability.module.css";
import EventMeshFlow from "./EventMeshFlow.tsx";

/* ── Top-level tab type ───────────────────────────────────────────────── */
type MainTabKey = "messages" | "tickets" | "approvals" | "eventmesh";

/* ── Ticket and Approval interfaces ───────────────────────────────────── */
interface Ticket {
  ticket_id: string;
  incident_id: string;
  iflow_id: string;
  error_type: string;
  title: string;
  description: string;
  priority: string;
  status: string;
  assigned_to: string | null;
  resolution_notes: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

interface Approval {
  incident_id: string;
  iflow_id: string;
  error_type: string;
  error_message: string;
  root_cause: string;
  proposed_fix: string;
  rca_confidence: number;
  status: string;
  created_at: string;
  pending_since: string;
  message_guid: string;
}

/* ── Status config ───────────────────────────────────────────────────── */
type StatusCfg = { label: string; color: string; bg: string; dot: string };

const RED:    StatusCfg = { label: "Failed",        color: "#dc2626", bg: "#fee2e2", dot: "#ef4444" };
const GREEN:  StatusCfg = { label: "Success",       color: "#16a34a", bg: "#dcfce7", dot: "#22c55e" };
const BLUE:   StatusCfg = { label: "Processing",    color: "#2563eb", bg: "#dbeafe", dot: "#3b82f6" };
const AMBER:  StatusCfg = { label: "Retry",         color: "#d97706", bg: "#fef3c7", dot: "#f59e0b" };
const PURPLE: StatusCfg = { label: "Pending",       color: "#7c3aed", bg: "#ede9fe", dot: "#8b5cf6" };
const GREY:   StatusCfg = { label: "Unknown",       color: "#6b7280", bg: "#f3f4f6", dot: "#9ca3af" };

const STATUS_CONFIG: Record<string, StatusCfg> = {
  FAILED:     RED,
  SUCCESS:    GREEN,
  PROCESSING: BLUE,
  RETRY:      AMBER,
  DETECTED:                        { ...RED,    label: "Detected" },
  CLASSIFIED:                      { ...BLUE,   label: "Classified" },
  RCA_IN_PROGRESS:                 { ...BLUE,   label: "Analyzing" },
  RCA_COMPLETE:                    { ...BLUE,   label: "RCA Done" },
  RCA_FAILED:                      { ...RED,    label: "RCA Failed" },
  FIX_IN_PROGRESS:                 { ...AMBER,  label: "Fixing" },
  FIX_FAILED:                      { ...RED,    label: "Fix Failed" },
  FIX_FAILED_UPDATE:               { ...RED,    label: "Fix Failed (Update)" },
  FIX_FAILED_DEPLOY:               { ...RED,    label: "Fix Failed (Deploy)" },
  FIX_FAILED_RUNTIME:              { ...RED,    label: "Fix Failed (Runtime)" },
  FIX_APPLIED_PENDING_VERIFICATION:{ ...AMBER,  label: "Verifying" },
  AUTO_FIXED:                      { ...GREEN,  label: "Auto-Fixed" },
  HUMAN_FIXED:                     { ...GREEN,  label: "Fixed" },
  FIX_VERIFIED:                    { ...GREEN,  label: "Verified" },
  PENDING_APPROVAL:                { ...PURPLE, label: "Pending Approval" },
  AWAITING_APPROVAL:               { ...PURPLE, label: "Awaiting Approval" },
  TICKET_CREATED:                  { ...PURPLE, label: "Ticket Created" },
  ARTIFACT_MISSING:                { ...GREY,   label: "Artifact Missing or Deleted" },
  PIPELINE_ERROR:                  { ...RED,    label: "Pipeline Error" },
  REJECTED:                        { ...GREY,   label: "Rejected" },
  RETRIED:                         { ...GREEN,  label: "Retried" },
};

function StatusPill({ status }: { status: string }) {
  const key = (status || "").toUpperCase();
  const cfg = STATUS_CONFIG[key] ?? { ...GREY, label: status || "Unknown" };
  return (
    <span className={styles.statusPill} style={{ color: cfg.color, background: cfg.bg }}>
      <span className={styles.statusDot} style={{ background: cfg.dot }} />
      {cfg.label}
    </span>
  );
}

const TERMINAL_STATUSES = new Set([
  "AUTO_FIXED", "HUMAN_FIXED", "FIX_VERIFIED", "RETRIED",
  "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
  "PIPELINE_ERROR", "REJECTED", "TICKET_CREATED", "ARTIFACT_MISSING",
]);

/* ── Tab definitions ─────────────────────────────────────────────────── */
type TabKey = "error" | "ai" | "properties" | "artifact" | "attachments" | "history";

const TABS: { key: TabKey; label: string; tip: string }[] = [
  { key: "error",       label: "Error Details",                       tip: "Raw error message, error type and processing timestamps from SAP CPI" },
  { key: "ai",          label: "AI Recommendations & Suggested Fix",  tip: "AI-generated diagnosis, proposed fix and confidence score from SAP AI Core" },
  { key: "properties",  label: "Properties",                          tip: "Message properties, adapter configuration and business context" },
  { key: "artifact",    label: "Artifact",                            tip: "iFlow artifact metadata: version, deployment info and runtime node" },
  { key: "attachments", label: "Attachments",                         tip: "Message payload attachments from the CPI processing log" },
  { key: "history",     label: "History",                             tip: "Timeline of status changes for this remediation incident" },
];

const INITIAL_FILTERS: IFilterState = {
  statuses: [], types: [], artifacts: [],
  dateFrom: "", dateTo: "", idQuery: "", searchQuery: "",
};

const CARD_TIPS: Record<string, string> = {
  FAILED:      "Messages in FAILED, FIX_FAILED, RCA_FAILED or DETECTED state — need attention",
  SUCCESS:     "Messages that reached AUTO_FIXED, HUMAN_FIXED or FIX_VERIFIED state",
  PROCESSING:  "Messages currently in RCA, classification or fix-in-progress stages",
  RETRY:       "Messages pending approval, ticket created or scheduled for retry",
};

// Explicit ordered keys for the 4 summary cards — never rely on STATUS_CONFIG insertion order
const SUMMARY_CARD_KEYS = ["FAILED", "SUCCESS", "PROCESSING", "RETRY"] as const;

/* ── Error type reference data ────────────────────────────────────────── */
type ErrorTypeMeta = { label: string; description: string; action: "AUTO_FIX" | "TICKET_CREATED" | "APPROVAL" | "RETRY"; dot: string };

const ERROR_TYPE_META: Record<string, ErrorTypeMeta> = {
  MAPPING_ERROR:        { label: "Mapping Error",        description: "Message mapping structural issue — source/target field mismatch or wrong XSD.",                          action: "AUTO_FIX",       dot: "#22c55e" },
  DATA_VALIDATION:      { label: "Data Validation",      description: "Payload fails schema validation — missing required field or wrong data type.",                             action: "AUTO_FIX",       dot: "#22c55e" },
  AUTH_CONFIG_ERROR:    { label: "Auth Config Error",    description: "Wrong credential alias or security material reference inside the iFlow.",                                  action: "AUTO_FIX",       dot: "#22c55e" },
  ADAPTER_CONFIG_ERROR: { label: "Adapter Config Error", description: "Receiver adapter misconfigured — endpoint URL or protocol settings wrong (HTTP 4xx).",                    action: "AUTO_FIX",       dot: "#22c55e" },
  GROOVY_ERROR:         { label: "Groovy Error",         description: "Groovy script step threw an uncaught exception — logic or null-reference bug.",                           action: "AUTO_FIX",       dot: "#22c55e" },
  SCRIPT_ERROR:         { label: "Script Error",         description: "Script step execution failure — syntax error or missing variable.",                                        action: "AUTO_FIX",       dot: "#22c55e" },
  SOAP_ERROR:           { label: "SOAP Error",           description: "SOAP adapter configuration issue — WSDL mismatch or SOAPAction header wrong.",                            action: "AUTO_FIX",       dot: "#22c55e" },
  ODATA_ERROR:          { label: "OData Error",          description: "OData adapter error — entity set path or query options misconfigured.",                                   action: "AUTO_FIX",       dot: "#22c55e" },
  ROUTING_ERROR:        { label: "Routing Error",        description: "Message routing misconfiguration — router condition does not match any branch.",                           action: "AUTO_FIX",       dot: "#22c55e" },
  PROPERTY_ERROR:       { label: "Property Error",       description: "Exchange property referenced in a step does not exist or has the wrong name.",                             action: "AUTO_FIX",       dot: "#22c55e" },
  SSL_ERROR:            { label: "SSL Error",            description: "SSL/TLS certificate error — certificate expired, untrusted CA, or wrong trust store.",                    action: "TICKET_CREATED", dot: "#ef4444" },
  BACKEND_ERROR:        { label: "Backend Error",        description: "Backend service returned HTTP 5xx — the target system is down or returning errors.",                       action: "TICKET_CREATED", dot: "#ef4444" },
  SFTP_ERROR:           { label: "SFTP Error",           description: "SFTP server-side issue — check directory path, permissions, and server availability.",                    action: "TICKET_CREATED", dot: "#ef4444" },
  DUPLICATE_ERROR:      { label: "Duplicate Error",      description: "Duplicate record rejected by the target system — idempotency issue requires a source-side fix.",           action: "TICKET_CREATED", dot: "#ef4444" },
  PAYLOAD_SIZE_ERROR:   { label: "Payload Size Error",   description: "Payload exceeds the size limit — message splitting or infrastructure change required.",                    action: "TICKET_CREATED", dot: "#ef4444" },
  IDOC_ERROR:           { label: "IDoc Error",           description: "IDoc processing failure in SAP ERP — SAP Basis or functional team must investigate.",                      action: "TICKET_CREATED", dot: "#ef4444" },
  RESOURCE_ERROR:       { label: "Resource Error",       description: "Out-of-memory or CPU exhaustion on the integration node — infrastructure team needed.",                    action: "TICKET_CREATED", dot: "#ef4444" },
  AUTH_ERROR:           { label: "Auth Error",           description: "Authentication failed but cause is ambiguous — could be credentials or iFlow config.",                     action: "APPROVAL",       dot: "#f59e0b" },
  UNKNOWN_ERROR:        { label: "Unknown Error",        description: "Error could not be classified by the rule engine or LLM — human review required before any fix.",         action: "APPROVAL",       dot: "#f59e0b" },
  CONNECTIVITY_ERROR:   { label: "Connectivity Error",   description: "Transient network issue — connection refused or timed out. Agent retries automatically.",                  action: "RETRY",          dot: "#3b82f6" },
};

const ACTION_GROUPS: { key: ErrorTypeMeta["action"]; iconName: "lightning" | "tickets" | "user" | "loop"; label: string; desc: string; color: string; bg: string }[] = [
  { key: "AUTO_FIX",       iconName: "lightning", label: "Auto-Fix",          desc: "Agent resolves automatically — no manual action needed",  color: "#15803d", bg: "#f0fdf4" },
  { key: "TICKET_CREATED", iconName: "tickets",   label: "Ticket Created",    desc: "Escalated to a human team — a ticket has been created",   color: "#b91c1c", bg: "#fff5f5" },
  { key: "APPROVAL",       iconName: "user",      label: "Awaiting Approval", desc: "Agent needs human sign-off before applying any fix",      color: "#92400e", bg: "#fffbeb" },
  { key: "RETRY",          iconName: "loop",      label: "Retry",             desc: "Transient issue — agent retries the iFlow automatically", color: "#1e40af", bg: "#eff6ff" },
];

/* ── Field-change highlight component ────────────────────────────────── */
function FieldChangeHighlight({ changes }: { changes: IFieldChange[] }) {
  if (!changes?.length) return null;
  return (
    <div className={styles.fieldChanges}>
      {changes.map((fc, i) => (
        <div key={i} className={styles.fieldChangeRow}>
          Field <span className={styles.oldField}>{fc.old_field}</span> was renamed to{" "}
          <span className={styles.newField}>{fc.new_field}</span> but message mapping still references{" "}
          <span className={styles.oldField}>{fc.old_field}</span>
        </div>
      ))}
    </div>
  );
}

/* ── Confidence badge ────────────────────────────────────────────────── */
function ConfidenceBadge({ value, label }: { value: number; label: string }) {
  const pct = Math.round(value * 100);
  const color = value >= 0.9 ? "#16a34a" : value >= 0.7 ? "#d97706" : "#dc2626";
  return (
    <div className={styles.confidenceSection}>
      <span className={styles.confidenceVal} style={{ color }}>
        Confidence: {value.toFixed(2)} ({label})
      </span>
      <div className={styles.confidenceBar}>
        <div className={styles.confidenceFill} style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

/* ── Fix Plan Step component ─────────────────────────────────────────── */
function FixPlanSteps({ steps }: { steps: IFixPlanStep[] }) {
  return (
    <div className={styles.fixPlanSteps}>
      {steps.map((s) => (
        <div key={s.step_number} className={styles.fixPlanStep}>
          <div className={styles.fixStepHeader}>
            <span className={styles.fixStepNum}>{s.step_number}.</span>
            <span className={styles.fixStepTitle}>{s.title}</span>
          </div>
          <p className={styles.fixStepDesc}>{s.description}</p>
          {s.sub_steps?.length > 0 && (
            <ul className={styles.fixSubSteps}>
              {s.sub_steps.map((sub, j) => <li key={j}>{sub}</li>)}
            </ul>
          )}
          {s.note && <div className={styles.fixStepNote}>{s.note}</div>}
        </div>
      ))}
    </div>
  );
}

/* ── Pipeline stage rail (shown during fix execution) ───────────────── */
const FIX_STAGES = ["Submit", "Get iFlow", "Validate", "Patch", "Deploy"] as const;

function PipelineStageRail({ stepIndex, totalSteps }: { stepIndex: number; totalSteps: number }) {
  const slots = FIX_STAGES.length;
  const active = Math.min(
    stepIndex <= 0 ? 0 : Math.round((stepIndex / Math.max(totalSteps, 1)) * (slots - 1)),
    slots - 1
  );
  return (
    <div className={styles.stageRail}>
      {FIX_STAGES.map((label, i) => {
        const done     = i < active;
        const isActive = i === active;
        return (
          <div key={label} style={{ display: "contents" }}>
            <div className={styles.stageStep}>
              <div
                className={[
                  styles.stageDot,
                  done     ? styles.stageDotDone   : "",
                  isActive ? styles.stageDotActive : "",
                ].filter(Boolean).join(" ")}
              >
                {done ? "✓" : i + 1}
              </div>
              <span
                className={[
                  styles.stageLabel,
                  isActive ? styles.stageLabelActive : "",
                  done     ? styles.stageLabelDone   : "",
                ].filter(Boolean).join(" ")}
              >
                {label}
              </span>
            </div>
            {i < slots - 1 && (
              <div className={[styles.stageConnector, done ? styles.stageConnectorDone : ""].filter(Boolean).join(" ")} />
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ── Timeline component for History tab ──────────────────────────────── */
function Timeline({ entries }: { entries: IHistoryTimelineEntry[] }) {
  const statusIcon: Record<string, string> = {
    completed: "✓", failed: "✕", pending: "○",
    in_progress: "↻", info: "i",
  };
  const statusColor: Record<string, string> = {
    completed: "#16a34a", failed: "#dc2626", pending: "#d97706",
    in_progress: "#2563eb", info: "#6b7280",
  };
  return (
    <div className={styles.timeline}>
      {entries.map((e, i) => (
        <div key={i} className={styles.timelineEntry}>
          <div className={styles.timelineDot} style={{ background: statusColor[e.status] || "#6b7280" }}>
            {statusIcon[e.status] ?? "·"}
          </div>
          <div className={styles.timelineContent}>
            <div className={styles.timelineStep}>{e.step}</div>
            <div className={styles.timelineDesc}>{e.description}</div>
            {e.timestamp && <div className={styles.timelineTs}>{e.timestamp}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Ticket description parser ───────────────────────────────────────── */
function parseTicketDescription(desc: string): Record<string, string> {
  if (!desc) return {};
  const markerPattern = /\b(iFlow|Error|Root cause|Proposed fix|Incident ID|Occurrence count|RCA confidence):\s*/g;
  const parts = desc.split(markerPattern);
  // parts: [preamble, key1, val1, key2, val2, ...]  (split with capturing group)
  const result: Record<string, string> = {};
  for (let i = 1; i < parts.length; i += 2) {
    const key = parts[i].toLowerCase().replace(/\s+/g, '_');
    const val = (parts[i + 1] || '').trim();
    result[key] = val;
  }
  return result;
}

/* ── Rich text renderer ──────────────────────────────────────────────── */
function RichText({ text }: { text: string }) {
  if (!text) return null;
  const lines = text.split(/\n/).filter((l) => l.trim());
  return (
    <div className={styles.richText}>
      {lines.map((line, i) => {
        const isBullet = /^[-•*]\s/.test(line);
        const isNum    = /^\d+\.\s/.test(line);
        if (isBullet) return (
          <div key={i} className={styles.richBullet}>
            <span className={styles.richBulletDot}>•</span>
            <span>{line.replace(/^[-•*]\s/, "")}</span>
          </div>
        );
        if (isNum) return (
          <div key={i} className={styles.richBullet}>
            <span className={styles.richBulletDot}>{line.match(/^\d+/)?.[0]}.</span>
            <span>{line.replace(/^\d+\.\s/, "")}</span>
          </div>
        );
        return <p key={i} className={styles.richPara}>{line}</p>;
      })}
    </div>
  );
}

/* ── AI Error Explanation card ────────────────────────────────────────── */
const CATEGORY_COLORS: Record<string, { color: string; bg: string }> = {
  HTTP_ERROR:          { color: "#b91c1c", bg: "#fee2e2" },
  MAPPING_ERROR:       { color: "#92400e", bg: "#fef3c7" },
  CONNECTIVITY_ERROR:  { color: "#1e40af", bg: "#dbeafe" },
  AUTH_ERROR:          { color: "#6b21a8", bg: "#f3e8ff" },
  DATA_ERROR:          { color: "#92400e", bg: "#fef3c7" },
  TIMEOUT_ERROR:       { color: "#9a3412", bg: "#ffedd5" },
  CONFIG_ERROR:        { color: "#1e40af", bg: "#dbeafe" },
  RUNTIME_ERROR:       { color: "#b91c1c", bg: "#fee2e2" },
};

function ErrorExplanationCard({ exp }: { exp: IErrorExplanation }) {
  const catStyle = CATEGORY_COLORS[exp.error_category] ?? { color: "#374151", bg: "#f3f4f6" };
  return (
    <div className={styles.explainCard}>
      <div className={styles.explainCardHeader}>
        <span className={styles.explainSparkle}><SvgIcon name="rca" size={15} /></span>
        <span className={styles.explainCardTitle}>AI Error Analysis</span>
        <span className={styles.explainCategoryBadge} style={{ color: catStyle.color, background: catStyle.bg }}>
          {exp.category_label || exp.error_category}
        </span>
      </div>

      {exp.summary && (
        <div className={styles.explainSummaryBox}>
          <p className={styles.explainSummaryText}>{exp.summary}</p>
        </div>
      )}

      {exp.what_happened && (
        <div className={styles.explainSection}>
          <div className={styles.explainSectionLabel}>What Happened</div>
          <p className={styles.explainSectionBody}>{exp.what_happened}</p>
        </div>
      )}

      {exp.likely_causes?.length > 0 && (
        <div className={styles.explainSection}>
          <div className={styles.explainSectionLabel}>Likely Causes</div>
          <ul className={styles.explainList}>
            {exp.likely_causes.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}

      {exp.recommended_actions?.length > 0 && (
        <div className={styles.explainSection}>
          <div className={styles.explainSectionLabel}>Recommended Actions</div>
          <ol className={styles.explainList}>
            {exp.recommended_actions.map((a, i) => <li key={i}>{a}</li>)}
          </ol>
        </div>
      )}
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   MAIN COMPONENT
   ════════════════════════════════════════════════════════════════════════ */
export default function Observability() {
  // Main tab state
  const [mainTab, setMainTab] = useState<MainTabKey>("messages");
  const [guideOpen, setGuideOpen] = useState(false);
  
  const [filters, setFilters] = useState<IFilterState>(INITIAL_FILTERS);
  const [selectedGuid, setSelectedGuid] = useState<string | null>(null);
  const [selectedMsg, setSelectedMsg] = useState<IMonitorMessage | null>(null);
  const [detail, setDetail] = useState<IMessageDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>("error");

  // Error explanation state
  const [errorExplain, setErrorExplain]           = useState<IErrorExplanation | null>(null);
  const [errorExplainLoading, setErrorExplainLoading] = useState(false);
  const [errorExplainErr, setErrorExplainErr]     = useState<string | null>(null);

  // Fix-related state
  const [fixPatch, setFixPatch] = useState<IFixPatchResponse | null>(null);
  const [fixPatchLoading, setFixPatchLoading] = useState(false);
  const [fixState, setFixState] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [fixResult, setFixResult] = useState<string>("");
  const [fixProgress, setFixProgress] = useState<{
    currentStep: string; stepIndex: number; totalSteps: number; stepsDone: string[];
  } | null>(null);
  const [analyzeLoading, setAnalyzeLoading] = useState(false);

  // Approval action feedback
  const [approvalActionError, setApprovalActionError] = useState<string | null>(null);
  const [bulkActionLoading, setBulkActionLoading] = useState<"approving" | "rejecting" | null>(null);

  // Ticket resolution state
  const [resolvingTicketId, setResolvingTicketId] = useState<string | null>(null);
  const [ticketActionError, setTicketActionError] = useState<string | null>(null);

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["monitor-messages"],
    queryFn: fetchMonitorMessages,
    refetchInterval: 30_000,   // was 10s — each poll hits SAP CPI OData (slow)
    staleTime: 20_000,
  });

  // Fetch tickets
  const { data: ticketsData, isLoading: ticketsLoading, refetch: refetchTickets } = useQuery({
    queryKey: ["escalation-tickets"],
    queryFn: fetchTickets,
    refetchInterval: 30_000,
    enabled: mainTab === "tickets",
  });

  // Fetch approvals
  const { data: approvalsData, isLoading: approvalsLoading, refetch: refetchApprovals } = useQuery({
    queryKey: ["pending-approvals"],
    queryFn: fetchPendingApprovals,
    refetchInterval: 30_000,
    enabled: mainTab === "approvals",
  });

  const STATUS_GROUP: Record<string, string[]> = {
    FAILED:     ["FAILED", "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME", "RCA_FAILED", "PIPELINE_ERROR", "DETECTED", "ARTIFACT_MISSING"],
    SUCCESS:    ["AUTO_FIXED", "HUMAN_FIXED", "FIX_VERIFIED", "RETRIED", "SUCCESS"],
    PROCESSING: ["RCA_IN_PROGRESS", "FIX_IN_PROGRESS", "CLASSIFIED", "RCA_COMPLETE", "FIX_APPLIED_PENDING_VERIFICATION", "PROCESSING"],
    RETRY:      ["RETRY", "PENDING_APPROVAL", "TICKET_CREATED", "AWAITING_APPROVAL"],
  };

  const messages = useMemo(() => {
    return ((data?.messages || []) as IMonitorMessage[]).filter((m) => {
      const s = (m.status || "").toUpperCase();
      if (filters.statuses.length) {
        const allowed = filters.statuses.flatMap((g) => STATUS_GROUP[g] || [g]);
        if (!allowed.includes(s)) return false;
      }
      if (filters.searchQuery) {
        const q = filters.searchQuery.toLowerCase();
        if (!(m.iflow_display || m.title || "").toLowerCase().includes(q)) return false;
      }
      if (filters.idQuery) {
        const q = filters.idQuery.toLowerCase();
        if (!(m.message_guid || "").toLowerCase().includes(q) &&
            !(m.iflow_display || "").toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }, [data, filters]);

  const counts = useMemo(() => {
    const all = (data?.messages || []) as IMonitorMessage[];
    const result: Record<string, number> = { FAILED: 0, SUCCESS: 0, PROCESSING: 0, RETRY: 0 };
    all.forEach((m) => {
      const s = (m.status || "").toUpperCase();
      if (["FAILED", "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME", "RCA_FAILED", "PIPELINE_ERROR", "DETECTED", "ARTIFACT_MISSING"].includes(s)) result.FAILED++;
      else if (["AUTO_FIXED", "HUMAN_FIXED", "FIX_VERIFIED", "RETRIED", "SUCCESS"].includes(s)) result.SUCCESS++;
      else if (["RCA_IN_PROGRESS", "FIX_IN_PROGRESS", "CLASSIFIED", "RCA_COMPLETE", "FIX_APPLIED_PENDING_VERIFICATION"].includes(s)) result.PROCESSING++;
      else if (["RETRY", "PENDING_APPROVAL", "TICKET_CREATED", "AWAITING_APPROVAL"].includes(s)) result.RETRY++;
    });
    return result;
  }, [data]);

  const tickets = (ticketsData?.tickets || []) as Ticket[];
  const approvals = (approvalsData?.pending || []) as Approval[];

  /* ── Approval actions ──────────────────────────────────────────────── */
  const handleApprove = useCallback(async (incidentId: string) => {
    setApprovalActionError(null);
    try {
      await approveIncident(incidentId, true, "Approved via UI");
      refetchApprovals();
    } catch {
      setApprovalActionError("Approval failed — network error. Please check your connection.");
    }
  }, [refetchApprovals]);

  const handleReject = useCallback(async (incidentId: string) => {
    setApprovalActionError(null);
    try {
      await approveIncident(incidentId, false, "Rejected via UI");
      refetchApprovals();
    } catch {
      setApprovalActionError("Rejection failed — network error. Please check your connection.");
    }
  }, [refetchApprovals]);

  const handleApproveAll = useCallback(async () => {
    const pending = (approvalsData?.pending || []) as Approval[];
    const targets = pending.filter((a) => a.status === "AWAITING_APPROVAL");
    if (!targets.length) return;
    setApprovalActionError(null);
    setBulkActionLoading("approving");
    const errors: string[] = [];
    await Promise.allSettled(
      targets.map((a) =>
        approveIncident(a.incident_id, true, "Bulk approved via UI").catch(() => {
          errors.push(a.incident_id);
        })
      )
    );
    setBulkActionLoading(null);
    if (errors.length) setApprovalActionError(`${errors.length} approval(s) failed. Others succeeded.`);
    refetchApprovals();
  }, [approvalsData, refetchApprovals]);

  const handleRejectAll = useCallback(async () => {
    const pending = (approvalsData?.pending || []) as Approval[];
    const targets = pending.filter((a) => a.status === "AWAITING_APPROVAL");
    if (!targets.length) return;
    setApprovalActionError(null);
    setBulkActionLoading("rejecting");
    const errors: string[] = [];
    await Promise.allSettled(
      targets.map((a) =>
        approveIncident(a.incident_id, false, "Bulk rejected via UI").catch(() => {
          errors.push(a.incident_id);
        })
      )
    );
    setBulkActionLoading(null);
    if (errors.length) setApprovalActionError(`${errors.length} rejection(s) failed. Others succeeded.`);
    refetchApprovals();
  }, [approvalsData, refetchApprovals]);

  /* ── Mark ticket resolved ─────────────────────────────────────────── */
  const handleMarkResolved = useCallback(async (ticketId: string, currentStatus: string) => {
    setResolvingTicketId(ticketId);
    setTicketActionError(null);
    try {
      if (currentStatus.toUpperCase() === "OPEN") {
        await updateTicket(ticketId, { status: "IN_PROGRESS" });
      }
      await updateTicket(ticketId, { status: "RESOLVED" });
      refetchTickets();
    } catch (e) {
      setTicketActionError(e instanceof Error ? e.message : "Failed to update ticket");
    } finally {
      setResolvingTicketId(null);
    }
  }, [refetchTickets]);

  /* ── Select a message and load full detail ─────────────────────────── */
  const handleSelect = useCallback(async (msg: IMonitorMessage) => {
    const guid = msg.message_guid;
    if (!guid) return;
    // Cancel any running fix poll for the previous message
    pollAbortRef.current.cancelled = true;
    setSelectedGuid(guid);
    setSelectedMsg(msg);
    setDetail(null);
    setFixPatch(null);
    setFixState("idle");
    setFixResult("");
    setFixProgress(null);
    setActiveTab("error");
    setErrorExplain(null);
    setErrorExplainLoading(false);
    setErrorExplainErr(null);
    setDetailLoading(true);
    try {
      const d = await fetchMonitorMessageDetail(guid) as IMessageDetail;
      setDetail(d);

      // Restore previously generated fix plan from DB
      if (d.ai_recommendation?.fix_patch) {
        setFixPatch(d.ai_recommendation.fix_patch);
      }

      // Restore fix outcome state from incident status
      const incStatus = (d.incident_status || "").toUpperCase();
      if (["AUTO_FIXED", "HUMAN_INITIATED_FIX", "FIX_VERIFIED", "RETRIED"].includes(incStatus)) {
        setFixState("success");
        setFixResult(d.ai_recommendation?.fix_summary || "Fix applied and deployed successfully.");
      } else if (["FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME"].includes(incStatus)) {
        setFixState("error");
        setFixResult(d.ai_recommendation?.fix_summary || "Fix failed — see history for details.");
      }

      if (d.ai_recommendation?.diagnosis) {
        setActiveTab("ai");
      }
    } catch {
      // Keep previous state
    } finally {
      setDetailLoading(false);
    }
  }, []);

  /* ── Run / re-run AI analysis ──────────────────────────────────────── */
  const handleAnalyze = useCallback(async () => {
    if (!selectedGuid) return;
    setAnalyzeLoading(true);
    try {
      await analyzeMessage(selectedGuid);
      const d = await fetchMonitorMessageDetail(selectedGuid) as IMessageDetail;
      setDetail(d);
      setActiveTab("ai");
    } catch {
      // handled
    } finally {
      setAnalyzeLoading(false);
    }
  }, [selectedGuid]);

  /* ── Explain error ─────────────────────────────────────────────────── */
  const handleExplainError = useCallback(async () => {
    if (!selectedGuid) return;
    setErrorExplainLoading(true);
    setErrorExplainErr(null);
    try {
      const exp = await explainError(selectedGuid) as IErrorExplanation;
      setErrorExplain(exp);
    } catch (e) {
      setErrorExplainErr(e instanceof Error ? e.message : "Failed to explain error");
    } finally {
      setErrorExplainLoading(false);
    }
  }, [selectedGuid]);

  /* ── Generate fix patch ────────────────────────────────────────────── */
  const handleGenerateFixPatch = useCallback(async () => {
    if (!selectedGuid) return;
    setFixPatchLoading(true);
    try {
      const patch = await generateFixPatch(selectedGuid) as IFixPatchResponse;
      setFixPatch(patch);
    } catch {
      // handled
    } finally {
      setFixPatchLoading(false);
    }
  }, [selectedGuid]);

  /* ── Live fix polling (shared by handleApplyFix and auto-resume) ──── */
  const pollAbortRef = useRef<{ cancelled: boolean }>({ cancelled: false });

  const startFixPolling = useCallback(async (incidentId: string) => {
    let resolved = false;
    for (let i = 0; i < 60; i++) {
      if (pollAbortRef.current.cancelled) break;
      await new Promise((r) => setTimeout(r, 5000));
      try {
        const s = await fetchFixStatus(incidentId) as Record<string, unknown>;
        const st = (s.status as string || "").toUpperCase();

        // Update the live step progress from the backend
        setFixProgress({
          currentStep: (s.current_step as string) || st,
          stepIndex:   (s.step_index as number)  || 1,
          totalSteps:  (s.total_steps as number) || 4,
          stepsDone:   (s.steps_done as string[]) || [],
        });

        if (TERMINAL_STATUSES.has(st)) {
          resolved = true;
          setFixProgress(null);
          if (["AUTO_FIXED", "HUMAN_FIXED", "FIX_VERIFIED", "RETRIED"].includes(st)) {
            setFixState("success");
            setFixResult((s.fix_summary as string) || "Fix applied and deployed.");
          } else {
            setFixState("error");
            setFixResult((s.fix_summary as string) || `Fix failed (${st}).`);
          }
          break;
        }
      } catch {
        // keep polling — transient network errors
      }
    }
    if (!resolved && !pollAbortRef.current.cancelled) {
      setFixProgress(null);
      setFixResult("Still in progress. Refresh later for final status.");
    }
  }, []);

  /* ── Apply fix ──────────────────────────────────────────────────────── */
  const handleApplyFix = useCallback(async () => {
    if (!selectedGuid) return;
    setFixState("loading");
    setFixResult("");
    setFixProgress({ currentStep: "Submitting fix request…", stepIndex: 0, totalSteps: 4, stepsDone: [] });
    pollAbortRef.current.cancelled = false;
    try {
      const proposedFix =
        fixPatch?.summary_structured?.proposed_fix ||
        detail?.ai_recommendation?.proposed_fix ||
        undefined;
      const result = await applyMessageFix(selectedGuid, "user", proposedFix) as Record<string, unknown>;
      const incidentId = (result.incident_id as string) || detail?.incident_id || "";

      const syncStatus = (result.status as string || "").toUpperCase();
      const syncFixApplied = result.fix_applied === true;
      const syncDeploy = result.deploy_success === true;

      if (syncStatus === "AUTO_FIXED" || syncStatus === "HUMAN_FIXED" || (syncFixApplied && syncDeploy)) {
        setFixProgress(null);
        setFixState("success");
        setFixResult((result.summary as string) || "Fix applied and deployed successfully.");
      } else if (syncStatus === "FIX_FAILED") {
        setFixProgress(null);
        setFixState("error");
        setFixResult((result.summary as string) || "Fix failed.");
      } else if (incidentId) {
        await startFixPolling(incidentId);
      } else {
        setFixProgress(null);
        setFixState("success");
        setFixResult((result.message as string) || "Fix queued. Refresh later for status.");
      }

      try {
        const d = await fetchMonitorMessageDetail(selectedGuid) as IMessageDetail;
        setDetail(d);
      } catch { /* ignore */ }
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : "Fix failed";
      // 409 = another session already triggered the fix — connect to live progress
      if (errMsg.includes("409") || errMsg.toLowerCase().includes("already in progress")) {
        const incidentId = detail?.incident_id || "";
        if (incidentId) {
          setFixResult("");
          setFixProgress({ currentStep: "Fix already in progress — connecting…", stepIndex: 0, totalSteps: 4, stepsDone: [] });
          await startFixPolling(incidentId);
        } else {
          setFixState("error");
          setFixProgress(null);
          setFixResult("Fix already in progress by another user. Reload this message to see status.");
        }
      } else {
        setFixState("error");
        setFixProgress(null);
        setFixResult(errMsg);
      }
    }
  }, [selectedGuid, fixPatch, detail, startFixPolling]);

  /* ── Auto-resume fix state from DB on message select ───────────────── */
  useEffect(() => {
    if (!detail) return;
    const st = (detail.status || "").toUpperCase();
    const incidentId = detail.incident_id || "";

    // Only auto-restore if the user hasn't manually triggered anything this session
    if (fixState !== "idle") return;

    if (["FIX_IN_PROGRESS", "RCA_IN_PROGRESS", "FIX_APPLIED_PENDING_VERIFICATION"].includes(st)) {
      const stepLabel =
        st === "RCA_IN_PROGRESS"                   ? "Analyzing root cause…" :
        st === "FIX_APPLIED_PENDING_VERIFICATION"  ? "Verifying fix…" :
                                                     "Fix in progress…";
      setFixState("loading");
      setFixProgress({ currentStep: stepLabel, stepIndex: 0, totalSteps: 4, stepsDone: [] });
      if (incidentId) {
        // Full live polling
        pollAbortRef.current.cancelled = false;
        startFixPolling(incidentId);
      }
      // No incidentId: still show the loading indicator; user can see the state
    } else if (["AUTO_FIXED", "HUMAN_FIXED", "FIX_VERIFIED", "RETRIED"].includes(st)) {
      setFixState("success");
      setFixResult(detail.ai_recommendation?.fix_summary || "Fix applied and deployed.");
    } else if (["FIX_FAILED", "PIPELINE_ERROR"].includes(st)) {
      setFixState("error");
      setFixResult(detail.ai_recommendation?.fix_summary || "Fix failed.");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail]);

  useEffect(() => {
    return () => { pollAbortRef.current.cancelled = true; };
  }, [selectedGuid]);

  /* ════════════════════════════════════════════════════════════════════
     RENDER
     ════════════════════════════════════════════════════════════════════ */
  return (
    <div className={styles.page}>

      {/* ── Main Tab Navigation ── */}
      <div className={styles.mainTabBar}>
        <button
          className={`${styles.mainTab} ${mainTab === "messages" ? styles.mainTabActive : ""}`}
          onClick={() => setMainTab("messages")}
        >
          <SvgIcon name="messages" size={14} style={{ verticalAlign: "middle" }} /> Messages
        </button>
        <button
          className={`${styles.mainTab} ${mainTab === "tickets" ? styles.mainTabActive : ""}`}
          onClick={() => setMainTab("tickets")}
        >
          <SvgIcon name="tickets" size={14} style={{ verticalAlign: "middle" }} /> Tickets ({tickets.length})
        </button>
        <button
          className={`${styles.mainTab} ${mainTab === "approvals" ? styles.mainTabActive : ""}`}
          onClick={() => setMainTab("approvals")}
        >
          <SvgIcon name="approvals" size={14} style={{ verticalAlign: "middle" }} /> Approvals ({approvals.length})
        </button>
        <button
          className={`${styles.mainTab} ${mainTab === "eventmesh" ? styles.mainTabActive : ""}`}
          onClick={() => setMainTab("eventmesh")}
        >
          <SvgIcon name="event-mesh" size={14} style={{ verticalAlign: "middle" }} /> Event Mesh
        </button>
      </div>

      {/* ── Error Type Guide ── */}
      <div className={styles.errorGuide}>
        <button className={styles.errorGuideToggle} onClick={() => setGuideOpen((o) => !o)}>
          <span style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
            <SvgIcon name="document" size={14} />
            Error Type Guide
          </span>
          <span className={styles.errorGuideChevron}><SvgIcon name={guideOpen ? "chevron-down" : "chevron-right"} size={14} /></span>
        </button>
        {guideOpen && (
          <div className={styles.errorGuideBody}>
            {ACTION_GROUPS.map((group) => {
              const types = Object.entries(ERROR_TYPE_META).filter(([, m]) => m.action === group.key);
              return (
                <div key={group.key} className={styles.errorGuideGroup}>
                  <div className={styles.errorGuideGroupHeader} style={{ color: group.color, background: group.bg }}>
                    <span className={styles.errorGuideGroupIcon}><SvgIcon name={group.iconName} size={16} /></span>
                    <span className={styles.errorGuideGroupLabel}>{group.label}</span>
                    <span className={styles.errorGuideGroupDesc}>{group.desc}</span>
                  </div>
                  <div className={styles.errorGuideCards}>
                    {types.map(([type, meta]) => (
                      <div key={type} className={styles.errorGuideCard} style={{ borderLeft: `3px solid ${meta.dot}` }}>
                        <span className={styles.errorGuideCardType}>{type}</span>
                        <span className={styles.errorGuideCardLabel}>{meta.label}</span>
                        <p className={styles.errorGuideCardDesc}>{meta.description}</p>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ══════════════════════════════════════════════════════════════════
          MESSAGES TAB
          ══════════════════════════════════════════════════════════════════ */}
      <div style={{ display: mainTab === "messages" ? "block" : "none" }}>
          {/* ── Summary cards ── */}
          <div className={styles.summaryRow}>
            {SUMMARY_CARD_KEYS.map((k) => {
              const cfg = STATUS_CONFIG[k];
              return (
                <div
                  key={k}
                  className={`${styles.summaryCard} ${filters.statuses.includes(k) ? styles.summaryCardActive : ""}`}
                  style={{ borderTop: `3px solid ${cfg.dot}` }}
                  onClick={() => setFilters((f) => ({
                    ...f,
                    statuses: f.statuses.includes(k) ? f.statuses.filter((s) => s !== k) : [...f.statuses, k],
                  }))}
                  data-tip={CARD_TIPS[k] ?? `Click to filter by ${cfg.label} status`}
                >
                  <span className={styles.summaryCount} style={{ color: cfg.color }}>
                    {counts[k] ?? 0}
                  </span>
                  <span className={styles.summaryLabel} style={{ color: cfg.color }}>{cfg.label}</span>
                </div>
              );
            })}
          </div>

          {/* ── Filters ── */}
          <div className={styles.filterBar}>
            <input
              className={styles.filterInput}
              placeholder="Search messages..."
              value={filters.searchQuery}
              onChange={(e) => setFilters((f) => ({ ...f, searchQuery: e.target.value }))}
              title="Filter messages by iFlow name or message title"
            />
            <input
              className={styles.filterInput}
              placeholder="Message ID / iFlow name..."
              value={filters.idQuery}
              onChange={(e) => setFilters((f) => ({ ...f, idQuery: e.target.value }))}
              title="Filter by message GUID or iFlow name (exact or partial match)"
            />
            <select
              className={styles.filterSelect}
              value=""
              onChange={(e) => {
                const v = e.target.value;
                if (!v) return;
                setFilters((f) => ({ ...f, statuses: f.statuses.includes(v) ? f.statuses.filter((s) => s !== v) : [...f.statuses, v] }));
              }}
              title="Filter messages by their current remediation pipeline status"
            >
              <option value="">Filter by Status...</option>
              {Object.entries(STATUS_CONFIG).map(([k, c]) => <option key={k} value={k}>{c.label}</option>)}
            </select>
            <button
              className={styles.refreshBtn}
              onClick={() => refetch()}
              disabled={isFetching}
              data-tip="Reload messages from SAP CPI (auto-refreshes every 30s)"
            >
              {isFetching ? "↻ Refreshing..." : "Refresh"}
            </button>
            <button className={styles.resetBtn} onClick={() => setFilters(INITIAL_FILTERS)} data-tip="Clear all active filters and show all messages">Reset</button>
          </div>

          {/* Active filter chips */}
          {filters.statuses.length > 0 && (
            <div className={styles.chipRow}>
              {filters.statuses.map((s) => {
                const cfg = STATUS_CONFIG[s];
                return (
                  <span key={s} className={styles.filterChip} style={{ background: cfg.bg, color: cfg.color, borderColor: cfg.dot }}>
                    {cfg.label}
                    <button onClick={() => setFilters((f) => ({ ...f, statuses: f.statuses.filter((x) => x !== s) }))} data-tip="Remove this filter">x</button>
                  </span>
                );
              })}
            </div>
          )}

          {/* ── Two-column layout ── */}
          <div className={styles.columns}>
            {/* Message list */}
            <div className={`${styles.listCol} ${selectedGuid ? styles.listColNarrow : ""}`}>
              {isLoading ? (
                <div className={styles.centered}>
                  <div className={styles.spinner} />
                  <span>Loading messages...</span>
                </div>
              ) : messages.length === 0 ? (
                <div className={styles.centered}>
                  <span>No messages found</span>
                </div>
              ) : (
                <div className={styles.messageList}>
                  {messages.map((msg, i) => {
                    const cfg = STATUS_CONFIG[msg.status?.toUpperCase()] ?? STATUS_CONFIG.FAILED;
                    const isSelected = selectedGuid !== null && selectedGuid === msg.message_guid;
                    return (
                      <div
                        key={msg.message_guid || i}
                        className={`${styles.messageRow} ${isSelected ? styles.messageRowSelected : ""}`}
                        style={{ borderLeft: `3px solid ${isSelected ? cfg.dot : "transparent"}` }}
                        onClick={() => handleSelect(msg)}
                      >
                        <div className={styles.messageMain}>
                          <StatusPill status={msg.status} />
                          <span className={styles.messageName}>
                            {msg.iflow_display || msg.title || "Unknown"}
                          </span>
                        </div>
                        <div className={styles.messageMeta}>
                          <span className={styles.metaItem} data-tip="Message processing duration in SAP CPI">{msg.duration || "--"}</span>
                          <span className={styles.metaItem} data-tip="Last updated or processing start timestamp">{msg.log_start || msg.updatedAt || "--"}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* ── Detail panel ── */}
            {selectedGuid && (
              <div className={styles.detailPanel}>
                {/* Header */}
                <div className={styles.detailHeader}>
                  <div className={styles.detailHeaderLeft}>
                    <h3 className={styles.detailTitle} title={detail?.iflow_display || selectedMsg?.iflow_display || selectedGuid || undefined}>
                      {detail?.iflow_display || selectedMsg?.iflow_display || selectedGuid}
                    </h3>
                    <StatusPill status={detail?.status || selectedMsg?.status || "UNKNOWN"} />
                    {detail?.last_updated && (
                      <span className={styles.detailUpdated}>
                        Last Updated at: {detail.last_updated}
                      </span>
                    )}
                  </div>
                  <div className={styles.detailHeaderRight}>
                    <button
                      className={styles.recheckBtn}
                      onClick={handleAnalyze}
                      disabled={analyzeLoading}
                      data-tip="Re-run AI analysis — useful after applying a fix to get fresh diagnosis and recommendations"
                    >
                      {analyzeLoading ? "Analyzing..." : "Recheck"}
                    </button>
                    <button className={styles.closeBtn} onClick={() => { setSelectedGuid(null); setSelectedMsg(null); setDetail(null); }} data-tip="Close detail panel">x</button>
                  </div>
                </div>

                {/* Tab bar */}
                <div className={styles.tabBar}>
                  {TABS.map((tab) => (
                    <button
                      key={tab.key}
                      className={`${styles.tab} ${activeTab === tab.key ? styles.tabActive : ""}`}
                      onClick={() => setActiveTab(tab.key)}
                      data-tip={tab.tip}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>

                {/* Tab content */}
                {detailLoading ? (
                  <div className={styles.centered}>
                    <div className={styles.spinner} />
                    <span>Loading details...</span>
                  </div>
                ) : detail ? (
                  <div className={styles.detailBody}>
                    {/* ─── Error Details tab ─── */}
                    {activeTab === "error" && (
                      <div className={styles.tabContent}>
                        <div className={styles.errorBox}>
                          <code className={styles.errorCode}>
                            {detail.error_details.error_message || detail.error_details.raw_error_text || "No error details available"}
                          </code>
                        </div>
                        {detail.error_details.error_type && (
                          <div className={styles.detailMeta}>
                            <span className={styles.metaLabel}>Error Type:</span>
                            <span className={styles.metaValue}>{detail.error_details.error_type}</span>
                          </div>
                        )}
                        {detail.error_details.log_start && (
                          <div className={styles.detailMeta}>
                            <span className={styles.metaLabel}>Processing Start:</span>
                            <span className={styles.metaValue}>{detail.error_details.log_start}</span>
                          </div>
                        )}
                        {detail.error_details.log_end && (
                          <div className={styles.detailMeta}>
                            <span className={styles.metaLabel}>Processing End:</span>
                            <span className={styles.metaValue}>{detail.error_details.log_end}</span>
                          </div>
                        )}

                        {/* ── AI Error Explanation ── */}
                        <div className={styles.explainTrigger}>
                          {errorExplain ? (
                            <ErrorExplanationCard exp={errorExplain} />
                          ) : (
                            <button
                              className={styles.explainBtn}
                              onClick={handleExplainError}
                              disabled={errorExplainLoading}
                              data-tip="Ask SAP AI Core to explain this error in plain English with likely causes and recommended actions"
                            >
                              {errorExplainLoading
                                ? <><span className={styles.explainSpinner} /> Analyzing error...</>
                                : <><span className={styles.explainSparkle}><SvgIcon name="rca" size={14} /></span> Explain with AI</>
                              }
                            </button>
                          )}
                          {errorExplainErr && (
                            <div className={styles.explainErrText}>{errorExplainErr}</div>
                          )}
                        </div>
                      </div>
                    )}

                    {/* ─── AI Recommendations & Suggested Fix tab ─── */}
                    {activeTab === "ai" && (
                      <div className={styles.tabContent}>
                        {!detail.ai_recommendation?.diagnosis && !analyzeLoading ? (
                          <div className={styles.noRcaBox}>
                            <p>No AI analysis available yet for this message.</p>
                            <button className={styles.analyzeBtn} onClick={handleAnalyze} disabled={analyzeLoading} data-tip="Trigger SAP AI Core to analyze this message and generate a root cause and fix recommendation">
                              {analyzeLoading ? "Running Analysis..." : "Run AI Analysis"}
                            </button>
                          </div>
                        ) : (
                          <>
                            {/* AI Recommendations header */}
                            <div className={styles.aiHeader}>
                              <span className={styles.aiIcon}><SvgIcon name="rca" size={16} /></span>
                              <span className={styles.aiTitle}>AI Recommendations & Suggested Fix</span>
                            </div>

                            {/* Diagnosis */}
                            {detail.ai_recommendation.diagnosis && (
                              <div className={styles.aiSection}>
                                <div className={styles.aiSectionLabel}>Diagnosis:</div>
                                <div className={styles.aiSectionText}>
                                  <RichText text={detail.ai_recommendation.diagnosis} />
                                </div>
                              </div>
                            )}

                            {/* Field change highlights */}
                            <FieldChangeHighlight changes={detail.ai_recommendation.field_changes} />

                            {/* Proposed fix */}
                            {detail.ai_recommendation.proposed_fix && (
                              <div className={styles.aiSection}>
                                <div className={styles.aiSectionLabel}>Suggested Fix:</div>
                                <div className={styles.aiSectionText}>
                                  <RichText text={detail.ai_recommendation.proposed_fix} />
                                </div>
                              </div>
                            )}

                            {/* Confidence */}
                            {detail.ai_recommendation.confidence > 0 && (
                              <div data-tip="AI confidence in the root cause: ≥90% = High (green), 70–89% = Medium (amber), <70% = Low (red)">
                                <ConfidenceBadge
                                  value={detail.ai_recommendation.confidence}
                                  label={detail.ai_recommendation.confidence_label}
                                />
                              </div>
                            )}

                            {/* Fix Patch section — action bar lives in the sticky footer */}
                            {fixPatch && (
                              <div className={styles.fixPatchSection}>
                                <h4 className={styles.fixPatchTitle}>Steps (Fix Plan)</h4>
                                {fixPatch.summary && (
                                  <div className={styles.fixPatchSummary}>
                                    <strong>Summary:</strong> {fixPatch.summary}
                                  </div>
                                )}
                                <FixPlanSteps steps={fixPatch.steps} />
                              </div>
                            )}

                          </>
                        )}
                      </div>
                    )}

                    {/* ─── Properties tab ─── */}
                    {activeTab === "properties" && (
                      <div className={styles.tabContent}>
                        <h4 className={styles.propGroupTitle}>Message Properties</h4>
                        <div className={styles.propGrid}>
                          {Object.entries(detail.properties.message || {}).map(([k, v]) => v ? (
                            <div key={k} className={styles.propRow}>
                              <span className={styles.propLabel}>{k.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}</span>
                              <span className={styles.propValue}>{String(v)}</span>
                            </div>
                          ) : null)}
                        </div>
                        {detail.properties.adapter && Object.values(detail.properties.adapter).some(Boolean) && (
                          <>
                            <h4 className={styles.propGroupTitle}>Adapter</h4>
                            <div className={styles.propGrid}>
                              {Object.entries(detail.properties.adapter).map(([k, v]) => v ? (
                                <div key={k} className={styles.propRow}>
                                  <span className={styles.propLabel}>{k.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}</span>
                                  <span className={styles.propValue}>{String(v)}</span>
                                </div>
                              ) : null)}
                            </div>
                          </>
                        )}
                        {detail.properties.business_context && Object.values(detail.properties.business_context).some(Boolean) && (
                          <>
                            <h4 className={styles.propGroupTitle}>Business Context</h4>
                            <div className={styles.propGrid}>
                              {Object.entries(detail.properties.business_context).map(([k, v]) => v ? (
                                <div key={k} className={styles.propRow}>
                                  <span className={styles.propLabel}>{k.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}</span>
                                  <span className={styles.propValue}>{String(v)}</span>
                                </div>
                              ) : null)}
                            </div>
                          </>
                        )}
                      </div>
                    )}

                    {/* ─── Artifact tab ─── */}
                    {activeTab === "artifact" && (
                      <div className={styles.tabContent}>
                        <div className={styles.propGrid}>
                          {[
                            ["Name",         detail.artifact.name],
                            ["Artifact ID",  detail.artifact.artifact_id],
                            ["Version",      detail.artifact.version],
                            ["Package",      detail.artifact.package],
                            ["Deployed On",  detail.artifact.deployed_on],
                            ["Deployed By",  detail.artifact.deployed_by],
                            ["Runtime Node", detail.artifact.runtime_node],
                            ["Status",       detail.artifact.status],
                          ].map(([label, val]) => val ? (
                            <div key={label} className={styles.propRow}>
                              <span className={styles.propLabel}>{label}</span>
                              <span className={styles.propValue}>{String(val)}</span>
                            </div>
                          ) : null)}
                        </div>
                      </div>
                    )}

                    {/* ─── Attachments tab ─── */}
                    {activeTab === "attachments" && (
                      <div className={styles.tabContent}>
                        {detail.attachments?.length > 0 ? (
                          <div>Attachments available: {detail.attachments.length}</div>
                        ) : (
                          <div className={styles.emptyTab}>No attachments available for this message.</div>
                        )}
                      </div>
                    )}

                    {/* ─── History tab ─── */}
                    {activeTab === "history" && (
                      <div className={styles.tabContent}>
                        {detail.history?.length > 0 ? (
                          <Timeline entries={detail.history} />
                        ) : (
                          <div className={styles.emptyTab}>No history entries yet.</div>
                        )}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className={styles.centered}>
                    <span>Could not load message details.</span>
                  </div>
                )}

                {/* ── Sticky fix-action footer ───────────────────────────────
                     Sibling of detailBody — always visible regardless of scroll */}
                {detail && activeTab === "ai" && detail.ai_recommendation?.diagnosis && (
                  <div className={styles.fixFooter}>
                    {fixState === "loading" && fixProgress && (
                      <div className={styles.fixProgressInline}>
                        <PipelineStageRail
                          stepIndex={fixProgress.stepIndex}
                          totalSteps={fixProgress.totalSteps}
                        />
                        <div className={styles.fixProgressCurrentStep}>
                          <span className={styles.fixProgressSpinner} />
                          <span>{fixProgress.currentStep}</span>
                        </div>
                      </div>
                    )}
                    {fixResult && (
                      <div className={`${styles.fixResultBanner} ${styles[`fixResultBanner_${fixState}`] || ""}`}>
                        {fixResult}
                      </div>
                    )}
                    <div className={styles.fixFooterActions}>
                      {fixPatch ? (
                        <button
                          className={`${styles.applyFixBtn} ${styles[`applyFixBtn_${fixState}`] || ""}`}
                          onClick={handleApplyFix}
                          disabled={fixState === "loading" || fixState === "success"}
                          data-tip="Execute the fix pipeline: get-iflow → validate → update-iflow → deploy-iflow via the SAP IS API"
                        >
                          {fixState === "idle"    && <><SvgIcon name="lightning" size={13} style={{ marginRight: "0.35rem", verticalAlign: "middle" }} />Apply Fix</>}
                          {fixState === "loading" && <><span className={styles.btnSpinner} /> Applying...</>}
                          {fixState === "success" && "✓ Fix Applied"}
                          {fixState === "error"   && "↺ Retry Fix"}
                        </button>
                      ) : (
                        detail.ai_recommendation.can_generate_fix && (
                          <button
                            className={styles.generateFixBtn}
                            onClick={handleGenerateFixPatch}
                            disabled={fixPatchLoading}
                            data-tip="Ask the AI to generate a detailed step-by-step fix plan with XML change instructions"
                          >
                            {fixPatchLoading
                              ? <><span className={styles.btnSpinner} /> Generating...</>
                              : <><SvgIcon name="wrench" size={14} style={{ verticalAlign: "middle", marginRight: "0.3rem" }} />Generate Fix Patch</>}
                          </button>
                        )
                      )}
                      {fixPatch && fixState === "idle" && (
                        <span className={styles.fixFooterHint}>
                          Fix plan ready — click Apply Fix to execute the automated patch &amp; deploy.
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
      </div>

      {/* ══════════════════════════════════════════════════════════════════
          TICKETS TAB
          ══════════════════════════════════════════════════════════════════ */}
      {mainTab === "tickets" && (
        <div className={styles.ticketsContainer}>
          <div className={styles.ticketsHeader}>
            <div>
              <h2>Escalation Tickets</h2>
              <p className={styles.tabDescription}>Incidents that could not be auto-remediated are escalated here as tickets for manual review and resolution.</p>
            </div>
            <button onClick={() => refetchTickets()} disabled={ticketsLoading}>
              {ticketsLoading ? "Loading..." : "Refresh"}
            </button>
          </div>

          {/* KPI Cards for Tickets */}
          {!ticketsLoading && tickets.length > 0 && (
            <div className={styles.kpiRow}>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #dc2626" }}>
                <span className={styles.kpiValue} style={{ color: "#dc2626" }}>
                  {tickets.filter(t => ["CRITICAL", "HIGH"].includes((t.priority || "").toUpperCase())).length}
                </span>
                <span className={styles.kpiLabel}>High Priority</span>
              </div>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #d97706" }}>
                <span className={styles.kpiValue} style={{ color: "#d97706" }}>
                  {tickets.filter(t => (t.status || "").toUpperCase() === "OPEN").length}
                </span>
                <span className={styles.kpiLabel}>Open Tickets</span>
              </div>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #2563eb" }}>
                <span className={styles.kpiValue} style={{ color: "#2563eb" }}>
                  {tickets.filter(t => (t.status || "").toUpperCase() === "IN_PROGRESS").length}
                </span>
                <span className={styles.kpiLabel}>In Progress</span>
              </div>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #16a34a" }}>
                <span className={styles.kpiValue} style={{ color: "#16a34a" }}>
                  {tickets.filter(t => (t.status || "").toUpperCase() === "RESOLVED").length}
                </span>
                <span className={styles.kpiLabel}>Resolved</span>
              </div>
            </div>
          )}
          
          {ticketActionError && (
            <div className={styles.approvalErrorBanner}>
              {ticketActionError}
              <button onClick={() => setTicketActionError(null)} aria-label="Dismiss">✕</button>
            </div>
          )}

          {ticketsLoading ? (
            <div className={styles.centered}>
              <div className={styles.spinner} />
              <span>Loading tickets...</span>
            </div>
          ) : tickets.length === 0 ? (
            <div className={styles.emptyState}>
              <span><SvgIcon name="tickets" size={32} /></span>
              <p>No tickets found</p>
            </div>
          ) : (
            <div className={styles.ticketsList}>
              {tickets.map((ticket) => (
                <div key={ticket.ticket_id} className={styles.ticketCard}>
                  <div className={styles.ticketHeader}>
                    <div>
                      <h3>{ticket.title}</h3>
                      <span className={styles.ticketId}>#{ticket.ticket_id}</span>
                    </div>
                    <div className={styles.ticketBadges}>
                      <span className={`${styles.badge} ${styles[`priority_${ticket.priority.toLowerCase()}`]}`}>
                        {ticket.priority}
                      </span>
                      <span className={`${styles.badge} ${styles[`status_${ticket.status.toLowerCase().replace(/\s+/g, '_')}`]}`}>
                        {ticket.status}
                      </span>
                    </div>
                  </div>
                  
                  <div className={styles.ticketBody}>
                    <div className={styles.ticketMeta}>
                      <span><strong>iFlow:</strong> {ticket.iflow_id}</span>
                      <span><strong>Error Type:</strong> {ticket.error_type}</span>
                      {ticket.assigned_to && <span><strong>Assigned To:</strong> {ticket.assigned_to}</span>}
                    </div>

                    {(() => {
                      const parsed = parseTicketDescription(ticket.description);
                      const hasStructure = parsed.error || parsed.root_cause || parsed.proposed_fix;
                      if (!hasStructure) return <p className={styles.ticketDescription}>{ticket.description}</p>;
                      return (
                        <div className={styles.ticketSections}>
                          {parsed.error && (
                            <div className={styles.approvalSection}>
                              <strong>Error Message</strong>
                              <p className={styles.errorText}>{parsed.error}</p>
                            </div>
                          )}
                          {parsed.root_cause && (
                            <div className={styles.approvalSection}>
                              <strong>Root Cause</strong>
                              <p>{parsed.root_cause}</p>
                            </div>
                          )}
                          {parsed.proposed_fix && (
                            <div className={styles.approvalSection}>
                              <strong>Proposed Fix</strong>
                              <p className={styles.fixText}>{parsed.proposed_fix}</p>
                            </div>
                          )}
                          {(parsed.occurrence_count || parsed.rca_confidence) && (
                            <div className={styles.ticketSectionMeta}>
                              {parsed.occurrence_count && (
                                <span><strong>Occurrences:</strong> {parsed.occurrence_count}</span>
                              )}
                              {parsed.rca_confidence && !isNaN(parseFloat(parsed.rca_confidence)) && (
                                <span><strong>RCA Confidence:</strong> {(parseFloat(parsed.rca_confidence) * 100).toFixed(0)}%</span>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })()}

                    {ticket.resolution_notes && (
                      <div className={styles.ticketResolution}>
                        <strong>Resolution:</strong>
                        <p>{ticket.resolution_notes}</p>
                      </div>
                    )}
                  </div>
                  
                  <div className={styles.ticketFooter}>
                    <span>Created: {new Date(ticket.created_at).toLocaleString()}</span>
                    <span>Updated: {new Date(ticket.updated_at).toLocaleString()}</span>
                    <div className={styles.approvalActions} style={{ marginLeft: "auto" }}>
                      {(ticket.status || "").toUpperCase() !== "RESOLVED" ? (
                        <button
                          className={`${styles.btn} ${styles.btnApprove}`}
                          disabled={resolvingTicketId === ticket.ticket_id}
                          onClick={() => handleMarkResolved(ticket.ticket_id, ticket.status)}
                          title="Mark this ticket as resolved"
                        >
                          {resolvingTicketId === ticket.ticket_id ? "Resolving…" : "✓ Mark Resolved"}
                        </button>
                      ) : (
                        <span style={{ color: "#16a34a", fontSize: "0.82rem", fontWeight: 600 }}>✓ Resolved</span>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ══════════════════════════════════════════════════════════════════
          APPROVALS TAB
          ══════════════════════════════════════════════════════════════════ */}
      {/* ══════════════════════════════════════════════════════════════════
          EVENT MESH TAB
          ══════════════════════════════════════════════════════════════════ */}
      {mainTab === "eventmesh" && <EventMeshFlow />}

      {mainTab === "approvals" && (
        <div className={styles.approvalsContainer}>
          <div className={styles.approvalsHeader}>
            <div>
              <h2>Pending Approvals</h2>
              <p className={styles.tabDescription}>AI-proposed fixes awaiting human sign-off before being deployed to SAP CPI. Review and approve or reject each one below.</p>
            </div>
            <div className={styles.approvalsHeaderActions}>
              <button
                className={`${styles.btn} ${styles.btnApprove}`}
                onClick={handleApproveAll}
                disabled={!!bulkActionLoading || approvalsLoading || approvals.filter(a => a.status === "AWAITING_APPROVAL").length === 0}
                title="Approve all awaiting incidents at once"
              >
                {bulkActionLoading === "approving" ? "Approving..." : "✓ Approve All"}
              </button>
              <button
                className={`${styles.btn} ${styles.btnReject}`}
                onClick={handleRejectAll}
                disabled={!!bulkActionLoading || approvalsLoading || approvals.filter(a => a.status === "AWAITING_APPROVAL").length === 0}
                title="Reject all awaiting incidents at once"
              >
                {bulkActionLoading === "rejecting" ? "Rejecting..." : "✗ Reject All"}
              </button>
              <button onClick={() => refetchApprovals()} disabled={approvalsLoading || !!bulkActionLoading}>
                {approvalsLoading ? "Loading..." : "Refresh"}
              </button>
            </div>
          </div>

          {approvalActionError && (
            <div className={styles.approvalErrorBanner}>
              {approvalActionError}
              <button onClick={() => setApprovalActionError(null)} aria-label="Dismiss">✕</button>
            </div>
          )}

          {/* KPI Cards for Approvals */}
          {!approvalsLoading && approvals.length > 0 && (
            <div className={styles.kpiRow}>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #7c3aed" }}>
                <span className={styles.kpiValue} style={{ color: "#7c3aed" }}>
                  {approvals.filter(a => a.status === "AWAITING_APPROVAL").length}
                </span>
                <span className={styles.kpiLabel}>Awaiting Approval</span>
              </div>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #16a34a" }}>
                <span className={styles.kpiValue} style={{ color: "#16a34a" }}>
                  {approvals.filter(a => a.rca_confidence >= 0.9).length}
                </span>
                <span className={styles.kpiLabel}>High Confidence (≥90%)</span>
              </div>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #d97706" }}>
                <span className={styles.kpiValue} style={{ color: "#d97706" }}>
                  {approvals.filter(a => a.rca_confidence >= 0.7 && a.rca_confidence < 0.9).length}
                </span>
                <span className={styles.kpiLabel}>Medium Confidence (70-89%)</span>
              </div>
              <div className={styles.kpiCard} style={{ borderTop: "3px solid #dc2626" }}>
                <span className={styles.kpiValue} style={{ color: "#dc2626" }}>
                  {approvals.filter(a => a.rca_confidence < 0.7).length}
                </span>
                <span className={styles.kpiLabel}>Low Confidence (&lt;70%)</span>
              </div>
            </div>
          )}
          
          {approvalsLoading ? (
            <div className={styles.centered}>
              <div className={styles.spinner} />
              <span>Loading approvals...</span>
            </div>
          ) : approvals.length === 0 ? (
            <div className={styles.emptyState}>
              <span><SvgIcon name="check-circle" size={32} /></span>
              <p>No pending approvals</p>
            </div>
          ) : (
            <div className={styles.approvalsList}>
              {approvals.map((approval) => (
                <div key={approval.incident_id} className={styles.approvalCard}>
                  <div className={styles.approvalHeader}>
                    <div>
                      <h3>{approval.iflow_id}</h3>
                      <span className={styles.approvalId}>Incident: {approval.incident_id}</span>
                    </div>
                    <StatusPill status={approval.status} />
                  </div>
                  
                  <div className={styles.approvalBody}>
                    <div className={styles.approvalSection}>
                      <strong>Error Type:</strong>
                      <span>{approval.error_type}</span>
                    </div>
                    
                    <div className={styles.approvalSection}>
                      <strong>Error Message:</strong>
                      <p className={styles.errorText}>{approval.error_message}</p>
                    </div>
                    
                    <div className={styles.approvalSection}>
                      <strong>Root Cause:</strong>
                      <p>{approval.root_cause}</p>
                    </div>
                    
                    <div className={styles.approvalSection}>
                      <strong>Proposed Fix:</strong>
                      <p className={styles.fixText}>{approval.proposed_fix}</p>
                    </div>
                    
                    <div className={styles.approvalMeta}>
                      <span><strong>Confidence:</strong> {(approval.rca_confidence * 100).toFixed(0)}%</span>
                      <span><strong>Created:</strong> {new Date(approval.created_at).toLocaleString()}</span>
                      <span><strong>Pending Since:</strong> {new Date(approval.pending_since).toLocaleString()}</span>
                    </div>
                  </div>
                  
                  {approval.status === "AWAITING_APPROVAL" && (
                    <div className={styles.approvalActions}>
                      <button
                        className={`${styles.btn} ${styles.btnApprove}`}
                        onClick={() => handleApprove(approval.incident_id)}
                      >
                        ✓ Approve
                      </button>
                      <button
                        className={`${styles.btn} ${styles.btnReject}`}
                        onClick={() => handleReject(approval.incident_id)}
                      >
                        ✗ Reject
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}