import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";

import * as React from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { DataTable, useTableParams } from "../components/DataTable";
import { FilterBar, PageHeader } from "../components/Layout";
import { Badge, Card, SearchInput, Select, StatusDot } from "../components/ui";
import { api, type Facets, type Page, type Run } from "../lib/api";
import { STATUS_VAR, fmtInt, fmtSecs, fmtUSD, relTime } from "../lib/utils";
import { useWorkspace } from "../workspace";

/** Stage progress as a compact bar — a review has 9 stages and "which one" is
 *  the only question worth answering in a table cell. */
function StageBar({ run }: { run: Run }) {
  if (run.status !== "running") return null;
  const pct = Math.round(((run.stage_index + 1) / Math.max(1, run.stages.length)) * 100);
  return (
    <span className="flex items-center gap-2">
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-surface-2">
        <span className="block h-full rounded-full bg-[var(--series-1)]" style={{ width: `${pct}%` }} />
      </span>
      <span className="text-[12px] text-fg-muted">{run.stage}</span>
    </span>
  );
}

export function RunsPage() {
  const [search, setSearch] = useSearchParams();
  const t = useTableParams(search, setSearch);
  const navigate = useNavigate();
  const [q, setQ] = React.useState(t.get("q"));

  // Debounced so typing does not fire a request per keystroke.
  React.useEffect(() => {
    const id = setTimeout(() => {
      if (q !== t.get("q")) t.setFilter("q", q);
    }, 300);
    return () => clearTimeout(id);
  }, [q]); // eslint-disable-line react-hooks/exhaustive-deps

  const { account } = useWorkspace();
  const params = {
    account,
    limit: t.limit,
    offset: t.offset,
    status: t.get("status"),
    source: t.get("source"),
    tier: t.get("tier"),
    repo: t.get("repo"),
    q: t.get("q"),
  };

  const { data, isFetching, error, refetch } = useQuery<Page<Run>>({
    queryKey: ["runs", params],
    queryFn: () => api.runs(params),
    enabled: Boolean(account),
    // Keeps the old page on screen while the next one loads, so paging does
    // not flash an empty table.
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });

  const { data: facets } = useQuery<Facets>({
    queryKey: ["run-facets", account],
    queryFn: () => api.runFacets(account),
    enabled: Boolean(account),
    staleTime: 300_000,
  });

  const columns = React.useMemo<ColumnDef<Run, any>[]>(
    () => [
      {
        header: "Repository",
        accessorKey: "repo",
        cell: ({ row }) => (
          <div className="min-w-0">
            <p className="truncate font-medium text-fg">{row.original.repo}</p>
            <p className="text-[12px] text-fg-muted">
              {row.original.pr ? `PR #${row.original.pr}` : "local"} · {relTime(row.original.started_at)}
            </p>
          </div>
        ),
      },
      {
        header: "Status",
        accessorKey: "status",
        cell: ({ row }) => (
          <div className="space-y-1">
            <StatusDot
              color={STATUS_VAR[row.original.status] || "var(--axis)"}
              label={row.original.status}
            />
            <StageBar run={row.original} />
          </div>
        ),
      },
      {
        header: "Tier",
        accessorKey: "tier",
        size: 88,
        cell: ({ row }) => (
          <div className="space-y-1">
            {row.original.billing === "byok" ? (
              <Badge tone="brand" title={`Ran on your own API keys · ${row.original.model}`}>
                Your keys
              </Badge>
            ) : (
              <Badge>{row.original.tier || "—"}</Badge>
            )}
            <p className="text-[11px] text-fg-faint">{row.original.source}</p>
          </div>
        ),
      },
      {
        header: "Comments",
        accessorKey: "posted",
        size: 100,
        cell: ({ row }) => (
          <span className="tabular">
            {fmtInt(row.original.posted)}
            <span className="text-fg-faint"> / {fmtInt(row.original.killed)} killed</span>
          </span>
        ),
      },
      {
        header: "Cost",
        accessorKey: "cost",
        size: 110,
        cell: ({ row }) =>
          row.original.cache_hit ? (
            <span className="text-good">$0 cached</span>
          ) : (
            <span className="tabular">{fmtUSD(row.original.cost)}</span>
          ),
      },
      {
        header: "Elapsed",
        accessorKey: "elapsed",
        size: 90,
        cell: ({ row }) => <span className="tabular">{fmtSecs(row.original.elapsed)}</span>,
      },
    ],
    [],
  );

  return (
    <>
      <PageHeader title="Reviews" description="Every review PullAgent has run, newest first." />

      <FilterBar>
        <SearchInput
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search repository…"
          wrapperClassName="w-60"
        />
        <Select
          value={t.get("status")}
          onChange={(v) => t.setFilter("status", v)}
          placeholder="Any status"
          options={(facets?.statuses || []).map((s) => ({ value: s, label: s }))}
        />
        <Select
          value={t.get("tier")}
          onChange={(v) => t.setFilter("tier", v)}
          placeholder="Any tier"
          options={(facets?.tiers || []).map((s) => ({ value: s, label: s }))}
        />
        <Select
          value={t.get("source")}
          onChange={(v) => t.setFilter("source", v)}
          placeholder="Any source"
          options={(facets?.sources || []).map((s) => ({ value: s, label: s }))}
        />
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
          onRowClick={(r) => navigate(`/runs/${r.id}`)}
          rowId={(r) => r.id}
          emptyTitle="No reviews match these filters"
          emptyHint="Open a pull request on an installed repository, or clear the filters."
        />
      </Card>
    </>
  );
}
