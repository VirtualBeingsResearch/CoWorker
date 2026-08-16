export const COMMUNICATION_TOKEN_STORAGE_KEY = 'coworker-web-communication-token';
const COMMUNICATION_TOKEN_CHANGE_EVENT = 'coworker-web-communication-token-change';

export function readCommunicationToken(): string {
  try {
    return window.sessionStorage.getItem(COMMUNICATION_TOKEN_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

export function persistCommunicationToken(token: string) {
  try {
    if (token) window.sessionStorage.setItem(COMMUNICATION_TOKEN_STORAGE_KEY, token);
    else window.sessionStorage.removeItem(COMMUNICATION_TOKEN_STORAGE_KEY);
  } catch {
    // 敏感信息不落本地长期存储；无 sessionStorage 时仅保留在当前页面状态中。
  }
  window.dispatchEvent(new CustomEvent(COMMUNICATION_TOKEN_CHANGE_EVENT, { detail: token }));
}

export function subscribeCommunicationToken(listener: (token: string) => void): () => void {
  const handler = (event: Event) => {
    listener((event as CustomEvent<string>).detail ?? readCommunicationToken());
  };
  window.addEventListener(COMMUNICATION_TOKEN_CHANGE_EVENT, handler);
  return () => window.removeEventListener(COMMUNICATION_TOKEN_CHANGE_EVENT, handler);
}
