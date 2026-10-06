import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CircleAlert,
  FlaskConical,
  KeyRound,
  Layers,
  Lock,
  Pencil,
  Plug,
  Plus,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import * as React from "react";

import { PageHeader } from "../components/Layout";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  Input,
  Select,
  Skeleton,
  Tooltip,
} from "../components/ui";
import {
  ApiError,
  api,
  type Connection,
  type CustomTier,
  type Preset,
  type Routing,
  type Workspace,
} from "../lib/api";
import { cn, fmtUSD, relTime } from "../lib/utils";
import { ALL, useWorkspace } from "../workspace";
import { ConnectionDialog } from "./models/ConnectionDialog";
import { GuidelinesCard } from "./models/GuidelinesCard";
import { SampleReviewDialog } from "./models/SampleReviewDialog";
import { TierBuilder } from "./models/TierBuilder";
import { EFFORT_LABEL, lensName } from "./models/format";

/**
 * Models: connect your own provider accounts, build review tiers on their
 * models, and choose which tier reviews which kind of pull request.
 *
 * Scoped to a workspace - a GitHub account or organisation. CR's own managed
 * setup is described only by what it does; which provider serves it is
 * deployment detail and never shown here.
 */
export function ModelsPage() {
  const { account, loading, error: listError } = useWorkspace();
  const scoped = Boolean(account) && account !== ALL;

  const ws = useQuery<Workspace>({
    queryKey: ["workspace", account],
    queryFn: () => api.workspace(account),
    enabled: scoped,
  });

  return (
    <>
      <PageHeader
        title="Models"
        description="Connect your own model providers, build review tiers on them, and choose which repositories they review. Anything you don't change runs on PullAgent's managed models."
      />
      {listError || ws.error ? (
        <Card>
          <ErrorState error={listError || ws.error} onRetry={() => ws.refetch()} />
        </Card>
      ) : loading || (scoped && ws.isLoading) ? (
        <LoadingState />
      ) : account === ALL ? (
        <Card>
          <EmptyState
            icon={Layers}
            title="Choose a workspace"
            hint="Models, keys and routing belong to one account or organisation. Pick one in the switcher at the top."
          />
        </Card>
      ) : !account ? (
        <Card>
          <EmptyState
            icon={Layers}
            title="No workspace yet"
            hint="Install PullAgent on your account or an organisation, then come back to configure its models."
          />
        </Card>
      ) : ws.data ? (
        <WorkspaceView ws={ws.data} />
      ) : null}
    </>
  );
}

function LoadingState() {
  return (
    <div className="grid gap-3">
      <Skeleton className="h-56 rounded-2xl" />
      <Skeleton className="h-40 rounded-2xl" />
    </div>
  );
}

function errorText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

