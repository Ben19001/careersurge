import { useEffect, useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { api, type Batch, type Job } from "@/lib/api";
import { usePoll } from "@/lib/poll";
import { RefusalNotice } from "@/components/refusal-notice";
import { useSourcing } from "@/lib/use-sourcing";
import { ATS_LABEL, formatDate, formatDuration, formatPay } from "@/lib/format";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";

// Above this many jobs the screen warns (it never blocks): one batch this size runs for hours.
const SOFT_WARNING_JOBS = 100;

const PROFILE_ROWS: [string, string][] = [
  ["full_name", "Name"],
  ["email", "Email"],
  ["phone", "Phone"],
  ["current_location", "Location"],
  ["linkedin_url", "LinkedIn"],
  ["github_url", "GitHub"],
  ["salary_expectation", "Salary expectation"],
];

const CONSENT_ROWS: [string, string][] = [
  ["auto_consent_authorized", "Agree to routine consent questions"],
  ["background_check_authorized", "Agree to background checks"],
  ["arbitration_authorized", "Agree to arbitration clauses"],
];

function WhatWillBeSent({ inputs }: { inputs: NonNullable<Batch["inputs"]> }) {
  const shown = PROFILE_ROWS.filter(([key]) => inputs.profile[key]);
  return (
    <Card data-testid="what-will-be-sent">
      <CardHeader>
        <CardTitle className="text-base">What will be sent</CardTitle>
        <p className="text-sm text-muted-foreground">
          These are frozen when you approve. Editing your profile later does not change what this
          batch sends. A question that needs anything else waits for you in Needs you.
        </p>
      </CardHeader>
      <CardContent className="grid gap-5 text-sm sm:grid-cols-2">
        <section>
          <h3 className="mb-2 font-medium">Profile</h3>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            {shown.map(([key, label]) => (
              <div key={key} className="contents">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="break-words">{String(inputs.profile[key])}</dd>
              </div>
            ))}
          </dl>
        </section>
        <section className="space-y-5">
          <div>
            <h3 className="mb-2 font-medium">Resume</h3>
            {inputs.resume ? (
              <p className="break-all">
                {inputs.resume.name}
                <span className="block text-xs text-muted-foreground">
                  sha256 {inputs.resume.sha256.slice(0, 16)}…
                </span>
              </p>
            ) : (
              <p className="text-destructive">The resume file is missing.</p>
            )}
          </div>
          <div>
            <h3 className="mb-2 font-medium">Consent policy</h3>
            <ul className="space-y-1">
              {CONSENT_ROWS.map(([key, label]) => (
                <li key={key}>
                  {inputs.consent[key] ? "✓" : "✗"} {label}
                </li>
              ))}
            </ul>
            <Link to="/profile" className="mt-2 inline-block text-xs underline">
              Change in Profile
            </Link>
          </div>
        </section>
        <section className="sm:col-span-2">
          <h3 className="mb-2 font-medium">Saved answers</h3>
          {inputs.global_answers.length === 0 ? (
            <p className="text-muted-foreground">None yet.</p>
          ) : (
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              {inputs.global_answers.map((entry) => (
                <div key={entry.question} className="contents">
                  <dt className="text-muted-foreground">{entry.question}</dt>
                  <dd className="break-words">{entry.answer}</dd>
                </div>
              ))}
            </dl>
          )}
        </section>
      </CardContent>
    </Card>
  );
}

function JobRow({ job, checked, onToggle }: { job: Job; checked: boolean; onToggle: () => void }) {
  const pay = formatPay(job.pay);
  const match = job.repost_match;
  return (
    <li className="flex gap-3 border-b px-4 py-3 last:border-b-0" data-testid="job-row">
      <Checkbox
        checked={checked}
        onCheckedChange={onToggle}
        aria-label={`Include ${job.title ?? "this job"} at ${job.company ?? "this company"}`}
        className="mt-1"
      />
      <div className="min-w-0 flex-1">
        <p className="font-medium">
          {job.title ?? "Untitled role"}
          <span className="font-normal text-muted-foreground">
            {" "}
            at {job.company ?? "unknown company"}
          </span>
        </p>
        <p className="text-sm text-muted-foreground">
          {[job.location, pay, ATS_LABEL[job.ats] ?? job.ats].filter(Boolean).join(" · ")}
        </p>
        {match?.kind === "strong" && (
          <p className="mt-1 text-sm text-destructive" data-testid="strong-repost">
            Same posting as {match.title ?? "an earlier listing"} at{" "}
            {match.company ?? "the same company"}, first seen {formatDate(match.first_seen_at)}.{" "}
            {match.url && /^https?:\/\//.test(match.url) && (
              <a className="underline" href={match.url} target="_blank" rel="noreferrer">
                View earlier posting.
              </a>
            )}{" "}
            Left unticked so you do not apply twice.
          </p>
        )}
        {match?.kind === "weak" && (
          <Badge variant="secondary" className="mt-1" data-testid="weak-repost">
            Possible repost of {match.title ?? "an earlier listing"} (first seen{" "}
            {formatDate(match.first_seen_at)})
          </Badge>
        )}
      </div>
    </li>
  );
}

