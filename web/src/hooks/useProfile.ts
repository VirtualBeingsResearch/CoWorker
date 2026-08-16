import { useEffect, useState } from 'react';
import { getProfile } from '../api/client';
import type { ProfileInfo } from '../api/types';
import { t } from '../i18n/admin';

const POLL_INTERVAL = 30_000;

export function useProfile(communicationToken = '', communicationTokenRequired = false) {
  const [data, setData] = useState<ProfileInfo>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;

    if (communicationTokenRequired && !communicationToken) {
      setError(t('需要通信令牌后查看身份档案'));
      return () => {
        active = false;
        clearTimeout(timer);
      };
    }

    const tick = async () => {
      try {
        const next = await getProfile(communicationToken);
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
  }, [communicationToken, communicationTokenRequired]);

  return { data, error };
}
