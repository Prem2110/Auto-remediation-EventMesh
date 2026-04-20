import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchPipelineStatus, fetchQueueStats, startPipeline, stopPipeline } from "../../services/api.ts";
import _styles from "./agent-cards.module.css";
const styles = _styles as Record<string, string>;

// ── Pipeline agent meta ───────────────────────────────────────────────────────
const PIPELINE_META: Record<string, { emoji: string; desc: string; gradient: string; accent: string }> = {
  observer:     { emoji:"👁️",  desc:"Polls SAP CPI for failed messages, deduplicates, publishes to AEM.",        gradient:"linear-gradient(135deg,#0f172a 0%,#1e40af 100%)", accent:"#60a5fa" },
  classifier:   { emoji:"🏷️",  desc:"Classifies error type, confidence, and severity — zero LLM cost.",          gradient:"linear-gradient(135deg,#1e1b4b 0%,#7c3aed 100%)", accent:"#a78bfa" },
  orchestrator: { emoji:"🎯",  desc:"Routes by confidence threshold; fan-outs to RCA + Knowledge in parallel.",   gradient:"linear-gradient(135deg,#134e4a 0%,#0f766e 100%)", accent:"#2dd4bf" },
  rca:          { emoji:"🧠",  desc:"LLM root cause analysis via SAP AI Core (parallel with Knowledge).",         gradient:"linear-gradient(135deg,#064e3b 0%,#059669 100%)", accent:"#34d399" },
  fixer:        { emoji:"🔧",  desc:"Generates patch, assesses risk level (LOW/MEDIUM/HIGH), sets simulation.",   gradient:"linear-gradient(135deg,#312e81 0%,#6d28d9 100%)", accent:"#c084fc" },
};

export default function AgentCards() {
  const navigate  = useNavigate();
  const [hoveredId, setHoveredId] = useState<string|null>(null);
  const [toggling, setToggling]   = useState(false);

  const { data: pipelineData, refetch: refetchPipeline } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn:  fetchPipelineStatus,
    refetchInterval: 5_000,
  });

  const { data: queueRaw } = useQuery({
    queryKey: ["queue-stats"],
    queryFn:  fetchQueueStats,
    refetchInterval: 8_000,
    enabled:  pipelineData?.pipeline_running ?? false,
  });

  const pipelineRunning = pipelineData?.pipeline_running ?? false;
  const agentStatuses   = pipelineData?.agents ?? {};

  const qs = (queueRaw ?? {}) as Record<string, unknown>;
  const aemEnabled    = pipelineData?.aem_connected ?? false;
  const aemQueues     = (qs.queues ?? {}) as Record<string, { queue_depth: number; messages_retrieved: number }>;
  const aemQueueDepth = Object.values(aemQueues).reduce((s, q) => s + (q.queue_depth ?? 0), 0);
  const stageCounts   = (qs.stage_counts ?? {}) as Record<string, number>;

  async function handlePipelineToggle() {
    setToggling(true);
    try {
      pipelineRunning ? await stopPipeline() : await startPipeline();
      await refetchPipeline();
    } finally {
      setToggling(false);
    }
  }

  return (
    <div className={styles.page}>

      {/* ── Header ── */}
      <div className={styles.header}>
        <div>
          <h1 className={styles.pageTitle}>Agent Mesh</h1>
          <p className={styles.pageSubtitle}>Auto-Remediation Pipeline Agents</p>
        </div>
      </div>

      {/* ── Remediation Pipeline strip ── */}
      <div className={styles.pipelineStrip}>
        <div className={styles.pipelineStripLeft}>
          <span className={styles.pipelineStripTitle}>Auto-Remediation Pipeline</span>
          <span className={`${styles.pipelineBadge} ${pipelineRunning ? styles.pipelineBadgeOn : styles.pipelineBadgeOff}`}>
            {pipelineRunning ? "● Running" : "○ Stopped"}
          </span>
          {aemEnabled && (
            <span className={styles.pipelineAem}>AEM Connected</span>
          )}
          {aemEnabled && pipelineRunning && aemQueueDepth > 0 && (
            <span className={styles.pipelineAem}>Queue: {aemQueueDepth}</span>
          )}
          {aemEnabled && pipelineRunning && Object.keys(stageCounts).length > 0 && (
            <span className={styles.pipelineAemStages}>
              {Object.entries(stageCounts).map(([stage, n]) => (
                <span key={stage} className={styles.stageChip}>{stage}: {n}</span>
              ))}
            </span>
          )}
        </div>
        <div className={styles.pipelineStripRight}>
          <button
            className={`${styles.pipelineToggleBtn} ${pipelineRunning ? styles.pipelineToggleBtnStop : styles.pipelineToggleBtnStart}`}
            onClick={handlePipelineToggle}
            disabled={toggling}
          >
            {toggling ? "…" : pipelineRunning ? "Stop Pipeline" : "Start Pipeline"}
          </button>
          <button className={styles.pipelineDetailsBtn} onClick={() => navigate("/pipeline")}>
            View Details →
          </button>
        </div>
      </div>

      {/* ── Pipeline agent cards ── */}
      <div className={styles.sectionLabel}>Remediation Agents</div>
      <div className={styles.grid}>
        {Object.entries(PIPELINE_META).map(([key, meta], i) => {
          const raw   = agentStatuses[key] ?? "unknown";
          const isRun = raw === "running";
          return (
            <div key={key}
              className={`${styles.card} ${hoveredId === `p-${key}` ? styles.cardHovered : ""}`}
              style={{ animationDelay: `${i * 60}ms` }}
              onMouseEnter={() => setHoveredId(`p-${key}`)}
              onMouseLeave={() => setHoveredId(null)}>
              <div className={styles.cardBanner} style={{ background: meta.gradient }}>
                <span className={styles.cardEmoji}>{meta.emoji}</span>
                <div className={styles.cardHeaderRight}>
                  <span className={`${styles.statusDot} ${isRun ? styles["status-online"] : styles["status-offline"]}`} />
                  <span className={styles.statusText}>{isRun ? "Running" : pipelineRunning ? "Stopped" : "Idle"}</span>
                </div>
              </div>
              <div className={styles.cardBody}>
                <div className={styles.cardTitleRow}>
                  <h3 className={styles.cardTitle}>{key.charAt(0).toUpperCase() + key.slice(1)} Agent</h3>
                  <span className={styles.cardVersion}>pipeline</span>
                </div>
                <p className={styles.cardDesc}>{meta.desc}</p>
              </div>
              <div className={styles.cardFooter}>
                <button className={styles.launchBtn} style={{ background: meta.gradient }}
                  onClick={() => navigate("/pipeline")}>
                  Details →
                </button>
              </div>
            </div>
          );
        })}
      </div>

    </div>
  );
}
