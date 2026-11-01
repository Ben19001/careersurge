import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { api, apiUpload } from "@/lib/api";
import {
  draftToForm,
  formToDraft,
  formToFields,
  type Draft,
  type DraftScalar,
  type ProfileForm,
} from "@/lib/profile-form";
import { RESUME_RULE, resumeProblem } from "@/lib/resume";
import { useProfileGate } from "@/lib/profile-gate";
import { RefusalNotice } from "@/components/refusal-notice";
import { useSourcing } from "@/lib/use-sourcing";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { FiltersForm, type Filters } from "@/components/filters-form";
import { ConsentBlock } from "@/components/consent-block";
import { KeyField } from "@/components/key-field";
import { Field, ProfileFields, Select } from "@/components/profile-editors";

type Step = "keys" | "resume" | "review" | "questions" | "permissions" | "filters" | "run";
const STEPS: [Step, string][] = [
  ["keys", "Keys"],
  ["resume", "Resume"],
  ["review", "Review"],
  ["questions", "Questions"],
  ["permissions", "Permissions"],
  ["filters", "Job search"],
  ["run", "Run"],
];
const STEP_TITLES: Record<Step, string> = {
  keys: "Connect your accounts",
  resume: "Upload your resume",
  review: "Review what we found",
  questions: "Common application questions",
  permissions: "Let CareerSurge confirm for you",
  filters: "What jobs are you looking for?",
  run: "Find your first jobs",
};
const AUTOSAVE_MS = 300;
// What `PUT /api/profile` answers when the draft's stored PDF is gone and no file was sent.
const RESUME_REQUIRED = "a resume is required";

type FieldShape = {
  kind: "text" | "single" | "number";
  options: string[];
  max_length: number | null;
};
type Question = {
  normalized_question: string;
  question: string;
  field_shape: FieldShape;
  answer: string | null;
};

function Problem({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <Alert variant="destructive" data-testid="step-problem">
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  );
}

function KeysStep({ onDone }: { onDone: () => void }) {
  const [apify, setApify] = useState(false);
  const [jev, setJev] = useState(false);
  return (
    <div className="space-y-5">
      <p className="text-sm text-muted-foreground">
        CareerSurge runs on this computer with your own keys. This is what leaves it: Apify gets
        your search filters (job titles, locations); Jev gets lines from your resume and the labels
        of application form questions. Nothing else is sent. Read{" "}
        <a className="underline" href="https://docs.typesafe.ai" target="_blank" rel="noreferrer">
          TypeSafe&apos;s documentation and terms
        </a>{" "}
        for how Jev handles it.
      </p>
      <KeyField
        name="APIFY_TOKEN"
        label="Apify key"
        help="Finds the job listings. Apify bills your own account per job."
        onStatus={setApify}
      />
      <KeyField
        name="JEV_API_KEY"
        label="Jev key"
        help="Reads your resume and matches form questions. If it is rejected, the app falls back to simpler matching."
        onStatus={setJev}
      />
      <Button disabled={!apify || !jev} onClick={onDone}>
        Continue
      </Button>
    </div>
  );
}

