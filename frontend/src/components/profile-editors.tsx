import { useState, type ReactNode } from "react";
import { Plus, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  DEGREE_TYPES,
  EMPLOYMENT_TYPES,
  MONTHS,
  emptyEducation,
  emptyProject,
  emptyWork,
  type DraftScalar,
  type EducationEntry,
  type ProfileForm,
  type ProjectEntry,
  type ScalarKey,
  type WorkEntry,
} from "@/lib/profile-form";

// The review editors, ported from the retired app (9ed4663: app.profile.tsx) onto the local profile's
// snake_case shape, with native selects in place of the old Radix ones.
const ENTRY_TEXT_MAX = 200;
const ENTRY_DESC_MAX = 2000;

export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string | null;
  error?: string | null;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {hint && !error && <p className="text-xs text-muted-foreground">{hint}</p>}
      {error && (
        <p className="text-xs text-destructive" data-testid={`error-${htmlFor ?? label}`}>
          {error}
        </p>
      )}
    </div>
  );
}

export function Select({
  value,
  onChange,
  options,
  placeholder,
  disabled,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  options: [string, string][];
  placeholder: string;
  disabled?: boolean;
  label: string;
}) {
  // A value the resume gave that is not in the standard list stays selectable, so it is not shown blank.
  const known = options.some(([optionValue]) => optionValue === value);
  return (
    <select
      value={value}
      disabled={disabled}
      aria-label={label}
      onChange={(event) => onChange(event.target.value)}
      className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
    >
      <option value="">{placeholder}</option>
      {value && !known && <option value={value}>{value}</option>}
      {options.map(([optionValue, optionLabel]) => (
        <option key={optionValue} value={optionValue}>
          {optionLabel}
        </option>
      ))}
    </select>
  );
}

export function TagListEditor({
  values,
  onChange,
  placeholder,
  label,
  maxItems = 50,
  maxLength = 80,
  splitOnComma = true,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
  label: string;
  maxItems?: number;
  maxLength?: number;
  // False for an entry that can hold a comma itself, such as the location "Austin, Texas".
  splitOnComma?: boolean;
}) {
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);

  function addItem() {
    const trimmed = input.trim();
    if (!trimmed) return;
    if (trimmed.length > maxLength) {
      setError(`Maximum ${maxLength} characters allowed.`);
      return;
    }
    if (values.length >= maxItems) {
      setError(`Maximum ${maxItems} items allowed.`);
      return;
    }
    if (!values.includes(trimmed)) onChange([...values, trimmed]);
    setInput("");
    setError(null);
  }

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-input bg-background p-2">
      {values.map((value, index) => (
        <Badge key={`${value}-${index}`} variant="secondary" className="gap-1">
          {value}
          <button
            type="button"
            onClick={() => onChange(values.filter((_, other) => other !== index))}
            className="ml-1 rounded-full hover:bg-muted"
            aria-label={`Remove ${value}`}
          >
            <X className="h-3 w-3" />
          </button>
        </Badge>
      ))}
      {values.length < maxItems && (
        <input
          value={input}
          aria-label={label}
          onChange={(event) => {
            setInput(event.target.value);
            setError(null);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" || (splitOnComma && event.key === ",")) {
              event.preventDefault();
              addItem();
            }
          }}
          onBlur={addItem}
          placeholder={placeholder}
          className="min-w-[180px] flex-1 bg-transparent px-2 py-1 text-sm outline-none placeholder:text-muted-foreground"
        />
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="gap-1"
        onClick={addItem}
        disabled={!input.trim()}
      >
        <Plus className="h-4 w-4" />
        Add
      </Button>
      {error && <p className="basis-full text-xs text-destructive">{error}</p>}
    </div>
  );
}

function EntryCard({
  title,
  onRemove,
  children,
}: {
  title: string;
  onRemove: () => void;
  children: ReactNode;
}) {
  return (
    <div className="rounded-md border border-input bg-background p-4" data-testid="entry-card">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {title}
        </span>
        <button
          type="button"
          onClick={onRemove}
          className="rounded-full p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label={`Remove ${title}`}
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      {children}
    </div>
  );
}

type DateRangePatch = {
  start_month?: string;
  start_year?: string;
  end_month?: string;
  end_year?: string;
  current?: boolean;
};

