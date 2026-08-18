import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const credentials = await readFile(
  new URL('../src/lib/browserCredentials.ts', import.meta.url),
  'utf8',
);
const app = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8');
const chatDock = await readFile(new URL('../src/components/ChatDock.tsx', import.meta.url), 'utf8');
const adminApp = await readFile(new URL('../src/admin/AdminApp.tsx', import.meta.url), 'utf8');

test('browser password credentials use stable, distinct account identifiers', () => {
  assert.match(credentials, /ADMIN_CREDENTIAL_USERNAME = 'coworker-admin'/);
  assert.match(credentials, /COMMUNICATION_CREDENTIAL_USERNAME = 'coworker-communication'/);
  assert.match(credentials, /section-coworker-admin username/);
  assert.match(credentials, /section-coworker-admin current-password/);
  assert.match(credentials, /section-coworker-communication username/);
  assert.match(credentials, /section-coworker-communication current-password/);
});

test('admin and communication forms declare their respective browser credentials', () => {
  assert.match(adminApp, /value=\{ADMIN_CREDENTIAL_USERNAME\}/);
  assert.match(adminApp, /autoComplete=\{ADMIN_PASSWORD_AUTOCOMPLETE\}/);

  for (const source of [app, chatDock]) {
    assert.match(source, /value=\{COMMUNICATION_CREDENTIAL_USERNAME\}/);
    assert.match(source, /autoComplete=\{COMMUNICATION_PASSWORD_AUTOCOMPLETE\}/);
  }
});