function WorkspaceView({ ws }: { ws: Workspace }) {
  const [builder, setBuilder] = React.useState<{ open: boolean; tier: CustomTier | null }>({
    open: false,
    tier: null,
  });
  const [connDialog, setConnDialog] = React.useState<{
    open: boolean;
    connection: Connection | null;
  }>({ open: false, connection: null });

  const [sampleFor, setSampleFor] = React.useState<Connection | null>(null);
  const addConnection = () => setConnDialog({ open: true, connection: null });
  const hasConnections = ws.connections.length > 0;

  const addButton = (
    <Button
      variant={hasConnections ? "secondary" : "primary"}
      size="sm"
      onClick={addConnection}
      disabled={!ws.vault_ready}
    >
      <Plus aria-hidden className="h-3.5 w-3.5" />
      Add connection
    </Button>
  );

  return (
    <>
      {!ws.can_manage && (
        <div
          role="status"
          className="mb-3 flex items-start gap-2.5 rounded-xl border border-line bg-surface-2 px-4 py-3 text-[13px] text-fg"
        >
          <Lock aria-hidden className="mt-0.5 h-4 w-4 shrink-0 text-fg-muted" />
          <span>
            You can view {ws.account}'s setup. Only its owners and admins can change keys, tiers,
            routing and guidelines.
          </span>
        </div>
      )}
      <fieldset disabled={!ws.can_manage} className="contents">
      {!ws.vault_ready && (
        <div
          role="status"
          className="mb-3 flex items-start gap-2.5 rounded-xl border border-warning/40 bg-warning/8 px-4 py-3 text-[13px] text-fg"
        >
          <TriangleAlert aria-hidden className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <span>
            Connecting your own providers is not set up on this server yet. An administrator
            needs to configure <code className="font-mono">CR_SECRETS_KEY</code>.
          </span>
        </div>
      )}

      <RoutingCard ws={ws} />

      {hasConnections ? (
        <>
          <ConnectionsCard
            ws={ws}
            addButton={addButton}
            onEdit={(connection) => setConnDialog({ open: true, connection })}
            onSample={setSampleFor}
          />
          <TiersCard
            ws={ws}
            onNew={() => setBuilder({ open: true, tier: null })}
            onEdit={(tier) => setBuilder({ open: true, tier })}
          />
        </>
      ) : (
        <Card className="mb-3">
          <CardBody className="flex flex-wrap items-center gap-4 p-5">
            <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-brand-500/10 text-brand-600 ring-1 ring-brand-500/15 ring-inset dark:text-brand-300">
              <Plug aria-hidden className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="font-display text-[14.5px] font-semibold text-fg">
                Use your own models
              </p>
              <p className="mt-0.5 text-[12.5px] text-fg-muted">
                Connect Anthropic, OpenAI, Azure, OpenRouter, Groq or NVIDIA with your own key,
                then build review tiers on any of your models.
              </p>
            </div>
            {addButton}
          </CardBody>
        </Card>
      )}

      <GuidelinesCard ws={ws} />

      </fieldset>

      <SampleReviewDialog workspace={ws} connection={sampleFor} onClose={() => setSampleFor(null)} />
      <ConnectionDialog
        open={connDialog.open}
        onOpenChange={(open) => setConnDialog((d) => ({ ...d, open }))}
        workspace={ws}
        connection={connDialog.connection}
      />
      <TierBuilder
        open={builder.open}
        onOpenChange={(open) => setBuilder((b) => ({ ...b, open }))}
        workspace={ws}
        tier={builder.tier}
      />
    </>
  );
}

/* --- routing ------------------------------------------------------------- */

function presetSummary(p: Preset): string {
  const f = p.finders.length;
  const v = p.verifiers.length;
  return `${f} finder ${f === 1 ? "lens" : "lenses"} · ${v} verifier ${
    v === 1 ? "lens" : "lenses"
  } · ${EFFORT_LABEL[p.effort].toLowerCase()} effort · up to ${p.max_comments} comments`;
}

function modelLabel(ws: Workspace, ref: string): string {
  for (const c of ws.connections) {
    const m = c.models.find((x) => x.ref === ref);
    if (m) return m.label;
  }
  return ref.split(":").slice(2).join(":") || ref;
}

function tierSummary(ws: Workspace, t: CustomTier): string {
  const s = t.spec;
  const f = s.finders.length;
  return `${f} finder ${f === 1 ? "lens" : "lenses"} on ${modelLabel(
    ws,
    s.finder.model,
  )} · verified by ${modelLabel(ws, s.verifier.model)} · up to ${s.max_comments} comments`;
}

function managedHint(ws: Workspace): string {
  return ws.presets.map((p) => `${p.slot} ${p.label.toLowerCase()}: ${presetSummary(p)}`).join("\n");
}

/**
 * Where reviews run. By default everything is CR-managed; a rule forces a
 * repository - or every repository - onto one of the customer's own tiers,
 * so those pull requests never spend CR credits. A repository rule beats the
 * rule for all repositories.
 */
