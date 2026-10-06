import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Navigate, RouterProvider, createBrowserRouter } from "react-router-dom";

import { AuthGate, RequireAdmin } from "./auth";
import { WorkspaceProvider } from "./workspace";
import { Layout } from "./components/Layout";
import { TooltipProvider } from "./components/ui";
import { ApiError } from "./lib/api";
import { AccountsPage } from "./pages/Accounts";
import { FindingsPage } from "./pages/Findings";
import { ModelsPage } from "./pages/Models";
import { OverviewPage } from "./pages/Overview";
import { QueuePage } from "./pages/Queue";
import { ReposPage } from "./pages/Repos";
import { RunDetailPage } from "./pages/RunDetail";
import { RunsPage } from "./pages/Runs";
import { SuppressionsPage } from "./pages/Suppressions";
import "./theme.css";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // A 401 means "sign in", not "try again harder" — retrying it just
      // delays the sign-in screen. Same for 403/404.
      retry: (count, error) =>
        error instanceof ApiError && error.status < 500 ? false : count < 2,
      refetchOnWindowFocus: false,
      staleTime: 10_000,
    },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: "runs", element: <RunsPage /> },
      { path: "runs/:id", element: <RunDetailPage /> },
      { path: "findings", element: <FindingsPage /> },
      { path: "repos", element: <ReposPage /> },
      { path: "suppressions", element: <SuppressionsPage /> },
      { path: "queue", element: <QueuePage /> },
      { path: "models", element: <ModelsPage /> },
      {
        path: "accounts",
        element: (
          <RequireAdmin>
            <AccountsPage />
          </RequireAdmin>
        ),
      },
      // Unknown client-side routes land on the overview rather than a blank
      // screen — the server already serves index.html for any path.
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <AuthGate>
          <WorkspaceProvider>
            <RouterProvider router={router} />
          </WorkspaceProvider>
        </AuthGate>
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