export function ApproveScreen() {
  const { data: batch, error, refresh } = usePoll(() => api<Batch>("/api/batch"), 10000);
  const [checked, setChecked] = useState<Set<number> | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const sourcing = useSourcing();

  // The first load decides the starting boxes (everything except a strong repost); a poll after that
  // only drops ids that left `prepared`, so the user's unticking is never undone.
  useEffect(() => {
    if (!batch) return;
    setChecked((current) => {
      const ids = new Set(batch.jobs.map((job) => job.id));
      if (current === null) {
        return new Set(
          batch.jobs.filter((job) => job.repost_match?.kind !== "strong").map((job) => job.id),
        );
      }
      return new Set([...current].filter((id) => ids.has(id)));
    });
  }, [batch]);

  const jobs = useMemo(() => batch?.jobs ?? [], [batch]);
  const approveIds = jobs.filter((job) => checked?.has(job.id)).map((job) => job.id);
  const skipIds = jobs.filter((job) => !checked?.has(job.id)).map((job) => job.id);

  // The ATS queues run side by side, so the batch finishes when the longest queue does.
  const etaSeconds = useMemo(() => {
    if (!batch) return 0;
    const perApplication = batch.eta.seconds_per_application;
    const load: Record<string, number> = { ...batch.eta.queued };
    for (const job of jobs) {
      if (checked?.has(job.id)) load[job.ats] = (load[job.ats] ?? 0) + 1;
    }
    return Math.max(0, ...Object.values(load)) * perApplication;
  }, [batch, jobs, checked]);

  async function decide(approve: number[], skip: number[]) {
    setBusy(true);
    setProblem(null);
    try {
      const result = await api<{ approved: number; skipped: number }>("/api/batch/approve", {
        approve_ids: approve,
        skip_ids: skip,
      });
      setNotice(
        [
          result.approved > 0 &&
            `Approved ${result.approved} ${result.approved === 1 ? "job" : "jobs"}.`,
          result.skipped > 0 && `Skipped ${result.skipped}.`,
        ]
          .filter(Boolean)
          .join(" "),
      );
      await refresh();
    } catch (caught) {
      setProblem((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggle(id: number) {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (!batch) {
    return <p className="text-muted-foreground">{error ? error.message : "Loading…"}</p>;
  }

  return (
    <div className="space-y-6">
      <RefusalNotice refusal={sourcing.refusal} busy={sourcing.busy} onRun={sourcing.run} />
      {sourcing.fetching && (
        <Alert data-testid="fetching-jobs">
          <AlertDescription>Fetching jobs… they appear here when ready.</AlertDescription>
        </Alert>
      )}
      {notice && (
        <Alert data-testid="approve-notice">
          <AlertDescription>
            {notice}{" "}
            <Link to="/progress" className="underline">
              Watch progress
            </Link>
          </AlertDescription>
        </Alert>
      )}
      {problem && (
        <Alert variant="destructive" data-testid="approve-problem">
          <AlertDescription>{problem}</AlertDescription>
        </Alert>
      )}

      {jobs.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center text-muted-foreground">
            Nothing to approve right now. New jobs appear here after the next job check, or{" "}
            <Link to="/settings" className="underline">
              check now in Settings
            </Link>
            .
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="text-lg">
                {jobs.length} {jobs.length === 1 ? "job is" : "jobs are"} ready for your approval
              </CardTitle>
              <p className="text-sm text-muted-foreground">
                Ticked jobs will be applied to from this computer. Unticked jobs stay here for
                later.
              </p>
            </CardHeader>
            <CardContent className="p-0">
              <ul>
                {jobs.map((job) => (
                  <JobRow
                    key={job.id}
                    job={job}
                    checked={checked?.has(job.id) ?? false}
                    onToggle={() => toggle(job.id)}
                  />
                ))}
              </ul>
            </CardContent>
          </Card>

          {batch.inputs ? (
            <WhatWillBeSent inputs={batch.inputs} />
          ) : (
            <Alert variant="destructive">
              <AlertDescription>
                There is no profile yet, so nothing can be approved. Set up your profile in{" "}
                <Link to="/onboarding" className="underline">
                  onboarding
                </Link>
                .
              </AlertDescription>
            </Alert>
          )}

          {approveIds.length > SOFT_WARNING_JOBS && (
            <Alert data-testid="big-batch-warning">
              <AlertDescription>
                {approveIds.length} jobs is a big batch. It will take hours and the computer must
                stay awake. You can still approve it, or untick some and approve the rest later.
              </AlertDescription>
            </Alert>
          )}

          <div className="sticky bottom-0 -mx-4 flex flex-wrap items-center justify-between gap-3 border-t bg-background px-4 py-3">
            <p className="text-sm text-muted-foreground" data-testid="eta">
              {approveIds.length === 0
                ? "Tick at least one job to approve."
                : `About ${formatDuration(etaSeconds)} to finish, if nothing blocks.`}
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                disabled={busy || skipIds.length === 0}
                onClick={() => decide([], skipIds)}
              >
                Skip unticked ({skipIds.length})
              </Button>
              <Button
                disabled={busy || approveIds.length === 0 || !batch.inputs}
                onClick={() => decide(approveIds, [])}
              >
                Approve {approveIds.length} {approveIds.length === 1 ? "job" : "jobs"}
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