function RoutingCard({ ws }: { ws: Workspace }) {
  const qc = useQueryClient();
  const [error, setError] = React.useState<string | null>(null);
  const [adding, setAdding] = React.useState(false);
  const [repo, setRepo] = React.useState("");
  const [repoTier, setRepoTier] = React.useState("");

  const save = useMutation({
    mutationFn: (routing: Routing) => api.saveRouting(ws.account, routing),
    onSuccess: () => {
      setError(null);
      setAdding(false);
      setRepo("");
      setRepoTier("");
      qc.invalidateQueries({ queryKey: ["workspace", ws.account] });
    },
    onError: (e) => setError(errorText(e, "Could not change the routing.")),
  });

  const runnable = ws.tiers.filter((t) => !t.problems.length);
  const tierOptions = runnable.map((t) => ({ value: String(t.id), label: t.name }));
  const tierById = new Map(ws.tiers.map((t) => [t.id, t]));
  const overrides = Object.entries(ws.routing.repos).sort(([x], [y]) => x.localeCompare(y));
  const available = ws.repos.filter(
    (r) => !Object.keys(ws.routing.repos).some((o) => o.toLowerCase() === r.toLowerCase()),
  );
  const canRoute = runnable.length > 0;

  const update = (next: Partial<Routing>) => save.mutate({ ...ws.routing, ...next });
  const setRepoRule = (name: string, tierId: number | null) => {
    const repos = { ...ws.routing.repos };
    if (tierId === null) delete repos[name];
    else repos[name] = tierId;
    update({ repos });
  };
  const fullRepo = repo.includes("/") ? repo.trim() : `${ws.account}/${repo.trim()}`;

  return (
    <Card className="mb-3 overflow-hidden">
      <CardHeader>
        <div>
          <CardTitle>Review routing</CardTitle>
          <CardDescription>
            Pull requests run on PullAgent's managed models unless you route them to one of your tiers.
            Routed repositories run entirely on your own API keys and never use PullAgent credits.
          </CardDescription>
        </div>
      </CardHeader>
      <CardBody className="pt-2 pb-3">
        <RouteRow
          title="All repositories"
          subtitle="Every repository in this workspace."
          tier={ws.routing.all !== null ? tierById.get(ws.routing.all) || null : null}
          ws={ws}
          value={ws.routing.all !== null ? String(ws.routing.all) : ""}
          options={tierOptions}
          disabled={save.isPending || (!canRoute && ws.routing.all === null)}
          onChange={(v) => update({ all: v ? Number(v) : null })}
          ariaLabel="Tier for all repositories"
        />

        {overrides.length > 0 && (
          <p className="mt-3 mb-1 text-[11px] font-semibold tracking-[0.05em] text-fg-faint uppercase">
            Repository overrides
          </p>
        )}
        {overrides.map(([name, tierId]) => (
          <RouteRow
            key={name}
            title={name}
            mono
            subtitle="Overrides the rule for all repositories."
            tier={tierById.get(tierId) || null}
            ws={ws}
            value={String(tierId)}
            options={tierOptions}
            disabled={save.isPending}
            onChange={(v) => setRepoRule(name, v ? Number(v) : null)}
            onRemove={() => setRepoRule(name, null)}
            ariaLabel={`Tier for ${name}`}
          />
        ))}

        {adding ? (
          <div className="mt-3 grid gap-2 rounded-xl border border-dashed border-line-strong p-3 sm:grid-cols-[minmax(0,1fr)_14rem_auto_auto] sm:items-center">
            <div>
              <Input
                list="routing-repos"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder={`${ws.account}/repository`}
                aria-label="Repository"
                spellCheck={false}
                autoFocus
              />
              <datalist id="routing-repos">
                {available.map((r) => (
                  <option key={r} value={r} />
                ))}
              </datalist>
            </div>
            <Select
              variant="field"
              value={repoTier}
              onChange={setRepoTier}
              options={tierOptions}
              placeholder="Choose a tier"
              capitalize={false}
              allowEmpty={false}
              ariaLabel="Tier for the repository"
              className="w-full"
            />
            <Button
              variant="primary"
              size="sm"
              disabled={!repo.trim() || !repoTier}
              loading={save.isPending}
              onClick={() => setRepoRule(fullRepo, Number(repoTier))}
            >
              Add
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
              Cancel
            </Button>
          </div>
        ) : (
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button size="sm" onClick={() => setAdding(true)} disabled={!canRoute}>
              <Plus aria-hidden className="h-3.5 w-3.5" />
              Route a repository
            </Button>
            {!canRoute && (
              <span className="text-[12px] text-fg-muted">
                {ws.connections.length
                  ? "Build a tier below, then route repositories to it."
                  : "Connect your own provider and build a tier to route repositories to it."}
              </span>
            )}
          </div>
        )}

        {error && (
          <p role="alert" className="mt-2 text-[12.5px] text-critical">
            {error}
          </p>
        )}
      </CardBody>
    </Card>
  );
}

