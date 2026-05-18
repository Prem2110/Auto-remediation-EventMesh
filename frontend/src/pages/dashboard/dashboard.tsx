import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import React, { useState } from "react";
import {
  PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  AreaChart, Area,
} from "recharts";
import {
  fetchDashboardAll,
  fetchFailedMessagesPaginated,
  fetchActiveIncidentsPaginated,
  fetchTickets,
  updateTicket,
  type PaginatedMessagesResponse,
  type PaginatedIncidentsResponse,
} from "../../services/api.ts";
import Pagination from "../../components/pagination/Pagination";
import { useThemeColors } from "../../hooks/useThemeColors.ts";
import styles from "./dashboard.module.css";

// ── Formatters ────────────────────────────────────────────────────────────────
function formatISODate(value: string | null | undefined): string {
  if (!value) return "-";
  const d = new Date(value);
  if (isNaN(d.getTime())) return value;
  return d.toLocaleString("en-GB", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

const INCIDENT_STATE: Record<string, string> = {
  RCA_COMPLETE: styles.stateSuccess,
  IN_PROGRESS:  styles.stateWarning,
  PENDING:      styles.stateNone,
  FAILED:       styles.stateError,
  FIX_APPLIED:  styles.stateSuccess,
};

// ── Status badge (colored pill with dot) ─────────────────────────────────────
const STATUS_BADGE_STYLES: Record<string, { bg: string; color: string; dot: string; label: string }> = {
  TICKET_CREATED:      { bg: "#f3e8ff", color: "#7c3aed", dot: "#7c3aed",  label: "Ticket Created" },
  ARTIFACT_MISSING:    { bg: "#f1f5f9", color: "#64748b", dot: "#94a3b8",  label: "Artifact Missing" },
  RCA_COMPLETED:       { bg: "#dcfce7", color: "#16a34a", dot: "#16a34a",  label: "RCA Completed" },
  RCA_COMPLETE:        { bg: "#dcfce7", color: "#16a34a", dot: "#16a34a",  label: "RCA Completed" },
  FIX_IN_PROGRESS:     { bg: "#fff7ed", color: "#ea580c", dot: "#ea580c",  label: "Fix In Progress" },
  IN_PROGRESS:         { bg: "#fff7ed", color: "#ea580c", dot: "#ea580c",  label: "Fix In Progress" },
  FIX_COMPLETED:       { bg: "#dcfce7", color: "#16a34a", dot: "#16a34a",  label: "Fix Completed" },
  RCA_IN_PROGRESS:     { bg: "#fef3c7", color: "#d97706", dot: "#d97706",  label: "RCA In Progress" },
  FAILED:              { bg: "#fee2e2", color: "#dc2626", dot: "#dc2626",  label: "Failed" },
  AUTO_FIXED:          { bg: "#dcfce7", color: "#16a34a", dot: "#16a34a",  label: "Auto Fixed" },
  FIX_FAILED:          { bg: "#fee2e2", color: "#dc2626", dot: "#dc2626",  label: "Fix Failed" },
  PENDING:             { bg: "#f1f5f9", color: "#64748b", dot: "#94a3b8",  label: "Pending" },
};

function StatusBadge({ status }: { status: string }) {
  const key = status.toUpperCase().replace(/ /g, "_");
  const style = STATUS_BADGE_STYLES[key];
  if (!style) {
    return <span className={styles.statusError}>{status}</span>;
  }
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: "0.3rem",
      padding: "0.2rem 0.6rem", borderRadius: "999px",
      background: style.bg, color: style.color,
      fontSize: "0.75rem", fontWeight: 600,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: style.dot, flexShrink: 0 }} />
      {style.label}
    </span>
  );
}

// ── Hero stats banner ─────────────────────────────────────────────────────────
interface HeroStat {
  value: unknown; label: string; icon?: string; tooltip?: string;
  valueStyle?: React.CSSProperties;
}

