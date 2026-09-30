import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ExternalLink, Search } from "lucide-react";
import * as React from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { DataTable, useTableParams } from "../components/DataTable";
import { FilterBar, PageHeader } from "../components/Layout";
import { Card, Input } from "../components/ui";
import { api, type Page, type RepoRow } from "../lib/api";
import { fmtInt, fmtUSD, relTime } from "../lib/utils";

/**
 * The repository rollup. A team does not think in "review #412" — it thinks in
 * "what is this costing us on the API repo, and is CR actually finding things
 * there". This is that view.
 */
export function ReposPage() {
  const [search, setSearch] = useSearchParams();
  const t = useTableParams(search, setSearch);
  const navigate = useNavigate();
  const [q, setQ] = React.useState(t.get("q"));

  React.useEffect(() => {
    const id = setTimeout(() => {
      if (q !== t.get("q")) t.setFilter("q", q);
    }, 300);
    return () => clearTimeout(id);
  }, [q]); // eslint-disable-line react-hooks/exhaustive-deps

  const params = { limit: t.limit, offset: t.offset, q: t.get("q") };

  const { data, isFetching, error, refetch } = useQuery<Page<RepoRow>>({
    queryKey: ["repos", params],
    queryFn: () => api.repos(params),
    placeholderData: keepPreviousData,
  });

  const columns = React.useMemo<ColumnDef<RepoRow, any>[]>(
    () => [
      {
        header: "Repository",
        accessorKey: "repo",
        cell: ({ row }) => (
          <div className="flex items-center gap-2">
            <span className="font-medium text-slate-900 dark:text-slate-100">{row.original.repo}</span>
            <a
              href={`https://github.com/${row.original.repo}`}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-slate-400 hover:text-brand-600"
              aria-label={`Open ${row.original.repo} on GitHub`}
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </div>
        ),
      },
      {
        header: "Reviews",
        accessorKey: "runs",
        size: 100,
        cell: ({ row }) => <span className="tabular-nums">{fmtInt(row.original.runs)}</span>,
      },
      {
        header: "Pull requests",
        accessorKey: "pull_requests",
        size: 120,
        cell: ({ row }) => <span className="tabular-nums">{fmtInt(row.original.pull_requests)}</span>,
      },
      {
        header: "Comments",
        accessorKey: "posted",
        size: 130,
        cell: ({ row }) => (
          <span className="tabular-nums">
            {fmtInt(row.original.posted)}
            <span className="text-slate-400"> / {fmtInt(row.original.killed)} killed</span>
          </span>
        ),
      },
      {
        header: "Suppressed",
        accessorKey: "suppressions",
        size: 110,
        cell: ({ row }) => <span className="tabular-nums">{fmtInt(row.original.suppressions)}</span>,
      },
      {
        header: "Spend",
        accessorKey: "cost",
        size: 100,
        cell: ({ row }) => (
          <span className="font-600 tabular-nums">{fmtUSD(row.original.cost)}</span>
        ),
      },
      {
        header: "Last review",
        accessorKey: "last_run",
        size: 120,
        cell: ({ row }) => (
          <span className="text-slate-500 dark:text-slate-400">{relTime(row.original.last_run)}</span>
        ),
      },
    ],
    [],
  );

  return (
    <>
      <PageHeader
        title="Repositories"
        description="Spend and review activity rolled up per repository, most expensive first."
      />

      <FilterBar>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search repositories…"
            className="w-64 pl-8"
          />
        </div>
      </FilterBar>

      <Card className="overflow-hidden">
        <DataTable
          columns={columns}
          data={data?.items || []}
          total={data?.total || 0}
          limit={t.limit}
          offset={t.offset}
          onOffsetChange={t.setOffset}
          onLimitChange={t.setLimit}
          loading={isFetching}
          error={error}
          onRetry={refetch}
          onRowClick={(r) => navigate(`/runs?repo=${encodeURIComponent(r.repo)}`)}
          rowId={(r) => r.repo}
          emptyTitle="No repositories reviewed yet"
          emptyHint="Install the App on a repository and open a pull request."
        />
      </Card>
    </>
  );
}
