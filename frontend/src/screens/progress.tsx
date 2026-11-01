import { useEffect, useState } from "react";
import { api, type FailedApplication, type Progress, type QueueState } from "@/lib/api";
import { RefusalNotice } from "@/components/refusal-notice";
import { useSourcing } from "@/lib/use-sourcing";
import { ATS_LABEL, formatCountdown } from "@/lib/format";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const STATUS_LABEL: [string, string][] = [
  ["prepared", "Waiting for approval"],
  ["pending", "Approved, waiting"],
  ["in_progress", "Applying now"],
  ["submitting", "Submitting"],
  ["applied", "Applied"],
  ["blocked_needs_input", "Needs you"],
  ["failed", "Failed"],
  ["cancelled", "Skipped"],
];

// Plain-English `skip_reason` of a failed application.
const FAILED_REASON: Record<string, string> = {
  listing_closed: "The posting was closed",
  unsupported_form: "The application form is not one the app can fill",
  timeout: "The application page timed out",
  error: "The application hit an error",
};

function stateText(queue: QueueState, secondsLeft: number | null): string {
  if (queue.state === "paused") {
    return queue.paused_reason === "blocked"
      ? "Paused: the site showed a block page three times in a row"
      : "Paused";
  }
  if (queue.state === "running") return "Applying now";
  if (queue.state === "backed_off" && secondsLeft !== null) {
    return `Backing off after a block page - resumes in ${formatCountdown(secondsLeft)}`;
  }
  // The cooldown also runs after the last application, so a countdown with nothing waiting is "Idle".
  if (queue.state === "cooling" && secondsLeft !== null && queue.pending > 0) {
    return `Next application in ${formatCountdown(secondsLeft)}`;
  }
  return queue.pending > 0 ? "Starting soon" : "Idle";
}

// What the newest finished job check found. A check that finds nothing ends in seconds, so without
// this line Run now looked like it had done nothing at all.
function lastCheckText(run: NonNullable<Progress["sourcing_run"]>): string {
  const when = new Date(run.ingested_at ?? run.started_at).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
  if (run.status === "failed") return `Last job check, at ${when}, failed. Nothing new was added.`;
  const found = run.rows_fetched ?? 0;
  if (found === 0) {
    return `Last job check, at ${when}: no jobs matched your search. With the automatic time range, each check only covers jobs posted since the one before. To see more, widen the titles or locations in Settings.`;
  }
  return `Last job check, at ${when}: found ${found} ${found === 1 ? "job" : "jobs"}. New ones are on the Approve screen.`;
}