function RouteRow({
  title,
  subtitle,
  mono,
  tier,
  ws,
  value,
  options,
  disabled,
  onChange,
  onRemove,
  ariaLabel,
}: {
  title: string;
  subtitle: string;
  mono?: boolean;
  tier: CustomTier | null;
  ws: Workspace;
  value: string;
  options: { value: string; label: string }[];
  disabled: boolean;
  onChange: (v: string) => void;
  onRemove?: () => void;
  ariaLabel: string;
}) {
  return (
    <div className="grid gap-2 border-b border-line py-3 last:border-0 md:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_15rem_2rem] md:items-center">
      <div className="min-w-0">
        <p className={cn("truncate text-[13px] font-medium text-fg", mono && "font-mono text-[12.5px]")}>
          {title}
        </p>
        <p className="text-[12px] text-fg-muted">{subtitle}</p>
      </div>
      <div className="min-w-0 text-[12.5px]">
        {tier ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone="brand">
              <KeyRound aria-hidden className="h-3 w-3" />
              Your keys
            </Badge>
            <span className="font-medium text-fg">{tier.name}</span>
            <span className="text-fg-muted">{tierSummary(ws, tier)}</span>
            {tier.problems.length > 0 && (
              <Tooltip label={tier.problems.join(" · ")}>
                <span>
                  <Badge tone="warning">Can't run</Badge>
                </span>
              </Tooltip>
            )}
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge>
              <Lock aria-hidden className="h-3 w-3" />
              Managed by PullAgent
            </Badge>
            <Tooltip label={<span className="whitespace-pre-line">{managedHint(ws)}</span>}>
              <span className="cursor-help text-fg-muted underline decoration-line-strong decoration-dotted underline-offset-2">
                Sized per pull request
              </span>
            </Tooltip>
          </div>
        )}
      </div>
      <Select
        variant="field"
        value={value}
        onChange={onChange}
        options={options}
        placeholder="Managed by PullAgent"
        capitalize={false}
        ariaLabel={ariaLabel}
        disabled={disabled}
        className="w-full"
      />
      {onRemove ? (
        <Button size="icon" variant="ghost" onClick={onRemove} aria-label={`Stop routing ${title}`}>
          <X className="h-3.5 w-3.5" />
        </Button>
      ) : (
        <span aria-hidden className="hidden md:block" />
      )}
    </div>
  );
}

/* --- connections --------------------------------------------------------- */

function modelTooltip(m: Connection["models"][number]): string {
  const price = m.pricing
    ? `${m.pricing.input} in / ${m.pricing.output} out per 1M tokens (${
        m.price_source === "custom" ? "your price" : "list price"
      })`
    : "No price set: usage is tracked in tokens only. Add a price under Edit.";
  const spend = m.spend_30d > 0 ? ` · ${fmtUSD(m.spend_30d)} in the last 30 days` : "";
  return `${m.label} · ${price}${spend}`;
}

