import { useState, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchAemStatus, fetchAemIncidents } from "../../services/api.ts";
import type { AemStatusResponse } from "../../services/api.ts";
import styles from "./EventMeshFlow.module.css";

// ── Types ──────────────────────────────────────────────────────────────────────

interface Incident {
  [key: string]: unknown;
  incident_id: string;
  iflow_id?: string;
  iflow_name?: string;
  status: string;
}

function isIncident(value: Record<string, unknown>): value is Incident {
  return typeof value.incident_id === "string" && typeof value.status === "string";
}

interface LogEntry {
  id: string;
  ts: string;
  icon: string;
  iflowName: string;
  stage: string;
  status: string;
  isNew: boolean;
}

// ── Node definitions ───────────────────────────────────────────────────────────

type NodeId = "cpi" | "orchestrator" | "observer" | "rca" | "fixer" | "verifier";
type GlowState = "blue" | "green" | "red" | "idle";

interface NodeDef { id: NodeId; label: string; sub: string; abbr: string; cx: number }

const CY = 68;
const R  = 30;
const SVG_H = 178;
const SVG_W = 900;

const NODES: NodeDef[] = [
  { id: "cpi",          label: "SAP CPI",      sub: "Source",   abbr: "CPI", cx: 75  },
  { id: "orchestrator", label: "Orchestrator",  sub: "Dispatch", abbr: "ORC", cx: 225 },
  { id: "observer",     label: "Observer",      sub: "Detect",   abbr: "OBS", cx: 375 },
  { id: "rca",          label: "RCA Agent",     sub: "Analyze",  abbr: "RCA", cx: 525 },
  { id: "fixer",        label: "Fixer Agent",   sub: "Repair",   abbr: "FIX", cx: 675 },
  { id: "verifier",     label: "Verifier",      sub: "Verify",   abbr: "VER", cx: 825 },
];

// ── Status mappings ────────────────────────────────────────────────────────────

const STATUS_TO_NODE: Partial<Record<string, NodeId>> = {
  DETECTED:             "orchestrator",
  CLASSIFIED:           "observer",
  RCA_IN_PROGRESS:      "observer",
  RCA_COMPLETE:         "rca",
  FIX_IN_PROGRESS:      "fixer",
  FIX_DEPLOYED:         "fixer",
  AWAITING_APPROVAL:    "fixer",
  FIX_VERIFIED:         "verifier",
  HUMAN_INITIATED_FIX:  "verifier",
  AUTO_FIXED:           "verifier",
  RETRIED:              "verifier",
  FIX_FAILED:           "fixer",
  FIX_FAILED_UPDATE:    "fixer",
  FIX_FAILED_DEPLOY:    "fixer",
  FIX_FAILED_RUNTIME:   "fixer",
  RCA_FAILED:           "rca",
  TICKET_CREATED:       "rca",
  PIPELINE_ERROR:       "orchestrator",
};

function particleColor(status: string): "blue" | "green" | "red" {
  const s = status.toUpperCase();
  if (s.includes("FAIL") || s === "REJECTED") return "red";
  if (["FIX_VERIFIED", "HUMAN_INITIATED_FIX", "AUTO_FIXED", "RETRIED"].includes(s)) return "green";
  return "blue";
}

function statusToStage(status: string): string {
  const s = status.toUpperCase();
  if (["FIX_VERIFIED", "HUMAN_INITIATED_FIX", "AUTO_FIXED", "RETRIED"].includes(s)) return "verified";
  if (s.startsWith("FIX") || s === "AWAITING_APPROVAL" || s === "FIX_DEPLOYED") return "fix";
  if (["RCA_IN_PROGRESS", "RCA_COMPLETE", "RCA_FAILED", "TICKET_CREATED"].includes(s)) return "rca";
  if (s === "CLASSIFIED") return "classified";
  return "observed";
}

