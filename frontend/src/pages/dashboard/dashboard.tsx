import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { useState } from "react";
import {
  PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer,
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  LineChart, Line,
} from "recharts";
import {
  fetchDashboardAll,
  fetchFailedMessagesPaginated,
  fetchActiveIncidentsPaginated,
  type PaginatedMessagesResponse,
  type PaginatedIncidentsResponse,
} from "../../services/api.ts";
import Pagination from "../../components/pagination/Pagination";
import styles from "./dashboard.module.css";

// ── Colour palettes ────────────────────────────────────────────────────────────
const CHART_COLORS = ["#ff6b6b", "#4dabf7", "#ffd43b", "#69db7c", "#845ef7", "#f06595", "#74c0fc"];

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

// ── KPI card ──────────────────────────────────────────────────────────────────
function KpiCard({ header, subheader, value, unit, indicator, valueColor, tooltip }: {
  header: string; subheader?: string; value: unknown;
  unit?: string; indicator?: "Up" | "Down"; valueColor?: "Good" | "Critical"; tooltip?: string;
}) {
  const colorClass =
    valueColor === "Good"     ? styles.valueGood :
    valueColor === "Critical" ? styles.valueCritical : "";
  const arrow = indicator === "Up" ? " ↑" : indicator === "Down" ? " ↓" : "";

  return (
    <div className={styles.kpiCard} {...(tooltip ? { "data-tip": tooltip } : {})}>
      <div className={styles.kpiHeader}>{header}</div>
      {subheader && <div className={styles.kpiSub}>{subheader}</div>}
      <div className={`${styles.kpiValue} ${colorClass}`}>
        {String(value ?? "-")}{unit ? ` ${unit}` : ""}{arrow}
      </div>
    </div>
  );
}

