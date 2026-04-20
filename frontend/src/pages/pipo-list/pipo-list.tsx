import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchPipoDetails } from "../../services/api.ts";
import type { IPipoDetail } from "../../types/index.ts";
import styles from "./pipo-list.module.css";

export default function PipoList() {
  const [search, setSearch] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["pipo-details"],
    queryFn: fetchPipoDetails,
    staleTime: 60_000,
  });

  const items = useMemo(() => {
    const all = (data || []) as IPipoDetail[];
    if (!search.trim()) return all;
    const q = search.toLowerCase();
    return all.filter(
      (item) =>
        item.name?.toLowerCase().includes(q) ||
        item.issue?.toLowerCase().includes(q)
    );
  }, [data, search]);

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>PIPO Connections</h1>
        <p className={styles.pageMeta}>{items.length} connection{items.length !== 1 ? "s" : ""}</p>
      </div>

      <div className={styles.searchBar}>
        <input
          className={styles.searchInput}
          placeholder="🔍 Search by name or issue…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {isLoading ? (
        <div className={styles.centered}>
          <div className={styles.spinner} />
          <span>Loading connections…</span>
        </div>
      ) : (
        <div className={styles.tableWrapper}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Issue</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, idx) => (
                <tr key={idx}>
                  <td><strong>{item.name || "—"}</strong></td>
                  <td>{item.issue || "—"}</td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={2} className={styles.emptyCell}>
                    <div style={{ fontSize: "2rem" }}>🔌</div>
                    <div>{search ? "No results match your search." : "No PIPO details found."}</div>
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
