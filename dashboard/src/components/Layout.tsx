/**
 * The application shell: sidebar navigation, top bar, theme toggle, account menu.
 *
 * The dashboard used to be one scrolling page. It is now routed, because the
 * questions a team asks are different shapes - "what did this cost us", "what
 * did it say about this repo", "why is that queued job stuck", "who is waiting
 * for access" - and stacking all of them into one column means every one of
 * them is buried.
 *
 * Scrolling belongs to the content pane, not to the document. `html`, `body`
 * and `#root` are pinned to 100% height in theme.css and the shell is a
 * full-height flex row, so the rail and the top bar stay put while only the
 * pane under the header moves. The previous layout put the sidebar in normal
 * document flow at `lg:static`, which meant a long table scrolled the
 * navigation off the top of the screen with it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BadgeCheck,
  BookOpen,
  Boxes,
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
import { ALL, useWorkspace } from "../workspace";
import { api, type Run } from "../lib/api";
import { cn } from "../lib/utils";
import {
  Badge,
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Select,
  Tooltip,
} from "./ui";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  adminOnly?: boolean;
}

/**
 * Grouped, because eight flat links is a list to read rather than a structure
 * to navigate. The split is by question: what happened, what is the machine
 * doing about it, and how is it set up.
 */
const NAV: { section: string | null; items: NavItem[] }[] = [
  {
    section: null,
    items: [{ to: "/", label: "Overview", icon: Gauge }],
  },
  {
    section: "Activity",
    items: [
      { to: "/runs", label: "Reviews", icon: Activity },
      { to: "/findings", label: "Findings", icon: ListChecks },
      { to: "/repos", label: "Repositories", icon: FolderGit2 },
    ],
  },
  {
    section: "Operations",
    items: [
      { to: "/suppressions", label: "Suppressions", icon: ListFilter },
      { to: "/queue", label: "Queue", icon: BadgeCheck },
      { to: "/accounts", label: "Access", icon: ShieldCheck, adminOnly: true },
    ],
  },
  {
    section: "Configuration",
    items: [{ to: "/models", label: "Models", icon: Boxes }],
  },
];

/** Route -> title, so the top bar can name the page without each page telling it. */
const TITLES: Record<string, string> = {
  "/": "Overview",
  "/runs": "Reviews",
  "/findings": "Findings",
  "/repos": "Repositories",
  "/suppressions": "Suppressions",
  "/queue": "Queue",
  "/accounts": "Access",
  "/models": "Models",
};

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

/** The CR mark. A gradient tile rather than a flat square, and the one place
 *  in the UI where brand colour is decorative. */
function Logomark({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "grid shrink-0 place-items-center rounded-[10px] bg-linear-to-br from-brand-400 to-brand-700",
        "font-display text-white shadow-[0_2px_8px_-2px_color-mix(in_srgb,var(--color-brand-600)_60%,transparent)]",
        "inset-shadow-[0_1px_0_rgba(255,255,255,.25)]",
        className,
      )}
    >
      PA
    </div>
  );
}

function NavRow({ item, adminBadge }: { item: NavItem; adminBadge?: boolean }) {
  const { to, label, icon: Icon } = item;
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        cn(
          "group relative flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium",
          "transition-colors duration-150",
          isActive
            ? "bg-brand-500/10 text-brand-700 dark:bg-brand-500/14 dark:text-brand-200"
            : "text-fg-muted hover:bg-surface-2 hover:text-fg",
        )
      }
    >
      {({ isActive }) => (
        <>
          {/* The accent rail: a short bar on the left edge of the active row.
              Colour alone is doing less work because the pill is there too. */}
          <span
            aria-hidden
            className={cn(
              "absolute top-1/2 -left-2.5 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-brand-500 transition-all duration-200",
              isActive ? "opacity-100" : "scale-y-50 opacity-0",
            )}
          />
          <Icon
            className={cn(
              "h-4 w-4 shrink-0 transition-colors",
              isActive ? "text-brand-600 dark:text-brand-300" : "text-fg-faint group-hover:text-fg-muted",
            )}
          />
          {label}
          {adminBadge && (
            <Badge className="ml-auto px-1 py-0 text-[9.5px] tracking-wide uppercase">admin</Badge>
          )}
        </>
      )}
    </NavLink>
  );
}

