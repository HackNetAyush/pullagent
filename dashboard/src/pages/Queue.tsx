import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, CircleSlash, Server, XCircle } from "lucide-react";
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { DataTable, useTableParams } from "../components/DataTable";
import { FilterBar, PageHeader } from "../components/Layout";
import {
  Badge,
  Card,
  CardBody,
  CardDescription,
  CardHeader,
  CardTitle,
  Select,
  Skeleton,
  StatusDot,
  Tooltip,
} from "../components/ui";
import { api, type AppStatus, type Job, type Page } from "../lib/api";
import { STATUS_VAR, relTime } from "../lib/utils";

const KINDS = ["review", "reply", "command", "index"];
const STATUSES = ["queued", "running", "done", "failed", "superseded", "skipped"];

/**
 * The queue view. Worth its own page because the two questions it answers —
 * "is anything stuck" and "why did that review never happen" — are invisible
 * everywhere else: a superseded job leaves no comment and no run row.
 */
export function QueuePage() {
  const [search, setSearch] = useSearchParams();
  const t = useTableParams(search, setSearch);

  const params = {
    limit: t.limit,
    offset: t.offset,
    kind: t.get("kind"),
    status: t.get("status"),
  };

  const { data, isFetching, error, refetch } = useQuery<Page<Job>>({
    queryKey: ["jobs", params],
    queryFn: () => api.jobs(params),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });

  const {
    data: status,
    isLoading: statusLoading,
    error: statusError,
  } = useQuery<AppStatus>({
    queryKey: ["app-status"],
    queryFn: api.appStatus,
    refetchInterval: 10_000,
    retry: false,
  });

  /**
   * `/api/app/*` is served by `cr app serve`, not by the plain dashboard
   * server, so a 404 here means "this build is not running the App" rather
   * than "something broke". Either way the answer is a sentence, not a
   * skeleton that shimmers forever waiting for a reply that will never come.
   */
  const unavailable = Boolean(statusError);
  const statusNote = (
    <p className="text-[13px] leading-relaxed text-fg-muted">
      The App service is not reachable from this server. Run{" "}
      <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[12px]">cr app serve</code>{" "}
      to see live worker and installation state.
    </p>
  );

  const columns = React.useMemo<ColumnDef<Job, any>[]>(
    () => [
      {
        header: "Job",
        accessorKey: "key",
        cell: ({ row }) => (
          <div className="min-w-0">
            <p className="truncate font-medium text-fg">{row.original.key}</p>
            <p className="text-[12px] text-fg-muted">
              {relTime(row.original.created_at)}
              {row.original.attempts > 0 && ` · ${row.original.attempts} attempt(s)`}
            </p>
          </div>
        ),
      },
      {
        header: "Kind",
        accessorKey: "kind",
        size: 100,
        cell: ({ row }) => <Badge>{row.original.kind}</Badge>,
      },
      {
        header: "Status",
        accessorKey: "status",
        size: 130,
        cell: ({ row }) => (
          <StatusDot
            color={STATUS_VAR[row.original.status] || "var(--axis)"}
            label={row.original.status}
          />
        ),
      },
      {
        header: "Detail",
        accessorKey: "error",
        cell: ({ row }) =>
          row.original.error ? (
            <Tooltip label={row.original.error}>
              <span className="line-clamp-1 max-w-sm text-[12px] text-critical">
                {row.original.error}
              </span>
            </Tooltip>
          ) : row.original.status === "superseded" ? (
            <span className="text-[12px] text-fg-faint">replaced by a newer push</span>
          ) : (
            <span className="text-fg-faint">—</span>
          ),
      },
      {
        header: "Finished",
        accessorKey: "finished_at",
        size: 110,
        cell: ({ row }) => (
          <span className="text-fg-muted">{relTime(row.original.finished_at)}</span>
        ),
      },
    ],
    [],
  );

  return (
    <>
      <PageHeader title="Queue" description="What the App is working on, and what it decided not to." />

      <div className="mb-3 grid gap-3 sm:grid-cols-3">
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Worker</CardTitle>
              <CardDescription>In-flight and waiting right now.</CardDescription>
            </div>
            <Server className="h-4 w-4 text-fg-faint" />
          </CardHeader>
          <CardBody className="pt-1">
            {unavailable ? (
              statusNote
            ) : statusLoading || !status ? (
              <Skeleton className="h-10" />
            ) : (
              <div className="flex items-baseline gap-4">
                <div>
                  <p className="font-display text-xl font-bold tabular text-fg">
                    {status.queue.running.length}
                  </p>
                  <p className="text-[12px] text-fg-muted">
                    running / {status.queue.concurrency} max
                  </p>
                </div>
                <div>
                  <p className="font-display text-xl font-bold tabular text-fg">
                    {status.queue.waiting.length}
                  </p>
                  <p className="text-[12px] text-fg-muted">waiting</p>
                </div>
              </div>
            )}
          </CardBody>
        </Card>

        <Card className="sm:col-span-2">
          <CardHeader>
            <div>
              <CardTitle>Installation</CardTitle>
              <CardDescription>Which account and how many repositories.</CardDescription>
            </div>
          </CardHeader>
          <CardBody className="pt-1">
            {unavailable ? (
              statusNote
            ) : statusLoading || !status ? (
              <Skeleton className="h-10" />
            ) : status.installations.length === 0 ? (
              <p className="text-[13px] text-fg-muted">
                No installations recorded.
              </p>
            ) : (
              <div className="flex flex-wrap gap-x-6 gap-y-2">
                {status.installations.map((i) => (
                  <div key={i.id}>
                    <p className="text-[13px] font-semibold text-fg">
                      {i.account}
                    </p>
                    <p className="text-[12px] text-fg-muted">
                      {i.repos.length} repositories ·{" "}
                      {i.suspended ? (
                        <span className="text-warning">suspended</span>
                      ) : (
                        <span className="text-good">active</span>
                      )}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </CardBody>
        </Card>
      </div>

      <FilterBar>
        <Select
          value={t.get("kind")}
          onChange={(v) => t.setFilter("kind", v)}
          placeholder="Any kind"
          options={KINDS.map((k) => ({ value: k, label: k }))}
        />
        <Select
          value={t.get("status")}
          onChange={(v) => t.setFilter("status", v)}
          placeholder="Any status"
          options={STATUSES.map((k) => ({ value: k, label: k }))}
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
          rowId={(j) => j.id}
          emptyTitle="The queue is empty"
          emptyHint="Jobs appear here when a webhook arrives."
        />
      </Card>
    </>
  );
}

export { CheckCircle2, CircleSlash, XCircle };