function ResumeStep({
  onParsed,
  onBack,
}: {
  onParsed: (file: File, draft: Draft) => void;
  onBack: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function choose(file: File | undefined) {
    if (!file) return;
    const refused = resumeProblem(file);
    if (refused) return setProblem(refused);
    setProblem(null);
    setBusy(true);
    try {
      const body = new FormData();
      body.append("file", file, file.name);
      const { draft } = await apiUpload<{ draft: Draft }>("/api/resume/parse", body);
      onParsed(file, draft);
    } catch (caught) {
      setProblem((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Field
        label="Your resume"
        htmlFor="resume"
        hint={`${RESUME_RULE} You review everything it finds next.`}
      >
        <Input
          id="resume"
          type="file"
          accept="application/pdf,.pdf"
          disabled={busy}
          onChange={(event) => void choose(event.target.files?.[0])}
        />
      </Field>
      {busy && <p className="text-sm text-muted-foreground">Reading your resume…</p>}
      <Problem message={problem} />
      <Button variant="outline" onClick={onBack} disabled={busy}>
        Back
      </Button>
    </div>
  );
}

function ReviewStep({
  initialDraft,
  initialFile,
  onConfirmed,
}: {
  initialDraft: Draft;
  initialFile: File | null;
  onConfirmed: () => void;
}) {
  const [form, setForm] = useState<ProfileForm>(() => draftToForm(initialDraft));
  const [file, setFile] = useState<File | null>(initialFile);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [autosaved, setAutosaved] = useState(false);
  const [busy, setBusy] = useState(false);
  // Set when Confirm finds the draft's stored PDF gone, so the attach input is offered.
  const [resumeGone, setResumeGone] = useState(false);
  const first = useRef(true);
  // The parse stored the PDF and the draft names it, so a reopened review confirms without the file.
  const draftHasResume = typeof initialDraft.resume_path === "string";

  // Every change is saved as the draft (after a short pause), so closing the app loses nothing.
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setAutosaved(false);
    const timer = window.setTimeout(() => {
      api("/api/profile/draft", { draft: formToDraft(form, initialDraft) }, "PUT")
        .then(() => setAutosaved(true))
        .catch((caught: Error) => setProblem(caught.message));
    }, AUTOSAVE_MS);
    return () => window.clearTimeout(timer);
  }, [form, initialDraft]);

  async function confirm() {
    setProblem(null);
    const missing: Record<string, string> = {};
    if (!form.full_name.trim()) missing.full_name = "Your full name is required.";
    if (!form.email.trim()) missing.email = "Your email is required.";
    if (!file && !draftHasResume) missing.resume = "Attach your resume to confirm.";
    setErrors(missing);
    if (Object.keys(missing).length > 0) return;

    const body = new FormData();
    body.append("fields", JSON.stringify(formToFields(form)));
    if (file) body.append("file", file, file.name);
    setBusy(true);
    try {
      await apiUpload("/api/profile", body, "PUT");
      onConfirmed();
    } catch (caught) {
      const message = (caught as Error).message;
      if (message === RESUME_REQUIRED) {
        setResumeGone(true);
        setErrors({
          resume: "The resume saved with this draft is gone. Attach it again to confirm.",
        });
      } else {
        setProblem(message);
      }
    } finally {
      setBusy(false);
    }
  }

  const scalars = initialDraft as Record<string, DraftScalar | undefined>;
  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        Check everything we found. Whatever you submit to employers comes from here, so fix anything
        that is wrong or missing. Changes are saved as you type.
      </p>
      <ProfileFields form={form} onChange={setForm} scalars={scalars} errors={errors} />
      {(resumeGone || (!initialFile && !draftHasResume)) && (
        <Field
          label="Attach your resume again to confirm"
          htmlFor="resume-again"
          hint={RESUME_RULE}
          error={errors.resume}
        >
          <Input
            id="resume-again"
            type="file"
            accept="application/pdf,.pdf"
            onChange={(event) => {
              const chosen = event.target.files?.[0] ?? null;
              const refused = chosen ? resumeProblem(chosen) : null;
              setProblem(refused);
              setFile(refused ? null : chosen);
            }}
          />
        </Field>
      )}
      <Problem message={problem} />
      <div className="flex items-center gap-3">
        <Button disabled={busy} onClick={confirm}>
          Confirm profile
        </Button>
        {autosaved && (
          <span className="text-sm text-muted-foreground" data-testid="draft-saved">
            Draft saved
          </span>
        )}
      </div>
    </div>
  );
}

function QuestionsStep({ onDone }: { onDone: () => void }) {
  const [questions, setQuestions] = useState<Question[] | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    api<{ questions: Question[] }>("/api/global-questions")
      .then(({ questions: loaded }) => {
        setQuestions(loaded);
        const known: Record<string, string> = {};
        for (const question of loaded) known[question.normalized_question] = question.answer ?? "";
        setAnswers(known);
      })
      .catch((caught: Error) => setProblem(caught.message));
  }, []);

  async function save() {
    setProblem(null);
    try {
      for (const question of questions ?? []) {
        const value = (answers[question.normalized_question] ?? "").trim();
        if (value && value !== (question.answer ?? "")) {
          await api("/api/global-questions/answer", {
            normalized_question: question.normalized_question,
            value,
          });
        }
      }
      onDone();
    } catch (caught) {
      setProblem((caught as Error).message);
    }
  }

  if (!questions) return <p className="text-muted-foreground">Loading…</p>;
  return (
    <div className="space-y-5">
      <p className="text-sm text-muted-foreground">
        These come up on most applications. Answer once and they are reused. You can skip any of
        them: a question you skipped waits for you in Needs you when a form asks it.
      </p>
      {questions.map((question) => {
        const { kind, options } = question.field_shape;
        const key = question.normalized_question;
        const value = answers[key] ?? "";
        const change = (next: string) => setAnswers({ ...answers, [key]: next });
        return (
          <Field key={key} label={question.question} htmlFor={key}>
            {kind === "single" ? (
              // An answer saved before the list existed that is not in it shows "Pick one" and stays
              // in use until the user picks.
              <Select
                value={options.includes(value) ? value : ""}
                onChange={change}
                options={options.map((option): [string, string] => [option, option])}
                placeholder="Pick one"
                label={question.question}
              />
            ) : kind === "number" ? (
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">$</span>
                <Input
                  id={key}
                  type="number"
                  inputMode="numeric"
                  min={0}
                  step={1}
                  value={value}
                  onChange={(event) => change(event.target.value)}
                />
                <span className="whitespace-nowrap text-sm text-muted-foreground">per year</span>
              </div>
            ) : (
              <Input id={key} value={value} onChange={(event) => change(event.target.value)} />
            )}
          </Field>
        );
      })}
      <Problem message={problem} />
      <div className="flex gap-3">
        <Button onClick={save}>Save answers and continue</Button>
        <Button variant="outline" onClick={onDone}>
          Skip
        </Button>
      </div>
    </div>
  );
}

