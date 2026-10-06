import { useMutation, useQueryClient } from "@tanstack/react-query";
import { NotebookPen, Pencil, Plus } from "lucide-react";
import * as React from "react";

import {
  Button,
  Card,
  CardBody,
  CardDescription,
  CardHeader,
  CardTitle,
  Dialog,
  Field,
  Input,
} from "../../components/ui";
import { ApiError, api, type Workspace } from "../../lib/api";
import { cn } from "../../lib/utils";

/**
 * Review guidelines per repository: house rules the reviewer should know -
 * conventions, things that look wrong but are deliberate, areas that matter
 * most. They apply to every review of the repository, managed or on the
 * workspace's own keys, and sit in the cached part of the prompt.
 */
export function GuidelinesCard({ ws }: { ws: Workspace }) {
  const [editing, setEditing] = React.useState<{ repo: string; text: string } | null>(null);
  const entries = Object.entries(ws.guidelines).sort(([a], [b]) => a.localeCompare(b));

  return (
    <Card className="mb-3 overflow-hidden">
      <CardHeader>
        <div>
          <CardTitle>Review guidelines</CardTitle>
          <CardDescription>
            House rules for a repository: conventions, deliberate patterns, what matters most.
            Every review of that repository reads them.
          </CardDescription>
        </div>
        <Button size="sm" onClick={() => setEditing({ repo: "", text: "" })}>
          <Plus aria-hidden className="h-3.5 w-3.5" />
          Add guidelines
        </Button>
      </CardHeader>
      {entries.length > 0 && (
        <CardBody className="pt-2 pb-1">
          <ul className="divide-y divide-line">
            {entries.map(([repo, text]) => (
              <li key={repo} className="flex items-start gap-3 py-3">
                <NotebookPen aria-hidden className="mt-0.5 h-4 w-4 shrink-0 text-fg-faint" />
                <div className="min-w-0 flex-1">
                  <p className="font-mono text-[12.5px] text-fg">{repo}</p>
                  <p className="mt-0.5 line-clamp-2 text-[12.5px] text-fg-muted">{text}</p>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setEditing({ repo, text })}
                  aria-label={`Edit guidelines for ${repo}`}
                >
                  <Pencil aria-hidden className="h-3.5 w-3.5" />
                  Edit
                </Button>
              </li>
            ))}
          </ul>
        </CardBody>
      )}
      <GuidelinesDialog ws={ws} editing={editing} onClose={() => setEditing(null)} />
    </Card>
  );
}

function GuidelinesDialog({
  ws,
  editing,
  onClose,
}: {
  ws: Workspace;
  editing: { repo: string; text: string } | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [repo, setRepo] = React.useState("");
  const [text, setText] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const existing = Boolean(editing?.repo);
  const max = ws.limits.max_guidelines;

  React.useEffect(() => {
    setRepo(editing?.repo || "");
    setText(editing?.text || "");
    setError(null);
  }, [editing]);

  const fullRepo = repo.includes("/") ? repo.trim() : `${ws.account}/${repo.trim()}`;
  const save = useMutation({
    mutationFn: (value: string) => api.saveGuidelines(ws.account, fullRepo, value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspace", ws.account] });
      onClose();
    },
    onError: (e) => setError(e instanceof ApiError ? e.message : "Could not save."),
  });

  return (
    <Dialog
      open={Boolean(editing)}
      onOpenChange={(open) => !open && onClose()}
      size="lg"
      title={existing ? `Guidelines for ${editing?.repo}` : "Add review guidelines"}
      description="Plain language. They go into every review of this repository, on PullAgent's models or yours."
      footer={
        <>
          {existing && (
            <Button
              variant="ghost"
              className="mr-auto text-critical"
              loading={save.isPending && !text}
              onClick={() => save.mutate("")}
            >
              Remove guidelines
            </Button>
          )}
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={save.isPending && Boolean(text)}
            disabled={!repo.trim() || !text.trim() || text.length > max}
            onClick={() => save.mutate(text)}
          >
            Save guidelines
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        {!existing && (
          <Field label="Repository" htmlFor="gl-repo">
            <Input
              id="gl-repo"
              list="gl-repos"
              value={repo}
              spellCheck={false}
              placeholder={`${ws.account}/repository`}
              onChange={(e) => setRepo(e.target.value)}
            />
            <datalist id="gl-repos">
              {ws.repos
                .filter((r) => !(r.toLowerCase() in ws.guidelines))
                .map((r) => (
                  <option key={r} value={r} />
                ))}
            </datalist>
          </Field>
        )}
        <Field
          label="Guidelines"
          htmlFor="gl-text"
          error={error}
          hint={
            <span className={cn(text.length > max && "text-critical")}>
              {text.length.toLocaleString()} / {max.toLocaleString()} characters. Avoid dates and
              IDs that change: they would stop the prompt from being cached.
            </span>
          }
        >
          <textarea
            id="gl-text"
            value={text}
            rows={9}
            onChange={(e) => setText(e.target.value)}
            placeholder={
              "e.g.\n- All database access goes through repositories in app/data; flag raw SQL elsewhere.\n- Feature flags are read once at start-up; per-request reads are deliberate in app/flags.\n- Money is always integer cents."
            }
            className={cn(
              "w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 font-mono text-[12.5px] leading-relaxed text-fg shadow-xs",
              "placeholder:text-fg-faint focus:border-brand-500 focus:outline-none",
              "focus:shadow-[0_0_0_3px_color-mix(in_srgb,var(--color-brand-500)_16%,transparent)]",
            )}
          />
        </Field>
      </div>
    </Dialog>
  );
}
