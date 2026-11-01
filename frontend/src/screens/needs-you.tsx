import { useEffect, useState } from "react";
import {
  api,
  apiBlobUrl,
  apiUpload,
  type Blocked,
  type BlockedQuestion,
  type OpenedPosting,
} from "@/lib/api";
import { useBlocked } from "@/lib/blocked";
import { ATS_LABEL } from "@/lib/format";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";

type Scope = BlockedQuestion["scope"];

// A certification is made to one employer, so an attestation never offers the wider choices.
function scopeChoices(card: Blocked, question: BlockedQuestion): { value: Scope; label: string }[] {
  const choices: { value: Scope; label: string }[] = [{ value: "job", label: "This job only" }];
  if (card.has_company) {
    choices.push({ value: "company", label: `Any job at ${card.company ?? "this company"}` });
  }
  choices.push({ value: "global", label: "Every job" });
  return question.question_type === "attestation" ? choices.slice(0, 1) : choices;
}

function QuestionForm({
  card,
  question,
  onDone,
}: {
  card: Blocked;
  question: BlockedQuestion;
  onDone: () => void;
}) {
  const shape = question.field_shape;
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const choices = scopeChoices(card, question);
  // A wide attestation row (Jev can scope one that way) is narrowed to this job on save, so the
  // answer is stored as the card says.
  const [scope, setScope] = useState<Scope>(choices.length === 1 ? "job" : question.scope);
  const inputId = `q-${card.id}-${question.qna_id}`;

  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      if (shape.kind === "file") {
        if (!file) throw new Error("Choose a file first.");
        const form = new FormData();
        form.set("qna_id", String(question.qna_id));
        form.set("application_id", String(card.id));
        form.set("scope", scope);
        form.set("file", file);
        await apiUpload("/api/answer", form);
      } else {
        const value = shape.kind === "multi" ? picked : shape.kind === "single" ? text : text;
        await api("/api/answer", {
          qna_id: question.qna_id,
          application_id: card.id,
          value,
          scope,
        });
      }
      onDone();
    } catch (caught) {
      setProblem((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 py-5 first:pt-0 last:pb-0" data-testid="question">
      <Label htmlFor={inputId} className="block text-base font-medium">
        {question.question}
      </Label>
      {shape.kind === "text" &&
        (question.question_type === "open_ended" ? (
          <Textarea
            id={inputId}
            value={text}
            maxLength={shape.max_length ?? undefined}
            onChange={(e) => setText(e.target.value)}
          />
        ) : (
          <Input
            id={inputId}
            value={text}
            maxLength={shape.max_length ?? undefined}
            onChange={(e) => setText(e.target.value)}
          />
        ))}
      {/* One choice is a lone checkbox (an "I have read..." box): a radio with nothing to pick between
          read as broken, so it is a box to tick, and ticking it answers with that choice. */}
      {shape.kind === "single" && shape.options.length === 1 && (
        <div className="flex items-center gap-2">
          <Checkbox
            id={inputId}
            checked={text === shape.options[0]}
            onCheckedChange={(on) => setText(on ? shape.options[0] : "")}
          />
          <Label htmlFor={inputId} className="font-normal">
            {shape.options[0]}
          </Label>
        </div>
      )}
      {shape.kind === "single" && shape.options.length !== 1 && (
        <RadioGroup id={inputId} value={text} onValueChange={setText} className="gap-2">
          {shape.options.map((option) => (
            <div key={option} className="flex items-center gap-2">
              <RadioGroupItem value={option} id={`${inputId}-${option}`} />
              <Label htmlFor={`${inputId}-${option}`} className="font-normal">
                {option}
              </Label>
            </div>
          ))}
        </RadioGroup>
      )}
      {shape.kind === "multi" && (
        <div className="space-y-1" id={inputId}>
          {shape.options.map((option) => (
            <div key={option} className="flex items-center gap-2">
              <Checkbox
                id={`${inputId}-${option}`}
                checked={picked.includes(option)}
                onCheckedChange={(on) =>
                  setPicked((current) =>
                    on ? [...current, option] : current.filter((item) => item !== option),
                  )
                }
              />
              <Label htmlFor={`${inputId}-${option}`} className="font-normal">
                {option}
              </Label>
            </div>
          ))}
        </div>
      )}
      {shape.kind === "file" && (
        <input
          id={inputId}
          type="file"
          accept=".pdf,.doc,.docx"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="block text-sm"
        />
      )}
      {choices.length > 1 ? (
        <fieldset className="space-y-2" data-testid="reuse-scope">
          <legend className="text-sm text-muted-foreground">Reuse this answer for</legend>
          <RadioGroup value={scope} onValueChange={(next) => setScope(next as Scope)}>
            {choices.map((choice) => (
              <div key={choice.value} className="flex items-center gap-2">
                <RadioGroupItem value={choice.value} id={`${inputId}-scope-${choice.value}`} />
                <Label htmlFor={`${inputId}-scope-${choice.value}`} className="font-normal">
                  {choice.label}
                </Label>
              </div>
            ))}
          </RadioGroup>
        </fieldset>
      ) : (
        <p className="text-sm text-muted-foreground">Used for this job only.</p>
      )}
      {shape.kind === "text" && shape.max_length && (
        <p className="text-sm text-muted-foreground">Up to {shape.max_length} characters.</p>
      )}
      {shape.kind === "file" && (
        <p className="text-sm text-muted-foreground">PDF, DOC or DOCX, up to 1 MB.</p>
      )}
      {problem && (
        <p className="text-sm text-destructive" role="alert" data-testid="answer-problem">
          {problem}
        </p>
      )}
      <Button disabled={busy} onClick={submit}>
        Save answer
      </Button>
    </div>
  );
}

function LastPage({ card, onClose }: { card: Blocked; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    let created: string | null = null;
    apiBlobUrl(`/api/screenshot/${card.id}`)
      .then((blobUrl) => {
        created = blobUrl;
        setUrl(blobUrl);
      })
      .catch((caught: Error) => setProblem(caught.message));
    return () => {
      if (created) URL.revokeObjectURL(created);
    };
  }, [card.id]);

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-auto">
        <DialogHeader>
          <DialogTitle>Last page seen</DialogTitle>
          <DialogDescription>
            {card.title} at {card.company}
          </DialogDescription>
        </DialogHeader>
        {url && (
          <img
            src={url}
            alt="The last page the app saw for this application"
            data-testid="last-page"
          />
        )}
        {problem && <p className="text-destructive">{problem}</p>}
      </DialogContent>
    </Dialog>
  );
}

