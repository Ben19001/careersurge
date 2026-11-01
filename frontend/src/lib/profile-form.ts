// The profile as the editors see it, and the two server shapes it converts to and from:
// the review draft (`parse_resume`: each scalar is {value, candidates, confidence, source}) and the
// saved profile (`get_profile`: plain columns). Entries keep their snake_case keys so they round-trip.

export type WorkEntry = {
  title: string;
  company: string;
  location: string;
  employment_type: string;
  start_month: string;
  start_year: string;
  end_month: string;
  end_year: string;
  current: boolean;
  description: string;
  picks?: unknown;
};

export type EducationEntry = {
  institution: string;
  degree: string;
  field_of_study: string;
  gpa: string;
  start_month: string;
  start_year: string;
  end_month: string;
  end_year: string;
  current: boolean;
  picks?: unknown;
};

export type ProjectEntry = { name: string; description: string };

export const SCALARS = [
  "full_name",
  "email",
  "phone",
  "city",
  "state",
  "linkedin_url",
  "github_url",
] as const;
export type ScalarKey = (typeof SCALARS)[number];

export type ProfileForm = Record<ScalarKey, string> & {
  portfolio_links: string[];
  skills: string[];
  certifications: string[];
  work_experience: WorkEntry[];
  education: EducationEntry[];
  projects: ProjectEntry[];
};

export type DraftScalar = {
  value: string | string[] | null;
  candidates: string[];
  confidence: number | null;
  source: "jev" | "regex_fallback" | "regex" | "none";
};
export type Draft = Record<string, unknown>;

export const MONTHS: [string, string][] = [
  ["01", "January"],
  ["02", "February"],
  ["03", "March"],
  ["04", "April"],
  ["05", "May"],
  ["06", "June"],
  ["07", "July"],
  ["08", "August"],
  ["09", "September"],
  ["10", "October"],
  ["11", "November"],
  ["12", "December"],
];
export const DEGREE_TYPES = [
  "High School",
  "Associate",
  "Bachelor",
  "Master",
  "Doctorate",
  "Certificate",
  "Other",
];
export const EMPLOYMENT_TYPES = [
  "Full-time",
  "Part-time",
  "Contract",
  "Internship",
  "Freelance",
  "Temporary",
];

export const emptyWork = (): WorkEntry => ({
  title: "",
  company: "",
  location: "",
  employment_type: "",
  start_month: "",
  start_year: "",
  end_month: "",
  end_year: "",
  current: false,
  description: "",
});
export const emptyEducation = (): EducationEntry => ({
  institution: "",
  degree: "",
  field_of_study: "",
  gpa: "",
  start_month: "",
  start_year: "",
  end_month: "",
  end_year: "",
  current: false,
});
export const emptyProject = (): ProjectEntry => ({ name: "", description: "" });

function text(value: unknown): string {
  return value == null ? "" : String(value);
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(text) : [];
}
function entries<T>(value: unknown, blank: () => T): T[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const filled = { ...blank(), ...(entry as object) } as Record<string, unknown>;
    for (const key of Object.keys(filled)) {
      if (filled[key] == null) filled[key] = blank()[key as keyof T];
    }
    return filled as T;
  });
}

// A saved profile's columns to the form. Missing or null columns become empty text.
export function profileToForm(profile: Record<string, unknown> | null): ProfileForm {
  const source = profile ?? {};
  const form = {} as ProfileForm;
  for (const key of SCALARS) form[key] = text(source[key]);
  form.portfolio_links = strings(source.portfolio_links);
  form.skills = strings(source.skills);
  form.certifications = strings(source.certifications);
  form.work_experience = entries(source.work_experience, emptyWork);
  form.education = entries(source.education, emptyEducation);
  form.projects = entries(source.projects, emptyProject);
  return form;
}

export function draftToForm(draft: Draft): ProfileForm {
  const flat: Record<string, unknown> = { ...draft };
  for (const key of [...SCALARS, "portfolio_links"]) {
    flat[key] = (draft[key] as DraftScalar | undefined)?.value ?? null;
  }
  return profileToForm(flat);
}

// The form back into the draft, keeping each scalar's candidates and source so a reopened review
// still knows what the resume offered.
export function formToDraft(form: ProfileForm, draft: Draft): Draft {
  const next: Draft = { ...draft };
  for (const key of SCALARS) {
    const old = (draft[key] as DraftScalar | undefined) ?? {
      candidates: [],
      confidence: null,
      source: "none",
    };
    next[key] = { ...old, value: form[key] === "" ? null : form[key] };
  }
  const links = (draft.portfolio_links as DraftScalar | undefined) ?? {
    candidates: [],
    confidence: null,
    source: "regex",
  };
  next.portfolio_links = { ...links, value: form.portfolio_links };
  next.skills = form.skills;
  next.certifications = form.certifications;
  next.work_experience = form.work_experience;
  next.education = form.education;
  next.projects = form.projects;
  return next;
}

// The columns `PUT /api/profile` takes. An empty scalar is null; the server derives the first and last
// name and the location from these.
export function formToFields(form: ProfileForm): Record<string, unknown> {
  const fields: Record<string, unknown> = {};
  for (const key of SCALARS) {
    const value = form[key].trim();
    if (key === "full_name" || key === "email") {
      fields[key] = value;
    } else if (value === "") {
      fields[key] = null;
    } else {
      fields[key] = value;
    }
  }
  fields.portfolio_links = form.portfolio_links;
  fields.skills = form.skills;
  fields.certifications = form.certifications;
  fields.work_experience = form.work_experience;
  fields.education = form.education;
  fields.projects = form.projects;
  return fields;
}
