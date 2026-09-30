/**
 * The primitive layer.
 *
 * Built on Radix for anything with behaviour worth not reinventing — focus
 * traps, roving tabindex, escape handling, portal placement — and styled with
 * Tailwind. Everything here is presentation only; no component fetches.
 */
import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu";
import * as SelectPrimitive from "@radix-ui/react-select";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cva, type VariantProps } from "class-variance-authority";
import { Check, ChevronDown, Loader2 } from "lucide-react";
import * as React from "react";

import { cn } from "../../lib/utils";

/* --- surfaces ------------------------------------------------------------ */

export function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "rounded-xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(16,24,40,.04)]",
        "dark:border-slate-700/60 dark:bg-[#151b28]",
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex items-start justify-between gap-4 px-5 pt-4", className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.ComponentProps<"h3">) {
  return (
    <h3
      className={cn("font-display text-[15px] font-600 text-slate-900 dark:text-slate-100", className)}
      {...props}
    />
  );
}

export function CardDescription({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("mt-0.5 text-[13px] text-slate-500 dark:text-slate-400", className)} {...props} />;
}

export function CardBody({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("px-5 py-4", className)} {...props} />;
}

/* --- button -------------------------------------------------------------- */

const buttonStyles = cva(
  "inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors " +
    "disabled:pointer-events-none disabled:opacity-50 whitespace-nowrap",
  {
    variants: {
      variant: {
        primary: "bg-brand-600 text-white hover:bg-brand-700",
        secondary:
          "border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 " +
          "dark:border-slate-700 dark:bg-transparent dark:text-slate-200 dark:hover:bg-slate-800",
        ghost: "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
        danger: "bg-[var(--critical)] text-white hover:opacity-90",
        success: "bg-[var(--good)] text-white hover:opacity-90",
      },
      size: {
        sm: "h-8 px-2.5 text-[13px]",
        md: "h-9 px-3.5 text-sm",
        icon: "h-8 w-8",
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

export function Badge({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium",
        "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
        className,
      )}
      {...props}
    />
  );
}

/**
 * A coloured dot plus its label. The pairing is the point: status colour never
 * carries meaning on its own, so the word is always rendered beside it.
 */
export function StatusDot({ color, label, className }: { color: string; label: string; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)}>
      <span
        aria-hidden
        className="h-2 w-2 shrink-0 rounded-full ring-2 ring-white dark:ring-[#151b28]"
        style={{ background: color }}
      />
      <span className="text-[13px] text-slate-700 dark:text-slate-300">{label}</span>
    </span>
  );
}

/* --- inputs -------------------------------------------------------------- */

export function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-9 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-900 " +
          "placeholder:text-slate-400 focus:border-brand-500 focus:outline-none " +
          "dark:border-slate-700 dark:bg-[#0f1523] dark:text-slate-100 dark:placeholder:text-slate-500",
        className,
      )}
      {...props}
    />
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
  return (
    <SelectPrimitive.Root value={value || ALL} onValueChange={(v) => onChange(v === ALL ? "" : v)}>
      <SelectPrimitive.Trigger
        className={cn(
          "inline-flex h-9 items-center justify-between gap-2 rounded-lg border border-slate-200 " +
            "bg-white px-3 text-[13px] text-slate-700 hover:bg-slate-50 " +
            "dark:border-slate-700 dark:bg-[#0f1523] dark:text-slate-200 dark:hover:bg-slate-800",
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <ChevronDown className="h-3.5 w-3.5 opacity-60" />
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={4}
          className="z-50 max-h-72 overflow-auto rounded-lg border border-slate-200 bg-white p-1 shadow-lg dark:border-slate-700 dark:bg-[#151b28]"
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
      className="relative flex cursor-pointer select-none items-center rounded-md py-1.5 pl-7 pr-3 text-[13px] text-slate-700 outline-none data-[highlighted]:bg-slate-100 dark:text-slate-200 dark:data-[highlighted]:bg-slate-800"
    >
      <SelectPrimitive.ItemIndicator className="absolute left-2">
        <Check className="h-3.5 w-3.5" />
      </SelectPrimitive.ItemIndicator>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
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
        sideOffset={6}
        align="end"
        className={cn(
          "z-50 min-w-44 rounded-lg border border-slate-200 bg-white p-1 shadow-lg " +
            "dark:border-slate-700 dark:bg-[#151b28]",
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
        "flex cursor-pointer select-none items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] " +
          "text-slate-700 outline-none data-[highlighted]:bg-slate-100 " +
          "dark:text-slate-200 dark:data-[highlighted]:bg-slate-800",
        className,
      )}
      {...props}
    />
  );
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
          className="z-50 max-w-xs rounded-md bg-slate-900 px-2.5 py-1.5 text-[12px] leading-snug text-white shadow-lg dark:bg-slate-700"
        >
          {label}
          <TooltipPrimitive.Arrow className="fill-slate-900 dark:fill-slate-700" />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

/* --- state --------------------------------------------------------------- */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("skeleton rounded-md", className)} />;
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
      {Icon && <Icon className="h-7 w-7 text-slate-300 dark:text-slate-600" />}
      <p className="font-display text-sm font-600 text-slate-700 dark:text-slate-200">{title}</p>
      {hint && <p className="max-w-sm text-[13px] text-slate-500 dark:text-slate-400">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong";
  return (
    <EmptyState
      title="Could not load this"
      hint={message}
      action={
        onRetry ? (
          <Button size="sm" onClick={onRetry}>
            Try again
          </Button>
        ) : undefined
      }
    />
  );
}