export function Layout() {
  const { theme, toggle } = useTheme();
  const { data: me } = useMe();
  const [open, setOpen] = React.useState(false);
  const location = useLocation();

  // A tapped nav link on mobile should close the drawer behind it.
  React.useEffect(() => setOpen(false), [location.pathname]);

  // Same query key the Overview uses, so this shares one poll rather than
  // opening a second one for the same endpoint.
  const { account, setAccount, current, list } = useWorkspace();
  const { data: active } = useQuery<Run[]>({
    queryKey: ["active", account],
    queryFn: () => api.activeRuns(account),
    enabled: Boolean(account),
    refetchInterval: 5_000,
    retry: false,
  });

  const sections = NAV.map((s) => ({
    ...s,
    items: s.items.filter((n) => !n.adminOnly || me?.is_admin),
  })).filter((s) => s.items.length > 0);

  const title = TITLES[location.pathname] ?? (location.pathname.startsWith("/runs/") ? "Review" : "");
  const inFlight = active?.length ?? 0;

  return (
    <div className="flex h-full overflow-hidden bg-plane">
      {/* Backdrop only exists on mobile, where the sidebar is a drawer. */}
      {open && (
        <div
          className="animate-fade-in fixed inset-0 z-30 bg-[#0b101b]/50 backdrop-blur-[2px] lg:hidden"
          onClick={() => setOpen(false)}
          aria-hidden
        />
      )}

      <aside
        className={cn(
          // h-full + flex-col is what keeps the rail pinned: it is a sibling of
          // the scrolling pane, never inside it.
          "fixed inset-y-0 left-0 z-40 flex h-full w-[264px] shrink-0 flex-col",
          "border-r border-line bg-rail",
          "transition-transform duration-200 ease-out lg:static lg:translate-x-0",
          open ? "translate-x-0 shadow-lg" : "-translate-x-full",
        )}
      >
        <div className="flex h-15 shrink-0 items-center gap-2.5 px-4">
          <Logomark className="h-8 w-8 text-[12px] font-bold" />
          <div className="min-w-0">
            <p className="font-display text-[14px] leading-tight font-bold text-fg">PullAgent</p>
            <p className="truncate text-[11px] leading-tight text-fg-faint">
              {me?.login ? `@${me.login}` : "AI code review"}
            </p>
          </div>
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

        {/* Only the link list scrolls, and only when the list outgrows the rail. */}
        <nav className="min-h-0 flex-1 overflow-y-auto px-4 py-2">
          {sections.map((s, i) => (
            <div key={s.section ?? "root"} className={cn(i > 0 && "mt-5")}>
              {s.section && (
                <p className="mb-1.5 px-2.5 text-[10.5px] font-semibold tracking-[0.08em] text-fg-faint uppercase">
                  {s.section}
                </p>
              )}
              <div className="space-y-0.5">
                {s.items.map((item) => (
                  <NavRow key={item.to} item={item} adminBadge={item.adminOnly} />
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="shrink-0 border-t border-line p-3">
          <a
            href={list?.install_url || "https://github.com/apps/pullagent"}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[12.5px] text-fg-muted transition-colors hover:bg-surface-2 hover:text-fg"
          >
            <BookOpen className="h-3.5 w-3.5 text-fg-faint" />
            Docs &amp; install
          </a>
        </div>
      </aside>

      <div className="flex h-full min-w-0 flex-1 flex-col">
        <header className="flex h-15 shrink-0 items-center gap-3 border-b border-line bg-surface/80 px-4 backdrop-blur-xl sm:px-6">
          <Button
            size="icon"
            variant="ghost"
            className="-ml-1 lg:hidden"
            onClick={() => setOpen(true)}
            aria-label="Open navigation"
          >
            <Menu className="h-4 w-4" />
          </Button>

          <h2 className="truncate font-display text-[15px] font-bold text-fg">{title}</h2>

          {/* "Is it working right now" belongs in the chrome, not only on the
              overview - it is the one fact worth knowing on every page. */}
          {inFlight > 0 && (
            <Tooltip label={`${inFlight} review${inFlight === 1 ? "" : "s"} running right now`}>
              <span className="hidden items-center gap-1.5 rounded-full border border-line bg-surface-2 py-1 pr-2.5 pl-2 text-[12px] text-fg-muted sm:inline-flex">
                <span
                  aria-hidden
                  className="animate-pulse-ring h-1.5 w-1.5 rounded-full"
                  style={{ background: "var(--series-1)" }}
                />
                {inFlight} in flight
              </span>
            </Tooltip>
          )}

          <div className="ml-auto flex items-center gap-1.5">
            {list && list.workspaces.length + (list.is_admin ? 1 : 0) > 1 && (
              <Select
                value={account}
                onChange={(v) => v && setAccount(v)}
                options={[
                  ...(list.is_admin ? [{ value: ALL, label: "All workspaces" }] : []),
                  ...list.workspaces.map((w) => ({
                    value: w.login,
                    label: w.kind === "personal" ? `${w.login} (you)` : w.login,
                  })),
                ]}
                placeholder="Workspace"
                ariaLabel="Workspace"
                variant="field"
                capitalize={false}
                allowEmpty={false}
                className="h-8 max-w-[14rem] min-w-36"
              />
            )}
            <Tooltip label={theme === "dark" ? "Switch to light" : "Switch to dark"}>
              <Button size="icon" variant="ghost" onClick={toggle} aria-label="Toggle theme">
                {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </Button>
            </Tooltip>

            {me?.signed_in ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button className="flex items-center gap-2 rounded-lg py-1 pr-1.5 pl-1 transition-colors hover:bg-surface-2">
                    {me.avatar_url ? (
                      <img
                        src={me.avatar_url}
                        alt=""
                        className="h-7 w-7 rounded-full ring-1 ring-line"
                      />
                    ) : (
                      <span className="grid h-7 w-7 place-items-center rounded-full bg-linear-to-br from-brand-400 to-brand-700 text-[11px] font-semibold text-white">
                        {me.login?.[0]?.toUpperCase()}
                      </span>
                    )}
                    <span className="hidden text-[13px] font-medium text-fg sm:block">
                      {me.login}
                    </span>
                    <ChevronDown className="h-3.5 w-3.5 text-fg-faint" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <div className="px-2.5 py-2">
                    <p className="text-[13px] font-semibold text-fg">{me.name || me.login}</p>
                    <p className="mt-0.5 text-[12px] text-fg-muted">
                      {me.is_admin ? "Administrator" : "Member"}
                    </p>
                  </div>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    className="text-critical data-[highlighted]:text-critical"
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
              <Button
                variant="primary"
                size="sm"
                onClick={() => window.location.assign("/auth/login")}
              >
                Sign in
              </Button>
            ) : null}
          </div>
        </header>

        {/* The one scrolling region in the app. `key` restarts the entrance
            animation on navigation so a route change reads as a change. */}
        <main className="min-h-0 flex-1 overflow-y-auto">
          {/* Outside the per-route container, so moving between pages does not
              remount the banner and ask GitHub again. */}
          {list && current && !current.installed && (
            <div className="mx-auto max-w-[1500px] px-4 pt-6 sm:px-6">
              <InstallBanner key={current.login} account={current.login} url={list.install_url} />
            </div>
          )}
          <div key={location.pathname} className="animate-fade-up mx-auto max-w-[1500px] px-4 py-6 sm:px-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

/** Shown on a workspace where the App is not installed: nothing there is
 *  reviewed, so the empty pages need a reason and a next step.
 *
 *  Installing happens on GitHub, in another tab, and its webhook can land
 *  after the person comes back (or never, on a laptop). So while this banner
 *  is up, and only then, it asks GitHub about this one account: when it
 *  appears, when the tab comes back into view, and on "Check again". */
function InstallBanner({ account, url }: { account: string; url: string }) {
  const qc = useQueryClient();
  const [checked, setChecked] = React.useState(false);
  const check = useMutation({
    mutationFn: () => api.checkInstallation(account),
    onSettled: async () => {
      await qc.invalidateQueries({ queryKey: ["workspaces"] });
      setChecked(true);
    },
  });
  const { mutate, isPending } = check;
  // Coming back to a tab fires both "focus" and "visibilitychange"; one ask
  // is enough, and one in flight is enough.
  const pending = React.useRef(false);
  pending.current = isPending;
  const lastAsked = React.useRef(0);

  React.useEffect(() => {
    const ask = () => {
      if (document.visibilityState !== "visible" || pending.current) return;
      if (Date.now() - lastAsked.current < 2_000) return;
      lastAsked.current = Date.now();
      mutate();
    };
    ask();
    window.addEventListener("focus", ask);
    document.addEventListener("visibilitychange", ask);
    return () => {
      window.removeEventListener("focus", ask);
      document.removeEventListener("visibilitychange", ask);
    };
  }, [account, mutate]);

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-3 rounded-xl border border-brand-500/25 bg-brand-500/6 px-4 py-3"
    >
      <p className="min-w-0 flex-1 text-[13px] text-fg">
        <span className="font-semibold">PullAgent is not installed on {account}.</span>{" "}
        <span className="text-fg-muted">
          {checked
            ? `Already installed? Make sure ${account} was the account you picked on GitHub.`
            : `Install it to have ${account}'s pull requests reviewed. Nothing appears here until then.`}
        </span>
      </p>
      <div className="flex shrink-0 items-center gap-2">
        <Button variant="ghost" size="sm" loading={isPending} onClick={() => mutate()}>
          Check again
        </Button>
        {url && (
          <a href={url} target="_blank" rel="noreferrer">
            <Button variant="primary" size="sm">
              Install on GitHub
            </Button>
          </a>
        )}
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
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="font-display text-[26px] leading-tight font-bold tracking-[-0.02em] text-fg">
          {title}
        </h1>
        {description && (
          <p className="mt-1.5 max-w-2xl text-[13.5px] leading-relaxed text-fg-muted">
            {description}
          </p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Filters live in one row above the content, never scattered. */
export function FilterBar({ children }: { children: React.ReactNode }) {
  return <div className="mb-3 flex flex-wrap items-center gap-2">{children}</div>;
}
