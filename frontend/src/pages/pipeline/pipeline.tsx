import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchPipelineStatus,
  fetchToolDistribution,
  startPipeline,
  stopPipeline,
  fetchQueueStats,
  fetchPipelineTrace,
  searchKnowledge,
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

// Legacy 9-agent metadata (AEM pipeline)
const AEM_AGENTS: Record<string, { emoji: string; label: string; desc: string; tools: string; gradient: string; accent: string }> = {
  observer:     { emoji:"👁️",  label:"Observer",     desc:"Subscribes to AEM integration/errors/# (polls in dev mode)", tools:"", gradient:"linear-gradient(135deg,#0f172a 0%,#1e40af 100%)", accent:"#60a5fa" },
  classifier:   { emoji:"🏷️",  label:"Classifier",   desc:"Error type, confidence, severity — zero LLM cost",        tools:"", gradient:"linear-gradient(135deg,#1e1b4b 0%,#7c3aed 100%)", accent:"#a78bfa" },
  orchestrator: { emoji:"🎯",  label:"Orchestrator",  desc:"Routes by confidence; fan-out to RCA + Knowledge",        tools:"", gradient:"linear-gradient(135deg,#134e4a 0%,#0f766e 100%)", accent:"#2dd4bf" },
  rca:          { emoji:"🧠",  label:"RCA",           desc:"Root cause analysis via SAP AI Core (parallel)",          tools:"", gradient:"linear-gradient(135deg,#064e3b 0%,#059669 100%)", accent:"#34d399" },
  knowledge:    { emoji:"📚",  label:"Knowledge",     desc:"HANA vector similarity search ≥0.75",                     tools:"", gradient:"linear-gradient(135deg,#78350f 0%,#d97706 100%)", accent:"#fbbf24" },
  aggregator:   { emoji:"🔀",  label:"Aggregator",    desc:"Merges RCA + Knowledge results",                          tools:"", gradient:"linear-gradient(135deg,#1e3a5f 0%,#0284c7 100%)", accent:"#38bdf8" },
  fixer:        { emoji:"🔧",  label:"Fixer",         desc:"Patch generation + risk assessment (LOW/MEDIUM/HIGH)",    tools:"", gradient:"linear-gradient(135deg,#312e81 0%,#6d28d9 100%)", accent:"#c084fc" },
  executor:     { emoji:"🚀",  label:"Executor",      desc:"Deploy via SAP IS API — AUTO / APPROVAL / TICKET",       tools:"", gradient:"linear-gradient(135deg,#4c0519 0%,#be123c 100%)", accent:"#fb7185" },
  learner:      { emoji:"🎓",  label:"Learner",       desc:"KB update, outcome recording, Classifier feedback",       tools:"", gradient:"linear-gradient(135deg,#1c1917 0%,#854d0e 100%)", accent:"#fbbf24" },
};

const SPECIALIST_ORDER = ["observer", "classifier", "rca", "fixer", "verifier"];
const AEM_ORDER = ["observer", "classifier", "orchestrator", "rca", "knowledge", "aggregator", "fixer", "executor", "learner"];

const STAGE_TIPS: Record<string, string> = {
  observed:   "Incidents picked up from the SAP CPI message processing log",
  classified: "Error type and severity determined by the rule-based classifier",
  rca:        "Root cause analysis completed via SAP AI Core",
  fix:        "Patch generated and deployed via the SAP Integration Suite API",
  verified:   "Fix confirmed through automated integration test execution",
};

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

interface KnowledgeResult {
  fix_description: string;
  similarity_score: number;
  error_type: string;
  iflow_name?: string;
}