function ConnectionsCard({
  ws,
  addButton,
  onEdit,
  onSample,
}: {
  ws: Workspace;
  addButton: React.ReactNode;
  onEdit: (c: Connection) => void;
  onSample: (c: Connection) => void;
}) {
  return (
    <Card className="mb-3 overflow-hidden">
      <CardHeader>
        <div>
          <CardTitle>API connections</CardTitle>
          <CardDescription className="flex items-center gap-1">
            <Lock aria-hidden className="h-3 w-3" />
            Keys are encrypted at rest. Only their last four characters are shown again.
          </CardDescription>
        </div>
        {addButton}
      </CardHeader>
      <CardBody className="pt-2 pb-1">
        <ul className="divide-y divide-line">
          {ws.connections.map((c) => (
            <ConnectionRow
              key={c.id}
              ws={ws}
              c={c}
              onEdit={() => onEdit(c)}
              onSample={() => onSample(c)}
            />
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}

function ConnectionRow({
  ws,
  c,
  onEdit,
  onSample,
}: {
  ws: Workspace;
  c: Connection;
  onEdit: () => void;
  onSample: () => void;
}) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = React.useState(false);
  const remove = useMutation({
    mutationFn: () => api.deleteConnection(ws.account, c.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspace", ws.account] }),
    onSettled: () => setConfirming(false),
  });

  return (
    <li className="grid gap-2 py-3.5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-display text-[14px] font-semibold text-fg">{c.label}</p>
          {c.spend_30d > 0 && (
            <span className="text-[12px] text-fg-muted tabular">
              {fmtUSD(c.spend_30d)} in the last 30 days
            </span>
          )}
          {c.used_by.length > 0 && (
            <Tooltip label={`Used by ${c.used_by.join(", ")}`}>
              <span>
                <Badge tone="brand">
                  {c.used_by.length} {c.used_by.length === 1 ? "tier" : "tiers"}
                </Badge>
              </span>
            </Tooltip>
          )}
        </div>
        <p className="mt-0.5 truncate text-[12px] text-fg-muted">
          {c.provider_label} · <span className="font-mono">{c.host}</span> · key{" "}
          <span className="font-mono">{c.hint}</span>
          {c.tested_at && <> · tested {relTime(c.tested_at)}</>}
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {c.models.map((m) => (
            <Tooltip key={m.ref} label={modelTooltip(m)}>
              <span className="inline-flex items-center gap-1.5 rounded-md bg-surface-2 px-2 py-0.5 text-[11.5px] text-fg ring-1 ring-line ring-inset">
                <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-good" />
                <span className="font-mono">{m.name}</span>
                {m.pricing ? (
                  <span className="text-fg-faint tabular">
                    ${m.pricing.input}/${m.pricing.output}
                  </span>
                ) : (
                  <span className="text-warning">no price</span>
                )}
              </span>
            </Tooltip>
          ))}
        </div>
        {remove.error && (
          <p role="alert" className="mt-2 text-[12px] text-critical">
            {errorText(remove.error, "Could not remove this connection.")}
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1 sm:justify-end">
        <Button size="sm" variant="ghost" onClick={onSample}>
          <FlaskConical aria-hidden className="h-3.5 w-3.5" />
          Try a sample review
        </Button>
        <Button size="sm" variant="ghost" onClick={onEdit}>
          <Pencil aria-hidden className="h-3.5 w-3.5" />
          Edit
        </Button>
        {confirming ? (
          <Button
            size="sm"
            variant="danger"
            loading={remove.isPending}
            onClick={() => remove.mutate()}
            onBlur={() => !remove.isPending && setConfirming(false)}
            autoFocus
          >
            Remove connection
          </Button>
        ) : (
          <Button
            size="icon"
            variant="ghost"
            onClick={() => setConfirming(true)}
            aria-label={`Remove ${c.label}`}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </li>
  );
}

/* --- custom tiers -------------------------------------------------------- */

function TiersCard({
  ws,
  onNew,
  onEdit,
}: {
  ws: Workspace;
  onNew: () => void;
  onEdit: (t: CustomTier) => void;
}) {
  return (
    <Card className="mb-3 overflow-hidden">
      <CardHeader>
        <div>
          <CardTitle>Your tiers</CardTitle>
          <CardDescription>
            Choose the model and reasoning effort for every agent, from your connections.
          </CardDescription>
        </div>
        <Button variant={ws.tiers.length ? "secondary" : "primary"} size="sm" onClick={onNew}>
          <Plus aria-hidden className="h-3.5 w-3.5" />
          New tier
        </Button>
      </CardHeader>
      <CardBody className="pt-2">
        {ws.tiers.length === 0 ? (
          <p className="rounded-xl border border-dashed border-line-strong px-4 py-5 text-center text-[12.5px] text-fg-muted">
            No tiers yet. Build one, then route repositories to it under Review routing.
          </p>
        ) : (
          <ul className="grid gap-2.5 lg:grid-cols-2">
            {ws.tiers.map((t) => (
              <TierItem key={t.id} ws={ws} tier={t} onEdit={() => onEdit(t)} />
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

function TierItem({ ws, tier, onEdit }: { ws: Workspace; tier: CustomTier; onEdit: () => void }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = React.useState(false);
  const remove = useMutation({
    mutationFn: () => api.deleteTier(ws.account, tier.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["workspace", ws.account] }),
  });

  const routedRepos = Object.entries(ws.routing.repos)
    .filter(([, id]) => id === tier.id)
    .map(([name]) => name);
  const everywhere = ws.routing.all === tier.id;
  const s = tier.spec;
  const overrides = [...s.finders, ...s.verifiers].filter((c) => c.model || c.effort).length;

  return (
    <li className="flex flex-col gap-3 rounded-xl border border-line p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-display text-[14.5px] font-semibold text-fg">{tier.name}</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {everywhere && <Badge tone="brand">All repositories</Badge>}
            {routedRepos.length > 0 && (
              <Tooltip label={routedRepos.join(", ")}>
                <span>
                  <Badge tone="brand">
                    {routedRepos.length} {routedRepos.length === 1 ? "repository" : "repositories"}
                  </Badge>
                </span>
              </Tooltip>
            )}
            {!everywhere && !routedRepos.length && <Badge>Not routed</Badge>}
            {tier.problems.length > 0 && <Badge tone="warning">Can't run</Badge>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button size="icon" variant="ghost" onClick={onEdit} aria-label={`Edit ${tier.name}`}>
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          {confirming ? (
            <Button
              size="xs"
              variant="danger"
              loading={remove.isPending}
              onClick={() => remove.mutate()}
              onBlur={() => setConfirming(false)}
              autoFocus
            >
              Delete
            </Button>
          ) : (
            <Button
              size="icon"
              variant="ghost"
              onClick={() => setConfirming(true)}
              aria-label={`Delete ${tier.name}`}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>

      <dl className="grid grid-cols-[5.5rem_1fr] gap-x-3 gap-y-1.5 text-[12.5px]">
        <dt className="text-fg-muted">Finders</dt>
        <dd className="min-w-0 text-fg">
          {[...s.finders.map((c) => lensName(c.lens)), ...(s.custom_finders || []).map((c) => c.name)].join(", ")}
          <span className="block text-fg-muted">
            {modelLabel(ws, s.finder.model)} · {EFFORT_LABEL[s.finder.effort].toLowerCase()}{" "}
            effort
          </span>
        </dd>
        <dt className="text-fg-muted">Verifiers</dt>
        <dd className="min-w-0 text-fg">
          {[
            ...s.verifiers.map((c) => lensName(c.lens)),
            ...(s.custom_verifiers || []).map((c) => c.name),
          ].join(", ")}
          <span className="block text-fg-muted">
            {modelLabel(ws, s.verifier.model)} · {EFFORT_LABEL[s.verifier.effort].toLowerCase()}{" "}
            effort
          </span>
        </dd>
        <dt className="text-fg-muted">Comments</dt>
        <dd className="text-fg">
          Up to {s.max_comments}
          {overrides > 0 && (
            <span className="text-fg-muted">
              {" "}
              · {overrides} per-agent {overrides === 1 ? "override" : "overrides"}
            </span>
          )}
        </dd>
      </dl>

      {tier.problems.length > 0 && (
        <p className="flex items-start gap-1.5 text-[12px] text-warning">
          <CircleAlert aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {tier.problems[0]}
        </p>
      )}
      {Object.keys(tier.warnings || {}).length > 0 && (
        <p className="flex items-start gap-1.5 text-[12px] text-warning">
          <TriangleAlert aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          A custom lens is worded in a way that can make the model drop real findings. Open the
          tier to see why.
        </p>
      )}
      {remove.error && (
        <p role="alert" className="text-[12px] text-critical">
          {errorText(remove.error, "Could not delete this tier.")}
        </p>
      )}
    </li>
  );
}
