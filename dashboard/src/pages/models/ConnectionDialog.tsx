import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, Loader2, Plus, Server, X } from "lucide-react";
import * as React from "react";

import { Button, Dialog, Field, Input, Select } from "../../components/ui";
import {
  ApiError,
  api,
  type Connection,
  type ConnectionDraft,
  type ConnectionTest,
  type ModelPrice,
  type ModelTestResult,
  type Workspace,
} from "../../lib/api";
import { cn } from "../../lib/utils";
import { fmtRate } from "./format";

/**
 * Add or edit a provider connection: provider, key, the Azure resource name
 * where needed, any number of model or deployment names, and optionally the
 * price of each so spend on them is tracked.
 *
 * "Test connection" calls every listed model once on the customer's key.
 * Saving needs a passing test for anything that changes what gets called -
 * the key, the resource, a new model. Renaming, re-pricing or removing
 * models does not. The server enforces the same rule with a receipt.
 */

/** What a test covers. Prices and the name are deliberately not part of it. */
function signature(d: ConnectionDraft): string {
  return JSON.stringify([d.provider, d.api_key.trim(), d.resource.trim(), d.models]);
}

function emptyDraft(provider: string): ConnectionDraft {
  return { provider, api_key: "", resource: "", label: "", models: [], prices: {} };
}

type PriceText = { input: string; output: string };

function priceText(p: ModelPrice | null | undefined): PriceText {
  return { input: p ? String(p.input) : "", output: p ? String(p.output) : "" };
}

/** "" -> no price; a number -> that price; anything else -> invalid. */
function parsePrice(t: PriceText | undefined): ModelPrice | null | "invalid" {
  if (!t || (!t.input.trim() && !t.output.trim())) return null;
  const input = Number(t.input);
  const output = Number(t.output);
  if (!t.input.trim() || !t.output.trim() || !(input >= 0) || !(output >= 0)) return "invalid";
  if (input > 1000 || output > 1000) return "invalid";
  return { input, output };
}

