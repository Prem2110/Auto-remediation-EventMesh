import { useState, useCallback, useEffect } from "react";

export type SortOrder = "asc" | "desc";

export interface ServerTableState {
  page: number;
  pageSize: number;
  sortBy: string;
  sortOrder: SortOrder;
  filters: Record<string, string>;
}

export interface ServerTableResponse<T> {
  items?: T[];
  // entity-specific aliases (accepted alongside items)
  messages?: T[];
  tickets?: T[];
  incidents?: T[];
  pending?: T[];
  total?: number;
  total_count?: number;
  total_pages?: number;
  has_next?: boolean;
  has_previous?: boolean;
  page?: number;
  page_size?: number;
  sort_by?: string;
  sort_order?: string;
}

export interface UseServerTableReturn<T> {
  state: ServerTableState;
  // data helpers
  items: T[];
  total: number;
  totalPages: number;
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  // actions
  setPage: (page: number) => void;
  nextPage: () => void;
  previousPage: () => void;
  setPageSize: (size: number) => void;
  setSort: (column: string) => void;
  setFilter: (key: string, value: string) => void;
  clearFilters: () => void;
  // helpers for useQuery
  queryKey: (string | number | Record<string, string>)[];
  toParams: () => URLSearchParams;
}

export function resolveItems<T>(response: ServerTableResponse<T>): T[] {
  return (
    response.items ??
    response.messages ??
    response.tickets ??
    response.incidents ??
    response.pending ??
    []
  ) as T[];
}

export function resolveTotal<T>(response: ServerTableResponse<T>): number {
  return response.total ?? response.total_count ?? 0;
}

export function resolveTotalPages<T>(response: ServerTableResponse<T>, pageSize: number): number {
  const total = resolveTotal(response);
  return response.total_pages ?? (total > 0 ? Math.ceil(total / pageSize) : 1);
}

export function useServerTable<T = unknown>(
  baseQueryKey: string,
  options: {
    defaultPageSize?: number;
    defaultSortBy?: string;
    defaultSortOrder?: SortOrder;
    defaultFilters?: Record<string, string>;
  } = {}
): UseServerTableReturn<T> {
  const {
    defaultPageSize   = 20,
    defaultSortBy     = "created_at",
    defaultSortOrder  = "desc",
    defaultFilters    = {},
  } = options;

  const [page, setPageState]     = useState(1);
  const [pageSize, setPageSizeState] = useState(defaultPageSize);
  const [sortBy, setSortBy]      = useState(defaultSortBy);
  const [sortOrder, setSortOrder] = useState<SortOrder>(defaultSortOrder);
  const [filters, setFilters]    = useState<Record<string, string>>(defaultFilters);

  // Response metadata (populated via updateFromResponse)
  const [total, setTotal]             = useState(0);
  const [totalPages, setTotalPages]   = useState(1);
  const [hasNext, setHasNext]         = useState(false);
  const [hasPrev, setHasPrev]         = useState(false);

  const clampPage = useCallback(
    (p: number) => Math.max(1, Math.min(p, totalPages || 1)),
    [totalPages]
  );

  const setPage = useCallback((p: number) => setPageState(clampPage(p)), [clampPage]);
  const nextPage     = useCallback(() => { if (hasNext)  setPageState((p) => p + 1); }, [hasNext]);
  const previousPage = useCallback(() => { if (hasPrev)  setPageState((p) => Math.max(1, p - 1)); }, [hasPrev]);

  const setPageSize = useCallback((size: number) => {
    setPageSizeState(Math.max(1, Math.min(size, 500)));
    setPageState(1);
  }, []);

  // Toggle asc/desc if same column, else sort new column desc
  const setSort = useCallback((column: string) => {
    setSortBy((prev) => {
      if (prev === column) {
        setSortOrder((o) => (o === "asc" ? "desc" : "asc"));
        return column;
      }
      setSortOrder("desc");
      return column;
    });
    setPageState(1);
  }, []);

  const setFilter = useCallback((key: string, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPageState(1);
  }, []);

  const clearFilters = useCallback(() => {
    setFilters(defaultFilters);
    setPageState(1);
  }, [defaultFilters]);

  // Call this with each API response to keep metadata in sync
  const updateFromResponse = useCallback((response: ServerTableResponse<T>) => {
    const t  = resolveTotal(response);
    const tp = resolveTotalPages(response, pageSize);
    setTotal(t);
    setTotalPages(tp);
    setHasNext(response.has_next ?? page < tp);
    setHasPrev(response.has_previous ?? page > 1);
  }, [page, pageSize]);

  // Auto-clamp page when totalPages shrinks
  useEffect(() => {
    if (page > totalPages && totalPages > 0) setPageState(totalPages);
  }, [totalPages, page]);

  const toParams = useCallback((): URLSearchParams => {
    const p = new URLSearchParams({
      page:       String(page),
      page_size:  String(pageSize),
      sort_by:    sortBy,
      sort_order: sortOrder,
    });
    Object.entries(filters).forEach(([k, v]) => { if (v) p.set(k, v); });
    return p;
  }, [page, pageSize, sortBy, sortOrder, filters]);

  const queryKey = [baseQueryKey, page, pageSize, sortBy, sortOrder, filters] as (
    string | number | Record<string, string>
  )[];

  const state: ServerTableState = { page, pageSize, sortBy, sortOrder, filters };

  return {
    state,
    items: [] as T[],   // populated by caller via resolveItems()
    total,
    totalPages,
    hasNextPage: hasNext,
    hasPreviousPage: hasPrev,
    setPage,
    nextPage,
    previousPage,
    setPageSize,
    setSort,
    setFilter,
    clearFilters,
    queryKey,
    toParams,
    // expose updater so callers can sync response metadata
    ...(({ updateFromResponse }) => ({ updateFromResponse }))(
      { updateFromResponse }
    ),
  } as UseServerTableReturn<T> & { updateFromResponse: (r: ServerTableResponse<T>) => void };
}
