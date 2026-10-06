import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Plus, TriangleAlert, X } from "lucide-react";
import * as React from "react";

import { Button, Dialog, Field, Input, Select, Tooltip } from "../../components/ui";
import {
  ApiError,
  api,
  type ConnectionModel,
  type CustomLens,
  type CustomTier,
  type Effort,
  type Lens,
  type LensChoice,
  type TierSpec,
  type Workspace,
} from "../../lib/api";
import { cn, fmtUSD } from "../../lib/utils";
import { EFFORT_LABEL, filterWarnings, fmtRate, lensName } from "./format";

/**
 * Build or edit a custom tier: the model and effort for each kind of agent,
 * which built-in lenses run, any lenses the workspace writes itself, an
 * optional per-lens override, and the comment budget - with a running
 * estimate of what one review costs.
 *
 * Only models on the workspace's own connections are offered. The server
 * checks the same thing, so this is a convenience, not the guard.
 */

interface LensDraft {
  enabled: boolean;
  /** "" means "same as the stage default". */
  model: string;
  effort: Effort | "";
}

interface CustomDraft {
  id: string;
  name: string;
  instruction: string;
  model: string;
  effort: Effort | "";
}

interface Draft {
  name: string;
  finderModel: string;
  finderEffort: Effort;
  verifierModel: string;
  verifierEffort: Effort;
  finders: Record<string, LensDraft>;
  verifiers: Record<string, LensDraft>;
  customFinders: CustomDraft[];
  customVerifiers: CustomDraft[];
  maxComments: number;
}

const DEFAULT_FINDERS = ["correctness", "security", "api_contract"];
const DEFAULT_VERIFIERS = ["correctness", "reachability"];

/**
 * A rough per-review cost: every agent reads a typical PR context and writes
 * an answer whose length grows with reasoning effort. Prompt-cache discounts
 * are ignored, so real reviews usually come in under this.
 */
const CONTEXT_TOKENS = 25_000;
const FINDER_OUTPUT: Record<Effort, number> = {
  low: 2_000,
  medium: 5_000,
  high: 10_000,
  xhigh: 16_000,
  max: 24_000,
};
const VERIFIER_OUTPUT: Record<Effort, number> = {
  low: 1_000,
  medium: 2_000,
  high: 4_000,
  xhigh: 6_000,
  max: 8_000,
};

/** Keep an effort the model supports; otherwise fall back to medium, or its nearest level. */
function fitEffort(model: ConnectionModel | undefined, effort: Effort): Effort {
  const levels = model?.effort_levels || [];
  if (!levels.length || levels.includes(effort)) return effort;
  return levels.includes("medium") ? "medium" : levels[levels.length - 1];
}

function lensDrafts(
  lenses: Lens[],
  chosen: LensChoice[] | null,
  defaults: string[],
): Record<string, LensDraft> {
  const byId = new Map((chosen || []).map((c) => [c.lens, c]));
  return Object.fromEntries(
    lenses.map((l) => {
      const c = byId.get(l.id);
      return [
        l.id,
        {
          enabled: chosen ? Boolean(c) : defaults.includes(l.id),
          model: c?.model || "",
          effort: c?.effort || "",
        },
      ];
    }),
  );
}

function customDrafts(lenses: CustomLens[] | undefined): CustomDraft[] {
  return (lenses || []).map((c) => ({
    id: c.id,
    name: c.name,
    instruction: c.instruction,
    model: c.model || "",
    effort: c.effort || "",
  }));
}

