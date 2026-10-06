import { useMutation } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, CircleX, FlaskConical } from "lucide-react";
import * as React from "react";

import { Button, Dialog, Select } from "../../components/ui";
import { ApiError, api, type Connection, type SampleReviewResult, type Workspace } from "../../lib/api";
import { cn } from "../../lib/utils";

/**
 * "Is this model any good at reviewing?" - one finder and one verifier on a
 * small change with a planted bug, on the customer's own key. A passing
 * connection test only proves the model answers; this shows whether it
 * catches a real defect before real pull requests depend on it.
 */

// Shown so the reader knows exactly what the model was given. Kept in step
// with cr.app.workspace.SAMPLE_DIFF.
const PLANTED = `+def paginate(items, page, size):
+    """Return page \`page\` (zero-indexed) of \`items\`, \`size\` items per page."""
+    if page < 0 or size <= 0:
+        raise ValueError("page must be >= 0 and size > 0")
+    start = page * size
+    end = start + size - 1      # <- the planted bug: drops the last item
+    return items[start:end]`;

export function SampleReviewDialog({
  workspace,
  connection,
  onClose,
}: {
  workspace: Workspace;
  connection: Connection | null;
  onClose: () => void;
}) {
  const [model, setModel] = React.useState("");
  const [results, setResults] = React.useState<Record<string, SampleReviewResult>>({});

  React.useEffect(() => {
    setModel(connection?.models[0]?.name || "");
    setResults({});
  }, [connection]);

  const run = useMutation({
    mutationFn: (name: string) => api.sampleReview(workspace.account, connection!.id, name),
    onSuccess: (r) => setResults((all) => ({ ...all, [r.model]: r })),
  });

  const result = results[model];

  return (
    <Dialog
      open={Boolean(connection)}
      onOpenChange={(open) => !open && onClose()}
      size="lg"
      title="Try a sample review"
      description="Runs one finder and one verifier on a small change with a planted bug, on your key. Costs a cent or two."
      footer={
        <>
          {run.error && (
            <p role="alert" className="mr-auto text-[12.5px] text-critical">
              {run.error instanceof ApiError ? run.error.message : "The sample review failed."}
            </p>
          )}
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button
            variant="primary"
            loading={run.isPending}
            disabled={!model}
            onClick={() => run.mutate(model)}
          >
            <FlaskConical aria-hidden className="h-3.5 w-3.5" />
            {result ? "Run again" : "Run sample review"}
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        <div className="grid gap-1.5 sm:max-w-sm">
          <label htmlFor="sample-model" className="text-[12.5px] font-medium text-fg">
            Model
          </label>
          <Select
            id="sample-model"
            variant="field"
            value={model}
            onChange={setModel}
            options={(connection?.models || []).map((m) => ({ value: m.name, label: m.name }))}
            placeholder="Choose a model"
            capitalize={false}
            allowEmpty={false}
          />
        </div>

        <div>
          <p className="mb-1.5 text-[12px] text-fg-muted">The change it reviews</p>
          {/* No ligatures: code must read as typed, "<=" not "≤". */}
          <pre className="overflow-x-auto rounded-xl border border-line bg-surface-2 p-3 font-mono text-[11.5px] leading-relaxed text-fg [font-variant-ligatures:none]">
            {PLANTED}
          </pre>
        </div>

        {run.isPending && (
          <p role="status" className="text-[12.5px] text-fg-muted">
            Reviewing… this takes about as long as a small real review.
          </p>
        )}
        {result && !run.isPending && <Outcome r={result} />}
      </div>
    </Dialog>
  );
}

function Outcome({ r }: { r: SampleReviewResult }) {
  const tone = r.verified ? "good" : r.found ? "warning" : "critical";
  const Icon = r.verified ? CircleCheck : r.found ? CircleAlert : CircleX;
  const headline = r.verified
    ? "Caught the planted bug, and it survived verification."
    : r.found
      ? "Found the bug, but the verifier rejected it, so it would not have been posted."
      : "Missed the planted bug.";
  return (
    <div
      role="status"
      className={cn(
        "rounded-xl border px-4 py-3",
        tone === "good" && "border-good/30 bg-good/8",
        tone === "warning" && "border-warning/40 bg-warning/8",
        tone === "critical" && "border-critical/30 bg-critical/6",
      )}
    >
      <p className="flex items-start gap-2 text-[13px] font-medium text-fg">
        <Icon
          aria-hidden
          className={cn(
            "mt-0.5 h-4 w-4 shrink-0",
            tone === "good" && "text-good",
            tone === "warning" && "text-warning",
            tone === "critical" && "text-critical",
          )}
        />
        {headline}
      </p>
      {r.claim && <p className="mt-1.5 pl-6 text-[12.5px] text-fg-muted">“{r.claim}”</p>}
      <p className="mt-1.5 pl-6 text-[12px] text-fg-muted tabular">
        {r.elapsed_s}s · ${r.cost_usd.toFixed(4)}
        {r.other_findings > 0 &&
          ` · ${r.other_findings} other ${r.other_findings === 1 ? "comment" : "comments"} (none expected)`}
      </p>
      {r.errors.length > 0 && (
        <p className="mt-1.5 pl-6 text-[12px] text-critical">{r.errors[0]}</p>
      )}
    </div>
  );
}
