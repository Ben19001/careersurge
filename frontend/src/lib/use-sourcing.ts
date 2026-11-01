import { useState } from "react";
import { api, ApiError, type Progress, type Refusal } from "@/lib/api";
import { usePoll } from "@/lib/poll";

// The progress poll, plus Run now. `refusal` is the one the user just hit, else the one the scheduler stored.
export function useSourcing() {
  const { data: progress, refresh } = usePoll(() => api<Progress>("/api/progress"), 2000);
  const [live, setLive] = useState<Refusal | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  // True once the run was accepted (202); false when it was refused or failed.
  async function run(confirmBacklog = false): Promise<boolean> {
    setBusy(true);
    setProblem(null);
    try {
      await api("/api/sourcing/run", { confirm_backlog: confirmBacklog });
      setLive(null);
      return true;
    } catch (caught) {
      const payload = caught instanceof ApiError ? caught.payload : null;
      if (payload && typeof payload.reason === "string") {
        setLive({ reason: payload.reason, detail: (payload.detail as Refusal["detail"]) ?? {} });
      } else {
        setProblem((caught as Error).message);
      }
      return false;
    } finally {
      setBusy(false);
      await refresh();
    }
  }

  return {
    progress,
    refresh,
    refusal: live ?? progress?.sourcing_refusal ?? null,
    fetching: progress?.sourcing_run?.status === "started",
    busy,
    problem,
    run,
  };
}
