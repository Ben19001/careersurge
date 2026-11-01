import { useEffect, useState, type ReactNode } from "react";
import { api, type ApiError } from "@/lib/api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";

type Flags = {
  auto_consent_authorized: boolean;
  background_check_authorized: boolean;
  arbitration_authorized: boolean;
};

const NO_FLAGS: Flags = {
  auto_consent_authorized: false,
  background_check_authorized: false,
  arbitration_authorized: false,
};

// The profile column holding the wording version each flag was ticked under.
const VERSION_COLUMNS: Record<keyof Flags, string> = {
  auto_consent_authorized: "consent_tos_version",
  background_check_authorized: "background_check_tos_version",
  arbitration_authorized: "arbitration_tos_version",
};

const STALE_NOTE = "The wording changed since you agreed. Tick again to agree to this text.";

// The three auto-agree permissions, shared by the onboarding Permissions step and the Profile
// screen. It loads the saved flags, saves them on its own button through PUT /api/profile/consent, and
// leaves what happens next to the caller: `onSaved` runs after a save, `extraActions` sits beside the button.
export function ConsentBlock({
  submitLabel,
  onSaved,
  extraActions,
}: {
  submitLabel: string;
  onSaved?: () => void;
  extraActions?: ReactNode;
}) {
  const [flags, setFlags] = useState<Flags | null>(null);
  // Flags saved as agreed but under older wording: shown unticked, with STALE_NOTE, until ticked again.
  const [stale, setStale] = useState<Flags>(NO_FLAGS);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{
      profile: Record<string, unknown> | null;
      consent_versions: Record<keyof Flags, string>;
    }>("/api/profile")
      .then(({ profile, consent_versions }) => {
        // A flag counts as agreed only under the wording now in force, as the filler reads it.
        const agreed = { ...NO_FLAGS };
        const outdated = { ...NO_FLAGS };
        for (const flag of Object.keys(NO_FLAGS) as (keyof Flags)[]) {
          const set = Boolean(profile?.[flag]);
          const current = profile?.[VERSION_COLUMNS[flag]] === consent_versions[flag];
          agreed[flag] = set && current;
          outdated[flag] = set && !current;
        }
        // As in `change`: without a current general agreement the two narrower boxes show unticked too, so
        // saving clears them. Intended: they only widen the general one, and it fails closed.
        setFlags(agreed.auto_consent_authorized ? agreed : NO_FLAGS);
        setStale(outdated);
      })
      .catch((caught: Error) => setProblem(caught.message));
  }, []);

  if (!flags) {
    return problem ? (
      <Alert variant="destructive">
        <AlertDescription>{problem}</AlertDescription>
      </Alert>
    ) : (
      <p className="text-muted-foreground">Loading…</p>
    );
  }

  function change(next: Flags) {
    // Background checks and arbitration widen the general permission, so they cannot stay ticked without it.
    setFlags(next.auto_consent_authorized ? next : NO_FLAGS);
    setSaved(false);
  }

  async function save(current: Flags) {
    setProblem(null);
    setBusy(true);
    try {
      await api("/api/profile/consent", current, "PUT");
      // Saving re-stamps every ticked flag with the current wording and clears every unticked one.
      setStale(NO_FLAGS);
      setSaved(true);
      onSaved?.();
    } catch (caught) {
      setProblem((caught as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4" data-testid="consent-block">
      <div className="rounded-lg border p-4">
        <label className="flex cursor-pointer items-start gap-3">
          <Checkbox
            className="mt-0.5"
            data-testid="consent-general"
            checked={flags.auto_consent_authorized}
            onCheckedChange={(on) => change({ ...flags, auto_consent_authorized: on === true })}
          />
          <span className="text-sm">
            I let this app, running on my computer, submit job applications for me and affirm
            standard application statements in my name - that the information I provided is true and
            accurate, that employers may contact my references, and the usual privacy and
            equal-opportunity acknowledgements. I am responsible for every answer it sends.
            <span className="mt-1 block text-xs text-muted-foreground">
              Optional. This does not cover arbitration - that is the separate box below. The app
              never agrees to non-competes, IP assignment or signatures for you; those come to you
              to answer.
            </span>
          </span>
        </label>
        {stale.auto_consent_authorized && !flags.auto_consent_authorized && (
          <p className="mt-3 text-sm" data-testid="consent-stale-general">
            {STALE_NOTE}
          </p>
        )}
        {!flags.auto_consent_authorized && (
          <p
            className="mt-3 text-sm text-amber-700 dark:text-amber-400"
            data-testid="consent-warning"
          >
            Most application forms ask you to confirm your details are true. Without this, each of
            those jobs waits for you in Needs you.
          </p>
        )}
      </div>

      <div className="rounded-lg border p-4">
        <h3 className="text-sm font-semibold">
          Background checks <span className="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <label className="mt-3 flex cursor-pointer items-start gap-3">
          <Checkbox
            className="mt-0.5"
            data-testid="consent-background"
            disabled={!flags.auto_consent_authorized}
            checked={flags.background_check_authorized}
            onCheckedChange={(on) => change({ ...flags, background_check_authorized: on === true })}
          />
          <span className="text-sm">
            I let this app affirm, in my name, an employer's request to obtain a consumer report or
            background screening about me as part of an application.
            <span className="mt-1 block text-xs text-muted-foreground">
              Separate and optional. Leave this unchecked and those requests wait for you to answer
              them yourself. Needs the box above.
            </span>
          </span>
        </label>
        {stale.background_check_authorized && !flags.background_check_authorized && (
          <p className="mt-3 text-sm" data-testid="consent-stale-background">
            {STALE_NOTE}
          </p>
        )}
      </div>

      <div className="rounded-lg border p-4">
        <h3 className="text-sm font-semibold">
          Arbitration agreements{" "}
          <span className="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <label className="mt-3 flex cursor-pointer items-start gap-3">
          <Checkbox
            className="mt-0.5"
            data-testid="consent-arbitration"
            disabled={!flags.auto_consent_authorized}
            checked={flags.arbitration_authorized}
            onCheckedChange={(on) => change({ ...flags, arbitration_authorized: on === true })}
          />
          <span className="text-sm">
            I let this app accept, in my name, an employer's binding-arbitration agreement when
            their application requires one.
            <span className="mt-1 block text-xs text-muted-foreground">
              Accepting one usually means giving up your right to sue that employer in court and to
              join a class action against them, including over hiring discrimination. Separate and
              optional. Leave it unchecked and these wait for you to read and answer yourself. Needs
              the first box.
            </span>
          </span>
        </label>
        {stale.arbitration_authorized && !flags.arbitration_authorized && (
          <p className="mt-3 text-sm" data-testid="consent-stale-arbitration">
            {STALE_NOTE}
          </p>
        )}
      </div>

      {problem && (
        <Alert variant="destructive" data-testid="step-problem">
          <AlertDescription>{problem}</AlertDescription>
        </Alert>
      )}
      <div className="flex items-center gap-3">
        <Button disabled={busy} onClick={() => save(flags)}>
          {submitLabel}
        </Button>
        {extraActions}
        {saved && (
          <span className="text-sm text-muted-foreground" data-testid="consent-saved">
            Saved
          </span>
        )}
      </div>
    </div>
  );
}