const STATUS_ICON: Partial<Record<string, string>> = {
  DETECTED:             "📥",
  CLASSIFIED:           "🔍",
  RCA_IN_PROGRESS:      "🧠",
  RCA_COMPLETE:         "🧠",
  FIX_IN_PROGRESS:      "🔧",
  FIX_DEPLOYED:         "🔧",
  AWAITING_APPROVAL:    "⏳",
  FIX_VERIFIED:         "✅",
  HUMAN_INITIATED_FIX:  "✅",
  AUTO_FIXED:           "✅",
  RETRIED:              "✅",
  FIX_FAILED:           "❌",
  FIX_FAILED_UPDATE:    "❌",
  FIX_FAILED_DEPLOY:    "❌",
  FIX_FAILED_RUNTIME:   "❌",
  RCA_FAILED:           "❌",
  TICKET_CREATED:       "🎫",
  PIPELINE_ERROR:       "⚠️",
};

const STAGE_CFG: Record<string, { color: string; bg: string; border: string; label: string }> = {
  observed:   { color: "#93c5fd", bg: "rgba(37,99,235,0.18)",   border: "rgba(59,130,246,0.35)",  label: "Observed"   },
  classified: { color: "#fcd34d", bg: "rgba(217,119,6,0.18)",   border: "rgba(245,158,11,0.35)",  label: "Classified" },
  rca:        { color: "#c4b5fd", bg: "rgba(124,58,237,0.18)",  border: "rgba(167,139,250,0.35)", label: "RCA"        },
  fix:        { color: "#fca5a5", bg: "rgba(220,38,38,0.18)",   border: "rgba(248,113,113,0.35)", label: "Fix"        },
  verified:   { color: "#86efac", bg: "rgba(22,163,74,0.18)",   border: "rgba(74,222,128,0.35)",  label: "Verified"   },
};

// ── Glow colors ───────────────────────────────────────────────────────────────

const GLOW_STROKE: Record<GlowState, string> = {
  blue:  "#3b82f6",
  green: "#22c55e",
  red:   "#ef4444",
  idle:  "#374151",
};

const GLOW_FILL: Record<GlowState, string> = {
  blue:  "rgba(59,130,246,0.13)",
  green: "rgba(34,197,94,0.13)",
  red:   "rgba(239,68,68,0.13)",
  idle:  "rgba(15,23,42,0.6)",
};

const GLOW_TEXT: Record<GlowState, string> = {
  blue:  "#93c5fd",
  green: "#86efac",
  red:   "#fca5a5",
  idle:  "#6b7280",
};

const GLOW_CLASS: Record<GlowState, string> = {
  blue:  styles.glowBlue,
  green: styles.glowGreen,
  red:   styles.glowRed,
  idle:  "",
};

// ── Node-state computation ────────────────────────────────────────────────────

type NodeCounts = { blue: number; green: number; red: number; total: number };
type NodeCountMap = Map<NodeId, NodeCounts>;

function computeNodeCounts(incidents: Incident[]): NodeCountMap {
  const map: NodeCountMap = new Map();
  NODES.forEach(n => map.set(n.id, { blue: 0, green: 0, red: 0, total: 0 }));
  for (const inc of incidents) {
    const nodeId = STATUS_TO_NODE[inc.status.toUpperCase()];
    if (!nodeId) continue;
    const e = map.get(nodeId)!;
    e[particleColor(inc.status)]++;
    e.total++;
  }
  return map;
}

function nodeGlowState(c: NodeCounts): GlowState {
  if (c.red > 0) return "red";
  if (c.green > 0) return "green";
  if (c.blue > 0) return "blue";
  return "idle";
}

// ── SVG sub-components ────────────────────────────────────────────────────────

interface NodeParticlesProps { cx: number; counts: NodeCounts }

