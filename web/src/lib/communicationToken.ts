export const COMMUNICATION_TOKEN_STORAGE_KEY = 'coworker-web-communication-token';
const COMMUNICATION_TOKEN_CHANGE_EVENT = 'coworker-web-communication-token-change';

function readStored(storage: Storage): string {
  try {
    return storage.getItem(COMMUNICATION_TOKEN_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function writeStored(storage: Storage, token: string): boolean {
  try {
    if (token) storage.setItem(COMMUNICATION_TOKEN_STORAGE_KEY, token);
    else storage.removeItem(COMMUNICATION_TOKEN_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}

export function readCommunicationToken(): string {
  const stored = readStored(window.localStorage);
  if (stored) return stored;

  // 旧版本只写 sessionStorage。读到后迁到 localStorage，关闭页面后仍可恢复。
  const legacy = readStored(window.sessionStorage);
  if (!legacy) return '';
  if (writeStored(window.localStorage, legacy)) writeStored(window.sessionStorage, '');
  return legacy;
}

export function persistCommunicationToken(token: string) {
  const persisted = writeStored(window.localStorage, token);
  if (persisted || !token) writeStored(window.sessionStorage, '');
  else writeStored(window.sessionStorage, token);
  window.dispatchEvent(new CustomEvent(COMMUNICATION_TOKEN_CHANGE_EVENT, { detail: token }));
}

export function subscribeCommunicationToken(listener: (token: string) => void): () => void {
  const handler = (event: Event) => {
    listener((event as CustomEvent<string>).detail ?? readCommunicationToken());
  };
  window.addEventListener(COMMUNICATION_TOKEN_CHANGE_EVENT, handler);
  return () => window.removeEventListener(COMMUNICATION_TOKEN_CHANGE_EVENT, handler);
}
