import React from "react";
import styles from "./pagination.module.css";
import type { SortOrder } from "../../hooks/useServerTable";

// ─── Pagination bar ───────────────────────────────────────────────────────────

export interface PaginationProps {
  currentPage: number;
  totalPages: number;
  pageSize: number;
  totalCount: number;
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  onPageChange?: (page: number) => void;
  onPreviousClick?: () => void;
  onNextClick?: () => void;
  onPageSizeChange?: (size: number) => void;
  label?: string;
}

export const Pagination: React.FC<PaginationProps> = ({
  currentPage,
  totalPages,
  pageSize,
  totalCount,
  hasNextPage,
  hasPreviousPage,
  onPageChange,
  onPreviousClick,
  onNextClick,
  onPageSizeChange,
  label = "items",
}) => {
  const startItem = totalCount > 0 ? (currentPage - 1) * pageSize + 1 : 0;
  const endItem   = Math.min(currentPage * pageSize, totalCount);

  const goTo = onPageChange ?? (() => {});
  const prev = onPreviousClick ?? (() => goTo(Math.max(1, currentPage - 1)));
  const next = onNextClick     ?? (() => goTo(Math.min(totalPages, currentPage + 1)));

  const pageNumbers = buildPageNumbers(currentPage, totalPages);

  return (
    <div className={styles.paginationWrapper}>
      <div className={styles.paginationContainer}>
        <div className={styles.info}>
          {totalCount > 0
            ? `${startItem}–${endItem} of ${totalCount} ${label}`
            : `0 ${label}`}
        </div>
      </div>

      <div className={styles.controls}>
        <button className={styles.btn} onClick={() => goTo(1)} disabled={!hasPreviousPage} type="button">«</button>
        <button className={styles.btn} onClick={prev} disabled={!hasPreviousPage} type="button">‹ Prev</button>

        {pageNumbers.map((p, i) =>
          p === "…" ? (
            <span key={`dots-${i}`} className={styles.dots}>…</span>
          ) : (
            <button
              key={p}
              type="button"
              className={`${styles.btn} ${currentPage === p ? styles.btnCurrent : ""}`}
              onClick={() => goTo(p as number)}
            >
              {p}
            </button>
          )
        )}

        <button className={styles.btn} onClick={next} disabled={!hasNextPage} type="button">Next ›</button>
        <button className={styles.btn} onClick={() => goTo(totalPages)} disabled={!hasNextPage} type="button">»</button>

        {onPageSizeChange && (
          <select
            className={styles.pageSizeSelect}
            value={pageSize}
            onChange={(e) => onPageSizeChange(parseInt(e.target.value, 10))}
          >
            {[10, 20, 50, 100].map((n) => (
              <option key={n} value={n}>{n} / page</option>
            ))}
          </select>
        )}
      </div>
    </div>
  );
};

// ─── Sortable column header ───────────────────────────────────────────────────

export interface SortableHeaderProps {
  label: string;
  column: string;
  currentSort: string;
  currentOrder: SortOrder;
  onSort: (column: string) => void;
  className?: string;
}

export const SortableHeader: React.FC<SortableHeaderProps> = ({
  label, column, currentSort, currentOrder, onSort, className,
}) => {
  const active = currentSort === column;
  const arrow  = active ? (currentOrder === "asc" ? " ↑" : " ↓") : " ↕";
  return (
    <th
      className={`${styles.sortableHeader} ${active ? styles.sortableHeaderActive : ""} ${className ?? ""}`}
      onClick={() => onSort(column)}
      style={{ cursor: "pointer", userSelect: "none" }}
    >
      {label}<span className={styles.sortArrow}>{arrow}</span>
    </th>
  );
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function buildPageNumbers(current: number, total: number): (number | "…")[] {
  return Array.from({ length: total }, (_, i) => i + 1)
    .filter((p) => p === 1 || p === total || Math.abs(p - current) <= 1)
    .reduce<(number | "…")[]>((acc, p, i, arr) => {
      if (i > 0 && (p as number) - (arr[i - 1] as number) > 1) acc.push("…");
      acc.push(p);
      return acc;
    }, []);
}

export default Pagination;