function HeroStatsBanner({ stats, loading }: { stats: HeroStat[]; loading: boolean }) {
  const row1 = stats.slice(0, 5);
  const row2 = stats.slice(5, 9);

  function renderRow(row: HeroStat[]) {
    return row.map((s, i) => (
      <div key={i} className={styles.heroStat} {...(s.tooltip ? { "data-tip": s.tooltip } : {})}>
        {i > 0 && <div className={styles.heroStatDivider} />}
        <div className={styles.heroStatInner}>
          <div className={styles.heroStatValue} style={s.valueStyle}>
            {loading ? "—" : String(s.value ?? "—")}
          </div>
          <div className={styles.heroStatLabel}>
            {s.icon && <span className={styles.heroStatIcon}>{s.icon}</span>}
            {s.label}
          </div>
        </div>
      </div>
    ));
  }

  return (
    <div className={styles.heroBanner}>
      <div className={styles.heroStatsRow}>{renderRow(row1)}</div>
      <div className={styles.heroStatsDividerH} />
      <div className={styles.heroStatsRow}>{renderRow(row2)}</div>
    </div>
  );
}

// ── Section title ─────────────────────────────────────────────────────────────
function SectionTitle({ title, tooltip }: { title: string; tooltip?: string }) {
  return (
    <h3 className={styles.sectionTitle}>
      {title}
      {tooltip && (
        <span className={styles.tooltipAnchor}>
          <span className={styles.tooltipIcon}>ⓘ</span>
          <span className={styles.tooltipBox}>{tooltip}</span>
        </span>
      )}
    </h3>
  );
}

// ── Two-column legend for pie charts with many categories ─────────────────────
function TwoColumnLegend({ payload }: { payload?: Array<{ value: string; color: string }> }) {
  if (!payload?.length) return null;
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "1fr 1fr",
      gap: "0.2rem 1rem",
      fontSize: "0.78rem",
      padding: "0 0.75rem",
      maxHeight: 300,
      overflowY: "auto",
      alignSelf: "center",
    }}>
      {payload.map((entry, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: "0.35rem", minWidth: 0 }}>
          <span style={{
            width: 9, height: 9, borderRadius: 2,
            background: entry.color, flexShrink: 0,
          }} />
          <span style={{ color: "#94a3b8", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {entry.value}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Skeleton helpers ──────────────────────────────────────────────────────────
function SkeletonChart() {
  return <div className={`${styles.skeleton} ${styles.skeletonChart}`} />;
}

function SkeletonRows({ count = 5 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }).map((_, i) => (
        <tr key={i}>
          <td colSpan={9}><div className={`${styles.skeleton} ${styles.skeletonRow}`} /></td>
        </tr>
      ))}
    </>
  );
}