function initialDraft(ws: Workspace, models: ConnectionModel[], tier?: CustomTier | null): Draft {
  if (tier) {
    const s = tier.spec;
    return {
      name: s.name,
      finderModel: s.finder.model,
      finderEffort: s.finder.effort,
      verifierModel: s.verifier.model,
      verifierEffort: s.verifier.effort,
      finders: lensDrafts(ws.lenses.finders, s.finders, []),
      verifiers: lensDrafts(ws.lenses.verifiers, s.verifiers, []),
      customFinders: customDrafts(s.custom_finders),
      customVerifiers: customDrafts(s.custom_verifiers),
      maxComments: s.max_comments,
    };
  }
  // Start from the first model on the first connection.
  const first = models[0] || null;
  const ref = first?.ref || "";
  return {
    name: "",
    finderModel: ref,
    finderEffort: fitEffort(first || undefined, "medium"),
    verifierModel: ref,
    verifierEffort: fitEffort(first || undefined, "medium"),
    finders: lensDrafts(ws.lenses.finders, null, DEFAULT_FINDERS),
    verifiers: lensDrafts(ws.lenses.verifiers, null, DEFAULT_VERIFIERS),
    customFinders: [],
    customVerifiers: [],
    maxComments: 8,
  };
}

function toSpec(d: Draft): TierSpec {
  const choices = (lenses: Record<string, LensDraft>): LensChoice[] =>
    Object.entries(lenses)
      .filter(([, l]) => l.enabled)
      .map(([lens, l]) => ({ lens, model: l.model || null, effort: l.effort || null }));
  const custom = (lenses: CustomDraft[]): CustomLens[] =>
    lenses.map((c) => ({
      id: c.id,
      name: c.name.trim(),
      instruction: c.instruction.trim(),
      model: c.model || null,
      effort: c.effort || null,
    }));
  return {
    name: d.name.trim(),
    finder: { model: d.finderModel, effort: d.finderEffort },
    verifier: { model: d.verifierModel, effort: d.verifierEffort },
    finders: choices(d.finders),
    verifiers: choices(d.verifiers),
    custom_finders: custom(d.customFinders),
    custom_verifiers: custom(d.customVerifiers),
    max_comments: d.maxComments,
  };
}

/** The estimate, plus the models it could not price. */
function estimate(d: Draft, byRef: Map<string, ConnectionModel>) {
  let usd = 0;
  const unpriced = new Set<string>();
  const call = (ref: string, effort: Effort, output: Record<Effort, number>) => {
    const m = byRef.get(ref);
    if (!m?.pricing) {
      if (m) unpriced.add(m.name);
      return;
    }
    usd += (CONTEXT_TOKENS * m.pricing.input + output[effort] * m.pricing.output) / 1_000_000;
  };
  const finders = [
    ...Object.values(d.finders).filter((l) => l.enabled),
    ...d.customFinders,
  ];
  for (const l of finders) {
    call(l.model || d.finderModel, (l.effort || d.finderEffort) as Effort, FINDER_OUTPUT);
  }
  const verifiers = [
    ...Object.values(d.verifiers).filter((l) => l.enabled),
    ...d.customVerifiers,
  ];
  for (const l of verifiers) {
    call(l.model || d.verifierModel, (l.effort || d.verifierEffort) as Effort, VERIFIER_OUTPUT);
  }
  // One duplicate-merge pass on the verifier defaults.
  call(d.verifierModel, d.verifierEffort, VERIFIER_OUTPUT);
  return { usd, unpriced: [...unpriced] };
}

let nextId = 0;
function newCustomId(name: string, taken: string[]): string {
  const base =
    "custom_" +
      name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 24) || "custom_lens";
  let id = base === "custom_" ? `custom_lens_${++nextId}` : base;
  while (taken.includes(id)) id = `${base}_${++nextId}`.slice(0, 37);
  return id;
}