function NodeParticles({ cx, counts }: NodeParticlesProps) {
  const slots: string[] = [
    ...Array<string>(Math.min(counts.blue,  5)).fill("#3b82f6"),
    ...Array<string>(Math.min(counts.green, Math.max(0, 5 - counts.blue))).fill("#22c55e"),
    ...Array<string>(Math.min(counts.red,   Math.max(0, 5 - counts.blue - counts.green))).fill("#ef4444"),
  ].slice(0, 5);

  const extra = counts.total - slots.length;
  if (slots.length === 0) return null;

  const spacing = 10;
  const startX = cx - ((slots.length - 1) * spacing) / 2;
  const dotY = CY + R + 48;

  return (
    <g>
      {slots.map((fill, i) => (
        <circle
          key={i}
          cx={startX + i * spacing}
          cy={dotY}
          r={4}
          fill={fill}
          className={styles.particleDot}
          style={{ animationDelay: `${i * 0.25}s` }}
        />
      ))}
      {extra > 0 && (
        <text x={startX + slots.length * spacing + 5} y={dotY + 4} fontSize={9} fill="#6b7280">
          +{extra}
        </text>
      )}
    </g>
  );
}

// ── Pipeline Diagram ──────────────────────────────────────────────────────────

interface PipelineDiagramProps {
  incidents: Incident[];
  aemEnabled: boolean;
  messagesRetrieved: number;
}

function PipelineDiagram({ incidents, aemEnabled, messagesRetrieved }: PipelineDiagramProps) {
  const counts = computeNodeCounts(incidents);

  function getGlow(nodeId: NodeId): GlowState {
    if (nodeId === "cpi") return (aemEnabled && messagesRetrieved > 0) ? "blue" : "idle";
    return nodeGlowState(counts.get(nodeId) ?? { blue: 0, green: 0, red: 0, total: 0 });
  }

  function connActive(fromIdx: number): boolean {
    return getGlow(NODES[fromIdx + 1].id) !== "idle";
  }

  return (
    <div className={styles.diagramCard}>
      <div className={styles.diagramHeader}>
        <span className={styles.diagramTitle}>Event Mesh Pipeline Flow</span>
        <span className={styles.aemStatus} data-enabled={String(aemEnabled)}>
          <span className={styles.aemDot} />
          {aemEnabled ? "AEM Connected" : "AEM Disconnected"}
        </span>
      </div>
      <div className={styles.svgWrapper}>
        <svg
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          preserveAspectRatio="xMidYMid meet"
          style={{ width: "100%", display: "block" }}
          aria-label="SAP Event Mesh pipeline diagram"
        >
          <defs>
            <marker id="em-arrow-idle" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto">
              <path d="M0,1 L9,5 L0,9z" fill="#374151" />
            </marker>
            <marker id="em-arrow-active" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto">
              <path d="M0,1 L9,5 L0,9z" fill="#2563eb" />
            </marker>
          </defs>

          {/* Connectors */}
          {NODES.slice(0, -1).map((node, i) => {
            const x1 = node.cx + R + 4;
            const x2 = NODES[i + 1].cx - R - 4;
            const active = connActive(i);
            return (
              <line
                key={`c${i}`}
                x1={x1} y1={CY} x2={x2} y2={CY}
                stroke={active ? "#2563eb" : "#2d3748"}
                strokeWidth={active ? 2.5 : 1.5}
                className={active ? styles.connectorActive : styles.connector}
                markerEnd={active ? "url(#em-arrow-active)" : "url(#em-arrow-idle)"}
              />
            );
          })}

          {/* Nodes */}
          {NODES.map((node) => {
            const glow    = getGlow(node.id);
            const nodeCts = node.id === "cpi"
              ? { blue: aemEnabled ? 1 : 0, green: 0, red: 0, total: aemEnabled ? 1 : 0 }
              : (counts.get(node.id) ?? { blue: 0, green: 0, red: 0, total: 0 });
            const total = node.id === "cpi" ? (aemEnabled && messagesRetrieved > 0 ? messagesRetrieved : 0) : nodeCts.total;

            return (
              <g key={node.id} className={GLOW_CLASS[glow]}>
                {/* Outer ring */}
                {glow !== "idle" && (
                  <circle cx={node.cx} cy={CY} r={R + 9} fill="none" stroke={GLOW_STROKE[glow]} strokeWidth={1} opacity={0.25} />
                )}
                {/* Main circle */}
                <circle cx={node.cx} cy={CY} r={R} fill={GLOW_FILL[glow]} stroke={GLOW_STROKE[glow]} strokeWidth={glow !== "idle" ? 2 : 1.5} />
                {/* Abbreviation */}
                <text x={node.cx} y={CY + 5} textAnchor="middle" fontSize={11} fontWeight={700} fontFamily="Inter,sans-serif" fill={GLOW_TEXT[glow]} letterSpacing={1}>
                  {node.abbr}
                </text>
                {/* Count badge */}
                {total > 0 && (
                  <g>
                    <circle cx={node.cx + R - 4} cy={CY - R + 4} r={9} fill={GLOW_STROKE[glow]} />
                    <text x={node.cx + R - 4} y={CY - R + 8} textAnchor="middle" fontSize={8} fontWeight={700} fill="#fff">
                      {total > 99 ? "99+" : String(total)}
                    </text>
                  </g>
                )}
                {/* Label */}
                <text x={node.cx} y={CY + R + 16} textAnchor="middle" fontSize={11} fontWeight={600} fontFamily="Inter,sans-serif" fill={glow !== "idle" ? GLOW_TEXT[glow] : "#9ca3af"}>
                  {node.label}
                </text>
                <text x={node.cx} y={CY + R + 30} textAnchor="middle" fontSize={9} fontFamily="Inter,sans-serif" fill="#4b5563">
                  {node.sub}
                </text>
              </g>
            );
          })}

          {/* Particles */}
          {NODES.map((node) => {
            const nodeCts = node.id === "cpi"
              ? { blue: 0, green: 0, red: 0, total: 0 }
              : (counts.get(node.id) ?? { blue: 0, green: 0, red: 0, total: 0 });
            return <NodeParticles key={`p-${node.id}`} cx={node.cx} counts={nodeCts} />;
          })}
        </svg>
      </div>
    </div>
  );
}