interface Ticket {
  ticket_id: string;
  incident_id: string;
  iflow_id: string;
  error_type: string;
  title: string;
  description: string;
  priority: string;
  status: string;
  created_at: string;
  updated_at: string;
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Dashboard() {
  const tc = useThemeColors();
  const chartColors = [tc.chart1, tc.chart2, tc.chart3, tc.chart4, tc.chart5, tc.chart6, tc.chart7];

  // Chart/KPI data auto-refreshes every 60s; paginated tables do not.
  const chartOpts = { refetchInterval: 60_000, retry: 3, retryDelay: 3_000 } as const;
  const tableOpts = { retry: 2, retryDelay: 2_000, placeholderData: keepPreviousData };

  const [activeTab, setActiveTab] = useState<"overview" | "tickets">("overview");
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const queryClient = useQueryClient();

  // ─ Fetch consolidated dashboard data (charts, KPIs) ─────────────────────────
  const { data: dashData, isLoading: dashLoading } = useQuery({
    queryKey: ["dash-all"],
    queryFn: fetchDashboardAll,
    ...chartOpts,
  });

  // ─ Pagination for Recent Failed Messages ──────────────────────────────────────
  const [failuresPage, setFailuresPage] = useState(1);
  const [failuresPageSize, setFailuresPageSize] = useState(20);
  const { data: failuresData, isLoading: failuresLoading, isFetching: failuresFetching } = useQuery({
    queryKey: ["dash-failures-paginated", failuresPage, failuresPageSize],
    queryFn: () => fetchFailedMessagesPaginated(failuresPage, failuresPageSize),
    ...tableOpts,
  });

  // ─ Pagination for Active Incidents ────────────────────────────────────────────
  const [incidentsPage, setIncidentsPage] = useState(1);
  const [incidentsPageSize, setIncidentsPageSize] = useState(20);
  const { data: incidentsData, isLoading: incidentsLoading, isFetching: incidentsFetching } = useQuery({
    queryKey: ["dash-incidents-paginated", incidentsPage, incidentsPageSize],
    queryFn: () => fetchActiveIncidentsPaginated(incidentsPage, incidentsPageSize),
    ...tableOpts,
  });

  // ─ Tickets (always fetched for tab count badge) ───────────────────────────────
  const { data: ticketsData, isLoading: ticketsLoading } = useQuery({
    queryKey: ["dash-tickets"],
    queryFn: () => fetchTickets(1, 50),
    refetchInterval: 60_000,
    retry: 2,
  });
  const tickets = ((ticketsData as { tickets?: unknown[] } | undefined)?.tickets ?? []) as Ticket[];
  const openTickets = tickets.filter((t) => (t.status || "").toUpperCase() === "OPEN");

  async function handleMarkResolved(ticketId: string, currentStatus: string) {
    setResolvingId(ticketId);
    try {
      if (currentStatus.toUpperCase() === "OPEN") {
        await updateTicket(ticketId, { status: "IN_PROGRESS" });
      }
      await updateTicket(ticketId, { status: "RESOLVED" });
    } catch {
      // swallow — refresh to show actual state
    } finally {
      queryClient.invalidateQueries({ queryKey: ["dash-tickets"] });
      setResolvingId(null);
    }
  }

  // Parse consolidated dashboard data
  const dash = (dashData ?? {}) as Record<string, unknown>;
  const kpi = (dash.kpi ?? {}) as Record<string, unknown>;
  const statusData = (dash.status_breakdown ?? []) as { status: string; count: number }[];
  const errorData = (dash.error_distribution ?? []) as { error_type: string; count: number }[];
  const iflowData = (dash.top_iflows ?? []) as { iflow_name: string; failure_count: number }[];
  const timelineData = (dash.timeline ?? []) as { time: string; count: number }[];
  
  // Extract paginated table data
  const failuresResp = failuresData as PaginatedMessagesResponse | undefined;
  const recentFails = (failuresResp?.messages ?? []) as Record<string, unknown>[];
  const failuresTotalCount = failuresResp?.total_count ?? 0;
  const failuresTotalPages = failuresResp?.total_pages ?? 1;
  const failuresHasNext = failuresResp?.has_next ?? false;
  const failuresHasPrev = failuresResp?.has_previous ?? false;

  const incidentsResp = incidentsData as PaginatedIncidentsResponse | undefined;
  const activeInc = (incidentsResp?.incidents ?? []) as Record<string, unknown>[];
  const incidentsTotalCount = incidentsResp?.total_count ?? 0;
  const incidentsTotalPages = incidentsResp?.total_pages ?? 1;
  const incidentsHasNext = incidentsResp?.has_next ?? false;
  const incidentsHasPrev = incidentsResp?.has_previous ?? false;

  return (
    <div className={styles.page}>
      <h2 className={styles.pageTitle}>SAP CPI Dashboard Overview</h2>

      {/* ── Tab Navigation ── */}
      <div className={styles.tabNav}>
        <button
          className={`${styles.tabNavBtn} ${activeTab === "overview" ? styles.tabNavBtnActive : ""}`}
          onClick={() => setActiveTab("overview")}
        >
          Overview
        </button>
        {/* Tickets tab — hidden */}
        {false && (
          <button
            className={`${styles.tabNavBtn} ${activeTab === "tickets" ? styles.tabNavBtnActive : ""}`}
            onClick={() => setActiveTab("tickets")}
          >
            Tickets
            {openTickets.length > 0 && (
              <span className={styles.tabBadge}>{openTickets.length}</span>
            )}
          </button>
        )}
      </div>

      {/* ══ OVERVIEW TAB ══ */}
      <div style={{ display: activeTab === "overview" ? "contents" : "none" }}>

      {/* ── Hero Stats Banner ── */}
      <HeroStatsBanner
        loading={dashLoading}
        stats={[
          /* row 1 — 5 stats */
          { value: kpi.in_progress,     label: "In Progress",    tooltip: "Incidents currently being analyzed or fixed by pipeline agents" },
          { value: kpi.total_incidents, label: "Total Incidents", tooltip: "All incidents tracked by the auto-remediation pipeline" },
          { value: kpi.pending_approval,label: "Pending Approval",tooltip: "Fixes awaiting manual approval before deployment" },
          { value: kpi.fix_failed,      label: "FIX FAILED",     tooltip: "Fix attempts that failed",       valueStyle: { color: "#fca5a5" } },
          { value: kpi.auto_fixed,      label: "AUTO FIXED",     tooltip: "Incidents fixed automatically",  valueStyle: { color: "#86efac" } },
          /* row 2 — 4 stats */
          { value: kpi.total_failed_messages, label: "Failed Messages",    tooltip: "SAP CPI messages currently in FAILED state" },
          { value: kpi.auto_fix_rate != null ? `${kpi.auto_fix_rate}%` : "—",                             label: "Auto Fix Rate",       tooltip: "Percentage of incidents resolved automatically" },
          { value: kpi.avg_resolution_time_minutes != null ? `${kpi.avg_resolution_time_minutes}` : "—", label: "Avg Resolution Time", tooltip: "Mean time from detection to terminal state" },
          { value: kpi.rca_coverage_percent != null ? `${kpi.rca_coverage_percent}` : "0",               label: "RCA Coverage",        tooltip: "Percentage of incidents that received AI root cause analysis" },
        ]}
      />

      {/* ── Status Breakdown + Error Distribution (side by side) ── */}
      <div className={styles.chartsRow}>
        <div className={styles.chartHalf}>
          <SectionTitle title="Status Breakdown" tooltip="Distribution of incidents by their current pipeline stage (e.g. detecting, fixing, resolved)." />
          {dashLoading ? <SkeletonChart /> : (
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie data={statusData} dataKey="count" nameKey="status" cx="38%" label>
                  {statusData.map((_, i) => (
                    <Cell key={i} fill={chartColors[i % chartColors.length]} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend
                  layout="vertical"
                  align="right"
                  verticalAlign="middle"
                  content={(props) => (
                    <TwoColumnLegend payload={props.payload as Array<{ value: string; color: string }>} />
                  )}
                />
              </PieChart>
            </ResponsiveContainer>
          )}
        </div>

        <div className={styles.chartHalf}>
          <SectionTitle title="Error Distribution" tooltip="Breakdown of incidents by error category (e.g. MAPPING_ERROR, CONNECTION_ERROR, SECURITY_ERROR)." />
          {dashLoading ? <SkeletonChart /> : (
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie data={errorData} dataKey="count" nameKey="error_type" innerRadius="38%" outerRadius="68%">
                  {errorData.map((_, i) => (
                    <Cell key={i} fill={chartColors[i % chartColors.length]} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend layout="vertical" align="right" verticalAlign="middle" />
              </PieChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      {/* ── Top Failing Integration Artifact ── */}
      <div className={styles.chartBlock}>
        <SectionTitle title="Top Failing Integration Artifact" tooltip="Ranks integration flows (iFlows) by total failure count. Helps identify which SAP CPI artifacts need the most attention." />
        {dashLoading ? <SkeletonChart /> : (
          <ResponsiveContainer width="100%" height={320}>
            <BarChart data={iflowData} layout="vertical" margin={{ left: 10, right: 30, top: 5, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" horizontal={false} />
              <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
              <YAxis
                type="category"
                dataKey="iflow_name"
                width={190}
                tick={{ fontSize: 11 }}
                tickFormatter={(v: string) => v.length > 25 ? `${v.slice(0, 23)}…` : v}
              />
              <Tooltip />
              <Bar dataKey="failure_count" name="Failures" fill={tc.primary} radius={[0, 3, 3, 0]} />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── Failure Over Time ── */}
      <div className={styles.chartBlock}>
        <SectionTitle title="Failure Over Time" tooltip="Time-series area chart showing failure count per time interval, as reported by SAP CPI message logs. X-axis represents the timestamp; Y-axis shows the number of failures in that window." />
        {dashLoading ? <SkeletonChart /> : (
          <ResponsiveContainer width="100%" height={300}>
            <AreaChart data={timelineData} margin={{ left: 10, right: 10 }}>
              <defs>
                <linearGradient id="failureAreaGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#f97316" stopOpacity={0.25} />
                  <stop offset="95%" stopColor="#f97316" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={tc.cardBorder} />
              <XAxis dataKey="time" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Area type="monotone" dataKey="count" name="Failures" stroke="#f97316" strokeWidth={2} fill="url(#failureAreaGrad)" dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── Recent Failed Messages with Pagination ── */}
      <div className={styles.tableBlock}>
        <div className={styles.tableBlockHeader}>
          <span className={styles.tableBlockTitle}>Recent Failed Messages ({failuresTotalCount})</span>
          <div className={styles.tableBlockControls}>
            <div className={styles.tableSearch}>
              <span className={styles.tableSearchIcon}>🔍</span>
              <input className={styles.tableSearchInput} placeholder="search message ID / iflow name" />
            </div>
            <select className={styles.tableFilterSelect}>
              <option>Status</option>
            </select>
          </div>
        </div>
        <div className={styles.tableWrapper} style={failuresFetching && !failuresLoading ? { opacity: 0.6, pointerEvents: "none" } : undefined}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th title="Unique message processing log ID from SAP CPI">Message GUID</th>
                <th title="Integration flow that generated this failure">Integration Scenario</th>
                <th title="Current processing status">Status</th>
                <th title="Message processing end time from SAP CPI">Time</th>
                <th title="Time preview">Time Preview</th>
              </tr>
            </thead>
            <tbody>
              {failuresLoading ? (
                <SkeletonRows count={5} />
              ) : recentFails.length === 0 ? (
                <tr><td colSpan={5} className={styles.emptyCell}>No data</td></tr>
              ) : (
                recentFails.map((row, i) => (
                  <tr key={i}>
                    <td className={styles.mono}>{String(row.message_guid ?? "-")}</td>
                    <td>{String(row.iflow_name ?? "-")}</td>
                    <td><StatusBadge status={String(row.status ?? "")} /></td>
                    <td>{formatISODate(row.log_end as string)}</td>
                    <td className={styles.mono} style={{ color: "#94a3b8" }}>-</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        {!failuresLoading && recentFails.length > 0 && (
          <Pagination
            currentPage={failuresPage}
            totalPages={failuresTotalPages}
            pageSize={failuresPageSize}
            totalCount={failuresTotalCount}
            hasNextPage={failuresHasNext}
            hasPreviousPage={failuresHasPrev}
            onPreviousClick={() => setFailuresPage((p) => Math.max(1, p - 1))}
            onNextClick={() => setFailuresPage((p) => p + 1)}
            onPageSizeChange={(s) => { setFailuresPageSize(s); setFailuresPage(1); }}
          />
        )}
      </div>

      {/* ── Active Incidents with Pagination ── */}
      <div className={styles.tableBlock}>
        <div className={styles.tableBlockHeader}>
          <span className={styles.tableBlockTitle}>Active Incidents ({incidentsTotalCount})</span>
          <div className={styles.tableBlockControls}>
            <div className={styles.tableSearch}>
              <span className={styles.tableSearchIcon}>🔍</span>
              <input className={styles.tableSearchInput} placeholder="search message ID / iflow name" />
            </div>
            <select className={styles.tableFilterSelect}>
              <option>Status</option>
            </select>
          </div>
        </div>
        <div className={styles.tableWrapper} style={incidentsFetching && !incidentsLoading ? { opacity: 0.6, pointerEvents: "none" } : undefined}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th title="SAP CPI message processing log identifier">Message GUID</th>
                <th title="Integration flow associated with this incident">Integration Scenario</th>
                <th title="Classified error category (e.g. MAPPING_ERROR, CONNECTION_ERROR)">Error Type</th>
                <th title="Current pipeline stage for this incident">Status</th>
                <th title="When this incident was first detected">Created At</th>
                <th title="Most recent occurrence of this error pattern">Last Seen</th>
                <th title="Number of times this error pattern has been detected">Occurrences</th>
                <th title="AI model confidence in the root cause analysis (0–1 scale)">RCA Confidence</th>
              </tr>
            </thead>
            <tbody>
              {incidentsLoading ? (
                <SkeletonRows count={5} />
              ) : activeInc.length === 0 ? (
                <tr><td colSpan={8} className={styles.emptyCell}>No data</td></tr>
              ) : (
                activeInc.map((row, i) => {
                  const stateClass = INCIDENT_STATE[String(row.status ?? "")] ?? styles.stateNone;
                  return (
                    <tr key={i}>
                      <td className={styles.mono}>{String(row.message_guid ?? "-")}</td>
                      <td>{String(row.iflow_id ?? "-")}</td>
                      <td>{String(row.error_type ?? "-")}</td>
                      <td><span className={`${styles.statusBadge} ${stateClass}`}>{String(row.status ?? "-")}</span></td>
                      <td>{formatISODate(row.created_at as string)}</td>
                      <td>{formatISODate(row.last_seen as string)}</td>
                      <td>{String(row.occurrence_count ?? "-")}</td>
                      <td>{String(row.rca_confidence ?? "-")}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        {!incidentsLoading && activeInc.length > 0 && (
          <Pagination
            currentPage={incidentsPage}
            totalPages={incidentsTotalPages}
            pageSize={incidentsPageSize}
            totalCount={incidentsTotalCount}
            hasNextPage={incidentsHasNext}
            hasPreviousPage={incidentsHasPrev}
            onPreviousClick={() => setIncidentsPage((p) => Math.max(1, p - 1))}
            onNextClick={() => setIncidentsPage((p) => p + 1)}
            onPageSizeChange={(s) => { setIncidentsPageSize(s); setIncidentsPage(1); }}
          />
        )}
      </div>

      </div>{/* end overview tab */}

      {/* ══ TICKETS TAB ══ — hidden */}
      {false && activeTab === "tickets" && (
        <div className={styles.tableBlock}>
          <div className={styles.ticketsTabHeader}>
            <SectionTitle title={`Escalation Tickets (${tickets.length})`} />
            <div className={styles.ticketKpiRow}>
              <span className={styles.ticketKpi} style={{ color: "#dc2626" }}>
                <strong>{openTickets.length}</strong> Open
              </span>
              <span className={styles.ticketKpi} style={{ color: "#2563eb" }}>
                <strong>{tickets.filter((t) => (t.status || "").toUpperCase() === "IN_PROGRESS").length}</strong> In Progress
              </span>
              <span className={styles.ticketKpi} style={{ color: "#16a34a" }}>
                <strong>{tickets.filter((t) => (t.status || "").toUpperCase() === "RESOLVED").length}</strong> Resolved
              </span>
            </div>
          </div>

          {ticketsLoading ? (
            <div className={styles.tableWrapper}>
              <table className={styles.table}>
                <tbody><SkeletonRows count={5} /></tbody>
              </table>
            </div>
          ) : tickets.length === 0 ? (
            <div className={styles.emptyCell} style={{ padding: "2.5rem", textAlign: "center" }}>
              No escalation tickets yet. Tickets are created automatically when an iFlow fix fails.
            </div>
          ) : (
            <div className={styles.tableWrapper}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Ticket ID</th>
                    <th>iFlow</th>
                    <th>Error Type</th>
                    <th>RCA Summary</th>
                    <th>Priority</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {tickets.map((ticket) => {
                    const status = (ticket.status || "OPEN").toUpperCase();
                    const priority = (ticket.priority || "MEDIUM").toUpperCase();
                    const statusClass =
                      status === "RESOLVED"    ? styles.stateSuccess :
                      status === "IN_PROGRESS" ? styles.stateWarning :
                      styles.stateError;
                    const rcaSummary = (() => {
                      const m = (ticket.description || "").match(/Proposed fix:\s*([^\n]+)/);
                      return m ? m[1].trim().slice(0, 120) : (ticket.description || "").slice(0, 120);
                    })();
                    const isResolving = resolvingId === ticket.ticket_id;
                    return (
                      <tr key={ticket.ticket_id}>
                        <td className={styles.mono}>{ticket.ticket_id.slice(0, 8)}…</td>
                        <td>{ticket.iflow_id || "-"}</td>
                        <td>{ticket.error_type || "-"}</td>
                        <td className={styles.errorPreview} style={{ color: "#334155", maxWidth: 220 }} title={rcaSummary}>
                          {rcaSummary || "-"}
                        </td>
                        <td>
                          <span className={`${styles.statusBadge} ${priority === "HIGH" ? styles.stateError : styles.stateWarning}`}>
                            {priority}
                          </span>
                        </td>
                        <td>
                          <span className={`${styles.statusBadge} ${statusClass}`}>{status}</span>
                        </td>
                        <td>{formatISODate(ticket.created_at)}</td>
                        <td>
                          <div className={styles.ticketActions}>
                            {status !== "RESOLVED" && (
                              <button
                                className={styles.resolveBtn}
                                disabled={isResolving}
                                onClick={() => handleMarkResolved(ticket.ticket_id, ticket.status)}
                                title="Mark this ticket as resolved"
                              >
                                {isResolving ? "…" : "Mark Resolved"}
                              </button>
                            )}
                            {status === "RESOLVED" && (
                              <span style={{ color: "#16a34a", fontSize: "0.78rem" }}>✓ Resolved</span>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
