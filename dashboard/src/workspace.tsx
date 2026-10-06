/**
 * The current workspace: the GitHub account (a person or an organisation)
 * whose reviews, findings, spend and settings every page shows.
 *
 * Every dashboard read is scoped to one, and the server refuses any account
 * the signed-in user does not belong to - this context only decides which of
 * *their* workspaces is on screen. CR administrators additionally get "All
 * workspaces" (`*`), which the server allows for them alone.
 */
import { useQuery } from "@tanstack/react-query";
import * as React from "react";

import { api, type WorkspaceList, type WorkspaceRef } from "./lib/api";

export const ALL = "*";
const STORAGE_KEY = "cr-workspace";

interface WorkspaceContextValue {
  /** The account every query is scoped to; "" until the list has loaded. */
  account: string;
  setAccount: (account: string) => void;
  /** The current workspace's entry, or null for "All workspaces". */
  current: WorkspaceRef | null;
  list: WorkspaceList | undefined;
  loading: boolean;
  error: unknown;
}

const Ctx = React.createContext<WorkspaceContextValue | null>(null);

function readStored(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function writeStored(value: string) {
  try {
    localStorage.setItem(STORAGE_KEY, value);
  } catch {
    /* private mode or blocked storage: the choice just won't persist */
  }
}

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const { data: list, isLoading, error } = useQuery<WorkspaceList>({
    queryKey: ["workspaces"],
    queryFn: api.workspaces,
    staleTime: 60_000,
  });
  const [chosen, setChosen] = React.useState(readStored);

  const account = React.useMemo(() => {
    if (!list) return "";
    const names = list.workspaces.map((w) => w.login.toLowerCase());
    if (chosen === ALL && list.is_admin) return ALL;
    if (chosen && names.includes(chosen.toLowerCase())) {
      return list.workspaces.find((w) => w.login.toLowerCase() === chosen.toLowerCase())!.login;
    }
    // A laptop install has no "own" account: show everything by default.
    if (list.is_admin && list.login === "local") return ALL;
    return list.workspaces[0]?.login || "";
  }, [list, chosen]);

  const setAccount = React.useCallback((value: string) => {
    setChosen(value);
    writeStored(value);
  }, []);

  const current =
    account && account !== ALL
      ? list?.workspaces.find((w) => w.login === account) || null
      : null;

  return (
    <Ctx.Provider value={{ account, setAccount, current, list, loading: isLoading, error }}>
      {children}
    </Ctx.Provider>
  );
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = React.useContext(Ctx);
  if (!ctx) throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  return ctx;
}

