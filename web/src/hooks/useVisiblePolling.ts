import { useEffect, useRef, useState } from 'react';

interface PollingState<T> {
  data: T;
  error: string | null;
}

/**
 * Poll a JSON resource only while the page is visible. A stable JSON fingerprint
 * prevents unchanged responses from re-rendering the whole status page.
 */
export function useVisiblePolling<T>(
  load: () => Promise<T>,
  initialValue: T,
  intervalMs: number,
  fallbackError: string,
): PollingState<T> {
  const [data, setData] = useState<T>(initialValue);
  const [error, setError] = useState<string | null>(null);
  const fingerprintRef = useRef(JSON.stringify(initialValue));

  useEffect(() => {
    let active = true;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      clearTimeout(timer);
      if (active && document.visibilityState === 'visible') {
        timer = setTimeout(tick, intervalMs);
      }
    };

    const tick = async () => {
      if (!active || inFlight || document.visibilityState !== 'visible') return;
      inFlight = true;
      try {
        const next = await load();
        if (!active) return;
        const fingerprint = JSON.stringify(next);
        if (fingerprint !== fingerprintRef.current) {
          fingerprintRef.current = fingerprint;
          setData(next);
        }
        setError(null);
      } catch (e) {
        if (!active) return;
        setError(e instanceof Error ? e.message : fallbackError);
      } finally {
        inFlight = false;
        schedule();
      }
    };

    const onVisibilityChange = () => {
      clearTimeout(timer);
      if (document.visibilityState === 'visible') void tick();
    };

    document.addEventListener('visibilitychange', onVisibilityChange);
    if (document.visibilityState === 'visible') void tick();

    return () => {
      active = false;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [fallbackError, intervalMs, load]);

  return { data, error };
}