// ── Stats Row ─────────────────────────────────────────────────────────────────

function StatsRow({ aemStatus, incidents }: { aemStatus: AemStatusResponse | null; incidents: Incident[] }) {
  const fixedStatuses = new Set(["FIX_VERIFIED", "HUMAN_INITIATED_FIX", "AUTO_FIXED", "RETRIED"]);
  const terminalStatuses = new Set([
    "FIX_VERIFIED", "HUMAN_INITIATED_FIX", "AUTO_FIXED", "RETRIED",
    "FIX_FAILED", "FIX_FAILED_UPDATE", "FIX_FAILED_DEPLOY", "FIX_FAILED_RUNTIME",
    "RCA_FAILED", "PIPELINE_ERROR", "REJECTED", "TICKET_CREATED", "ARTIFACT_MISSING",
  ]);

  const fixedCount    = incidents.filter(i => fixedStatuses.has(i.status.toUpperCase())).length;
  const failedCount   = incidents.filter(i => i.status.toUpperCase().includes("FAIL") || i.status.toUpperCase() === "REJECTED").length;
  const inProgress    = incidents.filter(i => !terminalStatuses.has(i.status.toUpperCase())).length;

  const CARDS: Array<{ label: string; value: number | string; accent: string }> = [
    { label: "Webhooks Received",  value: aemStatus?.messages_retrieved ?? "—", accent: "#3b82f6" },
    { label: "Total Incidents",    value: aemStatus?.total_incidents ?? incidents.length,   accent: "#6366f1" },
    { label: "Fixed & Verified",   value: fixedCount,   accent: "#22c55e" },
    { label: "In Progress",        value: inProgress,   accent: "#f59e0b" },
    { label: "Failed",             value: failedCount,  accent: "#ef4444" },
    { label: "Queue Depth",        value: aemStatus?.queue_depth ?? "—", accent: "#8b5cf6" },
  ];

  return (
    <div className={styles.statsRow}>
      {CARDS.map(card => (
        <div key={card.label} className={styles.statCard} style={{ borderTopColor: card.accent }}>
          <span className={styles.statValue} style={{ color: card.accent }}>{card.value}</span>
          <span className={styles.statLabel}>{card.label}</span>
        </div>
      ))}
    </div>
  );
}

// ── Event Log ─────────────────────────────────────────────────────────────────

