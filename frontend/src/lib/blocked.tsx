import { createContext, useContext, type ReactNode } from "react";
import { api, type Blocked } from "@/lib/api";
import { usePoll } from "@/lib/poll";

// The Needs you tray, polled every 3 s for the whole app so the nav badge and the screen agree.
const BlockedContext = createContext<{ blocked: Blocked[]; refresh: () => Promise<void> }>({
  blocked: [],
  refresh: async () => {},
});

export function BlockedProvider({ children }: { children: ReactNode }) {
  const { data, refresh } = usePoll(
    async () => (await api<{ blocked: Blocked[] }>("/api/blocked")).blocked,
    3000,
  );
  return (
    <BlockedContext.Provider value={{ blocked: data ?? [], refresh }}>
      {children}
    </BlockedContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useBlocked() {
  return useContext(BlockedContext);
}
