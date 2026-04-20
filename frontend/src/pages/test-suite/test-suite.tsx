import { useState, useEffect, useCallback } from "react";
import { fetchTestSuiteLogs } from "../../services/api.ts";
import type { ITestLog, ITestExecution } from "../../types/index.ts";
import styles from "./test-suite.module.css";

function determineStatus(executions: ITestExecution[]): string {
  if (!executions || executions.length === 0) return "-";
  const hasFail = executions.some((e) =>
    e.message_logs ? /Exception|error|400|429|500/.test(e.message_logs) : false
  );
  return hasFail ? "Fail" : "Success";
}

function StatusBadge({ status }: { status: string }) {
  if (status === "Success")
    return <span className={`${styles.statusBadge} ${styles.badgeSuccess}`}>✓ Success</span>;
  if (status === "Fail")
    return <span className={`${styles.statusBadge} ${styles.badgeFail}`}>✗ Fail</span>;
  return <span className={`${styles.statusBadge} ${styles.badgePending}`}>{status}</span>;
}

export default function TestSuite() {
  const [tests, setTests] = useState<ITestLog[]>([]);
  const [filtered, setFiltered] = useState<ITestLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [users, setUsers] = useState<string[]>([]);

  const [search, setSearch]       = useState("");
  const [userFilter, setUserFilter]     = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [dateFrom, setDateFrom]   = useState("");
  const [dateTo, setDateTo]       = useState("");

  const applyFilters = useCallback(
    (data: ITestLog[], sq: string, uf: string, sf: string, df: string, dt: string) => {
      let r = data;
      if (sq) { const q = sq.toLowerCase(); r = r.filter((t) => t.initiatedUser.toLowerCase().includes(q) || t.prompt.toLowerCase().includes(q)); }
      if (uf) r = r.filter((t) => t.initiatedUser === uf);
      if (sf) r = r.filter((t) => t.execution === sf);
      if (df || dt) {
        const from = df ? new Date(df).setHours(0, 0, 0, 0)       : null;
        const to   = dt ? new Date(dt).setHours(23, 59, 59, 999)   : null;
        r = r.filter((t) => {
          const ts = new Date(t.initiatedTime.replace(" ", "T")).getTime();
          return (!from || ts >= from) && (!to || ts <= to);
        });
      }
      setFiltered(r);
    }, []
  );

  useEffect(() => {
    async function load() {
      try {
        const data = await fetchTestSuiteLogs();
        const raw = (data.ts_logs || []) as Record<string, unknown>[];
        const mapped: ITestLog[] = raw.map((t) => ({
          id: String(t.id || ""),
          initiatedUser: String(t.user || ""),
          initiatedTime: String(t.created_at || ""),
          prompt: String(t.prompt || ""),
          execution: determineStatus((t.executions || []) as ITestExecution[]),
          payload: t.payload,
          operation: String(t.operation || ""),
          executions: (t.executions || []) as ITestExecution[],
        }));
        setTests(mapped);
        applyFilters(mapped, "", "", "", "", "");
        setUsers([...new Set(mapped.map((t) => t.initiatedUser))]);
      } catch { /* leave empty */ } finally { setLoading(false); }
    }
    load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function handleDownload(test: ITestLog) {
    const lines = [
      `Test Suite Execution Report`,
      `Test ID: ${test.id}`,
      `User: ${test.initiatedUser}`,
      `Created At: ${test.initiatedTime}`,
      `Prompt: ${test.prompt}`,
      `Status: ${test.execution}`,
      "",
      "Executions:",
      ...(test.executions || []).map((exec, i) =>
        `  #${i + 1}: ${exec.http_method || ""} - ${exec.message_id || ""} - ${exec.message_logs ? "Fail" : "Success"}`),
    ].join("\n");
    const blob = new Blob([lines], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `TestSuite_${test.id}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  const successCount = filtered.filter((t) => t.execution === "Success").length;
  const failCount    = filtered.filter((t) => t.execution === "Fail").length;

  return (
    <div className={styles.page}>
      {/* Header */}
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.pageTitle}>Test Suite</h1>
          <p className={styles.pageMeta}>
            {filtered.length} results · <span style={{ color: "#16a34a" }}>✓ {successCount} passed</span>
            {" · "}<span style={{ color: "#dc2626" }}>✗ {failCount} failed</span>
          </p>
        </div>
      </div>

      {/* Filter bar */}
      <div className={styles.filterBar}>
        <input
          className={styles.filterInput}
          placeholder="🔍 Search user or prompt…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select className={styles.filterSelect} value={userFilter}
          onChange={(e) => setUserFilter(e.target.value)}>
          <option value="">All Users</option>
          {users.map((u) => <option key={u} value={u}>{u}</option>)}
        </select>
        <select className={styles.filterSelect} value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">All Statuses</option>
          <option value="Success">Success</option>
          <option value="Fail">Fail</option>
        </select>
        <input type="date" className={styles.filterDate} value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)} title="From" />
        <input type="date" className={styles.filterDate} value={dateTo}
          onChange={(e) => setDateTo(e.target.value)} title="To" />
        <button className={`${styles.filterBtn} ${styles.goBtn}`}
          onClick={() => applyFilters(tests, search, userFilter, statusFilter, dateFrom, dateTo)}>
          Apply
        </button>
        <button className={`${styles.filterBtn} ${styles.clearBtn}`}
          onClick={() => { setSearch(""); setUserFilter(""); setStatusFilter(""); setDateFrom(""); setDateTo(""); setFiltered(tests); }}>
          Clear
        </button>
      </div>

      {/* Table */}
      {loading ? (
        <div className={styles.centered}>
          <div className={styles.spinner} />
          <span>Loading test results…</span>
        </div>
      ) : (
        <div className={styles.tableWrapper}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>User</th>
                <th>Time</th>
                <th>Prompt</th>
                <th>Status</th>
                <th>Operation</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((test) => (
                <tr
                  key={test.id}
                  className={
                    test.execution === "Success" ? styles.rowSuccess
                    : test.execution === "Fail"  ? styles.rowFail
                    : styles.rowPending
                  }
                >
                  <td className={styles.nowrap}>
                    <strong>{test.initiatedUser}</strong>
                  </td>
                  <td className={styles.nowrap}>{test.initiatedTime}</td>
                  <td>
                    <span className={styles.promptCell} title={test.prompt}>{test.prompt}</span>
                  </td>
                  <td><StatusBadge status={test.execution} /></td>
                  <td>{test.operation || "—"}</td>
                  <td>
                    <button className={styles.downloadBtn} onClick={() => handleDownload(test)} title="Download report">
                      ⬇
                    </button>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={6} className={styles.emptyCell}>
                    <div style={{ fontSize: "2rem" }}>📋</div>
                    <div>No test results found</div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