export default function Pipeline() {
  const qc = useQueryClient();
  const [toggling, setToggling] = useState(false);
  const [knowledgeQuery, setKnowledgeQuery] = useState("");
  const [knowledgeResults, setKnowledgeResults] = useState<KnowledgeResult[] | null>(null);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);

  // ── Queries ──────────────────────────────────────────────────────────────
  const { data: pipelineData } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn: fetchPipelineStatus,
    refetchInterval: 15_000,   // was 4s — status changes rarely
  });

  // Tools never change after startup — fetch once, cache for 10 minutes
  const { data: toolDist } = useQuery({
    queryKey: ["tool-distribution"],
    queryFn: fetchToolDistribution,
    staleTime: 10 * 60 * 1000,
    refetchInterval: false,
  });

  const { data: queueStats } = useQuery({
    queryKey: ["queue-stats"],
    queryFn: fetchQueueStats,
    refetchInterval: 30_000,   // was 8s — queue depth doesn't need sub-second precision
    enabled: pipelineData?.pipeline_running ?? false,
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

  // ── Knowledge search ─────────────────────────────────────────────────────
  async function handleKnowledgeSearch() {
    if (!knowledgeQuery.trim()) return;
    setKnowledgeLoading(true);
    try {
      const res = await searchKnowledge(knowledgeQuery.trim());
      setKnowledgeResults(res.results as KnowledgeResult[]);
    } catch {
      setKnowledgeResults([]);
    } finally {
      setKnowledgeLoading(false);
    }
  }

  const running = pipelineData?.pipeline_running ?? false;
  const agentStatuses = pipelineData?.agents ?? {};
  const incidents: TraceIncident[] = (traceData?.incidents ?? []) as TraceIncident[];
  const isSpecialist = pipelineData?.pipeline_type === "specialist";
  const toolDistribution = toolDist ?? pipelineData?.tool_distribution;

  // ── AEM queue stats derivations ──────────────────────────────────────────
  const qs                  = (queueStats as Record<string, unknown>) ?? {};
  const aemQueues           = (qs.queues  ?? {}) as Record<string, { queue_depth: number; messages_retrieved: number; messages_published: number; messages_dropped: number }>;
  const aemStages           = (qs.stage_counts ?? {}) as Record<string, number>;
  const aemTotal            = (qs.total_incidents as number) ?? 0;
  const aemSempError        = qs.semp_error as string | null;
  const aemHasStats         = Boolean(queueStats && !(qs.warning));
  const aemReceiverConnected = (qs.receiver_connected as boolean) ?? false;

  const AGENT_META = isSpecialist ? SPECIALIST_AGENTS : AEM_AGENTS;
  const STAGE_ORDER = isSpecialist ? SPECIALIST_ORDER : AEM_ORDER;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>

      {/* ── Header ── */}
      <div className={styles.header}>
        <div>
          <h1 className={styles.pageTitle}>Auto-Remediation Pipeline</h1>
          <p className={styles.pageSubtitle}>
            {isSpecialist
              ? "5 specialist agents · Per-agent tools · SAP AI Core · HANA vector search"
              : "9-agent AEM pipeline · SAP AI Core · HANA vector search"
            }
          </p>
        </div>
        <div className={styles.headerRight}>
          <span
            className={`${styles.statusBadge} ${running ? styles.statusBadgeOn : styles.statusBadgeOff}`}
            data-tip={running ? "Pipeline is actively monitoring SAP CPI for failures" : "Pipeline is stopped — no new incidents will be detected"}
          >
            {running ? "● Running" : "○ Stopped"}
          </span>
          {isSpecialist && running && (
            <span className={styles.aemBadge} data-tip="5-agent specialist mode — each agent has a curated, minimal tool set for safety and efficiency">Specialist</span>
          )}
          {!isSpecialist && pipelineData?.aem_connected && (
            <span className={styles.aemBadge} data-tip="Advanced Event Mesh (Solace PubSub+) is active — queue receiver is connected and consuming messages">AEM Connected</span>
          )}
          {!isSpecialist && !pipelineData?.aem_connected && aemHasStats && (
            <span className={styles.aemBadge} style={{ background: "#7f1d1d", color: "#fca5a5" }} data-tip="AEM is enabled but the queue receiver is disconnected — automatic reconnect is in progress">AEM Reconnecting…</span>
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
        {isSpecialist ? "Agent Flow — Each agent gets only the tools it needs" : "Agent Flow"}
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
                  {isSpecialist && meta.tools && (
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

      {/* ── Queue stats ── */}
      {aemHasStats && (
        <>
          <div className={styles.sectionLabel}>
            AEM Queue Stats
            {aemTotal > 0 && (
              <span className={styles.sectionCount}>{aemTotal} total incidents</span>
            )}
            {!aemReceiverConnected && (
              <span
                className={styles.aemSempError}
                style={{ marginLeft: "0.75rem", animation: "pulse 1.5s infinite" }}
                data-tip="The Solace queue receiver dropped its connection and is automatically reconnecting with exponential backoff. No messages are being consumed until it reconnects."
              >
                ⚠ Receiver reconnecting…
              </span>
            )}
          </div>

          {/* Queue cards */}
          <div className={styles.queueGrid}>
            {Object.entries(aemQueues).map(([qName, stats]) => (
              <div key={qName} className={styles.queueCard}>
                <span className={styles.queueName}>{qName}</span>
                <div className={styles.queueStats}>
                  <span className={styles.queueStat}>
                    <span className={`${styles.queueStatKey} tooltip-right`} data-tip="queue depth — messages currently waiting in AEM. Red means a processing backlog exists; the pipeline is falling behind.">queue depth</span>
                    <span className={`${styles.queueStatVal} ${stats.queue_depth > 0 ? styles.queueStatValAlert : ""}`}>
                      {stats.queue_depth}
                    </span>
                  </span>
                  <span className={styles.queueStat}>
                    <span className={`${styles.queueStatKey} tooltip-right`} data-tip="consumed — messages pulled from the AEM queue this session. Each SAP CPI error event enters the pipeline here.">consumed</span>
                    <span className={styles.queueStatVal}>{stats.messages_retrieved}</span>
                  </span>
                  <span className={styles.queueStat}>
                    <span className={`${styles.queueStatKey} tooltip-right`} data-tip={`published — stage-transition messages sent back to the AEM topic this session. Each pipeline stage (classify → RCA → fix → verify) publishes back to advance the incident. published ÷ consumed ≈ ${stats.messages_retrieved > 0 ? (stats.messages_published / stats.messages_retrieved).toFixed(1) : "—"} stages per incident on average. If published >> consumed the pipeline is doing many stage transitions per error.`}>published</span>
                    <span className={styles.queueStatVal}>{stats.messages_published}</span>
                  </span>
                  {stats.messages_dropped > 0 && (
                    <span className={styles.queueStat}>
                      <span className={`${styles.queueStatKey} tooltip-right`} data-tip="dropped — messages lost due to internal buffer overflow (capacity: 1000). This means errors arrived faster than the pipeline could process them. Increase SOLACE_INBOUND_QUEUE_MAXSIZE in .env to raise the buffer limit.">dropped</span>
                      <span className={`${styles.queueStatVal} ${styles.queueStatValAlert}`}>{stats.messages_dropped}</span>
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Stage pipeline counts */}
          {Object.keys(aemStages).length > 0 && (
            <div className={styles.aemStagesRow}>
              {(["observed", "classified", "rca", "fix", "verified"] as const).map((stage, i, arr) => (
                <span key={stage} className={styles.aemStageItem}>
                  <span className={styles.aemStageName} data-tip={STAGE_TIPS[stage]}>{stage}</span>
                  <span className={`${styles.aemStageCount} ${(aemStages[stage] ?? 0) > 0 ? styles.aemStageCountActive : ""}`}>
                    {aemStages[stage] ?? 0}
                  </span>
                  {i < arr.length - 1 && <span className={styles.aemStageArrow}>→</span>}
                </span>
              ))}
              {aemSempError && (
                <span className={styles.aemSempError} data-tip="SEMP (Solace Element Management Protocol) REST API error — queue statistics may be unreliable">SEMP: {aemSempError}</span>
              )}
            </div>
          )}
        </>
      )}

      {/* ── Knowledge search ── */}
      <div className={styles.sectionLabel} data-tip="Search past incidents for similar error patterns and their proven fixes">Knowledge Base Search</div>
      <div className={styles.knowledgePanel}>
        <div className={styles.knowledgeInputRow}>
          <input
            className={styles.knowledgeInput}
            placeholder="Search for similar fixes… e.g. 'Connection refused to SOAP endpoint'"
            value={knowledgeQuery}
            onChange={e => setKnowledgeQuery(e.target.value)}
            onKeyDown={e => e.key === "Enter" && handleKnowledgeSearch()}
            title="Enter an error message or description to find similar past incidents and their fixes"
          />
          <button
            className={styles.knowledgeBtn}
            onClick={handleKnowledgeSearch}
            disabled={knowledgeLoading || !knowledgeQuery.trim()}
            data-tip="Search the knowledge base for similar error patterns and proven fixes"
          >
            {knowledgeLoading ? "Searching…" : "Search"}
          </button>
        </div>

        {knowledgeResults !== null && (
          <div className={styles.knowledgeResults}>
            {knowledgeResults.length === 0 ? (
              <div className={styles.knowledgeEmpty}>No similar fixes found.</div>
            ) : (
              knowledgeResults.map((r, i) => (
                <div key={i} className={styles.knowledgeResultCard}>
                  <div className={styles.knowledgeResultHeader}>
                    <span className={styles.knowledgeResultType}>{r.error_type}</span>
                    <span className={styles.knowledgeResultScore} data-tip="Similarity score: 100% = exact keyword match, 60% = partial match based on error pattern overlap">
                      {(r.similarity_score * 100).toFixed(1)}% match
                    </span>
                  </div>
                  {r.iflow_name && (
                    <span className={styles.knowledgeResultIflow}>{r.iflow_name}</span>
                  )}
                  <p className={styles.knowledgeResultFix}>{r.fix_description}</p>
                </div>
              ))
            )}
          </div>
        )}
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
