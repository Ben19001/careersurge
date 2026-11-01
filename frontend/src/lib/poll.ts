import { useCallback, useEffect, useRef, useState } from "react";

// Calls `load` now and every `everyMs` while mounted. `refresh` runs it again on demand (after an
// action). A failed poll keeps the last good data and reports the error.
export function usePoll<T>(load: () => Promise<T>, everyMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;

  const refresh = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (caught) {
      setError(caught as Error);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), everyMs);
    return () => window.clearInterval(timer);
  }, [refresh, everyMs]);

  return { data, error, refresh };
}
