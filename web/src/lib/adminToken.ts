export const ADMIN_TOKEN_STORAGE_KEY = 'coworker-admin-token';

function readStored(storage: Storage): string {
  try {
    return storage.getItem(ADMIN_TOKEN_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function writeStored(storage: Storage, token: string): boolean {
  try {
    if (token) storage.setItem(ADMIN_TOKEN_STORAGE_KEY, token);
    else storage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}

export function readAdminToken(): string {
  const stored = readStored(window.localStorage);
  if (stored) return stored;

  // 旧版本只写 sessionStorage。读到后迁到 localStorage，关闭页面后仍可恢复。
  const legacy = readStored(window.sessionStorage);
  if (!legacy) return '';
  if (writeStored(window.localStorage, legacy)) writeStored(window.sessionStorage, '');
  return legacy;
}

export function persistAdminToken(token: string) {
  const persisted = writeStored(window.localStorage, token);
  if (persisted || !token) writeStored(window.sessionStorage, '');
  else writeStored(window.sessionStorage, token);
}

export function clearAdminToken() {
  persistAdminToken('');
}
