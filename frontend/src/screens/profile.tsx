import { useEffect, useState } from "react";
import { api, apiUpload, type ApiError } from "@/lib/api";
import { profileToForm, formToFields, type ProfileForm } from "@/lib/profile-form";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ConsentBlock } from "@/components/consent-block";
import { Field, ProfileFields } from "@/components/profile-editors";
import { RESUME_RULE, resumeProblem } from "@/lib/resume";

// The saved profile in the review editors. A new resume is optional: without one the stored PDF stays.
export function ProfileScreen() {
  const [form, setForm] = useState<ProfileForm | null>(null);
  const [hasResume, setHasResume] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ profile: Record<string, unknown> | null }>("/api/profile")
      .then(({ profile }) => {
        setForm(profileToForm(profile));
        setHasResume(Boolean(profile?.resume_path));
      })
      .catch((caught: Error) => setProblem(caught.message));
  }, []);

  if (!form) {
    return problem ? (
      <Alert variant="destructive">
        <AlertDescription>{problem}</AlertDescription>
      </Alert>
    ) : (
      <p className="text-muted-foreground">Loading…</p>
    );
  }

  async function save(current: ProfileForm) {
    setProblem(null);
    setSaved(false);
    const missing: Record<string, string> = {};
    if (!current.full_name.trim()) missing.full_name = "Your full name is required.";
    if (!current.email.trim()) missing.email = "Your email is required.";
    setErrors(missing);
    if (Object.keys(missing).length > 0) return;

    const body = new FormData();
    body.append("fields", JSON.stringify(formToFields(current)));
    if (file) body.append("file", file, file.name);
    setBusy(true);
    try {
      await apiUpload("/api/profile", body, "PUT");
      setFile(null);
      setHasResume(true);
      setSaved(true);
    } catch (caught) {
      setProblem((caught as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Profile</h1>
      <p className="text-sm text-muted-foreground">
        Changes apply to batches you approve from now on. A batch you already approved keeps the
        profile it was approved with.
      </p>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Resume</CardTitle>
        </CardHeader>
        <CardContent>
          <Field
            label={hasResume ? "Replace your resume (optional)" : "Resume"}
            htmlFor="resume"
            hint={hasResume ? `A resume is on file. ${RESUME_RULE}` : RESUME_RULE}
          >
            <Input
              id="resume"
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
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-6">
          <ProfileFields
            form={form}
            errors={errors}
            onChange={(next) => {
              setForm(next);
              setSaved(false);
            }}
          />
        </CardContent>
      </Card>
      {problem && (
        <Alert variant="destructive">
          <AlertDescription>{problem}</AlertDescription>
        </Alert>
      )}
      <div className="flex items-center gap-3">
        <Button disabled={busy} onClick={() => save(form)}>
          Save profile
        </Button>
        {saved && (
          <span className="text-sm text-muted-foreground" data-testid="profile-saved">
            Saved
          </span>
        )}
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Permissions</CardTitle>
          <p className="text-sm text-muted-foreground">
            Which consent boxes CareerSurge may tick for you. Saved on its own button; applies from
            the next batch you approve.
          </p>
        </CardHeader>
        <CardContent>
          <ConsentBlock submitLabel="Save permissions" />
        </CardContent>
      </Card>
    </div>
  );
}
