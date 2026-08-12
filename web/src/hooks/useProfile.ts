import { getProfile } from '../api/client';
import type { ProfileInfo } from '../api/types';
import { useVisiblePolling } from './useVisiblePolling';

const POLL_INTERVAL = 30_000;

export function useProfile() {
  return useVisiblePolling<ProfileInfo>(getProfile, {}, POLL_INTERVAL, '档案接口异常');
}
