import { useEffect, useState } from 'react';
import { getProfile } from '../api/client';
import type { ProfileInfo } from '../api/types';

const POLL_INTERVAL = 30_000;

export function useProfile() {
  const [data, setData] = useState<ProfileInfo>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      try {
        const next = await getProfile();
        if (!active) return;
        setData(next);
        setError(null);
      } catch (e) {
        if (!active) return;
        setError(e instanceof Error ? e.message : '档案接口异常');
      } finally {
        if (active) timer = setTimeout(tick, POLL_INTERVAL);
      }
    };
    tick();

    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);

  return { data, error };
}
