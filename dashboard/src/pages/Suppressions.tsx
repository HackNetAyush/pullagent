import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Search } from "lucide-react";
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { DataTable, useTableParams } from "../components/DataTable";
import { FilterBar, PageHeader } from "../components/Layout";
import { Badge, Card, CardBody, Input, Select, Tooltip } from "../components/ui";
import { api, type Page, type Suppression } from "../lib/api";
import { fmtInt } from "../lib/utils";

/** Where each suppression came from — the audit trail for "why did CR stop
 *  telling me about this". */
const REASONS = [
  { value: "resolved", label: "Thread resolved" },
  { value: "thumbs_down", label: "Thumbs down" },
  { value: "conceded", label: "Conceded in discussion" },
  { value: "manual", label: "Manually ignored" },
];

const REASON_LABEL = Object.fromEntries(REASONS.map((r) => [r.value, r.label]));

export function SuppressionsPage() {
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
    reason: t.get("reason"),
    repo: t.get("repo"),
    q: t.get("q"),
  };

  const { data, isFetching, error, refetch } = useQuery<Page<Suppression>>({
    queryKey: ["suppressions", params],
    queryFn: () => api.suppressions(params),
    placeholderData: keepPreviousData,
  });

  const { data: facets } = useQuery({
    queryKey: ["run-facets"],
    queryFn: api.runFacets,
    staleTime: 300_000,
  });

  const columns = React.useMemo<ColumnDef<Suppression, any>[]>(
    () => [
      {
        header: "Finding",
        accessorKey: "claim",
        cell: ({ row }) => (
          <div className="min-w-0 max-w-xl">
            <p className="truncate text-slate-900 dark:text-slate-100">
              {row.original.claim || <span className="text-slate-400">(no text recorded)</span>}
            </p>
            <p className="truncate text-[12px] text-slate-500 dark:text-slate-400">
              {row.original.repo}
              {row.original.file ? ` · ${row.original.file}` : ""}
              {row.original.pr ? ` · PR #${row.original.pr}` : ""}
            </p>
          </div>
        ),
      },
      {
        header: "Reason",
        accessorKey: "reason",
        size: 170,
        cell: ({ row }) => (
          <Tooltip label={row.original.note || "No note recorded."}>
            <span>
              <Badge>{REASON_LABEL[row.original.reason] || row.original.reason}</Badge>
            </span>
          </Tooltip>
        ),
      },
      {
        header: "Times fired",
        accessorKey: "hits",
        size: 110,
        cell: ({ row }) => (
          <span className="tabular-nums">
            {row.original.hits > 0 ? (
              <span className="font-600 text-slate-900 dark:text-slate-100">
                {fmtInt(row.original.hits)}
              </span>
            ) : (
              <span className="text-slate-400">never</span>
            )}
          </span>
        ),
      },
      {
        header: "Fingerprint",
        accessorKey: "fingerprint",
        size: 130,
        cell: ({ row }) => (
          <code className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            {row.original.fingerprint}
          </code>
        ),
      },
    ],
    [],
  );

  const fired = (data?.items || []).reduce((a, s) => a + s.hits, 0);

  return (
    <>
      <PageHeader
        title="Suppressions"
        description="Findings a human rejected. CR will not raise any of these on that repository again."
      />

      <Card className="mb-3">
        <CardBody className="text-[13px] text-slate-600 dark:text-slate-300">
          Resolve a review thread, react 👎, reply <code className="rounded bg-slate-100 px-1 dark:bg-slate-800">@pullagent ignore</code>,
          or argue CR out of a finding — all four write a row here.{" "}
          <span className="text-slate-500 dark:text-slate-400">
            {fmtInt(data?.total || 0)} stored, {fmtInt(fired)} fired on this page.
          </span>
        </CardBody>
      </Card>

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
          value={t.get("reason")}
          onChange={(v) => t.setFilter("reason", v)}
          placeholder="Any reason"
          options={REASONS}
        />
        <Select
          value={t.get("repo")}
          onChange={(v) => t.setFilter("repo", v)}
          placeholder="Any repository"
          options={(facets?.repos || []).map((s) => ({ value: s, label: s }))}
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
          rowId={(s) => s.id}
          emptyTitle="Nothing suppressed yet"
          emptyHint="CR learns from dismissals — resolve a thread or react 👎 on a comment it got wrong."
        />
      </Card>
    </>
  );
}
