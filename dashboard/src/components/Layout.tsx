/**
 * The application shell: sidebar navigation, top bar, theme toggle, account menu.
 *
 * The dashboard used to be one scrolling page. It is now routed, because the
 * questions a team asks are different shapes — "what did this cost us", "what
 * did it say about this repo", "why is that queued job stuck", "who is waiting
 * for access" — and stacking all of them into one column means every one of
 * them is buried.
 */
import {
  Activity,
  BadgeCheck,
  ChevronDown,
  FolderGit2,
  Gauge,
  ListChecks,
  ListFilter,
  LogOut,
  Menu,
  Moon,
  ShieldCheck,
  Sun,
  X,
} from "lucide-react";
import * as React from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useMe } from "../auth";
import { cn } from "../lib/utils";
import {
  Badge,
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "./ui";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  adminOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: Gauge },
  { to: "/runs", label: "Reviews", icon: Activity },
  { to: "/findings", label: "Findings", icon: ListChecks },
  { to: "/repos", label: "Repositories", icon: FolderGit2 },
  { to: "/suppressions", label: "Suppressions", icon: ListFilter },
  { to: "/queue", label: "Queue", icon: BadgeCheck },
  { to: "/accounts", label: "Access", icon: ShieldCheck, adminOnly: true },
];

function useTheme() {
  const [theme, setTheme] = React.useState<"light" | "dark">(
    () =>
      (localStorage.getItem("cr-theme") as "light" | "dark") ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  );
  React.useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("cr-theme", theme);
  }, [theme]);
  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) };
}

export function Layout() {
  const { theme, toggle } = useTheme();
  const { data: me } = useMe();
  const [open, setOpen] = React.useState(false);
  const location = useLocation();

  // A tapped nav link on mobile should close the drawer behind it.
  React.useEffect(() => setOpen(false), [location.pathname]);

  const items = NAV.filter((n) => !n.adminOnly || me?.is_admin);

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[232px_1fr]">
      {/* Backdrop only exists on mobile, where the sidebar is a drawer. */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-slate-900/40 lg:hidden"
          onClick={() => setOpen(false)}
          aria-hidden
        />
      )}

      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-[232px] flex-col border-r border-slate-200 bg-white",
          "transition-transform lg:static lg:translate-x-0",
          "dark:border-slate-800 dark:bg-[#101624]",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-14 items-center gap-2 px-4">
          <div className="grid h-7 w-7 place-items-center rounded-lg bg-brand-600 font-display text-[13px] font-700 text-white">
            CR
          </div>
          <span className="font-display text-[15px] font-700 text-slate-900 dark:text-slate-100">
            Code Review
          </span>
          <Button
            size="icon"
            variant="ghost"
            className="ml-auto lg:hidden"
            onClick={() => setOpen(false)}
            aria-label="Close navigation"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2.5 py-2">
          {items.map(({ to, label, icon: Icon, adminOnly }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium transition-colors",
                  isActive
                    ? "bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-200"
                    : "text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800",
                )
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              {label}
              {adminOnly && (
                <Badge className="ml-auto bg-slate-100 text-[10px] dark:bg-slate-800">admin</Badge>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-slate-200 p-2.5 dark:border-slate-800">
          <a
            href="https://github.com/apps"
            target="_blank"
            rel="noreferrer"
            className="block rounded-lg px-2.5 py-2 text-[12px] text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
          >
            Docs &amp; install
          </a>
        </div>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-slate-200 bg-white/85 px-4 backdrop-blur dark:border-slate-800 dark:bg-[#101624]/85">
          <Button
            size="icon"
            variant="ghost"
            className="lg:hidden"
            onClick={() => setOpen(true)}
            aria-label="Open navigation"
          >
            <Menu className="h-4 w-4" />
          </Button>

          <div className="ml-auto flex items-center gap-2">
            <Button size="icon" variant="ghost" onClick={toggle} aria-label="Toggle theme">
              {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>

            {me?.signed_in ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button className="flex items-center gap-2 rounded-lg px-1.5 py-1 hover:bg-slate-100 dark:hover:bg-slate-800">
                    {me.avatar_url ? (
                      <img src={me.avatar_url} alt="" className="h-6 w-6 rounded-full" />
                    ) : (
                      <span className="grid h-6 w-6 place-items-center rounded-full bg-brand-600 text-[11px] font-600 text-white">
                        {me.login?.[0]?.toUpperCase()}
                      </span>
                    )}
                    <span className="hidden text-[13px] text-slate-700 sm:block dark:text-slate-200">
                      {me.login}
                    </span>
                    <ChevronDown className="h-3.5 w-3.5 opacity-60" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <div className="px-2.5 py-1.5">
                    <p className="text-[13px] font-600 text-slate-900 dark:text-slate-100">
                      {me.name || me.login}
                    </p>
                    <p className="text-[12px] text-slate-500 dark:text-slate-400">
                      {me.is_admin ? "Administrator" : "Member"}
                    </p>
                  </div>
                  <DropdownMenuItem
                    onSelect={async () => {
                      await fetch("/auth/logout", { method: "POST" });
                      window.location.assign("/");
                    }}
                  >
                    <LogOut className="h-3.5 w-3.5" />
                    Sign out
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : me?.sign_in_configured ? (
              <Button variant="primary" size="sm" onClick={() => window.location.assign("/auth/login")}>
                Sign in
              </Button>
            ) : null}
          </div>
        </header>

        <main className="min-w-0 flex-1 px-4 py-5 sm:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="font-display text-xl font-700 text-slate-900 dark:text-slate-50">{title}</h1>
        {description && (
          <p className="mt-0.5 text-[13px] text-slate-500 dark:text-slate-400">{description}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Filters live in one row above the content, never scattered. */
export function FilterBar({ children }: { children: React.ReactNode }) {
  return <div className="mb-3 flex flex-wrap items-center gap-2">{children}</div>;
}
