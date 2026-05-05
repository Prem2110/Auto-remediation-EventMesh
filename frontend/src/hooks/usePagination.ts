import { useState, useCallback, useEffect } from "react";

export interface PaginationState {
  currentPage: number;
  pageSize: number;
  totalCount: number;
  totalPages: number;
  hasNextPage: boolean;
  hasPreviousPage: boolean;
}

export interface UsePaginationReturn extends PaginationState {
  goToPage: (page: number) => void;
  nextPage: () => void;
  previousPage: () => void;
  setPageSize: (size: number) => void;
  setTotalCount: (count: number) => void;
}

export function usePagination(initialPageSize = 20, totalCount: number = 0): UsePaginationReturn {
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSizeState] = useState(initialPageSize);
  const [storedTotalCount, setStoredTotalCount] = useState(totalCount);

  // Update stored count when prop changes
  useEffect(() => {
    setStoredTotalCount(totalCount);
  }, [totalCount]);

  const calculatedTotalPages = Math.ceil(storedTotalCount / pageSize) || 1;
  const hasNextPage = currentPage < calculatedTotalPages;
  const hasPreviousPage = currentPage > 1;

  // Clamp current page if it exceeds total pages
  useEffect(() => {
    if (currentPage > calculatedTotalPages && calculatedTotalPages > 0) {
      setCurrentPage(calculatedTotalPages);
    }
  }, [calculatedTotalPages, currentPage]);

  const goToPage = useCallback((page: number) => {
    const validPage = Math.max(1, Math.min(page, calculatedTotalPages));
    setCurrentPage(validPage);
  }, [calculatedTotalPages]);

  const nextPage = useCallback(() => {
    if (hasNextPage) {
      setCurrentPage((p) => p + 1);
    }
  }, [hasNextPage]);

  const previousPage = useCallback(() => {
    if (hasPreviousPage) {
      setCurrentPage((p) => Math.max(1, p - 1));
    }
  }, [hasPreviousPage]);

  const setPageSize = useCallback((size: number) => {
    setPageSizeState(Math.max(1, Math.min(size, 500)));
    setCurrentPage(1); // Reset to first page when changing page size
  }, []);

  const setTotalCount = useCallback((count: number) => {
    setStoredTotalCount(count);
  }, []);

  return {
    currentPage,
    pageSize,
    totalCount: storedTotalCount,
    totalPages: calculatedTotalPages,
    hasNextPage,
    hasPreviousPage,
    goToPage,
    nextPage,
    previousPage,
    setPageSize,
    setTotalCount,
  };
}
