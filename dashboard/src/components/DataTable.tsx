/**
 * Server-paginated table on TanStack Table.
 *
 * Pagination is deliberately *server* side: the store holds every run and
 * finding CR has ever produced, and shipping all of them to the browser to
 * slice client-side stops working the first week a team uses this. The table
 * is handed one page plus the true total and never sees the rest.
 */
import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
} from "@tanstack/react-table";
import { ChevronLeft, ChevronRight, Inbox } from "lucide-react";

import { cn } from "../lib/utils";
import { Button, EmptyState, ErrorState, Skeleton } from "./ui";

export interface DataTableProps<T> {
  columns: ColumnDef<T, any>[];
  data: T[];
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
  onLimitChange?: (limit: number) => void;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  onRowClick?: (row: T) => void;
  emptyTitle?: string;
  emptyHint?: string;
  /** Row identity for React keys — index is wrong when a page shifts. */
  rowId: (row: T) => string | number;
}

const PAGE_SIZES = [10, 25, 50, 100];

export function DataTable<T>({
  columns,
  data,
  total,
  limit,
  offset,
  onOffsetChange,
  onLimitChange,
  loading,
  error,
  onRetry,
  onRowClick,
  emptyTitle = "Nothing here yet",
  emptyHint,
  rowId,
}: DataTableProps<T>) {
  const table = useReactTable({ data, columns, getCoreRowModel: getCoreRowModel() });

  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + data.length, total);
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));

  if (error) return <ErrorState error={error} onRetry={onRetry} />;

  return (
    <div>
      {/* The table scrolls inside its own box; the page never scrolls sideways. */}
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left">
          <thead>
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b border-slate-200 dark:border-slate-700/70">
                {hg.headers.map((h) => (
                  <th
                    key={h.id}
                    className="whitespace-nowrap px-4 py-2.5 text-[11px] font-600 uppercase tracking-wide text-slate-500 dark:text-slate-400"
                    style={{ width: h.column.columnDef.size }}
                  >
                    {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {loading && data.length === 0
              ? Array.from({ length: Math.min(limit, 8) }).map((_, i) => (
                  <tr key={i} className="border-b border-slate-100 dark:border-slate-800">
                    {columns.map((_c, j) => (
                      <td key={j} className="px-4 py-3">
                        <Skeleton className="h-4 w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              : table.getRowModel().rows.map((row) => (
                  <tr
                    key={rowId(row.original)}
                    onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                    tabIndex={onRowClick ? 0 : undefined}
                    onKeyDown={
                      onRowClick
                        ? (e) => {
                            if (e.key === "Enter") onRowClick(row.original);
                          }
                        : undefined
                    }
                    className={cn(
                      "border-b border-slate-100 last:border-0 dark:border-slate-800",
                      onRowClick && "cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50",
                    )}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="px-4 py-2.5 align-middle text-[13px]">
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                ))}
          </tbody>
        </table>
      </div>

      {!loading && data.length === 0 && (
        <EmptyState icon={Inbox} title={emptyTitle} hint={emptyHint} />
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 px-4 py-2.5 dark:border-slate-700/70">
        <p className="text-[12px] text-slate-500 dark:text-slate-400">
          {total === 0 ? "No rows" : `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`}
        </p>

        <div className="flex items-center gap-3">
          {onLimitChange && (
            <label className="flex items-center gap-1.5 text-[12px] text-slate-500 dark:text-slate-400">
              Rows
              <select
                value={limit}
                onChange={(e) => {
                  onLimitChange(Number(e.target.value));
                  onOffsetChange(0);
                }}
                className="h-7 rounded-md border border-slate-200 bg-white px-1.5 text-[12px] dark:border-slate-700 dark:bg-[#0f1523]"
              >
                {PAGE_SIZES.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
          )}

          <div className="flex items-center gap-1">
            <Button
              size="icon"
              variant="ghost"
              aria-label="Previous page"
              disabled={offset === 0 || loading}
              onClick={() => onOffsetChange(Math.max(0, offset - limit))}
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <span className="min-w-20 text-center text-[12px] tabular-nums text-slate-500 dark:text-slate-400">
              {page} / {pages}
            </span>
            <Button
              size="icon"
              variant="ghost"
              aria-label="Next page"
              disabled={offset + limit >= total || loading}
              onClick={() => onOffsetChange(offset + limit)}
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Table state in the URL, so a filtered page is a shareable link and the
 *  back button does what a reader expects. */
export function useTableParams(search: URLSearchParams, set: (next: URLSearchParams) => void) {
  const limit = Number(search.get("limit") || 25);
  const offset = Number(search.get("offset") || 0);

  const update = (patch: Record<string, string | number | undefined>) => {
    const next = new URLSearchParams(search);
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === "" ) next.delete(k);
      else next.set(k, String(v));
    }
    set(next);
  };

  return {
    limit,
    offset,
    get: (k: string) => search.get(k) || "",
    setOffset: (o: number) => update({ offset: o || undefined }),
    setLimit: (l: number) => update({ limit: l, offset: undefined }),
    /** Changing any filter resets to page one — staying on page 7 of a
     *  result set that now has two pages shows an empty table. */
    setFilter: (k: string, v: string) => update({ [k]: v || undefined, offset: undefined }),
  };
}