function FiltersStep({ onDone }: { onDone: () => void }) {
  const [filters, setFilters] = useState<Filters | null>(null);
  const [titlesError, setTitlesError] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    api<{ settings: { sourcing_filters: Filters } }>("/api/settings")
      .then(({ settings }) => setFilters(settings.sourcing_filters))
      .catch((caught: Error) => setProblem(caught.message));
  }, []);

  async function save(current: Filters) {
    setProblem(null);
    if (!(current.titles ?? []).length) {
      return setTitlesError("Add at least one job title.");
    }
    setTitlesError(null);
    try {
      await api("/api/settings", { settings: { sourcing_filters: current } }, "PUT");
      onDone();
    } catch (caught) {
      setProblem((caught as Error).message);
    }
  }

  if (!filters) return <p className="text-muted-foreground">Loading…</p>;
  return (
    <div className="space-y-5">
      <FiltersForm filters={filters} onChange={setFilters} titlesError={titlesError} />
      <Problem message={problem} />
      <Button onClick={() => save(filters)}>Save and continue</Button>
    </div>
  );
}

function RunStep() {
  const navigate = useNavigate();
  const sourcing = useSourcing();
  // Run now, and accepting the price, both land on Progress once the run has started.
  async function runAndWatch(confirmBacklog = false) {
    const started = await sourcing.run(confirmBacklog);
    if (started) navigate({ to: "/progress" });
    return started;
  }
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Check for jobs now. You see Apify&apos;s price before anything is charged, and nothing is
        submitted to an employer until you approve a batch.
      </p>
      <RefusalNotice refusal={sourcing.refusal} busy={sourcing.busy} onRun={runAndWatch} />
      <Problem message={sourcing.problem} />
      <div className="flex flex-wrap items-center gap-3">
        <Button disabled={sourcing.busy || sourcing.fetching} onClick={() => runAndWatch()}>
          Run now
        </Button>
        {sourcing.fetching && (
          <span className="text-sm text-muted-foreground" data-testid="fetching-jobs">
            Fetching jobs… they appear on the Approve screen when ready.
          </span>
        )}
        <Button variant="outline" onClick={() => navigate({ to: "/" })}>
          Skip, I&apos;ll run it later
        </Button>
      </div>
    </div>
  );
}

// First-run setup, one screen per step. Where it starts depends on what is already saved, so a closed
// app resumes at the first missing step: a saved profile without a job title goes to the filters (the
// questions are skippable, so they are not waited for), a saved draft to the review.
export function OnboardingScreen() {
  const [step, setStep] = useState<Step | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const gate = useProfileGate();

  useEffect(() => {
    Promise.all([
      api<{ set: boolean }>("/api/keys/APIFY_TOKEN"),
      api<{ set: boolean }>("/api/keys/JEV_API_KEY"),
      api<{ profile: unknown; draft: Draft | null }>("/api/profile"),
      api<{ settings: { sourcing_filters: Filters } }>("/api/settings"),
    ])
      .then(([apify, jev, saved, { settings }]) => {
        setDraft(saved.draft);
        const hasTitles = (settings.sourcing_filters.titles ?? []).length > 0;
        if (saved.profile) setStep(hasTitles ? "questions" : "filters");
        else if (!apify.set || !jev.set) setStep("keys");
        else setStep(saved.draft ? "review" : "resume");
      })
      .catch(() => setStep("keys"));
  }, []);

  if (step === null) return <p className="text-muted-foreground">Loading…</p>;
  const number = STEPS.findIndex(([name]) => name === step) + 1;

  return (
    <Card>
      <CardHeader>
        <p
          className="text-xs uppercase tracking-wide text-muted-foreground"
          data-testid="step-count"
        >
          Step {number} of {STEPS.length}
        </p>
        <CardTitle className="text-lg" data-testid="step-title">
          {STEP_TITLES[step]}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {step === "keys" && <KeysStep onDone={() => setStep(draft ? "review" : "resume")} />}
        {step === "resume" && (
          <ResumeStep
            onBack={() => setStep("keys")}
            onParsed={(chosen, parsed) => {
              setFile(chosen);
              setDraft(parsed);
              setStep("review");
            }}
          />
        )}
        {step === "review" && draft && (
          <ReviewStep
            initialDraft={draft}
            initialFile={file}
            onConfirmed={() => setStep("questions")}
          />
        )}
        {step === "questions" && <QuestionsStep onDone={() => setStep("permissions")} />}
        {step === "permissions" && (
          <div className="space-y-5">
            <p className="text-sm text-muted-foreground">
              Application forms ask you to tick boxes. Say which ones CareerSurge may tick for you;
              you can change this any time in Profile, and it applies from the next batch you
              approve. Every box is optional.
            </p>
            <ConsentBlock
              submitLabel="Save and continue"
              onSaved={() => setStep("filters")}
              extraActions={
                <Button variant="outline" onClick={() => setStep("filters")}>
                  Skip
                </Button>
              }
            />
          </div>
        )}
        {step === "filters" && (
          <FiltersStep
            onDone={async () => {
              await gate.refresh();
              setStep("run");
            }}
          />
        )}
        {step === "run" && <RunStep />}
      </CardContent>
    </Card>
  );
}
