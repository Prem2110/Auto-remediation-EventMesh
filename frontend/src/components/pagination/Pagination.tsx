import React from "react";
import styles from "./pagination.module.css";

export interface PaginationProps {
  currentPage: number;
  totalPages: number;
  pageSize: number;
  totalCount: number;
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  onPreviousClick: () => void;
  onNextClick: () => void;
  onPageSizeChange?: (size: number) => void;
}

export const Pagination: React.FC<PaginationProps> = ({
  currentPage,
  totalPages,
  pageSize,
  totalCount,
  hasNextPage,
  hasPreviousPage,
  onPreviousClick,
  onNextClick,
  onPageSizeChange,
}) => {
  const startItem = (currentPage - 1) * pageSize + 1;
  const endItem = Math.min(currentPage * pageSize, totalCount);

  return (
    <div className={styles.paginationContainer}>
      <div className={styles.info}>
        Showing {startItem} to {endItem} of {totalCount} items
      </div>

      <div className={styles.controls}>
        <button
          className={styles.btn}
          onClick={onPreviousClick}
          disabled={!hasPreviousPage}
          type="button"
        >
          ← Previous
        </button>

        <div className={styles.pageInfo}>
          Page {currentPage} of {totalPages}
        </div>

        <button
          className={styles.btn}
          onClick={onNextClick}
          disabled={!hasNextPage}
          type="button"
        >
          Next →
        </button>

        {onPageSizeChange && (
          <select
            className={styles.pageSizeSelect}
            value={pageSize}
            onChange={(e) => onPageSizeChange(parseInt(e.target.value, 10))}
          >
            <option value={10}>10 per page</option>
            <option value={20}>20 per page</option>
            <option value={50}>50 per page</option>
            <option value={100}>100 per page</option>
          </select>
        )}
      </div>
    </div>
  );
};

export default Pagination;