// The Failed count opens this list (newest first); a row opens its reason, error code and posting link.
function FailedList({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<FailedApplication[] | null>(null);
  const [selected, setSelected] = useState<FailedApplication | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    api<{ failed: FailedApplication[] }>("/api/failed")
      .then((body) => setRows(body.failed))
      .catch((caught: Error) => setProblem(caught.message));
  }, []);

  return (
    <Card data-testid="failed-list">
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base">Failed applications</CardTitle>
        <Button size="sm" variant="outline" onClick={onClose}>
          Hide
        </Button>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {problem && <p className="text-destructive">{problem}</p>}
        {!rows && !problem && <p className="text-muted-foreground">Loading…</p>}
        {rows && rows.length === 0 && <p className="text-muted-foreground">Nothing failed.</p>}
        <ul className="divide-y">
          {rows?.map((row) => (
            <li key={row.id}>
              <button
                type="button"
                className="flex w-full cursor-pointer flex-col gap-0.5 py-2 text-left hover:bg-muted/50"
                data-testid="failed-row"
                onClick={() => setSelected(row)}
              >
                <span className="font-medium">
                  {row.title ?? "Untitled role"}
                  <span className="font-normal text-muted-foreground">
                    {" "}
                    at {row.company ?? "unknown company"}
                  </span>
                </span>
                <span className="text-muted-foreground">
                  {FAILED_REASON[row.skip_reason ?? "error"]} ·{" "}
                  {new Date(row.updated_at).toLocaleString()}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </CardContent>
      {selected && (
        <Dialog open onOpenChange={(open) => !open && setSelected(null)}>
          <DialogContent data-testid="failed-detail">
            <DialogHeader>
              <DialogTitle>{FAILED_REASON[selected.skip_reason ?? "error"]}</DialogTitle>
              <DialogDescription>
                {selected.title ?? "Untitled role"} at {selected.company ?? "unknown company"}
              </DialogDescription>
            </DialogHeader>
            <dl className="space-y-1 text-sm">
              {selected.error && (
                <div>
                  <dt className="text-muted-foreground">Error code</dt>
                  <dd className="font-mono" data-testid="failed-error">
                    {selected.error}
                  </dd>
                </div>
              )}
              <div>
                <dt className="text-muted-foreground">Attempts</dt>
                <dd>{selected.attempt_count}</dd>
              </div>
            </dl>
            {selected.url && /^https?:\/\//.test(selected.url) && (
              <a className="text-sm underline" href={selected.url} target="_blank" rel="noreferrer">
                View posting
              </a>
            )}
          </DialogContent>
        </Dialog>
      )}
    </Card>
  );
}

export function ProgressScreen() {
  const sourcing = useSourcing();
  const { progress: data, refresh } = sourcing;
  const [problem, setProblem] = useState<string | null>(null);
  const [showFailed, setShowFailed] = useState(false);
  // The countdown ticks every second from the server's clock, not the window's.
  const [tick, setTick] = useState(0);
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((current) => current + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (data) setOffset(data.server_time - Date.now() / 1000);
  }, [data]);

  async function control(action: "pause" | "resume", ats?: string) {
    setProblem(null);
    try {
      await api(`/api/queues/${action}`, ats ? { ats } : {});
      await refresh();
    } catch (caught) {
      setProblem((caught as Error).message);
    }
  }

  if (!data) return <p className="text-muted-foreground">Loading…</p>;

  void tick;
  const now = Date.now() / 1000 + offset;
  const queues = Object.entries(data.queues);
  const allPaused = queues.every(([, queue]) => queue.state === "paused");

  return (
    <div className="space-y-6">
      {problem && (
        <Alert variant="destructive">
          <AlertDescription>{problem}</AlertDescription>
        </Alert>
      )}
      <RefusalNotice refusal={sourcing.refusal} busy={sourcing.busy} onRun={sourcing.run} />
      {sourcing.fetching && (
        <Alert data-testid="fetching-jobs">
          <AlertDescription>
            Fetching jobs… they appear on the Approve screen when ready.
          </AlertDescription>
        </Alert>
      )}
      {data.sourcing_run && data.sourcing_run.status !== "started" && (
        <p className="text-sm text-muted-foreground" data-testid="last-check">
          {lastCheckText(data.sourcing_run)}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          Applications go out from this computer. A closed laptop pauses the queue.
        </p>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={allPaused} onClick={() => control("pause")}>
            Pause all
          </Button>
          <Button variant="outline" size="sm" onClick={() => control("resume")}>
            Resume all
          </Button>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        {queues.map(([ats, queue]) => {
          const secondsLeft = queue.next_at === null ? null : queue.next_at - now;
          return (
            <Card key={ats} data-testid={`queue-${ats}`}>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">{ATS_LABEL[ats] ?? ats}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <p data-testid={`queue-state-${ats}`}>{stateText(queue, secondsLeft)}</p>
                <p className="text-muted-foreground">{queue.pending} waiting</p>
                {queue.state === "paused" ? (
                  <Button size="sm" variant="outline" onClick={() => control("resume", ats)}>
                    Resume
                  </Button>
                ) : (
                  <Button size="sm" variant="outline" onClick={() => control("pause", ats)}>
                    Pause
                  </Button>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Applications</CardTitle>
        </CardHeader>
        <CardContent>
          <dl
            className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4"
            data-testid="counts"
          >
            {STATUS_LABEL.map(([status, label]) => (
              <div key={status}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="text-lg font-medium" data-testid={`count-${status}`}>
                  {status === "failed" && (data.counts.failed ?? 0) > 0 ? (
                    <button
                      type="button"
                      className="cursor-pointer underline"
                      aria-label={`${data.counts.failed} failed, show list`}
                      onClick={() => setShowFailed(true)}
                    >
                      {data.counts.failed}
                    </button>
                  ) : (
                    (data.counts[status] ?? 0)
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
      {showFailed && <FailedList onClose={() => setShowFailed(false)} />}
    </div>
  );
}
