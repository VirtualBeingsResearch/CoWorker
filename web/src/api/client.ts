import type { FullStatus, ProfileInfo } from './types';

const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');

/** 对话的下行通道：EventSource 负责接收搭档的实时回复并自动重连。 */
export function getChatEventStreamUrl(participantId: string): string {
  return `${API_BASE}/sse/${encodeURIComponent(participantId)}`;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const detail = await response.json().then(body => body.detail || body.message).catch(() => response.statusText);
    throw new Error(detail || `请求失败：${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function postMessage(payload: {
  sender_id: string;
  content: string;
  conversation_id?: string;
}) {
  return requestJson<{ status: string; sender_id: string; conversation_id?: string }>('/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function switchModel(payload: { provider: string; model_id?: string }) {
  return requestJson<{ status: string; provider: string; model_id: string }>('/switch_model', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** 完整状态（身份证正面身份 + 生命体征的数据源）。后端动态计算 age_days。 */
export function getStatus() {
  return requestJson<FullStatus>('/status');
}

/** Agent 基础档案：身份、目标、最早记忆时间戳。变化慢，建议低频轮询。 */
export function getProfile() {
  return requestJson<ProfileInfo>('/profile');
}

/** 运行日志 SSE 流（身份证背面运行日志的数据源）。同源部署留空 API_BASE，走 Vite /logs 代理。 */
export function getRuntimeLogStreamUrl(): string {
  return `${API_BASE}/logs/stream`;
}
