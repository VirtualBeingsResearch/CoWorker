import { useEffect, useState } from 'react';
import { getStatus } from '../api/client';
import type { FullStatus } from '../api/types';

const POLL_INTERVAL = 5000;

/**
 * 身份证正面数据源：轮询后端 /api/status，回填身份（name/birth/team/age_days）与
 * 生命体征（activity_state/activity_label，驱动背景呼吸）。后端按当前日期动态计算
 * age_days，因此前端不再保留会随时间漂移的硬编码默认值。
 */
export function useStatus() {
  const [data, setData] = useState<FullStatus>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      try {
        const next = await getStatus();
        if (!active) return;
        setData(next);
        setError(null);
      } catch (e) {
        if (!active) return;
        setError(e instanceof Error ? e.message : '状态接口异常');
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
