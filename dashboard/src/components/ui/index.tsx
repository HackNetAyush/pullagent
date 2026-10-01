/**
 * The primitive layer.
 *
 * Built on Radix for anything with behaviour worth not reinventing - focus
 * traps, roving tabindex, escape handling, portal placement - and styled with
 * Tailwind. Everything here is presentation only; no component fetches.
 *
 * Colour comes from the theme-following tokens in `theme.css` (`bg-surface`,
 * `border-line`, `text-fg-muted`) rather than from paired `slate-x dark:slate-y`
 * classes. One class per surface means a theme change is a token change, not a
 * sweep through every component.
 */
import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu";
import * as SelectPrimitive from "@radix-ui/react-select";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cva, type VariantProps } from "class-variance-authority";
import { Check, ChevronDown, Loader2 } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/utils";

/* --- surfaces ------------------------------------------------------------ */

export function Card({
  className,
  interactive,
  ...props
}: React.ComponentProps<"div"> & { interactive?: boolean }) {
  return (
    <div
      className={cn(
        "rounded-2xl border border-line bg-surface shadow-xs",
        // A card that is a link target lifts on hover; a card that is just a
        // container must not, or everything on the page looks clickable.
        interactive &&
          "transition-[transform,box-shadow,border-color] duration-200 hover:-translate-y-px hover:border-line-strong hover:shadow-md",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("flex items-start justify-between gap-4 px-5 pt-4.5 pb-1", className)}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: React.ComponentProps<"h3">) {
  return (
    <h3
      className={cn("font-display text-[14.5px] font-semibold text-fg", className)}
      {...props}
    />
  );
}

export function CardDescription({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("mt-1 text-[12.5px] leading-snug text-fg-muted", className)} {...props} />;
}

export function CardBody({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("px-5 py-4", className)} {...props} />;
}

/** A hairline that spans a card edge to edge, ignoring the body padding. */
export function CardDivider({ className }: { className?: string }) {
  return <div className={cn("h-px bg-line", className)} />;
}

/* --- button -------------------------------------------------------------- */

const buttonStyles = cva(
  "relative inline-flex items-center justify-center gap-1.5 rounded-lg font-medium whitespace-nowrap " +
    "transition-[background-color,border-color,color,box-shadow,transform] duration-150 " +
    "active:scale-[.98] disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        // The primary sits on a brand gradient with a top inner highlight, so
        // it reads as raised without a heavy drop shadow under it.
        primary:
          "bg-brand-600 bg-linear-to-b from-brand-500 to-brand-600 text-white shadow-xs " +
          "inset-shadow-[0_1px_0_rgba(255,255,255,.18)] hover:from-brand-500 hover:to-brand-700",
        secondary:
          "border border-line bg-surface text-fg shadow-xs hover:border-line-strong hover:bg-surface-2",
        ghost: "text-fg-muted hover:bg-surface-2 hover:text-fg",
        subtle: "bg-surface-2 text-fg hover:bg-surface-3",
        danger: "bg-critical text-white shadow-xs hover:brightness-110",
        success: "bg-good text-white shadow-xs hover:brightness-110",
      },
      size: {
        xs: "h-7 gap-1 px-2 text-[12px]",
        sm: "h-8 px-2.5 text-[13px]",
        md: "h-9 px-3.5 text-[13.5px]",
        icon: "h-8 w-8",
        "icon-lg": "h-9 w-9",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ComponentProps<"button">,
    VariantProps<typeof buttonStyles> {
  loading?: boolean;
}

export function Button({ className, variant, size, loading, children, ...props }: ButtonProps) {
  return (
    <button
      className={cn(buttonStyles({ variant, size }), className)}
      disabled={props.disabled || loading}
      {...props}
    >
      {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
      {children}
    </button>
  );
}

/* --- badges -------------------------------------------------------------- */

const badgeStyles = cva(
  "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium leading-5 ring-1 ring-inset",
  {
    variants: {
      tone: {
        neutral: "bg-surface-2 text-fg-muted ring-line",
        brand: "bg-brand-500/10 text-brand-700 ring-brand-500/20 dark:text-brand-200",
        good: "bg-good/10 text-good ring-good/25",
        warning: "bg-warning/12 text-warning ring-warning/25",
        critical: "bg-critical/10 text-critical ring-critical/25",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export function Badge({
  className,
  tone,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeStyles>) {
  return <span className={cn(badgeStyles({ tone }), className)} {...props} />;
}

/**
 * A coloured dot plus its label. The pairing is the point: status colour never
 * carries meaning on its own, so the word is always rendered beside it.
 *
 * `pulse` is for live states only. A halo that never stops is noise; one that
 * means "this is moving right now" is information.
 */
export function StatusDot({
  color,
  label,
  pulse,
  className,
}: {
  color: string;
  label: string;
  pulse?: boolean;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)}>
      <span
        aria-hidden
        className={cn("h-2 w-2 shrink-0 rounded-full", pulse && "animate-pulse-ring")}
        style={{ background: color }}
      />
      <span className="text-[13px] capitalize text-fg-muted">{label}</span>
    </span>
  );
}

/* --- inputs -------------------------------------------------------------- */

export function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-9 w-full rounded-lg border border-line bg-surface px-3 text-[13.5px] text-fg shadow-xs",
        "transition-[border-color,box-shadow] placeholder:text-fg-faint",
        "focus:border-brand-500 focus:shadow-[0_0_0_3px_color-mix(in_srgb,var(--color-brand-500)_16%,transparent)] focus:outline-none",
        className,
      )}
      {...props}
    />
  );
}

/** Search field with the magnifier baked in, so every page spells it the same. */
export function SearchInput({
  className,
  wrapperClassName,
  ...props
}: React.ComponentProps<"input"> & { wrapperClassName?: string }) {
  return (
    <div className={cn("relative", wrapperClassName)}>
      <svg
        aria-hidden
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-fg-faint"
      >
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3.2-3.2" />
      </svg>
      <Input className={cn("pl-8", className)} {...props} />
    </div>
  );
}

export interface SelectOption {
  value: string;
  label: string;
}

/** A Radix select so keyboard and screen-reader behaviour is not hand-rolled. */
export function Select({
  value,
  onChange,
  options,
  placeholder = "All",
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
}) {
  const ALL = "__all__";
  const active = Boolean(value);
  return (
    <SelectPrimitive.Root value={value || ALL} onValueChange={(v) => onChange(v === ALL ? "" : v)}>
      <SelectPrimitive.Trigger
        className={cn(
          "inline-flex h-9 items-center justify-between gap-2 rounded-lg border px-3 text-[13px] shadow-xs",
          "transition-colors data-[state=open]:border-brand-500",
          // A filter that is doing something looks different from one that is
          // not - otherwise a stale filter silently hides half the table.
          active
            ? "border-brand-500/40 bg-brand-500/8 text-brand-700 dark:text-brand-200"
            : "border-line bg-surface text-fg-muted hover:border-line-strong hover:bg-surface-2",
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <ChevronDown className="h-3.5 w-3.5 opacity-60" />
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={6}
          className="animate-pop-in z-50 max-h-72 min-w-[var(--radix-select-trigger-width)] overflow-auto rounded-xl border border-line bg-surface p-1 shadow-lg"
        >
          <SelectPrimitive.Viewport>
            <Item value={ALL}>{placeholder}</Item>
            {options.map((o) => (
              <Item key={o.value} value={o.value}>
                {o.label}
              </Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}

function Item({ value, children }: { value: string; children: React.ReactNode }) {
  return (
    <SelectPrimitive.Item
      value={value}
      className="relative flex cursor-pointer items-center rounded-lg py-1.5 pr-3 pl-7 text-[13px] text-fg-muted capitalize outline-none select-none data-[highlighted]:bg-surface-2 data-[highlighted]:text-fg data-[state=checked]:text-fg"
    >
      <SelectPrimitive.ItemIndicator className="absolute left-2 text-brand-600 dark:text-brand-300">
        <Check className="h-3.5 w-3.5" />
      </SelectPrimitive.ItemIndicator>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  );
}

/**
 * A segmented control for small mutually exclusive choices (a time window, a
 * view mode). The selected pill is a real background, not a colour change, so
 * the control reads as a switch rather than as a row of links.
 */
export function Segmented<T extends string | number>({
  value,
  onChange,
  options,
  className,
  ariaLabel,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-xl border border-line bg-surface-2 p-0.5 shadow-xs",
        className,
      )}
    >
      {options.map((o) => {
        const selected = o.value === value;
        return (
          <button
            key={String(o.value)}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(o.value)}
            className={cn(
              "rounded-[9px] px-2.5 py-1 text-[12px] font-medium transition-all duration-150",
              selected
                ? "bg-surface text-fg shadow-xs"
                : "text-fg-muted hover:text-fg",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/* --- menus & tooltips ---------------------------------------------------- */

export const DropdownMenu = DropdownMenuPrimitive.Root;
export const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger;

export function DropdownMenuContent({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Content>) {
  return (
    <DropdownMenuPrimitive.Portal>
      <DropdownMenuPrimitive.Content
        sideOffset={8}
        align="end"
        className={cn(
          "animate-pop-in z-50 min-w-52 rounded-xl border border-line bg-surface p-1 shadow-lg",
          className,
        )}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  );
}

export function DropdownMenuItem({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Item>) {
  return (
    <DropdownMenuPrimitive.Item
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] text-fg-muted " +
          "outline-none select-none data-[highlighted]:bg-surface-2 data-[highlighted]:text-fg",
        className,
      )}
      {...props}
    />
  );
}

export function DropdownMenuSeparator({ className }: { className?: string }) {
  return <div className={cn("my-1 h-px bg-line", className)} />;
}

export function TooltipProvider({ children }: { children: React.ReactNode }) {
  return <TooltipPrimitive.Provider delayDuration={200}>{children}</TooltipPrimitive.Provider>;
}

export function Tooltip({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={6}
          collisionPadding={8}
          className="animate-pop-in z-50 max-w-xs rounded-lg bg-[#0f1523] px-2.5 py-1.5 text-[12px] leading-snug text-white shadow-lg dark:bg-surface-3 dark:ring-1 dark:ring-line"
        >
          {label}
          <TooltipPrimitive.Arrow className="fill-[#0f1523] dark:fill-[var(--surface-3)]" />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

/* --- state --------------------------------------------------------------- */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("skeleton rounded-lg", className)} />;
}

export function EmptyState({
  icon: Icon,
  title,
  hint,
  action,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      {Icon && (
        <div className="mb-1 grid h-11 w-11 place-items-center rounded-2xl border border-line bg-surface-2 text-fg-faint">
          <Icon className="h-5 w-5" />
        </div>
      )}
      <p className="font-display text-[14px] font-semibold text-fg">{title}</p>
      {hint && <p className="max-w-sm text-[13px] leading-relaxed text-fg-muted">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong";
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <div className="mb-1 grid h-11 w-11 place-items-center rounded-2xl bg-critical/10 text-critical ring-1 ring-critical/20 ring-inset">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-5 w-5">
          <path d="M12 9v4M12 17h.01" strokeLinecap="round" />
          <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
        </svg>
      </div>
      <p className="font-display text-[14px] font-semibold text-fg">Could not load this</p>
      <p className="max-w-sm text-[13px] leading-relaxed text-fg-muted">{message}</p>
      {onRetry && (
        <Button size="sm" className="mt-2" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}
