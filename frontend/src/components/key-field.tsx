import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/profile-editors";

// `rejected`: the service refused this key the last time the app used it (only Jev reports it).
type KeyStatus = { set: boolean; last4: string | null; rejected: boolean };

const REJECTED = "This key was refused the last time it was used. Check it and paste it again.";

// One API key: shows whether it is stored (last four characters only, the key never comes back) and
// saves a new one. `onStatus` tells a parent step whether both keys are in.
export function KeyField({
  name,
  label,
  help,
  onStatus,
}: {
  name: "APIFY_TOKEN" | "JEV_API_KEY";
  label: string;
  help: string;
  onStatus?: (set: boolean) => void;
}) {
  const [status, setStatus] = useState<KeyStatus | null>(null);
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<KeyStatus>(`/api/keys/${name}`)
      .then((loaded) => {
        setStatus(loaded);
        onStatus?.(loaded.set);
      })
      .catch((caught: Error) => setProblem(caught.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  async function save() {
    setBusy(true);
    setProblem(null);
    try {
      const saved = await api<KeyStatus>(`/api/keys/${name}`, { value }, "PUT");
      setStatus(saved);
      setValue("");
      onStatus?.(saved.set);
    } catch (caught) {
      setProblem((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Field
      label={label}
      htmlFor={name}
      hint={help}
      error={problem ?? (status?.rejected ? REJECTED : null)}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={name}
          type="password"
          autoComplete="off"
          className="min-w-[16rem] flex-1"
          placeholder={status?.set ? "Paste a new key to replace it" : "Paste your key"}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <Button type="button" disabled={busy || !value.trim()} onClick={save}>
          {status?.set ? "Replace key" : "Save key"}
        </Button>
        {status?.set && (
          <span className="text-sm text-muted-foreground" data-testid={`key-status-${name}`}>
            Saved, ends in {status.last4}
          </span>
        )}
      </div>
    </Field>
  );
}
