import { useState } from "react";
import { api, type Refusal } from "@/lib/api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

// Run now and the scheduler's stored refusal share this one handler, so a refusal reads and resolves the
// same wherever it shows (backend: SourcingRefused in local_sourcing.py).

const money = (value: unknown) => `$${Number(value).toFixed(Number(value) < 1 ? 4 : 2)}`;

// A job fetch that failed after Apify started it. A failure the user can fix is retried, so say why and
// when; any other ends the run (no retry_at), so say why only.
function fetchFailed(detail: Refusal["detail"]): string {
  const message = String(detail.message ?? "The connection to Apify failed").replace(/\.$/, "");
  if (!detail.retry_at) return `Fetching jobs failed: ${message}.`;
  const retryTime = new Date(String(detail.retry_at)).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
  return `Fetching jobs failed: ${message}. Retrying at ${retryTime}.`;
}

const NOTICE: Record<string, (detail: Refusal["detail"]) => string> = {
  budget: (detail) =>
    `Your monthly budget has ${money(detail.remaining)} left and a job check needs at least ${money(detail.minimal)}. Raise the budget in Settings to check again.`,
  no_key: () => "Add your Apify key in Settings before checking for jobs.",
  no_titles: () => "Add at least one job title in your search filters before checking for jobs.",
  run_in_progress: () => "A job check is already running.",
  price_unreadable: (detail) =>
    String(
      detail.message ?? "Apify's price could not be read, so nothing was charged. Try again later.",
    ),
  tls: fetchFailed,
  ingest_failed: fetchFailed,
};

// A confirm for the three refusals the user can resolve, a notice for the rest.
export function RefusalNotice({
  refusal,
  busy,
  onRun,
}: {
  refusal: Refusal | null;
  busy: boolean;
  onRun: (confirmBacklog?: boolean) => Promise<boolean>;
}) {
  const [problem, setProblem] = useState<string | null>(null);
  if (!refusal) return null;
  const { reason, detail } = refusal;

  async function confirmPrice() {
    setProblem(null);
    try {
      await api("/api/sourcing/confirm-price", { price: detail.price });
      await onRun();
    } catch (caught) {
      setProblem((caught as Error).message);
    }
  }

  let message: string;
  let action: { label: string; run: () => void } | null = null;
  if (reason === "price_unconfirmed" || reason === "price_rose") {
    message =
      reason === "price_rose"
        ? `Apify now charges ${money(detail.price)} per job, up from the ${money(detail.confirmed)} you accepted.`
        : `Apify charges ${money(detail.price)} per job. Nothing is charged until you accept this price.`;
    action = { label: `Accept ${money(detail.price)} per job and run`, run: confirmPrice };
  } else if (reason === "backlog_unconfirmed") {
    message = `${detail.waiting} applications are already waiting for you. Fetch more jobs anyway?`;
    action = { label: "Fetch more anyway", run: () => void onRun(true) };
  } else {
    const notice = NOTICE[reason];
    message = notice ? notice(detail) : reason.replaceAll("_", " ");
  }

  return (
    <Alert data-testid="sourcing-refusal" data-reason={reason}>
      <AlertDescription className="space-y-2">
        <p>{message}</p>
        {problem && <p className="text-destructive">{problem}</p>}
        {action && (
          <Button size="sm" disabled={busy} onClick={action.run}>
            {action.label}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
