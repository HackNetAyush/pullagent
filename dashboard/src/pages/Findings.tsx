import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Search } from "lucide-react";
import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";

import { DataTable, useTableParams } from "../components/DataTable";
import { FilterBar, PageHeader } from "../components/Layout";
import { Badge, Card, Input, Select, Tooltip } from "../components/ui";
import { api, type Finding, type Page } from "../lib/api";
import { SEVERITY_ORDER, SEVERITY_VAR, fmtPct } from "../lib/utils";

const CATEGORIES = [
  "correctness",
  "security",
  "concurrency",
  "api_contract",
  "performance",
  "test_coverage",
];

/** Severity chip. The ordinal colour is a swatch; the word carries the meaning. */
function Severity({ value }: { value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span
        aria-hidden
        className="h-2.5 w-2.5 shrink-0 rounded-[3px]"
        style={{ background: SEVERITY_VAR[value] || "var(--axis)" }}
      />
      <span className="text-[12px] capitalize text-slate-700 dark:text-slate-300">{value}</span>
    </span>
  );
}

export function FindingsPage() {
  const [search, setSearch] = useSearchParams();
  const t = useTableParams(search, setSearch);
  const [q, setQ] = React.useState(t.get("q"));

  React.useEffect(() => {
    const id = setTimeout(() => {
      if (q !== t.get("q")) t.setFilter("q", q);
    }, 300);
    return () => clearTimeout(id);
  }, [q]); // eslint-disable-line react-hooks/exhaustive-deps

  const params = {
    limit: t.limit,
    offset: t.offset,
    severity: t.get("severity"),
    category: t.get("category"),
    repo: t.get("repo"),
    posted: t.get("posted") || undefined,
    q: t.get("q"),
  };

  const { data, isFetching, error, refetch } = useQuery<Page<Finding>>({
    queryKey: ["findings", params],
    queryFn: () => api.findings(params),
    placeholderData: keepPreviousData,
  });

  const { data: facets } = useQuery({
    queryKey: ["run-facets"],
    queryFn: api.runFacets,
    staleTime: 300_000,
  });

  const columns = React.useMemo<ColumnDef<Finding, any>[]>(
    () => [
      {
        header: "Finding",
        accessorKey: "claim",
        cell: ({ row }) => (
          <div className="min-w-0 max-w-xl">
            <Tooltip label={row.original.failure_scenario}>
              <p className="truncate font-medium text-slate-900 dark:text-slate-100">
                {row.original.claim}
              </p>
            </Tooltip>
            <p className="truncate text-[12px] text-slate-500 dark:text-slate-400">
              {row.original.file}:{row.original.line} · found by {row.original.found_by || "—"}
            </p>
          </div>
        ),
      },
      {
        header: "Severity",
        accessorKey: "severity",
        size: 110,
        cell: ({ row }) => <Severity value={row.original.severity} />,
      },
      {
        header: "Category",
        accessorKey: "category",
        size: 130,
        cell: ({ row }) => <Badge>{row.original.category?.replace(/_/g, " ")}</Badge>,
      },
      {
        header: "Confidence",
        accessorKey: "confidence",
        size: 100,
        cell: ({ row }) => <span className="tabular-nums">{fmtPct(row.original.confidence)}</span>,
      },
      {
        header: "Posted",
        accessorKey: "posted",
        size: 90,
        cell: ({ row }) =>
          row.original.posted ? (
            <span className="text-[var(--good)]">posted</span>
          ) : (
            <span className="text-slate-400">held back</span>
          ),
      },
      {
        header: "Review",
        accessorKey: "run_id",
        size: 140,
        cell: ({ row }) => (
          <Link
            to={`/runs/${row.original.run_id}`}
            className="text-brand-600 hover:underline dark:text-brand-200"
            onClick={(e) => e.stopPropagation()}
          >
            {row.original.repo}
            {row.original.pr ? `#${row.original.pr}` : ""}
          </Link>
        ),
      },
    ],
    [],
  );

  return (
    <>
      <PageHeader
        title="Findings"
        description="Every defect CR has reported, across all reviews — including the ones it decided not to post."
      />

      <FilterBar>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search claims…"
            className="w-64 pl-8"
          />
        </div>
        <Select
          value={t.get("severity")}
          onChange={(v) => t.setFilter("severity", v)}
          placeholder="Any severity"
          options={SEVERITY_ORDER.map((s) => ({ value: s, label: s }))}
        />
        <Select
          value={t.get("category")}
          onChange={(v) => t.setFilter("category", v)}
          placeholder="Any category"
          options={CATEGORIES.map((s) => ({ value: s, label: s.replace(/_/g, " ") }))}
        />
        <Select
          value={t.get("repo")}
          onChange={(v) => t.setFilter("repo", v)}
          placeholder="Any repository"
          options={(facets?.repos || []).map((s) => ({ value: s, label: s }))}
        />
        <Select
          value={t.get("posted")}
          onChange={(v) => t.setFilter("posted", v)}
          placeholder="Posted or not"
          options={[
            { value: "true", label: "Posted" },
            { value: "false", label: "Held back" },
          ]}
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
          rowId={(f) => f.id}
          emptyTitle="No findings match these filters"
        />
      </Card>
    </>
  );
}