// ── Section title ─────────────────────────────────────────────────────────────
function SectionTitle({ title }: { title: string }) {
  return <h3 className={styles.sectionTitle}>{title}</h3>;
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

// ── Main component ────────────────────────────────────────────────────────────
export default function Dashboard() {
  // Chart/KPI data auto-refreshes every 60s; paginated tables do not.
  const chartOpts = { refetchInterval: 60_000, retry: 3, retryDelay: 3_000 } as const;
  const tableOpts = { retry: 2, retryDelay: 2_000, placeholderData: keepPreviousData };

  // ─ Fetch consolidated dashboard data (charts, KPIs, AEM stats) ────────────────
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

  // Parse consolidated dashboard data
  const dash = (dashData ?? {}) as Record<string, unknown>;
  const kpi = (dash.kpi ?? {}) as Record<string, unknown>;
  const statusData = (dash.status_breakdown ?? []) as { status: string; count: number }[];
  const errorData = (dash.error_distribution ?? []) as { error_type: string; count: number }[];
  const iflowData = (dash.top_iflows ?? []) as { iflow_name: string; failure_count: number }[];
  const timelineData = (dash.timeline ?? []) as { time: string; count: number }[];
  
  const aem = (dash.aem ?? {}) as Record<string, unknown>;
  const aemEnabled = !aem.warning;
  const aemSempError = aem.semp_error as string | null;
  const aemQueues = (aem.queues ?? {}) as Record<string, { queue_depth: number; messages_retrieved: number }>;
  const aemQueueDepth = Object.values(aemQueues).reduce((s, q) => s + (q.queue_depth ?? 0), 0);
  const aemStageRaw = (aem.stage_counts ?? {}) as Record<string, number>;
  const aemStageData = Object.entries(aemStageRaw).map(([stage, count]) => ({ stage, count }));

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
      <h2 className={styles.pageTitle}>Smart Monitoring</h2>

      {/* ── AEM Status Banner ── */}
      {!dashLoading && (
        <div className={styles.aemBanner} data-enabled={aemEnabled}>
          <span className={styles.aemDot} />
          <span className={styles.aemLabel} data-tip={aemEnabled ? "Advanced Event Mesh is connected — incidents sourced via Solace pub/sub messaging" : "AEM is disabled — using direct SAP CPI polling"}>
            {aemEnabled ? "AEM Connected" : "AEM Disabled"}
          </span>
          {aemEnabled && (
            <>
              <span className={styles.aemSep}>|</span>
              <span className={styles.aemStat} data-tip="Total messages waiting across all AEM queues for processing">Queue depth: <strong>{aemQueueDepth}</strong></span>
              {Object.entries(aemQueues).map(([name, q]) => (
                <span key={name} className={styles.aemStat} data-tip={`Queue "${name}": ${q.queue_depth} waiting, ${q.messages_retrieved} retrieved this session`}>
                  {name}: <strong>{q.queue_depth}</strong> queued · <strong>{q.messages_retrieved}</strong> retrieved
                </span>
              ))}
              {aemSempError && (
                <span className={styles.aemError} data-tip="SEMP (Solace Element Management Protocol) REST API error — queue statistics may be inaccurate">SEMP: {aemSempError}</span>
              )}
            </>
          )}
        </div>
      )}

      {/* ── KPI Cards ── */}
      <div className={styles.kpiRow}>
        {dashLoading ? (
          Array.from({ length: 9 }).map((_, i) => (
            <div key={i} className={styles.kpiCard}>
              <div className={`${styles.skeleton}`} style={{ height: "0.75rem", width: "70%" }} />
              <div className={`${styles.skeleton} ${styles.skeletonKpiValue}`} />
            </div>
          ))
        ) : (
          <>
            <KpiCard header="Failed Messages" subheader="Live" value={kpi.total_failed_messages} tooltip="SAP CPI messages currently in FAILED state, polled live from the message processing log" />
            <KpiCard header="Total Incidents" value={kpi.total_incidents} tooltip="All incidents tracked by the auto-remediation pipeline, including resolved and active" />
            <KpiCard header="In Progress" value={kpi.in_progress} tooltip="Incidents currently being analyzed or fixed by pipeline agents" />
            <KpiCard header="Fix Failed" value={kpi.fix_failed} indicator="Down" valueColor="Critical" tooltip="Incidents where the automated fix failed — manual review required" />
            <KpiCard header="Auto Fixed" value={kpi.auto_fixed} indicator="Up" valueColor="Good" tooltip="Incidents resolved automatically without any human intervention" />
            <KpiCard header="Pending Approval" value={kpi.pending_approval} tooltip="Fixes generated but awaiting manual approval before deployment to production" />
            <KpiCard header="Auto Fix Rate" value={kpi.auto_fix_rate} unit="%" tooltip="Percentage of incidents resolved automatically vs all closed incidents" />
            <KpiCard header="Avg Resolution Time" subheader="minutes" value={kpi.avg_resolution_time_minutes} unit="min" tooltip="Mean time from incident detection to terminal state (auto-fixed or failed)" />
            <KpiCard header="RCA Coverage" value={kpi.rca_coverage_percent} unit="%" indicator="Up" valueColor="Good" tooltip="Percentage of incidents that received AI-powered root cause analysis" />
          </>
        )}
      </div>

      {/* ── Status Breakdown ── */}
      <div className={styles.chartBlock}>
        <SectionTitle title="Status Breakdown" />
        {dashLoading ? <SkeletonChart /> : (
          <ResponsiveContainer width="100%" height={320}>
            <PieChart>
              <Pie data={statusData} dataKey="count" nameKey="status" cx="35%" label>
                {statusData.map((_, i) => (
                  <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
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

      {/* ── Error Distribution + Top Failing iFlows ── */}
      <div className={styles.chartsRow}>
        <div className={styles.chartHalf}>
          <SectionTitle title="Error Distribution" />
          {dashLoading ? <SkeletonChart /> : (
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie data={errorData} dataKey="count" nameKey="error_type" innerRadius="40%" outerRadius="70%" label>
                  {errorData.map((_, i) => (
                    <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          )}
        </div>

        <div className={styles.chartHalf}>
          <SectionTitle title="Top Failing iFlows" />
          {dashLoading ? <SkeletonChart /> : (
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={iflowData} layout="vertical" margin={{ left: 10, right: 10 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis type="number" allowDecimals={false} />
                <YAxis
                  type="category"
                  dataKey="iflow_name"
                  width={175}
                  tick={{ fontSize: 11 }}
                  tickFormatter={(v: string) => v.length > 22 ? `${v.slice(0, 20)}…` : v}
                />
                <Tooltip />
                <Bar dataKey="failure_count" name="Failures" fill="#4dabf7" />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      {/* ── Failures Over Time ── */}
      <div className={styles.chartBlock}>
        <SectionTitle title="Failures Over Time" />
        {dashLoading ? <SkeletonChart /> : (
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={timelineData} margin={{ left: 10, right: 10 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="time" tick={{ fontSize: 11 }} />
              <YAxis />
              <Tooltip />
              <Line type="monotone" dataKey="count" name="Failures" stroke="#ff6b6b" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── AEM Pipeline Stage Counts ── */}
      {aemEnabled && (
        <div className={styles.chartBlock}>
          <SectionTitle title="AEM Pipeline Stage Counts" />
          {dashLoading ? <SkeletonChart /> : aemStageData.length === 0 ? (
            <div className={styles.emptyCell}>No stage data — pipeline not yet running</div>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={aemStageData} margin={{ left: 10, right: 10 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="stage" tick={{ fontSize: 12 }} />
                <YAxis allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="count" name="Incidents" fill="#845ef7" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      )}

      {/* ── Recent Failed Messages with Pagination ── */}
      <div className={styles.tableBlock}>
        <SectionTitle title="Recent Failed Messages" />
        <div className={styles.tableWrapper} style={failuresFetching && !failuresLoading ? { opacity: 0.6, pointerEvents: "none" } : undefined}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th title="Unique message processing log ID from SAP CPI">Message GUID</th>
                <th title="Integration flow that generated this failure">iFlow Name</th>
                <th title="Current processing status">Status</th>
                <th title="Message processing end time from SAP CPI">Time</th>
                <th title="Truncated error message from the CPI processing log">Error Preview</th>
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
                    <td><span className={styles.statusError}>{String(row.status ?? "-")}</span></td>
                    <td>{formatISODate(row.log_end as string)}</td>
                    <td className={styles.errorPreview}>{String(row.error_preview ?? "-")}</td>
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
        <SectionTitle title="Active Incidents" />
        <div className={styles.tableWrapper} style={incidentsFetching && !incidentsLoading ? { opacity: 0.6, pointerEvents: "none" } : undefined}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th title="Auto-generated UUID for this remediation incident">Incident ID</th>
                <th title="SAP CPI message processing log identifier">Message GUID</th>
                <th title="Integration flow associated with this incident">iFlow</th>
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
                <tr><td colSpan={9} className={styles.emptyCell}>No data</td></tr>
              ) : (
                activeInc.map((row, i) => {
                  const stateClass = INCIDENT_STATE[String(row.status ?? "")] ?? styles.stateNone;
                  return (
                    <tr key={i}>
                      <td className={styles.mono}>{String(row.incident_id ?? "-")}</td>
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
    </div>
  );
}