// Start and end month and year with a "currently here" box that clears and disables the end.
function MonthYearRange({
  entry,
  currentLabel,
  onChange,
}: {
  entry: WorkEntry | EducationEntry;
  currentLabel: string;
  onChange: (patch: DateRangePatch) => void;
}) {
  const year = (value: string, label: string, key: "start_year" | "end_year", disabled = false) => (
    <Input
      type="number"
      inputMode="numeric"
      min={1950}
      max={2100}
      placeholder="Year"
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange({ [key]: event.target.value })}
    />
  );
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <Label className="text-xs text-muted-foreground">Start</Label>
          <div className="mt-1 grid grid-cols-2 gap-2">
            <Select
              label="Start month"
              placeholder="Month"
              options={MONTHS}
              value={entry.start_month}
              onChange={(value) => onChange({ start_month: value })}
            />
            {year(entry.start_year, "Start year", "start_year")}
          </div>
        </div>
        <div>
          <Label className="text-xs text-muted-foreground">End</Label>
          <div className="mt-1 grid grid-cols-2 gap-2">
            <Select
              label="End month"
              placeholder="Month"
              options={MONTHS}
              value={entry.end_month}
              disabled={entry.current}
              onChange={(value) => onChange({ end_month: value })}
            />
            {year(entry.end_year, "End year", "end_year", entry.current)}
          </div>
        </div>
      </div>
      <label className="flex w-fit cursor-pointer items-center gap-2 text-sm">
        <Checkbox
          checked={entry.current}
          onCheckedChange={(checked) =>
            onChange(checked ? { current: true, end_month: "", end_year: "" } : { current: false })
          }
        />
        {currentLabel}
      </label>
    </div>
  );
}

function AddEntryButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Button type="button" variant="outline" size="sm" className="gap-2" onClick={onClick}>
      <Plus className="h-4 w-4" />
      {label}
    </Button>
  );
}

function replaceAt<T>(list: T[], index: number, changes: Partial<T>): T[] {
  return list.map((entry, other) => (other === index ? { ...entry, ...changes } : entry));
}

export function EducationEditor({
  entries,
  onChange,
}: {
  entries: EducationEntry[];
  onChange: (next: EducationEntry[]) => void;
}) {
  return (
    <div className="space-y-3">
      {entries.map((entry, index) => {
        const patch = (changes: Partial<EducationEntry>) =>
          onChange(replaceAt(entries, index, changes));
        return (
          <EntryCard
            key={index}
            title={`Education ${index + 1}`}
            onRemove={() => onChange(entries.filter((_, other) => other !== index))}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="School / institution">
                <Input
                  value={entry.institution}
                  maxLength={ENTRY_TEXT_MAX}
                  onChange={(event) => patch({ institution: event.target.value })}
                />
              </Field>
              <Field label="Major / field of study">
                <Input
                  value={entry.field_of_study}
                  maxLength={ENTRY_TEXT_MAX}
                  onChange={(event) => patch({ field_of_study: event.target.value })}
                />
              </Field>
              <Field label="Degree type">
                <Select
                  label="Degree type"
                  placeholder="Select degree"
                  options={DEGREE_TYPES.map((degree) => [degree, degree])}
                  value={entry.degree}
                  onChange={(value) => patch({ degree: value })}
                />
              </Field>
              <Field label="GPA">
                <Input
                  value={entry.gpa}
                  maxLength={12}
                  placeholder="3.8 / 4.0"
                  onChange={(event) => patch({ gpa: event.target.value })}
                />
              </Field>
            </div>
            <div className="mt-3">
              <MonthYearRange
                entry={entry}
                currentLabel="I currently study here"
                onChange={patch}
              />
            </div>
          </EntryCard>
        );
      })}
      <AddEntryButton
        label="Add education"
        onClick={() => onChange([...entries, emptyEducation()])}
      />
    </div>
  );
}

export function WorkExperienceEditor({
  entries,
  onChange,
}: {
  entries: WorkEntry[];
  onChange: (next: WorkEntry[]) => void;
}) {
  return (
    <div className="space-y-3">
      {entries.map((entry, index) => {
        const patch = (changes: Partial<WorkEntry>) => onChange(replaceAt(entries, index, changes));
        return (
          <EntryCard
            key={index}
            title={`Experience ${index + 1}`}
            onRemove={() => onChange(entries.filter((_, other) => other !== index))}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Title">
                <Input
                  value={entry.title}
                  maxLength={ENTRY_TEXT_MAX}
                  onChange={(event) => patch({ title: event.target.value })}
                />
              </Field>
              <Field label="Company">
                <Input
                  value={entry.company}
                  maxLength={ENTRY_TEXT_MAX}
                  onChange={(event) => patch({ company: event.target.value })}
                />
              </Field>
              <Field label="Location">
                <Input
                  value={entry.location}
                  maxLength={ENTRY_TEXT_MAX}
                  onChange={(event) => patch({ location: event.target.value })}
                />
              </Field>
              <Field label="Experience type">
                <Select
                  label="Experience type"
                  placeholder="Select type"
                  options={EMPLOYMENT_TYPES.map((type) => [type, type])}
                  value={entry.employment_type}
                  onChange={(value) => patch({ employment_type: value })}
                />
              </Field>
            </div>
            <div className="mt-3">
              <MonthYearRange entry={entry} currentLabel="I currently work here" onChange={patch} />
            </div>
            <div className="mt-3">
              <Field label="Description">
                <Textarea
                  value={entry.description}
                  rows={3}
                  maxLength={ENTRY_DESC_MAX}
                  placeholder="What you did, impact, technologies…"
                  onChange={(event) => patch({ description: event.target.value })}
                />
              </Field>
            </div>
          </EntryCard>
        );
      })}
      <AddEntryButton label="Add experience" onClick={() => onChange([...entries, emptyWork()])} />
    </div>
  );
}

