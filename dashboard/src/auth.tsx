/**
 * Sign-in state for the UI.
 *
 * The server is the authority — `guard()` in `server.py` rejects reads with a
 * 401 once sign-in is configured. This module exists so the UI can *explain*
 * that instead of rendering a grid of broken panels, and so admin-only
 * navigation is hidden rather than merely failing when clicked. Hiding a link
 * is courtesy; the server-side check is the security.
 */
import { useQuery } from "@tanstack/react-query";
import { LogIn, ShieldAlert } from "lucide-react";
import * as React from "react";

import { ApiError, api, type Me } from "./lib/api";
import { Button, Card, CardBody, EmptyState } from "./components/ui";

export function useMe() {
  return useQuery<Me>({
    queryKey: ["me"],
    queryFn: api.me,
    staleTime: 60_000,
    retry: false,
  });
}

/**
 * Renders children once we know who the viewer is. When sign-in is configured
 * and nobody is signed in, shows the door rather than a wall of 401s.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { data: me, isLoading, error } = useMe();

  if (isLoading) {
    return (
      <div className="grid h-full place-items-center bg-plane">
        <div className="flex flex-col items-center gap-3">
          <div className="animate-pulse-ring grid h-11 w-11 place-items-center rounded-xl bg-linear-to-br from-brand-400 to-brand-700 font-display text-[15px] font-bold text-white">
            PA
          </div>
          <div className="skeleton h-2 w-24 rounded-full" />
        </div>
      </div>
    );
  }

  // /api/me is unauthenticated, so a failure here is the server being down —
  // not a missing session. Say so, rather than inviting a pointless sign-in.
  if (error) {
    return (
      <SignInScreen
        title="Cannot reach the server"
        hint={error instanceof Error ? error.message : "The API did not respond."}
      />
    );
  }

  if (me && me.sign_in_configured && !me.signed_in) {
    return (
      <SignInScreen
        title="Sign in to PullAgent"
        hint="This dashboard is restricted to signed-in members of this installation."
        showButton
      />
    );
  }

  return <>{children}</>;
}

function SignInScreen({
  title,
  hint,
  showButton,
}: {
  title: string;
  hint: string;
  showButton?: boolean;
}) {
  return (
    <div className="relative grid h-full place-items-center overflow-hidden bg-plane px-4">
      {/* Decorative only: a faint grid and a brand wash, so the sign-in screen
          is not a single flat rectangle. Nothing here carries meaning. */}
      <div aria-hidden className="plane-grid pointer-events-none absolute inset-0" />
      <div
        aria-hidden
        className="pointer-events-none absolute -top-40 left-1/2 h-80 w-[36rem] -translate-x-1/2 rounded-full opacity-25 blur-[90px]"
        style={{ background: "var(--color-brand-500)" }}
      />

      <Card className="animate-fade-up relative w-full max-w-sm shadow-lg">
        <CardBody className="flex flex-col items-center gap-3 px-8 py-10 text-center">
          <div className="grid h-12 w-12 place-items-center rounded-2xl bg-linear-to-br from-brand-400 to-brand-700 font-display text-[16px] font-bold text-white shadow-md inset-shadow-[0_1px_0_rgba(255,255,255,.25)]">
            PA
          </div>
          <div>
            <h1 className="font-display text-[19px] font-bold tracking-[-0.01em] text-fg">{title}</h1>
            <p className="mt-1.5 text-[13px] leading-relaxed text-fg-muted">{hint}</p>
          </div>
          {showButton && (
            <Button
              variant="primary"
              className="mt-2 w-full"
              onClick={() => location.assign("/auth/login")}
            >
              <LogIn className="h-4 w-4" />
              Continue with GitHub
            </Button>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

/** Wrap an admin-only page. The matching server route checks again. */
export function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { data: me } = useMe();
  if (me && !me.is_admin) {
    return (
      <EmptyState
        icon={ShieldAlert}
        title="Administrators only"
        hint="Ask an existing administrator to grant you access."
      />
    );
  }
  return <>{children}</>;
}

/** Turn a 401 from any query into the sign-in screen instead of an error card. */
export function isAuthError(e: unknown): boolean {
  return e instanceof ApiError && e.status === 401;
}
