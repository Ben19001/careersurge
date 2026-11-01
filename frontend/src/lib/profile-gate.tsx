import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import type { Filters } from "@/components/filters-form";

// Whether onboarding is done: a profile is saved AND the search filters name at least one job title (Run now
// refuses `no_titles` without one). Until then the app is in onboarding, which reopens at the first missing
// step. The steps call `refresh` once they have saved what the gate looks at. `failed` is the reason the
// first check could not run.
const ProfileGateContext = createContext<{
  onboarded: boolean | null;
  failed: string | null;
  refresh: () => Promise<void>;
}>({
  onboarded: null,
  failed: null,
  refresh: async () => {},
});

export function ProfileGateProvider({ children }: { children: ReactNode }) {
  const [onboarded, setOnboarded] = useState<boolean | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const { profile } = await api<{ profile: unknown }>("/api/profile");
      const { settings } = await api<{ settings: { sourcing_filters: Filters } }>("/api/settings");
      const titles = settings.sourcing_filters.titles ?? [];
      setOnboarded(profile !== null && titles.length > 0);
      setFailed(null);
    } catch (caught) {
      setFailed((caught as Error).message);
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return (
    <ProfileGateContext.Provider value={{ onboarded, failed, refresh }}>
      {children}
    </ProfileGateContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useProfileGate() {
  return useContext(ProfileGateContext);
}
