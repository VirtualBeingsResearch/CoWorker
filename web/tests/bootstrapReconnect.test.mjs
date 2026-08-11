import assert from 'node:assert/strict';
import test from 'node:test';

import { createBootstrapReconnectProof, resolveBootstrapAdminTarget } from '../src/admin/bootstrapReconnect.ts';

test('keeps the current origin when the API binding is unchanged', () => {
  const target = resolveBootstrapAdminTarget(
    'http://localhost:8000/admin?step=setup',
    { host: '127.0.0.1', port: 8000 },
    { host: '127.0.0.1', port: 8000 },
  );

  assert.deepEqual(target, {
    adminUrl: 'http://localhost:8000/admin',
    reconnectUrl: 'http://localhost:8000/api/bootstrap/reconnect',
    originChanged: false,
  });
});

test('moves to the configured port after bootstrap', () => {
  const target = resolveBootstrapAdminTarget(
    'http://localhost:8000/admin',
    { host: '127.0.0.1', port: 8000 },
    { host: '127.0.0.1', port: 8124 },
  );

  assert.deepEqual(target, {
    adminUrl: 'http://localhost:8124/admin',
    reconnectUrl: 'http://localhost:8124/api/bootstrap/reconnect',
    originChanged: true,
  });
});

test('keeps the browser hostname for a wildcard bind address', () => {
  const target = resolveBootstrapAdminTarget(
    'http://coworker.local:8000/admin',
    { host: '127.0.0.1', port: 8000 },
    { host: '0.0.0.0', port: 8000 },
  );

  assert.deepEqual(target, {
    adminUrl: 'http://coworker.local:8000/admin',
    reconnectUrl: 'http://coworker.local:8000/api/bootstrap/reconnect',
    originChanged: false,
  });
});

test('moves to an explicitly changed host and supports IPv6 literals', () => {
  const target = resolveBootstrapAdminTarget(
    'http://localhost:8000/admin',
    { host: '127.0.0.1', port: 8000 },
    { host: '::1', port: 8124 },
  );

  assert.deepEqual(target, {
    adminUrl: 'http://[::1]:8124/admin',
    reconnectUrl: 'http://[::1]:8124/api/bootstrap/reconnect',
    originChanged: true,
  });
});

test('keeps a stable reverse-proxy URL when the internal bind port changes', () => {
  const target = resolveBootstrapAdminTarget(
    'https://coworker.example.com/admin',
    { host: '127.0.0.1', port: 8000, public_url: 'https://coworker.example.com' },
    { host: '0.0.0.0', port: 8124, public_url: 'https://coworker.example.com' },
  );

  assert.deepEqual(target, {
    adminUrl: 'https://coworker.example.com/admin',
    reconnectUrl: 'https://coworker.example.com/api/bootstrap/reconnect',
    originChanged: false,
  });
});

test('moves from a direct address to a newly configured public URL', () => {
  const target = resolveBootstrapAdminTarget(
    'http://localhost:8000/admin',
    { host: '127.0.0.1', port: 8000, public_url: '' },
    { host: '127.0.0.1', port: 8000, public_url: 'https://coworker.example.com/' },
  );

  assert.deepEqual(target, {
    adminUrl: 'https://coworker.example.com/admin',
    reconnectUrl: 'https://coworker.example.com/api/bootstrap/reconnect',
    originChanged: true,
  });
});

test('creates an unguessable reconnect proof accepted by the bootstrap API', () => {
  const first = createBootstrapReconnectProof();
  const second = createBootstrapReconnectProof();

  assert.match(first, /^[0-9a-f]{64}$/);
  assert.notEqual(first, second);
});
