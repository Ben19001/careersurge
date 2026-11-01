import { useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { api } from "@/lib/api";
import { RefusalNotice } from "@/components/refusal-notice";
import { useSourcing } from "@/lib/use-sourcing";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { FiltersForm, type Filters } from "@/components/filters-form";
import { KeyField } from "@/components/key-field";
import { Field, Select } from "@/components/profile-editors";

// Mirrors the backend's floor (MIN_COOLDOWN_FLOOR_S in local_queues.py); the server checks it too.
const MIN_COOLDOWN_FLOOR_S = 30;

type Settings = {
  sourcing_filters: Filters;
  sourcing_time_range: string;
  sourcing_monthly_budget_usd: number;
  sourcing_confirmed_price_usd: number | null;
  sourcing_interval: string;
  apply_cooldown_floor_s: number;
};

type SettingsResponse = {
  settings: Settings;
  spent_this_month_usd: number;
  budget_resets_on: string;
};

// "2026-11-01" -> "Nov 1". Read as UTC so the user's time zone cannot shift the day.
function resetDay(isoDate: string): string {
  return new Date(`${isoDate}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export function SettingsScreen() {
  const [settings, setSettings] = useState<Settings | null>(null);
  // What this month has cost, from the same tally the server's budget guard refuses on.
  const [spend, setSpend] = useState<{ spent: number; budget: number; resetsOn: string } | null>(
    null,
  );
  const [saved, setSaved] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [budget, setBudget] = useState("");
  const [floor, setFloor] = useState("");
  const sourcing = useSourcing();
  const navigate = useNavigate();

  const refreshSpend = () =>
    api<SettingsResponse>("/api/settings")
      .then((loaded) =>
        setSpend({
          spent: loaded.spent_this_month_usd,
          budget: loaded.settings.sourcing_monthly_budget_usd,
          resetsOn: loaded.budget_resets_on,
        }),
      )
      .catch(() => {});

  useEffect(() => {
    api<SettingsResponse>("/api/settings")
      .then(({ settings: loaded, spent_this_month_usd, budget_resets_on }) => {
        setSettings(loaded);
        setSpend({
          spent: spent_this_month_usd,
          budget: loaded.sourcing_monthly_budget_usd,
          resetsOn: budget_resets_on,
        });
        setBudget(String(loaded.sourcing_monthly_budget_usd));
        setFloor(String(loaded.apply_cooldown_floor_s));
      })
      .catch((caught: Error) => setProblem(caught.message));
  }, []);

  if (!settings) {
    return problem ? (
      <Alert variant="destructive">
        <AlertDescription>{problem}</AlertDescription>
      </Alert>
    ) : (
      <p className="text-muted-foreground">Loading…</p>
    );
  }

  const change = (patch: Partial<Settings>) => {
    setSettings({ ...settings, ...patch });
    setSaved(false);
  };

  async function save(current: Settings): Promise<boolean> {
    setProblem(null);
    const monthly = Number(budget);
    const cooldown = Number(floor);
    if (!(monthly > 0)) {
      setProblem("The monthly budget must be a number above 0.");
      return false;
    }
    if (!(cooldown >= MIN_COOLDOWN_FLOOR_S)) {
      setProblem(`The cooldown floor must be at least ${MIN_COOLDOWN_FLOOR_S} seconds.`);
      return false;
    }
    try {
      await api(
        "/api/settings",
        {
          settings: {
            sourcing_filters: current.sourcing_filters,
            sourcing_time_range: current.sourcing_time_range,
            sourcing_monthly_budget_usd: monthly,
            sourcing_interval: current.sourcing_interval,
            apply_cooldown_floor_s: cooldown,
          },
        },
        "PUT",
      );
      setSaved(true);
      await refreshSpend();
      return true;
    } catch (caught) {
      setProblem((caught as Error).message);
      return false;
    }
  }

  // Run now, and accepting the price, land on Progress once the run has started, as onboarding does;
  // a refusal keeps the user here with its notice.
  async function runAndWatch(confirmBacklog = false) {
    const started = await sourcing.run(confirmBacklog);
    if (started) navigate({ to: "/progress" });
    else await refreshSpend();
    return started;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Settings</h1>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Keys</CardTitle>
          <p className="text-sm text-muted-foreground">
            Stored on this computer, in its keychain when it has one. They are never shown again
            after you save them.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <KeyField
            name="APIFY_TOKEN"
            label="Apify key"
            help="Finds the job listings. Apify bills your own account per job."
          />
          <KeyField
            name="JEV_API_KEY"
            label="Jev key"
            help="Reads your resume and matches form questions. If it is rejected, the app falls back to simpler matching."
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Job search</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <FiltersForm
            filters={settings.sourcing_filters}
            onChange={(sourcing_filters) => change({ sourcing_filters })}
          />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Time range"
              htmlFor="time-range"
              hint="Only jobs posted within this window. Automatic covers the time since the last search, so you never pay twice for the same job."
            >
              <Select
                label="Time range"
                placeholder="Automatic"
                options={[
                  ["1h", "Last hour"],
                  ["24h", "Last 24 hours"],
                  ["7d", "Last 7 days"],
                ]}
                value={settings.sourcing_time_range === "auto" ? "" : settings.sourcing_time_range}
                onChange={(value) => change({ sourcing_time_range: value || "auto" })}
              />
            </Field>
            <Field
              label="Monthly budget (USD)"
              htmlFor="budget"
              hint={
                spend
                  ? `$${spend.spent.toFixed(2)} of $${spend.budget.toFixed(2)} used this month. Resets on ${resetDay(spend.resetsOn)}.`
                  : "The most you are willing to spend on Apify this month."
              }
              error={
                spend && spend.spent >= spend.budget
                  ? `Budget used up. No searches until ${resetDay(spend.resetsOn)}, or raise the budget.`
                  : null
              }
            >
              <Input
                id="budget"
                type="number"
                min={0}
                step="0.5"
                value={budget}
                onChange={(event) => {
                  setBudget(event.target.value);
                  setSaved(false);
                }}
              />
            </Field>
            <Field
              label="Check for new jobs"
              htmlFor="interval"
              hint="How often the app searches for new jobs. Each search is charged to your Apify account, within the budget."
            >
              <select
                id="interval"
                value={settings.sourcing_interval}
                onChange={(event) => change({ sourcing_interval: event.target.value })}
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
              >
                <option value="manual">Only when I press Run now</option>
                <option value="4h">Every 4 hours</option>
                <option value="1h">Every hour</option>
              </select>
            </Field>
            <Field
              label="Minimum pause between applications (seconds)"
              htmlFor="floor"
              hint="On each site, the app waits at least this long, plus a random 0 to 2 minutes, after every application. Slow pages make it wait longer. 30 seconds at least."
            >
              <Input
                id="floor"
                type="number"
                min={MIN_COOLDOWN_FLOOR_S}
                value={floor}
                onChange={(event) => {
                  setFloor(event.target.value);
                  setSaved(false);
                }}
              />
            </Field>
          </div>
          {settings.sourcing_confirmed_price_usd !== null && (
            <p className="text-sm text-muted-foreground">
              Accepted Apify price: ${settings.sourcing_confirmed_price_usd} per job.
            </p>
          )}
          {problem && (
            <Alert variant="destructive">
              <AlertDescription>{problem}</AlertDescription>
            </Alert>
          )}
          <div className="flex items-center gap-3">
            <Button onClick={() => save(settings)}>Save settings</Button>
            {saved && (
              <span className="text-sm text-muted-foreground" data-testid="settings-saved">
                Saved
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Check for jobs now</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Saves the settings above, then checks. Progress shows the check; new jobs land on the
            Approve screen.
          </p>
          <RefusalNotice refusal={sourcing.refusal} busy={sourcing.busy} onRun={runAndWatch} />
          {sourcing.problem && (
            <Alert variant="destructive">
              <AlertDescription>{sourcing.problem}</AlertDescription>
            </Alert>
          )}
          <div className="flex items-center gap-3">
            <Button
              disabled={sourcing.busy || sourcing.fetching}
              onClick={async () => {
                if (await save(settings)) await runAndWatch();
              }}
            >
              Run now
            </Button>
            {sourcing.fetching && (
              <span className="text-sm text-muted-foreground" data-testid="fetching-jobs">
                Fetching jobs…
              </span>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
