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
      <div className="grid min-h-screen place-items-center">
        <div className="skeleton h-9 w-40 rounded-lg" />
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
        title="Sign in to CR"
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
    <div className="grid min-h-screen place-items-center px-4">
      <Card className="w-full max-w-sm">
        <CardBody className="flex flex-col items-center gap-3 py-10 text-center">
          <div className="grid h-11 w-11 place-items-center rounded-xl bg-brand-600 font-display text-base font-700 text-white">
            CR
          </div>
          <div>
            <h1 className="font-display text-lg font-700 text-slate-900 dark:text-slate-50">{title}</h1>
            <p className="mt-1 text-[13px] text-slate-500 dark:text-slate-400">{hint}</p>
          </div>
          {showButton && (
            <Button variant="primary" className="mt-1 w-full" onClick={() => location.assign("/auth/login")}>
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