function EventLog({ entries, onClear }: { entries: LogEntry[]; onClear: () => void }) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [entries.length]);

  return (
    <div className={styles.logCard}>
      <div className={styles.logHeader}>
        <div className={styles.logHeaderLeft}>
          <span className={styles.logTitle}>Live Event Log</span>
          <span className={styles.liveChip}>Live</span>
        </div>
        <button className={styles.clearBtn} onClick={onClear}>Clear</button>
      </div>
      <div className={styles.logScroll} ref={scrollRef}>
        {entries.length === 0 ? (
          <div className={styles.logEmpty}>
            <span className={styles.logEmptyIcon}>📡</span>
            <span className={styles.logEmptyText}>Waiting for events…</span>
          </div>
        ) : (
          entries.map(entry => {
            const stageCfg = STAGE_CFG[entry.stage] ?? STAGE_CFG.observed;
            const ts = new Date(entry.ts);
            const timeStr = ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
            return (
              <div key={entry.id} className={styles.logEntry}>
                <span className={styles.logTs}>{timeStr}</span>
                <span className={styles.logIcon}>{entry.icon}</span>
                <span className={`${styles.logIflow} ${entry.isNew ? styles.logIflowNew : ""}`}>
                  {entry.iflowName}
                </span>
                <span
                  className={styles.stageBadge}
                  style={{ color: stageCfg.color, background: stageCfg.bg, borderColor: stageCfg.border }}
                >
                  {stageCfg.label}
                </span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function EventMeshFlow() {
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const prevMapRef = useRef<Map<string, string>>(new Map());

  const { data: aemStatus } = useQuery({
    queryKey: ["aem-flow-status"],
    queryFn: fetchAemStatus,
    refetchInterval: 3_000,
    retry: false,
    staleTime: 0,
  });

  const { data: incidentsData } = useQuery({
    queryKey: ["aem-flow-incidents"],
    queryFn: () => fetchAemIncidents(100),
    refetchInterval: 3_000,
    retry: 2,
    staleTime: 0,
  });

  const incidents: Incident[] = (incidentsData?.incidents ?? []).filter(isIncident);

  // Detect new incidents and status changes to drive the log
  useEffect(() => {
    if (!incidentsData) return;
    const prev = prevMapRef.current;
    const newEntries: LogEntry[] = [];

    for (const inc of incidents) {
      const prevStatus = prev.get(inc.incident_id);
      const statusUp = inc.status.toUpperCase();
      if (!prevStatus) {
        newEntries.push({
          id:        `${inc.incident_id}-${Date.now()}-new`,
          ts:        new Date().toISOString(),
          icon:      STATUS_ICON[statusUp] ?? "📥",
          iflowName: inc.iflow_name ?? inc.iflow_id ?? "—",
          stage:     statusToStage(inc.status),
          status:    inc.status,
          isNew:     true,
        });
      } else if (prevStatus !== inc.status) {
        newEntries.push({
          id:        `${inc.incident_id}-${Date.now()}-chg`,
          ts:        new Date().toISOString(),
          icon:      STATUS_ICON[statusUp] ?? "🔄",
          iflowName: inc.iflow_name ?? inc.iflow_id ?? "—",
          stage:     statusToStage(inc.status),
          status:    inc.status,
          isNew:     false,
        });
      }
    }

    if (newEntries.length > 0) {
      setLogEntries(prev => [...newEntries, ...prev].slice(0, 120));
    }

    const newMap = new Map<string, string>();
    incidents.forEach(inc => newMap.set(inc.incident_id, inc.status));
    prevMapRef.current = newMap;
  }, [incidents, incidentsData]);

  return (
    <div className={styles.root}>
      <PipelineDiagram
        incidents={incidents}
        aemEnabled={aemStatus?.aem_enabled ?? false}
        messagesRetrieved={aemStatus?.messages_retrieved ?? 0}
      />
      <StatsRow aemStatus={aemStatus ?? null} incidents={incidents} />
      <EventLog entries={logEntries} onClear={() => setLogEntries([])} />
    </div>
  );
}