export function TierBuilder({
  open,
  onOpenChange,
  workspace,
  tier,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspace: Workspace;
  tier?: CustomTier | null;
}) {
  const qc = useQueryClient();
  const models = React.useMemo(
    () => workspace.connections.flatMap((c) => c.models),
    [workspace],
  );
  const byRef = React.useMemo(() => new Map(models.map((m) => [m.ref, m])), [models]);
  const connectionOf = React.useMemo(
    () =>
      new Map(
        workspace.connections.flatMap((c) => c.models.map((m) => [m.ref, c.label] as const)),
      ),
    [workspace],
  );

  const [draft, setDraft] = React.useState<Draft>(() => initialDraft(workspace, models, tier));
  const [error, setError] = React.useState<string | null>(null);

  // Re-seed every time the dialog opens, so "New tier" never shows the last edit.
  React.useEffect(() => {
    if (open) {
      setDraft(initialDraft(workspace, models, tier));
      setError(null);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation({
    mutationFn: (spec: TierSpec) =>
      tier
        ? api.updateTier(workspace.account, tier.id, spec)
        : api.createTier(workspace.account, spec),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspace", workspace.account] });
      onOpenChange(false);
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Could not save this tier."),
  });

  const modelOptions = models.map((m) => ({
    value: m.ref,
    label: `${m.label} · ${connectionOf.get(m.ref) || ""}${
      m.pricing ? ` · ${fmtRate(m.pricing.input)} / ${fmtRate(m.pricing.output)}` : ""
    }`,
  }));
  const effortOptions = (ref: string) =>
    (byRef.get(ref)?.effort_levels || []).map((e) => ({ value: e, label: EFFORT_LABEL[e] }));

  const finderCount =
    Object.values(draft.finders).filter((l) => l.enabled).length + draft.customFinders.length;
  const verifierCount =
    Object.values(draft.verifiers).filter((l) => l.enabled).length + draft.customVerifiers.length;
  const badCustom = [...draft.customFinders, ...draft.customVerifiers].find(
    (c) => !c.name.trim() || c.instruction.trim().length < 20,
  );
  const problem = !draft.name.trim()
    ? "Give the tier a name."
    : !draft.finderModel || !draft.verifierModel
      ? "Choose a model for both stages."
      : !finderCount
        ? "Turn on at least one finder lens."
        : !verifierCount
          ? "Turn on at least one verifier lens."
          : badCustom
            ? "Each custom lens needs a name and an instruction of at least 20 characters."
            : null;

  const cost = estimate(draft, byRef);

  const setStage = (stage: "finder" | "verifier", ref: string) =>
    setDraft((d) => {
      const m = byRef.get(ref);
      return stage === "finder"
        ? { ...d, finderModel: ref, finderEffort: fitEffort(m, d.finderEffort) }
        : { ...d, verifierModel: ref, verifierEffort: fitEffort(m, d.verifierEffort) };
    });

  const setLens = (kind: "finders" | "verifiers", id: string, patch: Partial<LensDraft>) =>
    setDraft((d) => ({ ...d, [kind]: { ...d[kind], [id]: { ...d[kind][id], ...patch } } }));

  const setCustom = (
    kind: "customFinders" | "customVerifiers",
    id: string,
    patch: Partial<CustomDraft>,
  ) =>
    setDraft((d) => ({
      ...d,
      [kind]: d[kind].map((c) => (c.id === id ? { ...c, ...patch } : c)),
    }));
  const addCustom = (kind: "customFinders" | "customVerifiers") =>
    setDraft((d) => {
      const taken = [...d.customFinders, ...d.customVerifiers].map((c) => c.id);
      const id = newCustomId(`lens ${taken.length + 1}`, taken);
      return { ...d, [kind]: [...d[kind], { id, name: "", instruction: "", model: "", effort: "" }] };
    });
  const removeCustom = (kind: "customFinders" | "customVerifiers", id: string) =>
    setDraft((d) => ({ ...d, [kind]: d[kind].filter((c) => c.id !== id) }));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={tier ? `Edit ${tier.name}` : "New custom tier"}
      description="Pick the model and reasoning effort for each agent, from your own connections. Billed by your provider."
      footer={
        <>
          <div className="mr-auto min-w-0 text-[12.5px]" role={error ? "alert" : "status"}>
            {error ? (
              <span className="text-critical">{error}</span>
            ) : (
              <Tooltip
                label={
                  <span className="block max-w-xs">
                    Every agent reads a typical pull request (~25K tokens) and writes an answer that
                    grows with reasoning effort. Prompt-cache discounts are not counted, so most
                    reviews cost less.
                  </span>
                }
              >
                <span className="cursor-help text-fg-muted">
                  ≈{" "}
                  <span className="font-semibold text-fg tabular">{fmtUSD(cost.usd)}</span> per
                  typical review
                  {cost.unpriced.length > 0 && (
                    <span className="text-warning">
                      {" "}
                      · no price set for {cost.unpriced.join(", ")}
                    </span>
                  )}
                </span>
              </Tooltip>
            )}
          </div>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={save.isPending}
            disabled={Boolean(problem)}
            title={problem || undefined}
            onClick={() => {
              setError(null);
              save.mutate(toSpec(draft));
            }}
          >
            {tier ? "Save changes" : "Create tier"}
          </Button>
        </>
      }
    >
      <div className="grid gap-5">
        <div className="grid gap-4 sm:grid-cols-[1fr_9rem]">
          <Field label="Name" htmlFor="tier-name">
            <Input
              id="tier-name"
              value={draft.name}
              maxLength={40}
              placeholder="e.g. Fast and cheap"
              onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            />
          </Field>
          <Field label="Max comments" htmlFor="tier-max" hint="Per review.">
            <Input
              id="tier-max"
              type="number"
              min={1}
              max={workspace.limits.max_comments}
              value={draft.maxComments}
              onChange={(e) =>
                setDraft((d) => ({
                  ...d,
                  maxComments: Math.min(
                    workspace.limits.max_comments,
                    Math.max(1, Number(e.target.value) || 1),
                  ),
                }))
              }
            />
          </Field>
        </div>

        <StageSection
          title="Finders"
          blurb="Each enabled lens reads the diff independently and reports every defect it can prove."
          model={draft.finderModel}
          effort={draft.finderEffort}
          onModel={(ref) => setStage("finder", ref)}
          onEffort={(e) => setDraft((d) => ({ ...d, finderEffort: e }))}
          modelOptions={modelOptions}
          effortOptions={effortOptions(draft.finderModel)}
          lenses={workspace.lenses.finders}
          drafts={draft.finders}
          onLens={(id, patch) => setLens("finders", id, patch)}
          lensEffortOptions={(ref) => effortOptions(ref || draft.finderModel)}
          idPrefix="finder"
          custom={draft.customFinders}
          customMax={workspace.limits.max_custom_finders}
          customHint="What should this lens look for? For example: Check every Django view for a missing permission_classes declaration."
          warn
          onAddCustom={() => addCustom("customFinders")}
          onCustom={(id, patch) => setCustom("customFinders", id, patch)}
          onRemoveCustom={(id) => removeCustom("customFinders", id)}
        />

        <StageSection
          title="Verifiers"
          blurb="Each enabled lens tries to refute every finding without seeing the finder's reasoning. Also merges duplicates."
          model={draft.verifierModel}
          effort={draft.verifierEffort}
          onModel={(ref) => setStage("verifier", ref)}
          onEffort={(e) => setDraft((d) => ({ ...d, verifierEffort: e }))}
          modelOptions={modelOptions}
          effortOptions={effortOptions(draft.verifierModel)}
          lenses={workspace.lenses.verifiers}
          drafts={draft.verifiers}
          onLens={(id, patch) => setLens("verifiers", id, patch)}
          lensEffortOptions={(ref) => effortOptions(ref || draft.verifierModel)}
          idPrefix="verifier"
          custom={draft.customVerifiers}
          customMax={workspace.limits.max_custom_verifiers}
          customHint="The question each finding must survive. For example: Is the tenant id checked before this query runs?"
          onAddCustom={() => addCustom("customVerifiers")}
          onCustom={(id, patch) => setCustom("customVerifiers", id, patch)}
          onRemoveCustom={(id) => removeCustom("customVerifiers", id)}
        />
      </div>
    </Dialog>
  );
}

function StageSection({
  title,
  blurb,
  model,
  effort,
  onModel,
  onEffort,
  modelOptions,
  effortOptions,
  lenses,
  drafts,
  onLens,
  lensEffortOptions,
  idPrefix,
  custom,
  customMax,
  customHint,
  warn,
  onAddCustom,
  onCustom,
  onRemoveCustom,
}: {
  title: string;
  blurb: string;
  model: string;
  effort: Effort;
  onModel: (ref: string) => void;
  onEffort: (e: Effort) => void;
  modelOptions: { value: string; label: string }[];
  effortOptions: { value: string; label: string }[];
  lenses: Lens[];
  drafts: Record<string, LensDraft>;
  onLens: (id: string, patch: Partial<LensDraft>) => void;
  lensEffortOptions: (ref: string) => { value: string; label: string }[];
  idPrefix: string;
  custom: CustomDraft[];
  customMax: number;
  customHint: string;
  /** Flag wording that makes a finder drop findings. */
  warn?: boolean;
  onAddCustom: () => void;
  onCustom: (id: string, patch: Partial<CustomDraft>) => void;
  onRemoveCustom: (id: string) => void;
}) {
  return (
    <section className="rounded-xl border border-line" aria-labelledby={`${idPrefix}-title`}>
      <div className="border-b border-line bg-surface-2/50 px-4 py-3">
        <h3 id={`${idPrefix}-title`} className="font-display text-[14px] font-semibold text-fg">
          {title}
        </h3>
        <p className="mt-0.5 text-[12px] leading-snug text-fg-muted">{blurb}</p>
        <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_10rem]">
          <Field label="Default model" htmlFor={`${idPrefix}-model`}>
            <Select
              id={`${idPrefix}-model`}
              variant="field"
              value={model}
              onChange={onModel}
              options={modelOptions}
              placeholder="Choose a model"
              capitalize={false}
              allowEmpty={false}
              className="w-full"
            />
          </Field>
          <Field label="Default effort" htmlFor={`${idPrefix}-effort`}>
            <Select
              id={`${idPrefix}-effort`}
              variant="field"
              value={effortOptions.length ? effort : ""}
              onChange={(v) => v && onEffort(v as Effort)}
              options={effortOptions}
              placeholder={effortOptions.length ? "Choose" : "Not adjustable"}
              allowEmpty={false}
              disabled={!effortOptions.length}
              className="w-full"
            />
          </Field>
        </div>
      </div>

      <ul className="divide-y divide-line">
        {lenses.map((l) => {
          const d = drafts[l.id];
          const checkboxId = `${idPrefix}-lens-${l.id}`;
          const lensEfforts = lensEffortOptions(d.model);
          return (
            <li
              key={l.id}
              className="grid gap-2 px-4 py-3 md:grid-cols-[1fr_14rem_9rem] md:items-center"
            >
              <label htmlFor={checkboxId} className="flex min-w-0 cursor-pointer items-start gap-2.5">
                <span className="relative mt-0.5 grid h-4 w-4 shrink-0 place-items-center">
                  <input
                    id={checkboxId}
                    type="checkbox"
                    checked={d.enabled}
                    onChange={(e) => onLens(l.id, { enabled: e.target.checked })}
                    className={cn(
                      "peer h-4 w-4 cursor-pointer appearance-none rounded border border-line-strong bg-surface",
                      "transition-colors checked:border-brand-600 checked:bg-brand-600",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
                    )}
                  />
                  <Check
                    aria-hidden
                    className="pointer-events-none absolute h-3 w-3 text-white opacity-0 peer-checked:opacity-100"
                  />
                </span>
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium text-fg">{lensName(l.id)}</span>
                  <span className="block text-[12px] leading-snug text-fg-muted">
                    {l.description}
                  </span>
                </span>
              </label>
              <LensOverrides
                enabled={d.enabled}
                name={lensName(l.id)}
                model={d.model}
                effort={d.effort}
                modelOptions={modelOptions}
                effortOptions={lensEfforts}
                onChange={(patch) => onLens(l.id, patch)}
              />
            </li>
          );
        })}

        {custom.map((c, i) => {
          const warnings = warn ? filterWarnings(c.instruction) : [];
          return (
            <li key={c.id} className="grid gap-2.5 bg-brand-500/[0.03] px-4 py-3">
              <div className="flex items-center justify-between gap-2">
                <p className="text-[11px] font-semibold tracking-[0.05em] text-brand-600 uppercase dark:text-brand-300">
                  Your lens {i + 1}
                </p>
                <button
                  type="button"
                  onClick={() => onRemoveCustom(c.id)}
                  aria-label={`Remove ${c.name || "this custom lens"}`}
                  className="grid h-7 w-7 place-items-center rounded-md text-fg-faint hover:bg-surface-2 hover:text-fg"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
              <div className="grid gap-2 md:grid-cols-[1fr_14rem_9rem] md:items-start">
                <div className="grid gap-2">
                  <Input
                    value={c.name}
                    maxLength={40}
                    placeholder="Lens name, e.g. Tenant isolation"
                    aria-label="Custom lens name"
                    onChange={(e) => onCustom(c.id, { name: e.target.value })}
                  />
                  <textarea
                    value={c.instruction}
                    maxLength={2000}
                    rows={3}
                    placeholder={customHint}
                    aria-label="What this lens checks"
                    onChange={(e) => onCustom(c.id, { instruction: e.target.value })}
                    className={cn(
                      "w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 text-[13px] leading-relaxed text-fg shadow-xs",
                      "placeholder:text-fg-faint focus:border-brand-500 focus:outline-none",
                      "focus:shadow-[0_0_0_3px_color-mix(in_srgb,var(--color-brand-500)_16%,transparent)]",
                    )}
                  />
                </div>
                <LensOverrides
                  enabled
                  name={c.name || "custom lens"}
                  model={c.model}
                  effort={c.effort}
                  modelOptions={modelOptions}
                  effortOptions={lensEffortOptions(c.model)}
                  onChange={(patch) => onCustom(c.id, patch)}
                />
              </div>
              {warnings.map((w) => (
                <p key={w} className="flex items-start gap-1.5 text-[12px] text-warning">
                  <TriangleAlert aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  {w}
                </p>
              ))}
            </li>
          );
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-2.5">
        <Button size="xs" variant="ghost" onClick={onAddCustom} disabled={custom.length >= customMax}>
          <Plus aria-hidden className="h-3 w-3" />
          Write your own lens
        </Button>
        <span className="text-[11.5px] text-fg-faint">
          {custom.length}/{customMax} · runs after PullAgent's own instructions, never instead of them
        </span>
      </div>
    </section>
  );
}

function LensOverrides({
  enabled,
  name,
  model,
  effort,
  modelOptions,
  effortOptions,
  onChange,
}: {
  enabled: boolean;
  name: string;
  model: string;
  effort: Effort | "";
  modelOptions: { value: string; label: string }[];
  effortOptions: { value: string; label: string }[];
  onChange: (patch: { model?: string; effort?: Effort | "" }) => void;
}) {
  return (
    <>
      <div className={cn(!enabled && "pointer-events-none opacity-40")}>
        <Select
          variant="field"
          value={model}
          onChange={(v) => onChange({ model: v, effort: "" })}
          options={modelOptions}
          placeholder="Default model"
          capitalize={false}
          ariaLabel={`${name} model`}
          disabled={!enabled}
          className="w-full"
        />
      </div>
      <div className={cn(!enabled && "pointer-events-none opacity-40")}>
        <Select
          variant="field"
          value={effort}
          onChange={(v) => onChange({ effort: v as Effort | "" })}
          options={effortOptions}
          placeholder="Default effort"
          ariaLabel={`${name} effort`}
          disabled={!enabled || !effortOptions.length}
          className="w-full"
        />
      </div>
    </>
  );
}
