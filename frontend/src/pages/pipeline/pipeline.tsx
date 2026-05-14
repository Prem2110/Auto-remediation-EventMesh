import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchPipelineStatus,
  startPipeline,
  stopPipeline,
  fetchPipelineTrace,
  fetchAutoFixStatus,
  toggleAutoFix,
} from "../../services/api.ts";
import SvgIcon, { type IconName } from "../../components/icons/SvgIcon.tsx";
import _styles from "./pipeline.module.css";
// Vite 8 types CSS module values as `unknown`; cast so className={styles.x} compiles.
const styles = _styles as Record<string, string>;

// ── Agent metadata (5 specialist agents) ─────────────────────────────────────
const SPECIALIST_AGENTS: Record<string, { icon: IconName; label: string; desc: string; tools: string; gradient: string; accent: string }> = {
  observer:   { icon:"eye",          label:"Observer",   desc:"Monitors SAP CPI for failed messages, creates incidents",              tools:"3 local tools",    gradient:"linear-gradient(135deg,#eef2ff 0%,#dbeafe 100%)", accent:"#3b82f6" },
  classifier: { icon:"tag",          label:"Classifier", desc:"Classifies error type + confidence — rule-based, zero LLM cost",      tools:"3 local + 1 MCP",  gradient:"linear-gradient(135deg,#faf5ff 0%,#ede9fe 100%)", accent:"#8b5cf6" },
  rca:        { icon:"rca",          label:"RCA",        desc:"Root cause analysis: vector store + message logs + iFlow inspection", tools:"3 local + 2-3 MCP", gradient:"linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%)", accent:"#16a34a" },
  fixer:      { icon:"wrench",       label:"Fixer",      desc:"Get → validate → update → deploy iFlow with XML safety checks",      tools:"2 local + 6-8 MCP", gradient:"linear-gradient(135deg,#fff7ed 0%,#fed7aa 100%)", accent:"#ea580c" },
  verifier:   { icon:"check-circle", label:"Verifier",   desc:"Test fixed iFlow + replay failed messages for end-to-end verification", tools:"1 local + 3-4 MCP", gradient:"linear-gradient(135deg,#f0fdf4 0%,#bbf7d0 100%)", accent:"#15803d" },
};

const SPECIALIST_ORDER = ["observer", "classifier", "rca", "fixer", "verifier"];

// ── Types ─────────────────────────────────────────────────────────────────────
interface TraceIncident {
  incident_id: string;
  message_guid: string;
  iflow_name: string;
  iflow_id?: string;
  error_type: string;
  status: string;
  created_at: string;
  updated_at: string;
  root_cause?: string;
  proposed_fix?: string;
}

const TRACE_PAGE_SIZE = 15;

const IN_PROGRESS_STATUSES = new Set([
  "CLASSIFIED", "OBSERVED", "RCA_IN_PROGRESS", "RCA_COMPLETE", "FIX_IN_PROGRESS",
]);

// ── Pipeline stage popup ──────────────────────────────────────────────────────
const STAGE_NAMES = ["Observer", "RCA", "Fixer", "Verifier"];
const STAGE_DESCS = [
  "Enrich metadata from SAP OData",
  "Root cause analysis via AI",
  "Apply iFlow fix & deploy",
  "Verify fix resolved the error",
];

type StageState = "done" | "active" | "pending" | "failed";

function deriveStageStates(status: string): StageState[] {
  // [doneUpToIdx, activeIdx, failedIdx] — -1 means none
  const cfg: Record<string, [number, number, number]> = {
    "CLASSIFIED":         [-1,  0, -1],
    "OBSERVED":           [ 0,  1, -1],
    "RCA_IN_PROGRESS":    [ 0,  1, -1],
    "RCA_COMPLETE":       [ 1,  2, -1],
    "FIX_IN_PROGRESS":    [ 1,  2, -1],
    "AWAITING_APPROVAL":  [ 1,  2, -1],
    "TICKET_CREATED":     [ 2, -1, -1],
    "RETRIED":            [ 2, -1, -1],
    "FIX_VERIFIED":       [ 3, -1, -1],
    "REMEDIATED":         [ 3, -1, -1],
    "OBS_FAILED":         [-1, -1,  0],
    "RCA_FAILED":         [ 0, -1,  1],
    "RCA_INCONCLUSIVE":   [ 0, -1,  1],
    "FIX_FAILED":         [ 1, -1,  2],
    "FIX_FAILED_DEPLOY":  [ 1, -1,  2],
    "FIX_FAILED_UPDATE":  [ 1, -1,  2],
    "FIX_FAILED_RUNTIME": [ 2, -1,  3],
  };
  const [doneUpTo, activeIdx, failedIdx] = cfg[status] ?? [-1, 0, -1];
  return [0, 1, 2, 3].map((i): StageState => {
    if (i === failedIdx)  return "failed";
    if (i <= doneUpTo)    return "done";
    if (i === activeIdx)  return "active";
    return "pending";
  });
}

