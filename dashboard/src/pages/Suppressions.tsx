import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";

import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { DataTable, useTableParams } from "../components/DataTable";
import { FilterBar, PageHeader } from "../components/Layout";
import { Badge, Card, CardBody, SearchInput, Select, Tooltip } from "../components/ui";
import { api, type Page, type Suppression } from "../lib/api";
import { fmtInt } from "../lib/utils";
import { useWorkspace } from "../workspace";

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

  const { account } = useWorkspace();
  const params = {
    account,
    limit: t.limit,
    offset: t.offset,
    reason: t.get("reason"),
    repo: t.get("repo"),
    q: t.get("q"),
  };

  const { data, isFetching, error, refetch } = useQuery<Page<Suppression>>({
    queryKey: ["suppressions", params],
    queryFn: () => api.suppressions(params),
    enabled: Boolean(account),
    placeholderData: keepPreviousData,
  });

  const { data: facets } = useQuery({
    queryKey: ["run-facets", account],
    queryFn: () => api.runFacets(account),
    enabled: Boolean(account),
    staleTime: 300_000,
  });

  const columns = React.useMemo<ColumnDef<Suppression, any>[]>(
    () => [
      {
        header: "Finding",
        accessorKey: "claim",
        cell: ({ row }) => (
          <div className="min-w-0 max-w-xl">
            <p className="truncate text-fg">
              {row.original.claim || <span className="text-fg-faint">(no text recorded)</span>}
            </p>
            <p className="truncate text-[12px] text-fg-muted">
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
          <span className="tabular">
            {row.original.hits > 0 ? (
              <span className="font-semibold text-fg">
                {fmtInt(row.original.hits)}
              </span>
            ) : (
              <span className="text-fg-faint">never</span>
            )}
          </span>
        ),
      },
      {
        header: "Fingerprint",
        accessorKey: "fingerprint",
        size: 130,
        cell: ({ row }) => (
          <code className="rounded bg-surface-2 px-1.5 py-0.5 text-[11px] text-fg-muted">
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
        description="Findings a human rejected. PullAgent will not raise any of these on that repository again."
      />

      <Card className="mb-3">
        <CardBody className="text-[13px] text-fg-muted">
          Resolve a review thread, react 👎, reply <code className="rounded bg-surface-2 px-1">@pullagent ignore</code>,
          or argue PullAgent out of a finding — all four write a row here.{" "}
          <span className="text-fg-muted">
            {fmtInt(data?.total || 0)} stored, {fmtInt(fired)} fired on this page.
          </span>
        </CardBody>
      </Card>

      <FilterBar>
        <SearchInput
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search claims…"
          wrapperClassName="w-64"
        />
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
          emptyHint="PullAgent learns from dismissals — resolve a thread or react 👎 on a comment it got wrong."
        />
      </Card>
    </>
  );
}
