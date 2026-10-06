import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ShieldCheck } from "lucide-react";
import * as React from "react";

import { DataTable } from "../components/DataTable";
import { FilterBar, PageHeader } from "../components/Layout";
import { Badge, Button, Card, CardBody, Select, StatusDot, Tooltip } from "../components/ui";
import { api, type AccountRow } from "../lib/api";
import { STATUS_VAR, fmtInt, relTime } from "../lib/utils";

const STATUSES = [
  { value: "pending", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "denied", label: "Denied" },
];

/**
 * The allowlist, which is what makes a public App affordable: webhooks arrive
 * with no human in the loop, so without a gate any stranger who installs the
 * App points their PR volume at our model budget.
 *
 * This page is the human in that loop. Client-side it is hidden from
 * non-admins; server-side every route below requires an admin session, which
 * is the part that actually enforces it.
 */
export function AccountsPage() {
  const qc = useQueryClient();
  const [status, setStatus] = React.useState("");
  const [offset, setOffset] = React.useState(0);
  const [limit, setLimit] = React.useState(25);

  const { data, isFetching, error, refetch } = useQuery<AccountRow[]>({
    queryKey: ["accounts", status],
    queryFn: () => api.accounts(status || undefined),
    refetchInterval: 30_000,
  });

  const decide = useMutation({
    mutationFn: ({ login, next }: { login: string; next: string }) =>
      api.decideAccount(login, next),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });

  // This endpoint returns the full list, so the page is sliced here. The
  // table contract is identical either way, which is what lets it change to
  // server paging later without touching this component.
  const all = data || [];
  const rows = all.slice(offset, offset + limit);
  const pending = all.filter((a) => a.status === "pending").length;

  const columns = React.useMemo<ColumnDef<AccountRow, any>[]>(
    () => [
      {
        header: "Account",
        accessorKey: "login",
        cell: ({ row }) => (
          <div>
            <p className="font-medium text-fg">{row.original.login}</p>
            <p className="text-[12px] text-fg-muted">
              {row.original.account_type || "user"}
              {row.original.requested_by && ` · asked by ${row.original.requested_by}`}
            </p>
          </div>
        ),
      },
      {
        header: "Status",
        accessorKey: "status",
        size: 120,
        cell: ({ row }) => (
          <StatusDot color={STATUS_VAR[row.original.status] || "var(--axis)"} label={row.original.status} />
        ),
      },
      {
        header: "Blocked events",
        accessorKey: "blocked_events",
        size: 140,
        cell: ({ row }) =>
          row.original.blocked_events > 0 ? (
            <Tooltip label={`Last blocked ${relTime(row.original.last_blocked_at)}`}>
              <span className="tabular text-warning">
                {fmtInt(row.original.blocked_events)} turned away
              </span>
            </Tooltip>
          ) : (
            <span className="text-fg-faint">—</span>
          ),
      },
      {
        header: "Decided",
        accessorKey: "decided_by",
        size: 150,
        cell: ({ row }) =>
          row.original.decided_by ? (
            <span className="text-[12px] text-fg-muted">
              {row.original.decided_by} · {relTime(row.original.decided_at)}
            </span>
          ) : (
            <span className="text-fg-faint">—</span>
          ),
      },
      {
        header: "",
        id: "actions",
        size: 170,
        cell: ({ row }) => {
          const a = row.original;
          const busy = decide.isPending && decide.variables?.login === a.login;
          return (
            <div className="flex justify-end gap-1.5">
              {a.status !== "approved" && (
                <Button
                  size="sm"
                  variant="success"
                  loading={busy}
                  onClick={() => decide.mutate({ login: a.login, next: "approved" })}
                >
                  Approve
                </Button>
              )}
              {a.status !== "denied" && (
                <Button
                  size="sm"
                  variant={a.status === "approved" ? "secondary" : "danger"}
                  loading={busy}
                  onClick={() => decide.mutate({ login: a.login, next: "denied" })}
                >
                  {a.status === "approved" ? "Revoke" : "Deny"}
                </Button>
              )}
            </div>
          );
        },
      },
    ],
    [decide],
  );

  return (
    <>
      <PageHeader
        title="Access"
        description="Which GitHub accounts PullAgent will review for. Nothing is reviewed for an account that is not approved."
        actions={
          pending > 0 ? (
            <Badge className="bg-warning/15 text-warning">
              {pending} awaiting a decision
            </Badge>
          ) : undefined
        }
      />

      <Card className="mb-3">
        <CardBody className="flex items-start gap-2.5 text-[13px] text-fg-muted">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-good" />
          <p>
            This gate is default-deny — an account that is unreachable, unknown or pending is
            refused, not allowed through. Approving an account lets it spend this
            installation&rsquo;s model budget, so treat it as a billing decision.
          </p>
        </CardBody>
      </Card>

      <FilterBar>
        <Select
          value={status}
          onChange={(v) => {
            setStatus(v);
            setOffset(0);
          }}
          placeholder="Any status"
          options={STATUSES}
        />
      </FilterBar>

      <Card className="overflow-hidden">
        <DataTable
          columns={columns}
          data={rows}
          total={all.length}
          limit={limit}
          offset={offset}
          onOffsetChange={setOffset}
          onLimitChange={setLimit}
          loading={isFetching}
          error={error}
          onRetry={refetch}
          rowId={(a) => a.login}
          emptyTitle="No accounts yet"
          emptyHint="An account appears here the first time it installs the App or asks for access."
        />
      </Card>
    </>
  );
}