export default function Pipeline() {
  const qc = useQueryClient();
  const [toggling, setToggling] = useState(false);
  const [togglingAutoFix, setTogglingAutoFix] = useState(false);
  const [tracePage, setTracePage] = useState(1);
  const [modalIncident, setModalIncident] = useState<TraceIncident | null>(null);

  // ── Queries ──────────────────────────────────────────────────────────────
  const { data: pipelineData } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn: fetchPipelineStatus,
    refetchInterval: 15_000,
  });

  const { data: autoFixData, refetch: refetchAutoFix } = useQuery({
    queryKey: ["auto-fix-status"],
    queryFn: fetchAutoFixStatus,
    staleTime: 0,
  });

  const { data: traceData } = useQuery({
    queryKey: ["pipeline-trace", tracePage],
    queryFn: () => fetchPipelineTrace(tracePage, TRACE_PAGE_SIZE),
    refetchInterval: 15_000,
  });

  // ── Pipeline control ─────────────────────────────────────────────────────
  async function handleToggle() {
    setToggling(true);
    try {
      if (pipelineData?.pipeline_running) {
        await stopPipeline();
      } else {
        await startPipeline();
      }
      await qc.invalidateQueries({ queryKey: ["pipeline-status"] });
    } finally {
      setToggling(false);
    }
  }

  async function handleToggleAutoFix() {
    setTogglingAutoFix(true);
    try {
      await toggleAutoFix();
      await refetchAutoFix();
    } finally {
      setTogglingAutoFix(false);
    }
  }

  const autoFixOn = autoFixData?.auto_fix_enabled ?? true;
  const running = pipelineData?.pipeline_running ?? false;
  const agentStatuses = pipelineData?.agents ?? {};
  const incidents: TraceIncident[]  = (traceData?.incidents ?? []) as TraceIncident[];
  const traceTotal  = (traceData?.total ?? 0) as number;
  const tracePages  = Math.max(1, Math.ceil(traceTotal / TRACE_PAGE_SIZE));
  const AGENT_META = SPECIALIST_AGENTS;
  const STAGE_ORDER = SPECIALIST_ORDER;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>

      {/* ── Header ── */}
      <div className={styles.header}>
        <div>
          <h1 className={styles.pageTitle}>Auto-Remediation Pipeline</h1>
          <p className={styles.pageSubtitle}>
            5 specialist agents · Per-agent tools
          </p>
        </div>
        <div className={styles.headerRight}>
          <div
            className={`${styles.autoFixToggle} ${autoFixOn ? styles.autoFixToggleOn : styles.autoFixToggleOff} tooltip-below`}
            onClick={togglingAutoFix ? undefined : handleToggleAutoFix}
            data-tip={autoFixOn
              ? "Auto-Fix ON — AI applies fixes automatically when confidence is high. Click to require manual approval for all fixes."
              : "Auto-Fix OFF — All fixes require manual approval via Apply Fix. Click to re-enable autonomous fixing."}
          >
            <span className={styles.autoFixTrack}>
              <span className={styles.autoFixThumb} />
            </span>
            <span className={styles.autoFixLabel}>
              {togglingAutoFix ? "…" : autoFixOn ? "Auto-Fix" : "Manual"}
            </span>
            {autoFixOn && !togglingAutoFix && <span className={styles.autoFixActiveDot} />}
          </div>
          <button
            className={`${styles.toggleBtn} ${running ? styles.toggleBtnStop : styles.toggleBtnStart} tooltip-below`}
            onClick={handleToggle}
            disabled={toggling}
            data-tip={running ? "Stop the pipeline — in-flight incidents will complete before halting" : "Start the autonomous 5-agent remediation pipeline"}
          >
            {toggling ? "…" : running ? "Stop Pipeline" : "Start Pipeline"}
          </button>
        </div>
      </div>

      {/* ── Agent flow ── */}
      <div className={styles.sectionLabelGroup}>
        <div className={styles.sectionLabel}>Agent Flow</div>
        <div className={styles.sectionSubLabel}>Each agent gets only the tools it needs</div>
      </div>
      <div className={styles.agentFlow}>
        {STAGE_ORDER.map((key, i) => {
          const meta = AGENT_META[key];
          if (!meta) return null;
          const rawStatus = agentStatuses[key] ?? "unknown";
          const isRunning = rawStatus === "running";
          return (
            <div key={key} className={styles.flowItem}>
              <div
                className={`${styles.agentCard} ${isRunning ? styles.agentCardActive : ""}`}
                style={{ borderColor: isRunning ? meta.accent : "#e5e7eb" }}
              >
                <div className={styles.agentBanner} style={{ background: meta.gradient }}>
                  <span className={styles.agentEmoji}><SvgIcon name={meta.icon} size={22} style={{ color: meta.accent }} /></span>
                  <span className={`${styles.agentDot} ${isRunning ? styles.dotRunning : styles.dotIdle}`} />
                </div>
                <div className={styles.agentInfo}>
                  <span className={styles.agentLabel}>{meta.label}</span>
                  <span className={styles.agentStatus} style={{ color: isRunning ? meta.accent : "#22c55e" }}>
                    {isRunning ? "Running" : "Running"}
                  </span>
                  <span className={styles.agentDesc}>{meta.desc}</span>
                </div>
              </div>
              {i < STAGE_ORDER.length - 1 && (
                <span className={`${styles.flowArrow} ${isRunning ? styles.flowArrowActive : ""}`}>→</span>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Pipeline trace ── */}
      <div className={styles.traceTableHeader}>
        <div>
          <div className={styles.sectionLabel}>Pipeline Trace</div>
        </div>
        <div className={styles.traceSearch}>
          <span className={styles.traceSearchIcon}>🔍</span>
          <input className={styles.traceSearchInput} placeholder="search integration flow" />
        </div>
      </div>
      <div className={styles.traceTable}>
        {incidents.length === 0 ? (
          <div className={styles.traceEmpty}>No incidents yet. Start the pipeline to begin processing.</div>
        ) : (
          <table className={styles.table}>
            <thead>
              <tr>
                <th title="SAP Integration Suite integration flow that encountered the error">iFlow</th>
                <th title="Classified error category (e.g. MAPPING_ERROR, CONNECTION_ERROR)">Error Type</th>
                <th title="Current auto-remediation pipeline stage for this incident">Status</th>
                <th title="AI-generated summary of the root cause">Root Cause</th>
                <th title="When this incident was first detected by the pipeline">Created</th>
              </tr>
            </thead>
            <tbody>
              {incidents.map((inc) => (
                <tr key={inc.incident_id} className={styles.trClickable} onClick={() => setModalIncident(inc)}>
                  <td className={styles.tdIflow} title={inc.iflow_name || inc.iflow_id || inc.message_guid || ""}>
                    {inc.iflow_name || (
                      <span className={styles.tdIflowUnknown}>
                        {inc.message_guid ? "Resolving…" : "—"}
                      </span>
                    )}
                  </td>
                  <td><span className={styles.errorTypeBadge}>{inc.error_type}</span></td>
                  <td>
                    <span className={`${styles.statusChip} ${styles[`chip-${inc.status?.toLowerCase().replace(/\s+/g,"_")}`]}`}>
                      {inc.status}
                      {IN_PROGRESS_STATUSES.has(inc.status) && (
                        <span className={styles.dotLoader}>
                          <span /><span /><span />
                        </span>
                      )}
                    </span>
                  </td>
                  <td className={styles.tdRca}>{inc.root_cause ?? "—"}</td>
                  <td className={styles.tdDate}>{new Date(inc.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ── pagination ── */}
      {traceTotal > TRACE_PAGE_SIZE && (
        <div className={styles.pagination}>
          <span className={styles.paginationInfo}>
            {((tracePage - 1) * TRACE_PAGE_SIZE) + 1}–{Math.min(tracePage * TRACE_PAGE_SIZE, traceTotal)} of {traceTotal} incidents
          </span>
          <div className={styles.paginationControls}>
            <button className={styles.pageBtn} onClick={() => setTracePage(1)} disabled={tracePage === 1}>«</button>
            <button className={styles.pageBtn} onClick={() => setTracePage((p) => Math.max(1, p - 1))} disabled={tracePage === 1}>‹ Prev</button>
            {Array.from({ length: tracePages }, (_, i) => i + 1)
              .filter((p) => p === 1 || p === tracePages || Math.abs(p - tracePage) <= 1)
              .reduce<(number | "…")[]>((acc, p, i, arr) => {
                if (i > 0 && (p as number) - (arr[i - 1] as number) > 1) acc.push("…");
                acc.push(p);
                return acc;
              }, [])
              .map((p, i) =>
                p === "…" ? (
                  <span key={`ellipsis-${i}`} className={styles.pageDots}>…</span>
                ) : (
                  <button
                    key={p}
                    className={`${styles.pageBtn} ${tracePage === p ? styles.pageBtnActive : ""}`}
                    onClick={() => setTracePage(p as number)}
                  >{p}</button>
                )
              )}
            <button className={styles.pageBtn} onClick={() => setTracePage((p) => Math.min(tracePages, p + 1))} disabled={tracePage === tracePages}>Next ›</button>
            <button className={styles.pageBtn} onClick={() => setTracePage(tracePages)} disabled={tracePage === tracePages}>»</button>
          </div>
        </div>
      )}

      {/* ── Stage progress popup ── */}
      {modalIncident && (
      <div className={styles.modalOverlay} onClick={() => setModalIncident(null)}>
        <div className={styles.modal} onClick={(e) => e.stopPropagation()}>

          {/* header */}
          <div className={styles.modalHeader}>
            <div className={styles.modalHeaderLeft}>
              <div className={styles.modalIflow}>{modalIncident.iflow_name || modalIncident.message_guid || "—"}</div>
              <div className={styles.modalMeta}>
                <span className={styles.errorTypeBadge}>{modalIncident.error_type}</span>
                <span className={`${styles.statusChip} ${styles[`chip-${modalIncident.status?.toLowerCase().replace(/\s+/g,"_")}`]}`}>
                  {modalIncident.status}
                  {IN_PROGRESS_STATUSES.has(modalIncident.status) && (
                    <span className={styles.dotLoader}><span /><span /><span /></span>
                  )}
                </span>
              </div>
            </div>
            <button className={styles.modalClose} onClick={() => setModalIncident(null)}>×</button>
          </div>

          {/* stage list */}
          <div className={styles.modalBody}>
            <div className={styles.modalSectionTitle}>Pipeline Progress</div>
            <div className={styles.stageList}>
              {deriveStageStates(modalIncident.status).map((state, i) => (
                <div key={i} className={`${styles.stageRow} ${styles[`stageRow_${state}`]}`}>
                  <span className={`${styles.stageBullet} ${styles[`stageBullet_${state}`]}`} />
                  <div className={styles.stageInfo}>
                    <span className={styles.stageName}>{STAGE_NAMES[i]}</span>
                    <span className={styles.stageDesc}>{STAGE_DESCS[i]}</span>
                  </div>
                  <div className={styles.stageRight}>
                    {state === "active"  && <><span className={styles.stageActiveText}>Running</span><span className={styles.dotLoader}><span /><span /><span /></span></>}
                    {state === "done"    && <span className={styles.stageDoneText}>✓ Done</span>}
                    {state === "failed"  && <span className={styles.stageFailedText}>✗ Failed</span>}
                    {state === "pending" && <span className={styles.stagePendingText}>Waiting</span>}
                  </div>
                </div>
              ))}
            </div>

            {/* root cause */}
            {modalIncident.root_cause && (
              <div className={styles.modalSection}>
                <div className={styles.modalSectionTitle}>Root Cause</div>
                <div className={styles.modalText}>{modalIncident.root_cause}</div>
              </div>
            )}

            {/* proposed fix */}
            {modalIncident.proposed_fix && (
              <div className={styles.modalSection}>
                <div className={styles.modalSectionTitle}>Proposed Fix</div>
                <div className={styles.modalText}>{modalIncident.proposed_fix}</div>
              </div>
            )}
          </div>

        </div>
      </div>
    )}
    </div>
  );
}
