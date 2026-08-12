import { getStatus } from '../api/client';
import type { FullStatus } from '../api/types';
import { useVisiblePolling } from './useVisiblePolling';

const POLL_INTERVAL = 15_000;

/**
 * 身份证正面数据源：轮询后端 /api/status，回填身份（name/birth/team/age_days）与
 * 生命体征（activity_state/activity_label，驱动背景呼吸）。后端按当前日期动态计算
 * age_days，因此前端不再保留会随时间漂移的硬编码默认值。
 */
export function useStatus() {
  return useVisiblePolling<FullStatus>(getStatus, {}, POLL_INTERVAL, '状态接口异常');
}
