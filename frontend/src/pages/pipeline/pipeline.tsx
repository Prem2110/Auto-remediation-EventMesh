import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchPipelineStatus,
  fetchToolDistribution,
  startPipeline,
  stopPipeline,
  fetchPipelineTrace,
} from "../../services/api.ts";
import _styles from "./pipeline.module.css";
// Vite 8 types CSS module values as `unknown`; cast so className={styles.x} compiles.
const styles = _styles as Record<string, string>;

// ── Agent metadata (5 specialist agents) ─────────────────────────────────────
const SPECIALIST_AGENTS: Record<string, { emoji: string; label: string; desc: string; tools: string; gradient: string; accent: string }> = {
  observer:   { emoji:"👁️",  label:"Observer",   desc:"Monitors SAP CPI for failed messages, creates incidents",              tools:"3 local tools", gradient:"linear-gradient(135deg,#0f172a 0%,#1e40af 100%)", accent:"#60a5fa" },
  classifier: { emoji:"🏷️",  label:"Classifier", desc:"Classifies error type + confidence — rule-based, zero LLM cost",      tools:"3 local + 1 MCP", gradient:"linear-gradient(135deg,#1e1b4b 0%,#7c3aed 100%)", accent:"#a78bfa" },
  rca:        { emoji:"🧠",  label:"RCA",        desc:"Root cause analysis: vector store + message logs + iFlow inspection", tools:"3 local + 2-3 MCP", gradient:"linear-gradient(135deg,#064e3b 0%,#059669 100%)", accent:"#34d399" },
  fixer:      { emoji:"🔧",  label:"Fixer",      desc:"Get → validate → update → deploy iFlow with XML safety checks",      tools:"2 local + 6-8 MCP", gradient:"linear-gradient(135deg,#312e81 0%,#6d28d9 100%)", accent:"#c084fc" },
  verifier:   { emoji:"✅",  label:"Verifier",   desc:"Test fixed iFlow + replay failed messages for end-to-end verification", tools:"1 local + 3-4 MCP", gradient:"linear-gradient(135deg,#4c0519 0%,#be123c 100%)", accent:"#fb7185" },
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

export default function Pipeline() {
  const qc = useQueryClient();
  const [toggling, setToggling] = useState(false);

  // ── Queries ──────────────────────────────────────────────────────────────
  const { data: pipelineData } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn: fetchPipelineStatus,
    refetchInterval: 15_000,
  });

  // Tools never change after startup — fetch once, cache for 10 minutes
  const { data: toolDist } = useQuery({
    queryKey: ["tool-distribution"],
    queryFn: fetchToolDistribution,
    staleTime: 10 * 60 * 1000,
    refetchInterval: false,
  });

  const { data: traceData } = useQuery({
    queryKey: ["pipeline-trace"],
    queryFn: () => fetchPipelineTrace(30),
    refetchInterval: 15_000,   // was 6s
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


  const running = pipelineData?.pipeline_running ?? false;
  const agentStatuses = pipelineData?.agents ?? {};
  const incidents: TraceIncident[] = (traceData?.incidents ?? []) as TraceIncident[];
  const toolDistribution = toolDist ?? pipelineData?.tool_distribution;

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
            5 specialist agents · Per-agent tools · SAP AI Core
          </p>
        </div>
        <div className={styles.headerRight}>
          <span
            className={`${styles.statusBadge} ${running ? styles.statusBadgeOn : styles.statusBadgeOff}`}
            data-tip={running ? "Pipeline is actively monitoring SAP CPI for failures" : "Pipeline is stopped — no new incidents will be detected"}
          >
            {running ? "● Running" : "○ Stopped"}
          </span>
          {running && (
            <span className={styles.aemBadge} data-tip="5-agent specialist mode — each agent has a curated, minimal tool set for safety and efficiency">Specialist</span>
          )}
          <button
            className={`${styles.toggleBtn} ${running ? styles.toggleBtnStop : styles.toggleBtnStart}`}
            onClick={handleToggle}
            disabled={toggling}
            data-tip={running ? "Stop the pipeline — in-flight incidents will complete before halting" : "Start the autonomous 5-agent remediation pipeline"}
          >
            {toggling ? "…" : running ? "Stop Pipeline" : "Start Pipeline"}
          </button>
        </div>
      </div>

      {/* ── Agent flow ── */}
      <div className={styles.sectionLabel} data-tip="Agents run in sequence: Observer detects → Classifier categorizes → RCA analyzes → Fixer deploys → Verifier confirms">
        Agent Flow — Each agent gets only the tools it needs
      </div>
      <div className={styles.agentFlow}>
        {STAGE_ORDER.map((key, i) => {
          const meta = AGENT_META[key];
          if (!meta) return null;
          const rawStatus = agentStatuses[key] ?? "unknown";
          const isRunning = rawStatus === "running";
          const toolCount = toolDistribution?.[key]?.length;
          return (
            <div key={key} className={styles.flowItem}>
              <div
                className={`${styles.agentCard} ${isRunning ? styles.agentCardActive : ""}`}
                style={{ borderColor: isRunning ? meta.accent : "transparent" }}
              >
                <div className={styles.agentBanner} style={{ background: meta.gradient }}>
                  <span className={styles.agentEmoji}>{meta.emoji}</span>
                  <span className={`${styles.agentDot} ${isRunning ? styles.dotRunning : styles.dotIdle}`} />
                </div>
                <div className={styles.agentInfo}>
                  <span className={styles.agentLabel}>{meta.label}</span>
                  <span className={styles.agentStatus} style={{ color: isRunning ? meta.accent : "#64748b" }}>
                    {isRunning ? "Running" : running ? "Stopped" : "Idle"}
                  </span>
                  <span className={styles.agentDesc}>{meta.desc}</span>
                  {meta.tools && (
                    <span className={styles.agentDesc} style={{fontSize:"0.65rem", opacity:0.8, marginTop:"0.15rem"}}>
                      {meta.tools}{toolCount !== undefined ? ` (${String(toolCount)} MCP)` : ""}
                    </span>
                  )}
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
      <div className={styles.sectionLabel} data-tip="All incidents processed by the pipeline — auto-refreshes every 6 seconds">
        Pipeline Trace
        <span className={styles.sectionCount}>{incidents.length} incidents</span>
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
                <tr key={inc.incident_id}>
                  <td className={styles.tdIflow} title={inc.iflow_name || inc.iflow_id || inc.message_guid || ""}>
                    {inc.iflow_name || (
                      <span className={styles.tdIflowUnknown}>
                        {inc.message_guid ? "Resolving…" : "—"}
                      </span>
                    )}
                  </td>
                  <td><span className={styles.errorTypeBadge}>{inc.error_type}</span></td>
                  <td><span className={`${styles.statusChip} ${styles[`chip-${inc.status?.toLowerCase().replace(/\s+/g,"_")}`]}`}>{inc.status}</span></td>
                  <td className={styles.tdRca}>{inc.root_cause ?? "—"}</td>
                  <td className={styles.tdDate}>{new Date(inc.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

    </div>
  );
}
