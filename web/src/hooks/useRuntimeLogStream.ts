import { useEffect, useState } from 'react';
import { getRuntimeLogStreamUrl } from '../api/client';
import type { RuntimeLogEvent } from '../api/types';

const MAX_EVENTS = 80;

function parseEvent(data: string): RuntimeLogEvent | null {
  const trimmed = data.trim();
  if (!trimmed) return null;
  try {
    const ev = JSON.parse(trimmed) as RuntimeLogEvent;
    return ev && typeof ev === 'object' ? ev : null;
  } catch {
    return null;
  }
}

/**
 * 运行日志数据源：订阅后端 /api/logs/stream（InteractionLogger → RuntimeEventCollector 的
 * SSE 实时流）。连接时先收到历史回放、随后实时推送。EventSource 原生自动重连。
 * 按 seq 去重后追加，只保留最近 MAX_EVENTS 条（与表示层的事件流上限一致）。
 */
export function useRuntimeLogStream() {
  const [events, setEvents] = useState<RuntimeLogEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    const source = new EventSource(getRuntimeLogStreamUrl());

    source.onopen = () => setError(null);

    source.onmessage = event => {
      const parsed = parseEvent(event.data);
      if (!parsed) return;
      setEvents(prev => {
        // 按 seq 去重：历史回放与实时流可能在连接窗口内重叠投递同一条
        if (parsed.seq != null && prev.some(e => e.seq === parsed.seq)) return prev;
        return [...prev.slice(-(MAX_EVENTS - 1)), parsed];
      });
    };

    source.onerror = () => {
      setError('日志流连接异常，正在自动重连…');
    };

    return () => source.close();
  }, []);

  return { events, error };
}