function OtpForm({ card, onDone }: { card: Blocked; onDone: () => void }) {
  const [code, setCode] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const waitMs = (card.code_wait_s ?? 300) * 1000;
  const left = Math.max(0, Math.ceil((Date.parse(card.updated_at) + waitMs - now) / 1000));
  const inputId = `otp-${card.id}`;

  async function send() {
    setBusy(true);
    setProblem(null);
    try {
      await api(`/api/otp/${card.id}`, { code });
      onDone();
    } catch (caught) {
      setProblem((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <Label htmlFor={inputId} className="block text-sm font-medium">
        Enter the code {card.company ?? "the site"} sent to {card.email ?? "your email"}
      </Label>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={inputId}
          value={code}
          autoComplete="one-time-code"
          inputMode="text"
          maxLength={64}
          className="w-40"
          onChange={(e) => setCode(e.target.value)}
        />
        <Button disabled={busy || !code.trim()} onClick={send}>
          Send code
        </Button>
      </div>
      <p className="text-sm text-muted-foreground" data-testid="otp-countdown">
        {left > 0
          ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left. The form stays open until then.`
          : "Time is up. The app will try this one again and the site will email a new code."}
      </p>
      {problem && (
        <p className="text-sm text-destructive" role="alert">
          {problem}
        </p>
      )}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [result, setResult] = useState<"copied" | "failed" | null>(null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setResult("copied");
    } catch {
      setResult("failed");
    }
    setTimeout(() => setResult(null), 1500);
  }

  return (
    <Button variant="outline" onClick={copy}>
      {result === "copied" ? "Copied" : result === "failed" ? "Copy failed" : "Copy"}
    </Button>
  );
}

// What the app would have sent, so the user can paste it into the form in their own browser.
function AnswerList({ answers }: { answers: Blocked["answers"] }) {
  return (
    <div className="divide-y" data-testid="manual-answers">
      {answers.map((answer) => (
        <div
          key={answer.label}
          className="flex items-center justify-between gap-3 py-3 first:pt-0 last:pb-0"
        >
          <div className="min-w-0 space-y-1">
            <p className="text-sm text-muted-foreground">{answer.label}</p>
            <p className="break-words text-sm">
              {answer.is_file ? `Upload ${answer.value}` : answer.value}
            </p>
          </div>
          {!answer.is_file && <CopyButton text={answer.value} />}
        </div>
      ))}
    </div>
  );
}

function NeedsCard({ card, onDone }: { card: Blocked; onDone: () => void }) {
  const [problem, setProblem] = useState<string | null>(null);
  const [viewing, setViewing] = useState(false);
  // Set when the machine had no browser to open, so the user can paste the address into one.
  const [unopenedUrl, setUnopenedUrl] = useState<string | null>(null);

  async function openPosting(path: string) {
    setProblem(null);
    try {
      const opened = await api<OpenedPosting>(path, {});
      setUnopenedUrl(opened.opened ? null : opened.url);
      onDone();
    } catch (caught) {
      setProblem((caught as Error).message);
    }
  }

  async function act(path: string, body?: unknown) {
    setProblem(null);
    try {
      await api(path, body ?? {});
      onDone();
    } catch (caught) {
      setProblem((caught as Error).message);
    }
  }

  const who = `${card.title ?? "A job"} at ${card.company ?? "an unknown company"}`;
  return (
    <Card data-testid={`card-${card.blocked_reason}`}>
      <CardHeader>
        <CardTitle className="text-base">{who}</CardTitle>
        <CardDescription data-testid="job-meta">
          {[card.location, ATS_LABEL[card.ats] ?? card.ats].filter(Boolean).join(" · ")}
          {card.url && /^https?:\/\//.test(card.url) && (
            <>
              {" · "}
              <button
                type="button"
                className="underline"
                onClick={() => void openPosting(`/api/posting/${card.id}/open`)}
              >
                View posting
              </button>
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {card.blocked_reason === "question" && (
          <>
            <p className="text-sm text-muted-foreground">
              The form asks something your profile and saved answers do not cover.
            </p>
            {(card.responsibilities || card.requirements || card.key_skills.length > 0) && (
              <details data-testid="about-job">
                <summary className="cursor-pointer text-sm text-muted-foreground">
                  About this job
                </summary>
                <div className="mt-3 space-y-3 text-sm">
                  {card.responsibilities && (
                    <div className="space-y-1">
                      <h3 className="font-medium">Responsibilities</h3>
                      <p>{card.responsibilities}</p>
                    </div>
                  )}
                  {card.requirements && (
                    <div className="space-y-1">
                      <h3 className="font-medium">Requirements</h3>
                      <p>{card.requirements}</p>
                    </div>
                  )}
                  {card.key_skills.length > 0 && (
                    <div className="space-y-1">
                      <h3 className="font-medium">Key skills</h3>
                      <div className="flex flex-wrap gap-2">
                        {card.key_skills.map((skill) => (
                          <span key={skill} className="rounded-md border px-2 py-0.5 text-sm">
                            {skill}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </details>
            )}
            <div className="divide-y">
              {card.questions.map((question) => (
                <QuestionForm
                  key={question.qna_id}
                  card={card}
                  question={question}
                  onDone={onDone}
                />
              ))}
            </div>
          </>
        )}
        {card.blocked_reason === "verify_submit" && card.error === "manual_apply" && (
          <>
            <p>Apply in the browser that opened, then come back here.</p>
            {unopenedUrl && (
              <div className="flex flex-wrap items-center gap-3" data-testid="unopened-url">
                <p className="min-w-0 break-all text-sm text-muted-foreground">
                  No browser opened. Paste this address into one: {unopenedUrl}
                </p>
                <CopyButton text={unopenedUrl} />
              </div>
            )}
            {card.answers.length > 0 && <AnswerList answers={card.answers} />}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => act(`/api/verify-submit/${card.id}`, { action: "confirmed" })}>
                I applied
              </Button>
              <Button
                variant="outline"
                onClick={() => void openPosting(`/api/posting/${card.id}/open`)}
              >
                Open the posting again
              </Button>
              <Button
                variant="outline"
                onClick={() => act(`/api/verify-submit/${card.id}`, { action: "not_applied" })}
              >
                I didn&apos;t apply
              </Button>
              <Button
                variant="ghost"
                onClick={() => act(`/api/verify-submit/${card.id}`, { action: "skip" })}
              >
                Skip
              </Button>
              {card.has_screenshot && (
                <Button variant="link" onClick={() => setViewing(true)}>
                  View last page
                </Button>
              )}
            </div>
            {viewing && <LastPage card={card} onClose={() => setViewing(false)} />}
          </>
        )}
        {card.blocked_reason === "verify_submit" && card.error !== "manual_apply" && (
          <>
            {card.error === "email_link" ? (
              <p>
                {card.company ?? "The site"} emailed a link to {card.email ?? "your email"}. Click
                it, then press Done.
              </p>
            ) : (
              <p>Not sure {who} was sent. The app clicked Submit but could not confirm it.</p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => act(`/api/verify-submit/${card.id}`, { action: "confirmed" })}>
                {card.error === "email_link" ? "Done" : "I got a confirmation"}
              </Button>
              <Button
                variant="outline"
                onClick={() => act(`/api/verify-submit/${card.id}`, { action: "retry" })}
              >
                No confirmation, try again
              </Button>
              <Button
                variant="ghost"
                onClick={() => act(`/api/verify-submit/${card.id}`, { action: "skip" })}
              >
                Skip
              </Button>
              {card.has_screenshot && (
                <Button variant="link" onClick={() => setViewing(true)}>
                  View last page
                </Button>
              )}
            </div>
            {viewing && <LastPage card={card} onClose={() => setViewing(false)} />}
          </>
        )}
        {card.blocked_reason === "attempts_exhausted" && (
          <>
            <p>The app tried three times and could not finish this application.</p>
            {card.error && (
              <p className="break-words text-sm text-muted-foreground">Last error: {card.error}</p>
            )}
            <Button onClick={() => act(`/api/retry/${card.id}`)}>Retry</Button>
          </>
        )}
        {card.blocked_reason === "otp" && <OtpForm card={card} onDone={onDone} />}
        {card.blocked_reason === "captcha" && (
          <>
            <p>
              {card.company ?? "A site"} needs you to solve a captcha. A browser window opens on the
              form; solve it there and the app finishes the application. Closing the window puts
              this back here.
            </p>
            <p className="text-sm text-muted-foreground">
              &quot;Open in browser&quot; fills the form in a window the app controls, and some
              sites, Ashby among them, reject that as automated. &quot;Apply in my browser&quot;
              opens the posting in your usual browser, and you apply yourself.
            </p>
            {card.error === "no_free_slot" && (
              <p className="text-sm text-muted-foreground">
                Every browser slot was busy, so the window did not open. Try again in a minute.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => act(`/api/captcha/${card.id}/open`)}>Open in browser</Button>
              <Button
                variant="outline"
                onClick={() => void openPosting(`/api/captcha/${card.id}/manual`)}
              >
                Apply in my browser
              </Button>
              {card.has_screenshot && (
                <Button variant="link" onClick={() => setViewing(true)}>
                  View last page
                </Button>
              )}
            </div>
            {viewing && <LastPage card={card} onClose={() => setViewing(false)} />}
          </>
        )}
        {problem && (
          <p className="text-destructive" role="alert">
            {problem}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function NeedsYouScreen() {
  const { blocked, refresh } = useBlocked();
  if (blocked.length === 0) {
    return (
      <Alert>
        <AlertDescription>Nothing needs you right now.</AlertDescription>
      </Alert>
    );
  }
  return (
    <div className="space-y-6">
      {blocked.map((card) => (
        <NeedsCard key={card.id} card={card} onDone={() => void refresh()} />
      ))}
    </div>
  );
}