export function ProjectsEditor({
  entries,
  onChange,
}: {
  entries: ProjectEntry[];
  onChange: (next: ProjectEntry[]) => void;
}) {
  return (
    <div className="space-y-3">
      {entries.map((entry, index) => {
        const patch = (changes: Partial<ProjectEntry>) =>
          onChange(replaceAt(entries, index, changes));
        return (
          <EntryCard
            key={index}
            title={`Project ${index + 1}`}
            onRemove={() => onChange(entries.filter((_, other) => other !== index))}
          >
            <Field label="Project name">
              <Input
                value={entry.name}
                maxLength={ENTRY_TEXT_MAX}
                onChange={(event) => patch({ name: event.target.value })}
              />
            </Field>
            <div className="mt-3">
              <Field label="Description">
                <Textarea
                  value={entry.description}
                  rows={3}
                  maxLength={ENTRY_DESC_MAX}
                  placeholder="What the project does and your role…"
                  onChange={(event) => patch({ description: event.target.value })}
                />
              </Field>
            </div>
          </EntryCard>
        );
      })}
      <AddEntryButton label="Add project" onClick={() => onChange([...entries, emptyProject()])} />
    </div>
  );
}

const SCALAR_FIELDS: [ScalarKey, string][] = [
  ["full_name", "Full name"],
  ["email", "Email"],
  ["phone", "Phone"],
  ["city", "City"],
  ["state", "State"],
  ["linkedin_url", "LinkedIn"],
  ["github_url", "GitHub"],
];

// What the resume parse was unsure about, shown under a field so the user checks it.
function sourceHint(scalar: DraftScalar | undefined): string | null {
  if (!scalar) return null;
  if (scalar.source === "none") return "Not found in your resume - type it.";
  if (scalar.source === "regex_fallback")
    return "Taken from your resume without a second check - please verify.";
  if (scalar.source === "jev" && scalar.confidence != null && scalar.confidence < 0.9) {
    return "Low confidence - please verify.";
  }
  return null;
}

// Every profile field the review and the Profile screen edit. `scalars` carries the draft's per-field
// candidates and source (review only); `errors` marks fields the server or the form refused.
export function ProfileFields({
  form,
  onChange,
  scalars,
  errors = {},
}: {
  form: ProfileForm;
  onChange: (next: ProfileForm) => void;
  scalars?: Record<string, DraftScalar | undefined>;
  errors?: Record<string, string>;
}) {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        {SCALAR_FIELDS.map(([key, label]) => {
          const candidates = (scalars?.[key]?.candidates ?? []).filter(
            (item) => item !== form[key],
          );
          return (
            <Field
              key={key}
              label={label}
              htmlFor={key}
              hint={sourceHint(scalars?.[key])}
              error={errors[key]}
            >
              <Input
                id={key}
                value={form[key]}
                list={candidates.length > 0 ? `candidates-${key}` : undefined}
                aria-invalid={errors[key] ? true : undefined}
                onChange={(event) => onChange({ ...form, [key]: event.target.value })}
              />
              {candidates.length > 0 && (
                <datalist id={`candidates-${key}`}>
                  {candidates.map((candidate) => (
                    <option key={candidate} value={candidate} />
                  ))}
                </datalist>
              )}
            </Field>
          );
        })}
      </div>
      <Field label="Portfolio links">
        <TagListEditor
          label="Add a portfolio link"
          values={form.portfolio_links}
          placeholder="https://…"
          onChange={(next) => onChange({ ...form, portfolio_links: next })}
        />
      </Field>
      <Field label="Skills">
        <TagListEditor
          label="Add a skill"
          values={form.skills}
          placeholder="Type a skill and press Enter"
          maxLength={40}
          onChange={(next) => onChange({ ...form, skills: next })}
        />
      </Field>
      <section className="space-y-2">
        <h3 className="text-sm font-medium">Work experience</h3>
        <WorkExperienceEditor
          entries={form.work_experience}
          onChange={(next) => onChange({ ...form, work_experience: next })}
        />
      </section>
      <section className="space-y-2">
        <h3 className="text-sm font-medium">Education</h3>
        <EducationEditor
          entries={form.education}
          onChange={(next) => onChange({ ...form, education: next })}
        />
      </section>
      <Field label="Certifications">
        <TagListEditor
          label="Add a certification"
          values={form.certifications}
          placeholder="Type a certification and press Enter"
          maxLength={120}
          onChange={(next) => onChange({ ...form, certifications: next })}
        />
      </Field>
      <section className="space-y-2">
        <h3 className="text-sm font-medium">Projects</h3>
        <ProjectsEditor
          entries={form.projects}
          onChange={(next) => onChange({ ...form, projects: next })}
        />
      </section>
    </div>
  );
}