export function ConnectionDialog({
  open,
  onOpenChange,
  workspace,
  connection,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspace: Workspace;
  /** Edit this connection; omit to add a new one. */
  connection?: Connection | null;
}) {
  const qc = useQueryClient();
  const editing = Boolean(connection);
  const [draft, setDraft] = React.useState<ConnectionDraft>(emptyDraft(""));
  const [prices, setPrices] = React.useState<Record<string, PriceText>>({});
  const [pending, setPending] = React.useState("");
  const [test, setTest] = React.useState<{ sig: string; result: ConnectionTest } | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    if (connection) {
      setDraft({
        provider: connection.provider,
        api_key: "",
        resource: connection.resource,
        label: connection.label,
        models: connection.models.map((m) => m.name),
        prices: {},
      });
      setPrices(
        Object.fromEntries(
          connection.models
            .filter((m) => m.price_source === "custom")
            .map((m) => [m.name, priceText(m.pricing)]),
        ),
      );
    } else {
      setDraft(emptyDraft(""));
      setPrices({});
    }
    setPending("");
    setTest(null);
    setError(null);
  }, [open, connection]);

  const provider = workspace.providers.find((p) => p.id === draft.provider) || null;
  const max = workspace.limits.max_models;
  const sig = signature(draft);
  const fresh = test && test.sig === sig ? test.result : null;
  const results = new Map((fresh?.results || []).map((r) => [r.model, r]));
  const savedModels = new Set(connection?.models.map((m) => m.name) || []);
  const catalogPrice = new Map(
    [
      ...(provider?.suggestions || []).map((s) => [s.name, s.pricing] as const),
      ...(connection?.models || []).map((m) => [m.name, m.catalog_pricing] as const),
    ].filter(([, p]) => p),
  );

  // Mirrors the server: only a change to what gets called needs a new test.
  const needsTest =
    !editing ||
    Boolean(draft.api_key.trim()) ||
    (provider?.needs_resource && draft.resource.trim().toLowerCase() !== connection?.resource) ||
    draft.models.some((m) => !savedModels.has(m));

  const parsed = Object.fromEntries(draft.models.map((m) => [m, parsePrice(prices[m])]));
  const badPrice = draft.models.find((m) => parsed[m] === "invalid");

  const runTest = useMutation({
    mutationFn: (d: ConnectionDraft) => api.testConnection(workspace.account, d, connection?.id),
    onMutate: () => setError(null),
    onSuccess: (result, d) => setTest({ sig: signature(d), result }),
    onError: (e) => setError(e instanceof ApiError ? e.message : "The test could not run."),
  });

  const save = useMutation({
    mutationFn: () =>
      api.saveConnection(
        workspace.account,
        {
          ...draft,
          prices: Object.fromEntries(
            draft.models
              .filter((m) => parsed[m] && parsed[m] !== "invalid")
              .map((m) => [m, parsed[m] as ModelPrice]),
          ),
        },
        fresh?.receipt || "",
        connection?.id,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspace", workspace.account] });
      onOpenChange(false);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Could not save."),
  });

  const addModels = (raw: string) => {
    const names = raw
      .split(/[,\s]+/)
      .map((n) => n.trim())
      .filter(Boolean);
    if (!names.length) return;
    setDraft((d) => {
      const next = [...d.models];
      for (const n of names) if (!next.includes(n) && next.length < max) next.push(n);
      return { ...d, models: next };
    });
    setPending("");
  };
  const removeModel = (name: string) =>
    setDraft((d) => ({ ...d, models: d.models.filter((m) => m !== name) }));

  const missing = !draft.provider
    ? "Choose a provider."
    : !editing && !draft.api_key.trim()
      ? "Paste the API key."
      : provider?.needs_resource && !draft.resource.trim()
        ? "Enter the resource name."
        : !draft.models.length
          ? "Add at least one model."
          : null;
  const failed = fresh?.results.filter((r) => !r.ok).length || 0;
  const canSave =
    !missing && !badPrice && (needsTest ? Boolean(fresh?.ok && fresh.receipt) : true);

  const suggestions = (provider?.suggestions || []).filter((s) => !draft.models.includes(s.name));

  const status = error
    ? error
    : badPrice
      ? `Enter both prices for ${badPrice} as numbers, or leave both empty.`
      : fresh
        ? failed
          ? `${failed} of ${fresh.results.length} failed. Fix or remove ${
              failed === 1 ? "it" : "them"
            } and test again.`
          : `All ${fresh.results.length} passed. Ready to save.`
        : (missing ??
          (needsTest ? "Test the connection to enable saving." : "No test needed for these changes."));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={editing ? `Edit ${connection?.label}` : "Add an API connection"}
      description="Connect your own provider account. Reviews that use it are billed by that provider, on your key."
      footer={
        <>
          <p
            className={cn(
              "mr-auto text-[12.5px]",
              error || failed || badPrice
                ? "text-critical"
                : fresh?.ok
                  ? "text-good"
                  : "text-fg-muted",
            )}
            role={error ? "alert" : "status"}
          >
            {status}
          </p>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant={canSave ? "secondary" : "primary"}
            onClick={() => runTest.mutate(draft)}
            loading={runTest.isPending}
            disabled={Boolean(missing)}
          >
            Test connection
          </Button>
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            loading={save.isPending}
            disabled={!canSave}
            title={canSave ? undefined : "Run a passing test first"}
          >
            {editing ? "Save changes" : "Save connection"}
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Provider"
            htmlFor="conn-provider"
            hint={
              provider && (
                <span className="inline-flex items-center gap-1 font-mono">
                  <Server aria-hidden className="h-3 w-3" />
                  {provider.host.replace("{resource}", draft.resource.trim() || "<resource>")}
                </span>
              )
            }
          >
            <Select
              id="conn-provider"
              variant="field"
              value={draft.provider}
              onChange={(v) =>
                setDraft((d) => ({ ...emptyDraft(v), api_key: d.api_key, label: d.label }))
              }
              options={workspace.providers.map((p) => ({ value: p.id, label: p.label }))}
              placeholder="Choose a provider"
              capitalize={false}
              allowEmpty={false}
              disabled={editing}
              className="w-full"
            />
          </Field>
          <Field label="Name" htmlFor="conn-label" hint="Optional. Shown in your tiers.">
            <Input
              id="conn-label"
              value={draft.label}
              maxLength={64}
              placeholder={provider ? `${provider.label} production` : "e.g. Production"}
              onChange={(e) => setDraft((d) => ({ ...d, label: e.target.value }))}
            />
          </Field>
        </div>

        <div className={cn("grid gap-4", provider?.needs_resource && "sm:grid-cols-2")}>
          <Field label="API key" htmlFor="conn-key">
            <Input
              id="conn-key"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={draft.api_key}
              onChange={(e) => setDraft((d) => ({ ...d, api_key: e.target.value }))}
              placeholder={
                editing ? `Leave blank to keep the key ending ${connection?.hint}` : "Paste your key"
              }
            />
          </Field>
          {provider?.needs_resource && (
            <Field
              label="Azure resource name"
              htmlFor="conn-resource"
              hint="The name only, not a URL."
            >
              <Input
                id="conn-resource"
                value={draft.resource}
                spellCheck={false}
                placeholder="my-ai-resource"
                onChange={(e) => setDraft((d) => ({ ...d, resource: e.target.value }))}
              />
            </Field>
          )}
        </div>

        <Field
          label={`Models (${draft.models.length}/${max})`}
          htmlFor="conn-model"
          hint={
            provider
              ? `${provider.model_hint}. Press Enter to add; paste several separated by commas.`
              : "Choose a provider first."
          }
        >
          <div className="flex gap-2">
            <Input
              id="conn-model"
              value={pending}
              spellCheck={false}
              disabled={!provider || draft.models.length >= max}
              placeholder={provider ? "Model or deployment name" : ""}
              onChange={(e) => setPending(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === ",") {
                  e.preventDefault();
                  addModels(pending);
                }
              }}
              onPaste={(e) => {
                const text = e.clipboardData.getData("text");
                if (/[,\s]/.test(text.trim())) {
                  e.preventDefault();
                  addModels(text);
                }
              }}
            />
            <Button
              onClick={() => addModels(pending)}
              disabled={!pending.trim() || draft.models.length >= max}
            >
              <Plus aria-hidden className="h-3.5 w-3.5" />
              Add
            </Button>
          </div>
        </Field>

        {suggestions.length > 0 && draft.models.length < max && (
          <div>
            <p className="mb-1.5 text-[12px] text-fg-muted">Suggestions</p>
            <div className="flex flex-wrap gap-1.5">
              {suggestions.map((s) => (
                <button
                  key={s.name}
                  type="button"
                  onClick={() => addModels(s.name)}
                  className="inline-flex items-center gap-1 rounded-md border border-dashed border-line-strong px-2 py-1 text-[12px] text-fg-muted transition-colors hover:border-brand-500 hover:text-fg"
                >
                  <Plus aria-hidden className="h-3 w-3" />
                  {s.label}
                  {s.pricing && (
                    <span className="text-fg-faint">
                      {fmtRate(s.pricing.input)}/{fmtRate(s.pricing.output)}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </div>
        )}

        {draft.models.length > 0 && (
          <div>
            <div className="mb-1.5 flex items-baseline justify-between gap-2">
              <p className="text-[12px] text-fg-muted">
                Price per 1M tokens is optional. Set it to track what each model costs you.
              </p>
            </div>
            <ul className="divide-y divide-line rounded-xl border border-line" aria-label="Models">
              {draft.models.map((m) => (
                <ModelRow
                  key={m}
                  name={m}
                  result={results.get(m)}
                  testing={runTest.isPending}
                  saved={savedModels.has(m) && !needsTest}
                  price={prices[m] || { input: "", output: "" }}
                  catalog={catalogPrice.get(m) || null}
                  invalid={parsed[m] === "invalid"}
                  onPrice={(p) => setPrices((all) => ({ ...all, [m]: p }))}
                  onRemove={() => removeModel(m)}
                />
              ))}
            </ul>
          </div>
        )}
      </div>
    </Dialog>
  );
}

function ModelRow({
  name,
  result,
  testing,
  saved,
  price,
  catalog,
  invalid,
  onPrice,
  onRemove,
}: {
  name: string;
  result?: ModelTestResult;
  testing: boolean;
  /** Already tested on the saved connection, and nothing it depends on changed. */
  saved: boolean;
  price: PriceText;
  catalog: ModelPrice | null;
  invalid: boolean;
  onPrice: (p: PriceText) => void;
  onRemove: () => void;
}) {
  return (
    <li className="grid gap-2 px-3 py-2.5 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-center">
      <div className="flex min-w-0 items-start gap-2.5">
        <span className="mt-0.5 shrink-0" aria-hidden>
          {testing ? (
            <Loader2 className="h-4 w-4 animate-spin text-fg-faint" />
          ) : result?.ok || (!result && saved) ? (
            <CircleCheck className="h-4 w-4 text-good" />
          ) : result ? (
            <CircleAlert className="h-4 w-4 text-critical" />
          ) : (
            <span className="block h-4 w-4 rounded-full border border-dashed border-line-strong" />
          )}
        </span>
        <div className="min-w-0">
          <p className="truncate font-mono text-[12.5px] text-fg">{name}</p>
          <p
            className={cn(
              "text-[12px] leading-snug",
              result && !result.ok ? "text-critical" : "text-fg-muted",
            )}
          >
            {testing
              ? "Testing…"
              : result
                ? result.ok
                  ? `Works · ${result.latency_ms.toLocaleString()} ms${
                      result.effort ? " · reasoning effort supported" : ""
                    }`
                  : result.detail
                : saved
                  ? "Tested"
                  : "Not tested"}
          </p>
        </div>
      </div>
      <fieldset className="flex items-center gap-1.5" aria-label={`Price of ${name}`}>
        <PriceInput
          label="Input"
          value={price.input}
          placeholder={catalog ? String(catalog.input) : "in"}
          invalid={invalid}
          onChange={(v) => onPrice({ ...price, input: v })}
        />
        <span aria-hidden className="text-fg-faint">
          /
        </span>
        <PriceInput
          label="Output"
          value={price.output}
          placeholder={catalog ? String(catalog.output) : "out"}
          invalid={invalid}
          onChange={(v) => onPrice({ ...price, output: v })}
        />
      </fieldset>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${name}`}
        className="grid h-7 w-7 shrink-0 place-items-center justify-self-end rounded-md text-fg-faint hover:bg-surface-2 hover:text-fg"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}

function PriceInput({
  label,
  value,
  placeholder,
  invalid,
  onChange,
}: {
  label: string;
  value: string;
  placeholder: string;
  invalid: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <label className="relative block">
      <span className="sr-only">{label} price, USD per million tokens</span>
      <span
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-[12px] text-fg-faint"
      >
        $
      </span>
      <Input
        inputMode="decimal"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={cn("h-8 w-[5.5rem] pl-5 text-[12.5px] tabular", invalid && "border-critical")}
      />
    </label>
  );
}
