import {
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Maximize2, Minimize2, Pencil, Send, Trash2, UserRound, X } from 'lucide-react';
import { getChatEventStreamUrl, postMessage } from '../api/client';
import { t } from '../i18n/admin';

type ChatRole = 'user' | 'assistant';
type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'error';

type BubbleChatMeta = {
  id: string;
  kind: 'handoff' | 'reply';
  phase: 'start' | 'end' | null;
  resumed: boolean;
};

type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: number;
  bubble?: BubbleChatMeta | null;
};

type ChatProfile = {
  clientId: string;
  name: string;
};

const CHAT_PROFILE_STORAGE_KEY = 'coworker-web-chat-profile';
const LEGACY_USER_NAME_STORAGE_KEY = 'coworker-web-chat-user-name';
const CHAT_HISTORY_PREFIX = 'coworker-web-chat-history:';
const MAX_STORED_MESSAGES = 160;
const BUBBLE_REPLY_PREFIX = '🫧 泡泡：';
const COMPACT_ID_RE = /^[A-Za-z0-9_-]{12}$/;

function newId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = crypto.getRandomValues(new Uint8Array(9));
    return btoa(String.fromCharCode(...bytes))
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');
  }
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`.slice(-12);
}

function compactStoredClientId(clientId: string): string {
  if (COMPACT_ID_RE.test(clientId)) return clientId;
  const compactId = newId();
  try {
    const previousHistory = window.localStorage.getItem(historyKey(clientId));
    if (previousHistory && !window.localStorage.getItem(historyKey(compactId))) {
      window.localStorage.setItem(historyKey(compactId), previousHistory);
    }
  } catch {
    // 身份仍可缩短；旧历史保留在原 key 下，存储恢复后也不会被删除。
  }
  return compactId;
}

function normalizeName(value: string): string {
  return value.trim().replace(/\s+/g, ' ');
}

function readChatProfile(): ChatProfile {
  try {
    const raw = window.localStorage.getItem(CHAT_PROFILE_STORAGE_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const profile = parsed as Record<string, unknown>;
        if (typeof profile.clientId === 'string' && profile.clientId.trim()) {
          return {
            clientId: compactStoredClientId(profile.clientId.trim()),
            name: normalizeName(typeof profile.name === 'string' ? profile.name : ''),
          };
        }
      }
    }
  } catch {
    // 浏览器存储不可用或旧数据损坏时，仍可用一次性身份继续对话。
  }

  try {
    return {
      clientId: newId(),
      name: normalizeName(window.localStorage.getItem(LEGACY_USER_NAME_STORAGE_KEY) || ''),
    };
  } catch {
    return { clientId: newId(), name: '' };
  }
}

function persistChatProfile(profile: ChatProfile) {
  try {
    window.localStorage.setItem(CHAT_PROFILE_STORAGE_KEY, JSON.stringify(profile));
    // 兼容此前仅保存姓名的版本，避免升级后丢失已有资料。
    window.localStorage.setItem(LEGACY_USER_NAME_STORAGE_KEY, profile.name);
  } catch {
    // 无存储权限时，当前页面会话仍可继续。
  }
}

function historyKey(clientId: string): string {
  return `${CHAT_HISTORY_PREFIX}${clientId}`;
}

function readBubbleMetadata(value: unknown): BubbleChatMeta | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const bubble = value as Record<string, unknown>;
  const id = typeof bubble.id === 'string' ? bubble.id.trim() : '';
  const kind = bubble.kind === 'handoff' || bubble.kind === 'reply' ? bubble.kind : null;
  if (!id || !kind) return null;
  const phase = bubble.phase === 'start' || bubble.phase === 'end' ? bubble.phase : null;
  return { id, kind, phase, resumed: bubble.resumed === true };
}

function createMessage(
  role: ChatRole,
  content: string,
  bubble: BubbleChatMeta | null = null,
): ChatMessage {
  return { id: newId(), role, content, createdAt: Date.now(), bubble };
}

function loadChatHistory(clientId: string): ChatMessage[] {
  try {
    const raw = window.localStorage.getItem(historyKey(clientId));
    if (!raw) return [];

    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];

    return parsed.flatMap(item => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
      const message = item as Record<string, unknown>;
      if (
        typeof message.id !== 'string'
        || (message.role !== 'user' && message.role !== 'assistant')
        || typeof message.content !== 'string'
        || !message.content.trim()
      ) {
        return [];
      }

      return [{
        id: message.id,
        role: message.role as ChatRole,
        content: message.content,
        createdAt: typeof message.createdAt === 'number' && Number.isFinite(message.createdAt)
          ? message.createdAt
          : Date.now(),
        bubble: readBubbleMetadata(message.bubble),
      }];
    }).slice(-MAX_STORED_MESSAGES);
  } catch {
    return [];
  }
}

function participantIdFor(profile: ChatProfile): string {
  // 姓名通过首条消息共享；路由 ID 只需本机随机值，避免把展示信息反复放入模型上下文。
  return `w:${profile.clientId}`;
}

function readOutboundMessage(raw: unknown): { content: string; bubble: BubbleChatMeta | null } {
  const text = typeof raw === 'string' ? raw.trim() : String(raw ?? '').trim();
  if (!text) return { content: '', bubble: null };

  try {
    const decoded: unknown = JSON.parse(text);
    if (!decoded || typeof decoded !== 'object' || Array.isArray(decoded)) {
      return { content: text, bubble: null };
    }

    const payload = decoded as Record<string, unknown>;
    const extra = payload.extra && typeof payload.extra === 'object' && !Array.isArray(payload.extra)
      ? payload.extra as Record<string, unknown>
      : null;
    const bubble = readBubbleMetadata(extra?.bubble);
    const message = payload.message ?? payload.content;
    if (typeof message === 'string' && message.trim()) {
      return { content: message.trim(), bubble };
    }

    if (Array.isArray(payload.attachments) && payload.attachments.length) {
      return {
        content: t('收到了 {{count}} 个附件。', { count: payload.attachments.length }),
        bubble,
      };
    }
    return { content: '', bubble };
  } catch {
    return { content: text, bubble: null };
  }
}

function activeBubbleFromMessages(messages: ChatMessage[]): BubbleChatMeta | null {
  let active: BubbleChatMeta | null = null;
  for (const message of messages) {
    const bubble = message.bubble;
    if (!bubble || bubble.kind !== 'handoff') continue;
    if (bubble.phase === 'start') active = bubble;
    else if (bubble.phase === 'end' && active?.id === bubble.id) active = null;
  }
  return active;
}

function bubbleHandoffCopy(bubble: BubbleChatMeta): string {
  if (bubble.phase === 'end') return t('泡泡已结束，主线继续接手');
  return bubble.resumed ? t('泡泡再次接管会话') : t('泡泡已接管会话');
}

function connectionCopy(state: ConnectionState): string {
  switch (state) {
    case 'connected': return 'SSE 已接通';
    case 'connecting': return '正在接通 SSE';
    case 'reconnecting': return '正在重新接通';
    case 'error': return '暂时无法接通';
    default: return '等待接通';
  }
}

function localizedConnectionDetail(detail: string): string {
  const rejectionPrefix = '连接被拒绝：';
  return detail.startsWith(rejectionPrefix)
    ? t(rejectionPrefix) + detail.slice(rejectionPrefix.length)
    : t(detail);
}

function SignalMark() {
  return (
    <svg className="sprite-svg" viewBox="0 0 44 44" aria-hidden="true">
      <ellipse className="sprite-orbit" cx="22" cy="22" rx="17" ry="8.5" fill="none" stroke="currentColor" strokeWidth="1.25" opacity=".52" />
      <ellipse className="sprite-orbit slow" cx="22" cy="22" rx="8.5" ry="17" fill="none" stroke="currentColor" strokeWidth="1.25" opacity=".34" />
      <circle className="sprite-node" cx="22" cy="22" r="6" fill="currentColor" />
      <circle cx="22" cy="22" r="2.5" fill="var(--surface)" opacity=".92" />
      <circle className="sprite-node" cx="7.5" cy="22" r="2.15" fill="currentColor" opacity=".78" />
      <circle className="sprite-node" cx="35.5" cy="22" r="2.15" fill="currentColor" opacity=".78" />
    </svg>
  );
}

export function ChatDock({ counterpartName }: { counterpartName: string }) {
  const [profile, setProfile] = useState<ChatProfile>(() => readChatProfile());
  const [participantId, setParticipantId] = useState(() => participantIdFor(profile));
  const [messages, setMessages] = useState<ChatMessage[]>(() => loadChatHistory(profile.clientId));
  const [open, setOpen] = useState(false);
  const [isFullPage, setIsFullPage] = useState(false);
  const [shouldConnect, setShouldConnect] = useState(false);
  const [connectionGeneration, setConnectionGeneration] = useState(0);
  const [connection, setConnection] = useState<ConnectionState>('idle');
  const [connectionDetail, setConnectionDetail] = useState('');
  const [nameDraft, setNameDraft] = useState(profile.name);
  const [nameError, setNameError] = useState('');
  const [isProfileEditorOpen, setIsProfileEditorOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [pendingReplies, setPendingReplies] = useState(0);
  const [isSending, setIsSending] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const hasSharedNameRef = useRef(false);
  const userName = profile.name;
  const activeBubble = useMemo(() => activeBubbleFromMessages(messages), [messages]);

  useEffect(() => {
    persistChatProfile(profile);
  }, [profile]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        historyKey(profile.clientId),
        JSON.stringify(messages.slice(-MAX_STORED_MESSAGES)),
      );
    } catch {
      // 存储写入失败不影响在线收发。
    }
  }, [messages, profile.clientId]);

  useEffect(() => {
    if (!shouldConnect || !userName) return;

    let disposed = false;
    let rejected = false;
    let source: EventSource | null = null;

    setConnection('connecting');
    setConnectionDetail('正在建立 SSE 信号。');

    try {
      source = new EventSource(getChatEventStreamUrl(participantId));
    } catch {
      setConnection('error');
      setConnectionDetail('对话通道地址无效，请检查前端连接配置。');
      return;
    }

    eventSourceRef.current = source;

    source.onopen = () => {
      if (disposed || eventSourceRef.current !== source) {
        source?.close();
        return;
      }
      setConnection('connected');
      setConnectionDetail('');
    };

    source.onmessage = event => {
      if (disposed || eventSourceRef.current !== source) return;
      const { content, bubble } = readOutboundMessage(event.data);
      if (!content) return;

      if (content.startsWith('连接被拒绝：')) {
        rejected = true;
        setConnection('error');
        setConnectionDetail(content);
        source?.close();
        return;
      }

      setMessages(current => [
        ...current,
        createMessage('assistant', content, bubble),
      ].slice(-MAX_STORED_MESSAGES));
      if (bubble?.kind !== 'handoff') {
        setPendingReplies(current => Math.max(0, current - 1));
      }
    };

    source.onerror = () => {
      if (disposed || rejected || eventSourceRef.current !== source) return;
      if (source?.readyState === EventSource.CLOSED) {
        setConnection('error');
        setConnectionDetail('SSE 通道已关闭，请关闭后重新打开对话。');
        return;
      }
      setConnection('reconnecting');
      setConnectionDetail('信号短暂中断，浏览器正在自动重新接通。');
    };

    return () => {
      disposed = true;
      if (eventSourceRef.current === source) eventSourceRef.current = null;
      source?.close();
    };
  }, [connectionGeneration, participantId, shouldConnect, userName]);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      if (!userName || isProfileEditorOpen) nameInputRef.current?.focus();
      else composerRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [isProfileEditorOpen, open, userName]);

  useEffect(() => {
    if (!open || isProfileEditorOpen) return;
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    endRef.current?.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'end' });
  }, [isProfileEditorOpen, messages, open, pendingReplies]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (isFullPage) {
        setIsFullPage(false);
        return;
      }
      setOpen(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isFullPage, open]);

  useEffect(() => {
    if (!open || !isFullPage) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [isFullPage, open]);

  const streamIsReady = connection === 'connected'
    && eventSourceRef.current?.readyState === EventSource.OPEN;

  const sendMessage = async () => {
    const content = draft.trim();
    if (!content || !streamIsReady || isSending) {
      if (content && !streamIsReady) setConnectionDetail('还没有接通 SSE，请稍候再发送。');
      return;
    }

    const outgoing = hasSharedNameRef.current
      ? content
      : t('我叫{{name}}。\n\n{{content}}', { name: userName, content });
    const localMessage = createMessage('user', content);
    setMessages(current => [...current, localMessage].slice(-MAX_STORED_MESSAGES));
    setPendingReplies(current => current + 1);
    setDraft('');
    setIsSending(true);

    try {
      await postMessage({
        sender_id: participantId,
        content: outgoing,
      });
      hasSharedNameRef.current = true;
      setConnectionDetail('');
    } catch {
      setMessages(current => current.filter(message => message.id !== localMessage.id));
      setPendingReplies(current => Math.max(0, current - 1));
      setDraft(content);
      setConnectionDetail('消息未能送达，请检查连接后重试。');
    } finally {
      setIsSending(false);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void sendMessage();
  };

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void sendMessage();
  };

  const handleNameSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextName = normalizeName(nameDraft);
    if (!nextName) {
      setNameError('请先写下你的名字。');
      return;
    }

    const isNewIdentity = nextName !== userName;
    const nextProfile = { ...profile, name: nextName };
    persistChatProfile(nextProfile);
    setProfile(nextProfile);
    setNameDraft(nextName);
    setNameError('');
    setIsProfileEditorOpen(false);

    if (isNewIdentity) {
      setParticipantId(participantIdFor(nextProfile));
      setPendingReplies(0);
      hasSharedNameRef.current = false;
    }
    setConnectionGeneration(current => current + 1);
    setShouldConnect(true);
  };

  const openProfileEditor = () => {
    setNameDraft(userName);
    setNameError('');
    setIsProfileEditorOpen(true);
  };

  const clearStoredMessages = () => {
    if (!window.confirm(t('清除这个浏览器保存的全部聊天记录？此操作无法恢复。'))) return;
    setMessages([]);
    try {
      window.localStorage.removeItem(historyKey(profile.clientId));
    } catch {
      // 接下来的本次会话仍会保留为空白状态。
    }
  };

  const openChat = () => {
    setOpen(true);
    if (userName) setShouldConnect(true);
  };

  const status = t(connectionCopy(connection));
  const visibleConnectionDetail = localizedConnectionDetail(connectionDetail);

  return (
    <>
      <button
        type="button"
        className={`sprite-btn ${open ? 'open' : ''}`}
        onClick={openChat}
        aria-label={t('与 {{name}} 对话', { name: counterpartName })}
        aria-expanded={open}
        aria-controls="coworker-chat"
      >
        <span className="sprite-pulse" aria-hidden="true" />
        <SignalMark />
        <span className="sprite-greeting" aria-hidden="true">
          <b>{t('和 {{name}} 说句话', { name: counterpartName })}</b>
          <small>{t('把眼前的想法直接交给她。')}</small>
        </span>
      </button>

      <div
        className={`chat-overlay ${open ? 'open' : ''} ${isFullPage ? 'chat-overlay-page' : ''}`}
        onMouseDown={event => {
          if (!isFullPage && event.target === event.currentTarget) setOpen(false);
        }}
        aria-hidden={!open}
      >
        <section
          id="coworker-chat"
          className={`chat-modal ${userName ? '' : 'chat-modal-name'} ${isFullPage ? 'chat-modal-page' : ''}`}
          role="dialog"
          aria-modal="true"
          aria-labelledby="chat-title"
        >
          <header className="chat-header">
            <div>
              <p className="chat-eyebrow">
                {t(!userName ? '初次信号' : isProfileEditorOpen ? '资料信号' : isFullPage ? '全页信号' : '直连信号')}
              </p>
              <h2 id="chat-title">
                {!userName ? t('先告诉我该怎么称呼你') : isProfileEditorOpen ? t('修改你的名字') : t('和 {{name}} 说话', { name: counterpartName })}
              </h2>
            </div>
            <div className="chat-header-actions">
              <button
                type="button"
                className="chat-view-toggle"
                onClick={() => setIsFullPage(current => !current)}
                aria-label={isFullPage ? t('退出全页面聊天') : t('以全页面方式打开聊天')}
                aria-pressed={isFullPage}
                title={isFullPage ? t('退出全页面聊天') : t('全页面聊天')}
              >
                {isFullPage ? <Minimize2 size={15} aria-hidden="true" /> : <Maximize2 size={15} aria-hidden="true" />}
                <span>{isFullPage ? t('收起') : t('全页面聊天')}</span>
              </button>
              {userName && (
                <button type="button" className="chat-user-chip" onClick={openProfileEditor} aria-label={t('修改名字：{{name}}', { name: userName })}>
                  <UserRound size={14} aria-hidden="true" />
                  <span title={userName}>{userName}</span>
                  <Pencil size={12} aria-hidden="true" />
                </button>
              )}
              <button type="button" className="chat-close" onClick={() => setOpen(false)} aria-label={t('关闭对话')}>
                <X size={17} aria-hidden="true" />
              </button>
            </div>
          </header>

          {!userName ? (
            <div className="chat-name-step">
              <div className="chat-name-mark" aria-hidden="true">
                <SignalMark />
                <span>{t('你的名字，是这段对话的第一个信号')}</span>
              </div>
              <div className="chat-name-copy">
                <p>{t('先留一个称呼，{{name}} 就能更自然地和你说话。', { name: counterpartName })}</p>
              </div>
              <form className="chat-name-form" onSubmit={handleNameSubmit}>
                <label htmlFor="chat-user-name">{t('你的名字')}</label>
                <input
                  ref={nameInputRef}
                  id="chat-user-name"
                  type="text"
                  value={nameDraft}
                  onChange={event => {
                    setNameDraft(event.target.value);
                    if (nameError) setNameError('');
                  }}
                  placeholder={t('例如，小林')}
                  autoComplete="name"
                  maxLength={40}
                />
                {nameError && <p className="chat-name-error" role="alert">{t(nameError)}</p>}
                <button type="submit" className="chat-name-start" disabled={!nameDraft.trim()}>
                  <span>{t('开始对话')}</span>
                  <Send size={15} aria-hidden="true" />
                </button>
              </form>
              <p className="chat-name-note">{t('名字会用于建立这次连接；资料和界面聊天副本保存在此浏览器。')}</p>
            </div>
          ) : isProfileEditorOpen ? (
            <div className="chat-profile-editor">
              <div className="chat-profile-ident" aria-hidden="true">
                <span><UserRound size={17} /></span>
                <p>{t('这个名字会显示在这里，并作为新 SSE 连接身份的一部分。')}</p>
              </div>
              <form className="chat-profile-form" onSubmit={handleNameSubmit}>
                <label htmlFor="chat-profile-name">{t('你的名字')}</label>
                <input
                  ref={nameInputRef}
                  id="chat-profile-name"
                  type="text"
                  value={nameDraft}
                  onChange={event => {
                    setNameDraft(event.target.value);
                    if (nameError) setNameError('');
                  }}
                  autoComplete="name"
                  maxLength={40}
                />
                {nameError && <p className="chat-name-error" role="alert">{t(nameError)}</p>}
                <div className="chat-profile-actions">
                  <button type="submit" className="chat-name-start" disabled={!nameDraft.trim()}>
                    <span>{t('保存并重新连接')}</span>
                    <Send size={15} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="chat-profile-cancel"
                    onClick={() => {
                      setNameDraft(userName);
                      setNameError('');
                      setIsProfileEditorOpen(false);
                    }}
                  >
                    {t('取消')}
                  </button>
                </div>
              </form>
              <div className="chat-history-control">
                <div>
                  <strong>{t('本机聊天记录')}</strong>
                  <p>{t('已保存 {{count}} 条；修改名字后仍会保留在这个浏览器。', { count: messages.length })}</p>
                </div>
                <button type="button" className="chat-history-danger" onClick={clearStoredMessages}>
                  <Trash2 size={14} aria-hidden="true" />
                  {t('清除记录')}
                </button>
              </div>
              <p className="chat-profile-note">{t('此界面的聊天副本只保存在当前浏览器，不会同步到其他设备。')}</p>
            </div>
          ) : (
            <>
              <div className="chat-messages" aria-live="polite" aria-label={t('{{name}} 的对话记录', { name: counterpartName })}>
                {!messages.length && (
                  <div className="chat-empty">
                    <SignalMark />
                    <p>{t('把需要一起想的事、想完成的事，直接告诉 {{name}}。', { name: counterpartName })}</p>
                  </div>
                )}
                {messages.map(message => {
                  const bubble = message.bubble;
                  if (bubble?.kind === 'handoff') {
                    return (
                      <article className={`msg bubble-handoff phase-${bubble.phase || 'unknown'}`} key={message.id}>
                        <span className="bubble-handoff-mark" aria-hidden="true">🫧</span>
                        <span>{bubbleHandoffCopy(bubble)}</span>
                        <code title={bubble.id}>{bubble.id}</code>
                      </article>
                    );
                  }
                  const bubbleReply = bubble?.kind === 'reply';
                  const content = bubbleReply && message.content.startsWith(BUBBLE_REPLY_PREFIX)
                    ? message.content.slice(BUBBLE_REPLY_PREFIX.length).trimStart()
                    : message.content;
                  return (
                    <article
                      className={`msg ${message.role === 'user' ? 'user' : 'ai'}${bubbleReply ? ' bubble-reply' : ''}`}
                      key={message.id}
                    >
                      {bubbleReply && (
                        <span className="bubble-reply-label">
                          <span aria-hidden="true">🫧</span>
                          {t('泡泡直接回复')}
                          <code title={bubble.id}>{bubble.id}</code>
                        </span>
                      )}
                      <span>{content}</span>
                    </article>
                  );
                })}
                {pendingReplies > 0 && (
                  <div className="msg thinking" role="status">
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                    <span>{activeBubble ? t('泡泡正在处理') : t('正在组织回应')}</span>
                  </div>
                )}
                <div ref={endRef} />
              </div>

              <div className={`chat-tool chat-tool-${connection}`} role="status" aria-live="polite">
                <span className="chat-tool-statuses">
                  <span className="tool-live"><i className="live-dot" aria-hidden="true" />{status}</span>
                  {activeBubble && (
                    <span className="chat-bubble-live" title={activeBubble.id}>
                      <span aria-hidden="true">🫧</span>
                      {t('{{id}} 接管中', { id: activeBubble.id })}
                    </span>
                  )}
                </span>
                <span className="chat-session-note">{visibleConnectionDetail || t('回复通过 SSE 实时送达；界面副本保存在此浏览器。')}</span>
              </div>

              <form className="chat-composer" onSubmit={handleSubmit}>
                <label className="sr-only" htmlFor="chat-composer-input">{t('发送给 {{name}} 的消息', { name: counterpartName })}</label>
                <textarea
                  ref={composerRef}
                  id="chat-composer-input"
                  value={draft}
                  onChange={event => setDraft(event.target.value)}
                  onKeyDown={handleComposerKeyDown}
                  placeholder={streamIsReady ? t('想和 {{name}} 说什么？', { name: counterpartName }) : t('正在接通 SSE…')}
                  disabled={!streamIsReady || isSending}
                  rows={1}
                />
                <button type="submit" className="send-btn" disabled={!streamIsReady || isSending || !draft.trim()}>
                  <span>{isSending ? t('发送中') : t('发送')}</span>
                  <Send size={15} aria-hidden="true" />
                </button>
              </form>
            </>
          )}
        </section>
      </div>
    </>
  );
}
