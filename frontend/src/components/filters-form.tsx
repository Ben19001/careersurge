import { Checkbox } from "@/components/ui/checkbox";
import { Field, TagListEditor } from "@/components/profile-editors";

// The search filters of `settings['sourcing_filters']` (backend: `_OPTIONAL_FILTERS` in local_sourcing.py).
// Only titles are required; every other filter left empty is not sent to Apify.
export type Filters = {
  titles?: string[];
  locations?: string[];
  employment_types?: string[];
  work_arrangements?: string[];
  experience_years?: string[];
  title_exclusions?: string[];
  companies?: string[];
  company_exclusions?: string[];
  visa_sponsorship?: boolean;
  only_with_pay?: boolean;
};

const EMPLOYMENT_TYPES: [string, string][] = [
  ["FULL_TIME", "Full-time"],
  ["PART_TIME", "Part-time"],
  ["CONTRACTOR", "Contract"],
  ["INTERN", "Internship"],
  ["TEMPORARY", "Temporary"],
  ["VOLUNTEER", "Volunteer"],
  ["PER_DIEM", "Per diem"],
  ["OTHER", "Other"],
];
const WORK_ARRANGEMENTS: [string, string][] = [
  ["On-site", "On-site"],
  ["Hybrid", "Hybrid"],
  ["Remote OK", "Remote OK"],
  ["Remote Solely", "Remote only"],
];
const EXPERIENCE_YEARS: [string, string][] = [
  ["0-2", "0-2 years"],
  ["2-5", "2-5 years"],
  ["5-10", "5-10 years"],
  ["10+", "10+ years"],
];

function CheckGroup({
  label,
  options,
  values,
  onChange,
}: {
  label: string;
  options: [string, string][];
  values: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="space-y-1">
      <legend className="text-sm font-medium">{label}</legend>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {options.map(([value, text]) => (
          <label key={value} className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox
              checked={values.includes(value)}
              onCheckedChange={(checked) =>
                onChange(checked ? [...values, value] : values.filter((other) => other !== value))
              }
            />
            {text}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export function FiltersForm({
  filters,
  onChange,
  titlesError,
}: {
  filters: Filters;
  onChange: (next: Filters) => void;
  titlesError?: string | null;
}) {
  return (
    <div className="space-y-5">
      <Field
        label="Job titles (required)"
        hint="Be specific: a title matches any posting that contains it."
        error={titlesError}
        htmlFor="titles"
      >
        <TagListEditor
          label="Add a job title"
          values={filters.titles ?? []}
          placeholder="e.g. Backend Engineer"
          onChange={(titles) => onChange({ ...filters, titles })}
        />
      </Field>
      <Field
        label="Locations"
        hint="Spell names out: a city (Austin), or city, state, country (Austin, Texas, United States). Leave empty for the whole United States."
      >
        <TagListEditor
          label="Add a location"
          values={filters.locations ?? []}
          placeholder="e.g. Austin, Texas, United States"
          splitOnComma={false}
          onChange={(locations) => onChange({ ...filters, locations })}
        />
      </Field>
      <CheckGroup
        label="Employment types (none selected means full-time)"
        options={EMPLOYMENT_TYPES}
        values={filters.employment_types ?? []}
        onChange={(employment_types) => onChange({ ...filters, employment_types })}
      />
      <CheckGroup
        label="Work arrangement"
        options={WORK_ARRANGEMENTS}
        values={filters.work_arrangements ?? []}
        onChange={(work_arrangements) => onChange({ ...filters, work_arrangements })}
      />
      <CheckGroup
        label="Experience"
        options={EXPERIENCE_YEARS}
        values={filters.experience_years ?? []}
        onChange={(experience_years) => onChange({ ...filters, experience_years })}
      />
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Checkbox
            checked={filters.visa_sponsorship ?? false}
            onCheckedChange={(checked) =>
              onChange({ ...filters, visa_sponsorship: checked === true })
            }
          />
          Only jobs that sponsor visas
        </label>
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Checkbox
            checked={filters.only_with_pay ?? false}
            onCheckedChange={(checked) => onChange({ ...filters, only_with_pay: checked === true })}
          />
          Only jobs that list pay
        </label>
      </div>
      <Field label="Skip titles containing">
        <TagListEditor
          label="Add a title to skip"
          values={filters.title_exclusions ?? []}
          placeholder="e.g. Intern"
          onChange={(title_exclusions) => onChange({ ...filters, title_exclusions })}
        />
      </Field>
      <Field label="Only these companies">
        <TagListEditor
          label="Add a company"
          values={filters.companies ?? []}
          placeholder="Company name"
          onChange={(companies) => onChange({ ...filters, companies })}
        />
      </Field>
      <Field label="Never these companies">
        <TagListEditor
          label="Add a company to skip"
          values={filters.company_exclusions ?? []}
          placeholder="Company name"
          onChange={(company_exclusions) => onChange({ ...filters, company_exclusions })}
        />
      </Field>
    </div>
  );
}
