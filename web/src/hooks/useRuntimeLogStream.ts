import { useEffect, useState } from 'react';
import { getRuntimeLogStreamUrl } from '../api/client';
import type { RuntimeLogEvent } from '../api/types';
import { t } from '../i18n/admin';

const MAX_EVENTS = 80;
const RECONNECT_DELAY_MS = 3000;

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
 * 运行日志数据源：订阅后端 /logs/stream（InteractionLogger → RuntimeEventCollector 的
 * SSE 实时流）。连接时先收到历史回放、随后实时推送。需要通信令牌时通过 fetch 携带
 * Authorization（EventSource 无法设置 Header），并保留自动重连。
 * 按 seq 去重后追加，只保留最近 MAX_EVENTS 条（与表示层的事件流上限一致）。
 */
export function useRuntimeLogStream(
  communicationToken = '',
  communicationTokenRequired = false,
) {
  const [events, setEvents] = useState<RuntimeLogEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;

    setError(null);

    if (communicationTokenRequired && !communicationToken) {
      setError(t('需要通信令牌后查看运行日志'));
      return () => {
        disposed = true;
      };
    }

    const appendEvent = (parsed: RuntimeLogEvent) => {
      setEvents(prev => {
        // 按 seq 去重：历史回放与实时流可能在连接窗口内重叠投递同一条
        if (parsed.seq != null && prev.some(e => e.seq === parsed.seq)) return prev;
        return [...prev.slice(-(MAX_EVENTS - 1)), parsed];
      });
    };

    const connect = async () => {
      if (disposed) return;
      try {
        const response = await fetch(getRuntimeLogStreamUrl(), {
          headers: communicationToken
            ? { Authorization: `Bearer ${communicationToken}` }
            : undefined,
        });
        if (!response.ok) {
          setError(
            response.status === 401
              ? t('通信令牌无效，请重新输入。')
              : t('请求失败 {{status}}', { status: String(response.status) }),
          );
          return;
        }
        if (!response.body) {
          setError(t('请求失败 {{status}}', { status: 'empty-body' }));
          return;
        }

        reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        setError(null);

        while (!disposed) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let boundary = buffer.search(/\r?\n\r?\n/);
          while (boundary >= 0) {
            const rawEvent = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '');
            for (const line of rawEvent.split(/\r?\n/)) {
              if (!line.startsWith('data:')) continue;
              const parsed = parseEvent(line.slice(5).trimStart());
              if (parsed) appendEvent(parsed);
            }
            boundary = buffer.search(/\r?\n\r?\n/);
          }
        }

        if (!disposed) {
          setError(t('日志流连接异常，正在自动重连…'));
          reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
        }
      } catch {
        if (!disposed) {
          setError(t('日志流连接异常，正在自动重连…'));
          reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
        }
      }
    };

    void connect();

    return () => {
      disposed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (reader) reader.cancel().catch(() => {});
    };
  }, [communicationToken, communicationTokenRequired]);

  return { events, error };
}
