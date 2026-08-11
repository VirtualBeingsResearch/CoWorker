export type BootstrapAdminTarget = {
  adminUrl: string;
  reconnectUrl: string;
  originChanged: boolean;
};

type ApiBinding = {
  host?: unknown;
  port?: unknown;
};

function browserHostname(bindHost: unknown, currentHostname: string) {
  if (typeof bindHost !== 'string') return currentHostname;
  const normalized = bindHost.trim();
  const unwrapped = normalized.startsWith('[') && normalized.endsWith(']')
    ? normalized.slice(1, -1)
    : normalized;
  if (!unwrapped || unwrapped === '0.0.0.0' || unwrapped === '::') return currentHostname;
  return unwrapped.includes(':') ? `[${unwrapped}]` : unwrapped;
}

export function createBootstrapReconnectProof() {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}

export function resolveBootstrapAdminTarget(
  currentHref: string,
  baselineApi: ApiBinding,
  desiredApi: ApiBinding,
): BootstrapAdminTarget {
  const current = new URL(currentHref);
  const target = new URL('/admin', current);
  const hostChanged = desiredApi.host !== baselineApi.host;
  const portChanged = desiredApi.port !== baselineApi.port;

  if (hostChanged) target.hostname = browserHostname(desiredApi.host, current.hostname);
  if (portChanged) {
    const port = Number(desiredApi.port);
    if (Number.isInteger(port) && port >= 1 && port <= 65_535) target.port = String(port);
  }

  return {
    adminUrl: target.href,
    reconnectUrl: new URL('/api/bootstrap/reconnect', target).href,
    originChanged: target.origin !== current.origin,
  };
}
