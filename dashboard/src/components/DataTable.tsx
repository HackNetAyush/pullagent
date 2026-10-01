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
  /** Row identity for React keys - index is wrong when a page shifts. */
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
    <div className="relative">
      {/* A refetch dims the rows instead of swapping them for skeletons - the
          old page stays readable while the next one is in the air. */}
      {loading && data.length > 0 && (
        <div className="absolute inset-x-0 top-0 z-20 h-0.5 overflow-hidden rounded-t-2xl">
          <div className="skeleton h-full w-full" />
        </div>
      )}

      {/* The table scrolls inside its own box, so the page never scrolls
          sideways and the pager below stays reachable without scrolling to the
          bottom of a hundred rows. max-height only bites once the page is
          genuinely long; a short table is laid out exactly as before. */}
      <div className="max-h-[70vh] min-h-0 overflow-auto">
        <table className="w-full border-collapse text-left">
          <thead className="sticky top-0 z-10">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => (
                  <th
                    key={h.id}
                    // The header is sticky, so it needs its own opaque
                    // background and its own bottom hairline - a border on a
                    // sticky <tr> does not paint in every browser.
                    className="border-b border-line bg-surface px-4 py-2.5 text-[11px] font-semibold tracking-[0.05em] text-fg-faint uppercase"
                    style={{ width: h.column.columnDef.size }}
                  >
                    {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody className={cn("transition-opacity", loading && data.length > 0 && "opacity-60")}>
            {loading && data.length === 0
              ? Array.from({ length: Math.min(limit, 8) }).map((_, i) => (
                  <tr key={i} className="border-b border-line/70">
                    {columns.map((_c, j) => (
                      <td key={j} className="px-4 py-3.5">
                        <Skeleton className="h-3.5 w-full" />
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
                      "border-b border-line/70 transition-colors last:border-0",
                      onRowClick &&
                        "cursor-pointer hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-none",
                    )}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="px-4 py-3 align-middle text-[13px] text-fg-muted">
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

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-2.5">
        <p className="text-[12px] text-fg-muted">
          {total === 0 ? (
            "No rows"
          ) : (
            <>
              <span className="tabular font-medium text-fg">
                {from.toLocaleString()}–{to.toLocaleString()}
              </span>{" "}
              of <span className="tabular">{total.toLocaleString()}</span>
            </>
          )}
        </p>

        <div className="flex items-center gap-3">
          {onLimitChange && (
            <label className="flex items-center gap-1.5 text-[12px] text-fg-muted">
              Rows
              <select
                value={limit}
                onChange={(e) => {
                  onLimitChange(Number(e.target.value));
                  onOffsetChange(0);
                }}
                className="h-7 rounded-md border border-line bg-surface px-1.5 text-[12px] text-fg transition-colors hover:border-line-strong"
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
            <span className="tabular min-w-20 text-center text-[12px] text-fg-muted">
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
      if (v === undefined || v === "") next.delete(k);
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
    /** Changing any filter resets to page one - staying on page 7 of a
     *  result set that now has two pages shows an empty table. */
    setFilter: (k: string, v: string) => update({ [k]: v || undefined, offset: undefined }),
  };
}
